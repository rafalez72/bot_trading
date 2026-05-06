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
from src.config import DB_PATH
from src.copybot.learning import on_paper_trade_closed
from src.copybot.paper import EPSILON, _check_kill_switch, _ensure_market_stub
from src.copybot.realism import expected_net_pnl, post_close_costs
from src.db.schema import db, tx

log = logging.getLogger(__name__)


# Outbox para INSERTs de live_trades que fallaron persistentemente
# (database is locked tras retries). Se drena al startup del runner. Garantiza
# que un fill on-chain real nunca se pierda solo porque la DB estaba locked.
LIVE_OUTBOX_PATH = DB_PATH.parent / "live_trades_outbox.jsonl"


def _insert_live_trade_row(payload: dict) -> int | None:
    """Inserta el row en live_trades dentro de tx(). Idempotente por source_trade_id.

    Devuelve el live_trade.id si insertó, None si ya existía (duplicado).
    Cualquier sqlite OperationalError ('locked') propaga al caller para retry.
    """
    with tx() as conn:
        # Idempotencia: si ya existe, no duplicar (puede pasar si el outbox
        # se drena después de que el reconciler ya creó el row).
        if payload.get("source_trade_id"):
            dup = conn.execute(
                "SELECT id FROM live_trades WHERE source_trade_id=?",
                (payload["source_trade_id"],),
            ).fetchone()
            if dup:
                return None
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
                payload["source_wallet"],
                payload["source_trade_id"],
                payload["condition_id"],
                payload["token_id"],
                payload.get("outcome"),
                payload.get("outcome_index"),
                payload["entry_price"],
                payload["entry_size_usdc"],
                payload["entry_shares"],
                payload["entry_at"],
                payload.get("entry_order_id"),
                payload.get("entry_tx_hash"),
                payload.get("raw"),
                payload.get("asset"),
                payload.get("dry_run", 0),
                payload.get("peak_price", payload["entry_price"]),
            ),
        )
        return cur.lastrowid


def _persist_live_trade_with_outbox(payload: dict) -> int | None:
    """Inserta el row, con retries y fallback a outbox si la DB sigue locked.

    Retries: 5 intentos exponenciales (0.1, 0.2, 0.4, 0.8, 1.6s) — además de
    los 30s de busy_timeout que ya hace SQLite. Total ~33s peor caso.
    Si todo falla, escribe el payload a LIVE_OUTBOX_PATH (jsonl) y devuelve None.
    El runner drenará el outbox en el próximo startup.
    """
    import sqlite3 as _sq
    last_err: Exception | None = None
    delay = 0.1
    for attempt in range(5):
        try:
            return _insert_live_trade_row(payload)
        except _sq.OperationalError as e:
            if "locked" not in str(e).lower():
                # otros errores no son retryables — al outbox directo
                last_err = e
                break
            last_err = e
            log.warning(
                "live_trades INSERT locked (attempt %d/5): %s",
                attempt + 1, e,
            )
            time.sleep(delay)
            delay *= 2
        except Exception as e:
            # cualquier otra excepción: al outbox para análisis posterior,
            # no perdemos el trade
            last_err = e
            break

    # Fallback: escribir al outbox. Best-effort: si esto también falla,
    # al menos lo logueamos.
    try:
        LIVE_OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LIVE_OUTBOX_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "queued_at": int(time.time()),
                "last_error": str(last_err)[:300] if last_err else None,
                "payload": payload,
            }, separators=(",", ":")) + "\n")
        log.error(
            "live_trades INSERT falló persistente — escrito a outbox %s "
            "(source_trade_id=%s tx_hash=%s). Se drenará al próximo startup.",
            LIVE_OUTBOX_PATH, payload.get("source_trade_id"),
            payload.get("entry_tx_hash"),
        )
        try:
            from src.copybot.notifier import send
            send(
                f"⚠️ *OUTBOX*: live_trade INSERT falló — encolado a disco. "
                f"wallet=`{payload.get('source_wallet','?')[:10]}` "
                f"tx=`{(payload.get('entry_tx_hash') or 'no-tx')[:14]}`"
            )
        except Exception:
            pass
    except Exception as e:
        log.exception(
            "OUTBOX FAIL — no se pudo escribir el trade ni a DB ni a disco: %s",
            e,
        )
    return None


