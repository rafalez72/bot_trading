"""Auto-aprendizaje del bot a partir de paper trades.

Reglas (simples, transparentes — todas registradas en `learning_events`):

1. **Sizing dinámico** por trader:
   - Cada vez que se cierra un paper_trade:
     - WIN  → sizing_mult *= 1.05  (cap 2.0)
     - LOSS → sizing_mult *= 0.85  (floor 0.1)
   - Esto premia rachas y baja exposición a wallets que empiezan a fallar.

2. **Drop automático**:
   - 5 pérdidas consecutivas → status='dropped', sizing=0.
   - Pérdida acumulada > -50% del PnL inicial → drop.

3. **Promote (recuperación)**:
   - Wallet `paused` con 3 wins simulados consecutivos en backtest → vuelve a 'active'.

Cada decisión se persiste con before/after y el trigger humano-legible.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Iterable

from src.db.schema import db, tx

log = logging.getLogger(__name__)

WIN_MULT = 1.05
LOSS_MULT = 0.85
SIZING_MIN = 0.1
SIZING_MAX = 2.0
DROP_AFTER_LOSSES = 3
# PnL acumulado catastrófico → drop (independiente de racha consecutiva).
# Default: 10% del capital. En live activamos cuando hay datos significativos.
DROP_PNL_THRESHOLD_USDC = -10.0
DROP_PNL_MIN_TRADES = 5
# Inactivity drop: wallet sin actividad on-chain en N horas → drop.
# Detectado vía paper_cursor:<wallet> en index_state (avanza on-poll si
# hay trade nuevo, sino queda flat).
DROP_INACTIVITY_HOURS = 48

# Auto-pause por PnL reciente negativo (Feature D, 2026-05-09).
# A diferencia de auto_drop_by_inactivity (sin actividad on-chain) o
# auto_drop_by_rejects (señales rechazadas), este detecta wallets activos
# que ESTÁN tradeando pero perdiendo plata en las últimas 24h. Pause vs
# drop porque la mala racha puede revertir; el siguiente select_traders
# decide si reactiva o no según métricas históricas.
AUTO_PAUSE_PNL_24H_USDC = float(os.getenv("AUTO_PAUSE_PNL_24H_USDC", "-2.0"))
AUTO_PAUSE_MIN_TRADES_24H = 2  # mínimo de trades para considerar la muestra


def _log_event(
    conn,
    wallet: str,
    event_type: str,
    before: float | None,
    after: float | None,
    trigger: str,
    snapshot: dict | None = None,
) -> None:
    delta = (after - before) if (before is not None and after is not None) else None
    conn.execute(
        """
        INSERT INTO learning_events
            (wallet, event_type, before_value, after_value, delta, trigger, metric_snapshot)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            wallet,
            event_type,
            before,
            after,
            delta,
            trigger,
            json.dumps(snapshot or {}, separators=(",", ":")),
        ),
    )


