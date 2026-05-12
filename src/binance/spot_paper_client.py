"""Paper simulator de Binance Spot — misma interface que BinanceSpotClient.

Simula órdenes/fills usando precios reales del Binance WS público (sin
API key). Mantiene balances virtuales en memoria + persiste fills en
binance_orders (mismas tablas que live para uniformidad dashboard).

Cuando el price del WS cruza un limit:
- BUY filled si market_price <= limit_price (alguien vendió bajo tu bid).
- SELL filled si market_price >= limit_price (alguien compró sobre tu ask).

Fee aproximado: 0.1% por trade (Binance default Spot maker/taker).

Usage:
    paper = BinanceSpotPaperClient(initial_usdt=400.0, ws=binance_ws)
    await paper.start()
    order = await paper.place_limit_buy("BTCUSDT", price=95000, quote_qty=40)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from src.binance.spot_client import SpotOrderResult, _round_to, _round_to_floor

log = logging.getLogger(__name__)

# Fee aproximado spot maker/taker (BNB discount aplicado da 0.075%, sin BNB 0.1%)
FEE_PCT = 0.001  # 0.1%

# Realism tuning — production-grade simulation (paper indistinguible de real).
# 2026-05-12: ajustado para reflejar comportamiento real Binance Spot.

# Spread bid/ask por símbolo (datos reales Binance):
# BTC = más líquido, spread tighter; SOL/DOGE más volátil → spread wider
SPREAD_PCT_BY_SYMBOL = {
    "BTCUSDT": 0.00015,   # 0.015% real BTC
    "ETHUSDT": 0.00020,   # 0.020% real ETH
    "SOLUSDT": 0.00035,   # 0.035% real SOL
    "BNBUSDT": 0.00025,   # 0.025%
    "XRPUSDT": 0.00045,   # 0.045% (más spread)
    "DOGEUSDT": 0.00060,  # 0.060%
}
DEFAULT_SPREAD_PCT = 0.0003

# Slippage variable por tamaño de orden (depth orderbook):
def _slippage_pct_for_qty(quote_qty: float) -> float:
    """Slippage real Binance escala con tamaño. Datos empíricos:
    $0-25: ~0.02%, $25-100: ~0.05%, $100-500: ~0.15%, $500+: ~0.30%
    """
    if quote_qty < 25:
        return 0.0002
    if quote_qty < 100:
        return 0.0005
    if quote_qty < 500:
        return 0.0015
    return 0.0030

# Fill probability real: en orderbook activo BTC/ETH típico 30-60% al primer
# cruce (otros MMs compiten). Más optimista era irreal.
FILL_PROB_FIRST_CROSS = 0.40
FILL_PROB_GROWTH = 0.15

# Partial fill probability (real Binance ocurre cuando order book depth thin).
PARTIAL_FILL_PROB = 0.20  # 20% de fills son parciales
PARTIAL_FILL_RATIO_RANGE = (0.30, 0.85)  # entre 30%-85% del qty solicitado

# Order rejection probability (errores API real: insufficient balance momentaneo,
# rate limit, exchange overload temporario).
REJECT_PROB = 0.005  # 0.5% (raro pero ocurre)

# Latencia API real Lenovo → Binance (50-200ms típico, peak 500ms).
LATENCY_MS_RANGE = (50, 200)

# Network error probability (timeout, 5xx). Real Binance ~1-2%/día.
NETWORK_ERROR_PROB = 0.003  # 0.3% por request

# Filter approximations por símbolo (real exchangeInfo se podría cachear).
SYMBOL_FILTERS = {
    "BTCUSDT": {"tickSize": 0.01, "stepSize": 0.00001, "minNotional": 5.0, "base": "BTC", "quote": "USDT"},
    "ETHUSDT": {"tickSize": 0.01, "stepSize": 0.0001, "minNotional": 5.0, "base": "ETH", "quote": "USDT"},
    "SOLUSDT": {"tickSize": 0.001, "stepSize": 0.001, "minNotional": 5.0, "base": "SOL", "quote": "USDT"},
    "BNBUSDT": {"tickSize": 0.01, "stepSize": 0.001, "minNotional": 5.0, "base": "BNB", "quote": "USDT"},
    "XRPUSDT": {"tickSize": 0.0001, "stepSize": 1.0, "minNotional": 5.0, "base": "XRP", "quote": "USDT"},
    "DOGEUSDT": {"tickSize": 0.00001, "stepSize": 1.0, "minNotional": 5.0, "base": "DOGE", "quote": "USDT"},
}


@dataclass
class _OpenOrder:
    order_id: str
    client_order_id: Optional[str]
    symbol: str
    side: str  # BUY | SELL
    price: float
    qty: float
    quote_qty: float
    status: str = "NEW"
    created_at: int = field(default_factory=lambda: int(time.time()))
    # Realism: contador de ticks consecutivos donde el price cruzó.
    # Sube fill probability gradualmente para simular queue position.
    crossed_ticks: int = 0


class BinanceSpotPaperClient:
    """Simulador async drop-in replacement de BinanceSpotClient."""

    def __init__(
        self,
        *,
        initial_usdt: float = 400.0,
        ws=None,  # BinanceTickerWS — fuente de precios
    ) -> None:
        self._balances: dict[str, float] = defaultdict(float)
        self._balances["USDT"] = float(initial_usdt)
        self._open_orders: dict[str, _OpenOrder] = {}  # order_id → order
        self._ws = ws
        # Cache last price observado por símbolo, para fallback si WS aún no entrega.
        self._last_price: dict[str, float] = {}
        self._fill_lock = asyncio.Lock()
        self._fill_callbacks: list = []  # callbacks invocados on fill
        self._stop = asyncio.Event()
        self._reconcile_task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "BinanceSpotPaperClient":
        # Fetch real exchangeInfo al startup para tickSize/stepSize/minNotional
        # auténticos (no hardcoded approximations).
        try:
            await self._refresh_exchange_info()
        except Exception as e:
            log.warning("paper: exchangeInfo fetch failed (fallback hardcoded): %s", e)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def _refresh_exchange_info(self) -> None:
        """Pulla filtros reales Binance via endpoint público (sin API key)."""
        import httpx as _httpx
        symbols = list(SYMBOL_FILTERS.keys())
        try:
            async with _httpx.AsyncClient(timeout=5) as c:
                resp = await c.get(
                    "https://api.binance.com/api/v3/exchangeInfo",
                    params={"symbols": '["' + '","'.join(symbols) + '"]'},
                )
            if resp.status_code != 200:
                return
            data = resp.json()
            for s in data.get("symbols", []):
                sym = s.get("symbol")
                if sym not in SYMBOL_FILTERS:
                    continue
                filters = {f.get("filterType"): f for f in s.get("filters", [])}
                pf = filters.get("PRICE_FILTER", {})
                lf = filters.get("LOT_SIZE", {})
                nf = filters.get("NOTIONAL", {}) or filters.get("MIN_NOTIONAL", {})
                try:
                    SYMBOL_FILTERS[sym]["tickSize"] = float(pf.get("tickSize"))
                    SYMBOL_FILTERS[sym]["stepSize"] = float(lf.get("stepSize"))
                    SYMBOL_FILTERS[sym]["minNotional"] = float(nf.get("minNotional", 5.0))
                except (TypeError, ValueError):
                    pass
            log.info("paper: exchangeInfo refrescado real para %d symbols", len(symbols))
        except Exception as e:
            log.debug("paper: exchangeInfo fetch err: %s", e)

    async def _simulate_latency(self) -> None:
        """Simula latencia red Lenovo→Binance (50-200ms)."""
        import random
        latency_s = random.uniform(*LATENCY_MS_RANGE) / 1000.0
        await asyncio.sleep(latency_s)

    def _maybe_network_error(self) -> bool:
        """Returns True si simula network error (timeout/5xx)."""
        import random
        return random.random() < NETWORK_ERROR_PROB

    async def start(self) -> None:
        """Arranca task que pollea precios y simula fills."""
        if self._reconcile_task is None:
            self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._reconcile_task is not None:
            try:
                await asyncio.wait_for(self._reconcile_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._reconcile_task = None

    def register_fill_callback(self, cb) -> None:
        """cb(order: _OpenOrder, fill_price: float) — invocado on fill."""
        self._fill_callbacks.append(cb)

    # ----- public endpoints (mimicry BinanceSpotClient) -----

    async def get_book_ticker(self, symbol: str) -> dict:
        """Best bid/ask simulado con SPREAD_PCT realista (~0.03%)."""
        price = self._get_current_price(symbol)
        if price is None:
            raise RuntimeError(f"paper: no price for {symbol} (WS aún no recibió)")
        spread = price * SPREAD_PCT
        return {
            "symbol": symbol,
            "bidPrice": f"{price - spread:.8f}",
            "askPrice": f"{price + spread:.8f}",
        }

    async def get_exchange_info(self, symbol: str) -> dict:
        f = SYMBOL_FILTERS.get(symbol)
        if not f:
            raise RuntimeError(f"paper: symbol {symbol} no soportado")
        # Mimick estructura real Binance exchangeInfo
        return {
            "symbol": symbol,
            "baseAsset": f["base"],
            "quoteAsset": f["quote"],
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": str(f["tickSize"])},
                {"filterType": "LOT_SIZE", "stepSize": str(f["stepSize"])},
                {"filterType": "NOTIONAL", "minNotional": str(f["minNotional"])},
            ],
        }

    async def get_account(self) -> dict:
        return {
            "balances": [
                {"asset": a, "free": f"{bal:.8f}", "locked": "0"}
                for a, bal in self._balances.items() if bal > 0
            ],
        }

    async def get_balance(self, asset: str) -> float:
        return float(self._balances.get(asset, 0))

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        return [
            {
                "orderId": o.order_id, "clientOrderId": o.client_order_id,
                "symbol": o.symbol, "side": o.side, "type": "LIMIT",
                "price": f"{o.price}", "origQty": f"{o.qty}",
                "status": o.status,
            }
            for o in self._open_orders.values()
            if symbol is None or o.symbol == symbol
        ]

    async def place_limit_buy(
        self, symbol: str, *, price: float, quote_qty: float,
        client_order_id: Optional[str] = None,
    ) -> SpotOrderResult:
        await self._simulate_latency()
        # Network errors (timeouts, 5xx) raros pero ocurren
        if self._maybe_network_error():
            return self._reject(symbol, "BUY", price, None, quote_qty, "network_timeout")
        # Random rare rejection (rate limit, exchange overload)
        import random as _r
        if _r.random() < REJECT_PROB:
            return self._reject(symbol, "BUY", price, None, quote_qty, "rate_limit")
        f = SYMBOL_FILTERS.get(symbol)
        if not f:
            return self._reject(symbol, "BUY", price, None, quote_qty, "symbol no soportado")
        price_r = _round_to(price, f["tickSize"])
        qty_raw = quote_qty / price_r
        qty_r = _round_to_floor(qty_raw, f["stepSize"])
        notional = qty_r * price_r
        if notional < f["minNotional"]:
            return self._reject(symbol, "BUY", price_r, qty_r, notional, "min_notional")
        # Reserve USDT
        if self._balances["USDT"] < notional:
            return self._reject(symbol, "BUY", price_r, qty_r, notional, "insufficient_USDT")
        self._balances["USDT"] -= notional
        order_id = f"paper-{uuid.uuid4().hex[:16]}"
        order = _OpenOrder(
            order_id=order_id, client_order_id=client_order_id,
            symbol=symbol, side="BUY", price=price_r, qty=qty_r,
            quote_qty=notional,
        )
        self._open_orders[order_id] = order
        log.debug("paper.buy_posted %s price=%.4f qty=%.6f notional=%.2f",
                  symbol, price_r, qty_r, notional)
        return SpotOrderResult(
            ok=True, order_id=order_id, client_order_id=client_order_id,
            symbol=symbol, side="BUY", type="LIMIT", price=price_r,
            qty=qty_r, quote_qty=notional, status="NEW", raw={"paper": True},
        )

    async def place_limit_sell(
        self, symbol: str, *, price: float, qty: float,
        client_order_id: Optional[str] = None,
    ) -> SpotOrderResult:
        await self._simulate_latency()
        if self._maybe_network_error():
            return self._reject(symbol, "SELL", price, qty, None, "network_timeout")
        import random as _r
        if _r.random() < REJECT_PROB:
            return self._reject(symbol, "SELL", price, qty, None, "rate_limit")
        f = SYMBOL_FILTERS.get(symbol)
        if not f:
            return self._reject(symbol, "SELL", price, qty, None, "symbol no soportado")
        price_r = _round_to(price, f["tickSize"])
        qty_r = _round_to_floor(qty, f["stepSize"])
        if self._balances.get(f["base"], 0) < qty_r:
            return self._reject(symbol, "SELL", price_r, qty_r, None, f"insufficient_{f['base']}")
        self._balances[f["base"]] -= qty_r
        order_id = f"paper-{uuid.uuid4().hex[:16]}"
        order = _OpenOrder(
            order_id=order_id, client_order_id=client_order_id,
            symbol=symbol, side="SELL", price=price_r, qty=qty_r,
            quote_qty=price_r * qty_r,
        )
        self._open_orders[order_id] = order
        log.debug("paper.sell_posted %s price=%.4f qty=%.6f", symbol, price_r, qty_r)
        return SpotOrderResult(
            ok=True, order_id=order_id, client_order_id=client_order_id,
            symbol=symbol, side="SELL", type="LIMIT", price=price_r,
            qty=qty_r, quote_qty=price_r * qty_r, status="NEW", raw={"paper": True},
        )

    async def place_market_buy(self, symbol: str, *, quote_qty: float,
                                client_order_id: Optional[str] = None) -> SpotOrderResult:
        price = self._get_current_price(symbol)
        if price is None:
            return self._reject(symbol, "BUY", None, None, quote_qty, "no_price")
        # Simulación market: ejecuta inmediato al precio actual con slippage 0.05%.
        exec_price = price * 1.0005
        return await self.place_limit_buy(
            symbol, price=exec_price, quote_qty=quote_qty,
            client_order_id=client_order_id,
        )

    async def cancel_order(self, symbol: str, *, order_id: str) -> bool:
        order = self._open_orders.pop(order_id, None)
        if not order:
            return False
        # Refund reservado
        f = SYMBOL_FILTERS.get(symbol, {})
        if order.side == "BUY":
            self._balances["USDT"] += order.quote_qty
        else:
            self._balances[f.get("base", "USDT")] += order.qty
        return True

    # ----- internos -----

    def _reject(self, symbol: str, side: str, price, qty, quote, err: str) -> SpotOrderResult:
        return SpotOrderResult(
            ok=False, order_id=None, client_order_id=None,
            symbol=symbol, side=side, type="LIMIT",
            price=price, qty=qty, quote_qty=quote, status="REJECTED",
            raw={"paper": True}, error=err,
        )

    def _get_current_price(self, symbol: str) -> Optional[float]:
        # Intentar WS primero
        if self._ws is not None:
            try:
                tick = self._ws.get_price(symbol)
                if tick is not None:
                    price, _ts = tick
                    self._last_price[symbol] = float(price)
                    return float(price)
            except Exception:
                pass
        return self._last_price.get(symbol)

    async def _reconcile_loop(self) -> None:
        """Cada N segundos, chequea si algún limit order debe filearse."""
        while not self._stop.is_set():
            try:
                async with self._fill_lock:
                    await self._reconcile_fills()
            except Exception as e:
                log.warning("paper.reconcile_err: %s", e)
            await asyncio.sleep(2.0)

    async def _reconcile_fills(self) -> None:
        """Production-grade fill simulation.

        Reproduce comportamiento real Binance:
        - Spread por símbolo (BTC tight, SOL wider)
        - Slippage variable por tamaño de orden ($25 / $100 / $500+)
        - Fill probability 40% al primer cruce, +15% por tick
        - Partial fills 20% probability (30-85% del qty)
        """
        import random
        for oid in list(self._open_orders.keys()):
            order = self._open_orders.get(oid)
            if not order:
                continue
            price = self._get_current_price(order.symbol)
            if price is None:
                continue
            spread_pct = SPREAD_PCT_BY_SYMBOL.get(order.symbol, DEFAULT_SPREAD_PCT)
            spread = price * spread_pct
            best_bid = price - spread
            best_ask = price + spread
            crossed = False
            if order.side == "BUY" and best_ask <= order.price:
                crossed = True
            elif order.side == "SELL" and best_bid >= order.price:
                crossed = True
            if not crossed:
                order.crossed_ticks = 0
                continue
            order.crossed_ticks += 1
            fill_prob = min(1.0, FILL_PROB_FIRST_CROSS + FILL_PROB_GROWTH * (order.crossed_ticks - 1))
            if random.random() > fill_prob:
                continue
            # Slippage variable por tamaño de orden
            slip_pct = _slippage_pct_for_qty(order.quote_qty)
            if order.side == "BUY":
                fill_price = min(order.price, best_ask * (1 + slip_pct))
            else:
                fill_price = max(order.price, best_bid * (1 - slip_pct))
            # Partial fill simulation
            fill_qty = order.qty
            partial = False
            if random.random() < PARTIAL_FILL_PROB:
                ratio = random.uniform(*PARTIAL_FILL_RATIO_RANGE)
                fill_qty = order.qty * ratio
                partial = True
            # Aplicar fee
            f = SYMBOL_FILTERS.get(order.symbol, {})
            base = f.get("base", "")
            if order.side == "BUY":
                received = fill_qty * (1 - FEE_PCT)
                self._balances[base] += received
                # Refund USDT unused si partial
                if partial:
                    refund = (order.qty - fill_qty) * order.price
                    self._balances["USDT"] += refund
            else:
                received_usdt = fill_qty * fill_price * (1 - FEE_PCT)
                self._balances["USDT"] += received_usdt
                # Refund base unused si partial
                if partial:
                    self._balances[base] += (order.qty - fill_qty)
            if partial:
                # Order parcialmente filled — actualizar qty restante, mantener open
                order.qty -= fill_qty
                log.info(
                    "paper.partial_fill %s %s limit=%.4f fill=%.4f qty=%.6f/%.6f",
                    order.symbol, order.side, order.price, fill_price,
                    fill_qty, order.qty + fill_qty,
                )
                # No quitar del open_orders, sigue con menos qty
                # Trigger callback igual (round trip matcher acumula)
                for cb in self._fill_callbacks:
                    try:
                        await cb(order, fill_price)
                    except Exception:
                        pass
                continue
            order.status = "FILLED"
            self._open_orders.pop(oid, None)
            log.info(
                "paper.fill %s %s limit=%.4f fill=%.4f qty=%.6f market=%.4f (spread=%.4f slip=%.4f)",
                order.symbol, order.side, order.price, fill_price, fill_qty,
                price, spread, fill_price - order.price,
            )
            for cb in self._fill_callbacks:
                try:
                    await cb(order, fill_price)
                except Exception as e:
                    log.warning("paper.fill_callback_err: %s", e)
