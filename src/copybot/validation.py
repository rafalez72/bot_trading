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


# ---------------------------------------------------------------------------
# Cross-strategy capital + PnL helpers (bugs 1+2+3 del PRE_LIVE_AUDIT)
# ---------------------------------------------------------------------------
#
# Pre 2026-05-10: capital_full, kill_switch y notif "Acumulado" miraban SOLO
# la tabla activa de tradebook (paper_trades o live_trades). Las 5 strategies
# nuevas tienen tablas separadas (mm_orders, spike_arb_trades,
# adversarial_orders, long_horizon_trades, hedge_trades) — invisibles a los
# guards globales. Riesgo de over-allocation 4×–10× del cap y kill switch
# ciego.
#
# Estos helpers son single source of truth: misma config usada por
# validation.run_pre_open_checks, risk.check_kill_switch y
# learning.on_paper_trade_closed.
#
# Backward compat: fail-soft — si una tabla no existe (strategy nueva no
# inicializada todavía), su CONTRIB es 0, no error.
#
# Performance: UNION ALL en una sola query (no N round-trips).

# (table_name, size_col_or_None, pnl_col, exit_col)
# - size_col: columna que representa USDC comprometido en una posición abierta.
#   None ⇒ no contribuye a capital_in_use (caso hedge_trades: su poly leg ya
#   se contabiliza en paper/live_trades vía tradebook.open_position, así que
#   evitamos doble conteo).
# - pnl_col: columna del realized PnL del trade cerrado. Para hedge_trades
#   usamos pnl_perp_usdc (NO pnl_total_usdc) porque el poly leg ya entra en
#   paper/live_trades.pnl_usdc — sumarlo de nuevo doble-contaría.
# - exit_col: timestamp (epoch s) en el que el row alcanzó estado terminal.
#   Lo usamos para ventanas "hoy UTC" / "desde reset_at" y para consecutive
#   losses. Algunas tablas no exponen un closed_at propio (mm_orders /
#   adversarial_orders) — fallback a filled_at.
_CROSS_TABLE_SPECS: tuple[tuple[str, str | None, str, str], ...] = (
    ("paper_trades",          "entry_size_usdc", "pnl_usdc",      "exit_at"),
    ("live_trades",           "entry_size_usdc", "pnl_usdc",      "exit_at"),
    ("mm_orders",             "size_usdc",       "pnl_usdc",      "filled_at"),
    ("spike_arb_trades",      "size_usdc",       "pnl_usdc",      "closed_at"),
    ("adversarial_orders",    "size_usdc",       "pnl_usdc",      "filled_at"),
    ("long_horizon_trades",   "bet_usdc",        "pnl_usdc",      "closed_at"),
    ("hedge_trades",          None,              "pnl_perp_usdc", "closed_at"),
)


def _table_exists(conn, name: str) -> bool:
    """Devuelve True si la tabla existe en el backend activo.

    Backend-aware: SQLite usa sqlite_master, Postgres usa information_schema.
    Fail-soft: si la query rompe (DB no inicializada en tests), devuelve False.
    """
    try:
        from src.db.schema import BACKEND
    except Exception:
        BACKEND = "sqlite"
    try:
        if BACKEND == "postgres":
            r = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=?",
                (name,),
            ).fetchone()
        else:
            r = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
        return r is not None
    except Exception:
        return False


def _available_specs(
    conn,
    *,
    require_size: bool = False,
) -> list[tuple[str, str | None, str, str]]:
    """Lista de specs cuyas tablas existen en la DB activa.

    `require_size=True` ⇒ solo specs con `size_col is not None` (capital_full).
    """
    out = []
    for spec in _CROSS_TABLE_SPECS:
        table, size_col, _pnl, _exit = spec
        if require_size and size_col is None:
            continue
        if _table_exists(conn, table):
            out.append(spec)
    return out


def _total_capital_in_use(conn) -> float:
    """SUM de USDC comprometido en posiciones abiertas a través de TODAS las
    tablas relevantes (5 strategies nuevas + paper/live).

    Fail-soft por tabla: si una tabla no existe (strategy no inicializada
    todavía), contribuye 0. Performance: UNION ALL en una sola query.

    hedge_trades NO contribuye porque su poly leg ya está en paper/live_trades.
    """
    specs = _available_specs(conn, require_size=True)
    if not specs:
        return 0.0
    parts = []
    for table, size_col, _pnl, _exit in specs:
        parts.append(
            f"SELECT COALESCE(SUM({size_col}), 0) AS v "
            f"FROM {table} WHERE status='open'"
        )
    sql = "SELECT COALESCE(SUM(v), 0) AS total FROM (" + " UNION ALL ".join(parts) + ") AS u"
    try:
        r = conn.execute(sql).fetchone()
        return float(r["total"] or 0.0) if r else 0.0
    except Exception as e:
        log.warning("_total_capital_in_use failed: %s", e)
        return 0.0


# Statuses considerados "terminal cerrado" para SUM(pnl) cross-table.
# - paper/live_trades: status semántico ('closed_win', 'closed_loss',
#   'settled_win', 'settled_loss').
# - strategy tables: usan 'closed', 'filled' u otros. Como NO tienen el
#   sufijo _win/_loss, filtramos por "pnl_usdc IS NOT NULL" como proxy de
#   "ya cerrado con PnL realizado".
_CLASSIC_CLOSED = ("closed_win", "closed_loss", "settled_win", "settled_loss")


def _is_classic_trades_table(table: str) -> bool:
    return table in ("paper_trades", "live_trades")


