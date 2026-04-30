"""Tests for src/copybot/learning.py — auto_drop_by_rejects.

We exercise the public function with the same isolated-DB fixture used in
the rest of the suite. The bot defaults to paper mode, so the active
trades table is `paper_trades` and our "0 fills" check reads from that.
"""
from __future__ import annotations

import time

import pytest

from src.copybot import learning
from src.db.schema import db


def _seed_subscription(
    wallet: str,
    *,
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


def _insert_rejects(wallet: str, n: int, *, age_seconds: int = 60) -> None:
    """Insert N reject rows for `wallet`, all timestamped age_seconds ago."""
    at_ts = int(time.time()) - age_seconds
    with db() as conn:
        for _ in range(n):
            conn.execute(
                """
                INSERT INTO live_rejects (at, source_wallet, condition_id,
                    outcome_index, side, price, reason, detail)
                VALUES (?, ?, '0xcid', 0, 'BUY', 0.5, 'low_liquidity', NULL)
                """,
                (at_ts, wallet),
            )


def _insert_paper_fill(wallet: str, *, age_seconds: int = 60) -> None:
    """Insert one open paper_trade for `wallet` (counts as a fill in 24h)."""
    at_ts = int(time.time()) - age_seconds
    with db() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (source_wallet, source_trade_id, condition_id,
                outcome, outcome_index, side, entry_price, entry_size_usdc,
                entry_at, status)
            VALUES (?, 'tid-fill', '0xcid', 'YES', 0, 'BUY', 0.5, 5.0, ?, 'open')
            """,
            (wallet, at_ts),
        )


def test_auto_drop_on_fresh_db_returns_zero(isolated_db):
    """No subscriptions, no rejects → 0 dropped, no error."""
    assert learning.auto_drop_by_rejects() == 0


def test_auto_drop_drops_wallet_with_rejects_and_no_fills(isolated_db):
    """Wallet with 50+ rejects and 0 fills in 24h must be dropped."""
    _seed_subscription("0xclogger", sizing_mult=1.5)
    _insert_rejects("0xclogger", 60, age_seconds=120)

    n = learning.auto_drop_by_rejects(min_rejects=50, window_hours=24)
    assert n == 1

    with db() as conn:
        row = conn.execute(
            "SELECT status, sizing_mult FROM copy_subscriptions WHERE wallet=?",
            ("0xclogger",),
        ).fetchone()
    assert row["status"] == "dropped"
    assert row["sizing_mult"] == 0

    # learning_event row should have been logged with trigger='reject_clog ...'
    with db() as conn:
        ev = conn.execute(
            "SELECT trigger, event_type FROM learning_events WHERE wallet=? ORDER BY id DESC LIMIT 1",
            ("0xclogger",),
        ).fetchone()
    assert ev is not None
    assert ev["event_type"] == "drop"
    assert "reject_clog" in (ev["trigger"] or "")


def test_auto_drop_skips_wallet_with_fills(isolated_db):
    """Wallet with rejects BUT also fills in window → not dropped."""
    _seed_subscription("0xtrader", sizing_mult=1.0)
    _insert_rejects("0xtrader", 60, age_seconds=120)
    _insert_paper_fill("0xtrader", age_seconds=300)

    n = learning.auto_drop_by_rejects(min_rejects=50, window_hours=24)
    assert n == 0

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM copy_subscriptions WHERE wallet=?",
            ("0xtrader",),
        ).fetchone()
    assert row["status"] == "active"


def test_auto_drop_skips_wallet_under_threshold(isolated_db):
    """Wallet with <min_rejects rejects (and no fills) → not dropped."""
    _seed_subscription("0xfew", sizing_mult=1.0)
    _insert_rejects("0xfew", 10, age_seconds=120)

    n = learning.auto_drop_by_rejects(min_rejects=50, window_hours=24)
    assert n == 0

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM copy_subscriptions WHERE wallet=?",
            ("0xfew",),
        ).fetchone()
    assert row["status"] == "active"
