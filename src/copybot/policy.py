"""Self-improvement: aprende de paper_trades cerrados y persiste reglas
de bloqueo por bucket (categoría, hora del día, rango de precio entry).

Política:
- Cada `refresh()` analiza los closed paper_trades en una ventana.
- Para cada bucket con >= MIN_TRADES_PER_BUCKET, calcula win_rate y PnL.
- Si win_rate < BLOCK_WIN_RATE Y pnl < 0 → marca el bucket como `blocked`.
- Si win_rate >= UNBLOCK_WIN_RATE Y pnl > 0 → unblock.
- `is_blocked(category, hour, price)` es lo que usa `paper.open_position`.

Las reglas viven en la tabla `bot_state` con prefijo `policy:`.
"""
from __future__ import annotations

import json
import logging

from src.db.schema import db, tx

log = logging.getLogger(__name__)

MIN_TRADES_PER_BUCKET = 8
BLOCK_WIN_RATE = 0.40
UNBLOCK_WIN_RATE = 0.55

PRICE_BUCKETS: list[tuple[float, float, str]] = [
    (0.05, 0.20, "0.05-0.20"),
    (0.20, 0.40, "0.20-0.40"),
    (0.40, 0.60, "0.40-0.60"),
    (0.60, 0.80, "0.60-0.80"),
    (0.80, 0.95, "0.80-0.95"),
]


def _price_bucket(p: float) -> str | None:
    for lo, hi, label in PRICE_BUCKETS:
        if lo <= p < hi:
            return label
    return None


def _hour_bucket(ts: int) -> str:
    """Hora UTC. 0..23 → '00','01',… string para serialización."""
    import datetime as _d
    return f"{_d.datetime.utcfromtimestamp(ts).hour:02d}"


def _save_policy(category_block: list[str], hour_block: list[str], price_block: list[str]) -> None:
    payload = {
        "category_block": sorted(category_block),
        "hour_block": sorted(hour_block),
        "price_block": sorted(price_block),
    }
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES ('policy:rules', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (json.dumps(payload, separators=(",", ":")),),
        )


def get_policy() -> dict:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key='policy:rules'"
        ).fetchone()
    if not r:
        return {"category_block": [], "hour_block": [], "price_block": []}
    try:
        return json.loads(r["value"])
    except Exception:
        return {"category_block": [], "hour_block": [], "price_block": []}


def is_blocked(*, category: str | None, entry_at: int, entry_price: float) -> str | None:
    """Si el (category, hour, price) está bloqueado, devuelve el motivo."""
    rules = get_policy()
    if category and category in rules.get("category_block", []):
        return f"policy:category={category}"
    h = _hour_bucket(entry_at)
    if h in rules.get("hour_block", []):
        return f"policy:hour={h}"
    pb = _price_bucket(entry_price)
    if pb and pb in rules.get("price_block", []):
        return f"policy:price={pb}"
    return None


def _aggregate(window_days: int = 14) -> dict:
    """Agrega closed paper_trades por bucket dentro de la ventana."""
    import time as _t
    cutoff = int(_t.time()) - window_days * 86400

    with db() as conn:
        rows = conn.execute(
            """
            SELECT pt.id, pt.condition_id, pt.entry_price, pt.entry_at,
                   pt.pnl_usdc, pt.status, m.category
            FROM paper_trades pt
            LEFT JOIN markets m ON m.condition_id = pt.condition_id
            WHERE pt.exit_at >= ?
              AND pt.status IN ('closed_win','closed_loss','settled_win','settled_loss')
            """,
            (cutoff,),
        ).fetchall()

    by_cat: dict[str, dict] = {}
    by_hour: dict[str, dict] = {}
    by_price: dict[str, dict] = {}

    def _bump(d: dict, key: str, win: bool, pnl: float) -> None:
        b = d.setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if win else 0
        b["pnl"] += pnl

    for r in rows:
        win = r["status"].endswith("_win")
        pnl = r["pnl_usdc"] or 0
        cat = r["category"] or "(sin categoría)"
        h = _hour_bucket(r["entry_at"] or 0)
        pb = _price_bucket(r["entry_price"] or 0)
        _bump(by_cat, cat, win, pnl)
        _bump(by_hour, h, win, pnl)
        if pb:
            _bump(by_price, pb, win, pnl)

    return {"category": by_cat, "hour": by_hour, "price": by_price, "n_total": len(rows)}


def refresh(*, window_days: int = 14) -> dict:
    """Recalcula la policy a partir de la ventana indicada.

    Devuelve:
      { added_blocks: { category:[], hour:[], price:[] },
        removed_blocks: { ... },
        n_analyzed }
    """
    agg = _aggregate(window_days=window_days)
    if agg["n_total"] < MIN_TRADES_PER_BUCKET:
        log.info("policy.refresh skipped: muy poca data (%d)", agg["n_total"])
        return {"skipped": True, "n_analyzed": agg["n_total"]}

    cur = get_policy()
    new_cat = set(cur.get("category_block", []))
    new_hour = set(cur.get("hour_block", []))
    new_price = set(cur.get("price_block", []))

    def _evaluate(d: dict, blocked: set[str]) -> tuple[set[str], set[str]]:
        added: set[str] = set()
        removed: set[str] = set()
        for k, b in d.items():
            n = b["n"]
            if n < MIN_TRADES_PER_BUCKET:
                continue
            wr = b["wins"] / n
            pnl = b["pnl"]
            if k not in blocked and wr < BLOCK_WIN_RATE and pnl < 0:
                added.add(k)
            elif k in blocked and wr >= UNBLOCK_WIN_RATE and pnl > 0:
                removed.add(k)
        return added, removed

    cat_add, cat_rem = _evaluate(agg["category"], new_cat)
    hour_add, hour_rem = _evaluate(agg["hour"], new_hour)
    price_add, price_rem = _evaluate(agg["price"], new_price)

    new_cat = (new_cat | cat_add) - cat_rem
    new_hour = (new_hour | hour_add) - hour_rem
    new_price = (new_price | price_add) - price_rem

    _save_policy(list(new_cat), list(new_hour), list(new_price))

    # Log significativo
    if cat_add or hour_add or price_add or cat_rem or hour_rem or price_rem:
        with tx() as conn:
            conn.execute(
                """
                INSERT INTO learning_events
                    (wallet, event_type, before_value, after_value, delta, trigger, metric_snapshot)
                VALUES ('(system)', 'policy_update', NULL, NULL, NULL, ?, ?)
                """,
                (
                    f"policy refresh: +{len(cat_add)+len(hour_add)+len(price_add)} bloqueos, "
                    f"-{len(cat_rem)+len(hour_rem)+len(price_rem)} desbloqueos",
                    json.dumps({
                        "added": {
                            "category": sorted(cat_add),
                            "hour": sorted(hour_add),
                            "price": sorted(price_add),
                        },
                        "removed": {
                            "category": sorted(cat_rem),
                            "hour": sorted(hour_rem),
                            "price": sorted(price_rem),
                        },
                        "n_analyzed": agg["n_total"],
                    }),
                ),
            )

    return {
        "n_analyzed": agg["n_total"],
        "added_blocks": {
            "category": sorted(cat_add),
            "hour": sorted(hour_add),
            "price": sorted(price_add),
        },
        "removed_blocks": {
            "category": sorted(cat_rem),
            "hour": sorted(hour_rem),
            "price": sorted(price_rem),
        },
        "current_policy": {
            "category_block": sorted(new_cat),
            "hour_block": sorted(new_hour),
            "price_block": sorted(new_price),
        },
        "agg": agg,
    }
