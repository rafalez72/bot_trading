"""Pre-open validation checks compartidos entre paper y live.

Single source of truth para todos los filtros que decidieron abrir vs
rechazar un BUY copiado. Antes (pre 2026-05-10) la lógica estaba duplicada
en `paper.open_position` y `executor._open_position_validate`, lo que
generaba drift: hoy fixeamos crypto_arb exempts en paper.py y olvidamos
hacer lo mismo en executor.py — el bot N2 directamente NO podía operar
en LIVE.

Diseño:
- `TradeValidationContext` parametriza los call sites (tabla destino,
  capital, threshold de PnL esperado, callback de log_reject opcional).
- `run_pre_open_checks(conn, ctx)` corre todos los filtros en orden,
  devuelve `((size_usdc, market_row, category), None)` si pasa, o
  `(None, reject_reason)` si rechaza.
- `crypto_arb` es source_wallet sintético del bot N2 (no es una wallet
  real de Polymarket). Está exempt de los filtros que asumen "wallet
  real con subscription, cluster, métricas históricas, etc."

Filtros aplicados (en orden):
 1. kill_switch                        — global guard
 2. stale_trade                        — trade del source > N seg viejo
 3. no_subscription / inactive         — wallet no en copy_subscriptions (skip crypto_arb)
 4. cluster_blocked                    — cluster del wallet bloqueado (skip crypto_arb)
 5. duplicate                          — source_trade_id ya copiado
 6. market_too_short (Feature A)       — end_date <30min (skip crypto_arb)
 7. ultrashort_market (Feature E)      — slug crypto-shortterm/esports vivo (skip crypto_arb)
 8. expires_too_soon                   — slug-epoch <10min (skip crypto_arb)
 9. low_liquidity                      — liq < MIN (skip crypto_arb)
10. low_volume                         — vol < MIN (skip crypto_arb)
11. category_blocked                   — categoría con auto-block (skip crypto_arb)
12. policy_blocked                     — policy bucket-level (skip crypto_arb)
13. extreme_price                      — price <0.05 o >0.95
14. diversification_cap                — wallet > 50% trades 24h (skip crypto_arb)
15. expected_pnl_too_low               — fees > expected (skip crypto_arb)
16. wallet_concentration               — > N entries simultáneas (wallet, cid)
17. market_concentration               — sum entries > MAX_PER_MARKET_PCT × capital
18. capital_full                       — sum entries > capital
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from src.config import (
    BLOCK_ULTRASHORT_MARKETS,
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

log = logging.getLogger(__name__)
EPSILON = 1e-6

# Source sintético del bot N2 (crypto temporal arb). No tiene entry en
# copy_subscriptions, opera markets ultracortos por design, y usa su
# propio modelo de filtrado (edge probabilístico) — exempt de varios
# filtros del copytrading clásico.
SYNTHETIC_SOURCES = {"crypto_arb"}


def is_synthetic_source(source_wallet: str | None) -> bool:
    return source_wallet in SYNTHETIC_SOURCES


# --- Stale-log dedup (LRU, evita spam de 12+ rejects/seg del polling) ---

_STALE_LOG_TTL_SECONDS = 60
_STALE_LOG_MAX_ENTRIES = 1000
_stale_log_seen: dict[str, float] = {}


def should_log_stale(trade_id: str | None) -> bool:
    """True si este source_trade_id no fue logueado como 'stale' en los
    últimos _STALE_LOG_TTL_SECONDS. LRU con cap 1000.
    """
    if not trade_id:
        return True
    now = time.time()
    last = _stale_log_seen.get(trade_id)
    if last is not None and (now - last) < _STALE_LOG_TTL_SECONDS:
        return False
    if len(_stale_log_seen) >= _STALE_LOG_MAX_ENTRIES:
        try:
            oldest = min(_stale_log_seen, key=_stale_log_seen.get)
            _stale_log_seen.pop(oldest, None)
        except ValueError:
            _stale_log_seen.clear()
    _stale_log_seen[trade_id] = now
    return True


# --- Helpers de parsing ---

def _parse_end_date_to_epoch(end_date: Any) -> int | None:
    """Convierte el `end_date` de la tabla markets a epoch seconds."""
    if end_date is None or end_date == "":
        return None
    if isinstance(end_date, (int, float)):
        v = int(end_date)
        return v // 1000 if v > 9_999_999_999 else v
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _market_stub(conn, condition_id: str, raw: dict | None):
    """Asegura que el mercado existe en la tabla `markets`. Si no, crea
    un stub con slug/title del raw del trade. Devuelve la fila.
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
        "SELECT volume, liquidity, category, slug, end_date FROM markets WHERE condition_id=?",
        (condition_id,),
    ).fetchone()


