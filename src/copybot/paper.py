"""Motor de paper trading.

Reglas (transparentes):
- Cuando un wallet copiado hace BUY → abrimos paper_trade con
  entry_size_usdc = COPY_BASE_USDC × sizing_mult.
- Cuando hace SELL → buscamos la posición abierta más vieja del mismo
  (wallet, condition_id, outcome_index) y la cerramos al precio del SELL.
- Settlement: para paper_trades open cuyo mercado ya resolvió,
  liquidamos con outcome_prices[outcome_index].
- Cada cierre dispara `learning.on_paper_trade_closed`.

Política FIFO por simplicidad. No simulamos slippage ni partial fills.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

from src.config import (
    BOT_CAPITAL_USDC,
    COPY_BASE_USDC,
    MAX_PER_MARKET_PCT,
    MIN_MARKET_LIQUIDITY_USDC,
    MIN_MARKET_VOLUME_USDC,
)
from src.copybot.learning import on_paper_trade_closed
from src.copybot.realism import (
    post_close_costs,
    realistic_entry_price,
    realistic_exit_price,
)
from src.db.schema import db, tx

log = logging.getLogger(__name__)
EPSILON = 1e-6


def _resolved_payout(outcome_prices_json: str | None, outcome_index: int | None) -> float | None:
    if not outcome_prices_json or outcome_index is None:
        return None
    try:
        prices = json.loads(outcome_prices_json)
        total = sum(float(p) for p in prices)
        if abs(total - 1.0) > 0.05:
            return None
        return float(prices[outcome_index])
    except Exception:
        return None


REJECT_REASONS = {
    "no_subscription", "inactive", "duplicate", "kill_switch", "capital_full",
    "market_concentration", "low_liquidity", "low_volume", "extreme_price",
    "category_blocked", "policy_blocked", "cluster_blocked", "stale_trade",
}

# Trades del wallet original más viejos que esto cuando los procesamos = no
# copiar. Razón: nuestro polling tiene latencia (~5-7s) + el trade puede haber
# llegado en una tanda histórica. Si el trade ya tiene >MAX_TRADE_AGE_SECONDS
# de antigüedad, perdimos el edge — entramos tarde a un movimiento ya hecho.
# Caso real 2026-05-06: 2 trades LoL Game 2 entry@0.45 con el match casi
# terminado → expiraron a $0.001 minutos después → -$10 cada uno.
import os as _os
MAX_TRADE_AGE_SECONDS = int(_os.getenv("MAX_TRADE_AGE_SECONDS", "60"))


def _check_kill_switch(conn) -> bool:
    """Devuelve True si el kill switch está activo (no abrir nuevas posiciones)."""
    r = conn.execute(
        "SELECT value FROM bot_state WHERE key='kill_switch'"
    ).fetchone()
    return bool(r and r["value"] == "active")


def _ensure_market_stub(conn, condition_id: str, raw: dict | None) -> dict | None:
    """Si el mercado no existe, crea un stub usando los campos del raw del trade.

    Los trades de Polymarket traen slug/title/eventSlug, lo que nos permite
    categorizar mercados negRisk que la Gamma API no expone por conditionId.

    Devuelve la fila resultante (existente o stub).
    """
    m = conn.execute(
        "SELECT volume, liquidity, category, slug FROM markets WHERE condition_id=?",
        (condition_id,),
    ).fetchone()
    if m and (m["category"] or m["slug"]):
        return m
    if not raw:
        return m
    slug = raw.get("slug") or raw.get("eventSlug")
    title = raw.get("title") or raw.get("name")
    if not (slug or title):
        return m
    from src.copybot.categorize import infer
    category = infer(slug, title)
    if m:
        # Update parcial con lo que vino del raw
        conn.execute(
            """
            UPDATE markets SET
                slug = COALESCE(slug, ?),
                question = COALESCE(question, ?),
                category = COALESCE(category, ?)
            WHERE condition_id=?
            """,
            (slug, title, category, condition_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO markets (condition_id, slug, question, category, active, closed)
            VALUES (?, ?, ?, ?, 1, 0)
            """,
            (condition_id, slug, title, category),
        )
    return conn.execute(
        "SELECT volume, liquidity, category, slug FROM markets WHERE condition_id=?",
        (condition_id,),
    ).fetchone()


