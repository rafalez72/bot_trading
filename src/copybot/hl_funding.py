"""Hyperliquid funding rate accrual — production parity hourly task.

Cada HL_FUNDING_UPDATE_HOURS recorre `hl_trades` open, fetcha funding rate
del coin y acumula en `funding_paid`. `hl_executor.close_position` (y
force_close) descuentan este valor del PnL net.

Endpoint: `metaAndAssetCtxs` devuelve [meta, [assetCtx,...]]. Cada
assetCtx tiene `funding` (rate per-hour, signed: positivo = longs pagan).

Convención:
  - funding > 0 → longs pagan a shorts
  - LONG (is_buy=1): funding_paid += exposure × rate × elapsed_h
  - SHORT (is_buy=0): funding_paid -= exposure × rate × elapsed_h
"""
from __future__ import annotations

import asyncio
import logging

from src.config import HL_FUNDING_UPDATE_HOURS
from src.db.schema import db, tx
from src.hyperliquid.client import HyperliquidClient

log = logging.getLogger(__name__)


async def _fetch_funding_by_coin(client: HyperliquidClient) -> dict[str, float]:
    rates: dict[str, float] = {}
    try:
        data = await client.meta_and_asset_ctxs()
    except Exception as e:
        log.warning("HL funding fetch err: %s", e)
        return rates
    if not isinstance(data, list) or len(data) < 2:
        return rates
    meta, ctxs = data[0], data[1]
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    if not isinstance(ctxs, list) or len(ctxs) != len(universe):
        return rates
    for asset, ctx in zip(universe, ctxs):
        coin = (asset or {}).get("name") if isinstance(asset, dict) else None
        if not coin or not isinstance(ctx, dict):
            continue
        try:
            rates[coin] = float(ctx.get("funding") or 0)
        except (TypeError, ValueError):
            continue
    return rates


def _open_positions() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, coin, is_buy, entry_size_usdc, leverage, funding_paid "
            "FROM hl_trades WHERE status='open'"
        ).fetchall()
    return [dict(r) for r in rows]


def _apply_increment(trade_id: int, increment: float) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE hl_trades SET funding_paid=COALESCE(funding_paid,0)+? WHERE id=?",
            (increment, trade_id),
        )


async def update_funding_once(client: HyperliquidClient) -> int:
    rows = _open_positions()
    if not rows:
        return 0
    rates = await _fetch_funding_by_coin(client)
    if not rates:
        return 0
    elapsed_h = float(HL_FUNDING_UPDATE_HOURS)
    updated = 0
    for r in rows:
        coin = r["coin"]
        rate = rates.get(coin)
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
        log.info("HL funding: %d posiciones actualizadas", updated)
    return updated


async def hl_funding_loop() -> None:
    interval = max(60.0, float(HL_FUNDING_UPDATE_HOURS) * 3600.0)
    log.info("HL funding loop: arrancando (cada %.0fs)", interval)
    await asyncio.sleep(interval)
    async with HyperliquidClient() as client:
        while True:
            try:
                await update_funding_once(client)
            except Exception as e:
                log.exception("HL funding loop iter err: %s", e)
            await asyncio.sleep(interval)
