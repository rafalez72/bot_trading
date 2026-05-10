"""Helpers de cuenta para Binance USDM Futures.

Wrappers thin sobre :class:`BinancePerpClient` que cubren las operaciones de
configuración + introspección de margen que necesita el orchestrator de
``crypto_arb_hedge`` al startup y antes de cada open atómico.

Funciones expuestas:

- :func:`ensure_hedge_mode_enabled` — fuerza la cuenta a positionMode=Hedge
  (dual-side). Las órdenes con ``positionSide=LONG/SHORT`` requieren esto;
  sin hedge mode Binance las rechaza con ``-4061``.
- :func:`set_leverage` — setea leverage por símbolo. Idempotente.
- :func:`get_margin_info` — devuelve total/available/used del wallet USDT
  + PnL no realizado. El orchestrator llama esto pre-open para no pisar
  margen.
- :func:`startup_checks` — orquesta todos los anteriores + valida que las
  API keys existen y que hay USDT mínimo para operar. Devuelve True/False
  para que el caller decida si arrancar el loop.

Decisión de diseño: cada función ABRE su propio cliente si no se le pasa uno
(``client=None``). Esto permite:

1. Uso one-shot en scripts / tests.
2. Reuso eficiente en el loop del hedge (un solo cliente long-lived).

Ninguna de estas funciones es hot-path. Se llaman al startup
(``ensure_hedge_mode_enabled``, ``set_leverage``, ``startup_checks``) y cada
N segundos (``get_margin_info``) — overhead despreciable.
"""
from __future__ import annotations

import logging
import os
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
    """Verifica + setea positionMode=Hedge en la cuenta. Idempotente.

    Hedge mode permite tener LONG y SHORT simultáneos del mismo símbolo
    (cada uno como posición independiente). El orchestrator del hedge
    necesita esto: la pata Polymarket es una "long sintética" del side y
    la pata Binance perp es SHORT del símbolo subyacente. Si la cuenta
    no estuviera en hedge mode, abrir SHORT sin cerrar LONG previo
    fallaría con "Position side cannot be changed if there exists open
    orders".

    Flow:

    1. ``GET /fapi/v1/positionSide/dual`` → si ya está en hedge, return
       ``True`` sin POST (más limpio en logs).
    2. Si no, ``POST /fapi/v1/positionSide/dual`` con
       ``dualSidePosition=true``.
    3. ``-4059`` ("No need to change") → tratado como éxito (carrera con
       otro proceso que ya lo seteó).

    Devuelve ``True`` si el cambio fue efectivo o ya estaba seteado.
    Lanza :class:`BinancePerpError` si Binance responde con un código
    distinto del esperado (ej. credenciales inválidas).
    """
    own_client = client is None
    if own_client:
        client = BinancePerpClient()
        await client.__aenter__()

    try:
        # Fast-path: GET para evitar POST si ya está habilitado.
        try:
            already = await client.get_position_mode()
            if already:
                log.debug("perp_account.hedge_mode already set")
                return True
        except BinancePerpError as e:
            # Si el GET falla por algo no-trivial (ej. -2014 invalid key) no
            # tiene sentido seguir con el POST que va a fallar igual. Para
            # otros errores transient, dejamos que el POST haga su retry.
            log.warning("perp_account.hedge_mode get failed: %s — intentando POST", e)

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
            # -4059: ya estaba en hedge mode (carrera). Lo tratamos como éxito.
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
    """Devuelve ``{total, available, used, unrealized_pnl}`` del futures wallet.

    - ``total``: ``totalWalletBalance`` USDT.
    - ``available``: ``availableBalance`` — lo que se puede usar para abrir
      nuevas posiciones (excluye margen ya bloqueado en órdenes/posiciones).
    - ``used``: ``total - available`` (margen comprometido).
    - ``unrealized_pnl``: ``totalUnrealizedProfit`` — PnL flotante de
      posiciones abiertas. Útil para reportar al startup.

    El orchestrator llama esto pre-open para chequear que tenemos margen
    suficiente antes de mandar la order — si ``available < required``, abort
    sin tocar Polymarket (evita rollbacks innecesarios).

    Endpoint: ``GET /fapi/v2/account`` (vía
    :meth:`BinancePerpClient.get_account_info`).
    """
    own_client = client is None
    if own_client:
        client = BinancePerpClient()
        await client.__aenter__()
    try:
        info = await client.get_account_info()
        total = float(info.get("totalWalletBalance") or 0.0)
        available = float(info.get("availableBalance") or 0.0)
        unrealized = float(info.get("totalUnrealizedProfit") or 0.0)
        used = max(total - available, 0.0)
        return {
            "total": total,
            "available": available,
            "used": used,
            "unrealized_pnl": unrealized,
        }
    finally:
        if own_client:
            await client.__aexit__(None, None, None)


