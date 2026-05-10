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
import os

from src.db.schema import db, tx

log = logging.getLogger(__name__)

REWARD_NORMALIZER = 5.0   # divide PnL por este monto base ($5 default)
SIZING_MIN = 0.1
SIZING_MAX = 2.0
EXPLORATION_BONUS = 2.0   # constante c en UCB1

# Inactivity decay: si un wallet no tradeó en >=INACTIVITY_HOURS, multiplicamos
# su sizing por INACTIVITY_DECAY. Libera capital de "arms dormidas" con score
# histórico alto pero sin actividad reciente. Aplica DESPUÉS de UCB1.
INACTIVITY_HOURS = 24
INACTIVITY_DECAY = 0.7

# Kelly fraccional (opt-in). Si KELLY_SIZING_ENABLED=true, después del UCB
# rebalance, sobrescribimos new_sizings[w] con el bet_size derivado de Kelly
# fraccional 15% sobre stats del wallet últimos 30d. Default OFF (research dice
# que full Kelly da drawdown 50-80%; conservative 15% Kelly recomendado por
# pros). Solo afecta wallets con >= MIN_PULLS_FOR_REBALANCE closes.
KELLY_SIZING_ENABLED = os.getenv("KELLY_SIZING_ENABLED", "false").lower() == "true"
KELLY_FRACTION_PCT = float(os.getenv("KELLY_FRACTION_PCT", "0.15"))
KELLY_LOOKBACK_DAYS = int(os.getenv("KELLY_LOOKBACK_DAYS", "30"))
# Bankroll asumido para convertir Kelly bet → sizing_mult (relative). Si el
# usuario quiere absolute USDC, debe leer kelly_sizing.fractional_bet_size
# directamente desde el executor. Acá usamos un proxy: bet/bankroll → mult.
KELLY_BANKROLL_USDC = float(os.getenv("KELLY_BANKROLL_USDC", "100.0"))

# Floor de samples antes de aplicar size_up/size_down vía UCB:
# con n_pulls < MIN_PULLS_FOR_REBALANCE el rebalance es ruido — un solo trade
# con PnL -$0.50 puede gatillar size_down de 1.0→0.7. 2026-05-08 vimos 17
# learning_events en 6h con n=1-4 cada uno. La política nueva preserva el
# sizing previo (sin tocar la copy_subscription) hasta acumular 5 closes,
# y solo entonces empieza a rebalancear. Los wallets con n=0 siguen recibiendo
# el bonus de exploración (mult 1.0× por default — ya lo manejaba el shift).
MIN_PULLS_FOR_REBALANCE = 5


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


