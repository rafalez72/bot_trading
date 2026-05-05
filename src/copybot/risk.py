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
    LIVE_CAPITAL_USDC,
    LIVE_MODE,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TRAIL_ACTIVATION_PCT,
    TRAIL_DROP_PCT,
)
from src.copybot.tradebook import TABLE as TRADES_TABLE, force_close
from src.db.schema import db, tx

# Cap efectivo según modo: usado por kill_switch para calcular el threshold.
EFFECTIVE_CAPITAL_USDC = LIVE_CAPITAL_USDC if LIVE_MODE else BOT_CAPITAL_USDC

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
        # Reset manual: persistir el timestamp como "high-water mark" para
        # que check_kill_switch ignore los trades viejos que motivaron el
        # disparo. El bot retoma la operación con baseline limpia y solo
        # vuelve a activarse si NUEVAS pérdidas (post-reset) superan el cap.
        if not active:
            conn.execute(
                """
                INSERT INTO bot_state (key, value, updated_at)
                VALUES ('kill_switch_reset_at', ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = datetime('now')
                """,
                (str(int(time.time())),),
            )


def _reset_at() -> int:
    """Lee el timestamp del último reset manual del kill switch.

    Devuelve 0 si nunca se reseteó manualmente (fresh install).
    """
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key='kill_switch_reset_at'"
        ).fetchone()
    if not r:
        return 0
    try:
        return int(r["value"])
    except (TypeError, ValueError):
        return 0


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


RESET_GRACE_SECONDS = 5  # ventana post-reset donde no reactivamos (race protection)


def check_kill_switch() -> bool:
    """Recalcula. Devuelve True si quedó (o sigue) activo.

    El dry-run del live debe comportarse igual que real (es la última
    validación previa a plata real), así que cuenta TODOS los trades
    cerrados de la tabla activa, incluyendo dry_run=1.

    Ventana de evaluación: `max(now-24h, kill_switch_reset_at)`. El reset
    manual actúa como high-water mark — los trades viejos que motivaron
    el último disparo NO vuelven a contar. Si después del reset el PnL
    de los trades nuevos cae por debajo del threshold, se reactiva.

    Auto-recovery: si las pérdidas se recuperan por encima del threshold
    (ej. wins compensan), desactiva automáticamente. Antes era manual-only,
    pero combinado con un race del reset_at quedaba atascado.
    """
    now_ts = int(time.time())
    rolling_24h = now_ts - 86400
    reset_at = _reset_at()
    since = max(rolling_24h, reset_at)
    sql = f"""
        SELECT COALESCE(SUM(pnl_usdc), 0) as pnl,
               COUNT(*) as n
        FROM {TRADES_TABLE}
        WHERE exit_at >= ?
          AND status IN ('closed_win','closed_loss','settled_win','settled_loss')
    """
    with db() as conn:
        r = conn.execute(sql, (since,)).fetchone()
    pnl = r["pnl"] or 0
    threshold = -EFFECTIVE_CAPITAL_USDC * DAILY_KILL_SWITCH_PCT
    prev_active = kill_switch_status()["active"]

    if pnl <= threshold:
        # Race protection: si recién hubo un reset (<10s), saltear la activación.
        # Esto protege contra el caso donde reset_kill_switch corre en paralelo
        # y otro proceso ya leyó el reset_at viejo antes del commit.
        if now_ts - reset_at <= RESET_GRACE_SECONDS:
            log.info(
                "kill switch trigger suppressed: reset hace %ds (grace %ds)",
                now_ts - reset_at, RESET_GRACE_SECONDS,
            )
            return prev_active  # mantener estado actual, no tocar
        window = "desde reset" if since > rolling_24h else "24h"
        reason = (
            f"PnL {window} ${pnl:.2f} <= "
            f"-{DAILY_KILL_SWITCH_PCT*100:.0f}% del capital"
        )
        _set_kill(True, reason)
        if not prev_active:
            try:
                from src.copybot.notifier import kill_switch_activated
                kill_switch_activated(reason, pnl)
            except Exception as e:
                log.warning("notifier failed: %s", e)
        return True

    # Auto-recovery: pnl recuperado. Si estaba activo, desactivar.
    # _set_kill(False, ...) actualiza también el reset_at para que la
    # ventana arranque limpia.
    if prev_active:
        log.info(
            "kill switch auto-recovery: PnL $%.2f > threshold $%.2f",
            pnl, threshold,
        )
        _set_kill(False, f"auto-recovery: PnL ${pnl:+.2f} > threshold")
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
    """Devuelve el midpoint actual del orderbook para `asset` (token_id ERC1155).

    BUG-FIX 2026-05-05: antes usaba data-api.polymarket.com/trades?asset=... pero
    ese endpoint IGNORA el filtro asset y devuelve trades aleatorios → todos los
    assets reportaban el mismo precio falso → SL nunca disparaba. Ahora usa el
    endpoint del CLOB que SÍ filtra por token_id.

    Fallback chain:
      1. CLOB /midpoint  (preferido — precio justo bid/ask)
      2. CLOB /price?side=SELL  (precio actual de venta)
      3. None  (si el mercado no tiene orderbook)
    """
    # Usamos el proxy Vercel para el CLOB porque desde Argentina puede haber
    # geo-block. El bot ya usa CLOB_API que apunta al proxy.
    from src.config import CLOB_API
    try:
        r = await client.get(f"{CLOB_API}/midpoint", params={"token_id": asset}, timeout=8.0)
        if r.status_code == 200:
            mid = r.json().get("mid")
            if mid is not None:
                return float(mid)
    except Exception as e:
        log.debug("midpoint fetch failed asset=%s: %s", str(asset)[:14], e)
    # Fallback: precio SELL (lo que recibirías si vendieras ahora)
    try:
        r = await client.get(f"{CLOB_API}/price",
                             params={"token_id": asset, "side": "SELL"}, timeout=8.0)
        if r.status_code == 200:
            p = r.json().get("price")
            if p is not None:
                return float(p)
    except Exception as e:
        log.debug("price fetch failed asset=%s: %s", str(asset)[:14], e)
    return None


