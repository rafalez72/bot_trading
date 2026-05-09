"""Bridge entre el WS de Polymarket RTDS y el tradebook del copybot.

Reduce latencia de detección de trades de wallets copiados desde el polling
de Data API (~5-7s + ~250ms proxy Vercel) a <1s con un push streaming.

Diseño:
- ``ws_run_loop()`` corre como task en paralelo al polling existente, NO lo
  reemplaza. El polling sigue siendo la fuente de verdad de fallback: si el
  WS pierde un trade (raro), el polling lo recoge en el siguiente ciclo.
- La idempotencia en ``tradebook.open_position`` (guard por
  ``source_trade_id``) impide que el mismo evento se duplique. El WS
  genera el MISMO id que el polling (``<txh>:<asset>:<side>:<oi>`` vía
  ``indexer.trades._trade_id``) — si usaran prefijos distintos el guard
  NO los reconocería como duplicados y abriríamos doble posición ante
  cualquier race entre polling y WS.
- Cada 60s refrescamos la lista de wallets activas vía
  ``PolymarketTradesWS.update_watched`` para reflejar selección/drops sin
  reconectar.
- Cursors ``paper_cursor:<wallet>`` se avanzan al ``timestamp`` del trade
  procesado (mismo schema que el polling), de forma que ambos caminos
  convergen sobre el mismo estado.
- Compartimos el patrón ``tx()`` del runner — ``tx()`` ya envuelve
  ``_retry_locked`` para tolerar el SQLite BUSY ocasional.

Activación: ``WEBSOCKET_TRADES_ENABLED=true`` en .env (default: false).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.copybot.tradebook import (
    MODE as TRADEBOOK_MODE,
    close_position,
    open_position,
)
from src.copybot.ws_metrics import metrics as ws_metrics
from src.db.schema import db, tx
from src.indexer.trades import _trade_id as _make_trade_id
from src.polymarket.websocket import PolymarketTradesWS

log = logging.getLogger(__name__)

# Cada cuánto refrescamos la lista de wallets activas (sin reconectar el WS).
WALLET_REFRESH_S = 60.0


def _fetch_clob_mid(asset: str | None) -> float | None:
    """Lookup best-effort del midpoint actual del CLOB por token_id.

    Devuelve None si no hay asset, falla la red, o el endpoint dice 404.
    Timeout corto (1s) para NO bloquear burst de trades — el call-site corre
    en `asyncio.to_thread` así que el event loop no se bloquea, pero igual
    queremos cortar rápido cuando el CLOB está caído. Si no se puede cotizar,
    `our_entry_price` queda None y el análisis lo ignora.
    """
    if not asset:
        return None
    try:
        import httpx
        from src.config import CLOB_API
        r = httpx.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": asset},
            timeout=1.0,
        )
        if r.status_code != 200:
            return None
        mid = r.json().get("mid")
        return float(mid) if mid is not None else None
    except Exception:
        return None


def _active_wallets() -> list[str]:
    """Lee wallets ``status='active'`` de copy_subscriptions."""
    with db() as conn:
        rows = conn.execute(
            "SELECT wallet FROM copy_subscriptions WHERE status='active'"
        ).fetchall()
    return [r["wallet"] for r in rows]


def _set_cursor(wallet: str, ts: int) -> None:
    """Avanza ``paper_cursor:<wallet>`` al timestamp dado (idempotente).

    Comparamos via CASE WHEN — funciona en SQLite y Postgres. Antes usábamos
    ``MAX(a, b)`` que es 2-arg scalar function en SQLite pero NO existe en
    PG (PG usa GREATEST). El CASE evita el problema sin necesidad de
    traducción runtime.
    """
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO index_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = CASE
                    WHEN CAST(excluded.value AS BIGINT) > CAST(index_state.value AS BIGINT)
                    THEN excluded.value
                    ELSE index_state.value
                END,
                updated_at = datetime('now')
            """,
            (f"paper_cursor:{wallet}", str(ts)),
        )


