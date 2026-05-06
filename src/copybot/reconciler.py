"""Reconciliador de trades fantasma (post-2026-05-05).

Problema: el bot ejecutaba BUYs on-chain pero a veces no creaba el row en
`live_trades` (database is locked, exception en path de error donde sí
filleó, etc). Resultado: posiciones reales sin tracking → sin SL/TP →
pérdidas catastróficas.

Solución: cada N minutos comparar los trades on-chain del proxy wallet
(via Polymarket Data API) contra `live_trades` y auto-INSERTar las que
faltan. Así, aunque haya un bug en el path de write, la reconciliación
recupera el tracking en máximo N minutos.

Uso:
    from src.copybot.reconciler import reconcile_once
    summary = await reconcile_once()
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import httpx

from src.config import POLYMARKET_FUNDER_ADDRESS
from src.db.schema import db, tx

log = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
RECONCILE_WINDOW_HOURS = 24  # mira los últimos N horas


def _fetch_proxy_trades(limit: int = 200) -> list[dict]:
    """Trae los trades del proxy desde la Data API (públicos)."""
    if not POLYMARKET_FUNDER_ADDRESS:
        return []
    try:
        r = httpx.get(
            f"{DATA_API}/trades",
            params={"user": POLYMARKET_FUNDER_ADDRESS, "limit": limit},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("reconciler: data-api %d %s", r.status_code, r.text[:120])
            return []
        return r.json() or []
    except Exception as e:
        log.warning("reconciler: fetch falló: %s", e)
        return []


def _existing_tx_hashes(since_ts: int) -> set[str]:
    """Tx hashes de trades del bot ya conocidos en la DB."""
    out: set[str] = set()
    with db() as conn:
        rows = conn.execute(
            "SELECT entry_tx_hash, exit_tx_hash FROM live_trades WHERE entry_at >= ?",
            (since_ts,),
        ).fetchall()
    for r in rows:
        for h in (r["entry_tx_hash"], r["exit_tx_hash"]):
            if h:
                out.add(h.lower())
    return out


def _existing_keys(since_ts: int) -> set[tuple]:
    """Set de (cid, outcome_index, entry_at) para detectar duplicados sin tx_hash."""
    out: set[tuple] = set()
    with db() as conn:
        rows = conn.execute(
            "SELECT condition_id, outcome_index, entry_at FROM live_trades WHERE entry_at >= ?",
            (since_ts,),
        ).fetchall()
    for r in rows:
        out.add((r["condition_id"], r["outcome_index"], r["entry_at"]))
    return out


# Ventana (segundos) para matchear el fill on-chain del proxy contra un BUY
# del wallet original que se estaba copiando. El bot tarda ~10-30s en
# tomar la decisión + mandar la orden + matchearla. ±60s cubre el caso
# normal con holgura sin abrir la puerta a falsos positivos cuando varias
# wallets compran el mismo outcome casi al mismo tiempo.
RECONCILE_ATTRIBUTION_WINDOW_SEC = 60


def _find_source_wallet_for_fill(conn, cid: str, oi: int, ts: int) -> Optional[str]:
    """Busca el wallet copiado que probablemente originó este fill on-chain.

    Cruza contra `trades` (que indexa trades de TODOS los wallets monitoreados,
    no solo del proxy del bot). Match: mismo condition_id + outcome_index +
    side=BUY, timestamp dentro de [ts-window, ts] (la copia llega DESPUÉS
    del trade original; permitimos un pequeño margen +window por skew de clock).

    Solo retorna el wallet si está en `copy_subscriptions` con status active
    o paused (era una sub legítima en algún momento). Si hay múltiples
    candidatos, prefiere el más cercano en tiempo (más probable que sea
    el que disparó la copia).

    Devuelve el wallet (lowercase) o None si no hay match confiable.
    """
    if not cid or oi is None or ts <= 0:
        return None
    lo = ts - RECONCILE_ATTRIBUTION_WINDOW_SEC
    hi = ts + RECONCILE_ATTRIBUTION_WINDOW_SEC
    try:
        rows = conn.execute(
            """
            SELECT t.wallet, t.timestamp
            FROM trades t
            JOIN copy_subscriptions cs ON cs.wallet = t.wallet
            WHERE t.condition_id = ?
              AND t.outcome_index = ?
              AND UPPER(t.side) = 'BUY'
              AND t.timestamp BETWEEN ? AND ?
              AND cs.status IN ('active', 'paused')
            ORDER BY ABS(t.timestamp - ?) ASC
            LIMIT 1
            """,
            (cid, oi, lo, hi, ts),
        ).fetchall()
    except Exception as e:
        log.debug("reconciler: attribution lookup falló: %s", e)
        return None
    if not rows:
        return None
    return rows[0]["wallet"]


def _reconcile_buy(t: dict) -> Optional[int]:
    """Crea un row 'reconciled' en live_trades para un BUY on-chain detectado.

    Devuelve live_trade.id si insertó, None si era duplicado.

    Atribución mejorada (2026-05-05): antes de marcar como RECONCILED genérico,
    intenta encontrar el wallet original que se estaba copiando cruzando contra
    la tabla `trades` (timestamp ±60s, mismo cid+outcome+side, sub activa/paused).
    Si lo encuentra, conserva la atribución para que learning/bandit/categories
    no queden ciegos. Si no, fallback a 'RECONCILED' como antes.
    """
    cid = t.get("conditionId")
    oi = t.get("outcomeIndex")
    ts = int(t.get("timestamp") or 0)
    price = float(t.get("price") or 0)
    size = float(t.get("size") or 0)
    tx_hash = (t.get("transactionHash") or "").lower() or None
    asset = t.get("asset")
    outcome = t.get("outcome")
    if not cid or oi is None or ts == 0 or price <= 0 or size <= 0:
        return None

    size_usdc = size * price

    with tx() as conn:
        # Doble check: ¿ya existe por tx_hash o key (cid, oi, ts)?
        if tx_hash:
            r = conn.execute(
                "SELECT id FROM live_trades WHERE entry_tx_hash=? OR exit_tx_hash=?",
                (tx_hash, tx_hash),
            ).fetchone()
            if r:
                return None
        r = conn.execute(
            "SELECT id FROM live_trades WHERE condition_id=? AND outcome_index=? "
            "AND ABS(entry_at - ?) < 5",
            (cid, oi, ts),
        ).fetchone()
        if r:
            return None

        # Intento de atribución al wallet original copiado.
        attributed = _find_source_wallet_for_fill(conn, cid, oi, ts)
        if attributed:
            source_wallet = attributed
            source_trade_id = f"reconciled-attr:{tx_hash or cid+':'+str(ts)}"
            raw_blob = {
                "reconciled": True,
                "attributed_to": attributed,
                "raw_data_api": t,
            }
            log.info(
                "reconciler: fill cid=%s.. atribuido a wallet=%s.. (ts=%d)",
                cid[:12], attributed[:10], ts,
            )
        else:
            source_wallet = "RECONCILED"
            source_trade_id = f"reconciled:{tx_hash or cid+':'+str(ts)}"
            raw_blob = {"reconciled": True, "raw_data_api": t}

        cur = conn.execute(
            """
            INSERT INTO live_trades
                (source_wallet, source_trade_id, condition_id, token_id, outcome,
                 outcome_index, side, entry_price, entry_size_usdc, entry_shares,
                 entry_at, entry_order_id, entry_tx_hash, status, raw, asset, dry_run,
                 peak_price, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, 'open', ?, ?, 0, ?, NULL)
            """,
            (
                source_wallet,
                source_trade_id,
                cid, asset, outcome, oi,
                price, size_usdc, size,
                ts, None, tx_hash,
                json.dumps(raw_blob, separators=(",", ":")),
                asset,
                price,
            ),
        )
        return cur.lastrowid


def reconcile_once() -> dict:
    """Una pasada de reconciliación. Devuelve summary."""
    now = int(time.time())
    since = now - RECONCILE_WINDOW_HOURS * 3600

    trades = _fetch_proxy_trades(limit=200)
    existing_hashes = _existing_tx_hashes(since)

    inserted = 0
    skipped_dup = 0
    skipped_old = 0
    skipped_sell = 0

    for t in trades:
        ts = int(t.get("timestamp") or 0)
        if ts < since:
            skipped_old += 1
            continue
        side = (t.get("side") or "").upper()
        if side != "BUY":
            skipped_sell += 1
            continue
        tx_hash = (t.get("transactionHash") or "").lower()
        if tx_hash and tx_hash in existing_hashes:
            skipped_dup += 1
            continue
        try:
            new_id = _reconcile_buy(t)
            if new_id:
                inserted += 1
                log.warning(
                    "reconciler: trade fantasma trackeado #%d cid=%s.. price=%.4f size=%.0f",
                    new_id, (t.get("conditionId") or "?")[:12], float(t.get("price") or 0),
                    float(t.get("size") or 0),
                )
            else:
                skipped_dup += 1
        except Exception as e:
            log.exception("reconciler: error procesando trade: %s", e)

    summary = {
        "ts": now,
        "n_total": len(trades),
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "skipped_old": skipped_old,
        "skipped_sell": skipped_sell,
    }
    if inserted > 0:
        log.warning("reconciler: %d trades fantasma trackeados", inserted)
        try:
            from src.copybot.notifier import send
            send(f"⚠️ *RECONCILIADO*: {inserted} trades fantasma encontrados on-chain "
                 f"y trackeados ahora. Revisar dashboard.")
        except Exception:
            pass
    return summary
