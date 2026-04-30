"""Tests for src/copybot/risk.py — kill switch lifecycle.

The bot defaults to paper mode (LIVE_MODE unset), so `risk.TRADES_TABLE` is
"paper_trades" and `EFFECTIVE_CAPITAL_USDC` is 100.0 (BOT_CAPITAL_USDC default).
With DAILY_KILL_SWITCH_PCT=0.10, the kill threshold is -$10.

These tests insert losing rows into `paper_trades` directly via SQL and then
exercise the public risk API.
"""
from __future__ import annotations

import time

from src.copybot import risk
from src.db.schema import db


def _insert_closed_trade(
    *,
    pnl: float,
    exit_at: int,
    status: str = "closed_loss",
    source_wallet: str = "0xabc",
    condition_id: str = "0xcid",
) -> None:
    """Insert a fully-closed paper_trade row with the given pnl + exit_at."""
    with db() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades
                (source_wallet, source_trade_id, condition_id, outcome,
                 outcome_index, side, entry_price, entry_size_usdc,
                 entry_at, exit_price, exit_at, pnl_usdc, status)
            VALUES (?, NULL, ?, 'YES', 0, 'BUY', 0.5, 5.0,
                    ?, 0.4, ?, ?, ?)
            """,
            (
                source_wallet,
                condition_id,
                exit_at - 60,
                exit_at,
                pnl,
                status,
            ),
        )


def test_kill_switch_inactive_when_no_trades(isolated_db):
    """Fresh DB with no closed trades → kill switch stays inactive."""
    assert risk.check_kill_switch() is False

    status = risk.kill_switch_status()
    assert status["active"] is False
    # `since` may be None (never set) on a fresh DB.
    assert status.get("reason", "") == ""


def test_kill_switch_activates_on_loss_threshold(isolated_db, now_ts):
    """Sum of recent losses < -10% of capital ($-10) → activates."""
    # 3 losing trades summing to -$12 (well past -$10 threshold).
    _insert_closed_trade(pnl=-4.0, exit_at=now_ts - 100)
    _insert_closed_trade(pnl=-4.0, exit_at=now_ts - 200)
    _insert_closed_trade(pnl=-4.0, exit_at=now_ts - 300)

    assert risk.check_kill_switch() is True

    status = risk.kill_switch_status()
    assert status["active"] is True
    assert "PnL" in status["reason"]


def test_kill_switch_inactive_after_reset(isolated_db, now_ts):
    """Reset clears the active flag AND ignores pre-reset losses going forward."""
    # Activate via losses.
    _insert_closed_trade(pnl=-6.0, exit_at=now_ts - 200)
    _insert_closed_trade(pnl=-6.0, exit_at=now_ts - 300)
    assert risk.check_kill_switch() is True

    # Manual reset.
    risk.reset_kill_switch()

    status = risk.kill_switch_status()
    assert status["active"] is False
    assert "manual reset" in (status["reason"] or "")

    # Re-running check_kill_switch with the SAME losses should NOT reactivate.
    # The reset_at high-water mark filters them out (their exit_at < reset_at).
    # Need a small sleep so reset_at > the inserted exit_at values.
    time.sleep(1)
    assert risk.check_kill_switch() is False
    assert risk.kill_switch_status()["active"] is False


def test_kill_switch_reactivates_on_new_losses_after_reset(isolated_db, now_ts, monkeypatch):
    """After reset, NEW losses past the threshold reactivate the switch."""
    # Disable the race-protection grace window for this test (production = 5s).
    monkeypatch.setattr(risk, "RESET_GRACE_SECONDS", 0)

    # Step 1: trigger and reset.
    _insert_closed_trade(pnl=-6.0, exit_at=now_ts - 500)
    _insert_closed_trade(pnl=-6.0, exit_at=now_ts - 600)
    assert risk.check_kill_switch() is True
    risk.reset_kill_switch()
    assert risk.kill_switch_status()["active"] is False

    # Wait so the high-water mark is strictly before our "new" exit_at values.
    time.sleep(1)
    reset_at_ts = int(time.time())

    # Step 2: insert NEW losses dated after reset_at → should reactivate.
    _insert_closed_trade(pnl=-7.0, exit_at=reset_at_ts + 5)
    _insert_closed_trade(pnl=-7.0, exit_at=reset_at_ts + 10)

    assert risk.check_kill_switch() is True
    assert risk.kill_switch_status()["active"] is True