def _make_handle_trade(active_wallets_lc: set[str]):
    """Genera el callback con closure sobre el set actual de wallets activas.

    El set se mutates in-place desde ``ws_run_loop`` cuando refrescamos.
    """

    async def _handle_trade(payload: dict[str, Any]) -> None:
        try:
            wallet = payload.get("proxyWallet")
            if not isinstance(wallet, str):
                return
            wallet_lc = wallet.lower()

            # Defensivo: validar contra el set actual aunque el WS ya filtró.
            if wallet_lc not in active_wallets_lc:
                return

            side = (payload.get("side") or "").upper()
            cid = payload.get("conditionId")
            oi = payload.get("outcomeIndex")
            txh = payload.get("transactionHash")
            ts_raw = payload.get("timestamp")

            if not cid or side not in ("BUY", "SELL") or not txh:
                ws_metrics.on_bad_payload()
                return

            raw_price = payload.get("price")
            if raw_price is None:
                # Payload corrupto sin precio — no copiar ni avanzar cursor.
                ws_metrics.on_bad_payload()
                return
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                ws_metrics.on_bad_payload()
                return
            try:
                ts = int(ts_raw) if ts_raw is not None else 0
            except (TypeError, ValueError):
                ws_metrics.on_bad_payload()
                return
            if ts <= 0:
                ws_metrics.on_bad_payload()
                return
            # RTDS a veces manda timestamp en ms (13 dígitos) y a veces en s.
            # Normalizamos a segundos. Sin esto, _set_cursor escribe ms en
            # index_state y CAST(value AS INTEGER) en learning.py overflow
            # (INTEGER de PG es 32-bit, max ~2.1B; ms son ~1.7e12).
            if ts > 9_999_999_999:
                ts //= 1000

            # source_trade_id IDÉNTICO al que genera el polling — reusamos
            # _trade_id(payload) de indexer/trades para garantizar paridad
            # bit-a-bit. Si los IDs divergen (incluso en casos sutiles como
            # outcomeIndex=None vs vacío) el guard de duplicate de
            # open_position no funciona y se abren posiciones dobles.
            source_trade_id = _make_trade_id(payload)
            if source_trade_id is None:
                # Sin transactionHash _trade_id devuelve None; nunca debería
                # pasar acá porque ya validamos `txh` arriba, pero defensa.
                return

            # CRÍTICO (fix 2026-05-06): `open_position`, `close_position` y
            # `_set_cursor` son SYNC y hacen DB writes + HTTP calls al CLOB
            # (varios segundos por orden real). Llamarlos directamente desde
            # el callback async BLOQUEA el event loop. Cuando llegan varios
            # matches consecutivos, las tareas async (polling main, HL, DX,
            # heartbeat WS) se quedan sin CPU → cursores no avanzan, DB
            # locked, el bot deja de operar.
            #
            # Solución: `asyncio.to_thread` offloads cada llamada a un thread
            # pool. El event loop queda libre. El thread pool default de
            # asyncio (~32 workers) maneja bursts sin problema.
            if side == "BUY":
                # Feature G: copy-lag telemetry. Capturamos OUR ts y mid en
                # el momento de procesar, no los del source. Mid via CLOB
                # /midpoint con timeout 1s; si falla, our_entry_price queda
                # None y solo registramos el lag temporal.
                our_at = int(time.time())
                our_mid = await asyncio.to_thread(
                    _fetch_clob_mid, payload.get("asset")
                )
                pid, reason = await asyncio.to_thread(
                    open_position,
                    source_wallet=wallet_lc,
                    source_trade_id=source_trade_id,
                    condition_id=cid,
                    outcome=payload.get("outcome"),
                    outcome_index=oi,
                    price=price,
                    timestamp=ts,
                    raw=payload,
                    our_entry_at=our_at,
                    our_entry_price=our_mid,
                )
                if pid:
                    ws_metrics.on_open_buy()
                    log.info(
                        "ws.buy_opened wallet=%s cid=%s.. oi=%s px=%.3f mode=%s pid=%d",
                        wallet_lc[:10], (cid or "")[:10], oi, price,
                        TRADEBOOK_MODE, pid,
                    )
                else:
                    ws_metrics.on_skip(reason)
                    if reason and reason != "duplicate":
                        log.info(
                            "ws.buy_skipped wallet=%s cid=%s.. reason=%s",
                            wallet_lc[:10], (cid or "")[:10], reason,
                        )
            else:  # SELL
                pid = await asyncio.to_thread(
                    close_position,
                    source_wallet=wallet_lc,
                    condition_id=cid,
                    outcome_index=oi,
                    price=price,
                    timestamp=ts,
                )
                if pid:
                    ws_metrics.on_close_sell()
                    log.info(
                        "ws.sell_closed wallet=%s cid=%s.. oi=%s px=%.3f mode=%s pid=%d",
                        wallet_lc[:10], (cid or "")[:10], oi, price,
                        TRADEBOOK_MODE, pid,
                    )

            # Avanzar el cursor del polling para que el polling no reprocese
            # el mismo trade en el próximo ciclo (mismo schema que runner).
            try:
                await asyncio.to_thread(_set_cursor, wallet_lc, ts)
            except Exception:
                log.exception("WS: no se pudo avanzar cursor de %s", wallet_lc[:10])
        except Exception:
            # Defensa-en-profundidad: el WS ya tiene try/except alrededor del
            # callback, pero ante una excepción no queremos dejar que se
            # propague y rompa el loop interno.
            log.exception("WS _handle_trade falló para payload=%r", payload)

    return _handle_trade


async def ws_run_loop() -> None:
    """Loop principal del bridge WS: arranca el cliente RTDS + refresh task.

    Corre indefinidamente. Cancelable desde el caller (asyncio.Task.cancel).
    """
    wallets = _active_wallets()
    active_lc: set[str] = {w.lower() for w in wallets if w}
    log.info("ws_bridge: arrancando con %d wallet(s) activas", len(active_lc))

    # Arranca el thread que persiste el snapshot al archivo compartido.
    # El server (otro contenedor) lee desde ahí para /api/ws-status.
    ws_metrics.start_persist_thread(interval_s=5.0)
    ws_metrics.set_watched(len(active_lc))

    handle_trade = _make_handle_trade(active_lc)
    client = PolymarketTradesWS(active_lc, handle_trade)

    async def _refresher() -> None:
        """Cada WALLET_REFRESH_S, sincroniza el set watched con la DB."""
        while True:
            try:
                await asyncio.sleep(WALLET_REFRESH_S)
                fresh = {w.lower() for w in _active_wallets() if w}
                if fresh != active_lc:
                    added = fresh - active_lc
                    removed = active_lc - fresh
                    active_lc.clear()
                    active_lc.update(fresh)
                    client.update_watched(active_lc)
                    log.info(
                        "ws_bridge: wallets refrescadas (+%d -%d, total=%d)",
                        len(added), len(removed), len(active_lc),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ws_bridge: refresher iteration falló")

    refresher_task = asyncio.create_task(_refresher(), name="ws_bridge-refresher")
    try:
        await client.run()
    finally:
        client.stop()
        refresher_task.cancel()
        try:
            await refresher_task
        except (asyncio.CancelledError, Exception):
            pass
        log.info("ws_bridge: terminado")
