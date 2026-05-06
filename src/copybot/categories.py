"""Tracking de performance por categoría de mercado + bloqueo automático.

Reglas:
- Cada vez que se cierra un paper_trade, atribuimos el resultado a la
  `category` del mercado (markets.category).
- Después de >= MIN_TRADES_FOR_BLOCK trades en una categoría:
    * Si win_rate < BLOCK_WIN_RATE Y pnl_usdc < 0 → BLOCK
- Las categorías bloqueadas se filtran en `paper.open_position`.
- Re-evaluación: cada N closes recalculamos.
"""
from __future__ import annotations

import logging

from src.db.schema import db, tx

log = logging.getLogger(__name__)

MIN_TRADES_FOR_BLOCK = 10
BLOCK_WIN_RATE = 0.40
UNBLOCK_WIN_RATE = 0.55  # para volver a abrir hace falta más alto


def _category_for(condition_id: str) -> str | None:
    with db() as conn:
        r = conn.execute(
            "SELECT category FROM markets WHERE condition_id=?", (condition_id,)
        ).fetchone()
    return r["category"] if r and r["category"] else None


def update_for_paper_trade(paper_trade_id: int) -> None:
    """Llamar tras cada cierre — actualiza la performance de la categoría."""
    with db() as conn:
        pt = conn.execute(
            "SELECT condition_id, status, pnl_usdc, entry_size_usdc FROM paper_trades WHERE id=?",
            (paper_trade_id,),
        ).fetchone()
        if not pt:
            return
        if pt["status"] not in ("closed_win", "closed_loss", "settled_win", "settled_loss"):
            return

    cat = _category_for(pt["condition_id"])
    if not cat:
        cat = "(sin categoría)"

    is_win = pt["status"].endswith("_win")
    pnl = pt["pnl_usdc"] or 0
    invested = pt["entry_size_usdc"] or 0

    with tx() as conn:
        conn.execute(
            """
            INSERT INTO category_perf
                (category, n_trades, wins, losses, pnl_usdc, invested_usdc, status, updated_at)
            VALUES (?, 1, ?, ?, ?, ?, 'allowed', datetime('now'))
            ON CONFLICT(category) DO UPDATE SET
                n_trades       = n_trades + 1,
                wins           = wins   + excluded.wins,
                losses         = losses + excluded.losses,
                pnl_usdc       = pnl_usdc + excluded.pnl_usdc,
                invested_usdc  = invested_usdc + excluded.invested_usdc,
                updated_at     = datetime('now')
            """,
            (cat, 1 if is_win else 0, 0 if is_win else 1, pnl, invested),
        )
        # Re-evaluar bloqueo
        row = conn.execute(
            "SELECT n_trades, wins, losses, pnl_usdc, status FROM category_perf WHERE category=?",
            (cat,),
        ).fetchone()
        if not row:
            return
        n = row["n_trades"]
        wins_total = row["wins"]
        wr = wins_total / n if n else 0.0
        cur_status = row["status"]

        if cur_status == "allowed" and n >= MIN_TRADES_FOR_BLOCK:
            if wr < BLOCK_WIN_RATE and (row["pnl_usdc"] or 0) < 0:
                conn.execute(
                    """
                    UPDATE category_perf SET status='blocked',
                        blocked_at=datetime('now'),
                        blocked_reason=?
                    WHERE category=?
                    """,
                    (
                        f"win_rate {wr*100:.0f}% < {BLOCK_WIN_RATE*100:.0f}% "
                        f"con PnL ${row['pnl_usdc']:.2f}",
                        cat,
                    ),
                )
                log.warning("Categoría '%s' BLOQUEADA: %s", cat, wr)
                # Registrar también en learning_events
                conn.execute(
                    """
                    INSERT INTO learning_events
                        (wallet, event_type, before_value, after_value, delta, trigger, metric_snapshot)
                    VALUES ('(category)', 'category_block', NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        f"Categoría '{cat}' bloqueada — wr {wr*100:.0f}%, PnL ${row['pnl_usdc']:.2f}",
                        f'{{"category": "{cat}", "n": {n}, "wr": {wr:.3f}}}',
                    ),
                )
        elif cur_status == "blocked" and n >= MIN_TRADES_FOR_BLOCK * 2:
            # Permitir desbloqueo si vuelve a ser confiable (con bar más alta)
            if wr >= UNBLOCK_WIN_RATE and (row["pnl_usdc"] or 0) > 0:
                conn.execute(
                    """
                    UPDATE category_perf SET status='allowed',
                        blocked_at=NULL, blocked_reason=NULL
                    WHERE category=?
                    """,
                    (cat,),
                )
                log.info("Categoría '%s' DESBLOQUEADA", cat)


def is_blocked(condition_id: str) -> bool:
    cat = _category_for(condition_id)
    if not cat:
        return False
    with db() as conn:
        r = conn.execute(
            "SELECT status FROM category_perf WHERE category=?", (cat,)
        ).fetchone()
    return bool(r and r["status"] == "blocked")


def stats() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM category_perf
            ORDER BY pnl_usdc DESC
            """,
        ).fetchall()
    return [dict(r) for r in rows]
