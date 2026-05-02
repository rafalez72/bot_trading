"""Shadow tracker: pollea wallets dropped y registra sus trades.

Razón: cuando dropeamos un wallet (por inactividad/performance/etc), perdemos
visibilidad de qué hubieran hecho. Si en una semana descubrimos que un wallet
dropped por inactividad reapareció y tradeó bien, sabemos que el threshold
fue muy agresivo.

Flujo:
  - Cada N horas, lista wallets con status='dropped' en copy_subscriptions
  - Para cada uno, fetch /trades?user=<wallet>&limit=50 via PolymarketClient
  - Inserta cada trade nuevo en shadow_trades (UNIQUE on trade_id)
  - NO se hace nada operativo — pura observación

Análisis después: query SQL que junta shadow_trades con dropped wallets,
ver volumen + posibles PnL.
"""
from __future__ import annotations

import logging
import time

from src.db.schema import db, tx
from src.indexer.trades import _trade_id
from src.polymarket.client import PolymarketClient

log = logging.getLogger(__name__)


async def shadow_poll_dropped(client: PolymarketClient | None = None) -> int:
    """Pollea wallets dropped, registra trades en shadow_trades.

    Devuelve cantidad total de shadow trades insertados (nuevos).
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT cs.wallet, cs.reason
            FROM copy_subscriptions cs
            WHERE cs.status='dropped'
            """
        ).fetchall()

    if not rows:
        return 0

    own_client = client is None
    if own_client:
        client = PolymarketClient()
        await client.__aenter__()

    inserted = 0
    try:
        for r in rows:
            wallet = r["wallet"]
            drop_reason = r["reason"] or "unknown"
            try:
                trades = await client.trades(user=wallet, limit=50, offset=0)
            except Exception as e:
                log.debug("shadow poll %s falló: %s", wallet[:10], e)
                continue

            with tx() as conn:
                for t in trades:
                    tid = _trade_id(t)
                    if not tid:
                        continue
                    try:
                        ts = int(t.get("timestamp") or 0)
                        if ts <= 0:
                            continue
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO shadow_trades
                                (wallet, drop_reason, trade_id, timestamp,
                                 condition_id, slug, side, outcome_index, price, size_usdc)
                            VALUES (?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                wallet, drop_reason, tid, ts,
                                t.get("conditionId"),
                                t.get("slug") or t.get("eventSlug"),
                                (t.get("side") or "").upper(),
                                t.get("outcomeIndex"),
                                float(t.get("price") or 0),
                                float(t.get("size") or t.get("sizeInTokens") or 0),
                            ),
                        )
                        inserted += conn.total_changes
                    except Exception as e:
                        log.debug("shadow insert err %s: %s", tid, e)
    finally:
        if own_client:
            await client.__aexit__(None, None, None)

    if inserted:
        log.info("shadow_tracker: +%d trades observados de %d wallets dropped",
                 inserted, len(rows))
    return inserted


def summary() -> dict:
    """Resumen para análisis manual: qué wallets dropped tradearon más
    desde su drop. Llamar SQL directo, devuelve top 20."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.wallet, s.drop_reason,
                   COUNT(*) as n_trades,
                   MIN(s.timestamp) as first_post_drop,
                   MAX(s.timestamp) as last_post_drop,
                   SUM(s.price * s.size_usdc) as estimated_volume
            FROM shadow_trades s
            JOIN copy_subscriptions cs ON cs.wallet = s.wallet
            WHERE cs.status = 'dropped'
              AND (cs.stopped_at IS NULL OR s.timestamp >= strftime('%s', cs.stopped_at))
            GROUP BY s.wallet
            ORDER BY n_trades DESC
            LIMIT 20
            """
        ).fetchall()
    return [dict(r) for r in rows]
