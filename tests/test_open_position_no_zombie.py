"""Tests guarantee live_trades INSERT only happens after the order is confirmed.

Background (2026-05-10): the user described seeing live_trades rows with
tx_hash=NULL after the disastrous LIVE session — a hallmark of orders being
inserted before the SDK call completes. The flow was already structurally
correct (INSERT after `order.ok` check), but didn't guard against EXCEPTIONS
from `place_market_order`.

Fix: open_position now wraps the call in try/except. Any exception logs a
'order_exception' reject and returns immediately, leaving live_trades clean.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from src.copybot import executor
from src.db.schema import db


def _seed_subscription(
    wallet: str = "0xtrader",
    sizing_mult: float = 1.0,
    status: str = "active",
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO copy_subscriptions (wallet, status, sizing_mult)
            VALUES (?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET status=excluded.status,
                                              sizing_mult=excluded.sizing_mult
            """,
            (wallet, status, sizing_mult),
        )


def test_open_position_does_not_insert_when_place_order_raises(isolated_db):
    """Si place_market_order tira excepción, NO debe haber row en live_trades.

    Antes del fix: el outbox de live_trades retry-and-queue podía generar un
    INSERT con tx_hash=NULL si el wrapper crasheaba mid-call. Ahora cualquier
    excepción causa un rechazo limpio sin tocar live_trades.
    """
    _seed_subscription()

    with db() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM live_trades").fetchone()["n"]

    with patch(
        "src.polymarket.clob_client.place_market_order",
        side_effect=RuntimeError("upstream SDK kaboom"),
    ), patch(
        "src.polymarket.clob_client.get_token_id",
        return_value="tok-mock-12345",
    ):
        live_id, reject = executor.open_position(
            source_wallet="0xtrader",
            source_trade_id="trade-exc-1",
            condition_id="0xcid",
            outcome="YES",
            outcome_index=0,
            price=0.347,
            timestamp=int(time.time()),
            raw={"asset": "tok-mock-12345"},
        )

    # Caller debe ver el rechazo
    assert live_id is None
    assert reject == "order_exception"

    # Y no debe haber ningún row nuevo en live_trades
    with db() as conn:
        after = conn.execute("SELECT COUNT(*) AS n FROM live_trades").fetchone()["n"]
    assert after == before, (
        f"Esperaba 0 inserts en live_trades; antes={before} despues={after}. "
        "Si esto falla, una excepción del wrapper deja un trade zombi."
    )


def test_open_position_logs_reject_on_exception(isolated_db):
    """Una excepción en place_market_order debe persistir un row en
    live_rejects con reason='order_exception' para diagnóstico.
    """
    _seed_subscription()

    with patch(
        "src.polymarket.clob_client.place_market_order",
        side_effect=ValueError("invalid signature"),
    ), patch(
        "src.polymarket.clob_client.get_token_id",
        return_value="tok-mock-67890",
    ):
        executor.open_position(
            source_wallet="0xtrader",
            source_trade_id="trade-exc-2",
            condition_id="0xcid",
            outcome="YES",
            outcome_index=0,
            price=0.50,
            timestamp=int(time.time()),
            raw={"asset": "tok-mock-67890"},
        )

    with db() as conn:
        rejects = conn.execute(
            "SELECT reason FROM live_rejects WHERE source_wallet=? "
            "AND condition_id=?",
            ("0xtrader", "0xcid"),
        ).fetchall()
    reasons = [r["reason"] for r in rejects]
    assert "order_exception" in reasons, (
        f"Esperaba reason='order_exception' en live_rejects, got {reasons}."
    )
