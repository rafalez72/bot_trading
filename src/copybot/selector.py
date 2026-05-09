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
import os

from src.db.schema import db, init_db, tx

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 40  # 2026-05-08: 20 → 40 para diversificar capital y activar más wallets paused

# Cap del bucket HFT/scalpers (Nivel 1, 2026-05-08): wallets con muchísima
# actividad pero posiblemente menor PnL por trade. Complementan al bucket
# clásico (top by score). Default 20 = 50% del top_n principal.
DEFAULT_HFT_TOP_N = 20

# --- Filtros del bucket CLÁSICO (top by score) ---
# Todo wallet candidato debe cumplir TODOS estos requisitos.
MIN_SCORE = 0.55
MIN_PNL = 500.0              # ganancia realizada mínima
MIN_WIN_RATE = 0.55          # >= 55% de aciertos en cerradas
MIN_TOTAL_TRADES = 150       # historial significativo
MIN_VOLUME = 25_000.0        # liquidez del trader
MAX_DRAWDOWN_PCT = 50.0      # nunca tuvo caída > 50%
MIN_SHARPE = 0.4             # algo de consistencia

# --- Filtros del bucket HFT/scalper (top by trades_per_day) ---
# El selector clásico privilegia rentabilidad histórica y consistencia, lo que
# excluye scalpers que hacen 100s de ops/día con bajo PnL/trade pero
# acumulado positivo. Esos wallets son un pattern legítimo en Polymarket
# (especialmente en sports y crypto markets de 15min) y son la mayor fuente
# de volumen continuo. Los aceptamos con criterios más permisivos pero
# controlando el riesgo via MAX_DRAWDOWN, win_rate >= 50% y PnL acumulado >0.
HFT_MIN_PNL = 50.0           # acumulado mínimo (validación de no-perdedor neto)
HFT_MIN_WIN_RATE = 0.50      # 50% es el piso de "no random"
HFT_MIN_TOTAL_TRADES = 300   # historial robusto (3x el clásico)
HFT_MIN_VOLUME = 5_000.0     # vol total relajado (scalpers operan chico)
HFT_MAX_DRAWDOWN_PCT = 60.0  # tolerancia mayor pero acotada
HFT_MIN_TRADES_PER_DAY = 5.0 # >=5 ops/día sostenido = clasificación HFT
HFT_MIN_DAYS_ACTIVE = 7      # historial mínimo de 7 días para ser estadísticamente válido

# Post-fetch sanity filters (Feature C, 2026-05-09).
# Tras 13h con el bucket HFT activo (cf9c812) detectamos dos patrones tóxicos:
#   1) sharpe absurdamente alto (e.g. 200+) sobre muestras chicas → ruido,
#      no edge real. Lo aceptamos solo si total_trades >= 200 (sample grande
#      hace al sharpe alto creíble).
#   2) ratio volume/PnL altísimo (>>100x) → spread-scalper de microspreads
#      que NO podemos capturar con copy lag. Su edge vive en microsegundos.
HFT_MAX_SHARPE = float(os.getenv("HFT_MAX_SHARPE", "5.0"))
HFT_SHARPE_LARGE_SAMPLE = 200       # umbral de muestra para tolerar sharpe alto
HFT_MAX_VOL_PNL_RATIO = float(os.getenv("HFT_MAX_VOL_PNL_RATIO", "100.0"))


def _build_reason(m: dict) -> str:
    parts: list[str] = []
    pnl = m.get("realized_pnl_usdc") or 0
    roi = m.get("roi_pct") or 0
    win = (m.get("win_rate") or 0) * 100
    sharpe = m.get("sharpe_proxy") or 0
    vol = m.get("total_volume_usdc") or 0
    tpd = m.get("trades_per_day") or 0
    parts.append(f"PnL +${pnl:,.0f}")
    if 0 < roi <= 500:
        parts.append(f"ROI {roi:.0f}%")
    parts.append(f"win {win:.0f}%")
    if sharpe > 0.5:
        parts.append(f"sharpe {sharpe:.1f}")
    if vol > 10_000:
        parts.append(f"vol ${vol/1000:.0f}k")
    if tpd >= HFT_MIN_TRADES_PER_DAY:
        parts.append(f"HFT {tpd:.0f}/d")
    return " · ".join(parts)


