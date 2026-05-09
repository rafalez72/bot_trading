"""Tests for src/copybot/executor.py — live-trade validation and force_close.

Strategy: call `_open_position_validate` directly with a `tx()` connection.
We don't go through `open_position` because that requires the py-clob-client
package and live API creds. The validate function is pure DB logic and is
where every interesting reject path lives.

For `force_close`, we DO go through the public API and mock the CLOB call
via `unittest.mock.patch` against `src.polymarket.clob_client.place_market_order`.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.copybot import executor
from src.db.schema import db, tx
from src.polymarket.clob_client import OrderResult


# ---------- helpers ----------

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


def _set_kill_switch_active() -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value) VALUES ('kill_switch', 'active')
            ON CONFLICT(key) DO UPDATE SET value='active'
            """
        )


def _insert_open_live_trade(
    *,
    source_wallet: str = "0xtrader",
    condition_id: str = "0xcid",
    entry_price: float = 0.5,
    entry_size_usdc: float = 5.0,
    entry_shares: float = 10.0,
    token_id: str = "tok1",
    outcome_index: int = 0,
    dry_run: int = 1,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO live_trades
                (source_wallet, condition_id, token_id, outcome, outcome_index,
                 side, entry_price, entry_size_usdc, entry_shares,
                 entry_at, status, dry_run)
            VALUES (?, ?, ?, 'YES', ?, 'BUY', ?, ?, ?, ?, 'open', ?)
            """,
            (
                source_wallet, condition_id, token_id, outcome_index,
                entry_price, entry_size_usdc, entry_shares,
                int(time.time()), dry_run,
            ),
        )
        return cur.lastrowid


def _validate(**overrides):
    """Wrapper to call _open_position_validate with sane defaults."""
    defaults = dict(
        source_wallet="0xtrader",
        source_trade_id="trade-1",
        condition_id="0xcid",
        outcome_index=0,
        price=0.5,
        timestamp=int(time.time()),
        raw=None,
    )
    defaults.update(overrides)
    with tx() as conn:
        return executor._open_position_validate(conn, **defaults)


# ---------- tests ----------

def test_open_rejected_when_kill_switch_active(isolated_db):
    """Active kill switch short-circuits before any other check."""
    _seed_subscription()
    _set_kill_switch_active()

    result, reject = _validate()

    assert result is None
    assert reject == "kill_switch"


def test_open_rejected_when_wallet_inactive(isolated_db):
    """A wallet with status='paused' must be rejected as 'inactive'."""
    _seed_subscription(status="paused")

    result, reject = _validate()

    assert result is None
    assert reject == "inactive"


def test_open_rejected_when_no_subscription(isolated_db):
    """Wallet not in copy_subscriptions at all → 'no_subscription'."""
    # Deliberately NOT seeding a subscription.
    result, reject = _validate()

    assert result is None
    assert reject == "no_subscription"


@pytest.mark.parametrize("price,reason", [(0.01, "extreme_price"), (0.99, "extreme_price")])
def test_open_rejected_on_extreme_price(isolated_db, price, reason):
    """Prices outside [0.05, 0.95] are rejected as 'extreme_price'."""
    _seed_subscription()

    result, reject = _validate(price=price)

    assert result is None
    assert reject == reason


def test_open_rejected_on_low_expected_pnl(isolated_db):
    """A small size_usdc (sizing_mult=0.5 → $1.25) drops expected PnL below
    LIVE_MIN_EXPECTED_PNL_USDC ($0.50 default), so we reject."""
    _seed_subscription(sizing_mult=0.5)

    result, reject = _validate()

    assert result is None
    assert reject == "expected_pnl_too_low"


def test_open_rejected_on_wallet_concentration(isolated_db):
    """N=MAX_ENTRIES_PER_WALLET_MARKET open positions for the same
    (source_wallet, condition_id) → 'wallet_concentration' on the next entry.
    Default cap is 3 — we seed 3 open trades and assert the 4th rejects.
    """
    from src.config import MAX_ENTRIES_PER_WALLET_MARKET

    _seed_subscription()

    # Seed exactly the cap of open trades on the same (wallet, cid).
    for i in range(MAX_ENTRIES_PER_WALLET_MARKET):
        _insert_open_live_trade(entry_size_usdc=2.5)

    result, reject = _validate(source_trade_id="trade-different")

    assert result is None
    assert reject == "wallet_concentration"


def test_open_validate_passes_with_clean_state(isolated_db):
    """Sanity: with a valid subscription and no other state, the validator
    returns the (size, market_row, category) tuple and reject=None."""
    _seed_subscription()

    result, reject = _validate()

    assert reject is None
    assert result is not None
    size_usdc, market_row, category = result
    # LIVE_BASE_USDC default = 2.5, sizing_mult=1.0 → size=2.5
    assert size_usdc == pytest.approx(2.5)
    # No raw → _ensure_market_stub returns None.
    assert market_row is None
    assert category is None


def test_open_rejected_on_duplicate(isolated_db):
    """A live_trade already exists for this source_trade_id → 'duplicate'."""
    _seed_subscription()
    # Insert a row with the source_trade_id we'll try to use.
    with db() as conn:
        conn.execute(
            """
            INSERT INTO live_trades (source_wallet, source_trade_id, condition_id,
                side, entry_price, entry_size_usdc, status)
            VALUES ('0xtrader', 'trade-dup', '0xcid', 'BUY', 0.5, 2.5, 'open')
            """
        )

    result, reject = _validate(source_trade_id="trade-dup")

    assert result is None
    assert reject == "duplicate"


# ---------- force_close ----------

def test_force_close_updates_status_and_pnl(isolated_db):
    """force_close should mark the trade as closed_win/closed_loss and write
    pnl_usdc + exit_price using the mocked CLOB result.

    Note: when the trade row has dry_run=1, executor applies a pessimistic
    slippage to the exit (`_apply_dry_slippage`) — SELL receives
    price * (1 - LIVE_DRY_SLIPPAGE_PCT). With the default 1.5% that turns
    a quoted 0.7 into 0.6895. This is intentional production behavior.
    """
    from src.config import LIVE_DRY_SLIPPAGE_PCT

    trade_id = _insert_open_live_trade(
        entry_price=0.5,
        entry_size_usdc=5.0,
        entry_shares=10.0,
        dry_run=1,
    )

    fake_order = OrderResult(
        ok=True,
        order_id="order-abc",
        status="matched",
        filled_size=10.0,
        avg_price=0.7,         # exit > entry → win even after slippage
        tx_hash="0xtxhash",
    )

    with patch(
        "src.polymarket.clob_client.place_market_order",
        return_value=fake_order,
    ):
        executor.force_close(trade_id, exit_price=0.7, reason="take_profit_test")

    with db() as conn:
        row = conn.execute(
            "SELECT status, pnl_usdc, exit_price, exit_reason, exit_order_id "
            "FROM live_trades WHERE id=?",
            (trade_id,),
        ).fetchone()

    assert row is not None
    assert row["status"] == "closed_win"
    assert row["pnl_usdc"] is not None
    assert row["pnl_usdc"] > 0
    # Dry-run pessimistic slippage on SELL: 0.7 * (1 - 0.015) = 0.6895
    expected_exit = 0.7 * (1 - LIVE_DRY_SLIPPAGE_PCT)
    assert row["exit_price"] == pytest.approx(expected_exit)
    assert row["exit_reason"] == "take_profit_test"
    assert row["exit_order_id"] == "order-abc"


def test_force_close_loss_is_marked_closed_loss(isolated_db):
    """Symmetric: a sell at a worse price → closed_loss with negative pnl."""
    trade_id = _insert_open_live_trade(
        entry_price=0.5,
        entry_size_usdc=5.0,
        entry_shares=10.0,
    )

    fake_order = OrderResult(
        ok=True,
        order_id="order-loss",
        status="matched",
        filled_size=10.0,
        avg_price=0.3,         # below entry → loss
        tx_hash="0xtxloss",
    )

    with patch(
        "src.polymarket.clob_client.place_market_order",
        return_value=fake_order,
    ):
        executor.force_close(trade_id, exit_price=0.3, reason="stop_loss_test")

    with db() as conn:
        row = conn.execute(
            "SELECT status, pnl_usdc FROM live_trades WHERE id=?", (trade_id,)
        ).fetchone()

    assert row["status"] == "closed_loss"
    assert row["pnl_usdc"] < 0


def test_force_close_no_op_when_already_closed(isolated_db):
    """force_close on a row that's not 'open' should silently return without
    calling the CLOB. Verifies the early-return guard."""
    trade_id = _insert_open_live_trade()
    # Manually flip status to closed.
    with db() as conn:
        conn.execute("UPDATE live_trades SET status='closed_win' WHERE id=?", (trade_id,))

    with patch("src.polymarket.clob_client.place_market_order") as mock_order:
        executor.force_close(trade_id, exit_price=0.7, reason="should_be_skipped")
        assert mock_order.call_count == 0
