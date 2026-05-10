"""Cliente Binance USDM Futures (perp) — órdenes signed para hedge bot.

Diseñado para el módulo ``crypto_arb_hedge`` que abre Polymarket BUY +
Binance perp SHORT en paralelo (delta-neutral). Solo expone los endpoints
que necesita el orchestrator del hedge — no es un wrapper completo de
Binance Futures.

Endpoints REST (USDM Futures, base ``https://fapi.binance.com``):
- ``POST /fapi/v1/order``         → place market order (signed)
- ``GET  /fapi/v2/positionRisk``  → posiciones abiertas (signed)
- ``GET  /fapi/v2/balance``       → balance USDT (signed)
- ``GET  /fapi/v1/depth``         → orderbook bid/ask (público, no signed)
- ``GET  /fapi/v1/ticker/bookTicker`` → best bid/ask (público)

Auth (HMAC-SHA256):
1. Construir querystring con params + ``timestamp`` (epoch ms).
2. ``signature = HMAC_SHA256(secret, querystring).hexdigest()``.
3. Append ``&signature=...`` y mandar header ``X-MBX-APIKEY: <key>``.

Hedge mode: en cuentas con ``Hedge Mode=ON`` cada símbolo tiene dos
posiciones independientes (LONG y SHORT). El campo ``positionSide`` debe
ser explicit ``LONG`` o ``SHORT`` (no ``BOTH``). Llamar
``perp_account.ensure_hedge_mode_enabled()`` al startup del bot — sino las
órdenes con positionSide explícito fallan con error -4061.

Errores conocidos:
- 401 Invalid API key → key revocada o restricción de IP.
- 429 Too Many Requests → rate limit (1200 weight/min en endpoints públicos,
  2400 weight/min con API key). Usamos retry exponencial 3 intentos.
- -2010 NEW_ORDER_REJECTED → margen insuficiente, símbolo deshabilitado.
- -1021 Timestamp out of recvWindow → reloj local desincronizado >5s.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://fapi.binance.com"
DEFAULT_RECV_WINDOW_MS = 5000
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_RETRIES = 3


class BinancePerpError(Exception):
    """Error genérico del cliente perp. ``code`` = código Binance (negativo)
    si vino de la API, o status HTTP si fue 4xx/5xx sin body parseable."""

    def __init__(self, message: str, *, code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


class BinancePerpClient:
    """Cliente async de Binance USDM Futures.

    Uso típico::

        async with BinancePerpClient() as c:
            await c.set_leverage("BTCUSDT", 2)
            order = await c.place_market_order(
                symbol="BTCUSDT", side="SELL", quantity=0.001,
                position_side="SHORT",
            )
            pos = await c.get_position("BTCUSDT")

    Auth se carga desde env vars al construir si no se pasan keys explícitos.
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
        # External client lo respetamos sin cerrarlo (lo dueño es el caller).
        self._external_client = client is not None
        self._client = client

    # ----- ctx mgr -----

    async def __aenter__(self) -> "BinancePerpClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._external_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ----- helpers internos -----

    def _has_creds(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _sign(self, query: str) -> str:
        """HMAC-SHA256 hex digest de la querystring usando api_secret."""
        return hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _build_signed_query(self, params: dict[str, Any]) -> str:
        """Agrega timestamp + recvWindow + signature a los params dados.

        Devuelve la querystring final lista para concatenar a la URL o usar
        en el body POST. Binance acepta los params signed como query string
        incluso para POST — el body queda vacío.
        """
        if not self._has_creds():
            raise BinancePerpError(
                "BINANCE_API_KEY / BINANCE_API_SECRET no configurados"
            )
        full = dict(params)
        full["timestamp"] = int(time.time() * 1000)
        full["recvWindow"] = self._recv_window_ms
        # ordenado para que los tests verifiquen signature de forma determinista
        query = urlencode(sorted(full.items()))
        sig = self._sign(query)
        return f"{query}&signature={sig}"

    def _headers(self, *, signed: bool) -> dict[str, str]:
        h = {"User-Agent": "polymarket-copybot/perp_client"}
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
        """Wrapper con retry exponencial para 5xx y 429."""
        if self._client is None:
            # Permite usar sin async with — ideal para tests.
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
                    method,
                    full_url,
                    params=req_params,
                    headers=self._headers(signed=signed),
                )
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                last_exc = exc
                log.warning(
                    "binance_perp.request_error attempt=%d method=%s path=%s err=%s",
                    attempt + 1, method, path, exc,
                )
                if attempt + 1 < self._max_retries:
                    time.sleep(0)  # noop, await abajo
                    import asyncio
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise BinancePerpError(f"network error: {exc}") from exc

            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception as exc:  # noqa: BLE001
                    raise BinancePerpError(
                        f"invalid json from {path}: {exc}", payload=resp.text
                    ) from exc

            # Error: parsear body si hay JSON con {code, msg}
            try:
                err = resp.json()
            except Exception:
                err = {"raw": resp.text}

            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("msg") if isinstance(err, dict) else str(err)

            # 429 + 5xx → retry con backoff
            if resp.status_code in (429, 500, 502, 503, 504):
                log.warning(
                    "binance_perp.transient status=%d code=%s msg=%s attempt=%d",
                    resp.status_code, code, msg, attempt + 1,
                )
                if attempt + 1 < self._max_retries:
                    import asyncio
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue

            # No retry para 4xx con código Binance (la siguiente request da
            # mismo error). Lanzar inmediatamente.
            raise BinancePerpError(
                f"binance error status={resp.status_code} code={code} msg={msg}",
                code=code if isinstance(code, int) else resp.status_code,
                payload=err,
            )

        raise BinancePerpError(
            f"max retries reached ({self._max_retries})"
        ) from last_exc

    # ----- public API -----

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        *,
        position_side: str = "BOTH",
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> dict:
        """Mete una market order signed.

        Args:
            symbol: ``BTCUSDT``, etc.
            side: ``BUY`` o ``SELL`` (lado de la order).
            quantity: tamaño en unidades del activo (no USDT). Binance respeta
                lotSize/stepSize del símbolo — pasarlo redondeado por afuera.
            position_side: ``LONG`` / ``SHORT`` en hedge mode, ``BOTH`` en
                one-way mode (default Binance).
            reduce_only: si True, sólo cierra/reduce posición existente.
                Útil para ``close_position`` — en hedge mode NO se puede usar
                con positionSide explícito (Binance error -4014); se setea
                solamente en one-way mode (positionSide=BOTH).
            client_order_id: opcional, para idempotencia client-side.

        Devuelve el dict de respuesta de Binance con campos: ``orderId``,
        ``status``, ``avgPrice``, ``executedQty``, ``cumQuote``, etc.
        """
        side = side.upper()
        position_side = position_side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side inválido: {side!r}")
        if position_side not in ("LONG", "SHORT", "BOTH"):
            raise ValueError(f"position_side inválido: {position_side!r}")

        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side,
            "type": "MARKET",
            "quantity": _format_quantity(quantity),
            "positionSide": position_side,
        }
        # reduceOnly + positionSide explícito = error -4014. Solo lo
        # mandamos en one-way mode (BOTH).
        if reduce_only and position_side == "BOTH":
            params["reduceOnly"] = "true"
        if client_order_id:
            params["newClientOrderId"] = client_order_id

        return await self._request("POST", "/fapi/v1/order", params=params, signed=True)

    async def close_position(
        self,
        symbol: str,
        position_side: str,
        *,
        quantity: float | None = None,
    ) -> dict:
        """Cierra una posición abierta del lado dado.

        Lee la qty actual del positionRisk endpoint si ``quantity`` no se
        especifica. En hedge mode, una posición SHORT se cierra con BUY del
        mismo positionSide=SHORT (Binance entiende "reduce SHORT" por el
        positionSide); una LONG se cierra con SELL positionSide=LONG.
        """
        position_side = position_side.upper()
        if position_side not in ("LONG", "SHORT"):
            raise ValueError(
                f"close_position requiere LONG/SHORT, recibí {position_side!r}"
            )

        if quantity is None:
            pos = await self.get_position(symbol, position_side=position_side)
            qty_abs = abs(float(pos.get("qty") or 0.0))
            if qty_abs <= 0:
                raise BinancePerpError(
                    f"no hay posición abierta {position_side} en {symbol}"
                )
            quantity = qty_abs

        # En hedge mode: cerrar SHORT = BUY positionSide=SHORT,
        #                cerrar LONG  = SELL positionSide=LONG.
        side = "BUY" if position_side == "SHORT" else "SELL"
        return await self.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            position_side=position_side,
        )

    async def get_position(
        self,
        symbol: str,
        *,
        position_side: str | None = None,
    ) -> dict:
        """Devuelve {qty, entry_price, unrealized_pnl, position_side, raw}.

        Si ``position_side`` se especifica (LONG/SHORT) y la cuenta está en
        hedge mode, filtra esa posición específica. Sino devuelve la primera
        no-cero (compat one-way mode).
        """
        data = await self._request(
            "GET", "/fapi/v2/positionRisk",
            params={"symbol": symbol.upper()}, signed=True,
        )
        if not isinstance(data, list):
            raise BinancePerpError(
                f"positionRisk respuesta inesperada: {type(data).__name__}",
                payload=data,
            )

        match: dict | None = None
        for row in data:
            ps = (row.get("positionSide") or "").upper()
            if position_side and ps != position_side.upper():
                continue
            qty = float(row.get("positionAmt") or 0.0)
            if match is None or abs(qty) > abs(float(match.get("positionAmt") or 0.0)):
                match = row

        if match is None:
            return {
                "qty": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0,
                "position_side": position_side or "BOTH", "raw": None,
            }

        return {
            "qty": float(match.get("positionAmt") or 0.0),
            "entry_price": float(match.get("entryPrice") or 0.0),
            "unrealized_pnl": float(match.get("unRealizedProfit") or 0.0),
            "position_side": (match.get("positionSide") or "BOTH").upper(),
            "raw": match,
        }

    async def get_balance(self) -> float:
        """Devuelve el balance total USDT (futures wallet)."""
        data = await self._request("GET", "/fapi/v2/balance", signed=True)
        if not isinstance(data, list):
            raise BinancePerpError(
                f"balance respuesta inesperada: {type(data).__name__}",
                payload=data,
            )
        for row in data:
            if (row.get("asset") or "").upper() == "USDT":
                return float(row.get("balance") or 0.0)
        return 0.0

    async def get_perp_mid(self, symbol: str) -> dict:
        """Devuelve {bid, ask, mid, ts} del orderbook top-of-book.

        Endpoint público (no signed) — útil para validar slippage estimado
        antes de mandar la market order.
        """
        data = await self._request(
            "GET", "/fapi/v1/ticker/bookTicker",
            params={"symbol": symbol.upper()}, signed=False,
        )
        if not isinstance(data, dict):
            raise BinancePerpError(
                f"bookTicker respuesta inesperada: {type(data).__name__}",
                payload=data,
            )
        bid = float(data.get("bidPrice") or 0.0)
        ask = float(data.get("askPrice") or 0.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        ts = int(data.get("time") or int(time.time() * 1000))
        return {"bid": bid, "ask": ask, "mid": mid, "ts": ts}

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Setea el leverage del símbolo (signed). Idempotente."""
        if leverage <= 0 or leverage > 125:
            raise ValueError(f"leverage fuera de rango 1..125: {leverage}")
        return await self._request(
            "POST", "/fapi/v1/leverage",
            params={"symbol": symbol.upper(), "leverage": int(leverage)},
            signed=True,
        )

    async def get_funding_rate(self, symbol: str) -> dict:
        """Devuelve el último funding rate {rate, next_funding_ts, mark_price}.

        Endpoint público. ``rate`` es decimal (0.0001 = 0.01% por 8h en BTC).
        Si rate es muy negativo y vamos SHORT, recibimos plata; si es muy
        positivo, pagamos. ``crypto_arb_hedge`` usa esto como abort gate.
        """
        data = await self._request(
            "GET", "/fapi/v1/premiumIndex",
            params={"symbol": symbol.upper()}, signed=False,
        )
        if not isinstance(data, dict):
            raise BinancePerpError(
                f"premiumIndex respuesta inesperada: {type(data).__name__}",
                payload=data,
            )
        return {
            "rate": float(data.get("lastFundingRate") or 0.0),
            "next_funding_ts": int(data.get("nextFundingTime") or 0),
            "mark_price": float(data.get("markPrice") or 0.0),
            "raw": data,
        }


def _format_quantity(qty: float) -> str:
    """Formatea quantity sin notación científica.

    Binance rechaza ``1e-05`` con error -1102 ("invalid number"). Forzamos
    decimal plano con hasta 8 decimales (más que suficiente para BTC, que
    tiene stepSize=0.001).
    """
    return f"{qty:.8f}".rstrip("0").rstrip(".") or "0"
