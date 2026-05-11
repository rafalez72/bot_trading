"""Cliente Binance Spot — órdenes signed para grid bot + signal bot.

Endpoints REST (Spot, base ``https://api.binance.com``):
- ``POST /api/v3/order``            → place order (signed)
- ``DELETE /api/v3/order``          → cancel order (signed)
- ``GET  /api/v3/openOrders``       → órdenes abiertas (signed)
- ``GET  /api/v3/account``          → balances (signed)
- ``GET  /api/v3/depth``            → orderbook (público)
- ``GET  /api/v3/ticker/bookTicker``→ best bid/ask (público)
- ``GET  /api/v3/exchangeInfo``     → símbolo metadata (público)

Auth: mismo patrón que perp_client (HMAC-SHA256 + X-MBX-APIKEY header).

Seguridad para small cap:
- Sin transfer/withdraw permissions en la API key (solo READ + SPOT TRADE).
- IP whitelist obligatorio.
- Cap por orden vía max_quote_qty config (default $50 USDT/orden).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.binance.com"
DEFAULT_RECV_WINDOW_MS = 5000
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_RETRIES = 3


class BinanceSpotError(Exception):
    """Error genérico del cliente spot."""

    def __init__(self, message: str, *, code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


@dataclass
class SpotOrderResult:
    ok: bool
    order_id: str | None
    client_order_id: str | None
    symbol: str
    side: str
    type: str
    price: float | None
    qty: float | None
    quote_qty: float | None
    status: str | None  # NEW, FILLED, PARTIALLY_FILLED, CANCELED, REJECTED
    raw: Any
    error: str | None = None


class BinanceSpotClient:
    """Cliente async Binance Spot.

    Uso::

        async with BinanceSpotClient() as c:
            bal = await c.get_balance("USDT")
            order = await c.place_limit_buy("BTCUSDT", price=95000.0, quote_qty=50.0)
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        recv_window_ms: int = DEFAULT_RECV_WINDOW_MS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self._api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")
        self._base_url = base_url.rstrip("/")
        self._recv_window_ms = recv_window_ms
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._external_client = client is not None
        self._client = client
        self._exchange_info_cache: dict[str, dict] = {}

    async def __aenter__(self) -> "BinanceSpotClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._external_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _has_creds(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _sign(self, query: str) -> str:
        return hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _build_signed_query(self, params: dict[str, Any]) -> str:
        if not self._has_creds():
            raise BinanceSpotError("BINANCE_API_KEY / BINANCE_API_SECRET no configurados")
        full = dict(params)
        full["timestamp"] = int(time.time() * 1000)
        full["recvWindow"] = self._recv_window_ms
        query = urlencode(sorted(full.items()))
        return f"{query}&signature={self._sign(query)}"

    def _headers(self, *, signed: bool) -> dict[str, str]:
        h = {"User-Agent": "polymarket-copybot/spot_client"}
        if signed:
            h["X-MBX-APIKEY"] = self._api_key
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)

        url = f"{self._base_url}{path}"
        params = dict(params or {})
        if signed:
            query = self._build_signed_query(params)
            full_url = f"{url}?{query}"
            req_params: dict[str, Any] | None = None
        else:
            full_url = url
            req_params = params or None

        backoff = 0.5
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.request(
                    method, full_url, params=req_params,
                    headers=self._headers(signed=signed),
                )
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                last_exc = exc
                log.warning(
                    "binance_spot.request_error attempt=%d method=%s path=%s err=%s",
                    attempt + 1, method, path, exc,
                )
                if attempt + 1 < self._max_retries:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise BinanceSpotError(f"network error: {exc}") from exc

            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception as exc:  # noqa: BLE001
                    raise BinanceSpotError(
                        f"invalid json from {path}: {exc}", payload=resp.text,
                    ) from exc

            try:
                err = resp.json()
            except Exception:
                err = {"raw": resp.text}
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("msg") if isinstance(err, dict) else str(err)
            if resp.status_code in (429, 418, 500, 502, 503) and attempt + 1 < self._max_retries:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            raise BinanceSpotError(
                f"http {resp.status_code} {method} {path}: {msg}",
                code=code if isinstance(code, int) else resp.status_code,
                payload=err,
            )
        raise BinanceSpotError(f"unreachable retry exhausted: {last_exc}")

    # ----- public endpoints -----

    async def get_book_ticker(self, symbol: str) -> dict:
        """Best bid + ask."""
        return await self._request("GET", "/api/v3/ticker/bookTicker", params={"symbol": symbol})

    async def get_exchange_info(self, symbol: str) -> dict:
        """Cachea filtros del símbolo (tickSize, stepSize, minNotional)."""
        if symbol in self._exchange_info_cache:
            return self._exchange_info_cache[symbol]
        data = await self._request("GET", "/api/v3/exchangeInfo", params={"symbol": symbol})
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        for s in symbols:
            if s.get("symbol") == symbol:
                self._exchange_info_cache[symbol] = s
                return s
        raise BinanceSpotError(f"exchangeInfo: symbol {symbol} no encontrado")

    # ----- signed endpoints -----

    async def get_account(self) -> dict:
        return await self._request("GET", "/api/v3/account", params={}, signed=True)

    async def get_balance(self, asset: str) -> float:
        acc = await self.get_account()
        for b in acc.get("balances", []):
            if b.get("asset") == asset:
                try:
                    return float(b.get("free") or 0)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        data = await self._request("GET", "/api/v3/openOrders", params=params, signed=True)
        return data if isinstance(data, list) else []

    async def place_limit_buy(
        self, symbol: str, *, price: float, quote_qty: float,
        client_order_id: str | None = None,
    ) -> SpotOrderResult:
        """Limit BUY: gasta `quote_qty` USDT en `price` → calcula qty.

        Round qty al stepSize del símbolo. Round price al tickSize.
        """
        info = await self.get_exchange_info(symbol)
        tick = _filter_value(info, "PRICE_FILTER", "tickSize") or 0.01
        step = _filter_value(info, "LOT_SIZE", "stepSize") or 0.001
        min_notional = _filter_value(info, "NOTIONAL", "minNotional") or _filter_value(info, "MIN_NOTIONAL", "minNotional") or 5.0

        price_r = _round_to(price, tick)
        qty_raw = quote_qty / price_r
        qty_r = _round_to_floor(qty_raw, step)
        notional = qty_r * price_r
        if notional < min_notional:
            return SpotOrderResult(
                ok=False, order_id=None, client_order_id=client_order_id,
                symbol=symbol, side="BUY", type="LIMIT",
                price=price_r, qty=qty_r, quote_qty=notional, status="REJECTED",
                raw=None, error=f"min_notional {notional:.2f} < {min_notional}",
            )
        return await self._place_order(
            symbol=symbol, side="BUY", type_="LIMIT", price=price_r,
            qty=qty_r, client_order_id=client_order_id, time_in_force="GTC",
        )

    async def place_limit_sell(
        self, symbol: str, *, price: float, qty: float,
        client_order_id: str | None = None,
    ) -> SpotOrderResult:
        info = await self.get_exchange_info(symbol)
        tick = _filter_value(info, "PRICE_FILTER", "tickSize") or 0.01
        step = _filter_value(info, "LOT_SIZE", "stepSize") or 0.001
        price_r = _round_to(price, tick)
        qty_r = _round_to_floor(qty, step)
        return await self._place_order(
            symbol=symbol, side="SELL", type_="LIMIT", price=price_r,
            qty=qty_r, client_order_id=client_order_id, time_in_force="GTC",
        )

    async def place_market_buy(
        self, symbol: str, *, quote_qty: float,
        client_order_id: str | None = None,
    ) -> SpotOrderResult:
        """Market BUY usando quoteOrderQty (Binance calc qty)."""
        params = {
            "symbol": symbol, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": _fmt_decimal(quote_qty, 4),
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return await self._submit_order(params)

    async def cancel_order(self, symbol: str, *, order_id: str) -> bool:
        try:
            await self._request(
                "DELETE", "/api/v3/order",
                params={"symbol": symbol, "orderId": order_id},
                signed=True,
            )
            return True
        except BinanceSpotError as e:
            log.warning("spot.cancel_order failed symbol=%s id=%s err=%s", symbol, order_id, e)
            return False

    # ----- internos -----

    async def _place_order(
        self, *, symbol: str, side: str, type_: str,
        price: float, qty: float, client_order_id: str | None,
        time_in_force: str,
    ) -> SpotOrderResult:
        params: dict[str, Any] = {
            "symbol": symbol, "side": side, "type": type_,
            "timeInForce": time_in_force,
            "quantity": _fmt_decimal(qty, 8),
            "price": _fmt_decimal(price, 8),
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return await self._submit_order(params)

    async def _submit_order(self, params: dict[str, Any]) -> SpotOrderResult:
        try:
            data = await self._request("POST", "/api/v3/order", params=params, signed=True)
        except BinanceSpotError as e:
            return SpotOrderResult(
                ok=False, order_id=None,
                client_order_id=params.get("newClientOrderId"),
                symbol=params.get("symbol", ""), side=params.get("side", ""),
                type=params.get("type", ""), price=None, qty=None, quote_qty=None,
                status="REJECTED", raw=None, error=str(e),
            )
        return SpotOrderResult(
            ok=True,
            order_id=str(data.get("orderId")) if data.get("orderId") is not None else None,
            client_order_id=data.get("clientOrderId"),
            symbol=data.get("symbol", ""),
            side=data.get("side", ""),
            type=data.get("type", ""),
            price=_safe_float(data.get("price")),
            qty=_safe_float(data.get("executedQty")),
            quote_qty=_safe_float(data.get("cummulativeQuoteQty")),
            status=data.get("status"),
            raw=data,
        )


def _filter_value(info: dict, filter_type: str, key: str) -> float | None:
    for f in info.get("filters", []):
        if f.get("filterType") == filter_type:
            v = f.get(key)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _round_to(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(round(value / step) * step, 10)


def _round_to_floor(value: float, step: float) -> float:
    """Floor a múltiplo de step (para no exceder balance)."""
    if step <= 0:
        return value
    import math
    return round(math.floor(value / step) * step, 10)


def _fmt_decimal(v: float, decimals: int) -> str:
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".") or "0"


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f != 0 else 0.0
    except (TypeError, ValueError):
        return None
