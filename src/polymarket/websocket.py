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
        logger.info("watched wallets updated (count=%d)", len(self._watched))

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
                logger.info("websocket loop cancelled, exiting")
                raise
            except Exception as exc:  # noqa: BLE001 - we log and retry
                logger.warning(
                    "websocket session ended: %s; reconnecting in %.1fs",
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
        """Open one WS session: connect, subscribe, run heartbeat+consume."""
        logger.info("connecting to %s", self._url)
        async with websockets.connect(
            self._url,
            ping_interval=None,  # we manage our own app-level heartbeat
            close_timeout=5,
            max_size=2**20,  # 1 MiB
        ) as ws:
            logger.info("connected; subscribing to activity:trades")
            await self._subscribe(ws)

            heartbeat_task = asyncio.create_task(
                self._heartbeat(ws), name="rtds-heartbeat"
            )
            consume_task = asyncio.create_task(
                self._consume(ws), name="rtds-consume"
            )
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
                logger.info("disconnected from %s", self._url)

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
                    logger.error("received non-utf8 binary frame; skipping")
                    continue

            if not raw or raw == "pong":
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                # heartbeat acks or other plain-text frames — ok to skip quietly
                logger.debug("non-json frame: %r", raw[:120])
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error("failed to parse frame: %s; raw=%r", exc, raw[:200])
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

        wallet = payload.get("proxyWallet")
        if not isinstance(wallet, str):
            return

        if wallet.lower() not in self._watched:
            return

        logger.debug(
            "matched trade: wallet=%s side=%s size=%s price=%s tx=%s",
            wallet,
            payload.get("side"),
            payload.get("size"),
            payload.get("price"),
            payload.get("transactionHash"),
        )

        try:
            await self._on_trade(payload)
        except Exception:  # noqa: BLE001 - never let a callback kill the loop
            logger.exception("on_trade callback raised; continuing")


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