def select_hft_traders(top_n: int = DEFAULT_HFT_TOP_N) -> list[dict]:
    """Devuelve los wallets HFT/scalper que cumplen los filtros HFT_*.

    A diferencia de ``select_traders``, este NO toca ``copy_subscriptions`` —
    solo devuelve la lista de candidatos. ``select_traders`` los une a su
    pool y aplica la sincronización (added/kept/paused) global.

    Calcula ``trades_per_day = total_trades / days_active`` en Python
    (evita aritmética entre BIGINT con división — más portable PG/SQLite).
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT tm.* FROM trader_metrics tm
            WHERE tm.realized_pnl_usdc  >= ?
              AND tm.win_rate           >= ?
              AND tm.total_trades       >= ?
              AND tm.total_volume_usdc  >= ?
              AND tm.max_drawdown_pct   <= ?
              AND tm.first_trade_ts IS NOT NULL
              AND tm.last_trade_ts  IS NOT NULL
              AND tm.last_trade_ts > tm.first_trade_ts
              AND tm.wallet NOT IN (
                  SELECT wallet FROM copy_subscriptions WHERE status='dropped'
              )
            """,
            (
                HFT_MIN_PNL, HFT_MIN_WIN_RATE, HFT_MIN_TOTAL_TRADES,
                HFT_MIN_VOLUME, HFT_MAX_DRAWDOWN_PCT,
            ),
        ).fetchall()

    qualifying: list[dict] = []
    for r in rows:
        days_active = (r["last_trade_ts"] - r["first_trade_ts"]) / 86400.0
        if days_active < HFT_MIN_DAYS_ACTIVE:
            continue
        tpd = (r["total_trades"] or 0) / max(1.0, days_active)
        if tpd < HFT_MIN_TRADES_PER_DAY:
            continue

        # Feature C: filtros post-fetch para descartar HFT no copiables.
        sharpe = float(r["sharpe_proxy"] or 0.0)
        total_trades = int(r["total_trades"] or 0)
        pnl = float(r["realized_pnl_usdc"] or 0.0)
        vol = float(r["total_volume_usdc"] or 0.0)
        vol_pnl_ratio = vol / max(pnl, 1.0)

        # 1) Sharpe absurdo sobre muestra chica → ruido estadístico, no edge.
        if sharpe > HFT_MAX_SHARPE and total_trades < HFT_SHARPE_LARGE_SAMPLE:
            log.debug(
                "hft filter: %s rejected sharpe=%.1f vol/pnl=%.0f (sharpe>%.1f, n=%d<%d)",
                r["wallet"], sharpe, vol_pnl_ratio,
                HFT_MAX_SHARPE, total_trades, HFT_SHARPE_LARGE_SAMPLE,
            )
            continue

        # 2) Spread-scalper: vol/PnL >>100x → su edge es microspread que el
        # copy lag no puede capturar. Drop independiente del sharpe.
        if vol_pnl_ratio > HFT_MAX_VOL_PNL_RATIO:
            log.debug(
                "hft filter: %s rejected sharpe=%.1f vol/pnl=%.0f (>%.0fx scalper)",
                r["wallet"], sharpe, vol_pnl_ratio, HFT_MAX_VOL_PNL_RATIO,
            )
            continue

        d = dict(r)
        d["trades_per_day"] = tpd
        d["days_active"] = days_active
        qualifying.append(d)

    # Ordenamos por trades_per_day DESC (prioriza actividad). Empate
    # rompe por score DESC para preferir HFT con mejor track histórico.
    qualifying.sort(
        key=lambda x: ((x.get("trades_per_day") or 0), (x.get("score") or 0)),
        reverse=True,
    )
    return qualifying[:top_n]


def select_traders(
    top_n: int = DEFAULT_TOP_N,
    *,
    hft_top_n: int = DEFAULT_HFT_TOP_N,
    include_hft: bool = True,
) -> dict:
    """Sincroniza copy_subscriptions con el top actual.

    Combina dos buckets:
      1) Top by score (clásico) — prioriza rentabilidad y consistencia.
      2) Top by trades_per_day (HFT/scalper) — prioriza actividad continua.
    Los dos sets se unen y aplica la sincronización conjunta. Pueden
    overlappear; el wallet entra al pool una sola vez.

    Devuelve un resumen: { added, kept, paused, hft_added, total_active }.
    """
    init_db()
    summary: dict = {
        "added": [], "kept": [], "paused": [],
        "hft_added": [],
        "total_active": 0,
    }

    # Thresholds dinámicos (auto_filter puede haberlos modificado)
    from src.copybot.auto_filter import get_all as get_thresholds
    th = get_thresholds()

    with db() as conn:
        # Bucket clásico (top by score)
        candidates = conn.execute(
            """
            SELECT tm.* FROM trader_metrics tm
            WHERE tm.score              >= ?
              AND tm.realized_pnl_usdc  >= ?
              AND tm.win_rate           >= ?
              AND tm.total_trades       >= ?
              AND tm.total_volume_usdc  >= ?
              AND tm.max_drawdown_pct   <= ?
              AND tm.sharpe_proxy       >= ?
              AND tm.wallet NOT IN (
                  SELECT wallet FROM copy_subscriptions WHERE status='dropped'
              )
            ORDER BY tm.score DESC
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

    # Bucket HFT (top by trades_per_day) — agregamos al pool. El wallet
    # puede aparecer ya en el bucket clásico (overlap es OK, el dict no
    # duplica). Marcamos los nuevos HFT-only para auditoría.
    hft_only_wallets: set[str] = set()
    if include_hft:
        hft_candidates = select_hft_traders(top_n=hft_top_n)
        for hft in hft_candidates:
            w = hft["wallet"]
            if w not in cand_by_wallet:
                cand_wallets.add(w)
                cand_by_wallet[w] = hft
                hft_only_wallets.add(w)

    with tx() as conn:
        # 1) Activar / promover candidatos
        for w in cand_wallets:
            metric = cand_by_wallet[w]
            reason = _build_reason(metric)
            # HFT-only puede tener score chico/None — usar el que haya, default 0.
            score = metric.get("score") or 0.0
            if w not in existing:
                conn.execute(
                    """
                    INSERT INTO copy_subscriptions
                        (wallet, status, reason, score_at_start, sizing_mult)
                    VALUES (?, 'active', ?, ?, 1.0)
                    """,
                    (w, reason, score),
                )
                entry = {"wallet": w, "reason": reason, "score": score}
                summary["added"].append(entry)
                if w in hft_only_wallets:
                    summary["hft_added"].append(entry)
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