def _kelly_stats_for_wallet(conn, wallet: str, lookback_days: int = KELLY_LOOKBACK_DAYS) -> tuple[float, float, float, int]:
    """Devuelve (win_rate, avg_win, avg_loss, n) últimos `lookback_days` días.

    avg_loss es magnitud positiva. Si n=0 o no hay edge calculable, devuelve ceros.
    """
    from src.copybot.tradebook import TABLE as TRADES_TABLE
    r = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl_usdc < 0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(AVG(CASE WHEN pnl_usdc > 0 THEN pnl_usdc END), 0) AS avg_win,
            COALESCE(AVG(CASE WHEN pnl_usdc < 0 THEN -pnl_usdc END), 0) AS avg_loss,
            COUNT(*) AS n
        FROM {TRADES_TABLE}
        WHERE source_wallet = ?
          AND status IN ('closed_win','closed_loss','settled_win','settled_loss')
          AND entry_at >= (CAST(strftime('%s','now') AS INTEGER) - ?)
        """,
        (wallet, lookback_days * 86400),
    ).fetchone()
    if not r:
        return (0.0, 0.0, 0.0, 0)
    n = int(r["n"] or 0)
    wins = int(r["wins"] or 0)
    if n <= 0:
        return (0.0, 0.0, 0.0, 0)
    wr = wins / n
    return (wr, float(r["avg_win"] or 0.0), float(r["avg_loss"] or 0.0), n)


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

        # Inactivity decay: si el wallet no tradeó en >=INACTIVITY_HOURS, achicamos.
        # Buscamos MAX(entry_at) por wallet en la tabla activa (paper o live).
        from src.copybot.tradebook import TABLE as TRADES_TABLE
        cutoff_ts = None
        # Una sola query agrupada — más barato que un SELECT por wallet.
        last_trade_rows = conn.execute(
            f"""
            SELECT source_wallet, MAX(entry_at) AS last_at
            FROM {TRADES_TABLE}
            WHERE source_wallet IN ({','.join('?' for _ in new_sizings)})
            GROUP BY source_wallet
            """,
            tuple(new_sizings.keys()),
        ).fetchall() if new_sizings else []
        last_at_by_wallet: dict[str, int] = {
            r["source_wallet"]: int(r["last_at"] or 0) for r in last_trade_rows
        }
        import time as _t
        cutoff = int(_t.time()) - INACTIVITY_HOURS * 3600
        for w in list(new_sizings.keys()):
            last_at = last_at_by_wallet.get(w, 0)
            # last_at == 0 → nunca tradeó; tratamos como "inactivo" salvo que
            # sea un arm nuevo sin trades cerrados. Para no castigar arms
            # genuinamente nuevos (que ya tienen UCB inflado por exploración),
            # solo aplicamos decay si HAY un last_at registrado y es viejo.
            #
            # Compound decay: aplicamos INACTIVITY_DECAY una vez por cada
            # bloque de INACTIVITY_HOURS sin actividad. Ej. 48h inactivo → ×0.49.
            # 96h → ×0.24. Sin esto, una arm muerta queda con mult ≈ 0.7 para
            # siempre, desperdiciando capital frente a wallets activos.
            if last_at > 0 and last_at < cutoff:
                inactive_hours = (int(_t.time()) - last_at) / 3600.0
                periods = int(inactive_hours / INACTIVITY_HOURS)
                periods = max(1, min(periods, 6))  # cap a 6 períodos para no overflow
                decay_factor = INACTIVITY_DECAY ** periods
                new_sizings[w] = max(SIZING_MIN, new_sizings[w] * decay_factor)

        # Kelly fraccional override (opt-in via env). Para cada wallet con
        # suficientes closes, computamos bet_size con Kelly 15% y lo convertimos
        # a sizing_mult relativo (bet / bankroll_proxy). Wallets sin edge → 0.0.
        # Capa al rango [SIZING_MIN, SIZING_MAX] como el resto del flujo.
        if KELLY_SIZING_ENABLED:
            from src.copybot.kelly_sizing import fractional_bet_size
            for w in list(new_sizings.keys()):
                wr, avg_win, avg_loss, n_kelly = _kelly_stats_for_wallet(conn, w)
                if n_kelly < MIN_PULLS_FOR_REBALANCE:
                    continue
                bet = fractional_bet_size(
                    bankroll_usdc=KELLY_BANKROLL_USDC,
                    win_rate=wr,
                    avg_win=avg_win,
                    avg_loss=avg_loss,
                    kelly_fraction_pct=KELLY_FRACTION_PCT,
                )
                if bet <= 0.0:
                    new_sizings[w] = SIZING_MIN
                else:
                    mult = bet / KELLY_BANKROLL_USDC * (SIZING_MAX)  # scale a rango
                    new_sizings[w] = max(SIZING_MIN, min(SIZING_MAX, mult))

        # Persistir y registrar cambios significativos.
        # Política de "muestra suficiente": con n < MIN_PULLS_FOR_REBALANCE
        # NO tocamos sizing_mult — preservamos el valor anterior para evitar
        # rebalances ruidosos de 1-4 trades (alta varianza). Sí actualizamos
        # ucb_score (es solo telemetry).
        for r in rows:
            w = r["wallet"]
            before = r["sizing_mult"] or 1.0
            ucb_score = scores.get(w, 0.0)
            conn.execute(
                "UPDATE bandit_state SET ucb_score=? WHERE wallet=?",
                (ucb_score, w),
            )
            n_pulls = r["n"]
            if n_pulls < MIN_PULLS_FOR_REBALANCE:
                # Mantener sizing existente, no emitir learning_event.
                # Usamos el valor anterior como `after` en la dict de retorno
                # para que el caller no piense que hubo cambio.
                new_sizings[w] = before
                continue
            after = new_sizings.get(w, 1.0)
            conn.execute(
                "UPDATE copy_subscriptions SET sizing_mult=? WHERE wallet=?",
                (after, w),
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
                        f"UCB rebalance · n={n_pulls} mean_r={(r['sum_r']/n_pulls):.2f}",
                        f'{{"ucb": {ucb_score:.3f}, "n_pulls": {n_pulls}}}',
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