def drain_live_outbox() -> int:
    """Drena LIVE_OUTBOX_PATH al startup. Idempotente (usa source_trade_id).

    Lee el JSONL línea por línea, intenta insertar cada payload con
    `_insert_live_trade_row`. Las líneas que se insertan correctamente (o que
    eran duplicados ya en DB) se descartan. Las que fallan otra vez quedan
    en un nuevo outbox para reintento.

    Devuelve cantidad de trades drenados (insertados o ya-presentes).
    """
    if not LIVE_OUTBOX_PATH.exists():
        return 0
    try:
        with open(LIVE_OUTBOX_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        log.warning("drain_live_outbox: no se pudo leer %s: %s", LIVE_OUTBOX_PATH, e)
        return 0

    drained = 0
    failed: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if not payload:
                # línea corrupta — no la reencolamos (evita loop infinito)
                continue
            try:
                _insert_live_trade_row(payload)
                drained += 1
            except Exception as e:
                log.warning(
                    "drain_live_outbox: INSERT aún falla (source_trade_id=%s): %s",
                    payload.get("source_trade_id"), e,
                )
                failed.append(line)
        except json.JSONDecodeError:
            # línea corrupta — descartar
            continue

    # Reescribir outbox solo con los que siguen fallando (atomico-ish: escribimos
    # a un .tmp y renombramos).
    try:
        if failed:
            tmp = LIVE_OUTBOX_PATH.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(failed) + "\n")
            tmp.replace(LIVE_OUTBOX_PATH)
        else:
            LIVE_OUTBOX_PATH.unlink()
    except Exception as e:
        log.warning("drain_live_outbox: no se pudo limpiar outbox: %s", e)

    if drained:
        log.warning(
            "drain_live_outbox: %d trades drenados al startup (de %d en outbox)",
            drained, len(lines),
        )
        try:
            from src.copybot.notifier import send
            send(f"📤 *OUTBOX drenado*: {drained} live_trades recuperados al startup.")
        except Exception:
            pass
    return drained


def _log_reject(source_wallet, condition_id, outcome_index, side, price, reason, detail=None):
    """Registra un reject en live_rejects para observabilidad.

    Best-effort: nunca debe romper el flujo principal. Retry hasta 5 veces
    si la DB está locked (WAL+busy_timeout=30s normalmente cubre, pero hay
    edge cases con 3 runners paralelos donde no es suficiente).
    """
    import sqlite3 as _sq
    last_err = None
    for attempt in range(5):
        try:
            with tx() as conn:
                conn.execute(
                    "INSERT INTO live_rejects (at, source_wallet, condition_id, outcome_index, side, price, reason, detail) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (int(time.time()), source_wallet, condition_id, outcome_index, side, price, reason, detail),
                )
            return
        except _sq.OperationalError as e:
            if "locked" not in str(e).lower():
                log.warning("failed to log reject: %s", e)
                return
            last_err = e
            time.sleep(0.5 * (attempt + 1))
        except Exception as e:
            log.warning("failed to log reject: %s", e)
            return
    log.warning("failed to log reject after 5 retries: %s", last_err)


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

    # Anti-stale: ver paper.open_position. Mismo umbral compartido.
    from src.copybot.paper import MAX_TRADE_AGE_SECONDS
    age = int(time.time()) - int(timestamp or 0)
    if age > MAX_TRADE_AGE_SECONDS:
        _log_reject(source_wallet, condition_id, outcome_index, "BUY", price,
                    "stale_trade", detail=json.dumps({"age_s": age}))
        return None, "stale_trade"

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
    # CRÍTICO: el fill ya está on-chain. Si este INSERT falla, perdemos
    # tracking de una posición real → no SL/TP → reconciler la rescata como
    # 'RECONCILED' sin atribución al wallet. Para evitarlo: retries
    # exponenciales y, si todo falla, encolamos a disco (outbox) que el
    # runner drena al próximo startup.
    insert_payload = {
        "source_wallet": source_wallet,
        "source_trade_id": source_trade_id,
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": outcome,
        "outcome_index": outcome_index,
        "entry_price": actual_price,
        "entry_size_usdc": size_usdc,
        "entry_shares": actual_shares,
        "entry_at": timestamp,
        "entry_order_id": order.order_id,
        "entry_tx_hash": order.tx_hash,
        "raw": json.dumps(raw, separators=(",", ":")) if raw else None,
        "asset": token_id,
        "dry_run": 1 if LIVE_DRY_RUN else 0,
        "peak_price": actual_price,  # peak_price arranca == entry_price
    }
    live_id = _persist_live_trade_with_outbox(insert_payload)

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


