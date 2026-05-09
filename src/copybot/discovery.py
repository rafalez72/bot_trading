"""Auto-discovery de nuevos traders (Fase 6a).

Workflow del ciclo (cada DISCOVER_EVERY_MINUTES):
1. discover_traders → barre el feed global y descubre wallets nuevos.
2. Para wallets que aún NO están en `traders` con backfill:
   - backfill_wallet (descarga su historial — cap 3500)
3. compute_for_wallet de los nuevos.
4. recompute_clusters (con la nueva data se redibujan los clusters).
5. select_traders → re-evalúa el top con los criterios actuales.

El último timestamp de ejecución se persiste en `bot_state` para idempotencia.
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.db.schema import db, tx

log = logging.getLogger(__name__)

# 2026-05-08: bajamos de 8h → 25min para capturar wallets calientes más
# rápido. Cada ciclo cuesta ~80-230 reqs HTTP + 10-30s de DB-lock; con 25min
# eso es ~1-2% del tiempo lockeado y ~250 req/min sostenido (margen
# saludable vs el rate-limit implícito del data-api). Si bajás más allá,
# revisá clusters.py — recompute_clusters es la transacción gigante.
DISCOVER_EVERY_MINUTES = 25
DISCOVER_PAGES = 30
BACKFILL_LIMIT_PER_RUN = 10  # bajado de 50 — caso 2026-05-07: 50 wallets serial saturaba el watchdog
BACKFILL_CONCURRENCY = 5     # gather() en grupos de 5 para no saturar Data API


def _last_run_ts() -> int:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key='discovery_last_run'"
        ).fetchone()
    return int(r["value"]) if r else 0


def _set_last_run() -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES ('discovery_last_run', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (str(int(time.time())),),
        )


def _wallets_without_backfill(limit: int) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT wallet FROM traders
            WHERE last_indexed_at IS NULL
            ORDER BY first_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [r["wallet"] for r in rows]


async def run_cycle(*, force: bool = False) -> dict:
    """Ejecuta un ciclo completo de auto-discovery."""
    if not force and (time.time() - _last_run_ts()) < DISCOVER_EVERY_MINUTES * 60:
        return {"skipped": True, "reason": "ya corrió hace poco"}

    # Marcar el run como iniciado ANTES de empezar el trabajo pesado. Si el
    # proceso muere a mitad (caso 2026-05-07: watchdog kill durante backfill),
    # el siguiente arranque ve "ya corrió hace poco" y hace skip — rompiendo el
    # bucle de muerte donde cada restart re-disparaba discovery completo.
    _set_last_run()

    from src.analytics.metrics import compute_for_wallet, UPSERT_METRICS, composite_score
    from src.copybot.clusters import recompute_clusters, update_cluster_perf
    from src.copybot.selector import select_traders
    from src.indexer.trades import backfill_wallet, discover_traders

    out: dict = {"discovered": 0, "backfilled": 0, "computed": 0, "select": None}

    # 0) TopVolume sweep (1×/día). Tiene su propio guard interno por
    # `bot_state['topvolume_last_run']`, así que pasarlo cada ciclo es no-op
    # cuando no toca; sólo corre cuando ya pasaron DISCOVERY_TOPVOLUME_INTERVAL_HOURS.
    # Lo metemos antes del discover global porque alimenta wallets nuevos
    # ranked por volumen (los que más nos interesan), aumentando la chance
    # de que el resto del ciclo encuentre data fresca.
    try:
        from src.copybot.discovery_topvolume import discover_top_volume_wallets
        out["topvolume"] = await discover_top_volume_wallets()
    except Exception as e:
        log.exception("topvolume sweep failed: %s", e)
        out["topvolume"] = {"error": str(e)}

    # 1) Discover
    try:
        n = await discover_traders(max_pages=DISCOVER_PAGES, page_size=500)
        out["discovered"] = n
    except Exception as e:
        log.exception("discover failed: %s", e)

    # 2) Backfill paralelo de los wallets nuevos. Semaphore limita concurrencia
    # para no saturar la Data API (default 5 conexiones simultáneas). Antes era
    # loop secuencial que tardaba 5-15min y disparaba el watchdog del runner.
    pending = _wallets_without_backfill(BACKFILL_LIMIT_PER_RUN)
    if pending:
        sem = asyncio.Semaphore(BACKFILL_CONCURRENCY)

        async def _bf(w: str) -> bool:
            async with sem:
                try:
                    await backfill_wallet(w)
                    return True
                except Exception as e:
                    log.warning("backfill %s falló: %s", w[:10], e)
                    return False

        results = await asyncio.gather(*(_bf(w) for w in pending))
        out["backfilled"] = sum(1 for ok in results if ok)

    # 3) Compute metrics para los recién backfilleados
    for w in pending:
        try:
            m = compute_for_wallet(w)
            if m is None:
                continue
            s = composite_score(m)
            with tx() as conn:
                conn.execute(UPSERT_METRICS, m.as_row(s))
            out["computed"] += 1
        except Exception as e:
            log.exception("compute %s failed: %s", w[:10], e)

    # 4) Re-clustering
    try:
        cl = recompute_clusters()
        out["clusters"] = cl
        update_cluster_perf()
    except Exception as e:
        log.exception("clustering failed: %s", e)

    # 5) Re-select top
    try:
        import os
        from src.copybot.selector import DEFAULT_TOP_N
        top_n = int(os.getenv("DISCOVERY_TOP_N", str(DEFAULT_TOP_N)))
        out["select"] = select_traders(top_n=top_n)
    except Exception as e:
        log.exception("select failed: %s", e)

    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(run_cycle(force=True)))
