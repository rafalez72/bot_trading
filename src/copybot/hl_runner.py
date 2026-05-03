"""Runner del bot HL — corre en paralelo al main runner como asyncio task.

Activado por env HL_MODE=true. Mantiene su propio cursor por wallet en
index_state con clave 'hl_cursor:<wallet>'. Cursor en milisegundos epoch.

Loop:
  - Cada HL_SLEEP_SECONDS (default 5):
    1. Polea hl_subscriptions activos en paralelo (asyncio.gather)
    2. Para cada nuevo fill, classify_fill() decide:
         Open Long/Short → hl_executor.open_position
         Close Long/Short → hl_executor.close_position
    3. Avanza cursor.
  - Cada HL_SWEEP_SECONDS: SL/TP/trailing/liquidation sweep en open positions.
"""
from __future__ import annotations

import asyncio
import logging
import time

from rich.console import Console

from src.config import (
    HL_SLEEP_SECONDS,
    HL_STOP_LOSS_PCT,
    HL_TAKE_PROFIT_PCT,
    HL_TRAIL_ACTIVATION_PCT,
    HL_TRAIL_DROP_PCT,
    HL_LIQUIDATION_BUFFER,
    HL_SWEEP_SECONDS,
    HL_BASE_USDC,
    HL_USE_ORDERBOOK_FILL,
)
from src.copybot import hl_executor
from src.db.schema import db, tx
from src.hyperliquid.client import HyperliquidClient

# Production parity (HL): mismos thresholds que DX
HL_BROADCAST_LATENCY_S = 2.0
HL_REALTIME_THRESHOLD_S = 30.0


async def _realistic_fill_price(
    client: HyperliquidClient, coin: str, size_usdc: float, is_buy: int, fill_ts_ms: int
) -> float | None:
    """Walk L2 book para `size_usdc` tras simular latencia broadcast.
    Buy walks asks, sell walks bids. Fills viejos (>30s) → None (caller usa slippage)."""
    if not HL_USE_ORDERBOOK_FILL:
        return None
    age_s = time.time() - (fill_ts_ms / 1000.0)
    if age_s > HL_REALTIME_THRESHOLD_S:
        return None
    await asyncio.sleep(HL_BROADCAST_LATENCY_S)
    try:
        book = await client.l2_book(coin)
    except Exception as e:
        log.debug("HL l2_book %s err: %s", coin, e)
        return None
    if not isinstance(book, dict):
        return None
    levels = book.get("levels") or []
    if not isinstance(levels, list) or len(levels) < 2:
        return None
    # levels[0] = bids, levels[1] = asks
    side_levels = levels[1] if is_buy else levels[0]
    if not side_levels:
        return None
    remaining = float(size_usdc)
    total_qty = 0.0
    weighted_sum = 0.0
    for lv in side_levels:
        try:
            px = float(lv["px"])
            qty = float(lv["sz"])
        except (KeyError, TypeError, ValueError):
            continue
        if px <= 0 or qty <= 0:
            continue
        level_value = px * qty
        if remaining <= level_value:
            qty_taken = remaining / px
            weighted_sum += px * qty_taken
            total_qty += qty_taken
            remaining = 0.0
            break
        weighted_sum += px * qty
        total_qty += qty
        remaining -= level_value
    if total_qty <= 0:
        return None
    return weighted_sum / total_qty

log = logging.getLogger(__name__)
console = Console()


def _active_wallets() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT wallet FROM hl_subscriptions WHERE status='active'"
        ).fetchall()
    return [r["wallet"] for r in rows]


def _get_cursor(wallet: str) -> int | None:
    """Cursor en MILLISECONDS epoch (HL usa ms)."""
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM index_state WHERE key=?",
            (f"hl_cursor:{wallet}",),
        ).fetchone()
    return int(r["value"]) if r else None


