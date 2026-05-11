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

import time

from src.config import (
    BLOCK_ULTRASHORT_MARKETS,
    BOT_CAPITAL_USDC,
    COPY_BASE_USDC,
    MARKET_HORIZON_MIN_SECS,
    MAX_ENTRIES_PER_WALLET_MARKET,
    MAX_PER_MARKET_PCT,
    MAX_WALLET_24H_PCT,
    MIN_MARKET_LIQUIDITY_USDC,
    MIN_MARKET_VOLUME_USDC,
    MIN_TIME_TO_EXPIRY_SECONDS,
    STALE_TRADE_MAX_AGE_S,
)
from src.copybot._slug_expiry import parse_slug_expiry
from src.copybot.learning import on_paper_trade_closed
from src.copybot.realism import (
    expected_net_pnl,
    post_close_costs,
    realistic_entry_price,
    realistic_exit_price,
)
from src.db.schema import db, tx

log = logging.getLogger(__name__)
EPSILON = 1e-6


def _parse_end_date_to_epoch(end_date: str | int | float | None) -> int | None:
    """Convierte el `end_date` de la tabla markets a epoch seconds.

    El indexer guarda el campo tal cual lo devuelve Gamma (ISO-8601 con `Z`).
    Tolera también valores numéricos (epoch directo).
    """
    if end_date is None or end_date == "":
        return None
    if isinstance(end_date, (int, float)):
        v = int(end_date)
        # Si vino en ms (>1e11), normalizamos a s.
        return v // 1000 if v > 9_999_999_999 else v
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


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
    "expires_too_soon", "diversification_cap", "expected_pnl_too_low",
    "wallet_concentration", "market_too_short", "ultrashort_market",
}

# Trades del wallet original más viejos que esto cuando los procesamos = no
# copiar. Razón: nuestro polling tiene latencia (~5-7s) + el trade puede haber
# llegado en una tanda histórica. Si el trade ya tiene >MAX_TRADE_AGE_SECONDS
# de antigüedad, perdimos el edge — entramos tarde a un movimiento ya hecho.
# Caso real 2026-05-06: 2 trades LoL Game 2 entry@0.45 con el match casi
# terminado → expiraron a $0.001 minutos después → -$10 cada uno.
#
# 2026-05-09: el threshold ahora vive en config.STALE_TRADE_MAX_AGE_S (default
# 300s). Mantenemos `MAX_TRADE_AGE_SECONDS` como alias re-exportado para
# back-compat con executor.py. El env var legacy `MAX_TRADE_AGE_SECONDS` toma
# precedencia si está seteado (deploys viejos lo usan).
import os as _os
_legacy_age = _os.getenv("MAX_TRADE_AGE_SECONDS")
MAX_TRADE_AGE_SECONDS = int(_legacy_age) if _legacy_age is not None else STALE_TRADE_MAX_AGE_S


# LRU in-memory dict para deduplicar logs de 'stale_trade'. El polling
# re-procesa los mismos trades detectados por WS varias veces por segundo,
# generando spam (12+ rejects iguales por segundo en producción). Suprimimos
# el log si ya rechazamos el mismo source_trade_id en los últimos 60s. La
# rejection sigue ocurriendo (no se abre el trade); solo silencia el log.
_STALE_LOG_TTL_SECONDS = 60
_STALE_LOG_MAX_ENTRIES = 1000
_stale_log_seen: dict[str, float] = {}