def _check_kill_switch(conn) -> bool:
    r = conn.execute(
        "SELECT value FROM bot_state WHERE key='kill_switch'"
    ).fetchone()
    return bool(r and r["value"] == "active")


# --- Context parametrizado por modo paper/live ---

@dataclass
class TradeValidationContext:
    """Parametriza el call site (paper o live) sin duplicar lógica."""
    source_wallet: str
    source_trade_id: str
    condition_id: str
    outcome_index: int | None
    price: float
    timestamp: int
    raw: dict | None

    # Tabla destino para queries de duplicate / wallet_concentration /
    # market_concentration / diversification / capital_full. paper_trades
    # o live_trades.
    trades_table: str

    # Capital y base size del bot. paper usa BOT_CAPITAL_USDC + COPY_BASE_USDC,
    # live usa LIVE_CAPITAL_USDC + LIVE_BASE_USDC.
    capital_usdc: float
    base_usdc: float

    # Threshold de PnL esperado neto de fees. paper usa
    # PAPER_MIN_EXPECTED_PNL_USDC, live usa LIVE_MIN_EXPECTED_PNL_USDC.
    min_expected_pnl_usdc: float

    # Callback opcional para persistir rejects en DB (executor lo usa para
    # live_rejects, paper actualmente no persiste). Firma:
    # log_reject(reason, detail=None) — los otros campos los toma del ctx.
    log_reject: Callable[..., None] | None = None


def _maybe_log(ctx: TradeValidationContext, reason: str, **detail) -> None:
    if ctx.log_reject is not None:
        ctx.log_reject(reason, detail=detail or None)