def on_paper_trade_closed(paper_trade_id: int) -> None:
    """Llamar cada vez que un trade (paper o live) pasa a estado terminal.

    Pipeline:
      1. Trackear performance por categoría (puede bloquearla).
      2. Detectar racha de pérdidas → drop del trader.
      3. Drop por PnL acumulado catastrófico (independiente de racha).
      4. Recalcular sizings de TODOS los activos vía UCB1.

    Funciona en paper y live: queryea la tabla activa via tradebook.TABLE.
    """
    # Dependencias dentro de la función para evitar import circular
    from src.copybot.bandit import recompute_sizings
    from src.copybot.categories import update_for_paper_trade
    from src.copybot.tradebook import TABLE as TRADES_TABLE

    with tx() as conn:
        pt = conn.execute(
            f"SELECT * FROM {TRADES_TABLE} WHERE id=?", (paper_trade_id,)
        ).fetchone()
        if not pt:
            return
        if pt["status"] not in ("closed_win", "closed_loss", "settled_win", "settled_loss"):
            return
        wallet = pt["source_wallet"]
        is_win = pt["status"].endswith("_win")

        sub = conn.execute(
            "SELECT * FROM copy_subscriptions WHERE wallet=?", (wallet,)
        ).fetchone()
        if not sub:
            return

        # 2) Drop por racha consecutiva
        recent = conn.execute(
            f"""
            SELECT status FROM {TRADES_TABLE}
            WHERE source_wallet=?
              AND status IN ('closed_win','closed_loss','settled_win','settled_loss')
            ORDER BY exit_at DESC LIMIT ?
            """,
            (wallet, DROP_AFTER_LOSSES),
        ).fetchall()
        was_dropped = False
        if (
            len(recent) == DROP_AFTER_LOSSES
            and all(r["status"].endswith("_loss") for r in recent)
        ):
            before = sub["sizing_mult"] or 1.0
            conn.execute(
                """
                UPDATE copy_subscriptions
                SET status='dropped', stopped_at=datetime('now'), sizing_mult=0
                WHERE wallet=?
                """,
                (wallet,),
            )
            _log_event(
                conn, wallet, "drop", before, 0.0,
                f"{DROP_AFTER_LOSSES} pérdidas consecutivas",
                {"reason": "loss_streak"},
            )
            was_dropped = True

        # 3) Drop por PnL acumulado catastrófico (independiente de racha)
        if not was_dropped:
            agg = conn.execute(
                f"""
                SELECT COALESCE(SUM(pnl_usdc), 0) p, COUNT(*) n
                FROM {TRADES_TABLE}
                WHERE source_wallet=?
                  AND status IN ('closed_win','closed_loss','settled_win','settled_loss')
                """,
                (wallet,),
            ).fetchone()
            if agg["n"] >= DROP_PNL_MIN_TRADES and agg["p"] <= DROP_PNL_THRESHOLD_USDC:
                before = sub["sizing_mult"] or 1.0
                conn.execute(
                    """
                    UPDATE copy_subscriptions
                    SET status='dropped', stopped_at=datetime('now'), sizing_mult=0
                    WHERE wallet=?
                    """,
                    (wallet,),
                )
                _log_event(
                    conn, wallet, "drop", before, 0.0,
                    f"PnL acumulado ${agg['p']:+.2f} en {agg['n']} trades",
                    {"reason": "cumulative_pnl", "pnl": agg["p"], "n": agg["n"]},
                )
                was_dropped = True

    # Auto-reemplazo silencioso post-tx (sin notif por pedido del usuario)
    if was_dropped:
        # Auto-reemplazo: re-corre select_traders para llenar la vacante
        # con el siguiente mejor candidato del ranking
        try:
            from src.copybot.selector import DEFAULT_TOP_N, select_traders
            res = select_traders(top_n=DEFAULT_TOP_N)
            if res.get("added"):
                log.info(
                    "auto-replace: +%d nuevos traders activos para reemplazar al dropped",
                    len(res["added"]),
                )
            # Si después del select aún quedan vacantes (no hay candidatos
            # que pasen filtros), pedimos discovery on-demand al runner
            n_active = res.get("total_active", 0)
            if n_active < 20:
                with tx() as conn:
                    conn.execute(
                        """
                        INSERT INTO bot_state (key, value, updated_at)
                        VALUES ('discovery_pending', 'true', datetime('now'))
                        ON CONFLICT(key) DO UPDATE SET
                            value='true', updated_at=datetime('now')
                        """,
                    )
                log.info(
                    "discovery on-demand requested: solo %d/20 activos",
                    n_active,
                )
        except Exception as e:
            log.warning("auto-replace failed: %s", e)

    # 1) Categorías (fuera de la tx anterior porque abre la suya)
    try:
        update_for_paper_trade(paper_trade_id)
    except Exception as e:
        log.exception("category update failed: %s", e)

    # Notificación gain/loss simple por cada cierre
    try:
        from src.copybot.notifier import gain as notif_gain, loss as notif_loss
        pnl_amount = pt["pnl_usdc"] or 0
        with db() as c2:
            total_row = c2.execute(
                """
                SELECT COALESCE(SUM(pnl_usdc), 0) as p FROM paper_trades
                WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss')
                """
            ).fetchone()
        accumulated = total_row["p"] or 0
        if pnl_amount > 0:
            notif_gain(pnl_amount, accumulated)
        elif pnl_amount < 0:
            notif_loss(abs(pnl_amount), accumulated)
    except Exception as e:
        log.warning("gain/loss notif failed: %s", e)

    # 3) Bandit recompute (afuera de tx)
    try:
        recompute_sizings()
    except Exception as e:
        log.exception("bandit recompute failed: %s", e)