def _should_log_stale(trade_id: str | None) -> bool:
    """True si este source_trade_id no fue logueado como 'stale' en los últimos
    _STALE_LOG_TTL_SECONDS. Mantiene un LRU rudimentario (purga la entrada más
    vieja cuando supera _STALE_LOG_MAX_ENTRIES).

    Si trade_id es None/empty → siempre loguear (no podemos deduplicar sin key).
    """
    if not trade_id:
        return True
    now = time.time()
    last = _stale_log_seen.get(trade_id)
    if last is not None and (now - last) < _STALE_LOG_TTL_SECONDS:
        return False
    # Bound the dict size: si excede el cap, dropeamos la entrada más vieja.
    if len(_stale_log_seen) >= _STALE_LOG_MAX_ENTRIES:
        try:
            oldest_key = min(_stale_log_seen, key=_stale_log_seen.get)
            _stale_log_seen.pop(oldest_key, None)
        except ValueError:
            _stale_log_seen.clear()
    _stale_log_seen[trade_id] = now
    return True


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
        "SELECT volume, liquidity, category, slug, end_date FROM markets WHERE condition_id=?",
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
    our_entry_at: int | None = None,
    our_entry_price: float | None = None,
) -> tuple[int | None, str | None]:
    """Abre un paper_trade copiando el BUY del source.

    Devuelve (paper_trade_id, reject_reason). Si rechaza, paper_trade_id=None.

    `our_entry_at` y `our_entry_price` son telemetry de copy-lag: ts y mid
    locales en el momento que procesamos. Si se omiten, defaultean a los del
    source (compatibilidad pre-feature-G; rows viejas tienen NULL).
    """
    # 2026-05-10 refactor: todos los pre-open checks viven en
    # validation.run_pre_open_checks (single source of truth compartida con
    # executor.py para LIVE). Lo único que cambia entre paper/live es el
    # context (tabla destino, capital, threshold de pnl esperado).
    from src.copybot.validation import (
        TradeValidationContext,
        run_pre_open_checks,
    )
    from src.config import PAPER_MIN_EXPECTED_PNL_USDC

    ctx = TradeValidationContext(
        source_wallet=source_wallet,
        source_trade_id=source_trade_id,
        condition_id=condition_id,
        outcome_index=outcome_index,
        price=price,
        timestamp=timestamp,
        raw=raw,
        trades_table="paper_trades",
        capital_usdc=__import__("src.copybot.threshold_overrides", fromlist=["get_bot_capital_usdc"]).get_bot_capital_usdc(),
        base_usdc=COPY_BASE_USDC,
        min_expected_pnl_usdc=PAPER_MIN_EXPECTED_PNL_USDC,
        log_reject=None,  # paper no persiste rejects; los rejects se ven en logs
    )
    with tx() as conn:
        result, reject = run_pre_open_checks(conn, ctx)
        if reject:
            return None, reject
        size_usdc, m, _cat = result

        # Realismo: aplicar slippage de ejecución al precio del source.
        # No es un check de rechazo, así que se queda fuera del módulo
        # validation (que solo decide pasa/rechaza).
        liquidity = m["liquidity"] if m else None
        price = realistic_entry_price(price, liquidity)

        asset = (raw or {}).get("asset")
        # Feature G: telemetry de copy lag. Si el caller no pasó our_entry_at/
        # our_entry_price, defaulteamos our_entry_at a time.time() (procesamos
        # ahora). our_entry_price puede quedar None si el caller no pudo cotizar
        # mid (CLOB caído o asset no listado) — el análisis lo trata como
        # "lag price unknown".
        if our_entry_at is None:
            our_entry_at = int(time.time())
        cur = conn.execute(
            """
            INSERT INTO paper_trades
                (source_wallet, source_trade_id, condition_id, outcome, outcome_index,
                 side, entry_price, entry_size_usdc, entry_at, status, raw, asset,
                 peak_price, our_entry_at, our_entry_price)
            VALUES (?, ?, ?, ?, ?, 'BUY', ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """,
            (
                source_wallet, source_trade_id, condition_id, outcome, outcome_index,
                price, size_usdc, timestamp,
                json.dumps(raw, separators=(",", ":")) if raw else None,
                asset,
                price,  # peak_price arranca == entry_price
                int(our_entry_at),
                float(our_entry_price) if our_entry_price is not None else None,
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
    """Liquida paper_trades open o waiting_settlement cuyo mercado ya resolvió."""
    settled = 0
    with db() as conn:
        # Incluye waiting_settlement además de open: trades que sweep_stops
        # marcó parados (market closed/slug expirado/orderbook stale) deben
        # settlearse normalmente cuando outcome_prices esté en markets.
        rows = conn.execute(
            """
            SELECT pt.id, pt.entry_price, pt.entry_size_usdc, pt.outcome_index,
                   pt.side, m.outcome_prices
            FROM paper_trades pt
            JOIN markets m ON m.condition_id = pt.condition_id
            WHERE pt.status IN ('open', 'waiting_settlement') AND m.closed=1
            """,
        ).fetchall()

    to_settle: list[tuple] = []
    clv_records: list[tuple] = []  # (trade_id, entry, payout, side)
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
        clv_records.append((r["id"], entry, payout, r["side"]))

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
    # CLV tracking: ortogonal al PnL, no bloquea si falla. Llamado fuera
    # de la tx() porque record_clv abre su propia transacción.
    try:
        from src.copybot.clv_tracker import record_clv
        for tid, entry, payout, side in clv_records:
            try:
                record_clv(
                    trade_id=tid, entry_price=entry, closing_price=payout,
                    side=side or "BUY", source="paper",
                )
            except Exception:
                log.debug("clv_tracker.record_clv falló pid=%s", tid)
    except Exception:
        log.debug("clv_tracker import/loop error — settle continúa")

    for _, _, _, pid in to_settle:
        on_paper_trade_closed(pid)
        settled += 1
    return settled
