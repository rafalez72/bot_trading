"""Tests cross-strategy del capital_full + kill_switch + notif Acumulado.

Cubren los bugs 1, 2 y 3 del ``docs/PRE_LIVE_AUDIT.md``:

  - Bug 1: ``validation.run_pre_open_checks`` capital_full ignoraba las 5
    strategies adicionales (mm/spike/adv/lh/hedge).
  - Bug 2: ``risk.check_kill_switch`` y los 3 layers (daily $ / consecutive /
    drawdown) leían PnL solo de paper/live_trades — invisibles a las 5
    strategies nuevas.
  - Bug 3: ``learning.on_paper_trade_closed`` Acumulado del Telegram sumaba
    solo TRADES_TABLE actual — strategy closes silentes.

Single source of truth: ``src.copybot.validation._total_capital_in_use`` y
``_total_pnl_since`` (mismo helper para risk + learning).

Fail-soft: si una strategy table no existe todavía (no inicializada),
contribuye 0, no error.
"""
from __future__ import annotations

import time

from src.copybot import risk
from src.copybot.validation import (
    _recent_closed_cross_tables,
    _total_capital_in_use,
    _total_pnl_since,
)
from src.db.schema import db, tx


# --------------------------------------------------------------------------- #
# Helpers para insertar rows en cada tabla cross-strategy
# --------------------------------------------------------------------------- #


def _ensure_strategy_tables() -> None:
    """Crea las 5 strategy tables si no existen (lo que cada strategy hace en
    su init_table on-demand). Mantenemos el DDL local para no depender del
    backend (los tests usan SQLite vía conftest.isolated_db).
    """
    ddls = [
        """
        CREATE TABLE IF NOT EXISTS mm_orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id  TEXT,
            side          TEXT,
            price         REAL,
            size_usdc     REAL,
            order_id      TEXT,
            status        TEXT DEFAULT 'open',
            filled_at     INTEGER,
            fill_price    REAL,
            pnl_usdc      REAL,
            created_at    INTEGER DEFAULT (strftime('%s','now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS spike_arb_trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT,
            side            TEXT,
            spike_pct       REAL,
            mid_at_signal   REAL,
            limit_price     REAL,
            size_usdc       REAL,
            order_id        TEXT,
            fill_price      REAL,
            status          TEXT DEFAULT 'open',
            pnl_usdc        REAL,
            bucket_slug     TEXT,
            bucket_end_ts   INTEGER,
            signal_at       INTEGER,
            filled_at       INTEGER,
            closed_at       INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS adversarial_orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT,
            bucket_slug     TEXT,
            bucket_end_ts   INTEGER,
            loser_side      TEXT,
            p_up_at_signal  REAL,
            ask_price       REAL,
            size_usdc       REAL,
            order_id        TEXT,
            status          TEXT DEFAULT 'detected',
            fill_price      REAL,
            pnl_usdc        REAL,
            signal_at       INTEGER,
            filled_at       INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS long_horizon_trades (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            market_slug       TEXT,
            underlying        TEXT,
            side              TEXT,
            entry_mid         REAL,
            spot_at_entry     REAL,
            spot_1h_ago       REAL,
            spot_move_pct     REAL,
            edge_estimate_pct REAL,
            bet_usdc          REAL,
            order_id          TEXT,
            fill_price        REAL,
            status            TEXT DEFAULT 'open',
            pnl_usdc          REAL,
            opened_at         INTEGER,
            closed_at         INTEGER,
            end_date_market   INTEGER,
            raw               TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hedge_trades (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_slug        TEXT,
            symbol             TEXT,
            side               TEXT,
            poly_trade_id      INTEGER,
            perp_order_id      TEXT,
            perp_qty           REAL,
            perp_entry_price   REAL,
            spot_at_entry      REAL,
            edge_at_open       REAL,
            status             TEXT DEFAULT 'open',
            pnl_poly_usdc      REAL,
            pnl_perp_usdc      REAL,
            pnl_total_usdc     REAL,
            fees_total_usdc    REAL,
            opened_at          INTEGER,
            closed_at          INTEGER,
            raw                TEXT
        )
        """,
    ]
    with tx() as conn:
        for ddl in ddls:
            conn.execute(ddl)


