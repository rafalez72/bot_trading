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
from src.copybot import shadow_watch
from src.copybot.ws_metrics import metrics as ws_metrics
from src.db.schema import db, tx
from src.indexer.trades import _trade_id as _make_trade_id
from src.polymarket.websocket import PolymarketTradesWS

log = logging.getLogger(__name__)

# Cada cuánto refrescamos la lista de wallets activas (sin reconectar el WS).
WALLET_REFRESH_S = 60.0

# Cap absoluto de wallets watched por conexión RTDS. La spec upstream no es
# explícita pero observamos rejects con sets >300. Capeamos a 250 con margen
# para evitar disconnects durante un refresh ruidoso (active+shadow).
MAX_WATCHED_PER_CONN = 250


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


def _active_and_shadow_wallets(shadow_limit: int) -> tuple[set[str], set[str]]:
    """Devuelve (active_lc, shadow_lc) — sets *disjuntos* de wallets en lower-case.

    El shadow set excluye explícitamente las wallets ya activas, así una
    misma wallet nunca cae en ambas ramas del callback. ``select_shadow_candidates``
    ya filtra ``status='active'``, pero por defensa-en-profundidad reaplicamos
    la diferencia acá: en un race entre el INSERT a copy_subscriptions y la
    query del shadow, garantizar disjuntos a nivel set.
    """
    active_lc: set[str] = {w.lower() for w in _active_wallets() if w}
    shadow_lc: set[str] = set()
    if shadow_limit > 0:
        try:
            shadow_lc = set(shadow_watch.select_shadow_candidates(limit=shadow_limit))
        except Exception:
            log.exception("ws_bridge: select_shadow_candidates falló (ignoro shadow)")
            shadow_lc = set()
    # Garantizar disjoint: copy gana sobre shadow.
    shadow_lc -= active_lc
    return active_lc, shadow_lc


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


def _make_handle_trade(
    active_wallets_lc: set[str],
    shadow_wallets_lc: set[str] | None = None,
):
    """Genera el callback con closure sobre el set actual de wallets watched.

    Hay dos sets DISJUNTOS:
    - ``active_wallets_lc``: wallets en ``copy_subscriptions.status='active'``;
      sus trades activan el flujo completo (open_position → tradebook → ejecutor).
    - ``shadow_wallets_lc``: wallets candidatas (``shadow_watch.select_shadow_candidates``);
      sus trades SOLO se persisten en ``shadow_trades`` con ``mode='pre_promote_watch'``.
      NO se abre paper_trade, NO se ejecuta orden, NO dispara learning.

    Ambos sets se mutate in-place desde ``ws_run_loop`` al refrescar (cada
    ``WALLET_REFRESH_S``). Si una wallet shadow es promovida a active entre
    refreshes, el match cae al branch de "wallet desconocida" hasta el próximo
    refresh — comportamiento aceptable y conservador (preferimos perder un
    trade del primer refresh que duplicar entre branches).
    """
    if shadow_wallets_lc is None:
        shadow_wallets_lc = set()

    async def _handle_trade(payload: dict[str, Any]) -> None:
        try:
            wallet = payload.get("proxyWallet")
            if not isinstance(wallet, str):
                return
            wallet_lc = wallet.lower()

            # Routing: branch por set. Si no está en NINGUNO, ignoramos.
            in_active = wallet_lc in active_wallets_lc
            in_shadow = (not in_active) and (wallet_lc in shadow_wallets_lc)
            if not in_active and not in_shadow:
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

            # Shadow branch: NO copia, solo persiste para análisis posterior.
            # Importante: usamos un payload con timestamp ya normalizado a
            # segundos para que ``_trade_id`` interno + el ``ts`` que guarda
            # ``record_shadow_trade`` queden coherentes con el resto del bot.
            if in_shadow:
                shadow_payload = dict(payload)
                shadow_payload["timestamp"] = ts
                inserted = await asyncio.to_thread(
                    shadow_watch.record_shadow_trade, shadow_payload
                )
                if inserted:
                    log.info(
                        "shadow_watch: wallet=%s cid=%s.. side=%s price=%.3f",
                        wallet_lc[:10], (cid or "")[:10], side, price,
                    )
                # Avanzar cursor igual que en el path activo: si más adelante
                # promovemos esta wallet a active, el polling no reprocesa
                # trades viejos que el WS ya observó.
                try:
                    await asyncio.to_thread(_set_cursor, wallet_lc, ts)
                except Exception:
                    log.exception(
                        "WS shadow: no se pudo avanzar cursor de %s",
                        wallet_lc[:10],
                    )
                return  # CRÍTICO: NO caer al flujo de copy.

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
                    ws_metrics.on_skip(reason, wallet=wallet_lc, cid=cid)
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


