"""TopVolume sweep — descubre wallets nuevos rankeados por volumen 24h.

Motivación (2026-05-09): el feed global de Polymarket tiene ~6k trades/h y
~4.4k wallets únicos/h activos. La discovery histórica (`discovery.py`) sólo
ve wallets que ya cayeron en `trades` por backfill o por el discover de N
páginas; los wallets de mayor volumen rara vez aparecen al tope del feed
porque el feed va por timestamp, no por size. Resultado: nuestro universo
de 4052 wallets crece lento.

Este módulo arregla eso pulleando el feed global con paginación profunda,
recortando a 24h, agregando `size_usdc = price*size` por `proxyWallet`,
y disparando backfill para los top-N por volumen que no tengamos ya en
`trader_metrics`.

Diseño defensivo:
- Throttle ~5 req/s al data-api (rate-limit implícito).
- Stop a las 24h o tras ~10k trades (lo que ocurra primero).
- Skip wallets ya en `trader_metrics` (asumimos backfill ya hecho).
- Concurrency=5 para los backfill (mismo patrón que `discovery.py`).
- Time budget total: 30 min wall-clock.
- Idempotencia vía `bot_state['topvolume_last_run']`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Iterable

import httpx

from src.config import (
    DISCOVERY_TOPVOLUME_ENABLED,
    DISCOVERY_TOPVOLUME_INTERVAL_HOURS,
    DISCOVERY_TOPVOLUME_LIMIT,
)
from src.db.schema import db, tx
from src.polymarket.client import PolymarketClient

log = logging.getLogger(__name__)


# Constantes operacionales (no env-driven, son tuning interno).
PAGE_SIZE = 500
MAX_TRADES_FETCH = 10_000           # cap absoluto de páginas a barrer
WINDOW_SECONDS = 24 * 3600          # 24h
THROTTLE_SECONDS = 0.2              # ~5 req/s
BACKFILL_CONCURRENCY = 5
TIME_BUDGET_SECONDS = 30 * 60       # 30 min wall-clock
PROGRESS_LOG_EVERY = 1000           # log cada N trades fetched
STATE_KEY = "topvolume_last_run"


# ---------- bot_state helpers ----------

def _last_run_ts() -> int:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key=?", (STATE_KEY,)
        ).fetchone()
    return int(r["value"]) if r else 0


def _set_last_run() -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (STATE_KEY, str(int(time.time()))),
        )


def _wallets_already_have_metrics(wallets: Iterable[str]) -> set[str]:
    """Devuelve el subset de `wallets` que ya tiene fila en `trader_metrics`.

    Skip rationale: si ya está en trader_metrics es porque corrimos backfill
    para ese wallet en algún momento (ver `compute_for_wallet` en analytics/
    metrics.py). Refrescar staleness es harina de otro costal.
    """
    wallets_list = [w for w in wallets if w]
    if not wallets_list:
        return set()
    # Chunk para evitar SQL parameter limit (SQLite ~999, PG ~32k).
    chunk = 500
    seen: set[str] = set()
    with db() as conn:
        for i in range(0, len(wallets_list), chunk):
            batch = wallets_list[i : i + chunk]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT wallet FROM trader_metrics WHERE wallet IN ({placeholders})",
                batch,
            ).fetchall()
            seen.update(r["wallet"] for r in rows)
    return seen


# ---------- aggregation ----------

def _trade_volume(t: dict) -> tuple[str | None, float, int]:
    """Extrae (wallet, size_usdc, timestamp) del trade. Tolerante a campos faltantes."""
    wallet = (t.get("proxyWallet") or "").lower() or None
    try:
        price = float(t.get("price") or 0)
        size = float(t.get("size") or 0)
    except (TypeError, ValueError):
        price, size = 0.0, 0.0
    try:
        ts = int(t.get("timestamp") or 0)
    except (TypeError, ValueError):
        ts = 0
    return wallet, price * size, ts


async def _fetch_24h_trades(
    client: PolymarketClient,
    *,
    now_ts: int,
    max_trades: int = MAX_TRADES_FETCH,
    page_size: int = PAGE_SIZE,
    throttle: float = THROTTLE_SECONDS,
) -> list[dict]:
    """Pagina /trades?limit&offset hasta 24h o cap de trades.

    El endpoint devuelve timestamps DESC. Cortamos cuando vemos un trade con
    `timestamp < now - 86400` (todas las páginas siguientes serán más viejas).
    """
    cutoff = now_ts - WINDOW_SECONDS
    out: list[dict] = []
    offset = 0
    pages = 0
    while len(out) < max_trades:
        try:
            page = await client.trades(limit=page_size, offset=offset)
        except httpx.HTTPStatusError as e:
            # 400 es típico cuando offset supera el límite duro del backend.
            if e.response.status_code == 400:
                log.warning("topvolume: offset=%d → 400, fin de stream", offset)
                break
            raise
        if not page:
            break
        pages += 1
        # Filtramos in-place a la ventana de 24h. Si el último trade de la
        # página ya es más viejo que cutoff, no pedimos más páginas.
        last_ts = 0
        for t in page:
            try:
                ts = int(t.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts and ts < cutoff:
                last_ts = ts
                continue
            out.append(t)
            last_ts = ts or last_ts
        # Progress log
        if pages % max(1, (PROGRESS_LOG_EVERY // page_size)) == 0:
            log.info(
                "topvolume: fetched %d trades (offset=%d, last_ts_age=%ss)",
                len(out),
                offset,
                (now_ts - last_ts) if last_ts else "?",
            )
        # ¿Llegamos al borde de la ventana?
        if last_ts and last_ts < cutoff:
            break
        if len(page) < page_size:
            break
        offset += page_size
        if throttle > 0:
            await asyncio.sleep(throttle)
    log.info("topvolume: total %d trades en ventana 24h (pages=%d)", len(out), pages)
    return out


def _aggregate_by_wallet(trades: list[dict], *, now_ts: int) -> dict[str, float]:
    """Suma size_usdc por wallet, sólo considerando trades dentro de 24h."""
    cutoff = now_ts - WINDOW_SECONDS
    agg: dict[str, float] = defaultdict(float)
    for t in trades:
        wallet, usdc, ts = _trade_volume(t)
        if not wallet:
            continue
        if ts and ts < cutoff:
            continue
        if usdc <= 0:
            continue
        agg[wallet] += usdc
    return dict(agg)


def _top_n_by_volume(agg: dict[str, float], n: int) -> list[tuple[str, float]]:
    return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[: max(0, n)]


# ---------- entry point ----------

async def discover_top_volume_wallets(
    limit: int = DISCOVERY_TOPVOLUME_LIMIT,
    *,
    force: bool = False,
    time_budget_s: int = TIME_BUDGET_SECONDS,
    backfill_concurrency: int = BACKFILL_CONCURRENCY,
) -> dict:
    """Descubre top-N wallets por volumen 24h y backfillea los que falten.

    Returns dict con métricas: {fetched, candidates, skipped_known, backfilled,
    backfill_errors, computed, elapsed_s, skipped (bool)}.
    """
    started = time.time()
    out: dict = {
        "fetched": 0,
        "candidates": 0,
        "skipped_known": 0,
        "backfilled": 0,
        "backfill_errors": 0,
        "computed": 0,
        "elapsed_s": 0.0,
        "skipped": False,
    }

    if not DISCOVERY_TOPVOLUME_ENABLED and not force:
        out["skipped"] = True
        out["reason"] = "disabled"
        return out

    interval_s = DISCOVERY_TOPVOLUME_INTERVAL_HOURS * 3600
    if not force and (time.time() - _last_run_ts()) < interval_s:
        out["skipped"] = True
        out["reason"] = "ya corrió hace poco"
        return out

    # Marcar el run ANTES del trabajo pesado para evitar bucles de muerte si
    # nos matan por watchdog mid-fetch (mismo patrón que discovery.run_cycle).
    _set_last_run()

    now_ts = int(time.time())
    log.info(
        "topvolume: arrancando sweep, target top-%d wallets, budget=%ds",
        limit, time_budget_s,
    )

    # 1) Pull trades.
    try:
        async with PolymarketClient() as client:
            trades = await _fetch_24h_trades(client, now_ts=now_ts)
    except Exception as e:
        log.exception("topvolume: fetch falló: %s", e)
        out["elapsed_s"] = time.time() - started
        return out
    out["fetched"] = len(trades)

    if (time.time() - started) > time_budget_s:
        log.warning("topvolume: time budget excedido tras fetch, cortando")
        out["elapsed_s"] = time.time() - started
        return out

    # 2) Aggregate + rank.
    agg = _aggregate_by_wallet(trades, now_ts=now_ts)
    top = _top_n_by_volume(agg, limit)
    out["candidates"] = len(top)
    log.info(
        "topvolume: %d wallets únicos en 24h, top-%d total volume = $%.0f",
        len(agg), len(top), sum(v for _, v in top),
    )

    # 3) Filter: ya tenemos métricas → skip.
    candidates = [w for w, _ in top]
    known = _wallets_already_have_metrics(candidates)
    new_wallets = [w for w in candidates if w not in known]
    out["skipped_known"] = len(candidates) - len(new_wallets)
    log.info(
        "topvolume: %d ya en trader_metrics, %d nuevos para backfillear",
        out["skipped_known"], len(new_wallets),
    )

    if not new_wallets:
        out["elapsed_s"] = time.time() - started
        return out

    # 4) Backfill paralelo + compute_for_wallet.
    # Imports aquí para evitar import circular y para que el módulo se pueda
    # importar sin tirar deps pesadas si nadie llama a la función.
    from src.analytics.metrics import (
        UPSERT_METRICS,
        composite_score,
        compute_for_wallet,
    )
    from src.indexer.trades import backfill_wallet

    sem = asyncio.Semaphore(backfill_concurrency)
    cancel_event = asyncio.Event()

    async def _bf(w: str) -> bool:
        if cancel_event.is_set():
            return False
        async with sem:
            if cancel_event.is_set():
                return False
            try:
                await backfill_wallet(w)
                return True
            except Exception as e:
                log.warning("topvolume: backfill %s falló: %s", w[:10], e)
                return False

    # Watchdog: si excedemos time budget, cancelamos los pendientes.
    async def _watchdog() -> None:
        remaining = max(1, time_budget_s - int(time.time() - started))
        await asyncio.sleep(remaining)
        if not cancel_event.is_set():
            log.warning("topvolume: time budget excedido, cancelando backfills pendientes")
            cancel_event.set()

    wd_task = asyncio.create_task(_watchdog())
    try:
        results = await asyncio.gather(
            *(_bf(w) for w in new_wallets), return_exceptions=False
        )
    finally:
        wd_task.cancel()
        try:
            await wd_task
        except (asyncio.CancelledError, Exception):
            pass

    out["backfilled"] = sum(1 for ok in results if ok)
    out["backfill_errors"] = sum(1 for ok in results if not ok)

    # 5) Compute metrics para los recién backfilleados (sólo los OK).
    for w, ok in zip(new_wallets, results):
        if not ok:
            continue
        try:
            m = compute_for_wallet(w)
            if m is None:
                continue
            s = composite_score(m)
            with tx() as conn:
                conn.execute(UPSERT_METRICS, m.as_row(s))
            out["computed"] += 1
        except Exception as e:
            log.exception("topvolume: compute %s failed: %s", w[:10], e)

    out["elapsed_s"] = time.time() - started
    log.info(
        "topvolume: done in %.1fs — fetched=%d candidates=%d new=%d "
        "backfilled=%d errors=%d computed=%d",
        out["elapsed_s"],
        out["fetched"],
        out["candidates"],
        len(new_wallets),
        out["backfilled"],
        out["backfill_errors"],
        out["computed"],
    )
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(discover_top_volume_wallets(force=True)))