def cleanup_phantom_positions(min_age_seconds: int = 7200) -> int:
    """Detecta `live_trades` open cuyas posiciones ya no existen on-chain.

    Causa raíz: para markets negRisk (esports/MLB/btc-updown-5m) el bot
    no indexa la tabla `markets` con `closed=1`/`outcome_prices`, así que
    `settle_resolved()` nunca los settle. Cuando esos markets resuelven y
    el usuario redime (o se settle automáticamente para outcomes 0), el bot
    queda con rows phantom en estado 'open' que ocupan el cap del bot.

    Solución: consulta `data-api/positions` para `POLYMARKET_FUNDER_ADDRESS`
    (la wallet del bot). Construye un set de `asset` (token_id) on-chain.
    Para cada live_trade open con `entry_at` >= `min_age_seconds` atrás:
        si su `token_id` (o `asset`) NO está en ese set → marca como
        `closed_external` con `pnl_usdc=0`, `exit_reason='phantom_cleanup'`.

    Solo aplica a trades > min_age (default 2h) para evitar race con trades
    recién abiertos que aún no aparecen en /positions.

    Devuelve cantidad de phantoms limpiados. Si la API falla, devuelve 0
    (no toca nada — failsafe contra falsos positivos por error de red).
    """
    # IMPORTANTE: leer del config (que ya hace dotenv-load), NO via os.getenv —
    # las vars de .env están montadas como archivo, no exportadas al shell del
    # container. Bug detectado en deploy 2026-05-06.
    from src.config import POLYMARKET_FUNDER_ADDRESS as funder
    if not funder:
        return 0

    cutoff_ts = int(time.time()) - max(min_age_seconds, 0)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, source_wallet, condition_id, token_id, asset, entry_at,
                   entry_price, entry_size_usdc
            FROM live_trades
            WHERE status='open' AND dry_run=0 AND entry_at <= ?
            """,
            (cutoff_ts,),
        ).fetchall()
    if not rows:
        return 0

    # Fetch on-chain positions (sync — usamos httpx en sync mode acá porque
    # esta función la llama el runner sync).
    try:
        import httpx as _httpx
        from src.config import DATA_API
        r = _httpx.get(
            f"{DATA_API}/positions",
            params={"user": funder.lower()},
            timeout=20,
        )
        if r.status_code != 200:
            log.warning(
                "cleanup_phantom: data-api /positions %d %s",
                r.status_code, (r.text or "")[:120],
            )
            return 0
        positions = r.json() or []
    except Exception as e:
        log.warning("cleanup_phantom: fetch positions falló: %s", e)
        return 0

    if not isinstance(positions, list):
        log.warning("cleanup_phantom: /positions devolvió no-list (%r)", type(positions))
        return 0

    # Defensa: si la API responde lista vacía Y tenemos muchos rows en DB,
    # podría ser bug temporal de la API (no querer borrar todo de golpe).
    # Threshold razonable: si la respuesta es [], procedemos solo si la
    # cantidad a limpiar es "razonable" (< 50). Más que eso parece error.
    if not positions and len(rows) > 50:
        log.warning(
            "cleanup_phantom: /positions vacío y %d trades open en DB — "
            "salto cleanup por seguridad",
            len(rows),
        )
        return 0

    onchain_assets: set[str] = set()
    for p in positions:
        if not isinstance(p, dict):
            continue
        # /positions del Data API expone el ERC1155 token id en 'asset'
        a = p.get("asset")
        if isinstance(a, str) and a:
            onchain_assets.add(a)
        # algunos endpoints exponen 'tokenId'
        t = p.get("tokenId")
        if isinstance(t, str) and t:
            onchain_assets.add(t)

    phantom_ids: list[int] = []
    for r in rows:
        # Token id en nuestro DB puede estar en token_id o en asset.
        cand = (r["token_id"], r["asset"])
        present = any(c and c in onchain_assets for c in cand)
        if not present:
            phantom_ids.append(r["id"])

    if not phantom_ids:
        return 0

    now_ts = int(time.time())
    with tx() as conn:
        placeholders = ",".join("?" * len(phantom_ids))
        conn.execute(
            f"""
            UPDATE live_trades
            SET status='closed_external',
                exit_at=?,
                exit_price=entry_price,
                exit_shares=entry_shares,
                pnl_usdc=0,
                exit_reason='phantom_cleanup'
            WHERE id IN ({placeholders}) AND status='open'
            """,
            [now_ts] + phantom_ids,
        )
        conn.execute(
            """
            INSERT INTO learning_events
                (wallet, event_type, before_value, after_value, delta, trigger, metric_snapshot)
            VALUES ('(system)', 'phantom_cleanup', NULL, NULL, NULL, ?, ?)
            """,
            (
                f"Cleanup auto: {len(phantom_ids)} live_trades open ya no existen on-chain",
                json.dumps({"ids": phantom_ids, "onchain_count": len(onchain_assets)}),
            ),
        )

    log.warning(
        "cleanup_phantom: marcados %d live_trades como closed_external "
        "(no existen en /positions del proxy). on-chain assets count=%d",
        len(phantom_ids), len(onchain_assets),
    )
    try:
        from src.copybot.notifier import send
        send(
            f"🧹 *Phantom cleanup*: {len(phantom_ids)} live_trades marcados como "
            f"closed_external (ya no existen on-chain). Cap del bot liberado."
        )
    except Exception:
        pass
    return len(phantom_ids)
