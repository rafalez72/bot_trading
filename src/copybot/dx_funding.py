"""dYdX v4 funding rate accrual — production parity hourly task.

Cada DX_FUNDING_UPDATE_HOURS (default 1h) recorre las posiciones open de
`dx_trades`, fetcha el funding rate del market y acumula en
`funding_paid`. Al cerrar, `dx_executor.close_position` descuenta este
valor del PnL net.

Convención de funding (dYdX v4):
  - `nextFundingRate` en `/perpetualMarkets` es el rate que se aplica al
    próximo funding event (eventos cada 1h).
  - rate > 0 → longs pagan a shorts.
  - rate < 0 → shorts pagan a longs.

Por ende:
  - LONG (is_buy=1) con rate>0 → costo (funding_paid +=)
  - SHORT (is_buy=0) con rate>0 → ingreso (funding_paid -=)

Imprecisión aceptada: si una posición abre 30min antes del tick de
funding, atribuimos 1h completa. Error < $0.001 por trade chico — fine.
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.config import DX_FUNDING_UPDATE_HOURS
from src.db.schema import db, tx
from src.dydx.client import DydxClient

log = logging.getLogger(__name__)


async def _fetch_funding_rates(client: DydxClient) -> dict[str, float]:
    """Devuelve dict ticker → next funding rate (per-hour)."""
    rates: dict[str, float] = {}
    try:
        data = await client.perpetual_markets()
    except Exception as e:
        log.warning("DX funding fetch markets err: %s", e)
        return rates
    markets = data.get("markets", {}) if isinstance(data, dict) else {}
    if not isinstance(markets, dict):
        return rates
    for tk, info in markets.items():
        if not isinstance(info, dict):
            continue
        raw = info.get("nextFundingRate")
        try:
            rates[tk] = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            continue
    return rates


def _open_positions() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, ticker, is_buy, entry_size_usdc, leverage, funding_paid "
            "FROM dx_trades WHERE status='open'"
        ).fetchall()
    return [dict(r) for r in rows]


def _apply_increment(trade_id: int, increment: float) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE dx_trades SET funding_paid=COALESCE(funding_paid,0)+? WHERE id=?",
            (increment, trade_id),
        )


async def update_funding_once(client: DydxClient) -> int:
    """Una pasada de update. Retorna # posiciones actualizadas."""
    rows = _open_positions()
    if not rows:
        return 0
    rates = await _fetch_funding_rates(client)
    if not rates:
        return 0
    elapsed_h = float(DX_FUNDING_UPDATE_HOURS)
    updated = 0
    for r in rows:
        ticker = r["ticker"]
        rate = rates.get(ticker)
        if rate is None:
            continue
        exposure = float(r["entry_size_usdc"] or 0) * float(r["leverage"] or 1.0)
        if exposure <= 0:
            continue
        sign = 1 if r["is_buy"] else -1
        increment = exposure * rate * elapsed_h * sign
        if abs(increment) < 1e-9:
            continue
        _apply_increment(r["id"], increment)
        updated += 1
    if updated:
        log.info("DX funding: %d posiciones actualizadas (rate update)", updated)
    return updated


async def dx_funding_loop() -> None:
    """Loop perpetuo. Hookeado en dx_run_loop como task paralela."""
    interval = max(60.0, float(DX_FUNDING_UPDATE_HOURS) * 3600.0)
    log.info("DX funding loop: arrancando (cada %.0fs)", interval)
    # Esperar al primer tick antes del primer update — la posición debe estar
    # open al menos `interval` para que tenga sentido cobrar funding.
    await asyncio.sleep(interval)
    async with DydxClient() as client:
        while True:
            try:
                await update_funding_once(client)
            except Exception as e:
                log.exception("DX funding loop iter err: %s", e)
            await asyncio.sleep(interval)
