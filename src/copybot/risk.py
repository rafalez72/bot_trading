"""Stop-loss, take-profit y kill switch.

- `sweep_stops`: corre periódicamente, fetch del precio actual de cada
  asset con posición abierta, cierra si pasa el umbral.
- `check_kill_switch`: si PnL de hoy < -DAILY_KILL_SWITCH_PCT del capital,
  setea `bot_state.kill_switch=active` y pausa nuevas aperturas.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

import httpx

from src.config import (
    BOT_CAPITAL_USDC,
    DAILY_KILL_SWITCH_PCT,
    DATA_API,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
)
from src.copybot.paper import force_close
from src.db.schema import db, tx

log = logging.getLogger(__name__)


# ---------------- Kill switch ----------------

def _set_kill(active: bool, reason: str = "") -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES ('kill_switch', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            ("active" if active else "inactive",),
        )
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES ('kill_switch_reason', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (reason,),
        )


def kill_switch_status() -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM bot_state "
            "WHERE key IN ('kill_switch', 'kill_switch_reason')"
        ).fetchall()
    state = {r["key"]: dict(r) for r in rows}
    active = state.get("kill_switch", {}).get("value") == "active"
    return {
        "active": active,
        "reason": state.get("kill_switch_reason", {}).get("value", ""),
        "since": state.get("kill_switch", {}).get("updated_at"),
    }


def check_kill_switch() -> bool:
    """Recalcula. Devuelve True si quedó (o sigue) activo."""
    today_start = int(time.time()) - 86400  # rolling 24h
    with db() as conn:
        r = conn.execute(
            """
            SELECT COALESCE(SUM(pnl_usdc), 0) as pnl
            FROM paper_trades
            WHERE exit_at >= ?
              AND status IN ('closed_win','closed_loss','settled_win','settled_loss')
            """,
            (today_start,),
        ).fetchone()
    pnl = r["pnl"] or 0
    threshold = -BOT_CAPITAL_USDC * DAILY_KILL_SWITCH_PCT
    if pnl <= threshold:
        prev = kill_switch_status()["active"]
        reason = f"PnL 24h ${pnl:.2f} <= -{DAILY_KILL_SWITCH_PCT*100:.0f}% del capital"
        _set_kill(True, reason)
        if not prev:
            try:
                from src.copybot.notifier import kill_switch_activated
                kill_switch_activated(reason, pnl)
            except Exception as e:
                log.warning("notifier failed: %s", e)
        return True
    # Si está activo pero ya no se cumple la condición, lo dejamos manual
    # (un humano debe inspeccionar antes de re-activar)
    return False


def reset_kill_switch() -> None:
    _set_kill(False, "manual reset")
    try:
        from src.copybot.notifier import kill_switch_deactivated
        kill_switch_deactivated()
    except Exception:
        pass


# ---------------- Stop-loss / take-profit ----------------

async def _last_price(client: httpx.AsyncClient, asset: str) -> float | None:
    try:
        r = await client.get(
            f"{DATA_API}/trades", params={"asset": asset, "limit": 1}
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        return float(data[0].get("price") or 0)
    except Exception as e:
        log.debug("price fetch failed asset=%s: %s", asset, e)
        return None


async def sweep_stops() -> dict:
    """Recorre todas las paper_trades open y cierra las que disparen stop/tp."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, asset, entry_price, entry_size_usdc, source_wallet
            FROM paper_trades
            WHERE status='open' AND asset IS NOT NULL
            """,
        ).fetchall()

    if not rows:
        return {"checked": 0, "stop_loss": 0, "take_profit": 0}

    # Agrupar por asset para evitar requests duplicados
    by_asset: dict[str, list] = defaultdict(list)
    for r in rows:
        by_asset[r["asset"]].append(dict(r))

    sl_count = 0
    tp_count = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        for asset, positions in by_asset.items():
            cur = await _last_price(client, asset)
            if cur is None:
                continue
            for p in positions:
                entry = p["entry_price"] or 0
                if entry <= 0:
                    continue
                drop = (entry - cur) / entry
                rise = (cur - entry) / entry
                if drop >= STOP_LOSS_PCT:
                    force_close(p["id"], cur, reason=f"stop_loss_{int(drop*100)}pct")
                    sl_count += 1
                    log.info(
                        "STOP-LOSS  paper #%d  entry=%.3f cur=%.3f  -%.0f%%",
                        p["id"], entry, cur, drop * 100,
                    )
                    # Notif solo si es grande (>$3 perdido)
                    loss_usdc = (cur - entry) * (p["entry_size_usdc"] / entry)
                    if loss_usdc <= -3.0:
                        try:
                            from src.copybot.notifier import big_stop_loss
                            wallet = (p.get("source_wallet") or "").lower()
                            big_stop_loss(p["id"], wallet, loss_usdc, entry, cur)
                        except Exception:
                            pass
                elif TAKE_PROFIT_PCT > 0 and rise >= TAKE_PROFIT_PCT:
                    force_close(p["id"], cur, reason=f"take_profit_{int(rise*100)}pct")
                    tp_count += 1
                    log.info(
                        "TAKE-PROFIT paper #%d  entry=%.3f cur=%.3f  +%.0f%%",
                        p["id"], entry, cur, rise * 100,
                    )
                    gain_usdc = (cur - entry) * (p["entry_size_usdc"] / entry)
                    if gain_usdc >= 3.0:
                        try:
                            from src.copybot.notifier import big_take_profit
                            wallet = (p.get("source_wallet") or "").lower()
                            big_take_profit(p["id"], wallet, gain_usdc, entry, cur)
                        except Exception:
                            pass

    return {"checked": len(rows), "stop_loss": sl_count, "take_profit": tp_count}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("kill_switch:", kill_switch_status())
    print("checking…", check_kill_switch())
    print("sweeping…", asyncio.run(sweep_stops()))
