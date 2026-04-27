"""Auto-discovery de nuevos traders (Fase 6a).

Workflow del ciclo (cada DISCOVER_EVERY_HOURS):
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

DISCOVER_EVERY_HOURS = 8
DISCOVER_PAGES = 30
BACKFILL_LIMIT_PER_RUN = 50  # tope de wallets nuevos a backfillear por ciclo


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
    if not force and (time.time() - _last_run_ts()) < DISCOVER_EVERY_HOURS * 3600:
        return {"skipped": True, "reason": "ya corrió hace poco"}

    from src.analytics.metrics import compute_for_wallet, UPSERT_METRICS, composite_score
    from src.copybot.clusters import recompute_clusters, update_cluster_perf
    from src.copybot.selector import select_traders
    from src.indexer.trades import backfill_wallet, discover_traders

    out: dict = {"discovered": 0, "backfilled": 0, "computed": 0, "select": None}

    # 1) Discover
    try:
        n = await discover_traders(max_pages=DISCOVER_PAGES, page_size=500)
        out["discovered"] = n
    except Exception as e:
        log.exception("discover failed: %s", e)

    # 2) Backfill de los wallets nuevos
    pending = _wallets_without_backfill(BACKFILL_LIMIT_PER_RUN)
    for w in pending:
        try:
            await backfill_wallet(w)
            out["backfilled"] += 1
        except Exception as e:
            log.warning("backfill %s falló: %s", w[:10], e)

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
        out["select"] = select_traders(top_n=20)
    except Exception as e:
        log.exception("select failed: %s", e)

    _set_last_run()
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(run_cycle(force=True)))
