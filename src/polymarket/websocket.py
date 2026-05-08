"""Polymarket RTDS WebSocket trades listener.

Standalone module — NOT integrated with the main runner loop yet.
Connects to Polymarket's Real-Time Data Service, subscribes to the global
``activity:trades`` topic, and invokes a callback when an incoming trade
involves any of the configured watched wallets.

Reference:
    - Endpoint: ``wss://ws-live-data.polymarket.com``
      (source: Polymarket/real-time-data-client repo, README + client.ts)
    - Subscription envelope::

          {"action": "subscribe",
           "subscriptions": [{"topic": "activity", "type": "trades"}]}

    - Heartbeat: literal string ``"ping"`` (text frame) every 5s max.
      We send every 4s to stay well within the limit.
    - Inbound message envelope::

          {"topic": "activity", "type": "trades", "timestamp": ...,
           "connection_id": "...", "payload": <Trade>}

    - Trade payload fields (per upstream README):
        asset, bio, conditionId, eventSlug, icon, name, outcome,
        outcomeIndex, price, profileImage, proxyWallet, pseudonym,
        side, size, slug, timestamp, title, transactionHash

    The wallet field of interest is ``proxyWallet``. The activity stream
    represents one side per event (the trader's perspective), so we only
    need to match against ``proxyWallet`` (case-insensitive).

    No authentication required for the activity topic.

Manual smoke test::

    python -m src.polymarket.websocket
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Awaitable, Callable, Iterable

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.copybot.ws_metrics import metrics as ws_metrics

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-live-data.polymarket.com"
SUBSCRIPTION = {
    "action": "subscribe",
    "subscriptions": [{"topic": "activity", "type": "trades"}],
}
HEARTBEAT_INTERVAL_S = 4.0  # upstream limit is 5s; stay under it
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 60.0
PING_FRAME = "ping"  # upstream expects literal text frame "ping"

TradeCallback = Callable[[dict], Awaitable[None]]


class PolymarketTradesWS:
    """Async WebSocket client for Polymarket RTDS ``activity:trades``.

    Subscribes to the global trades stream and filters client-side for
    a configurable set of wallet addresses, matching against the
    ``proxyWallet`` field of each trade payload (case-insensitive).
    Calls ``on_trade(payload)`` for every matched trade.

    The client owns its own reconnect loop with exponential backoff
    (1s -> 60s) and a heartbeat task that runs alongside the consumer.
    """

    def __init__(
        self,
        watched_wallets: Iterable[str],
        on_trade: TradeCallback,
        *,
        url: str = WS_URL,
    ) -> None:
        self._watched: set[str] = {w.lower() for w in watched_wallets if w}
        self._on_trade = on_trade
        self._url = url
        self._stop = asyncio.Event()

    # ----- public API -----

    def update_watched(self, wallets: Iterable[str]) -> None:
        """Hot-update the watched wallet set (case-insensitive)."""
        self._watched = {w.lower() for w in wallets if w}
        ws_metrics.set_watched(len(self._watched))
        logger.info("ws.watched_updated count=%d", len(self._watched))

    async def run(self) -> None:
        """Main loop: connect, subscribe, consume; reconnect on failure.

        Returns only when cancelled. Exponential backoff with jitter is
        applied between reconnect attempts.
        """
        backoff = RECONNECT_MIN_S
        while not self._stop.is_set():
            try:
                await self._connect()
                # successful clean session -> reset backoff
                backoff = RECONNECT_MIN_S
            except asyncio.CancelledError:
                logger.info("ws.loop_cancelled exiting")
                raise
            except Exception as exc:  # noqa: BLE001 - we log and retry
                ws_metrics.on_disconnect(reason=str(exc))
                logger.warning(
                    "ws.session_ended reason=%s reconnect_in=%.1fs",
                    exc,
                    backoff,
                )
                # jittered backoff
                sleep_for = backoff + random.uniform(0, backoff * 0.25)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                    return  # stop requested during backoff
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, RECONNECT_MAX_S)

    def stop(self) -> None:
        """Signal the run loop to exit on next iteration."""
        self._stop.set()

    # ----- internal -----

    async def _connect(self) -> None:
        """Open one WS session: connect, subscribe, run heartbeat+consume.

        Diagnóstico instrumentado (2026-05-06): el bot quedaba colgado en
        este método sin log adicional. Si el cuelgue vuelve, los INFO logs
        ahora indican exactamente la fase: pre-connect → post-connect →
        post-subscribe → tasks-armed → done. Si vemos "pre-connect" sin
        "post-connect" en >10s, es handshake (DNS/TLS/CF block). Si vemos
        "post-connect" sin "post-subscribe", es el frame de subscribe.
        Si vemos "post-subscribe" sin "tasks-armed", es asyncio.wait.

        `open_timeout=10`: si el handshake tarda >10s, websockets raises
        TimeoutError → outer loop loggea y reintenta. Antes el default
        no era explícito y en Lenovo+AR podía quedar colgado indef.
        """
        ws_metrics.on_connect_attempt()
        logger.info("ws.connect_attempt url=%s watched=%d",
                    self._url, len(self._watched))
        async with websockets.connect(
            self._url,
            ping_interval=None,  # heartbeat manual a nivel app
            open_timeout=10,
            close_timeout=5,
            max_size=2**20,  # 1 MiB
        ) as ws:
            ws_metrics.on_connect_success()
            ws_metrics.set_watched(len(self._watched))
            logger.info("ws.connected subscribing watched=%d", len(self._watched))
            await self._subscribe(ws)
            logger.info("ws.subscribed topic=activity:trades")

            heartbeat_task = asyncio.create_task(
                self._heartbeat(ws), name="rtds-heartbeat"
            )
            consume_task = asyncio.create_task(
                self._consume(ws), name="rtds-consume"
            )
            logger.info("ws.tasks_armed heartbeat+consume")
            try:
                done, pending = await asyncio.wait(
                    {heartbeat_task, consume_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                # surface the first exception, if any
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
            finally:
                for task in (heartbeat_task, consume_task):
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                ws_metrics.on_disconnect(reason="session_ended")
                logger.info("ws.disconnected url=%s", self._url)

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps(SUBSCRIPTION))
        logger.info(
            "subscribed: topic=activity type=trades (watching %d wallet(s))",
            len(self._watched),
        )

    async def _heartbeat(self, ws) -> None:
        """Send the literal ``ping`` text frame every HEARTBEAT_INTERVAL_S."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                await ws.send(PING_FRAME)
        except (ConnectionClosed, WebSocketException):
            # bubble up to _connect so the outer loop reconnects
            raise
        except asyncio.CancelledError:
            raise

    async def _consume(self, ws) -> None:
        """Read messages until the socket closes; dispatch matched trades."""
        async for raw in ws:
            # The server replies "pong" to our pings as a plain text frame.
            # Anything not parseable as JSON we just ignore at DEBUG.
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    logger.error("ws.binary_non_utf8 skipping")
                    continue

            if not raw:
                continue
            if raw == "pong":
                ws_metrics.on_pong()
                continue

            try:
                msg = json.loads(raw)
                ws_metrics.on_frame(json_ok=True)
            except json.JSONDecodeError:
                # heartbeat acks or other plain-text frames — ok to skip quietly
                ws_metrics.on_frame(json_ok=False)
                logger.debug("ws.non_json_frame %r", raw[:120])
                continue
            except Exception as exc:  # noqa: BLE001
                ws_metrics.on_frame(json_ok=False)
                logger.error("ws.parse_failed err=%s raw=%r", exc, raw[:200])
                continue

            await self._handle_message(msg)

    async def _handle_message(self, msg: dict) -> None:
        """Dispatch one decoded message: filter to activity:trades and match."""
        if not isinstance(msg, dict):
            return

        topic = msg.get("topic")
        mtype = msg.get("type")
        payload = msg.get("payload")

        # Some server frames may be acks/status without a payload — ignore.
        if topic != "activity" or mtype != "trades" or not isinstance(payload, dict):
            return

        ws_metrics.on_activity_frame()

        wallet = payload.get("proxyWallet")
        if not isinstance(wallet, str):
            return

        if wallet.lower() not in self._watched:
            return

        ts_payload = payload.get("timestamp")
        try:
            ts_int = int(ts_payload) if ts_payload is not None else None
        except (TypeError, ValueError):
            ts_int = None
        ws_metrics.on_match(ts_payload=ts_int)

        logger.info(
            "ws.match wallet=%s side=%s size=%s price=%s tx=%s",
            wallet[:10],
            payload.get("side"),
            payload.get("size"),
            payload.get("price"),
            (payload.get("transactionHash") or "")[:10],
        )

        try:
            await self._on_trade(payload)
            ws_metrics.on_callback_ok()
        except Exception:  # noqa: BLE001 - never let a callback kill the loop
            ws_metrics.on_callback_error()
            logger.exception("ws.callback_error continuing")


# --------------------------------------------------------------------------- #
# Manual smoke test:  python -m src.polymarket.websocket
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(
        level=_logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    async def print_trade(t: dict) -> None:
        print("MATCHED:", t)

    async def main() -> None:
        watched = {"0x12637e8ddfb9f4aa4b8f25ec96837fb0f50bb826"}  # example
        ws = PolymarketTradesWS(watched, print_trade)
        await ws.run()

    asyncio.run(main())
