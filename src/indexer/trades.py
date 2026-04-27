"""Indexer de trades.

Estrategia en dos pasos:

1. **Discovery**: barre el feed global de trades (más reciente → atrás)
   y descubre wallets que están operando. Cada wallet nuevo va a `traders`.

2. **Backfill por wallet**: para cada wallet flagged (o todos), pagina
   TODO su historial vía /trades?user=<wallet> y lo guarda.

Idempotente vía PRIMARY KEY sintética: transactionHash + asset + side.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Iterable

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from src.db.schema import init_db, tx
from src.polymarket.client import PolymarketClient

log = logging.getLogger(__name__)
console = Console()


UPSERT_TRADER = """
INSERT INTO traders (wallet, first_seen_at, last_indexed_at, total_trades)
VALUES (?, datetime('now'), NULL, 0)
ON CONFLICT(wallet) DO NOTHING;
"""

UPSERT_TRADE = """
INSERT INTO trades (
    id, wallet, condition_id, side, outcome, outcome_index,
    price, size, usdc_value, timestamp, raw
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO NOTHING;
"""

UPDATE_TRADER_AFTER_INDEX = """
UPDATE traders
SET last_indexed_at = datetime('now'),
    total_trades = (SELECT COUNT(*) FROM trades WHERE wallet = traders.wallet)
WHERE wallet = ?;
"""


def _trade_id(t: dict) -> str | None:
    """ID estable. Un mismo tx puede tener varios fills → incluimos asset/side."""
    txh = t.get("transactionHash")
    if not txh:
        return None
    return f"{txh}:{t.get('asset','')}:{t.get('side','')}:{t.get('outcomeIndex','')}"


def _trade_row(t: dict) -> tuple | None:
    tid = _trade_id(t)
    wallet = (t.get("proxyWallet") or "").lower()
    cid = t.get("conditionId")
    if not (tid and wallet and cid):
        return None
    try:
        price = float(t.get("price") or 0)
        size = float(t.get("size") or 0)
    except (TypeError, ValueError):
        return None
    usdc = price * size
    return (
        tid,
        wallet,
        cid,
        (t.get("side") or "").upper(),
        t.get("outcome"),
        t.get("outcomeIndex"),
        price,
        size,
        usdc,
        int(t.get("timestamp") or 0),
        json.dumps(t, separators=(",", ":")),
    )


def _persist(rows: Iterable[tuple]) -> tuple[int, set[str]]:
    rows = [r for r in rows if r is not None]
    if not rows:
        return 0, set()
    wallets = {r[1] for r in rows}
    with tx() as conn:
        conn.executemany(UPSERT_TRADER, [(w,) for w in wallets])
        conn.executemany(UPSERT_TRADE, rows)
    return len(rows), wallets


# ---------- Discovery: feed global ----------
async def discover_traders(*, max_pages: int = 50, page_size: int = 500) -> int:
    """Barre los trades globales más recientes para descubrir wallets.

    Útil para sembrar la lista inicial. No garantiza historial completo;
    para eso se usa `backfill_wallet`.
    """
    init_db()
    discovered: set[str] = set()
    total = 0

    async with PolymarketClient() as client:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as prog:
            t = prog.add_task("Descubriendo wallets…", total=None)
            offset = 0
            for _ in range(max_pages):
                try:
                    page = await client.trades(limit=page_size, offset=offset)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 400:
                        log.warning("discover: offset=%d → 400, fin", offset)
                        break
                    raise
                if not page:
                    break
                rows = [_trade_row(x) for x in page]
                n, wallets = _persist(rows)
                total += n
                discovered |= wallets
                prog.update(
                    t,
                    description=(
                        f"Descubriendo… {len(discovered)} wallets, "
                        f"{total} trades"
                    ),
                )
                if len(page) < page_size:
                    break
                offset += page_size

    console.print(
        f"[green]✓[/green] {len(discovered)} wallets descubiertos, "
        f"{total} trades guardados"
    )
    return len(discovered)


# ---------- Backfill: historial completo de un wallet ----------
async def backfill_wallet(wallet: str) -> int:
    init_db()
    wallet = wallet.lower()
    saved = 0

    async with PolymarketClient() as client:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as prog:
            t = prog.add_task(f"Backfill {wallet[:10]}…", total=None)
            buffer: list[tuple] = []
            async for trade in client.iter_user_trades(wallet, page_size=500):
                row = _trade_row(trade)
                if row:
                    buffer.append(row)
                if len(buffer) >= 500:
                    n, _ = _persist(buffer)
                    saved += n
                    buffer.clear()
                    prog.update(t, description=f"Backfill {wallet[:10]}… {saved} trades")
            if buffer:
                n, _ = _persist(buffer)
                saved += n

    with tx() as conn:
        conn.execute(UPDATE_TRADER_AFTER_INDEX, (wallet,))

    console.print(f"[green]✓[/green] {wallet}: {saved} trades")
    return saved


async def backfill_all_known(limit: int | None = None) -> None:
    """Backfill de todos los wallets en `traders`."""
    from src.db.schema import db

    with db() as conn:
        rows = conn.execute(
            "SELECT wallet FROM traders ORDER BY first_seen_at ASC"
            + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
    wallets = [r["wallet"] for r in rows]
    console.print(f"[cyan]Backfill de {len(wallets)} wallets…[/cyan]")
    for i, w in enumerate(wallets, 1):
        console.print(f"[dim]({i}/{len(wallets)})[/dim] {w}")
        try:
            await backfill_wallet(w)
        except Exception as e:
            log.exception("backfill failed for %s: %s", w, e)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "discover":
        asyncio.run(discover_traders())
    elif len(sys.argv) > 2 and sys.argv[1] == "wallet":
        asyncio.run(backfill_wallet(sys.argv[2]))
    else:
        print("Uso: python -m src.indexer.trades discover")
        print("     python -m src.indexer.trades wallet 0x...")
