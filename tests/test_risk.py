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


# ---------- _horizon_bucket: pure function, no DB needed ----------


def test_horizon_bucket_unknown_falls_back_to_legacy():
    """`secs_left=None` → ('unknown', STOP_LOSS_PCT) — back-compat path."""
    label, thr = risk._horizon_bucket(None)
    assert label == "unknown"
    assert thr == risk.STOP_LOSS_PCT


def test_horizon_bucket_ultrashort():
    """secs_left negativo o muy chico → ultrashort threshold."""
    label, thr = risk._horizon_bucket(-100)
    assert label == "ultrashort"
    assert thr == risk.STOP_LOSS_PCT_ULTRASHORT

    label, thr = risk._horizon_bucket(60)  # 1 min
    assert label == "ultrashort"
    assert thr == risk.STOP_LOSS_PCT_ULTRASHORT


def test_horizon_bucket_short():
    """30min < secs_left < 2h → short."""
    label, thr = risk._horizon_bucket(1800)   # exactamente 30 min → short
    assert label == "short"
    assert thr == risk.STOP_LOSS_PCT_SHORT

    label, thr = risk._horizon_bucket(3600)   # 1 h
    assert label == "short"


def test_horizon_bucket_medium():
    """2h < secs_left < 12h → medium."""
    label, thr = risk._horizon_bucket(7200)   # 2 h boundary → medium
    assert label == "medium"
    assert thr == risk.STOP_LOSS_PCT_MEDIUM

    label, _ = risk._horizon_bucket(20000)    # ~5.5 h
    assert label == "medium"


def test_horizon_bucket_long():
    """secs_left >= 12h → long."""
    label, thr = risk._horizon_bucket(43200)  # 12 h boundary → long
    assert label == "long"
    assert thr == risk.STOP_LOSS_PCT_LONG

    label, _ = risk._horizon_bucket(86400 * 7)  # 1 week
    assert label == "long"


def test_parse_horizon_buckets_invalid_falls_back_to_defaults():
    """Strings malformados devuelven defaults sin crashear."""
    assert risk._parse_horizon_buckets("garbage") == (1800, 7200, 43200)
    assert risk._parse_horizon_buckets("1,2") == (1800, 7200, 43200)        # too few
    assert risk._parse_horizon_buckets("0,1,2") == (1800, 7200, 43200)      # zero invalid
    assert risk._parse_horizon_buckets("100,50,200") == (50, 100, 200)      # auto-sort


def test_end_date_to_epoch_handles_iso_and_null():
    """ISO 8601 con sufijo Z se parsea, vacío/None → None."""
    assert risk._end_date_to_epoch(None) is None
    assert risk._end_date_to_epoch("") is None
    assert risk._end_date_to_epoch("not-a-date") is None
    # 2030-01-01T00:00:00Z = 1893456000
    assert risk._end_date_to_epoch("2030-01-01T00:00:00Z") == 1893456000


# ---------- Pre-pase: waiting_settlement (anti force_close infinite loop) ----------

def _insert_open_paper_trade(
    *,
    condition_id: str,
    asset: str = "tok-1",
    entry_price: float = 0.5,
    entry_size_usdc: float = 5.0,
    source_wallet: str = "0xabc",
    outcome_index: int = 0,
) -> int:
    """Inserta un paper_trade open para un condition_id dado."""
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_trades
                (source_wallet, source_trade_id, condition_id, asset, outcome,
                 outcome_index, side, entry_price, entry_size_usdc, entry_at,
                 status)
            VALUES (?, ?, ?, ?, 'YES', ?, 'BUY', ?, ?, ?, 'open')
            """,
            (
                source_wallet, f"trade-{condition_id}", condition_id, asset,
                outcome_index, entry_price, entry_size_usdc, int(time.time()),
            ),
        )
        return cur.lastrowid


def _insert_market_for_risk(
    *,
    condition_id: str,
    slug: str,
    closed: int = 0,
    end_date: str | None = None,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO markets (condition_id, slug, question, active, closed, end_date)
            VALUES (?, ?, '?', 1, ?, ?)
            ON CONFLICT(condition_id) DO UPDATE SET
                slug=excluded.slug, closed=excluded.closed, end_date=excluded.end_date
            """,
            (condition_id, slug, closed, end_date),
        )