def _set_cursor(wallet: str, ms: int) -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO index_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value, updated_at=datetime('now')
            """,
            (f"hl_cursor:{wallet}", str(ms)),
        )


def _classify_fill(fill: dict) -> tuple[str | None, int]:
    """Devuelve (action, is_buy_int) donde action es 'open' o 'close'.

    HL fill 'dir' values: Open Long, Open Short, Close Long, Close Short, Buy, Sell
    """
    d = (fill.get("dir") or "").strip()
    if d == "Open Long":
        return "open", 1
    if d == "Open Short":
        return "open", 0
    if d == "Close Long":
        return "close", 1   # cierra LONG → busca trade is_buy=1
    if d == "Close Short":
        return "close", 0
    # Fallback Buy/Sell — interpretamos como apertura
    if d == "Buy":
        return "open", 1
    if d == "Sell":
        return "open", 0
    return None, 0


def _extract_leverage(fill: dict) -> float:
    """leverage puede ser flat number o {value: N, type: 'cross'}."""
    lev = fill.get("leverage")
    if isinstance(lev, dict):
        try:
            return float(lev.get("value", 1))
        except Exception:
            return 1.0
    if isinstance(lev, (int, float)):
        return float(lev)
    return 1.0


async def _process_wallet(client: HyperliquidClient, wallet: str) -> tuple[int, int]:
    """Polea trades de un wallet. Devuelve (examined, actions)."""
    cursor = _get_cursor(wallet)
    if cursor is None:
        # Primer arranque — no procesar histórico
        cursor = int(time.time() * 1000)
        _set_cursor(wallet, cursor)
        return 0, 0

    try:
        fills = await client.user_fills(wallet, limit=100)
    except Exception as e:
        log.warning("HL polling %s falló: %s", wallet[:10], e)
        return 0, 0

    new_fills = [f for f in fills if int(f.get("time") or 0) > cursor]
    new_fills.sort(key=lambda f: int(f.get("time") or 0))

    actions = 0
    last_ts = cursor
    for f in new_fills:
        ts_ms = int(f.get("time") or 0)
        ts_sec = ts_ms // 1000
        coin = f.get("coin")
        action, is_buy = _classify_fill(f)
        if not coin or not action:
            last_ts = max(last_ts, ts_ms)
            continue

        try:
            price = float(f.get("px") or 0)
        except (TypeError, ValueError):
            last_ts = max(last_ts, ts_ms)
            continue

        # source_fill_id = combo único del fill
        oid = f.get("oid", "")
        tid = f.get("tid", "")
        fill_id = f"{wallet}:{tid}:{oid}"

        leverage = _extract_leverage(f)

        try:
            # Production parity: walk L2 book + 2s latency. Fills viejos
            # caen a slippage simple.
            realistic_px = await _realistic_fill_price(
                client, coin, HL_BASE_USDC, is_buy, ts_ms
            )
            if action == "open":
                tid_, reason = hl_executor.open_position(
                    source_wallet=wallet, source_fill_id=fill_id,
                    coin=coin, is_buy=is_buy, source_price=price,
                    leverage=leverage, timestamp=ts_sec, raw=f,
                    realistic_entry_price=realistic_px,
                )
                if tid_:
                    actions += 1
            elif action == "close":
                pid = hl_executor.close_position(
                    source_wallet=wallet, coin=coin, is_buy=is_buy,
                    source_price=price, timestamp=ts_sec,
                    realistic_exit_price=realistic_px,
                )
                if pid:
                    actions += 1
        except Exception as e:
            log.exception("HL error procesando fill %s: %s", fill_id, e)

        last_ts = max(last_ts, ts_ms)

    if last_ts > cursor:
        _set_cursor(wallet, last_ts)
    return len(new_fills), actions


async def _sweep_open_positions(client: HyperliquidClient) -> None:
    """SL/TP/trailing/liquidation check sobre todas las posiciones abiertas."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, coin, is_buy, entry_price, peak_price, leverage, "
            "liquidation_price FROM hl_trades WHERE status='open'"
        ).fetchall()

    if not rows:
        return

    try:
        mids = await client.all_mids()
    except Exception as e:
        log.warning("HL all_mids failed: %s", e)
        return

    for r in rows:
        coin = r["coin"]
        if coin not in mids:
            continue
        try:
            cur = float(mids[coin])
        except (TypeError, ValueError):
            continue
        entry = r["entry_price"]
        is_buy = r["is_buy"]
        leverage = r["leverage"] or 1.0

        # Update peak (positive direction depends on is_buy)
        if is_buy:
            new_peak = max(r["peak_price"] or entry, cur)
        else:
            # LONG: peak = max price seen. SHORT: peak = MIN price seen.
            new_peak = min(r["peak_price"] or entry, cur)
        if new_peak != r["peak_price"]:
            with tx() as c:
                c.execute("UPDATE hl_trades SET peak_price=? WHERE id=?", (new_peak, r["id"]))

        # PnL pct (escalado por leverage no — SL/TP en pct precio)
        if is_buy:
            pnl_pct = (cur - entry) / entry
            peak_pnl_pct = (new_peak - entry) / entry
        else:
            pnl_pct = (entry - cur) / entry
            peak_pnl_pct = (entry - new_peak) / entry

        # Liquidation buffer
        liq_px = r["liquidation_price"]
        if liq_px:
            if is_buy and cur < liq_px * HL_LIQUIDATION_BUFFER:
                hl_executor.force_close(r["id"], cur, reason=f"liquidation_buffer_long_{int(pnl_pct*100)}pct")
                continue
            if (not is_buy) and cur > liq_px / HL_LIQUIDATION_BUFFER:
                hl_executor.force_close(r["id"], cur, reason=f"liquidation_buffer_short_{int(pnl_pct*100)}pct")
                continue

        # Trailing stop (cuando peak >= activation, cierra si caída del peak >= drop)
        if peak_pnl_pct >= HL_TRAIL_ACTIVATION_PCT:
            # current PnL vs peak PnL — cuanto bajó del peak
            drop_from_peak = peak_pnl_pct - pnl_pct
            if drop_from_peak >= HL_TRAIL_DROP_PCT:
                hl_executor.force_close(r["id"], cur, reason=f"trailing_stop_from_peak_{int(peak_pnl_pct*100)}")
                continue

        # SL
        if pnl_pct <= -HL_STOP_LOSS_PCT:
            hl_executor.force_close(r["id"], cur, reason=f"stop_loss_{int(-pnl_pct*100)}pct")
            continue
        # TP
        if pnl_pct >= HL_TAKE_PROFIT_PCT:
            hl_executor.force_close(r["id"], cur, reason=f"take_profit_{int(pnl_pct*100)}pct")
            continue