def open_position(
    *,
    source_wallet: str,
    source_trade_id: str,
    condition_id: str,
    outcome: str | None,
    outcome_index: int | None,
    price: float,
    timestamp: int,
    raw: dict | None = None,
) -> tuple[int | None, str | None]:
    """Abre un paper_trade copiando el BUY del source.

    Devuelve (paper_trade_id, reject_reason). Si rechaza, paper_trade_id=None.
    """
    with tx() as conn:
        if _check_kill_switch(conn):
            return None, "kill_switch"

        # Anti-stale: si el trade del source wallet tiene más de
        # MAX_TRADE_AGE_SECONDS de antigüedad cuando lo procesamos, no
        # copiar. Caso 2026-05-06: 2 trades LoL Game 2 entry tardío → -$10
        # cada uno cuando el match terminó a $0.001 minutos después.
        import time as _time
        age = int(_time.time()) - int(timestamp or 0)
        if age > MAX_TRADE_AGE_SECONDS:
            return None, "stale_trade"

        sub = conn.execute(
            "SELECT sizing_mult, status FROM copy_subscriptions WHERE wallet=?",
            (source_wallet,),
        ).fetchone()
        if not sub:
            return None, "no_subscription"
        if sub["status"] != "active":
            return None, "inactive"
        # Usar `is None` y NO `or` — un sizing_mult=0 (drop residual) con `or`
        # se convertía a 1.0, dejando wallets dropped operando como zombies.
        sm = sub["sizing_mult"]
        sizing = 1.0 if sm is None else float(sm)
        if sizing <= EPSILON:
            return None, "inactive"

        # Cluster check: si el cluster del wallet está blocked, no abrir
        cs = conn.execute(
            """
            SELECT cp.status, cp.avg_win_rate, cp.pnl_usdc, cp.n_trades
            FROM wallet_clusters wc
            JOIN cluster_perf cp ON cp.cluster_id = wc.cluster_id
            WHERE wc.wallet=?
            """,
            (source_wallet,),
        ).fetchone()
        if cs and cs["status"] == "blocked":
            return None, "cluster_blocked"
        # Si el cluster está penalizado, achicamos el sizing a la mitad
        if cs and cs["status"] == "penalized":
            sizing = sizing * 0.5

        # Duplicado por source_trade_id
        dup = conn.execute(
            "SELECT id FROM paper_trades WHERE source_trade_id=?",
            (source_trade_id,),
        ).fetchone()
        if dup:
            return None, "duplicate"

        # Liquidez / volumen / categoría del mercado
        # Si no está en `markets`, creamos stub con slug/title del raw del trade
        # (cubre los mercados negRisk que la Gamma API no devuelve por conditionId)
        m = _ensure_market_stub(conn, condition_id, raw)
        cat = None
        if m:
            liq = m["liquidity"]
            vol = m["volume"]
            # Sólo rechazamos por liquidez/volumen si TENEMOS el dato.
            # Para stubs (None) confiamos en que el source trader ya filtró.
            if liq is not None and liq < MIN_MARKET_LIQUIDITY_USDC:
                return None, "low_liquidity"
            if vol is not None and vol < MIN_MARKET_VOLUME_USDC:
                return None, "low_volume"
            cat = m["category"]
            if cat:
                cat_row = conn.execute(
                    "SELECT status FROM category_perf WHERE category=?", (cat,)
                ).fetchone()
                if cat_row and cat_row["status"] == "blocked":
                    return None, "category_blocked"

        # Policy self-improvement: bucket-level blocks
        from src.copybot.policy import is_blocked as policy_blocked
        block_reason = policy_blocked(category=cat, entry_at=timestamp, entry_price=price)
        if block_reason:
            return None, "policy_blocked"

        # Precio extremo: no copiar entries muy cercanos a 0 o 1
        # (los wins son chicos, los losses pueden ser totales)
        if price < 0.05 or price > 0.95:
            return None, "extreme_price"

        size_usdc = COPY_BASE_USDC * sizing

        # Realismo: el bot paga MÁS que el source por slippage de ejecución
        liquidity = m["liquidity"] if m else None
        price = realistic_entry_price(price, liquidity)

        # Cap por mercado: max MAX_PER_MARKET_PCT del capital en un solo cid
        per_market_cap = BOT_CAPITAL_USDC * MAX_PER_MARKET_PCT
        market_open = conn.execute(
            """
            SELECT COALESCE(SUM(entry_size_usdc), 0) as v
            FROM paper_trades
            WHERE condition_id=? AND status='open'
            """,
            (condition_id,),
        ).fetchone()["v"]
        if market_open + size_usdc > per_market_cap + EPSILON:
            return None, "market_concentration"

        # Cap global: total open <= BOT_CAPITAL_USDC
        global_open = conn.execute(
            "SELECT COALESCE(SUM(entry_size_usdc), 0) as v FROM paper_trades WHERE status='open'"
        ).fetchone()["v"]
        if global_open + size_usdc > BOT_CAPITAL_USDC + EPSILON:
            return None, "capital_full"

        asset = (raw or {}).get("asset")
        cur = conn.execute(
            """
            INSERT INTO paper_trades
                (source_wallet, source_trade_id, condition_id, outcome, outcome_index,
                 side, entry_price, entry_size_usdc, entry_at, status, raw, asset,
                 peak_price)
            VALUES (?, ?, ?, ?, ?, 'BUY', ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                source_wallet, source_trade_id, condition_id, outcome, outcome_index,
                price, size_usdc, timestamp,
                json.dumps(raw, separators=(",", ":")) if raw else None,
                asset,
                price,  # peak_price arranca == entry_price
            ),
        )
        return cur.lastrowid, None


def _settle_pnl(entry_price: float, size: float, exit_price: float) -> float:
    if entry_price <= EPSILON:
        return 0.0
    shares = size / entry_price
    return shares * (exit_price - entry_price)


def close_position(
    *,
    source_wallet: str,
    condition_id: str,
    outcome_index: int | None,
    price: float,
    timestamp: int,
    reason: str = "source_sell",
) -> int | None:
    """Cierra la posición FIFO más vieja matcheando (wallet, cid, outcome)."""
    with tx() as conn:
        row = conn.execute(
            """
            SELECT id, entry_price, entry_size_usdc FROM paper_trades
            WHERE source_wallet=? AND condition_id=? AND outcome_index=? AND status='open'
            ORDER BY entry_at ASC LIMIT 1
            """,
            (source_wallet, condition_id, outcome_index),
        ).fetchone()
        if not row:
            return None

        # Realismo en la salida: vendemos a precio peor que el observado
        liq_row = conn.execute(
            "SELECT liquidity FROM markets WHERE condition_id=?",
            (condition_id,),
        ).fetchone()
        liquidity = liq_row["liquidity"] if liq_row else None
        exit_p = realistic_exit_price(price, liquidity, is_stop_loss=False)

        gross_pnl = _settle_pnl(row["entry_price"], row["entry_size_usdc"], exit_p)
        _, _, net_pnl = post_close_costs(gross_pnl)
        status = "closed_win" if net_pnl > 0 else "closed_loss"

        conn.execute(
            """
            UPDATE paper_trades
            SET exit_price=?, exit_at=?, pnl_usdc=?, status=?, exit_reason=?
            WHERE id=?
            """,
            (exit_p, timestamp, net_pnl, status, reason, row["id"]),
        )
        pid = row["id"]

    on_paper_trade_closed(pid)
    return pid


def force_close(paper_trade_id: int, exit_price: float, *, reason: str) -> None:
    """Fuerza el cierre de un paper_trade (stop-loss / take-profit)."""
    is_sl = reason.startswith("stop_loss")
    with tx() as conn:
        row = conn.execute(
            """
            SELECT pt.entry_price, pt.entry_size_usdc, pt.status,
                   pt.condition_id, m.liquidity
            FROM paper_trades pt
            LEFT JOIN markets m ON m.condition_id = pt.condition_id
            WHERE pt.id=?
            """,
            (paper_trade_id,),
        ).fetchone()
        if not row or row["status"] != "open":
            return

        # En stop-loss el slippage es mayor (vender en mercado en caída)
        exit_p = realistic_exit_price(
            exit_price, row["liquidity"], is_stop_loss=is_sl
        )
        gross_pnl = _settle_pnl(row["entry_price"], row["entry_size_usdc"], exit_p)
        _, _, net_pnl = post_close_costs(gross_pnl)
        status = "closed_win" if net_pnl > 0 else "closed_loss"
        conn.execute(
            """
            UPDATE paper_trades
            SET exit_price=?, exit_at=strftime('%s','now'),
                pnl_usdc=?, status=?, exit_reason=?
            WHERE id=?
            """,
            (exit_p, net_pnl, status, reason, paper_trade_id),
        )
    on_paper_trade_closed(paper_trade_id)


def settle_resolved() -> int:
    """Liquida paper_trades open cuyo mercado ya resolvió."""
    settled = 0
    with db() as conn:
        rows = conn.execute(
            """
            SELECT pt.id, pt.entry_price, pt.entry_size_usdc, pt.outcome_index,
                   m.outcome_prices
            FROM paper_trades pt
            JOIN markets m ON m.condition_id = pt.condition_id
            WHERE pt.status='open' AND m.closed=1
            """,
        ).fetchall()

    to_settle: list[tuple] = []
    for r in rows:
        payout = _resolved_payout(r["outcome_prices"], r["outcome_index"])
        if payout is None:
            continue
        entry = r["entry_price"]
        size = r["entry_size_usdc"]
        if entry > EPSILON:
            shares = size / entry
            gross = shares * (payout - entry)
        else:
            gross = 0.0
        # En settlement no hay slippage (el contrato resuelve a $1 o $0)
        # pero sí hay fees + gas
        _, _, net_pnl = post_close_costs(gross)
        status = "settled_win" if net_pnl > 0 else "settled_loss"
        to_settle.append((payout, net_pnl, status, r["id"]))

    if not to_settle:
        return 0

    with tx() as conn:
        conn.executemany(
            """
            UPDATE paper_trades
            SET exit_price=?, exit_at=strftime('%s','now'), pnl_usdc=?, status=?
            WHERE id=?
            """,
            to_settle,
        )
    for _, _, _, pid in to_settle:
        on_paper_trade_closed(pid)
        settled += 1
    return settled