def _capped_union(active_lc: set[str], shadow_lc: set[str]) -> set[str]:
    """Une active+shadow respetando ``MAX_WATCHED_PER_CONN``.

    Las activas tienen prioridad ABSOLUTA: NUNCA se sacrifican por shadow.
    Si la unión excede el cap, recortamos *solo del shadow set*. Si las
    activas SOLAS exceden el cap (caso raro: >250 wallets copiadas), avisamos
    en WARN pero las pasamos enteras — perder copias-vivas por un cap
    arbitrario sería peor que un disconnect sospechoso.
    """
    if len(active_lc) >= MAX_WATCHED_PER_CONN:
        if len(active_lc) > MAX_WATCHED_PER_CONN:
            log.warning(
                "ws_bridge: %d wallets activas exceden cap RTDS=%d — "
                "pasamos todas igual (shadow=0)",
                len(active_lc), MAX_WATCHED_PER_CONN,
            )
        return set(active_lc)

    budget = MAX_WATCHED_PER_CONN - len(active_lc)
    if len(shadow_lc) <= budget:
        return active_lc | shadow_lc

    log.warning(
        "ws_bridge: union active(%d)+shadow(%d) excede cap=%d, "
        "shadow recortado a %d",
        len(active_lc), len(shadow_lc), MAX_WATCHED_PER_CONN, budget,
    )
    # Recorte determinístico: orden lex-asc para reproducibilidad. La
    # selección por `select_shadow_candidates` ya viene ordenada por
    # volumen DESC, pero al pasar por set() se desordena. Para no perder
    # las top-N de volumen necesitaríamos preservar lista; el cap solo
    # entra en juego cuando shadow_limit + active >250, raro en práctica.
    return active_lc | set(sorted(shadow_lc)[:budget])


async def ws_run_loop() -> None:
    """Loop principal del bridge WS: arranca el cliente RTDS + refresh task.

    Watched wallets = active_copy ∪ shadow_candidates (capped a
    ``MAX_WATCHED_PER_CONN``). El callback ``_handle_trade`` enruta cada
    match al flujo correspondiente (copy vs shadow) según a qué set pertenece.

    Corre indefinidamente. Cancelable desde el caller (asyncio.Task.cancel).
    """
    # Lazy import: SHADOW_WATCH_* viven en config.py y otros agents tocan
    # ese módulo. No queremos que un import-time error de config
    # rompa el módulo entero — solo el feature.
    try:
        from src.config import SHADOW_WATCH_ENABLED, SHADOW_WATCH_LIMIT
    except ImportError:
        SHADOW_WATCH_ENABLED = False
        SHADOW_WATCH_LIMIT = 0

    shadow_limit = int(SHADOW_WATCH_LIMIT) if SHADOW_WATCH_ENABLED else 0

    active_lc, shadow_lc = _active_and_shadow_wallets(shadow_limit)
    watched = _capped_union(active_lc, shadow_lc)
    log.info(
        "ws_bridge: arrancando con %d activas + %d shadow = %d watched (cap=%d)",
        len(active_lc), len(shadow_lc), len(watched), MAX_WATCHED_PER_CONN,
    )

    # Arranca el thread que persiste el snapshot al archivo compartido.
    # El server (otro contenedor) lee desde ahí para /api/ws-status.
    ws_metrics.start_persist_thread(interval_s=5.0)
    ws_metrics.set_watched(len(watched))

    handle_trade = _make_handle_trade(active_lc, shadow_lc)
    client = PolymarketTradesWS(watched, handle_trade)

    async def _refresher() -> None:
        """Cada WALLET_REFRESH_S, sincroniza ambos sets con la DB."""
        while True:
            try:
                await asyncio.sleep(WALLET_REFRESH_S)
                fresh_active, fresh_shadow = _active_and_shadow_wallets(shadow_limit)
                if fresh_active != active_lc or fresh_shadow != shadow_lc:
                    added_a = fresh_active - active_lc
                    removed_a = active_lc - fresh_active
                    added_s = fresh_shadow - shadow_lc
                    removed_s = shadow_lc - fresh_shadow
                    # Mutamos in-place — el closure de _make_handle_trade
                    # tiene referencia a estos sets, así que reasignar
                    # active_lc=... en este scope NO se vería allá.
                    active_lc.clear()
                    active_lc.update(fresh_active)
                    shadow_lc.clear()
                    shadow_lc.update(fresh_shadow)
                    new_watched = _capped_union(active_lc, shadow_lc)
                    client.update_watched(new_watched)
                    log.info(
                        "ws_bridge: refresh active(+%d -%d=%d) shadow(+%d -%d=%d) watched=%d",
                        len(added_a), len(removed_a), len(active_lc),
                        len(added_s), len(removed_s), len(shadow_lc),
                        len(new_watched),
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
