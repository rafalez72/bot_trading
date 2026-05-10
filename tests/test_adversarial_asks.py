"""Tests for adversarial_asks — dust ASKs pre-close del lado perdedor.

Cubre:
1. Loser detection: p_up extremo → loser side correcto.
2. Skip si secs_to_close > max (demasiado lejos del cierre).
3. Skip si p_up incierto (zona 0.4-0.6 default).
4. Settle path: posted → DB row insertada con status correcto + filled.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.copybot.adversarial_asks import (
    AdversarialAsks,
    AdversarialConfig,
    AdversarialDecision,
    SIDE_DOWN,
    SIDE_UP,
    STATUS_DETECTED,
    STATUS_FILLED,
    STATUS_OPEN,
    detect_loser_side,
    evaluate_market,
    init_schema,
    parse_slug,
    record_signal,
    update_settlement,
)


# ----------------- Helpers -----------------

def _mk_config(**overrides) -> AdversarialConfig:
    base = AdversarialConfig(
        enabled=True,
        signal_only=True,
        max_secs_to_close=60.0,
        min_secs_to_close=10.0,
        min_loser_prob=0.85,
        ask_price=0.05,
        size_usdc=2.0,
        check_interval_s=1.0,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _bucket_end_in(secs: int, anchor: int) -> int:
    return anchor + secs


# ===================================================
# Caso 1 — Loser detection
# ===================================================

class TestLoserDetection:

    def test_p_up_092_loser_is_down(self):
        # UP es ganador casi seguro → DOWN va a $0 → vendemos DOWN.
        assert detect_loser_side(p_up=0.92, min_loser_prob=0.85) == SIDE_DOWN

    def test_p_up_005_loser_is_up(self):
        # DOWN es ganador casi seguro → UP va a $0 → vendemos UP.
        assert detect_loser_side(p_up=0.05, min_loser_prob=0.85) == SIDE_UP

    def test_p_up_at_threshold_exact(self):
        # En el borde exacto, debe detectar loser (>= comparison).
        assert detect_loser_side(p_up=0.85, min_loser_prob=0.85) == SIDE_DOWN
        assert detect_loser_side(p_up=0.15, min_loser_prob=0.85) == SIDE_UP

    def test_p_up_uncertain_returns_none(self):
        # Zona incierta — no posteamos nada.
        assert detect_loser_side(p_up=0.50, min_loser_prob=0.85) is None
        assert detect_loser_side(p_up=0.40, min_loser_prob=0.85) is None
        assert detect_loser_side(p_up=0.60, min_loser_prob=0.85) is None
        assert detect_loser_side(p_up=0.84, min_loser_prob=0.85) is None
        assert detect_loser_side(p_up=0.16, min_loser_prob=0.85) is None

    def test_evaluate_market_p_up_high_picks_down_loser(self):
        # +1% spot move BTC con sigma 0.05%/min y secs_left=20 → p_up >> 0.85
        # (~70 sigmas de drift positivo en lo que queda).
        anchor = 1_777_505_400
        bucket_end = anchor + 30  # 30s a close, dentro de window [10,60]
        bucket_start = bucket_end - 300
        d = evaluate_market(
            bucket_slug=f"btc-updown-5m-{bucket_end}",
            bucket_end_ts=bucket_end,
            spot_now=70_700.0,        # +1% del start
            spot_at_bucket_start=70_000.0,
            now_ts=anchor,
            config=_mk_config(),
        )
        assert d.post is True
        assert d.loser_side == SIDE_DOWN
        assert d.p_up > 0.95
        assert d.symbol == "BTCUSDT"
        # bucket_start_ts referenciado correctamente en el decision metadata.
        assert d.bucket_end_ts == bucket_end
        assert bucket_start == bucket_end - 300  # sanity

    def test_evaluate_market_p_up_low_picks_up_loser(self):
        anchor = 1_777_505_400
        bucket_end = anchor + 30
        d = evaluate_market(
            bucket_slug=f"btc-updown-5m-{bucket_end}",
            bucket_end_ts=bucket_end,
            spot_now=69_300.0,
            spot_at_bucket_start=70_000.0,
            now_ts=anchor,
            config=_mk_config(),
        )
        assert d.post is True
        assert d.loser_side == SIDE_UP
        assert d.p_up < 0.05


# ===================================================
# Caso 2 — Skip si secs_to_close > max
# ===================================================

class TestWindowGate:

    def test_skip_too_far_from_close(self):
        anchor = 1_777_505_400
        bucket_end = anchor + 120  # 120s > max=60s
        d = evaluate_market(
            bucket_slug=f"btc-updown-5m-{bucket_end}",
            bucket_end_ts=bucket_end,
            spot_now=70_700.0,
            spot_at_bucket_start=70_000.0,
            now_ts=anchor,
            config=_mk_config(max_secs_to_close=60.0),
        )
        assert d.post is False
        assert d.loser_side is None
        assert d.reason == "too_far_from_close"

    def test_skip_too_close_to_close(self):
        # secs_to_close = 5 < min_secs_to_close=10 → skip.
        anchor = 1_777_505_400
        bucket_end = anchor + 5
        d = evaluate_market(
            bucket_slug=f"btc-updown-5m-{bucket_end}",
            bucket_end_ts=bucket_end,
            spot_now=70_700.0,
            spot_at_bucket_start=70_000.0,
            now_ts=anchor,
            config=_mk_config(),
        )
        assert d.post is False
        assert d.reason == "too_close_to_close"

    def test_skip_invalid_slug(self):
        d = evaluate_market(
            bucket_slug="random-non-crypto-market",
            bucket_end_ts=2_000_000_000,
            spot_now=100.0,
            spot_at_bucket_start=100.0,
            now_ts=1_999_999_970,
            config=_mk_config(),
        )
        assert d.post is False
        assert d.reason == "invalid_slug"

    def test_skip_no_spot_data(self):
        # Spot history vacío (bot recién arrancado) → no tenemos start price.
        anchor = 1_777_505_400
        bucket_end = anchor + 30
        d = evaluate_market(
            bucket_slug=f"btc-updown-5m-{bucket_end}",
            bucket_end_ts=bucket_end,
            spot_now=None,
            spot_at_bucket_start=None,
            now_ts=anchor,
            config=_mk_config(),
        )
        assert d.post is False
        assert d.reason == "no_spot"


# ===================================================
# Caso 3 — Skip si p_up incierto (0.4-0.6 zone)
# ===================================================

class TestUncertainSkip:

    def test_skip_when_spot_flat(self):
        # Sin movimiento → p_up ~ 0.5 → uncertain → skip.
        anchor = 1_777_505_400
        bucket_end = anchor + 30
        d = evaluate_market(
            bucket_slug=f"btc-updown-5m-{bucket_end}",
            bucket_end_ts=bucket_end,
            spot_now=70_000.0,
            spot_at_bucket_start=70_000.0,
            now_ts=anchor,
            config=_mk_config(),
        )
        assert d.post is False
        assert d.reason.startswith("uncertain_p_up_")
        assert d.p_up == pytest.approx(0.5, abs=1e-6)

    def test_skip_when_p_up_in_borderline(self):
        # +0.005% move BTC, sigma 0.05%/min, secs_left=30 → sigma_remaining
        # ≈ 0.05*sqrt(0.5) ≈ 0.0354. z = 0.005/0.0354 ≈ 0.14 → p_up ≈ 0.556.
        # Uncertain bajo min_loser_prob=0.85.
        anchor = 1_777_505_400
        bucket_end = anchor + 30
        d = evaluate_market(
            bucket_slug=f"btc-updown-5m-{bucket_end}",
            bucket_end_ts=bucket_end,
            spot_now=70_003.5,
            spot_at_bucket_start=70_000.0,
            now_ts=anchor,
            config=_mk_config(),
        )
        assert d.post is False
        assert d.loser_side is None
        assert 0.4 < d.p_up < 0.7

    def test_skip_when_loser_prob_threshold_too_strict(self):
        # Move modesto (+0.04%): sigma_remaining ≈ 0.0354 → z ≈ 1.13 →
        # p_up ≈ 0.871. Pasa el default min_loser_prob=0.85, pero no 0.95.
        anchor = 1_777_505_400
        bucket_end = anchor + 30
        d_default = evaluate_market(
            bucket_slug=f"btc-updown-5m-{bucket_end}",
            bucket_end_ts=bucket_end,
            spot_now=70_028.0,
            spot_at_bucket_start=70_000.0,
            now_ts=anchor,
            config=_mk_config(min_loser_prob=0.85),
        )
        assert d_default.post is True
        assert 0.85 <= d_default.p_up < 0.95

        d_strict = evaluate_market(
            bucket_slug=f"btc-updown-5m-{bucket_end}",
            bucket_end_ts=bucket_end,
            spot_now=70_028.0,
            spot_at_bucket_start=70_000.0,
            now_ts=anchor,
            config=_mk_config(min_loser_prob=0.95),
        )
        assert d_strict.post is False


# ===================================================
# Caso 4 — Settle path correcto con DB
# ===================================================

class TestSettlePath:

    @pytest.mark.asyncio
    async def test_post_signal_only_persists_detected_row(self, isolated_db):
        # Arrange: AdversarialAsks con signal_only mock.
        anchor = 1_777_505_400
        bucket_end = anchor + 30

        captured: list[tuple] = []

        async def fake_post_hook(token_id, side, price, size):
            captured.append((token_id, side, price, size))
            return None  # signal_only convention

        bot = AdversarialAsks(
            config=_mk_config(),
            post_ask_hook=fake_post_hook,
            now_fn=lambda: anchor,
        )
        # Inyectamos history de spot directamente.
        bot.spot_history["BTCUSDT"] = [
            ((bucket_end - 300) * 1000, 70_000.0),
            (anchor * 1000, 70_700.0),  # +1%
        ]

        market = {
            "slug": f"btc-updown-5m-{bucket_end}",
            "end_ts": bucket_end,
            "clobTokenIds": json.dumps(["TOKEN_UP", "TOKEN_DOWN"]),
        }

        # Act
        decision = await bot.evaluate_and_post(market)

        # Assert
        assert decision is not None
        assert decision.post is True
        assert decision.loser_side == SIDE_DOWN
        assert captured == [("TOKEN_DOWN", "SELL", 0.05, 2.0)]

        # DB row insertada con status=detected (signal_only) + datos correctos.
        from src.db.schema import db
        with db() as conn:
            rows = conn.execute(
                "SELECT bucket_slug, loser_side, status, ask_price, size_usdc, "
                "p_up_at_signal FROM adversarial_orders"
            ).fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["bucket_slug"] == f"btc-updown-5m-{bucket_end}"
        assert r["loser_side"] == SIDE_DOWN
        assert r["status"] == STATUS_DETECTED  # signal_only → no order_id
        assert float(r["ask_price"]) == pytest.approx(0.05)
        assert float(r["size_usdc"]) == pytest.approx(2.0)
        assert float(r["p_up_at_signal"]) > 0.95

    @pytest.mark.asyncio
    async def test_post_with_order_id_persists_open_status(self, isolated_db):
        # Si el hook devuelve un order_id → status='open' (no signal_only).
        anchor = 1_777_505_400
        bucket_end = anchor + 30

        async def hook(*args, **kwargs):
            return "ORDER_ABCDEF"

        bot = AdversarialAsks(
            config=_mk_config(signal_only=False),
            post_ask_hook=hook,
            now_fn=lambda: anchor,
        )
        bot.spot_history["BTCUSDT"] = [
            ((bucket_end - 300) * 1000, 70_000.0),
            (anchor * 1000, 70_700.0),
        ]

        market = {
            "slug": f"btc-updown-5m-{bucket_end}",
            "end_ts": bucket_end,
            "clobTokenIds": ["TOKEN_UP", "TOKEN_DOWN"],
        }
        await bot.evaluate_and_post(market)

        from src.db.schema import db
        with db() as conn:
            rows = conn.execute(
                "SELECT order_id, status FROM adversarial_orders"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["order_id"] == "ORDER_ABCDEF"
        assert rows[0]["status"] == STATUS_OPEN

    @pytest.mark.asyncio
    async def test_settle_updates_to_filled_with_pnl(self, isolated_db):
        # Insertamos detected, después update a filled con pnl positivo.
        init_schema()
        rec_id = record_signal(
            symbol="BTCUSDT",
            bucket_slug="btc-updown-5m-1777505430",
            bucket_end_ts=1_777_505_430,
            loser_side=SIDE_DOWN,
            p_up=0.95,
            ask_price=0.05,
            size_usdc=2.0,
            status=STATUS_OPEN,
            order_id="ORD_X",
            signal_at=1_777_505_400,
        )
        assert rec_id is not None and rec_id > 0

        ok = update_settlement(
            order_id_db=rec_id,
            status=STATUS_FILLED,
            fill_price=0.05,
            pnl_usdc=2.0 * 1.0,  # full size cobrado a $0.05/share asume settle $0
            filled_at=1_777_505_420,
        )
        assert ok is True

        from src.db.schema import db
        with db() as conn:
            r = conn.execute(
                "SELECT status, fill_price, pnl_usdc, filled_at "
                "FROM adversarial_orders WHERE id=?", (rec_id,),
            ).fetchone()
        assert r["status"] == STATUS_FILLED
        assert float(r["fill_price"]) == pytest.approx(0.05)
        assert float(r["pnl_usdc"]) == pytest.approx(2.0)
        assert int(r["filled_at"]) == 1_777_505_420

    @pytest.mark.asyncio
    async def test_dedup_does_not_repost_same_bucket_side(self, isolated_db):
        # Llamar evaluate_and_post dos veces no debe duplicar la row.
        anchor = 1_777_505_400
        bucket_end = anchor + 30

        async def hook(*args, **kwargs):
            return None

        bot = AdversarialAsks(
            config=_mk_config(),
            post_ask_hook=hook,
            now_fn=lambda: anchor,
        )
        bot.spot_history["BTCUSDT"] = [
            ((bucket_end - 300) * 1000, 70_000.0),
            (anchor * 1000, 70_700.0),
        ]
        market = {
            "slug": f"btc-updown-5m-{bucket_end}",
            "end_ts": bucket_end,
            "clobTokenIds": ["TOKEN_UP", "TOKEN_DOWN"],
        }
        await bot.evaluate_and_post(market)
        await bot.evaluate_and_post(market)

        from src.db.schema import db
        with db() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM adversarial_orders"
            ).fetchall()
        assert int(rows[0]["n"]) == 1


# ===================================================
# Sanity: parse_slug
# ===================================================

class TestParseSlug:

    def test_parse_btc_slug(self):
        assert parse_slug("btc-updown-5m-1777505400") == ("btc-updown-5m-", 1_777_505_400)

    def test_parse_eth_slug(self):
        assert parse_slug("eth-updown-5m-1777505999") == ("eth-updown-5m-", 1_777_505_999)

    def test_parse_invalid_returns_none(self):
        assert parse_slug("trump-2024") is None
        assert parse_slug("") is None
        assert parse_slug(None) is None  # type: ignore[arg-type]


# ===================================================
# Sanity: from_env
# ===================================================

class TestConfigFromEnv:

    def test_defaults(self, monkeypatch):
        for k in (
            "ADVERSARIAL_ENABLED", "ADVERSARIAL_SIGNAL_ONLY",
            "ADVERSARIAL_MAX_SECS_TO_CLOSE", "ADVERSARIAL_MIN_LOSER_PROB",
            "ADVERSARIAL_ASK_PRICE", "ADVERSARIAL_SIZE_USDC",
        ):
            monkeypatch.delenv(k, raising=False)
        c = AdversarialConfig.from_env()
        assert c.enabled is False
        assert c.signal_only is True
        assert c.max_secs_to_close == 60.0
        assert c.min_loser_prob == 0.85
        assert c.ask_price == 0.05
        assert c.size_usdc == 2.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ADVERSARIAL_ENABLED", "true")
        monkeypatch.setenv("ADVERSARIAL_SIGNAL_ONLY", "false")
        monkeypatch.setenv("ADVERSARIAL_MAX_SECS_TO_CLOSE", "45")
        monkeypatch.setenv("ADVERSARIAL_MIN_LOSER_PROB", "0.90")
        monkeypatch.setenv("ADVERSARIAL_ASK_PRICE", "0.03")
        monkeypatch.setenv("ADVERSARIAL_SIZE_USDC", "5.0")
        c = AdversarialConfig.from_env()
        assert c.enabled is True
        assert c.signal_only is False
        assert c.max_secs_to_close == 45.0
        assert c.min_loser_prob == 0.90
        assert c.ask_price == 0.03
        assert c.size_usdc == 5.0
