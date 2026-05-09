"""Shadow watch: observa wallets candidatas pre-promoción vía RTDS WS.

Razón: la pipeline de discovery + selector tarda días en backfilear y promover
una wallet a ``copy_subscriptions.status='active'``. Mientras tanto perdemos
visibilidad sobre cómo performean en *tiempo real* las candidatas que ya están
en ``trader_metrics`` con perfil prometedor pero todavía no copiadas.

Solución (nivel observación, sin riesgo):
- ``select_shadow_candidates`` devuelve hasta N wallets de ``trader_metrics``
  que pasan un filtro relajado y NO están siendo copiadas activamente.
- El bridge WS (``src/copybot/ws_bridge.py``) suscribe la unión
  *active_copy ∪ shadow* al RTDS. Al matchear una wallet en el set shadow,
  llama a ``record_shadow_trade`` que solo *inserta* en ``shadow_trades``
  con ``mode='pre_promote_watch'`` — NO abre paper_trade, NO ejecuta orden.

Diferencia con ``shadow_tracker.py`` (legacy):
- ``shadow_tracker`` observa wallets DROPPED (post-drop), polling HTTP cada
  N horas, ``mode='post_drop'``.
- ``shadow_watch`` observa wallets CANDIDATAS (pre-promote), streaming WS,
  ``mode='pre_promote_watch'``.

Ambos usan la misma tabla ``shadow_trades`` distinguidos por la columna
``mode`` agregada en la migration `_MIGRATIONS` de schema.py.

Filtro relajado (más permisivo que el de copy):
- ``realized_pnl_usdc > 50``  (vs >150 para copy)
- ``total_trades > 30``       (vs >50)
- ``win_rate > 0.50``          (vs >0.55)
- ``last_trade_ts >= now - 7d`` (activa última semana)
- NO en ``copy_subscriptions WHERE status='active'``

Top-N por ``total_volume_usdc DESC`` para priorizar wallets con flujo —
una wallet con mucho volumen genera más eventos shadow que validan/rechazan
su perfil más rápido.
"""
from __future__ import annotations

import logging
import time

from src.db.schema import db, tx
from src.indexer.trades import _trade_id

log = logging.getLogger(__name__)

# Ventana de actividad para considerar una wallet "viva". Si su último trade
# es de hace >7d, ni el WS la va a ver tradear ni nos sirve de muestra.
ACTIVITY_WINDOW_S = 7 * 86400

# Filtro relajado — el objetivo es OBSERVAR, no copiar.
MIN_PNL_USDC = 50.0
MIN_TRADES = 30
MIN_WIN_RATE = 0.50

SHADOW_MODE = "pre_promote_watch"


def select_shadow_candidates(limit: int = 200, *, now: int | None = None) -> list[str]:
    """Devuelve wallets candidatas para shadow watch (no copiadas, activas, perfil decente).

    Args:
        limit: cantidad máxima de wallets a devolver. El bridge WS maneja
            su propio cap absoluto contra la capacidad RTDS.
        now: timestamp UNIX para el filtro de actividad. Default ``time.time()``;
            inyectable para tests.

    Returns:
        Lista de wallets en *minúsculas* ordenadas por ``total_volume_usdc DESC``
        (top por flujo), de longitud ≤ ``limit``. La normalización a lower-case
        es coherente con cómo ``ws_bridge`` matchea contra ``proxyWallet.lower()``.
    """
    if limit <= 0:
        return []

    cutoff = (int(now) if now is not None else int(time.time())) - ACTIVITY_WINDOW_S

    # Subquery: wallets actualmente copiadas (status='active'). Excluimos esas
    # — las shadow son las NO copiadas (paused/dropped/never seen).
    # ``LEFT JOIN ... IS NULL`` en lugar de ``NOT IN`` evita el corner case de
    # NULLs en copy_subscriptions y también es más eficiente con índice en
    # copy_subscriptions(status).
    sql = """
        SELECT tm.wallet
        FROM trader_metrics tm
        LEFT JOIN copy_subscriptions cs
               ON cs.wallet = tm.wallet
              AND cs.status = 'active'
        WHERE cs.wallet IS NULL
          AND tm.realized_pnl_usdc > ?
          AND tm.total_trades > ?
          AND tm.win_rate > ?
          AND tm.last_trade_ts >= ?
        ORDER BY tm.total_volume_usdc DESC
        LIMIT ?
    """
    with db() as conn:
        rows = conn.execute(
            sql,
            (MIN_PNL_USDC, MIN_TRADES, MIN_WIN_RATE, cutoff, int(limit)),
        ).fetchall()

    out: list[str] = []
    for r in rows:
        w = r["wallet"]
        if isinstance(w, str) and w:
            out.append(w.lower())
    return out


def record_shadow_trade(payload: dict) -> bool:
    """Inserta un trade observado de wallet shadow en ``shadow_trades`` (idempotente).

    NO abre paper_trade, NO arma posición, NO toca tradebook. Solo persiste
    el evento crudo para análisis posterior.

    Args:
        payload: payload del WS RTDS (mismo schema que polling Data API).

    Returns:
        True si insertó una fila nueva, False si ya existía o el payload era
        inválido. Errores de DB se loggean a debug y devuelven False.
    """
    tid = _trade_id(payload)
    if not tid:
        return False

    wallet = payload.get("proxyWallet")
    if not isinstance(wallet, str) or not wallet:
        return False

    try:
        ts = int(payload.get("timestamp") or 0)
    except (TypeError, ValueError):
        return False
    if ts <= 0:
        return False
    # Normalizar ms→s (igual que ws_bridge).
    if ts > 9_999_999_999:
        ts //= 1000

    side = (payload.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return False

    try:
        price = float(payload.get("price") or 0)
        size = float(payload.get("size") or payload.get("sizeInTokens") or 0)
    except (TypeError, ValueError):
        return False

    try:
        with tx() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO shadow_trades
                    (wallet, drop_reason, trade_id, timestamp,
                     condition_id, slug, side, outcome_index,
                     price, size_usdc, mode)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    wallet.lower(),
                    None,  # drop_reason no aplica a pre_promote_watch
                    tid,
                    ts,
                    payload.get("conditionId"),
                    payload.get("slug") or payload.get("eventSlug"),
                    side,
                    payload.get("outcomeIndex"),
                    price,
                    size,
                    SHADOW_MODE,
                ),
            )
            inserted = (cur.rowcount or 0) > 0
    except Exception as e:
        log.debug("shadow_watch insert err tid=%s: %s", tid, e)
        return False

    return inserted
