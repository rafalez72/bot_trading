"""Bridge entre el WS de Polymarket RTDS y el tradebook del copybot.

Reduce latencia de detección de trades de wallets copiados desde el polling
de Data API (~5-7s + ~250ms proxy Vercel) a <1s con un push streaming.

Diseño:
- ``ws_run_loop()`` corre como task en paralelo al polling existente, NO lo
  reemplaza. El polling sigue siendo la fuente de verdad de fallback: si el
  WS pierde un trade (raro), el polling lo recoge en el siguiente ciclo.
- La idempotencia en ``tradebook.open_position`` (guard por
  ``source_trade_id``) impide que el mismo evento se duplique cuando el WS
  re-entrega tras un reconnect. Para evitar colisiones cruzadas con el id
  generado por el polling (``txh:asset:side:oi``), el WS usa el prefijo
  ``ws:<transactionHash>``.
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
from typing import Any

from src.copybot.tradebook import (
    MODE as TRADEBOOK_MODE,
    close_position,
    open_position,
)
from src.db.schema import db, tx
from src.polymarket.websocket import PolymarketTradesWS

log = logging.getLogger(__name__)

# Cada cuánto refrescamos la lista de wallets activas (sin reconectar el WS).
WALLET_REFRESH_S = 60.0


def _active_wallets() -> list[str]:
    """Lee wallets ``status='active'`` de copy_subscriptions."""
    with db() as conn:
        rows = conn.execute(
            "SELECT wallet FROM copy_subscriptions WHERE status='active'"
        ).fetchall()
    return [r["wallet"] for r in rows]


def _set_cursor(wallet: str, ts: int) -> None:
    """Avanza ``paper_cursor:<wallet>`` al timestamp dado (idempotente).

    Uso ``MAX(value, ts)`` para no retroceder el cursor si el polling ya lo
    avanzó más adelante (orden de mensajes WS no garantizado en absoluto).
    """
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO index_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = CAST(MAX(CAST(index_state.value AS INTEGER), CAST(excluded.value AS INTEGER)) AS TEXT),
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
                return

            try:
                price = float(payload.get("price") or 0)
            except (TypeError, ValueError):
                return
            try:
                ts = int(ts_raw) if ts_raw is not None else 0
            except (TypeError, ValueError):
                return
            if ts <= 0:
                return

            # source_trade_id determinístico para WS — distinto del polling
            # (el polling genera ``<txh>:<asset>:<side>:<oi>`` vía _trade_id).
            # Si el WS re-entrega tras reconnect, el guard duplicate de
            # tradebook.open_position por source_trade_id corta el segundo.
            source_trade_id = f"ws:{txh}"

            if side == "BUY":
                pid, reason = open_position(
                    source_wallet=wallet_lc,
                    source_trade_id=source_trade_id,
                    condition_id=cid,
                    outcome=payload.get("outcome"),
                    outcome_index=oi,
                    price=price,
                    timestamp=ts,
                    raw=payload,
                )
                if pid:
                    log.info(
                        "WS BUY  %s  cid=%s..  oi=%s  px=%.3f  → %s #%d",
                        wallet_lc[:10], (cid or "")[:10], oi, price,
                        TRADEBOOK_MODE, pid,
                    )
                elif reason and reason != "duplicate":
                    log.debug(
                        "WS skip BUY  %s  cid=%s..  → %s",
                        wallet_lc[:10], (cid or "")[:10], reason,
                    )
            else:  # SELL
                pid = close_position(
                    source_wallet=wallet_lc,
                    condition_id=cid,
                    outcome_index=oi,
                    price=price,
                    timestamp=ts,
                )
                if pid:
                    log.info(
                        "WS SELL %s  cid=%s..  oi=%s  px=%.3f  → %s #%d",
                        wallet_lc[:10], (cid or "")[:10], oi, price,
                        TRADEBOOK_MODE, pid,
                    )

            # Avanzar el cursor del polling para que el polling no reprocese
            # el mismo trade en el próximo ciclo (mismo schema que runner).
            try:
                _set_cursor(wallet_lc, ts)
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
