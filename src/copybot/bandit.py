"""Asignación de capital con bandit UCB1.

En vez de subir/bajar sizing_mult ×1.05 / ×0.85 por trade, recalculamos
el sizing de cada trader como la **prioridad UCB1** normalizada al pool
de traders activos.

UCB1:  score_i = mean_reward_i + sqrt(2 * ln(N) / n_i)

  - mean_reward_i = mean(PnL_normalizado de los closes del trader i)
  - n_i = paper trades cerrados del trader i
  - N = total de paper trades cerrados

Para arms con n_i = 0 (trader nuevo), damos prioridad infinita (bonus).

Sizing final:
  - Cada trader activo recibe sizing ∝ score
  - Normalizamos para que la SUMA de sizings sea = N (los traders activos)
  - Clamp a [SIZING_MIN, SIZING_MAX]

Ventaja vs el sistema anterior:
  - No castiga por una loss ruidosa (el sqrt(ln N / n) suaviza)
  - Premia exploración: trader nuevo o poco probado tiene size alto
  - Concentra capital en los que comprovaron ganar consistentemente
"""
from __future__ import annotations

import logging
import math

from src.db.schema import db, tx

log = logging.getLogger(__name__)

REWARD_NORMALIZER = 5.0   # divide PnL por este monto base ($5 default)
SIZING_MIN = 0.1
SIZING_MAX = 2.0
EXPLORATION_BONUS = 2.0   # constante c en UCB1


def _refresh_arm(conn, wallet: str) -> None:
    """Recalcula n_pulls y sum_reward para un wallet."""
    from src.copybot.tradebook import TABLE as TRADES_TABLE
    r = conn.execute(
        f"""
        SELECT
            COUNT(*) as n,
            COALESCE(SUM(pnl_usdc), 0) as pnl
        FROM {TRADES_TABLE}
        WHERE source_wallet=?
          AND status IN ('closed_win','closed_loss','settled_win','settled_loss')
        """,
        (wallet,),
    ).fetchone()
    n = r["n"] or 0
    pnl_sum = r["pnl"] or 0
    sum_reward = pnl_sum / REWARD_NORMALIZER  # normalizado en "múltiplos de la base"
    conn.execute(
        """
        INSERT INTO bandit_state (wallet, n_pulls, sum_reward, ucb_score, updated_at)
        VALUES (?, ?, ?, 0, datetime('now'))
        ON CONFLICT(wallet) DO UPDATE SET
            n_pulls = excluded.n_pulls,
            sum_reward = excluded.sum_reward,
            updated_at = datetime('now')
        """,
        (wallet, n, sum_reward),
    )


def recompute_sizings() -> dict:
    """Recalcula sizing_mult de TODOS los wallets activos vía UCB1.

    Devuelve { wallet: new_sizing }.
    """
    with tx() as conn:
        active = conn.execute(
            "SELECT wallet, sizing_mult FROM copy_subscriptions WHERE status='active'"
        ).fetchall()
        for r in active:
            _refresh_arm(conn, r["wallet"])

        rows = conn.execute(
            """
            SELECT cs.wallet, cs.sizing_mult,
                   COALESCE(bs.n_pulls, 0) as n,
                   COALESCE(bs.sum_reward, 0) as sum_r
            FROM copy_subscriptions cs
            LEFT JOIN bandit_state bs ON bs.wallet = cs.wallet
            WHERE cs.status='active'
            """,
        ).fetchall()
        if not rows:
            return {}

        total_n = sum(r["n"] for r in rows)
        ln_total = math.log(max(total_n, 1) + 1)

        scores: dict[str, float] = {}
        for r in rows:
            n = r["n"]
            mean_r = (r["sum_r"] / n) if n > 0 else 0.0
            if n == 0:
                # Bonus de exploración alto para arms nuevos
                ucb = mean_r + 1.0
            else:
                ucb = mean_r + math.sqrt(EXPLORATION_BONUS * ln_total / n)
            scores[r["wallet"]] = ucb

        # Shift para que el mínimo sea 0.1 (evitar sizings negativos por PnL acumulado negativo)
        if scores:
            min_s = min(scores.values())
            shift = max(0.0, -min_s + 0.1)
            scores = {w: s + shift for w, s in scores.items()}

        # Normalizar para que la suma == cantidad de wallets (cada uno "promedia" 1.0×)
        total_score = sum(scores.values()) or 1.0
        n_wallets = len(scores)
        target_sum = float(n_wallets)
        new_sizings: dict[str, float] = {}
        for w, s in scores.items():
            mult = (s / total_score) * target_sum
            mult = max(SIZING_MIN, min(SIZING_MAX, mult))
            new_sizings[w] = mult

        # Persistir y registrar cambios significativos
        for r in rows:
            w = r["wallet"]
            before = r["sizing_mult"] or 1.0
            after = new_sizings.get(w, 1.0)
            ucb_score = scores.get(w, 0.0)
            conn.execute(
                "UPDATE copy_subscriptions SET sizing_mult=? WHERE wallet=?",
                (after, w),
            )
            conn.execute(
                "UPDATE bandit_state SET ucb_score=? WHERE wallet=?",
                (ucb_score, w),
            )
            if abs(after - before) >= 0.05:
                event = "size_up" if after > before else "size_down"
                conn.execute(
                    """
                    INSERT INTO learning_events
                        (wallet, event_type, before_value, after_value, delta, trigger, metric_snapshot)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        w, event, before, after, after - before,
                        f"UCB rebalance · n={r['n']} mean_r={(r['sum_r']/r['n'] if r['n']>0 else 0):.2f}",
                        f'{{"ucb": {ucb_score:.3f}, "n_pulls": {r["n"]}}}',
                    ),
                )

    return new_sizings


def status() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT cs.wallet, cs.sizing_mult,
                   COALESCE(bs.n_pulls, 0) as n_pulls,
                   COALESCE(bs.sum_reward, 0) as sum_reward,
                   COALESCE(bs.ucb_score, 0) as ucb_score
            FROM copy_subscriptions cs
            LEFT JOIN bandit_state bs ON bs.wallet = cs.wallet
            WHERE cs.status='active'
            ORDER BY ucb_score DESC
            """,
        ).fetchall()
    return [dict(r) for r in rows]
