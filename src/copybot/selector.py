"""Selección automática de traders a copiar.

Política (transparente y auditable):
- Top N por `score` con `realized_pnl > 0`.
- Excluir wallets ya marcados como `dropped` por learning.
- Generar una razón humana explícita por cada selección.
- Marcar como `paused` los que dejaron de cumplir criterios sin borrarlos
  (preservamos la historia de paper trades).
"""
from __future__ import annotations

import json
import logging

from src.db.schema import db, init_db, tx

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 20
# Filtros estrictos — diseñados para minimizar pérdidas en producción.
# Todo wallet candidato debe cumplir TODOS estos requisitos.
MIN_SCORE = 0.55
MIN_PNL = 500.0              # ganancia realizada mínima
MIN_WIN_RATE = 0.55          # >= 55% de aciertos en cerradas
MIN_TOTAL_TRADES = 150       # historial significativo
MIN_VOLUME = 25_000.0        # liquidez del trader
MAX_DRAWDOWN_PCT = 50.0      # nunca tuvo caída > 50%
MIN_SHARPE = 0.4             # algo de consistencia


def _build_reason(m: dict) -> str:
    parts: list[str] = []
    pnl = m.get("realized_pnl_usdc") or 0
    roi = m.get("roi_pct") or 0
    win = (m.get("win_rate") or 0) * 100
    sharpe = m.get("sharpe_proxy") or 0
    vol = m.get("total_volume_usdc") or 0
    parts.append(f"PnL +${pnl:,.0f}")
    if 0 < roi <= 500:
        parts.append(f"ROI {roi:.0f}%")
    parts.append(f"win {win:.0f}%")
    if sharpe > 0.5:
        parts.append(f"sharpe {sharpe:.1f}")
    if vol > 10_000:
        parts.append(f"vol ${vol/1000:.0f}k")
    return " · ".join(parts)


def select_traders(top_n: int = DEFAULT_TOP_N) -> dict:
    """Sincroniza copy_subscriptions con el top actual.

    Devuelve un resumen: { added, kept, paused, dropped }.
    """
    init_db()
    summary = {"added": [], "kept": [], "paused": [], "total_active": 0}

    # Thresholds dinámicos (auto_filter puede haberlos modificado)
    from src.copybot.auto_filter import get_all as get_thresholds
    th = get_thresholds()

    with db() as conn:
        candidates = conn.execute(
            """
            SELECT * FROM trader_metrics
            WHERE score              >= ?
              AND realized_pnl_usdc  >= ?
              AND win_rate           >= ?
              AND total_trades       >= ?
              AND total_volume_usdc  >= ?
              AND max_drawdown_pct   <= ?
              AND sharpe_proxy       >= ?
            ORDER BY score DESC
            LIMIT ?
            """,
            (
                th["MIN_SCORE"], th["MIN_PNL"], th["MIN_WIN_RATE"],
                th["MIN_TOTAL_TRADES"], th["MIN_VOLUME"],
                th["MAX_DRAWDOWN_PCT"], th["MIN_SHARPE"], top_n,
            ),
        ).fetchall()

        existing = {
            r["wallet"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM copy_subscriptions"
            ).fetchall()
        }

    cand_wallets = {r["wallet"] for r in candidates}
    cand_by_wallet = {r["wallet"]: dict(r) for r in candidates}

    with tx() as conn:
        # 1) Activar / promover candidatos
        for w in cand_wallets:
            metric = cand_by_wallet[w]
            reason = _build_reason(metric)
            score = metric["score"]
            if w not in existing:
                conn.execute(
                    """
                    INSERT INTO copy_subscriptions
                        (wallet, status, reason, score_at_start, sizing_mult)
                    VALUES (?, 'active', ?, ?, 1.0)
                    """,
                    (w, reason, score),
                )
                summary["added"].append({"wallet": w, "reason": reason, "score": score})
            else:
                row = existing[w]
                # IMPORTANTE: NO reactivar wallets `dropped`. Drop es permanente.
                # Antes este bloque hacía UPDATE status='active' sin filtro, lo que
                # deshacía silenciosamente los auto-drops (loss_streak, cumulative_pnl,
                # reject_clog). Si un wallet con score alto fue dropeado por mala
                # performance reciente, debe quedarse fuera. Solo reactivamos `paused`.
                if row["status"] == "dropped":
                    summary.setdefault("skipped_dropped", []).append(
                        {"wallet": w, "reason": "permanently dropped"}
                    )
                    continue
                if row["status"] != "active":  # i.e. 'paused'
                    conn.execute(
                        """
                        UPDATE copy_subscriptions
                        SET status='active', reason=?, stopped_at=NULL
                        WHERE wallet=?
                        """,
                        (reason, w),
                    )
                else:
                    conn.execute(
                        "UPDATE copy_subscriptions SET reason=? WHERE wallet=?",
                        (reason, w),
                    )
                summary["kept"].append({"wallet": w, "reason": reason, "score": score})

        # 2) Pausar wallets activos que ya no están en el top
        for w, row in existing.items():
            if row["status"] == "active" and w not in cand_wallets:
                conn.execute(
                    """
                    UPDATE copy_subscriptions
                    SET status='paused', stopped_at=datetime('now')
                    WHERE wallet=?
                    """,
                    (w,),
                )
                summary["paused"].append({"wallet": w, "reason": "salió del top"})

    with db() as conn:
        summary["total_active"] = conn.execute(
            "SELECT COUNT(*) c FROM copy_subscriptions WHERE status='active'"
        ).fetchone()["c"]

    return summary


def list_active() -> list[dict]:
    """Lista los traders que el bot está copiando con stats agregadas."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                cs.wallet, cs.started_at, cs.reason, cs.score_at_start, cs.sizing_mult,
                cs.notes,
                tm.score, tm.realized_pnl_usdc, tm.roi_pct, tm.win_rate,
                tm.total_trades, tm.total_volume_usdc, tm.sharpe_proxy,
                tm.max_drawdown_pct,
                COALESCE(pt.wins, 0)   as paper_wins,
                COALESCE(pt.losses, 0) as paper_losses,
                COALESCE(pt.open_pos, 0) as paper_open,
                COALESCE(pt.pnl, 0)    as paper_pnl
            FROM copy_subscriptions cs
            LEFT JOIN trader_metrics tm ON tm.wallet = cs.wallet
            LEFT JOIN (
                SELECT
                    source_wallet,
                    SUM(CASE WHEN status IN ('closed_win','settled_win') THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN status IN ('closed_loss','settled_loss') THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_pos,
                    SUM(COALESCE(pnl_usdc, 0)) as pnl
                FROM paper_trades
                GROUP BY source_wallet
            ) pt ON pt.source_wallet = cs.wallet
            WHERE cs.status='active'
            ORDER BY tm.score DESC
            """,
        ).fetchall()
    return [dict(r) for r in rows]
