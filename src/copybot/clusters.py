"""Wallet clustering (Fase 6b).

Agrupa wallets por similaridad de comportamiento para que el bot pueda:
1. Aplicar penalizaciones a nivel cluster (si todo un cluster pierde, baja
   el sizing de TODOS sus miembros — incluidos los que aún no perdieron).
2. Heredar priors a wallets nuevos (cluster_id = K → empieza con sizing
   ajustado por el cluster, no con default 1.0).

Features usadas (vector por wallet desde `trader_metrics` + agregados de trades):
  1. log10(total_volume_usdc)         — escala de operatoria
  2. avg_position_size                — tamaño típico del trade
  3. win_rate                         — calidad
  4. sharpe_proxy                     — consistencia
  5. max_drawdown_pct                 — riesgo
  6. trade_frequency_per_day          — actividad
  7. avg_entry_price                  — perfil de entry
  8. price_std                        — variabilidad de precios entry
  9. mean_hour_utc                    — horario típico (sin/cos para circular)
  10. category_diversity              — cuántas categorías opera

K-means con K=6 por default (suficiente con 522 wallets).
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Iterable

import numpy as np
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

from src.db.schema import db, tx

log = logging.getLogger(__name__)

K_CLUSTERS = 6
MIN_TRADES_FOR_CLUSTERING = 50          # wallets con menos no entran al kmeans
PENALIZE_WIN_RATE = 0.40
BLOCK_WIN_RATE = 0.30
MIN_CLUSTER_TRADES = 12                 # antes de evaluar performance del cluster

# Override de seguridad: si CLUSTER_BLOCK_DISABLED=true, todos los clusters
# quedan 'allowed'. Útil durante transición a live cuando el cluster_perf
# basado en paper_trades quedó self-fulfilling-prophecy y bloquea whales.
CLUSTER_BLOCK_DISABLED = os.getenv("CLUSTER_BLOCK_DISABLED", "false").lower() in ("true", "1", "yes")


def _wallet_features() -> tuple[list[str], np.ndarray]:
    """Construye matriz de features [N x F] desde la base."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                tm.wallet,
                tm.total_volume_usdc, tm.avg_position_size,
                tm.win_rate, tm.sharpe_proxy, tm.max_drawdown_pct,
                tm.total_trades, tm.first_trade_ts, tm.last_trade_ts
            FROM trader_metrics tm
            WHERE tm.total_trades >= ?
            """,
            (MIN_TRADES_FOR_CLUSTERING,),
        ).fetchall()

    wallets: list[str] = []
    feats: list[list[float]] = []

    for r in rows:
        wallet = r["wallet"]
        # Agregar features desde la tabla `trades`
        with db() as conn:
            agg = conn.execute(
                """
                SELECT
                    COALESCE(AVG(price), 0)         as avg_p,
                    COALESCE(
                        (AVG(price * price) - AVG(price) * AVG(price)),
                        0
                    )                                 as var_p,
                    COUNT(DISTINCT condition_id)     as n_markets
                FROM trades
                WHERE wallet=?
                """,
                (wallet,),
            ).fetchone()
            cat_count = conn.execute(
                """
                SELECT COUNT(DISTINCT m.category) as n
                FROM trades t
                LEFT JOIN markets m ON m.condition_id = t.condition_id
                WHERE t.wallet=? AND m.category IS NOT NULL
                """,
                (wallet,),
            ).fetchone()
            hour_stats = conn.execute(
                """
                SELECT
                    AVG(CAST(strftime('%H', timestamp, 'unixepoch') AS REAL)) as mean_h
                FROM trades WHERE wallet=?
                """,
                (wallet,),
            ).fetchone()

        active_days = max(
            1, ((r["last_trade_ts"] or 0) - (r["first_trade_ts"] or 0)) // 86400
        )
        freq = r["total_trades"] / active_days

        avg_p = agg["avg_p"] or 0.5
        var_p = max(0.0, agg["var_p"] or 0)
        std_p = math.sqrt(var_p)
        mean_h = hour_stats["mean_h"] or 12.0
        # Embeddings circulares para hora
        hour_sin = math.sin(2 * math.pi * mean_h / 24)
        hour_cos = math.cos(2 * math.pi * mean_h / 24)

        feats.append([
            math.log10(max(r["total_volume_usdc"] or 1, 1)),
            r["avg_position_size"] or 0,
            r["win_rate"] or 0,
            r["sharpe_proxy"] or 0,
            r["max_drawdown_pct"] or 0,
            freq,
            avg_p,
            std_p,
            hour_sin,
            hour_cos,
            cat_count["n"] or 0,
        ])
        wallets.append(wallet)

    if not feats:
        return [], np.empty((0, 0))
    return wallets, np.asarray(feats, dtype=np.float64)


def recompute_clusters(*, k: int = K_CLUSTERS) -> dict:
    """Re-corre el K-means y persiste asignaciones."""
    wallets, X = _wallet_features()
    if len(wallets) < k:
        return {"clustered": 0, "reason": f"insuficientes wallets ({len(wallets)}<{k})"}

    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    km = KMeans(n_clusters=k, n_init=10, random_state=42)
    labels = km.fit_predict(Xs)

    feature_names = [
        "log_volume", "avg_pos", "win_rate", "sharpe", "max_dd_pct",
        "freq_per_day", "avg_entry", "price_std", "hour_sin", "hour_cos",
        "n_categories",
    ]

    with tx() as conn:
        conn.execute("DELETE FROM wallet_clusters")
        rows = []
        for w, lbl, feat in zip(wallets, labels, X):
            rows.append((
                w,
                int(lbl),
                json.dumps({k_: round(v, 4) for k_, v in zip(feature_names, feat.tolist())}),
            ))
        conn.executemany(
            "INSERT INTO wallet_clusters (wallet, cluster_id, features) VALUES (?, ?, ?)",
            rows,
        )

    # Centroides para introspección
    centroids = scaler.inverse_transform(km.cluster_centers_).tolist()
    return {
        "clustered": len(wallets),
        "k": k,
        "centroids": [
            {name: round(v, 3) for name, v in zip(feature_names, c)}
            for c in centroids
        ],
        "size_per_cluster": [int(np.sum(labels == i)) for i in range(k)],
    }


def update_cluster_perf() -> dict:
    """Recalcula la performance por cluster a partir de paper_trades."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                wc.cluster_id,
                COUNT(DISTINCT wc.wallet)      as n_wallets,
                COUNT(pt.id)                    as n_trades,
                SUM(CASE WHEN pt.status LIKE '%_win'  THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pt.status LIKE '%_loss' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pt.pnl_usdc), 0)   as pnl
            FROM wallet_clusters wc
            LEFT JOIN paper_trades pt
              ON pt.source_wallet = wc.wallet
              AND pt.status IN ('closed_win','closed_loss','settled_win','settled_loss')
            GROUP BY wc.cluster_id
            """,
        ).fetchall()

    out: list[dict] = []
    with tx() as conn:
        for r in rows:
            n = r["n_trades"] or 0
            wr = (r["wins"] / n) if n else 0.0
            pnl = r["pnl"] or 0
            if CLUSTER_BLOCK_DISABLED:
                status = "allowed"
            elif n >= MIN_CLUSTER_TRADES:
                if wr < BLOCK_WIN_RATE and pnl < 0:
                    status = "blocked"
                elif wr < PENALIZE_WIN_RATE and pnl < 0:
                    status = "penalized"
                else:
                    status = "allowed"
            else:
                status = "allowed"
            conn.execute(
                """
                INSERT INTO cluster_perf
                    (cluster_id, n_wallets, n_trades, wins, losses, pnl_usdc, avg_win_rate, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(cluster_id) DO UPDATE SET
                    n_wallets    = excluded.n_wallets,
                    n_trades     = excluded.n_trades,
                    wins         = excluded.wins,
                    losses       = excluded.losses,
                    pnl_usdc     = excluded.pnl_usdc,
                    avg_win_rate = excluded.avg_win_rate,
                    status       = excluded.status,
                    updated_at   = datetime('now')
                """,
                (
                    r["cluster_id"], r["n_wallets"], n,
                    r["wins"] or 0, r["losses"] or 0, pnl, wr, status,
                ),
            )
            out.append({
                "cluster_id": r["cluster_id"],
                "n_wallets": r["n_wallets"],
                "n_trades": n,
                "wins": r["wins"] or 0,
                "losses": r["losses"] or 0,
                "pnl": pnl,
                "win_rate": wr,
                "status": status,
            })
    return {"clusters": out}


def cluster_for(wallet: str) -> int | None:
    with db() as conn:
        r = conn.execute(
            "SELECT cluster_id FROM wallet_clusters WHERE wallet=?", (wallet,)
        ).fetchone()
    return r["cluster_id"] if r else None


def cluster_status_for_wallet(wallet: str) -> dict | None:
    """Devuelve {cluster_id, status} para usar en filtros."""
    with db() as conn:
        r = conn.execute(
            """
            SELECT wc.cluster_id, COALESCE(cp.status, 'allowed') as status,
                   COALESCE(cp.avg_win_rate, 0) as wr,
                   COALESCE(cp.pnl_usdc, 0) as pnl,
                   COALESCE(cp.n_trades, 0) as n
            FROM wallet_clusters wc
            LEFT JOIN cluster_perf cp ON cp.cluster_id = wc.cluster_id
            WHERE wc.wallet=?
            """,
            (wallet,),
        ).fetchone()
    return dict(r) if r else None


def status() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM cluster_perf ORDER BY cluster_id"
        ).fetchall()
    return [dict(r) for r in rows]