def auto_drop_by_rejects(min_rejects: int = 50, window_hours: int = 24) -> int:
    """Drop active wallets que generaron >= min_rejects rejects con 0 fills
    en la ventana window_hours. Devuelve la cantidad de wallets dropped.

    Estos wallets están "tapando el pipeline": sus señales de trade no pasan
    nuestros filtros pero igual ocupan el slot polling. Mejor sacarlos para
    dejar lugar a wallets cuyas señales sí abren posiciones.

    Persiste un learning_event con trigger='reject_clog' y notifica via
    Telegram (`notifier.trader_dropped`) si está disponible.

    Safe en DB fresh: si no hay rows en `live_rejects`, devuelve 0.
    """
    from src.copybot.tradebook import TABLE as TRADES_TABLE

    window_secs = window_hours * 3600
    candidates: list[tuple[str, int, float]] = []  # (wallet, n_rejects, sizing_before)

    with tx() as conn:
        # Buscamos wallets activos con >=min_rejects en la ventana y 0 fills.
        # Usamos un solo query con subqueries correlacionadas — barato porque
        # los índices idx_live_rejects_at y idx_live_source filtran rápido.
        rows = conn.execute(
            f"""
            SELECT cs.wallet AS wallet,
                   cs.sizing_mult AS sizing_mult,
                   (
                     SELECT COUNT(*) FROM live_rejects lr
                     WHERE lr.source_wallet = cs.wallet
                       AND lr.at >= strftime('%s','now') - ?
                   ) AS n_rejects,
                   (
                     SELECT COUNT(*) FROM {TRADES_TABLE} t
                     WHERE t.source_wallet = cs.wallet
                       AND t.entry_at >= strftime('%s','now') - ?
                   ) AS n_fills
            FROM copy_subscriptions cs
            WHERE cs.status = 'active'
            """,
            (window_secs, window_secs),
        ).fetchall()

        for r in rows:
            n_rejects = r["n_rejects"] or 0
            n_fills = r["n_fills"] or 0
            if n_rejects >= min_rejects and n_fills == 0:
                candidates.append((
                    r["wallet"],
                    int(n_rejects),
                    float(r["sizing_mult"] or 1.0),
                ))

        for wallet, n_rejects, sizing_before in candidates:
            conn.execute(
                """
                UPDATE copy_subscriptions
                SET status='dropped', stopped_at=datetime('now'), sizing_mult=0
                WHERE wallet=?
                """,
                (wallet,),
            )
            trigger = f"reject_clog ({n_rejects} rejects, 0 fills in {window_hours}h)"
            _log_event(
                conn, wallet, "drop", sizing_before, 0.0,
                trigger,
                {"reason": "reject_clog", "n_rejects": n_rejects, "window_hours": window_hours},
            )

    # Notif fuera de la tx (best-effort). El notifier acepta argumentos
    # libres pero la API tradicional pasa (wallet, reason).
    if candidates:
        try:
            from src.copybot import notifier
            for wallet, n_rejects, _ in candidates:
                reason = f"reject_clog: {n_rejects} rejects / 0 fills en {window_hours}h"
                try:
                    notifier.trader_dropped(wallet, reason)
                except Exception:
                    pass
        except Exception as e:
            log.warning("trader_dropped notif failed: %s", e)

    return len(candidates)


