"""Runner del bot dYdX v4 — corre en paralelo al PM y HL.

Activado por env DX_MODE=true. Cursor en index_state con clave 'dx_cursor:<wallet>'.
Cursor en milisegundos epoch (createdAt en ms).

Estrategia para clasificar fills (open vs close):
  dYdX no marca explícitamente "open" vs "close" en el fill. Inferimos:
  - Si tenemos posición open de ese wallet+ticker en dx_trades:
      → side opuesto al entry = close
      → side igual al entry = ignorar (incrementar posición no soportado en v1)
  - Si NO tenemos posición open de ese wallet+ticker:
      → side actual = open

Esto es simplificación. Si el wallet hace size scaling, no lo capturamos.
"""
from __future__ import annotations

import asyncio
import logging
import time

from rich.console import Console

from src.config import (
    DX_SLEEP_SECONDS,
    DX_STOP_LOSS_PCT,
    DX_TAKE_PROFIT_PCT,
    DX_TRAIL_ACTIVATION_PCT,
    DX_TRAIL_DROP_PCT,
    DX_LIQUIDATION_BUFFER,
    DX_SWEEP_SECONDS,
)
from src.copybot import dx_executor
from src.db.schema import db, tx
from src.dydx.client import DydxClient

log = logging.getLogger(__name__)
console = Console()


def _active_wallets() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT wallet FROM dx_subscriptions WHERE status='active'"
        ).fetchall()
    return [r["wallet"] for r in rows]


def _get_cursor(wallet: str) -> int | None:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM index_state WHERE key=?",
            (f"dx_cursor:{wallet}",),
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
            (f"dx_cursor:{wallet}", str(ms)),
        )


def _has_open(source_wallet: str, ticker: str) -> int | None:
    """Devuelve is_buy del trade open existente, o None si no hay."""
    with db() as conn:
        r = conn.execute(
            "SELECT is_buy FROM dx_trades WHERE source_wallet=? AND ticker=? AND status='open' LIMIT 1",
            (source_wallet, ticker),
        ).fetchone()
    return r["is_buy"] if r else None


async def _process_wallet(client: DydxClient, wallet: str) -> tuple[int, int]:
    cursor = _get_cursor(wallet)
    if cursor is None:
        cursor = int(time.time() * 1000)
        _set_cursor(wallet, cursor)
        return 0, 0

    try:
        fills = await client.fills(wallet, subaccount=0, limit=50)
    except Exception as e:
        log.warning("DX polling %s falló: %s", wallet[:14], e)
        return 0, 0

    new_fills = []
    for f in fills:
        # createdAt is ISO string; convert to ms
        created = f.get("createdAt") or ""
        try:
            from datetime import datetime
            ts_ms = int(datetime.fromisoformat(created.replace("Z","+00:00")).timestamp() * 1000)
        except Exception:
            continue
        if ts_ms > cursor:
            f["_ts_ms"] = ts_ms
            new_fills.append(f)
    new_fills.sort(key=lambda f: f["_ts_ms"])

    actions = 0
    last_ts = cursor
    for f in new_fills:
        ts_ms = f["_ts_ms"]
        ts_sec = ts_ms // 1000
        ticker = f.get("ticker") or f.get("market")
        side_raw = (f.get("side") or "").upper()
        is_buy = 1 if side_raw == "BUY" else 0
        try:
            price = float(f.get("price") or 0)
        except (TypeError, ValueError):
            last_ts = max(last_ts, ts_ms)
            continue
        if not ticker or price <= 0:
            last_ts = max(last_ts, ts_ms)
            continue

        # Determinar action por presencia de posición open
        existing_is_buy = _has_open(wallet, ticker)
        if existing_is_buy is None:
            action = "open"
        elif existing_is_buy != is_buy:
            action = "close"  # side opuesto = cierra
        else:
            # Mismo side = scale-up. Por ahora ignoramos.
            last_ts = max(last_ts, ts_ms)
            continue

        eid = f.get("eventId") or f.get("transactionHash") or f.get("createdAtHeight") or ""
        fill_id = f"{wallet}:{ticker}:{ts_ms}:{eid}"

        try:
            if action == "open":
                tid_, reason = dx_executor.open_position(
                    source_wallet=wallet, source_fill_id=fill_id,
                    ticker=ticker, is_buy=is_buy, source_price=price,
                    leverage=1.0, timestamp=ts_sec, raw=f,
                )
                if tid_:
                    actions += 1
            else:  # close
                pid = dx_executor.close_position(
                    source_wallet=wallet, ticker=ticker, is_buy=is_buy,
                    source_price=price, timestamp=ts_sec,
                )
                if pid:
                    actions += 1
        except Exception as e:
            log.exception("DX error procesando fill %s: %s", fill_id, e)

        last_ts = max(last_ts, ts_ms)

    if last_ts > cursor:
        _set_cursor(wallet, last_ts)
    return len(new_fills), actions


