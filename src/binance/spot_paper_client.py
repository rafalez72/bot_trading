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
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

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
        """Reusa WS para best bid/ask. Si no hay, retorna last_price ±0.01%."""
        price = self._get_current_price(symbol)
        if price is None:
            raise RuntimeError(f"paper: no price for {symbol} (WS aún no recibió)")
        spread = price * 0.0001
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
        for oid in list(self._open_orders.keys()):
            order = self._open_orders.get(oid)
            if not order:
                continue
            price = self._get_current_price(order.symbol)
            if price is None:
                continue
            filled = False
            fill_price = order.price
            # BUY fillea si el market price baja a su limit o más bajo.
            if order.side == "BUY" and price <= order.price:
                filled = True
                fill_price = order.price  # asumimos fill al limit
            # SELL fillea si el market sube al limit o más alto.
            elif order.side == "SELL" and price >= order.price:
                filled = True
                fill_price = order.price
            if not filled:
                continue
            # Aplicar fee
            f = SYMBOL_FILTERS.get(order.symbol, {})
            base = f.get("base", "")
            if order.side == "BUY":
                # Recibe base (menos fee en base).
                received = order.qty * (1 - FEE_PCT)
                self._balances[base] += received
            else:
                # Recibe USDT (menos fee en USDT).
                received_usdt = order.qty * fill_price * (1 - FEE_PCT)
                self._balances["USDT"] += received_usdt
            order.status = "FILLED"
            self._open_orders.pop(oid, None)
            log.info(
                "paper.fill %s %s price=%.4f qty=%.6f market=%.4f",
                order.symbol, order.side, fill_price, order.qty, price,
            )
            # Callbacks
            for cb in self._fill_callbacks:
                try:
                    await cb(order, fill_price)
                except Exception as e:
                    log.warning("paper.fill_callback_err: %s", e)
