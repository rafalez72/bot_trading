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


async def index_markets(*, only_active: bool = False) -> int:
    """Indexa mercados.

    La Gamma API por default devuelve sólo `closed=false`, así que hacemos
    dos pasadas: activos + cerrados. `only_active=True` salta la segunda.
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

    console.print(f"[green]✓[/green] {total} mercados indexados (activos + cerrados)")
    return total


if __name__ == "__main__":
    asyncio.run(index_markets())