def _total_pnl_since(conn, since_ts: int) -> float:
    """SUM(pnl_usdc-equivalent) cross-tables desde `since_ts`.

    Útil para layer1 (daily_loss_cap), drawdown y notif "Acumulado". Usa
    UNION ALL con normalización: cada tabla expone su pnl_col propio
    (pnl_usdc o pnl_perp_usdc en hedge) como una columna unificada `pnl`,
    filtrando por exit_col >= since_ts.
    """
    specs = _available_specs(conn)
    if not specs:
        return 0.0
    parts = []
    params: list[Any] = []
    for table, _size, pnl_col, exit_col in specs:
        if _is_classic_trades_table(table):
            status_clause = "status IN ('closed_win','closed_loss','settled_win','settled_loss')"
        else:
            # Strategy tables: terminal = pnl already realized + exit_col set.
            status_clause = f"{pnl_col} IS NOT NULL AND {exit_col} IS NOT NULL"
        parts.append(
            f"SELECT COALESCE({pnl_col}, 0) AS pnl "
            f"FROM {table} WHERE COALESCE({exit_col}, 0) >= ? AND {status_clause}"
        )
        params.append(since_ts)
    sql = "SELECT COALESCE(SUM(pnl), 0) AS total FROM (" + " UNION ALL ".join(parts) + ") AS u"
    try:
        r = conn.execute(sql, tuple(params)).fetchone()
        return float(r["total"] or 0.0) if r else 0.0
    except Exception as e:
        log.warning("_total_pnl_since failed: %s", e)
        return 0.0


def _recent_closed_cross_tables(conn, since_ts: int, limit: int) -> list[dict]:
    """Últimos `limit` trades cerrados (ordenados por exit DESC) cross-tables.

    Cada item: {table, id, exit_ts, pnl, is_loss}. Para consecutive_losses
    layer2: tomamos los últimos N items, definimos LOSS por:
      - paper/live_trades: status ∈ closed_loss/settled_loss
      - strategy tables  : pnl < 0 (terminal por construcción de la query)
    """
    specs = _available_specs(conn)
    if not specs:
        return []
    parts = []
    params: list[Any] = []
    for table, _size, pnl_col, exit_col in specs:
        if _is_classic_trades_table(table):
            status_clause = "status IN ('closed_win','closed_loss','settled_win','settled_loss')"
            is_loss_expr = "CASE WHEN status IN ('closed_loss','settled_loss') THEN 1 ELSE 0 END"
        else:
            status_clause = f"{pnl_col} IS NOT NULL AND {exit_col} IS NOT NULL"
            is_loss_expr = f"CASE WHEN COALESCE({pnl_col},0) < 0 THEN 1 ELSE 0 END"
        parts.append(
            f"SELECT '{table}' AS tbl, id AS id, "
            f"COALESCE({exit_col}, 0) AS exit_ts, "
            f"COALESCE({pnl_col}, 0) AS pnl, "
            f"{is_loss_expr} AS is_loss "
            f"FROM {table} WHERE COALESCE({exit_col}, 0) >= ? AND {status_clause}"
        )
        params.append(since_ts)
    sql = (
        "SELECT tbl, id, exit_ts, pnl, is_loss FROM ("
        + " UNION ALL ".join(parts)
        + ") AS u ORDER BY exit_ts DESC, id DESC LIMIT ?"
    )
    params.append(limit)
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except Exception as e:
        log.warning("_recent_closed_cross_tables failed: %s", e)
        return []
    out = []
    for r in rows:
        try:
            out.append({
                "table": r["tbl"],
                "id": int(r["id"]),
                "exit_ts": int(r["exit_ts"] or 0),
                "pnl": float(r["pnl"] or 0.0),
                "is_loss": bool(r["is_loss"]),
            })
        except Exception:
            continue
    return out

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
    # Threshold dinámico (override DB via /api/admin/thresholds) → env → default code.
    if not synthetic and m is not None:
        from src.copybot.threshold_overrides import get_market_horizon_min_secs
        try:
            end_date_raw = m["end_date"]
        except (KeyError, IndexError):
            end_date_raw = None
        end_ts = _parse_end_date_to_epoch(end_date_raw)
        if end_ts is not None:
            horizon_left = end_ts - int(time.time())
            horizon_min = get_market_horizon_min_secs()
            if horizon_left < horizon_min:
                _maybe_log(ctx, "market_too_short", horizon_left_s=horizon_left, min=horizon_min)
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
    # Thresholds dinámicos (override DB via /api/admin/thresholds) → env → default.
    cat = None
    if m:
        liq = m["liquidity"]
        vol = m["volume"]
        if not synthetic:
            from src.copybot.threshold_overrides import (
                get_min_market_liquidity_usdc,
                get_min_market_volume_usdc,
            )
            min_liq = get_min_market_liquidity_usdc()
            min_vol = get_min_market_volume_usdc()
            if liq is not None and liq < min_liq:
                _maybe_log(ctx, "low_liquidity", liquidity=liq, min=min_liq)
                return None, "low_liquidity"
            if vol is not None and vol < min_vol:
                _maybe_log(ctx, "low_volume", volume=vol, min=min_vol)
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

    # 18) capital_full (cap global) — cross-strategies.
    # Pre-fix solo miraba ctx.trades_table → 5 strategies (mm/spike/adv/lh/hedge)
    # invisibles → over-allocation 4×–10×. Ahora _total_capital_in_use suma
    # entry_size_usdc / size_usdc / bet_usdc de TODAS las tablas con
    # status='open' (helper en este mismo módulo, fail-soft si una tabla aún
    # no existe).
    global_open = _total_capital_in_use(conn)
    if global_open + size_usdc > ctx.capital_usdc + EPSILON:
        _maybe_log(ctx, "capital_full", open_usdc=global_open,
                   size_usdc=size_usdc, cap=ctx.capital_usdc)
        return None, "capital_full"

    return (size_usdc, m, cat), None
