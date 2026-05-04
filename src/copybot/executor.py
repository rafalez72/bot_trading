"""Live execution con USDC reales en Polymarket CLOB (Fase 5).

Mirror de paper.py con la MISMA interfaz pública:
  - open_position(...)
  - close_position(...)
  - force_close(...)
  - settle_resolved()

Diferencia clave: en vez de simular el trade, manda una orden FOK al CLOB.
Si la orden no matchea (sin liquidez al precio), no se abre nada.

Reusa toda la lógica de validación de paper.py (kill switch, suscripción,
duplicados, market caps, category blocks, policy, etc.). Solo cambia:
  1. el size base (LIVE_BASE_USDC en vez de COPY_BASE_USDC)
  2. el cap global (LIVE_CAPITAL_USDC en vez de BOT_CAPITAL_USDC)
  3. la persistencia (live_trades en vez de paper_trades)
  4. el "execution": orden real vs INSERT de paper

Modo dry-run: si LIVE_DRY_RUN=true, simula la orden (no la manda) pero
sí la persiste en live_trades con dry_run=1. Útil para validar el flow.
"""
from __future__ import annotations

import json
import logging
import re
import time

from src.config import (
    LIVE_BASE_USDC,
    LIVE_CAPITAL_USDC,
    LIVE_DRY_RUN,
    LIVE_DRY_SLIPPAGE_PCT,
    LIVE_MAX_PER_WALLET_USDC,
    LIVE_MIN_EXPECTED_PNL_USDC,
    MAX_PER_MARKET_PCT,
    MAX_WALLET_24H_PCT,
    MIN_MARKET_LIQUIDITY_USDC,
    MIN_MARKET_VOLUME_USDC,
    MIN_TIME_TO_EXPIRY_SECONDS,
)
from src.copybot.learning import on_paper_trade_closed
from src.copybot.paper import EPSILON, _check_kill_switch, _ensure_market_stub
from src.copybot.realism import expected_net_pnl, post_close_costs
from src.db.schema import db, tx

log = logging.getLogger(__name__)


def _log_reject(source_wallet, condition_id, outcome_index, side, price, reason, detail=None):
    """Registra un reject en live_rejects para observabilidad.

    Best-effort: nunca debe romper el flujo principal. Si la INSERT falla
    (DB locked, schema viejo, etc.) loguea warning y sigue.
    """
    try:
        with tx() as conn:
            conn.execute(
                "INSERT INTO live_rejects (at, source_wallet, condition_id, outcome_index, side, price, reason, detail) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (int(time.time()), source_wallet, condition_id, outcome_index, side, price, reason, detail),
            )
    except Exception as e:
        log.warning("failed to log reject: %s", e)


_MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_slug_expiry(slug: str | None) -> int | None:
    """Extrae el timestamp UTC de expiry del slug usando múltiples estrategias.

    Hallazgo del 2026-05-01: el filtro original solo cubría slugs con epoch al
    final (`-5m-1777505400`). Pero hay otros formatos:
      • `bitcoin-up-or-down-may-1-2026-4am-et`    → fecha + hora ET
      • `atp-tiffon-coppeja-2026-05-01`           → solo fecha (deporte)
      • `lol-gx-navi-2026-04-29`                  → idem (esports)

    Estrategias en orden:
      1. Epoch unix al final.
      2. <month>-<day>-<year>-<H><am|pm>(-et)? → fecha + hora ET (UTC-4 EDT).
      3. -YYYY-MM-DD$ al final → fecha cruda. CONSERVADOR: usamos las 00:00 UTC
         del día como expiry, así si la fecha es HOY o pasado → time_left negativo
         → reject. Para deportes esto es seguro porque el match puede terminar
         en cualquier momento del día.

    Devuelve epoch UTC o None si no parseó.
    """
    if not slug:
        return None
    s = slug.lower()

    # 1. Epoch al final
    m = re.search(r"-(\d{10,13})$", s)
    if m:
        try:
            ts = int(m.group(1))
        except (TypeError, ValueError):
            ts = None
        if ts is not None:
            if ts > 10**12:
                ts = ts // 1000
            if 1700000000 < ts < 1900000000:
                return ts

    # 2. <month>-<day>-<year>-<H><am|pm>(-et)?
    months_pat = "|".join(_MONTH_MAP.keys())
    m = re.search(
        rf"-({months_pat})-(\d{{1,2}})-(\d{{4}})-(\d{{1,2}})(am|pm)(?:-et)?$",
        s,
    )
    if m:
        try:
            from datetime import datetime, timezone, timedelta
            month = _MONTH_MAP[m.group(1)]
            day = int(m.group(2))
            year = int(m.group(3))
            hour = int(m.group(4))
            if m.group(5) == "pm" and hour != 12:
                hour += 12
            if m.group(5) == "am" and hour == 12:
                hour = 0
            # ET = UTC-4 (EDT, en vigor mar-nov, mayoría del año). Convertir
            # local ET a UTC sumando 4h.
            utc_dt = datetime(year, month, day, hour, 0, tzinfo=timezone.utc) + timedelta(hours=4)
            return int(utc_dt.timestamp())
        except Exception:
            pass

    # 3. -YYYY-MM-DD$ al final → fecha cruda. Usamos END of day (23:59:59 UTC)
    # como expiry asumido. Razón: markets deportivos típicamente se juegan a la
    # noche/tarde y resuelven por la noche. Si usamos 00:00 UTC del día, todo
    # partido de "hoy" queda con time_left negativo y se rechaza. Con 23:59:59
    # permitimos entrar durante el día y el filtro de 10min bloquea solo el
    # tail end. Si la fecha es ayer o anterior → expiry pasado → block (correcto).
    m = re.search(r"-(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        try:
            from datetime import datetime, timezone
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            event_utc = datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
            return int(event_utc.timestamp())
        except Exception:
            pass

    return None


def _apply_dry_slippage(side: str, price: float) -> float:
    """Aplica slippage pesimista al precio simulado de un dry-run.

    BUY paga más (price * (1 + slippage)).
    SELL recibe menos (price * (1 - slippage)).
    Clamp a [0.01, 0.99] para evitar precios degenerados en bordes.
    """
    if price is None or price <= 0:
        return price
    if side.upper() == "BUY":
        adj = price * (1 + LIVE_DRY_SLIPPAGE_PCT)
    else:
        adj = price * (1 - LIVE_DRY_SLIPPAGE_PCT)
    return max(0.01, min(0.99, adj))


def _open_position_validate(conn, *, source_wallet, source_trade_id, condition_id,
                            outcome_index, price, timestamp, raw):
    """Reusa toda la validación de paper.open_position.

    Devuelve (size_usdc, market_row, category, category_allowed) si pasa, o
    (None, reject_reason) si rechaza. Es deliberadamente similar a paper para
    mantener simetría — cualquier mejora a los filtros se aplica a ambos.
    """
    if _check_kill_switch(conn):
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "kill_switch")
        return None, "kill_switch"

    sub = conn.execute(
        "SELECT sizing_mult, status FROM copy_subscriptions WHERE wallet=?",
        (source_wallet,),
    ).fetchone()
    if not sub:
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "no_subscription")
        return None, "no_subscription"
    if sub["status"] != "active":
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "inactive",
                    detail=json.dumps({"sub_status": sub["status"]}))
        return None, "inactive"
    # IMPORTANTE: usar `is None` y NO `or` — un sizing_mult=0 (drop residual)
    # con `or` se convertía a 1.0, dejando wallets dropped operando como
    # zombies. Ahora 0 es 0 y cae al check de EPSILON debajo.
    sm = sub["sizing_mult"]
    sizing = 1.0 if sm is None else float(sm)
    if sizing <= EPSILON:
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "inactive",
                    detail=json.dumps({"sizing_mult": sizing}))
        return None, "inactive"

    cs = conn.execute(
        """
        SELECT cp.status FROM wallet_clusters wc
        JOIN cluster_perf cp ON cp.cluster_id = wc.cluster_id
        WHERE wc.wallet=?
        """,
        (source_wallet,),
    ).fetchone()
    if cs and cs["status"] == "blocked":
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "cluster_blocked")
        return None, "cluster_blocked"
    if cs and cs["status"] == "penalized":
        sizing = sizing * 0.5

    dup = conn.execute(
        "SELECT id FROM live_trades WHERE source_trade_id=?",
        (source_trade_id,),
    ).fetchone()
    if dup:
        # duplicate: no se loguea (ruido alto, valor bajo)
        return None, "duplicate"

    m = _ensure_market_stub(conn, condition_id, raw)
    # Filtro inteligente: bloquear markets que expiran en <MIN_TIME_TO_EXPIRY_SECONDS.
    # Reemplaza el filtro lazy por slug pattern. Más preciso: un -15m- recién abierto
    # (15 min restantes) ya pasa, pero un -1h- con 3 min restantes se rechaza.
    # Si no hay timestamp parseable en el slug, fail-open (no bloquea — los slugs
    # sin epoch suelen ser markets largos: deportes, política, etc).
    slug = (raw or {}).get("slug") or (raw or {}).get("eventSlug")
    if not slug and m:
        slug = m["slug"] if "slug" in m.keys() else None
    expiry_ts = _parse_slug_expiry(slug)
    if expiry_ts is not None:
        time_left = expiry_ts - int(time.time())
        if time_left < MIN_TIME_TO_EXPIRY_SECONDS:
            _log_reject(source_wallet, condition_id, outcome_index, "BUY", price,
                        "expires_too_soon",
                        detail=json.dumps({"slug": slug, "time_left_sec": time_left,
                                            "min_required": MIN_TIME_TO_EXPIRY_SECONDS}))
            return None, "expires_too_soon"
    cat = None
    if m:
        liq = m["liquidity"]
        vol = m["volume"]
        if liq is not None and liq < MIN_MARKET_LIQUIDITY_USDC:
            _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "low_liquidity",
                        detail=json.dumps({"liquidity": liq, "min": MIN_MARKET_LIQUIDITY_USDC}))
            return None, "low_liquidity"
        if vol is not None and vol < MIN_MARKET_VOLUME_USDC:
            _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "low_volume",
                        detail=json.dumps({"volume": vol, "min": MIN_MARKET_VOLUME_USDC}))
            return None, "low_volume"
        cat = m["category"]
        if cat:
            cat_row = conn.execute(
                "SELECT status FROM category_perf WHERE category=?", (cat,)
            ).fetchone()
            if cat_row and cat_row["status"] == "blocked":
                _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "category_blocked",
                            detail=json.dumps({"category": cat}))
                return None, "category_blocked"

    from src.copybot.policy import is_blocked as policy_blocked
    if policy_blocked(category=cat, entry_at=timestamp, entry_price=price):
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "policy_blocked",
                    detail=json.dumps({"category": cat}))
        return None, "policy_blocked"

    if price < 0.05 or price > 0.95:
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "extreme_price")
        return None, "extreme_price"

    # Diversification cap: si un solo wallet ya hizo > MAX_WALLET_24H_PCT
    # de TODOS los trades en 24h, rechazamos para forzar diversificación.
    # Guard total>=10 para evitar rechazos cuando recién arrancamos
    # (1 trade de un wallet sería 100% de un sample chico).
    from src.copybot.tradebook import TABLE as _TABLE_DIV
    div_row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN source_wallet=? THEN 1 ELSE 0 END) AS this_wallet
        FROM {_TABLE_DIV}
        WHERE entry_at >= strftime('%s','now') - 86400
        """,
        (source_wallet,),
    ).fetchone()
    total_24h = (div_row["total"] or 0) if div_row else 0
    this_wallet_24h = (div_row["this_wallet"] or 0) if div_row else 0
    if total_24h >= 10 and (this_wallet_24h / total_24h) > MAX_WALLET_24H_PCT:
        _log_reject(
            source_wallet, condition_id, outcome_index, "BUY", price,
            "diversification_cap",
            detail=json.dumps({
                "total_24h": total_24h,
                "this_wallet_24h": this_wallet_24h,
                "pct": this_wallet_24h / total_24h,
                "cap": MAX_WALLET_24H_PCT,
            }),
        )
        return None, "diversification_cap"

    # Size base × sizing del bandit. Adicionalmente: en markets thin
    # (liq < $3000) escalamos a 0.5× para mitigar slippage real (los markets
    # chicos tienen orderbooks delgados → tu orden mueve el precio).
    # Límite duro: liq < MIN_MARKET_LIQUIDITY_USDC ya rechazó arriba; el
    # escalado aquí cubre la franja $1500-3000 = "operable pero arriesgado".
    liq = m["liquidity"] if (m and m["liquidity"] is not None) else None
    liq_factor = 0.5 if (liq is not None and liq < 3000) else 1.0
    size_usdc = LIVE_BASE_USDC * sizing * liq_factor

    # Filter anti-fees: si el PnL esperado del trade no cubre fees + slippage,
    # no vale la pena. Esto descarta trades donde sizing_mult dejó el size muy chico.
    expected = expected_net_pnl(size_usdc)
    if expected < LIVE_MIN_EXPECTED_PNL_USDC:
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "expected_pnl_too_low",
                    detail=json.dumps({"size_usdc": size_usdc, "expected": expected,
                                        "min": LIVE_MIN_EXPECTED_PNL_USDC}))
        return None, "expected_pnl_too_low"

    # Cap por wallet (forzosa diversificación): no más de X open por wallet.
    wallet_open = conn.execute(
        """
        SELECT COALESCE(SUM(entry_size_usdc), 0) as v
        FROM live_trades
        WHERE source_wallet=? AND status='open'
        """,
        (source_wallet,),
    ).fetchone()["v"]
    if wallet_open + size_usdc > LIVE_MAX_PER_WALLET_USDC + EPSILON:
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "wallet_concentration",
                    detail=json.dumps({"open_usdc": wallet_open, "size_usdc": size_usdc,
                                        "cap": LIVE_MAX_PER_WALLET_USDC}))
        return None, "wallet_concentration"

    per_market_cap = LIVE_CAPITAL_USDC * MAX_PER_MARKET_PCT
    market_open = conn.execute(
        """
        SELECT COALESCE(SUM(entry_size_usdc), 0) as v
        FROM live_trades
        WHERE condition_id=? AND status='open'
        """,
        (condition_id,),
    ).fetchone()["v"]
    if market_open + size_usdc > per_market_cap + EPSILON:
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "market_concentration",
                    detail=json.dumps({"open_usdc": market_open, "size_usdc": size_usdc,
                                        "cap": per_market_cap}))
        return None, "market_concentration"

    global_open = conn.execute(
        "SELECT COALESCE(SUM(entry_size_usdc), 0) as v FROM live_trades WHERE status='open'"
    ).fetchone()["v"]
    if global_open + size_usdc > LIVE_CAPITAL_USDC + EPSILON:
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "capital_full",
                    detail=json.dumps({"open_usdc": global_open, "size_usdc": size_usdc,
                                        "cap": LIVE_CAPITAL_USDC}))
        return None, "capital_full"

    return (size_usdc, m, cat), None


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
    """Abre un live_trade ejecutando una orden BUY real en el CLOB."""
    # Lazy import: no cargar py-clob-client si nunca se llama
    from src.polymarket.clob_client import get_token_id, place_market_order

    with tx() as conn:
        result, reject = _open_position_validate(
            conn,
            source_wallet=source_wallet,
            source_trade_id=source_trade_id,
            condition_id=condition_id,
            outcome_index=outcome_index,
            price=price,
            timestamp=timestamp,
            raw=raw,
        )
        if reject:
            return None, reject
        size_usdc, _market, _cat = result

    # Resolver token_id desde el CLOB (fuera de la tx, llama a la API)
    token_id = (raw or {}).get("asset")
    if not token_id:
        token_id = get_token_id(condition_id, outcome_index)
    if not token_id:
        log.warning("no se pudo resolver token_id para cid=%s oi=%s", condition_id[:10], outcome_index)
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "no_token_id")
        return None, "no_token_id"

    # Ejecutar la orden
    order = place_market_order(
        token_id=token_id,
        side="BUY",
        size_usdc=size_usdc,
        price=price,
        dry_run=LIVE_DRY_RUN,
        condition_id=condition_id,
    )
    if not order.ok:
        log.warning("BUY no matcheada: %s (cid=%s.. price=%.3f)",
                    order.error, condition_id[:10], price)
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price, "order_unmatched",
                    detail=json.dumps({"error": str(order.error)[:200]}) if order.error else None)
        return None, "order_unmatched"

    actual_price = order.avg_price or price
    # Slippage pesimista para dry-run: el CLOB simulado nos devuelve el mid,
    # pero un fill real en BUY pagaría más. Ajustamos para que el PnL
    # proyectado sea realista. Solo aplica si el trade fue realmente dry.
    if LIVE_DRY_RUN:
        actual_price = _apply_dry_slippage("BUY", actual_price)
    actual_shares = order.filled_size or (size_usdc / actual_price if actual_price > 0 else 0)
    # Si ajustamos el precio post-fill, recalculamos shares para mantener
    # consistencia size_usdc = shares * price (size_usdc es lo que "gastamos").
    if LIVE_DRY_RUN and actual_price > 0:
        actual_shares = size_usdc / actual_price

    market_slug = (raw or {}).get("slug") or (raw or {}).get("eventSlug")
    with tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO live_trades
                (source_wallet, source_trade_id, condition_id, token_id, outcome,
                 outcome_index, side, entry_price, entry_size_usdc, entry_shares,
                 entry_at, entry_order_id, entry_tx_hash, status, raw, asset, dry_run,
                 peak_price)
            VALUES (?, ?, ?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                source_wallet, source_trade_id, condition_id, token_id, outcome,
                outcome_index, actual_price, size_usdc, actual_shares,
                timestamp, order.order_id, order.tx_hash,
                json.dumps(raw, separators=(",", ":")) if raw else None,
                token_id,
                1 if LIVE_DRY_RUN else 0,
                actual_price,  # peak_price arranca == entry_price
            ),
        )
        live_id = cur.lastrowid

    try:
        from src.copybot.notifier import live_open
        live_open(
            source_wallet=source_wallet, market_slug=market_slug,
            size_usdc=size_usdc, price=actual_price,
            order_id=order.order_id, tx_hash=order.tx_hash,
            dry_run=LIVE_DRY_RUN,
        )
    except Exception:
        pass
    return live_id, None


def _settle_pnl_shares(entry_price: float, shares: float, exit_price: float) -> float:
    """PnL en USDC dadas las shares ejecutadas."""
    if shares <= EPSILON:
        return 0.0
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
    """Cierra la posición FIFO matcheando (wallet, cid, outcome) con orden SELL real."""
    from src.polymarket.clob_client import place_market_order

    with tx() as conn:
        row = conn.execute(
            """
            SELECT id, entry_price, entry_shares, token_id, dry_run
            FROM live_trades
            WHERE source_wallet=? AND condition_id=? AND outcome_index=? AND status='open'
            ORDER BY entry_at ASC LIMIT 1
            """,
            (source_wallet, condition_id, outcome_index),
        ).fetchone()
        if not row:
            return None
        trade_id = row["id"]
        entry_price = row["entry_price"]
        shares = row["entry_shares"]
        token_id = row["token_id"]
        is_dry = bool(row["dry_run"])

    # SELL: notional aprox para el wrapper (que internamente convierte a shares)
    sell_size_usdc = shares * price
    order = place_market_order(
        token_id=token_id,
        side="SELL",
        size_usdc=sell_size_usdc,
        price=price,
        dry_run=is_dry or LIVE_DRY_RUN,
        condition_id=condition_id,
    )
    if not order.ok:
        log.warning("SELL no matcheada para live_trade #%d: %s", trade_id, order.error)
        return None

    actual_exit_price = order.avg_price or price
    # Slippage pesimista en dry-run: SELL recibe menos que el mid.
    if is_dry or LIVE_DRY_RUN:
        actual_exit_price = _apply_dry_slippage("SELL", actual_exit_price)
    actual_exit_shares = order.filled_size or shares
    gross_pnl = _settle_pnl_shares(entry_price, actual_exit_shares, actual_exit_price)
    fee_usdc, _gas, net_pnl = post_close_costs(gross_pnl)
    status = "closed_win" if net_pnl > 0 else "closed_loss"

    with tx() as conn:
        conn.execute(
            """
            UPDATE live_trades
            SET exit_price=?, exit_at=?, exit_order_id=?, exit_tx_hash=?,
                exit_shares=?, fees_usdc=?, pnl_usdc=?, status=?, exit_reason=?
            WHERE id=?
            """,
            (actual_exit_price, timestamp, order.order_id, order.tx_hash,
             actual_exit_shares, fee_usdc, net_pnl, status, reason, trade_id),
        )
        accum = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdc),0) as a FROM live_trades "
            "WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss')"
        ).fetchone()["a"]
        slug_row = conn.execute(
            "SELECT slug FROM markets WHERE condition_id=?", (condition_id,)
        ).fetchone()
        market_slug = slug_row["slug"] if slug_row else None

    try:
        from src.copybot.notifier import live_close
        live_close(
            source_wallet=source_wallet, market_slug=market_slug,
            pnl_usdc=net_pnl, accumulated=accum,
            exit_reason=reason, tx_hash=order.tx_hash, dry_run=is_dry,
        )
    except Exception:
        pass
    return trade_id


def force_close(live_trade_id: int, exit_price: float, *, reason: str) -> None:
    """Fuerza el cierre (stop-loss / take-profit) con orden SELL real."""
    from src.polymarket.clob_client import place_market_order

    with tx() as conn:
        row = conn.execute(
            """
            SELECT entry_price, entry_shares, token_id, dry_run, status,
                   source_wallet, condition_id
            FROM live_trades WHERE id=?
            """,
            (live_trade_id,),
        ).fetchone()
        if not row or row["status"] != "open":
            return
        entry_price = row["entry_price"]
        shares = row["entry_shares"]
        token_id = row["token_id"]
        is_dry = bool(row["dry_run"])
        source_wallet = row["source_wallet"]
        condition_id = row["condition_id"]

    sell_size_usdc = shares * exit_price
    order = place_market_order(
        token_id=token_id, side="SELL", size_usdc=sell_size_usdc,
        price=exit_price, dry_run=is_dry or LIVE_DRY_RUN,
        condition_id=condition_id,
    )
    if not order.ok:
        log.warning("force_close SELL no matcheada para live #%d: %s",
                    live_trade_id, order.error)
        return

    actual_price = order.avg_price or exit_price
    # Slippage pesimista en dry-run: SELL recibe menos que el mid.
    if is_dry or LIVE_DRY_RUN:
        actual_price = _apply_dry_slippage("SELL", actual_price)
    actual_shares = order.filled_size or shares
    gross = _settle_pnl_shares(entry_price, actual_shares, actual_price)
    fee, _gas, net = post_close_costs(gross)
    status = "closed_win" if net > 0 else "closed_loss"

    with tx() as conn:
        conn.execute(
            """
            UPDATE live_trades
            SET exit_price=?, exit_at=strftime('%s','now'), exit_order_id=?,
                exit_tx_hash=?, exit_shares=?, fees_usdc=?, pnl_usdc=?,
                status=?, exit_reason=?
            WHERE id=?
            """,
            (actual_price, order.order_id, order.tx_hash, actual_shares,
             fee, net, status, reason, live_trade_id),
        )
        accum = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdc),0) as a FROM live_trades "
            "WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss')"
        ).fetchone()["a"]
        slug_row = conn.execute(
            "SELECT slug FROM markets WHERE condition_id=?", (condition_id,)
        ).fetchone()
        market_slug = slug_row["slug"] if slug_row else None

    try:
        from src.copybot.notifier import live_close
        live_close(
            source_wallet=source_wallet, market_slug=market_slug,
            pnl_usdc=net, accumulated=accum,
            exit_reason=reason, tx_hash=order.tx_hash, dry_run=is_dry,
        )
    except Exception:
        pass


def settle_resolved() -> int:
    """Para mercados resueltos: marca live_trades como settled.

    NO redime las shares automáticamente — eso requiere llamar al contrato
    CTF (ConditionalTokensFramework). Por ahora dejamos al usuario hacer
    el "Redeem" manual desde polymarket.com (toma 1 click).

    Marca el trade como `settled_win` o `settled_loss` con el payout teórico.
    """
    settled = 0
    with db() as conn:
        rows = conn.execute(
            """
            SELECT lt.id, lt.entry_price, lt.entry_shares, lt.outcome_index,
                   lt.source_wallet, lt.condition_id, lt.dry_run,
                   m.outcome_prices, m.slug
            FROM live_trades lt
            JOIN markets m ON m.condition_id = lt.condition_id
            WHERE lt.status='open' AND m.closed=1
            """,
        ).fetchall()

    to_settle = []
    notif_payload = []  # (source_wallet, slug, net, dry_run) por trade settleado
    for r in rows:
        try:
            prices = json.loads(r["outcome_prices"] or "[]")
            payout = float(prices[r["outcome_index"]]) if r["outcome_index"] is not None else 0
        except Exception:
            continue
        gross = (r["entry_shares"] or 0) * (payout - r["entry_price"])
        fee, _gas, net = post_close_costs(gross)
        status = "settled_win" if net > 0 else "settled_loss"
        to_settle.append((payout, fee, net, status, r["id"]))
        notif_payload.append((
            r["source_wallet"], r["slug"], net, bool(r["dry_run"]),
        ))

    if not to_settle:
        return 0

    with tx() as conn:
        conn.executemany(
            """
            UPDATE live_trades
            SET exit_price=?, exit_at=strftime('%s','now'),
                fees_usdc=?, pnl_usdc=?, status=?, exit_reason='market_resolved'
            WHERE id=?
            """,
            to_settle,
        )
        accum = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdc),0) as a FROM live_trades "
            "WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss')"
        ).fetchone()["a"]
    settled = len(to_settle)

    try:
        from src.copybot.notifier import live_close
        for src_wallet, slug, net, is_dry in notif_payload:
            live_close(
                source_wallet=src_wallet, market_slug=slug,
                pnl_usdc=net, accumulated=accum,
                exit_reason="market_resolved", tx_hash=None, dry_run=is_dry,
            )
    except Exception:
        pass

    log.info("settled %d live_trades (recordá hacer Redeem en polymarket.com)", settled)
    return settled