def test_sweep_stops_marks_waiting_settlement_when_market_closed(isolated_db):
    """market.closed=1 en DB → trade pasa a 'waiting_settlement', NO force_close."""
    import asyncio

    cid = "0xclosed"
    _insert_market_for_risk(condition_id=cid, slug="us-election-2026", closed=1)
    pid = _insert_open_paper_trade(condition_id=cid)

    # Mock _last_price para que NUNCA retorne — si aún así llega ahí significa
    # que el pre-pase no excluyó el trade. Por seguridad, retornamos None.
    async def _fake_last_price(client, asset):
        raise AssertionError(
            "no debería llegar a _last_price: el market closed=1 debió "
            "marcarse waiting_settlement antes"
        )

    import src.copybot.risk as risk_mod
    orig = risk_mod._last_price
    risk_mod._last_price = _fake_last_price
    try:
        result = asyncio.run(risk.sweep_stops())
    finally:
        risk_mod._last_price = orig

    # El status del trade debe ser waiting_settlement
    with db() as conn:
        row = conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id=?", (pid,)
        ).fetchone()
    assert row["status"] == "waiting_settlement"
    assert "market_closed" in (row["exit_reason"] or "")
    assert result["waiting_settlement"] >= 1


def test_sweep_stops_marks_waiting_settlement_when_slug_expired(isolated_db):
    """slug-epoch en el pasado (btc-updown-5m-<past_ts>) → waiting_settlement."""
    import asyncio

    past_epoch = int(time.time()) - 600  # 10 min en el pasado
    slug = f"btc-updown-5m-{past_epoch}"
    cid = "0xexpired"
    _insert_market_for_risk(condition_id=cid, slug=slug, closed=0)
    pid = _insert_open_paper_trade(condition_id=cid, source_wallet="crypto_arb")

    async def _fake_last_price(client, asset):
        raise AssertionError("no debería llamarse cuando el slug expiró")

    import src.copybot.risk as risk_mod
    orig = risk_mod._last_price
    risk_mod._last_price = _fake_last_price
    try:
        result = asyncio.run(risk.sweep_stops())
    finally:
        risk_mod._last_price = orig

    with db() as conn:
        row = conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id=?", (pid,)
        ).fetchone()
    assert row["status"] == "waiting_settlement"
    assert "slug_expired" in (row["exit_reason"] or "")
    assert result["waiting_settlement"] >= 1


def test_sweep_stops_marks_waiting_settlement_after_3_empty_orderbooks(isolated_db, monkeypatch):
    """Orderbook vacío 3 veces consecutivas → trade marca waiting_settlement."""
    import asyncio

    cid = "0xempty"
    _insert_market_for_risk(condition_id=cid, slug="active-news-2026", closed=0)
    pid = _insert_open_paper_trade(condition_id=cid)

    # Reset del contador en caso de que otro test lo haya tocado
    monkeypatch.setattr(risk, "_empty_orderbook_count", {})

    async def _empty_price(client, asset):
        return None

    import src.copybot.risk as risk_mod
    orig = risk_mod._last_price
    risk_mod._last_price = _empty_price
    try:
        # 1er sweep: counter=1, status sigue open
        asyncio.run(risk.sweep_stops())
        with db() as conn:
            assert conn.execute(
                "SELECT status FROM paper_trades WHERE id=?", (pid,)
            ).fetchone()["status"] == "open"
        # 2do sweep: counter=2
        asyncio.run(risk.sweep_stops())
        with db() as conn:
            assert conn.execute(
                "SELECT status FROM paper_trades WHERE id=?", (pid,)
            ).fetchone()["status"] == "open"
        # 3er sweep: counter=3 → mark waiting_settlement
        asyncio.run(risk.sweep_stops())
    finally:
        risk_mod._last_price = orig

    with db() as conn:
        row = conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id=?", (pid,)
        ).fetchone()
    assert row["status"] == "waiting_settlement"
    assert "empty_orderbook" in (row["exit_reason"] or "")


def test_sweep_stops_resets_empty_counter_when_price_returns(isolated_db, monkeypatch):
    """Si después de 2 empties el orderbook vuelve a tener precio, counter resetea."""
    import asyncio

    cid = "0xreset"
    _insert_market_for_risk(condition_id=cid, slug="active-news-2026", closed=0)
    pid = _insert_open_paper_trade(condition_id=cid, entry_price=0.5)

    monkeypatch.setattr(risk, "_empty_orderbook_count", {})

    states = ["empty", "empty", "alive", "empty"]

    async def _alternating(client, asset):
        s = states.pop(0)
        return None if s == "empty" else 0.55  # alive: 10% rise, no SL trigger

    import src.copybot.risk as risk_mod
    orig = risk_mod._last_price
    risk_mod._last_price = _alternating
    try:
        for _ in range(4):
            asyncio.run(risk.sweep_stops())
    finally:
        risk_mod._last_price = orig

    # Counter debe estar en 1 (último empty), no en 3 → status sigue open.
    with db() as conn:
        status = conn.execute(
            "SELECT status FROM paper_trades WHERE id=?", (pid,)
        ).fetchone()["status"]
    assert status == "open", (
        f"expected 'open' (counter resetea con price alive), got {status!r}"
    )