def auto_drop_by_inactivity(window_hours: int | None = None) -> int:
    """Drop active wallets sin actividad on-chain en window_hours.

    El runner advance `index_state.paper_cursor:<wallet>` cuando ve trades
    nuevos en /trades?user=<wallet>. Si el cursor no se mueve en >72h,
    el wallet está dormido on-chain → drop para liberar el slot.

    NOTA: distinto de `auto_drop_by_rejects` (que detecta wallets activos
    pero con todas sus señales rechazadas) — este detecta wallets sin
    NINGUNA señal del lado del exchange.
    """
    import time as _t

    if window_hours is None:
        window_hours = DROP_INACTIVITY_HOURS
    cutoff = int(_t.time()) - window_hours * 3600

    dropped: list[tuple[str, int]] = []
    with tx() as conn:
        # CAST a BIGINT (no INTEGER) — algunos cursors viejos quedaron en
        # milisegundos por bug histórico del WS bridge (~1.7e12, no cabe en
        # INT32). Normalizamos a segundos en SQL: si valor > 9.99e9 → /1000.
        rows = conn.execute(
            """
            SELECT cs.wallet AS wallet,
                   COALESCE(
                       CASE WHEN CAST(ist.value AS BIGINT) > 9999999999
                            THEN CAST(ist.value AS BIGINT) / 1000
                            ELSE CAST(ist.value AS BIGINT) END,
                       0
                   ) AS cursor_ts
            FROM copy_subscriptions cs
            LEFT JOIN index_state ist ON ist.key = 'paper_cursor:' || cs.wallet
            WHERE cs.status = 'active'
              AND ist.value IS NOT NULL
              AND CASE WHEN CAST(ist.value AS BIGINT) > 9999999999
                       THEN CAST(ist.value AS BIGINT) / 1000
                       ELSE CAST(ist.value AS BIGINT) END < ?
            """,
            (cutoff,),
        ).fetchall()

        for r in rows:
            wallet = r["wallet"]
            cursor = r["cursor_ts"]
            inactive_h = (int(_t.time()) - cursor) / 3600.0
            conn.execute(
                """
                UPDATE copy_subscriptions
                SET status='dropped', stopped_at=datetime('now'), sizing_mult=0
                WHERE wallet=?
                """,
                (wallet,),
            )
            _log_event(
                conn, wallet, "drop", 1.0, 0.0,
                f"inactividad on-chain {inactive_h:.0f}h sin trades",
                {"reason": "inactivity_oncchain", "hours": round(inactive_h, 1)},
            )
            dropped.append((wallet, int(inactive_h)))

    if dropped:
        log.info("auto_drop_by_inactivity: %d wallets dropeados (>%dh sin actividad)",
                 len(dropped), window_hours)
        for w, h in dropped:
            log.info("  drop inactivity: %s.. (%dh)", w[:10], h)
        # Auto-replace tras drops
        try:
            from src.copybot.selector import DEFAULT_TOP_N, select_traders
            select_traders(top_n=DEFAULT_TOP_N)
        except Exception as e:
            log.warning("auto-replace post-inactivity failed: %s", e)

    return len(dropped)


def auto_pause_by_recent_loss(
    *,
    pnl_threshold: float | None = None,
    window_hours: int = 24,
    min_trades: int = AUTO_PAUSE_MIN_TRADES_24H,
) -> int:
    """Pausa wallets ``active`` cuyo PnL en la última ventana sea <= threshold.

    Distinto a:
      - ``auto_drop_by_inactivity``: detecta wallets sin trades on-chain.
      - ``auto_drop_by_rejects``: detecta wallets con señales todas rechazadas.
    Este detecta wallets que SÍ están tradeando, pero perdiendo plata.

    Pause (no drop): la mala racha puede revertir. El próximo
    ``select_traders`` decide si vuelve a 'active' o no.

    Skip:
      - wallets ya ``paused`` o ``dropped``.
      - wallets con menos de ``min_trades`` en la ventana (muestra chica).

    Returns el número de wallets pausados.
    """
    import time as _t
    from src.copybot.tradebook import TABLE as TRADES_TABLE

    threshold = pnl_threshold if pnl_threshold is not None else AUTO_PAUSE_PNL_24H_USDC
    cutoff = int(_t.time()) - window_hours * 3600

    paused: list[tuple[str, float, int]] = []  # (wallet, pnl, n_trades)

    with tx() as conn:
        rows = conn.execute(
            f"""
            SELECT cs.wallet AS wallet,
                   COALESCE(SUM(pt.pnl_usdc), 0) AS recent_pnl,
                   COUNT(pt.id) AS n_trades
            FROM copy_subscriptions cs
            LEFT JOIN {TRADES_TABLE} pt
              ON pt.source_wallet = cs.wallet
             AND pt.entry_at >= ?
             AND pt.status IN ('closed_win','closed_loss','settled_win','settled_loss')
            WHERE cs.status = 'active'
            GROUP BY cs.wallet
            """,
            (cutoff,),
        ).fetchall()

        for r in rows:
            n = int(r["n_trades"] or 0)
            pnl = float(r["recent_pnl"] or 0.0)
            if n < min_trades:
                continue
            if pnl > threshold:
                continue
            wallet = r["wallet"]
            conn.execute(
                """
                UPDATE copy_subscriptions
                SET status='paused', stopped_at=datetime('now')
                WHERE wallet=?
                """,
                (wallet,),
            )
            _log_event(
                conn, wallet, "pause", None, None,
                f"PnL {window_hours}h ${pnl:+.2f} <= ${threshold:+.2f} ({n} trades)",
                {
                    "reason": "auto_paused_recent_loss",
                    "pnl_window_usdc": pnl,
                    "n_trades": n,
                    "window_hours": window_hours,
                    "threshold": threshold,
                },
            )
            paused.append((wallet, pnl, n))

    if paused:
        log.info(
            "auto_pause_by_recent_loss: %d wallets pausados (pnl %dh <= $%.2f)",
            len(paused), window_hours, threshold,
        )
        for w, pnl, n in paused:
            log.info("  pause recent_loss: %s.. pnl=$%+.2f n=%d", w[:10], pnl, n)

    return len(paused)


