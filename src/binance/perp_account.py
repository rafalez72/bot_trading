"""Helpers de cuenta para Binance USDM Futures.

Wrappers thin sobre :class:`BinancePerpClient` que cubren las operaciones de
configuración + introspección de margen que necesita el orchestrator de
``crypto_arb_hedge`` al startup y antes de cada open atómico.

Funciones expuestas:

- :func:`ensure_hedge_mode_enabled` — fuerza la cuenta a positionMode=Hedge
  (dual-side). Las órdenes con ``positionSide=LONG/SHORT`` requieren esto;
  sin hedge mode Binance las rechaza con ``-4061``.
- :func:`set_leverage` — setea leverage por símbolo. Idempotente.
- :func:`get_margin_info` — devuelve total/available/used del wallet USDT.
  El orchestrator llama esto pre-open para no pisar margen.

Decisión de diseño: cada función ABRE su propio cliente si no se le pasa uno
(``client=None``). Esto permite:

1. Uso one-shot en scripts / tests.
2. Reuso eficiente en el loop del hedge (un solo cliente long-lived).

Ninguna de estas funciones es hot-path. Se llaman al startup
(``ensure_hedge_mode_enabled``, ``set_leverage``) y cada N segundos
(``get_margin_info``) — overhead despreciable.
"""
from __future__ import annotations

import logging
from typing import Any

from src.binance.perp_client import BinancePerpClient, BinancePerpError

log = logging.getLogger(__name__)

# Códigos de error que Binance devuelve cuando el mode/leverage YA está en el
# valor solicitado. No son fallas — la operación es idempotente y los
# tratamos como éxito silencioso.
_ALREADY_HEDGE_MODE_CODE = -4059   # "No need to change position side"
_NO_NEED_TO_CHANGE_MARGIN_TYPE = -4046


async def ensure_hedge_mode_enabled(
    client: BinancePerpClient | None = None,
) -> bool:
    """Setea positionMode=Hedge en la cuenta. Idempotente.

    Hedge mode permite tener LONG y SHORT simultáneos del mismo símbolo
    (cada uno como posición independiente). El orchestrator del hedge
    necesita esto: la pata Polymarket es una "long sintética" del side y
    la pata Binance perp es SHORT del símbolo subyacente. Si la cuenta
    no estuviera en hedge mode, abrir SHORT sin cerrar LONG previo
    fallaría con "Position side cannot be changed if there exists open
    orders".

    Devuelve ``True`` si el cambio fue efectivo o ya estaba seteado.
    Lanza :class:`BinancePerpError` si Binance responde con un código
    distinto del esperado (ej. credenciales inválidas).

    Endpoint: ``POST /fapi/v1/positionSide/dual`` con ``dualSidePosition=true``.
    """
    own_client = client is None
    if own_client:
        client = BinancePerpClient()
        await client.__aenter__()

    try:
        try:
            await client._request(
                "POST",
                "/fapi/v1/positionSide/dual",
                params={"dualSidePosition": "true"},
                signed=True,
            )
            log.info("perp_account.hedge_mode_enabled")
            return True
        except BinancePerpError as e:
            # -4059: ya estaba en hedge mode. Lo tratamos como éxito.
            if e.code == _ALREADY_HEDGE_MODE_CODE:
                log.debug("perp_account.hedge_mode already set")
                return True
            log.error("perp_account.hedge_mode failed: %s", e)
            raise
    finally:
        if own_client:
            await client.__aexit__(None, None, None)


async def set_leverage(
    symbol: str,
    leverage: int,
    *,
    client: BinancePerpClient | None = None,
) -> dict[str, Any]:
    """Setea leverage del símbolo. Wrapper idempotente.

    Reutiliza :meth:`BinancePerpClient.set_leverage`. Si Binance devuelve
    ``-4046`` (no need to change) lo tratamos como éxito y devolvemos un
    dict mínimo. Para cualquier otro error, propaga la excepción.
    """
    own_client = client is None
    if own_client:
        client = BinancePerpClient()
        await client.__aenter__()
    try:
        try:
            res = await client.set_leverage(symbol, leverage)
            log.info(
                "perp_account.leverage_set symbol=%s leverage=%d",
                symbol.upper(), leverage,
            )
            return res
        except BinancePerpError as e:
            if e.code == _NO_NEED_TO_CHANGE_MARGIN_TYPE:
                return {"symbol": symbol.upper(), "leverage": leverage,
                        "noop": True}
            raise
    finally:
        if own_client:
            await client.__aexit__(None, None, None)


async def get_margin_info(
    client: BinancePerpClient | None = None,
) -> dict[str, float]:
    """Devuelve ``{total, available, used}`` del wallet USDT del futures.

    - ``total``: ``balance`` USDT (incluye PnL no realizado).
    - ``available``: ``availableBalance`` — lo que se puede usar para abrir
      nuevas posiciones (excluye margen ya bloqueado en órdenes/posiciones).
    - ``used``: ``total - available`` (margen comprometido).

    El orchestrator llama esto pre-open para chequear que tenemos margen
    suficiente antes de mandar la order — si ``available < required``, abort
    sin tocar Polymarket (evita rollbacks innecesarios).

    Endpoint: ``GET /fapi/v2/balance``. Devuelve una lista, filtramos USDT.
    """
    own_client = client is None
    if own_client:
        client = BinancePerpClient()
        await client.__aenter__()
    try:
        data = await client._request("GET", "/fapi/v2/balance", signed=True)
        if not isinstance(data, list):
            raise BinancePerpError(
                f"balance respuesta inesperada: {type(data).__name__}",
                payload=data,
            )
        total = 0.0
        available = 0.0
        for row in data:
            if (row.get("asset") or "").upper() != "USDT":
                continue
            total = float(row.get("balance") or 0.0)
            # availableBalance puede no estar en respuestas viejas — fallback
            # a balance (peor caso: chequeamos contra total y aprobamos cuando
            # no deberíamos; la propia Binance rechazará el order entonces).
            available = float(row.get("availableBalance") or row.get("balance") or 0.0)
            break
        used = max(total - available, 0.0)
        return {"total": total, "available": available, "used": used}
    finally:
        if own_client:
            await client.__aexit__(None, None, None)


__all__ = [
    "ensure_hedge_mode_enabled",
    "set_leverage",
    "get_margin_info",
]