# USDT mínimo para considerar la cuenta operativa. Por debajo de este balance,
# Binance puede rechazar órdenes con margen insuficiente incluso al tamaño
# mínimo. No es un kill-switch — solo warning. Configurable vía env.
_MIN_USDT_BALANCE_DEFAULT = 50.0


async def startup_checks(
    client: BinancePerpClient | None = None,
    *,
    symbols: list[str] | None = None,
    leverage: int = 2,
    min_usdt_balance: float | None = None,
) -> bool:
    """Validaciones at-boot del orchestrator hedge.

    Pasos (todos en orden, secuencial):

    1. **API keys**: ``BINANCE_API_KEY`` + ``BINANCE_API_SECRET`` definidos en
       env. Si falta cualquiera → ``log.error`` y ``return False`` (no
       arrancar).
    2. **Hedge mode**: :func:`ensure_hedge_mode_enabled` — necesario para
       abrir SHORT con ``positionSide=SHORT`` sin pisar LONG existentes.
    3. **Leverage**: para cada símbolo de ``symbols``, set leverage al valor
       dado (default ``2x``, conservador). Errores per-symbol se loguean
       pero no abortan (puede ser símbolo deshabilitado temporalmente).
    4. **Margin info**: log de ``totalWalletBalance / availableBalance /
       totalUnrealizedProfit``. Si ``available < min_usdt_balance``, log
       warning pero ``return True`` igual — el bot puede operar con bet
       chico mientras el user fondea más.

    Devuelve ``True`` si todas las pre-condiciones críticas (1+2) pasaron.
    ``False`` si las API keys faltan o hedge mode no se pudo habilitar.

    Args:
        client: cliente compartido opcional. Si es None, abre uno propio.
        symbols: lista de símbolos a configurar leverage (ej.
            ``["BTCUSDT", "ETHUSDT"]``). Vacío/None = skip leverage.
        leverage: leverage por símbolo. Default 2x.
        min_usdt_balance: umbral de warning. Default lee
            ``HEDGE_MIN_USDT_BALANCE`` de env, fallback 50.
    """
    # 1. API keys check (precondición dura).
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        log.error(
            "perp_account.startup_checks: BINANCE_API_KEY / "
            "BINANCE_API_SECRET no configurados — abortando hedge loop"
        )
        return False

    if min_usdt_balance is None:
        try:
            min_usdt_balance = float(
                os.getenv("HEDGE_MIN_USDT_BALANCE", _MIN_USDT_BALANCE_DEFAULT)
            )
        except (TypeError, ValueError):
            min_usdt_balance = _MIN_USDT_BALANCE_DEFAULT

    own_client = client is None
    if own_client:
        client = BinancePerpClient()
        await client.__aenter__()

    try:
        # 2. Hedge mode (precondición dura). Falla → abort.
        try:
            await ensure_hedge_mode_enabled(client=client)
        except BinancePerpError as e:
            log.error(
                "perp_account.startup_checks: hedge mode no se pudo "
                "habilitar (%s) — abortando", e,
            )
            return False
        except Exception:
            log.exception(
                "perp_account.startup_checks: hedge mode unexpected error"
            )
            return False

        # 3. Leverage por símbolo (best-effort, no aborta).
        for sym in symbols or []:
            try:
                await set_leverage(sym, leverage, client=client)
            except BinancePerpError as e:
                log.warning(
                    "perp_account.startup_checks: set_leverage falló "
                    "symbol=%s leverage=%d err=%s",
                    sym, leverage, e,
                )
            except Exception:
                log.exception(
                    "perp_account.startup_checks: set_leverage unexpected "
                    "symbol=%s", sym,
                )

        # 4. Margin info (informativo + warning si bajo).
        try:
            margin = await get_margin_info(client=client)
            log.info(
                "perp_account.startup_checks margin total=%.2f available=%.2f "
                "used=%.2f unrealized_pnl=%.2f",
                margin.get("total", 0.0), margin.get("available", 0.0),
                margin.get("used", 0.0), margin.get("unrealized_pnl", 0.0),
            )
            if margin.get("available", 0.0) < min_usdt_balance:
                log.warning(
                    "perp_account.startup_checks: available USDT=%.2f < "
                    "min=%.2f — bot operará con bets chicos hasta fondear",
                    margin.get("available", 0.0), min_usdt_balance,
                )
        except Exception:
            # No es crítico — si falla el read de margin, dejá al caller
            # arrancar igual (la primera order va a validar margin igual).
            log.exception("perp_account.startup_checks: get_margin_info failed")

        return True
    finally:
        if own_client:
            await client.__aexit__(None, None, None)


__all__ = [
    "ensure_hedge_mode_enabled",
    "set_leverage",
    "get_margin_info",
    "startup_checks",
]
