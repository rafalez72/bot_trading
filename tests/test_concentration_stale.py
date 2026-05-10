"""Tests for the relaxed `wallet_concentration` and `stale_trade` checks.

Background (2026-05-09): production logs showed two issues:

1. `wallet_concentration` blocked legitimate DCA / pyramiding because the
   prior dollar-cap on per-wallet exposure rejected the 2nd/3rd entry on
   the same market. Fix: cap N=MAX_ENTRIES_PER_WALLET_MARKET (default 3)
   open positions per (source_wallet, condition_id) instead.

2. `stale_trade` was over-aggressive (60s threshold) AND polling re-processed
   the same already-detected-via-WS trade ~12 times/second, spamming logs.
   Fix: configurable threshold via STALE_TRADE_MAX_AGE_S (default 300s) and
   in-memory LRU dedup of log emissions per source_trade_id within 60s.

These tests pin the new behavior:
  - Allow N entries per (wallet, market), block N+1.
  - Respect the configurable threshold.
  - The LRU helper suppresses duplicate log emissions within the TTL window.
"""
from __future__ import annotations

import time

import pytest

from src.copybot import paper
from src.db.schema import db


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


def _insert_open_paper_trade(
    *,
    source_wallet: str = "0xtrader",
    condition_id: str = "0xcid",
    source_trade_id: str | None = None,
    entry_price: float = 0.5,
    entry_size_usdc: float = 5.0,
    outcome_index: int = 0,
) -> int:
    """Insert a synthetic open paper_trade row directly via SQL.

    Bypasses `paper.open_position` so the test isolates the concentration
    check (no need to satisfy market/liquidity/extreme-price filters).
    """
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_trades
                (source_wallet, source_trade_id, condition_id, outcome,
                 outcome_index, side, entry_price, entry_size_usdc,
                 entry_at, status)
            VALUES (?, ?, ?, 'YES', ?, 'BUY', ?, ?, ?, 'open')
            """,
            (
                source_wallet, source_trade_id, condition_id,
                outcome_index, entry_price, entry_size_usdc,
                int(time.time()),
            ),
        )
        return cur.lastrowid


@pytest.fixture(autouse=True)
def _reset_stale_log():
    """Each test starts with a clean LRU — global state would otherwise
    leak across tests and mask the dedup behavior."""
    paper._stale_log_seen.clear()
    yield
    paper._stale_log_seen.clear()


# ---------- wallet_concentration: relaxed to N entries ----------


def test_wallet_concentration_allows_up_to_n_entries(isolated_db):
    """Up to MAX_ENTRIES_PER_WALLET_MARKET-1 existing open trades must still
    let a new entry through (the count check uses `>=`, so N-1 existing
    means the new entry would be the Nth and is allowed)."""
    from src.config import MAX_ENTRIES_PER_WALLET_MARKET

    _seed_subscription()

    # Seed N-1 open trades on the same (wallet, cid). The validator will
    # see count = N-1 < N and let the next one through.
    for i in range(MAX_ENTRIES_PER_WALLET_MARKET - 1):
        _insert_open_paper_trade(
            source_trade_id=f"existing-{i}",
            entry_size_usdc=1.0,  # tiny, so neither expected_pnl nor caps reject
        )

    # Now invoke the public path. We use a fresh source_trade_id so the
    # duplicate guard doesn't kick in. Timestamp = now so stale_trade passes.
    pid, reject = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="new-entry-N",
        condition_id="0xcid",
        outcome=None,
        outcome_index=0,
        price=0.5,
        timestamp=int(time.time()),
        raw=None,
    )

    # The Nth entry must NOT be rejected as wallet_concentration. It may
    # still be rejected for an unrelated reason (e.g. expected_pnl too low
    # if the default sizing yields a tiny trade), but never for the cap we
    # explicitly relaxed.
    assert reject != "wallet_concentration", (
        f"Nth entry rejected with {reject!r} — cap should allow up to N "
        f"entries per (wallet, market)."
    )


def test_wallet_concentration_blocks_at_n_plus_one(isolated_db):
    """Exactly N existing open trades on (wallet, cid) → the N+1-th rejects
    as 'wallet_concentration'."""
    from src.config import MAX_ENTRIES_PER_WALLET_MARKET

    _seed_subscription()

    # Seed exactly N open trades on the same (wallet, cid) — count == N.
    for i in range(MAX_ENTRIES_PER_WALLET_MARKET):
        _insert_open_paper_trade(
            source_trade_id=f"existing-{i}",
            entry_size_usdc=1.0,
        )

    pid, reject = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="new-entry-overflow",
        condition_id="0xcid",
        outcome=None,
        outcome_index=0,
        price=0.5,
        timestamp=int(time.time()),
        raw=None,
    )

    assert pid is None
    assert reject == "wallet_concentration"


# ---------- stale_trade: threshold + log-dedup ----------


def test_stale_trade_respects_configured_threshold(isolated_db, monkeypatch):
    """A trade older than the threshold rejects; one within the threshold
    proceeds past the stale check.

    We monkeypatch the module-level threshold to a small value so the test
    is deterministic regardless of the env-driven default.
    """
    # 2026-05-10: el threshold se movió a src.copybot.validation tras
    # el refactor a single-source-of-truth para checks paper/live.
    from src.copybot import validation as _val
    monkeypatch.setattr(_val, "STALE_TRADE_MAX_AGE_S", 30)

    _seed_subscription()

    now = int(time.time())

    # Trade older than the threshold → reject as stale_trade.
    pid_old, reject_old = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="trade-stale",
        condition_id="0xcid",
        outcome=None,
        outcome_index=0,
        price=0.5,
        timestamp=now - 120,  # 120s old, threshold is 30s
        raw=None,
    )
    assert pid_old is None
    assert reject_old == "stale_trade"

    # Fresh trade → must NOT reject as stale (other rejects are fine; we
    # only assert the stale gate isn't the failure here).
    pid_fresh, reject_fresh = paper.open_position(
        source_wallet="0xtrader",
        source_trade_id="trade-fresh",
        condition_id="0xcid",
        outcome=None,
        outcome_index=0,
        price=0.5,
        timestamp=now,  # 0s old
        raw=None,
    )
    assert reject_fresh != "stale_trade"


def test_stale_trade_log_dedup_suppresses_repeats_within_window():
    """`_should_log_stale(trade_id)` returns True the first time and False
    on subsequent calls within the TTL window for the same trade_id.

    A different trade_id is always allowed through immediately (only the
    duplicate of the same id is suppressed).
    """
    # First call: never seen → log.
    assert paper._should_log_stale("trade-A") is True
    # Immediate repeat within TTL: suppress.
    assert paper._should_log_stale("trade-A") is False
    # A different id is independent.
    assert paper._should_log_stale("trade-B") is True
    assert paper._should_log_stale("trade-B") is False
    # None/empty source_trade_id falls back to "always log" (we can't dedup
    # without a key, and we'd rather over-log than silently drop).
    assert paper._should_log_stale(None) is True
    assert paper._should_log_stale("") is True