def recent_events(limit: int = 100) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM learning_events
            ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def summary() -> dict:
    """Resumen agregado del estado del bot."""
    from src.config import BOT_CAPITAL_USDC, COPY_BASE_USDC, DAILY_KILL_SWITCH_PCT
    from src.copybot.risk import kill_switch_status

    with db() as conn:
        copy_stats = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status='active'  THEN 1 ELSE 0 END) as active,
                SUM(CASE WHEN status='paused'  THEN 1 ELSE 0 END) as paused,
                SUM(CASE WHEN status='dropped' THEN 1 ELSE 0 END) as dropped
            FROM copy_subscriptions
            """,
        ).fetchone()

        paper_stats = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_n,
                COALESCE(SUM(CASE WHEN status='open'
                                   THEN entry_size_usdc ELSE 0 END), 0) as open_invested,
                SUM(CASE WHEN status IN ('closed_win','settled_win')   THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status IN ('closed_loss','settled_loss') THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(CASE WHEN status IN ('closed_win','settled_win',
                                                  'closed_loss','settled_loss')
                                   THEN entry_size_usdc ELSE 0 END), 0) as closed_invested,
                COALESCE(SUM(pnl_usdc), 0) as realized_pnl,
                COALESCE(SUM(entry_size_usdc), 0) as total_invested
            FROM paper_trades
            """,
        ).fetchone()

        learn_stats = conn.execute(
            """
            SELECT
                SUM(CASE WHEN event_type='size_up'   THEN 1 ELSE 0 END) as ups,
                SUM(CASE WHEN event_type='size_down' THEN 1 ELSE 0 END) as downs,
                SUM(CASE WHEN event_type='drop'      THEN 1 ELSE 0 END) as drops
            FROM learning_events
            """,
        ).fetchone()

    wins = paper_stats["wins"] or 0
    losses = paper_stats["losses"] or 0
    total = wins + losses
    closed_invested = paper_stats["closed_invested"] or 0
    realized_pnl = paper_stats["realized_pnl"] or 0
    open_invested = paper_stats["open_invested"] or 0

    # PnL del día (rolling 24h)
    import time as _t
    day_start = int(_t.time()) - 86400
    with db() as conn:
        day_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(pnl_usdc), 0) as pnl,
                SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_usdc <= 0 THEN 1 ELSE 0 END) as losses
            FROM paper_trades
            WHERE exit_at >= ?
              AND status IN ('closed_win','closed_loss','settled_win','settled_loss')
            """,
            (day_start,),
        ).fetchone()

    ks = kill_switch_status()
    return {
        "capital": {
            "total_usdc": BOT_CAPITAL_USDC,
            "in_open_positions_usdc": open_invested,
            "available_usdc": max(0.0, BOT_CAPITAL_USDC - open_invested),
            "usage_pct": (open_invested / BOT_CAPITAL_USDC * 100.0) if BOT_CAPITAL_USDC > 0 else 0.0,
            "kill_threshold_pct": DAILY_KILL_SWITCH_PCT * 100.0,
        },
        "kill_switch": ks,
        "today": {
            "pnl_usdc": day_row["pnl"] or 0,
            "wins": day_row["wins"] or 0,
            "losses": day_row["losses"] or 0,
        },
        "copying": {
            "active": copy_stats["active"] or 0,
            "paused": copy_stats["paused"] or 0,
            "dropped": copy_stats["dropped"] or 0,
        },
        "paper": {
            "open": paper_stats["open_n"] or 0,
            "open_invested_usdc": open_invested,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total) if total else 0.0,
            "closed_invested_usdc": closed_invested,
            "realized_pnl_usdc": realized_pnl,
            "roi_pct": (realized_pnl / closed_invested * 100.0) if closed_invested > 0 else 0.0,
            "total_invested_usdc": paper_stats["total_invested"] or 0,
            "total_pnl_usdc": realized_pnl,
            "total_volume_usdc": paper_stats["total_invested"] or 0,
            "trades_total": total,
            "base_bet_usdc": COPY_BASE_USDC,
        },
        "learning": {
            "size_increases": learn_stats["ups"] or 0,
            "size_decreases": learn_stats["downs"] or 0,
            "drops": learn_stats["drops"] or 0,
        },
    }