async def _sweep_open_positions(client: DydxClient) -> None:
    """SL/TP/trailing/liquidation sobre posiciones abiertas."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, ticker, is_buy, entry_price, peak_price, leverage, "
            "liquidation_price FROM dx_trades WHERE status='open'"
        ).fetchall()

    if not rows:
        return

    # Agrupamos por ticker y fetcheamos un orderbook por unique ticker
    tickers = {r["ticker"] for r in rows if r["ticker"]}
    mids: dict[str, float] = {}
    for tk in tickers:
        try:
            ob = await client.orderbook(tk)
            bids = ob.get("bids", []) if isinstance(ob, dict) else []
            asks = ob.get("asks", []) if isinstance(ob, dict) else []
            if bids and asks:
                mids[tk] = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
        except Exception as e:
            log.debug("DX orderbook %s err: %s", tk, e)

    for r in rows:
        cur = mids.get(r["ticker"])
        if not cur:
            continue
        entry = r["entry_price"]
        is_buy = r["is_buy"]
        leverage = r["leverage"] or 1.0

        if is_buy:
            new_peak = max(r["peak_price"] or entry, cur)
        else:
            new_peak = min(r["peak_price"] or entry, cur)
        if new_peak != r["peak_price"]:
            with tx() as c:
                c.execute("UPDATE dx_trades SET peak_price=? WHERE id=?", (new_peak, r["id"]))

        if is_buy:
            pnl_pct = (cur - entry) / entry
            peak_pct = (new_peak - entry) / entry
        else:
            pnl_pct = (entry - cur) / entry
            peak_pct = (entry - new_peak) / entry

        liq = r["liquidation_price"]
        if liq:
            if is_buy and cur < liq * DX_LIQUIDATION_BUFFER:
                dx_executor.force_close(r["id"], cur, reason=f"liquidation_buffer_long_{int(pnl_pct*100)}pct")
                continue
            if (not is_buy) and cur > liq / DX_LIQUIDATION_BUFFER:
                dx_executor.force_close(r["id"], cur, reason=f"liquidation_buffer_short_{int(pnl_pct*100)}pct")
                continue

        if peak_pct >= DX_TRAIL_ACTIVATION_PCT:
            drop_from_peak = peak_pct - pnl_pct
            if drop_from_peak >= DX_TRAIL_DROP_PCT:
                dx_executor.force_close(r["id"], cur, reason=f"trailing_stop_from_peak_{int(peak_pct*100)}")
                continue

        if pnl_pct <= -DX_STOP_LOSS_PCT:
            dx_executor.force_close(r["id"], cur, reason=f"stop_loss_{int(-pnl_pct*100)}pct")
            continue
        if pnl_pct >= DX_TAKE_PROFIT_PCT:
            dx_executor.force_close(r["id"], cur, reason=f"take_profit_{int(pnl_pct*100)}pct")
            continue


async def dx_run_loop() -> None:
    log.info("DX runner: arrancando (sleep=%ds, sweep=%ds)", DX_SLEEP_SECONDS, DX_SWEEP_SECONDS)
    cycle = 0
    last_sweep = 0.0

    async with DydxClient() as client:
        while True:
            cycle += 1
            wallets = _active_wallets()
            if not wallets:
                log.info("DX: no hay wallets activos. Insertá vía dx_seed_wallets.py")
                await asyncio.sleep(60)
                continue

            t0 = time.time()
            results = await asyncio.gather(
                *(_process_wallet(client, w) for w in wallets),
                return_exceptions=True,
            )
            total_examined, total_actions = 0, 0
            for w, res in zip(wallets, results):
                if isinstance(res, Exception):
                    log.warning("DX wallet %s gather error: %s", w[:14], res)
                    continue
                ex, ac = res
                total_examined += ex
                total_actions += ac

            now = time.time()
            if now - last_sweep >= DX_SWEEP_SECONDS:
                try:
                    await _sweep_open_positions(client)
                except Exception as e:
                    log.exception("DX sweep error: %s", e)
                last_sweep = now

            dt = time.time() - t0
            if total_actions > 0 or cycle % 30 == 0:
                console.print(
                    f"[magenta]DX cycle {cycle}[/magenta]  wallets={len(wallets)} "
                    f"examined={total_examined} actions={total_actions} ({dt:.1f}s)"
                )

            await asyncio.sleep(max(1.0, DX_SLEEP_SECONDS - dt))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    try:
        asyncio.run(dx_run_loop())
    except KeyboardInterrupt:
        console.print("[yellow]DX stopped[/yellow]")
