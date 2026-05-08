"""Indexer de mercados de Polymarket.

Descarga toda la metadata de mercados (activos + cerrados) y la persiste
en la tabla `markets`. Idempotente: re-correrlo refresca volume/liquidity.
"""
from __future__ import annotations

import asyncio
import json
import logging

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from src.db.schema import init_db, tx
from src.polymarket.client import PolymarketClient

log = logging.getLogger(__name__)
console = Console()

UPSERT_MARKET = """
INSERT INTO markets (
    condition_id, question, slug, category, end_date,
    active, closed, volume, liquidity, outcomes, outcome_prices, last_seen_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
ON CONFLICT(condition_id) DO UPDATE SET
    question        = excluded.question,
    slug            = excluded.slug,
    category        = excluded.category,
    end_date        = excluded.end_date,
    active          = excluded.active,
    closed          = excluded.closed,
    volume          = excluded.volume,
    liquidity       = excluded.liquidity,
    outcomes        = excluded.outcomes,
    outcome_prices  = excluded.outcome_prices,
    last_seen_at    = datetime('now');
"""


def _parse_json_array(v) -> list | None:
    if v is None:
        return None
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return [v]
    return None


def _to_row(m: dict) -> tuple | None:
    cid = m.get("conditionId")
    if not cid:
        return None
    outcomes = _parse_json_array(m.get("outcomes"))
    prices = _parse_json_array(m.get("outcomePrices"))
    return (
        cid,
        m.get("question"),
        m.get("slug"),
        m.get("category") or m.get("groupItemTitle"),
        m.get("endDate"),
        1 if m.get("active") else 0,
        1 if m.get("closed") else 0,
        float(m.get("volume") or 0),
        float(m.get("liquidity") or 0),
        json.dumps(outcomes) if outcomes is not None else None,
        json.dumps(prices) if prices is not None else None,
    )


async def index_markets(*, only_active: bool = False, quiet: bool = False) -> int:
    """Indexa mercados.

    La Gamma API por default devuelve sólo `closed=false`, así que hacemos
    dos pasadas: activos + cerrados. `only_active=True` salta la segunda
    (más rápido, suficiente para el loop automático del runner).
    `quiet=True` evita la barra de progreso de rich (logs limpios cuando
    se llama desde el runner en background).
    """
    init_db()
    total = 0
    batch: list[tuple] = []
    BATCH = 200

    async def _flush() -> None:
        nonlocal total, batch
        if not batch:
            return
        with tx() as conn:
            conn.executemany(UPSERT_MARKET, batch)
        total += len(batch)
        batch = []

    passes: list[tuple[str, bool | None]] = [("activos", False)]
    if not only_active:
        passes.append(("cerrados", True))

    async with PolymarketClient() as client:
        if quiet:
            for label, closed_flag in passes:
                async for m in client.iter_markets(page_size=500, closed=closed_flag):
                    row = _to_row(m)
                    if not row:
                        continue
                    batch.append(row)
                    if len(batch) >= BATCH:
                        await _flush()
                await _flush()
                log.info("markets_refresh: %s pass done — %d totales", label, total)
        else:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                TimeElapsedColumn(),
                console=console,
            ) as prog:
                for label, closed_flag in passes:
                    t = prog.add_task(f"Indexando mercados {label}…", total=None)
                    async for m in client.iter_markets(page_size=500, closed=closed_flag):
                        row = _to_row(m)
                        if not row:
                            continue
                        batch.append(row)
                        if len(batch) >= BATCH:
                            await _flush()
                            prog.update(
                                t,
                                description=f"Indexando {label}… {total} guardados",
                            )
                    await _flush()
                    prog.update(t, description=f"✓ {label}: {total} guardados")
            console.print(
                f"[green]✓[/green] {total} mercados indexados (activos + cerrados)"
            )
    return total


# --------------------------------------------------------------------------- #
# Loop automático invocable desde el runner.
# --------------------------------------------------------------------------- #

# Cada cuánto refrescamos mercados activos en background. Sólo activos:
# los cerrados no cambian liquidity/volume relevantes y son ~300k filas
# (caro re-indexarlos seguido). Si necesitás cerrados frescos, corré
# `python copybot.py markets` manual.
MARKETS_REFRESH_HOURS = 4


def _last_run_ts() -> int:
    from src.db.schema import db
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key='markets_last_run'"
        ).fetchone()
    return int(r["value"]) if r else 0


def _set_last_run() -> None:
    import time as _t
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES ('markets_last_run', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (str(int(_t.time())),),
        )


async def maybe_refresh(*, force: bool = False) -> dict:
    """Refresca mercados activos si pasaron MARKETS_REFRESH_HOURS.

    Devuelve {"skipped": True} o {"refreshed": N}. Idempotente.
    Pensado para llamarse desde el runner main loop, similar al
    discovery loop. NO levanta excepciones — el caller wrapeará con
    asyncio.wait_for para evitar que un cuelgue de la Gamma API
    bloquee el runner.
    """
    import time as _t
    if not force and (_t.time() - _last_run_ts()) < MARKETS_REFRESH_HOURS * 3600:
        return {"skipped": True}

    # Marcar el run ANTES de empezar — si el indexer cuelga y el watchdog
    # mata el proceso, el siguiente arranque ve "ya corrió hace poco" y
    # no reintenta inmediatamente (mismo patrón que discovery.py).
    _set_last_run()

    log.info("markets_refresh: arrancando refresh de mercados activos…")
    n = await index_markets(only_active=True, quiet=True)
    log.info("markets_refresh: %d mercados activos refrescados", n)
    return {"refreshed": n}


if __name__ == "__main__":
    asyncio.run(index_markets())