async def hl_run_loop() -> None:
    # Funding accrual hourly task — production parity con perps reales
    from src.copybot.hl_funding import hl_funding_loop
    funding_task = asyncio.create_task(hl_funding_loop(), name="hl_funding_loop")
    return await _hl_run_loop_impl()


async def _hl_run_loop_impl() -> None:
    """Loop principal del bot HL. Async task que corre en paralelo al PM bot."""
    log.info("HL runner: arrancando (sleep=%ds, sweep=%ds)", HL_SLEEP_SECONDS, HL_SWEEP_SECONDS)
    cycle = 0
    last_sweep = 0.0

    async with HyperliquidClient() as client:
        while True:
            cycle += 1
            wallets = _active_wallets()
            if not wallets:
                log.info("HL: no hay wallets activos. Insertá vía hl_seed_wallets.py")
                await asyncio.sleep(60)
                continue

            t0 = time.time()
            results = await asyncio.gather(
                *(_process_wallet(client, w) for w in wallets),
                return_exceptions=True,
            )
            total_examined = 0
            total_actions = 0
            for w, res in zip(wallets, results):
                if isinstance(res, Exception):
                    log.warning("HL wallet %s gather error: %s", w[:10], res)
                    continue
                ex, ac = res
                total_examined += ex
                total_actions += ac

            # Sweep periódico
            now = time.time()
            if now - last_sweep >= HL_SWEEP_SECONDS:
                try:
                    await _sweep_open_positions(client)
                except Exception as e:
                    log.exception("HL sweep error: %s", e)
                last_sweep = now

            dt = time.time() - t0
            if total_actions > 0 or cycle % 30 == 0:
                console.print(
                    f"[blue]HL cycle {cycle}[/blue]  wallets={len(wallets)} "
                    f"examined={total_examined} actions={total_actions} ({dt:.1f}s)"
                )

            await asyncio.sleep(max(1.0, HL_SLEEP_SECONDS - dt))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    try:
        asyncio.run(hl_run_loop())
    except KeyboardInterrupt:
        console.print("[yellow]HL stopped[/yellow]")