def run_pre_open_checks(conn, ctx: TradeValidationContext):
    """Aplica todos los pre-open checks. Devuelve:
       ((size_usdc, market_row, category), None) si pasa, o
       (None, reject_reason) si rechaza.
    """
    synthetic = is_synthetic_source(ctx.source_wallet)

    # 1) kill_switch (global)
    if _check_kill_switch(conn):
        _maybe_log(ctx, "kill_switch")
        return None, "kill_switch"

    # 2) stale_trade
    age = int(time.time()) - int(ctx.timestamp or 0)
    if age > STALE_TRADE_MAX_AGE_S:
        if should_log_stale(ctx.source_trade_id):
            _maybe_log(ctx, "stale_trade", age_s=age, threshold_s=STALE_TRADE_MAX_AGE_S)
        return None, "stale_trade"

    # 3) Subscription / cluster (skip crypto_arb)
    sizing = 1.0
    cs = None
    if not synthetic:
        sub = conn.execute(
            "SELECT sizing_mult, status FROM copy_subscriptions WHERE wallet=?",
            (ctx.source_wallet,),
        ).fetchone()
        if not sub:
            _maybe_log(ctx, "no_subscription")
            return None, "no_subscription"
        if sub["status"] != "active":
            _maybe_log(ctx, "inactive", sub_status=sub["status"])
            return None, "inactive"
        sm = sub["sizing_mult"]
        sizing = 1.0 if sm is None else float(sm)
        if sizing <= EPSILON:
            _maybe_log(ctx, "inactive", sizing_mult=sizing)
            return None, "inactive"

        # 4) cluster_blocked
        cs = conn.execute(
            """
            SELECT cp.status FROM wallet_clusters wc
            JOIN cluster_perf cp ON cp.cluster_id = wc.cluster_id
            WHERE wc.wallet=?
            """,
            (ctx.source_wallet,),
        ).fetchone()
        if cs and cs["status"] == "blocked":
            _maybe_log(ctx, "cluster_blocked")
            return None, "cluster_blocked"
        if cs and cs["status"] == "penalized":
            sizing = sizing * 0.5

    # 5) duplicate
    dup = conn.execute(
        f"SELECT id FROM {ctx.trades_table} WHERE source_trade_id=?",
        (ctx.source_trade_id,),
    ).fetchone()
    if dup:
        return None, "duplicate"

    # Resolver market stub + slug temprano
    m = _market_stub(conn, ctx.condition_id, ctx.raw)
    slug = (ctx.raw or {}).get("slug") or (ctx.raw or {}).get("eventSlug")
    if not slug and m is not None:
        try:
            slug = m["slug"]
        except (KeyError, IndexError):
            slug = None

    # 6) market_too_short (Feature A) — skip crypto_arb
    if not synthetic and m is not None:
        try:
            end_date_raw = m["end_date"]
        except (KeyError, IndexError):
            end_date_raw = None
        end_ts = _parse_end_date_to_epoch(end_date_raw)
        if end_ts is not None:
            horizon_left = end_ts - int(time.time())
            if horizon_left < MARKET_HORIZON_MIN_SECS:
                _maybe_log(ctx, "market_too_short", horizon_left_s=horizon_left)
                return None, "market_too_short"

    # 7) ultrashort_market (Feature E) — skip crypto_arb
    if BLOCK_ULTRASHORT_MARKETS and not synthetic:
        from src.copybot.categorize import is_ultrashort_market
        end_ts_for_cat = None
        if m is not None:
            try:
                end_ts_for_cat = _parse_end_date_to_epoch(m["end_date"])
            except (KeyError, IndexError):
                end_ts_for_cat = None
        if is_ultrashort_market(slug, end_ts_for_cat):
            _maybe_log(ctx, "ultrashort_market", slug=slug)
            return None, "ultrashort_market"

    # 8) expires_too_soon (slug-epoch) — skip crypto_arb
    if not synthetic:
        expiry_ts = parse_slug_expiry(slug)
        if expiry_ts is not None:
            time_left = expiry_ts - int(time.time())
            if time_left < MIN_TIME_TO_EXPIRY_SECONDS:
                _maybe_log(ctx, "expires_too_soon", slug=slug, time_left_sec=time_left,
                           min_required=MIN_TIME_TO_EXPIRY_SECONDS)
                return None, "expires_too_soon"

    # 9-11) low_liquidity / low_volume / category_blocked — skip crypto_arb
    cat = None
    if m:
        liq = m["liquidity"]
        vol = m["volume"]
        if not synthetic:
            if liq is not None and liq < MIN_MARKET_LIQUIDITY_USDC:
                _maybe_log(ctx, "low_liquidity", liquidity=liq, min=MIN_MARKET_LIQUIDITY_USDC)
                return None, "low_liquidity"
            if vol is not None and vol < MIN_MARKET_VOLUME_USDC:
                _maybe_log(ctx, "low_volume", volume=vol, min=MIN_MARKET_VOLUME_USDC)
                return None, "low_volume"
        cat = m["category"]
        if not synthetic and cat and cat != "(sin categoría)":
            cat_row = conn.execute(
                "SELECT status FROM category_perf WHERE category=?", (cat,)
            ).fetchone()
            if cat_row and cat_row["status"] == "blocked":
                _maybe_log(ctx, "category_blocked", category=cat)
                return None, "category_blocked"

    # 12) policy_blocked — skip crypto_arb
    if not synthetic:
        from src.copybot.policy import is_blocked as policy_blocked
        if policy_blocked(category=cat, entry_at=ctx.timestamp, entry_price=ctx.price):
            _maybe_log(ctx, "policy_blocked", category=cat)
            return None, "policy_blocked"

    # 13) extreme_price (aplica a todos)
    if ctx.price < 0.05 or ctx.price > 0.95:
        _maybe_log(ctx, "extreme_price")
        return None, "extreme_price"

    # 14) diversification_cap — skip crypto_arb
    if not synthetic:
        div_row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN source_wallet=? THEN 1 ELSE 0 END) AS this_wallet
            FROM {ctx.trades_table}
            WHERE entry_at >= strftime('%s','now') - 86400
            """,
            (ctx.source_wallet,),
        ).fetchone()
        total_24h = (div_row["total"] or 0) if div_row else 0
        this_wallet_24h = (div_row["this_wallet"] or 0) if div_row else 0
        if total_24h >= 10 and (this_wallet_24h / total_24h) > MAX_WALLET_24H_PCT:
            _maybe_log(ctx, "diversification_cap", total_24h=total_24h,
                       this_wallet_24h=this_wallet_24h, pct=this_wallet_24h / total_24h,
                       cap=MAX_WALLET_24H_PCT)
            return None, "diversification_cap"

    # Tamaño base × sizing del bandit. En markets thin (liq < $3000) escalamos
    # 0.5× para mitigar slippage real. Sólo aplica al copytrading (live).
    liq = m["liquidity"] if (m and m["liquidity"] is not None) else None
    liq_factor = 0.5 if (liq is not None and liq < 3000 and not synthetic) else 1.0
    size_usdc = ctx.base_usdc * sizing * liq_factor

    # 15) expected_pnl_too_low — skip crypto_arb
    if not synthetic:
        from src.copybot.realism import expected_net_pnl
        expected = expected_net_pnl(size_usdc)
        if expected < ctx.min_expected_pnl_usdc:
            _maybe_log(ctx, "expected_pnl_too_low", size_usdc=size_usdc,
                       expected=expected, min=ctx.min_expected_pnl_usdc)
            return None, "expected_pnl_too_low"

    # 16) wallet_concentration (aplica a todos: cap N entries por wallet+cid)
    entries_open = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM {ctx.trades_table}
        WHERE source_wallet=? AND condition_id=? AND status='open'
        """,
        (ctx.source_wallet, ctx.condition_id),
    ).fetchone()["c"]
    if entries_open >= MAX_ENTRIES_PER_WALLET_MARKET:
        _maybe_log(ctx, "wallet_concentration", entries_open=entries_open,
                   max=MAX_ENTRIES_PER_WALLET_MARKET)
        return None, "wallet_concentration"

    # 17) market_concentration (cap dollar por cid)
    per_market_cap = ctx.capital_usdc * MAX_PER_MARKET_PCT
    market_open = conn.execute(
        f"""
        SELECT COALESCE(SUM(entry_size_usdc), 0) as v
        FROM {ctx.trades_table}
        WHERE condition_id=? AND status='open'
        """,
        (ctx.condition_id,),
    ).fetchone()["v"]
    if market_open + size_usdc > per_market_cap + EPSILON:
        _maybe_log(ctx, "market_concentration", open_usdc=market_open,
                   size_usdc=size_usdc, cap=per_market_cap)
        return None, "market_concentration"

    # 18) capital_full (cap global)
    global_open = conn.execute(
        f"SELECT COALESCE(SUM(entry_size_usdc), 0) as v FROM {ctx.trades_table} WHERE status='open'"
    ).fetchone()["v"]
    if global_open + size_usdc > ctx.capital_usdc + EPSILON:
        _maybe_log(ctx, "capital_full", open_usdc=global_open,
                   size_usdc=size_usdc, cap=ctx.capital_usdc)
        return None, "capital_full"

    return (size_usdc, m, cat), None
