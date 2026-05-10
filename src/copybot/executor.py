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
    LIVE_MIN_EXPECTED_PNL_USDC,
    MAX_ENTRIES_PER_WALLET_MARKET,
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


# _MONTH_MAP y _parse_slug_expiry movidos a _slug_expiry.py para que paper.py
# también pueda usarlos sin import circular. Re-exportados acá por compat.
from src.copybot._slug_expiry import parse_slug_expiry as _parse_slug_expiry  # noqa: F401, E402


def _apply_dry_slippage(side: str, price: float) -> float:
    """Aplica slippage pesimista al precio simulado de un dry-run.

    NOTA (2026-05-10): post `place_market_order(dry_run=True)` ahora consulta
    el orderbook REAL (estimate_slippage) y devuelve VWAP realista. Esta
    función agrega un colchón adicional pequeño (LIVE_DRY_SLIPPAGE_PCT) sobre
    ese VWAP. Conservador por design — el dry-run ahora es ligeramente más
    pesimista que el live, lo que es aceptable: preferimos descubrir
    estrategias rentables en dry-run que ver pérdidas en live.

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
    """Aplica los mismos pre-open checks que paper.open_position.

    2026-05-10 refactor: la lógica de filtros vive en
    src.copybot.validation.run_pre_open_checks (single source of truth).
    Lo único que cambia entre paper/live es el `TradeValidationContext`
    (tabla destino, capital, threshold de PnL esperado, log_reject hook).

    Devuelve ((size_usdc, market_row, category), None) si pasa, o
    (None, reject_reason) si rechaza.
    """
    from src.copybot.validation import (
        TradeValidationContext,
        run_pre_open_checks,
    )

    def _log(reason: str, detail=None) -> None:
        _log_reject(
            source_wallet, condition_id, outcome_index, "BUY", price, reason,
            detail=json.dumps(detail) if detail else None,
        )

    ctx = TradeValidationContext(
        source_wallet=source_wallet,
        source_trade_id=source_trade_id,
        condition_id=condition_id,
        outcome_index=outcome_index,
        price=price,
        timestamp=timestamp,
        raw=raw,
        trades_table="live_trades",
        capital_usdc=LIVE_CAPITAL_USDC,
        base_usdc=LIVE_BASE_USDC,
        min_expected_pnl_usdc=LIVE_MIN_EXPECTED_PNL_USDC,
        log_reject=_log,
    )
    return run_pre_open_checks(conn, ctx)


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
    """Abre un live_trade ejecutando una orden BUY real en el CLOB.

    `our_entry_at` y `our_entry_price` (Feature G — copy-lag telemetry):
    timestamp y mid local en el momento que procesamos. ws_bridge los
    pasa siempre. Aceptamos en la firma para paridad con paper.open_position.
    Si la columna existe en live_trades los persistimos; sino se ignoran
    (back-compat con installs viejos).
    """
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

    # Guard adicional: en LIVE real, una orden ok=True debe tener al menos
    # un tx_hash o filled_size > 0. Si no, es un "phantom success" del SDK
    # (response degenerada) — NO insertar live_trade zombi: queda en estado
    # 'open' con entry_tx_hash=NULL y risk.sweep_stops loopearía intentando
    # cerrarlo. Devolvemos como rechazo para que el reconciler decida si
    # luego aparece on-chain.
    if not LIVE_DRY_RUN:
        has_evidence = bool(order.tx_hash) or (order.filled_size or 0) > 0
        if not has_evidence:
            log.warning(
                "BUY ok=True sin evidencia de fill (tx_hash=%r filled=%r) — "
                "NO inserto live_trade. cid=%s.. price=%.3f",
                order.tx_hash, order.filled_size, condition_id[:10], price,
            )
            _log_reject(
                source_wallet, condition_id, outcome_index, "BUY", price,
                "phantom_ok_no_fill",
                detail=json.dumps({"order_id": order.order_id, "status": order.status}),
            )
            return None, "phantom_ok_no_fill"

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
        # PnL acumulado respeta bot_state.pnl_reset_at (mismo mecanismo que
        # learning.on_paper_trade_closed). Si nunca se reseteó → suma todo.
        _reset_row = conn.execute(
            "SELECT value FROM bot_state WHERE key='pnl_reset_at'"
        ).fetchone()
        try:
            _reset_at = int(float((_reset_row["value"] if _reset_row else "0") or "0"))
        except (TypeError, ValueError):
            _reset_at = 0
        accum = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdc),0) as a FROM live_trades "
            "WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss') "
            "AND COALESCE(exit_at, 0) >= ?",
            (_reset_at,),
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
        # PnL acumulado respeta bot_state.pnl_reset_at (mismo mecanismo que
        # learning.on_paper_trade_closed). Si nunca se reseteó → suma todo.
        _reset_row = conn.execute(
            "SELECT value FROM bot_state WHERE key='pnl_reset_at'"
        ).fetchone()
        try:
            _reset_at = int(float((_reset_row["value"] if _reset_row else "0") or "0"))
        except (TypeError, ValueError):
            _reset_at = 0
        accum = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdc),0) as a FROM live_trades "
            "WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss') "
            "AND COALESCE(exit_at, 0) >= ?",
            (_reset_at,),
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
        # Incluye waiting_settlement además de open: trades que sweep_stops
        # marcó como esperando settlement (market closed/slug expirado/
        # orderbook stale) deben ser settleados normalmente cuando aparezca
        # outcome_prices en la tabla markets.
        rows = conn.execute(
            """
            SELECT lt.id, lt.entry_price, lt.entry_shares, lt.outcome_index,
                   lt.source_wallet, lt.condition_id, lt.dry_run,
                   m.outcome_prices, m.slug
            FROM live_trades lt
            JOIN markets m ON m.condition_id = lt.condition_id
            WHERE lt.status IN ('open', 'waiting_settlement') AND m.closed=1
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
        # PnL acumulado respeta bot_state.pnl_reset_at (mismo mecanismo que
        # learning.on_paper_trade_closed). Si nunca se reseteó → suma todo.
        _reset_row = conn.execute(
            "SELECT value FROM bot_state WHERE key='pnl_reset_at'"
        ).fetchone()
        try:
            _reset_at = int(float((_reset_row["value"] if _reset_row else "0") or "0"))
        except (TypeError, ValueError):
            _reset_at = 0
        accum = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdc),0) as a FROM live_trades "
            "WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss') "
            "AND COALESCE(exit_at, 0) >= ?",
            (_reset_at,),
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
        # Incluye waiting_settlement además de open: trades parqueados por
        # sweep_stops también pueden quedar phantom on-chain (settled/redeemed
        # mientras el bot estaba apagado, etc.) y deben ser limpiados.
        rows = conn.execute(
            """
            SELECT id, source_wallet, condition_id, token_id, asset, entry_at,
                   entry_price, entry_size_usdc
            FROM live_trades
            WHERE status IN ('open', 'waiting_settlement')
              AND dry_run=0 AND entry_at <= ?
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

    # Snapshot per-trade ANTES del UPDATE para tener datos para Telegram
    # (size, slug, wallet). Hacemos una sola query batch.
    phantom_details: list[dict] = []
    with db() as conn:
        placeholders_q = ",".join("?" * len(phantom_ids))
        det_rows = conn.execute(
            f"""
            SELECT lt.id, lt.entry_size_usdc, lt.source_wallet, lt.condition_id,
                   m.slug
            FROM live_trades lt
            LEFT JOIN markets m ON m.condition_id = lt.condition_id
            WHERE lt.id IN ({placeholders_q})
            """,
            phantom_ids,
        ).fetchall()
        for r in det_rows:
            phantom_details.append({
                "id": r["id"],
                "size_usdc": float(r["entry_size_usdc"] or 0),
                "source_wallet": r["source_wallet"],
                "slug": r["slug"],
            })

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
            WHERE id IN ({placeholders}) AND status IN ('open', 'waiting_settlement')
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

    # CRÍTICO (2026-05-10): NOTIF Telegram por cada phantom. Antes esto era
    # silente y el usuario perdió $76 sin enterarse de 29/50 trades. Para
    # bursts grandes (>5 phantoms) mandamos un solo summary; para pocos,
    # individual para que el user pueda ir a verificarlos en polymarket.com.
    try:
        from src.copybot.notifier import live_phantom
        if len(phantom_details) <= 5:
            for d in phantom_details:
                try:
                    live_phantom(
                        trade_id=d["id"],
                        size_usdc=d["size_usdc"],
                        market_slug=d["slug"],
                        source_wallet=d["source_wallet"],
                    )
                except Exception:
                    pass
        else:
            # batch summary: total capital atado
            total_size = sum(d["size_usdc"] for d in phantom_details)
            try:
                live_phantom(
                    n_phantoms=len(phantom_details),
                    size_usdc=total_size,
                )
            except Exception:
                pass
    except Exception:
        pass

    return len(phantom_ids)
