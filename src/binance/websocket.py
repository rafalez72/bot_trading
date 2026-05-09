"""Binance Spot WebSocket — cliente para stream miniTicker.

Suscribe al stream público de tickers (no auth) y mantiene un cache
in-memory del último precio + timestamp por símbolo. Pensado para
estrategias que comparan spot vs Polymarket en tiempo real (ver
``src/copybot/crypto_arb.py``).

Endpoint: ``wss://stream.binance.com:9443/stream?streams=...``
Stream miniTicker: 1 mensaje/segundo por símbolo con close, open, high,
low, volume del último 24h. El campo ``c`` es el precio current.

Sample payload (multiplexed wrapper sobre el stream individual)::

    {"stream":"btcusdt@miniTicker","data":{
        "e":"24hrMiniTicker","E":1568657781950,"s":"BTCUSDT",
        "c":"9947.96","o":"...","h":"...","l":"...","v":"...","q":"..."
    }}

Diseño:
- Sin autenticación (data pública).
- Reconnect loop con exponential backoff (1s → 60s) + jitter.
- Binance maneja el keepalive con frames `ping/pong` automáticos a nivel
  WS — la lib `websockets` los responde sola, no necesitamos heartbeat
  manual (a diferencia del de Polymarket RTDS).
- ``last_prices`` es un dict thread-safe (asyncio runs single-threaded
  pero igual lo accedemos via getters que retornan copias).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Awaitable, Callable, Iterable

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)

WS_URL_BASE = "wss://stream.binance.com:9443/stream"
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 60.0
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# Tipo del callback: recibe (symbol, price, ts_ms).
TickCallback = Callable[[str, float, int], Awaitable[None]]


def _build_stream_url(symbols: Iterable[str]) -> str:
    parts = "/".join(f"{s.lower()}@miniTicker" for s in symbols)
    return f"{WS_URL_BASE}?streams={parts}"


class BinanceTickerWS:
    """Async client del miniTicker stream de Binance Spot.

    Mantiene ``last_prices`` con el último precio recibido por símbolo
    (``{"BTCUSDT": (price, ts_ms)}``). Llama al ``on_tick`` callback en
    cada actualización (útil para reaccionar a movimientos en tiempo
    real desde otra parte del bot).

    Uso típico::

        ws = BinanceTickerWS(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        task = asyncio.create_task(ws.run())
        await asyncio.sleep(2)
        print(ws.last_prices)        # {"BTCUSDT": (94000.5, 17782...)}
    """

    def __init__(
        self,
        symbols: Iterable[str] = DEFAULT_SYMBOLS,
        on_tick: TickCallback | None = None,
        *,
        url: str | None = None,
    ) -> None:
        self._symbols = tuple(s.upper() for s in symbols)
        self._url = url or _build_stream_url(self._symbols)
        self._on_tick = on_tick
        self._stop = asyncio.Event()
        # last_prices: simbolo → (precio, ts_ms) — el ts viene del payload
        # (E = event time epoch ms).
        self._last_prices: dict[str, tuple[float, int]] = {}
        # Counter de mensajes recibidos (para health checks)
        self._msg_count = 0
        self._connect_count = 0
        self._connected = False
        self._last_msg_at: float | None = None

    # ----- public API -----

    @property
    def last_prices(self) -> dict[str, tuple[float, int]]:
        """Copy del cache actual de precios — safe to inspect from outside."""
        return dict(self._last_prices)

    def get_price(self, symbol: str) -> tuple[float, int] | None:
        """Devuelve (price, ts_ms) del símbolo, o None si nunca vino."""
        return self._last_prices.get(symbol.upper())

    def health(self) -> dict:
        """Snapshot básico de estado del WS — útil para /api/ws-status."""
        return {
            "connected": self._connected,
            "symbols": list(self._symbols),
            "msg_count": self._msg_count,
            "connect_count": self._connect_count,
            "last_msg_at": self._last_msg_at,
            "since_last_msg_s": (
                round(time.time() - self._last_msg_at, 1)
                if self._last_msg_at else None
            ),
            "last_prices_count": len(self._last_prices),
        }

    async def run(self) -> None:
        """Loop principal. Cancelable con ``stop()`` o asyncio.Task.cancel()."""
        backoff = RECONNECT_MIN_S
        while not self._stop.is_set():
            try:
                await self._connect()
                backoff = RECONNECT_MIN_S
            except asyncio.CancelledError:
                logger.info("binance_ws.loop_cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "binance_ws.session_ended err=%s reconnect_in=%.1fs",
                    exc, backoff,
                )
                self._connected = False
                sleep_for = backoff + random.uniform(0, backoff * 0.25)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, RECONNECT_MAX_S)

    def stop(self) -> None:
        self._stop.set()

    # ----- internal -----

    async def _connect(self) -> None:
        logger.info(
            "binance_ws.connect_attempt url=%s symbols=%d",
            self._url, len(self._symbols),
        )
        async with websockets.connect(
            self._url,
            # Binance manda ping cada 3min, debemos responder en 10min.
            # `websockets` lib responde automáticamente — no necesitamos
            # heartbeat manual. Pero seteamos ping_interval=None para que
            # NOSOTROS no mandemos pings (Binance los rechaza).
            ping_interval=None,
            open_timeout=10,
            close_timeout=5,
            max_size=2**20,
        ) as ws:
            self._connected = True
            self._connect_count += 1
            logger.info(
                "binance_ws.connected (#%d) symbols=%s",
                self._connect_count, ",".join(self._symbols),
            )
            try:
                await self._consume(ws)
            finally:
                self._connected = False
                logger.info("binance_ws.disconnected")

    async def _consume(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            try:
                wrapper = json.loads(raw)
            except json.JSONDecodeError:
                continue
            data = wrapper.get("data") if isinstance(wrapper, dict) else None
            if not isinstance(data, dict):
                continue
            symbol = data.get("s")
            close_price = data.get("c")
            event_ts = data.get("E")
            if not symbol or close_price is None:
                continue
            try:
                price = float(close_price)
                ts_ms = int(event_ts) if event_ts else int(time.time() * 1000)
            except (TypeError, ValueError):
                continue
            self._last_prices[symbol.upper()] = (price, ts_ms)
            self._msg_count += 1
            self._last_msg_at = time.time()
            if self._on_tick:
                try:
                    await self._on_tick(symbol.upper(), price, ts_ms)
                except Exception:  # noqa: BLE001
                    logger.exception("binance_ws.on_tick callback raised")


# --------------------------------------------------------------------------- #
# Manual smoke test:  python -m src.binance.websocket
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    async def show(symbol: str, price: float, ts_ms: int) -> None:
        print(f"  {symbol}: ${price:,.2f}  ts={ts_ms}")

    async def main() -> None:
        ws = BinanceTickerWS(["BTCUSDT", "ETHUSDT", "SOLUSDT"], on_tick=show)
        task = asyncio.create_task(ws.run())
        await asyncio.sleep(15)
        print("--- last_prices ---")
        for k, (p, t) in ws.last_prices.items():
            print(f"  {k}: ${p:,.2f}")
        ws.stop()
        try:
            await asyncio.wait_for(task, timeout=3)
        except asyncio.TimeoutError:
            task.cancel()

    asyncio.run(main())