async def sweep_stops() -> dict:
    """Recorre todas las posiciones open (paper o live) y cierra las que disparen stop/tp.

    Order de evaluación:
      1. Update peak_price si cur > peak actual.
      2. Trailing stop (si peak_gain >= TRAIL_ACTIVATION_PCT y cur cae
         TRAIL_DROP_PCT desde el peak) — toma prioridad sobre SL/TP.
      3. Stop-loss tradicional.
      4. Take-profit tradicional.
    """
    sql = f"""
        SELECT id, asset, entry_price, entry_size_usdc, source_wallet, peak_price
        FROM {TRADES_TABLE}
        WHERE status='open' AND asset IS NOT NULL
    """
    with db() as conn:
        rows = conn.execute(sql).fetchall()

    if not rows:
        return {"checked": 0, "stop_loss": 0, "take_profit": 0, "trailing": 0}

    # Agrupar por asset para evitar requests duplicados
    by_asset: dict[str, list] = defaultdict(list)
    for r in rows:
        by_asset[r["asset"]].append(dict(r))

    sl_count = 0
    tp_count = 0
    trail_count = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        for asset, positions in by_asset.items():
            cur = await _last_price(client, asset)
            if cur is None:
                continue
            for p in positions:
                entry = p["entry_price"] or 0
                if entry <= 0:
                    continue

                # 1) Update peak_price si corresponde. Persistimos solo si cambia.
                old_peak = p.get("peak_price")
                old_peak_val = old_peak if old_peak is not None else entry
                new_peak = max(old_peak_val, cur)
                if new_peak > old_peak_val:
                    try:
                        with tx() as conn:
                            conn.execute(
                                f"UPDATE {TRADES_TABLE} SET peak_price=? WHERE id=?",
                                (new_peak, p["id"]),
                            )
                    except Exception as e:
                        log.debug("peak_price update failed id=%s: %s", p["id"], e)

                drop = (entry - cur) / entry
                rise = (cur - entry) / entry
                peak_gain = (new_peak - entry) / entry

                # 2) Trailing stop — corre PRIMERO, toma prioridad sobre SL/TP.
                # Activa solo si el peak alcanzó la activación y el precio
                # actual cayó >=TRAIL_DROP_PCT desde el peak.
                trail_sl = new_peak * (1 - TRAIL_DROP_PCT)
                if peak_gain >= TRAIL_ACTIVATION_PCT and cur < trail_sl:
                    peak_pct = int(peak_gain * 100)
                    force_close(
                        p["id"], cur,
                        reason=f"trailing_stop_from_peak_{peak_pct}",
                    )
                    trail_count += 1
                    log.info(
                        "TRAIL-STOP %s #%d  entry=%.3f peak=%.3f cur=%.3f  peak+%d%% drop -%d%%",
                        TRADES_TABLE, p["id"], entry, new_peak, cur,
                        peak_pct, int((new_peak - cur) / new_peak * 100),
                    )
                    continue

                # 3) Stop-loss tradicional
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
                # 4) Take-profit tradicional
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

    return {
        "checked": len(rows),
        "stop_loss": sl_count,
        "take_profit": tp_count,
        "trailing": trail_count,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("kill_switch:", kill_switch_status())
    print("checking…", check_kill_switch())
    print("sweeping…", asyncio.run(sweep_stops()))