def _ins_mm(*, size_usdc: float, status: str = "open",
            pnl_usdc: float | None = None, filled_at: int | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO mm_orders (condition_id, side, price, size_usdc, "
            "status, pnl_usdc, filled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("0xcid_mm", "BUY", 0.5, size_usdc, status, pnl_usdc, filled_at),
        )


def _ins_spike(*, size_usdc: float, status: str = "open",
               pnl_usdc: float | None = None, closed_at: int | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO spike_arb_trades (symbol, side, size_usdc, status, "
            "pnl_usdc, closed_at, signal_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("BTCUSDT", "BUY", size_usdc, status, pnl_usdc,
             closed_at, (closed_at or 0) - 30),
        )


def _ins_lh(*, bet_usdc: float, status: str = "open",
            pnl_usdc: float | None = None, closed_at: int | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO long_horizon_trades (market_slug, underlying, side, "
            "bet_usdc, status, pnl_usdc, closed_at, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("some-market-eoy", "btc", "BUY", bet_usdc, status, pnl_usdc,
             closed_at, (closed_at or 0) - 300),
        )


def _ins_adv(*, size_usdc: float, status: str = "detected",
             pnl_usdc: float | None = None, filled_at: int | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO adversarial_orders (bucket_slug, bucket_end_ts, "
            "loser_side, size_usdc, status, pnl_usdc, filled_at, signal_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("btc-updown-5m-x", (filled_at or 0) + 300, "DOWN",
             size_usdc, status, pnl_usdc, filled_at,
             (filled_at or 0) - 30),
        )


def _ins_hedge(*, status: str = "open",
               pnl_perp_usdc: float | None = None,
               closed_at: int | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO hedge_trades (bucket_slug, symbol, side, perp_qty, "
            "perp_entry_price, spot_at_entry, status, pnl_perp_usdc, "
            "pnl_total_usdc, closed_at, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("btc-eoy", "BTCUSDT", "SHORT", 0.001, 60000.0, 60000.0,
             status, pnl_perp_usdc, pnl_perp_usdc, closed_at,
             (closed_at or 0) - 60),
        )


def _ins_paper_open(*, size_usdc: float) -> None:
    """paper_trade open (no cerrado) — para test de capital_full."""
    with tx() as conn:
        conn.execute(
            "INSERT INTO paper_trades (source_wallet, source_trade_id, "
            "condition_id, outcome, outcome_index, side, entry_price, "
            "entry_size_usdc, entry_at, status) "
            "VALUES (?, NULL, ?, 'YES', 0, 'BUY', 0.5, ?, ?, 'open')",
            ("0xabc", "0xcid_paper", size_usdc, int(time.time())),
        )


def _ins_paper_closed_loss(*, pnl: float, exit_at: int) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO paper_trades (source_wallet, source_trade_id, "
            "condition_id, outcome, outcome_index, side, entry_price, "
            "entry_size_usdc, entry_at, exit_price, exit_at, pnl_usdc, status) "
            "VALUES (?, NULL, ?, 'YES', 0, 'BUY', 0.5, 5.0, ?, 0.4, ?, ?, "
            "'closed_loss')",
            ("0xabc", "0xcid_p", exit_at - 60, exit_at, pnl),
        )


# --------------------------------------------------------------------------- #
# Test 1: capital_full suma cross-tables (bug 1)
# --------------------------------------------------------------------------- #


def test_capital_full_sums_across_strategy_tables(isolated_db):
    """``_total_capital_in_use`` suma open positions de mm + spike + lh + paper.

    Pre-fix: solo veía paper_trades → strategies sumaban "invisible". El
    helper unificado debe ver $7 paper + $15 mm + $10 spike + $20 lh = $52.
    hedge_trades NO contribuye (poly leg ya en paper/live_trades).
    """
    _ensure_strategy_tables()

    _ins_paper_open(size_usdc=7.0)
    _ins_mm(size_usdc=15.0, status="open")
    _ins_spike(size_usdc=10.0, status="open")
    _ins_lh(bet_usdc=20.0, status="open")
    # hedge open con perp_qty pero sin size col propio → 0 contribution
    _ins_hedge(status="open")
    # Strategies cerradas NO deben sumar (status != 'open').
    _ins_mm(size_usdc=99.0, status="closed", pnl_usdc=1.0, filled_at=1000)

    with db() as conn:
        total = _total_capital_in_use(conn)
    assert abs(total - 52.0) < 1e-6, f"expected $52.00, got ${total:.2f}"


# --------------------------------------------------------------------------- #
# Test 2: kill switch detecta loss cross-table (bug 2)
# --------------------------------------------------------------------------- #


def test_kill_switch_legacy_layer_triggers_on_strategy_only_losses(
    isolated_db, now_ts, monkeypatch
):
    """5 losses chicos en spike+lh (sin pnl en paper_trades) → legacy
    DAILY_KILL_SWITCH_PCT debe disparar porque ahora ve cross-tables.

    Pre-fix: SUM(pnl) solo de paper_trades = $0 → kill switch ciego mientras
    las strategy tables sangraban. Post-fix: SUM(pnl) = -$15 en strategy
    tables, supera el threshold -$10 (10% de $100 BOT_CAPITAL_USDC).
    """
    _ensure_strategy_tables()

    # 3 losses en spike + 2 en long_horizon, total -$15.
    _ins_spike(size_usdc=5.0, status="closed",
               pnl_usdc=-3.0, closed_at=now_ts - 100)
    _ins_spike(size_usdc=5.0, status="closed",
               pnl_usdc=-3.0, closed_at=now_ts - 200)
    _ins_spike(size_usdc=5.0, status="closed",
               pnl_usdc=-3.0, closed_at=now_ts - 300)
    _ins_lh(bet_usdc=10.0, status="closed",
            pnl_usdc=-3.0, closed_at=now_ts - 400)
    _ins_lh(bet_usdc=10.0, status="closed",
            pnl_usdc=-3.0, closed_at=now_ts - 500)

    # Sanity: paper_trades vacía — pre-fix esto era $0.
    with db() as conn:
        pnl_cross = _total_pnl_since(conn, now_ts - 86400)
    assert abs(pnl_cross - (-15.0)) < 1e-6, (
        f"cross-table pnl mismatch: ${pnl_cross:.2f}"
    )

    assert risk.check_kill_switch() is True
    assert risk.kill_switch_status()["active"] is True


def test_consecutive_losses_layer_counts_cross_tables(
    isolated_db, now_ts, monkeypatch
):
    """Layer 2 (consecutive_losses) ordena trades por exit_at DESC cross-tables.

    Setup: 3 losses en spike + 2 en lh, todos chicos (-$1) → SUM=-$5
    (legacy y daily_loss_cap no disparan). 5 losses consecutivos cross-table
    → layer "consecutive_losses" debe disparar.
    """
    _ensure_strategy_tables()
    monkeypatch.setattr(risk, "DAILY_KILL_SWITCH_PCT", 9.99)  # anular legacy

    # exit_at decrecientes para que el ORDER BY DESC encuentre los 5 más
    # recientes en orden estable.
    _ins_spike(size_usdc=5.0, status="closed",
               pnl_usdc=-1.0, closed_at=now_ts - 100)
    _ins_spike(size_usdc=5.0, status="closed",
               pnl_usdc=-1.0, closed_at=now_ts - 110)
    _ins_spike(size_usdc=5.0, status="closed",
               pnl_usdc=-1.0, closed_at=now_ts - 120)
    _ins_lh(bet_usdc=5.0, status="closed",
            pnl_usdc=-1.0, closed_at=now_ts - 130)
    _ins_lh(bet_usdc=5.0, status="closed",
            pnl_usdc=-1.0, closed_at=now_ts - 140)

    # Verificar primero el helper unificado: 5 items, todos is_loss=True
    with db() as conn:
        recent = _recent_closed_cross_tables(conn, 0, limit=5)
    assert len(recent) == 5
    assert all(r["is_loss"] for r in recent), recent

    assert risk.check_kill_switch() is True
    reason = risk.kill_switch_status()["reason"] or ""
    assert "consecutive_losses" in reason, reason


# --------------------------------------------------------------------------- #
# Test 3: notif Acumulado suma cross-tables (bug 3)
# --------------------------------------------------------------------------- #


def test_accumulated_pnl_sums_cross_tables(isolated_db, now_ts):
    """``_total_pnl_since`` agrega pnl_usdc cross-tables — mismo helper que
    learning.on_paper_trade_closed usa para el "Acumulado" del Telegram.

    Pre-fix: el accumulated del Telegram solo veía paper/live_trades →
    closes de mm/spike/adv/lh/hedge silentes. Post-fix: suma todo.
    """
    _ensure_strategy_tables()

    # +$10 paper, +$5 mm, -$3 spike, +$8 lh, -$2 adv, +$4 hedge_perp
    _ins_paper_closed_loss(pnl=10.0, exit_at=now_ts - 100)  # status reused
    _ins_mm(size_usdc=5.0, status="closed",
            pnl_usdc=5.0, filled_at=now_ts - 200)
    _ins_spike(size_usdc=5.0, status="closed",
               pnl_usdc=-3.0, closed_at=now_ts - 300)
    _ins_lh(bet_usdc=5.0, status="closed",
            pnl_usdc=8.0, closed_at=now_ts - 400)
    _ins_adv(size_usdc=5.0, status="closed",
             pnl_usdc=-2.0, filled_at=now_ts - 500)
    _ins_hedge(status="closed", pnl_perp_usdc=4.0, closed_at=now_ts - 600)

    with db() as conn:
        total = _total_pnl_since(conn, now_ts - 86400)
    # 10 + 5 - 3 + 8 - 2 + 4 = 22
    assert abs(total - 22.0) < 1e-6, f"expected $22.00, got ${total:.2f}"


# --------------------------------------------------------------------------- #
# Test 4: fail-soft cuando una strategy table no existe
# --------------------------------------------------------------------------- #


def test_helpers_failsoft_when_strategy_tables_missing(isolated_db, now_ts):
    """Si las 5 strategy tables no existen (DB fresh, no init_table corrido),
    los helpers devuelven 0 — no rompen. Solo cuentan paper/live_trades.

    Critical para backward compat: el bot N1 puede arrancar sin que ninguna
    strategy adicional esté inicializada (esto pasa en el bot real hoy).
    """
    # NO llamamos _ensure_strategy_tables — las strategy tables NO existen.
    _ins_paper_open(size_usdc=12.0)
    _ins_paper_closed_loss(pnl=-3.0, exit_at=now_ts - 100)

    with db() as conn:
        cap = _total_capital_in_use(conn)
        pnl = _total_pnl_since(conn, now_ts - 86400)
        recent = _recent_closed_cross_tables(conn, now_ts - 86400, limit=10)

    assert abs(cap - 12.0) < 1e-6, f"capital fail-soft mismatch: ${cap:.2f}"
    assert abs(pnl - (-3.0)) < 1e-6, f"pnl fail-soft mismatch: ${pnl:.2f}"
    # 1 trade cerrado (paper) — strategy tables aportan 0 items.
    assert len(recent) == 1, recent
    assert recent[0]["table"] == "paper_trades"
    assert recent[0]["is_loss"] is True
