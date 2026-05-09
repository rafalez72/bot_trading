"""Unit tests for the crypto_arb signal/edge math."""
from __future__ import annotations

import math

import pytest

from src.copybot.crypto_arb_signals import (
    DEFAULT_MIN_EDGE,
    SYMBOL_VOL_PCT_PER_MIN,
    edge_vs_mid,
    get_min_edge,
    get_sigma_pct_per_min,
    implied_up_probability,
)


# ----- implied_up_probability -----

def test_implied_up_probability_zero_move_is_half():
    p = implied_up_probability(spot_move_pct=0.0, secs_left=120, sigma_pct_per_min=0.1)
    assert p == pytest.approx(0.5, abs=1e-9)


def test_implied_up_probability_tiny_positive_move_above_half():
    p = implied_up_probability(spot_move_pct=1e-6, secs_left=120, sigma_pct_per_min=0.1)
    assert 0.5 < p < 0.5001


def test_implied_up_probability_large_positive_close_to_one():
    # +5% in 30s with sigma=0.1%/min is *enormous* (~70 sigmas).
    p = implied_up_probability(spot_move_pct=5.0, secs_left=30, sigma_pct_per_min=0.1)
    assert p > 0.9999


def test_implied_up_probability_large_negative_close_to_zero():
    p = implied_up_probability(spot_move_pct=-5.0, secs_left=30, sigma_pct_per_min=0.1)
    assert p < 0.0001


def test_implied_up_probability_secs_left_zero_edge_cases():
    # No more time; move sign decides outcome deterministically.
    assert implied_up_probability(spot_move_pct=0.5, secs_left=0, sigma_pct_per_min=0.1) == 1.0
    assert implied_up_probability(spot_move_pct=-0.5, secs_left=0, sigma_pct_per_min=0.1) == 0.0
    assert implied_up_probability(spot_move_pct=0.0, secs_left=0, sigma_pct_per_min=0.1) == 0.5


def test_implied_up_probability_negative_secs_left_safe():
    # Defensive: negative secs_left should not crash.
    p = implied_up_probability(spot_move_pct=0.1, secs_left=-5, sigma_pct_per_min=0.1)
    assert p == 1.0


def test_implied_up_probability_zero_sigma_safe():
    # Zero/negative sigma → degenerate but non-crashing branch.
    assert implied_up_probability(spot_move_pct=0.5, secs_left=60, sigma_pct_per_min=0.0) == 1.0
    assert implied_up_probability(spot_move_pct=-0.5, secs_left=60, sigma_pct_per_min=0.0) == 0.0
    assert implied_up_probability(spot_move_pct=0.0, secs_left=60, sigma_pct_per_min=0.0) == 0.5


def test_implied_up_probability_matches_normal_cdf_one_sigma():
    # With move = sigma_remaining, p_up should equal Phi(1) ~= 0.8413.
    sigma_per_min = 0.1
    secs_left = 60.0
    sigma_remaining = sigma_per_min * math.sqrt(secs_left / 60.0)
    p = implied_up_probability(
        spot_move_pct=sigma_remaining,
        secs_left=secs_left,
        sigma_pct_per_min=sigma_per_min,
    )
    assert p == pytest.approx(0.8413447, abs=1e-4)


# ----- edge_vs_mid -----

def test_edge_vs_mid_buy_up_when_implied_above_mid():
    # Strong positive move; mid still cheap → buy UP.
    edge, side, p_up = edge_vs_mid(
        spot_move_pct=0.5,
        secs_left=30,
        symbol="BTCUSDT",
        mid_up=0.50,
        threshold=0.10,
    )
    assert side == "Up"
    assert edge > 0.10
    assert p_up > 0.60


def test_edge_vs_mid_buy_down_when_implied_below_mid():
    # Strong negative move; mid_up still rich → buy DOWN.
    edge, side, p_up = edge_vs_mid(
        spot_move_pct=-0.5,
        secs_left=30,
        symbol="BTCUSDT",
        mid_up=0.50,
        threshold=0.10,
    )
    assert side == "Down"
    assert edge > 0.10
    assert p_up < 0.40


def test_edge_vs_mid_no_edge_when_mid_already_priced_in():
    # Implied ~ Phi(0.05/0.05) = Phi(1) ~ 0.84. Mid 0.85 → diff = -0.01,
    # well within ±threshold. No edge.
    edge, side, p_up = edge_vs_mid(
        spot_move_pct=0.05,
        secs_left=60,
        symbol="BTCUSDT",
        mid_up=0.85,
        threshold=0.10,
    )
    assert side is None
    assert edge == 0.0
    assert p_up == pytest.approx(0.8413447, abs=1e-3)


def test_edge_vs_mid_zero_move_zero_edge_at_fair_mid():
    edge, side, p_up = edge_vs_mid(
        spot_move_pct=0.0,
        secs_left=60,
        symbol="ETHUSDT",
        mid_up=0.50,
        threshold=0.05,
    )
    assert side is None
    assert edge == 0.0
    assert p_up == pytest.approx(0.5, abs=1e-9)


def test_edge_vs_mid_threshold_blocks_small_edges():
    # Implied ~0.55, mid 0.50 → edge 0.05 fails threshold 0.10.
    edge, side, _ = edge_vs_mid(
        spot_move_pct=0.005,
        secs_left=30,
        symbol="BTCUSDT",
        mid_up=0.50,
        threshold=0.10,
    )
    assert side is None
    assert edge == 0.0


def test_edge_vs_mid_threshold_allows_big_edges():
    # Big negative move with mid still at 0.5 → big DOWN edge.
    edge, side, _ = edge_vs_mid(
        spot_move_pct=-0.4,
        secs_left=30,
        symbol="BTCUSDT",
        mid_up=0.50,
        threshold=0.10,
    )
    assert side == "Down"
    assert edge >= 0.30


# ----- env helpers -----

def test_get_sigma_pct_per_min_known_symbol():
    assert get_sigma_pct_per_min("BTCUSDT") == SYMBOL_VOL_PCT_PER_MIN["BTCUSDT"]
    assert get_sigma_pct_per_min("XRPUSDT") == SYMBOL_VOL_PCT_PER_MIN["XRPUSDT"]


def test_get_sigma_pct_per_min_unknown_symbol_falls_back():
    assert get_sigma_pct_per_min("FOOUSDT") == 0.10


def test_get_sigma_pct_per_min_env_override(monkeypatch):
    monkeypatch.setenv("CRYPTO_ARB_VOL_BTCUSDT", "0.42")
    assert get_sigma_pct_per_min("BTCUSDT") == 0.42


def test_get_sigma_pct_per_min_env_override_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("CRYPTO_ARB_VOL_BTCUSDT", "not-a-number")
    assert get_sigma_pct_per_min("BTCUSDT") == SYMBOL_VOL_PCT_PER_MIN["BTCUSDT"]


def test_get_min_edge_default(monkeypatch):
    monkeypatch.delenv("CRYPTO_ARB_MIN_EDGE", raising=False)
    assert get_min_edge() == DEFAULT_MIN_EDGE


def test_get_min_edge_env_override(monkeypatch):
    monkeypatch.setenv("CRYPTO_ARB_MIN_EDGE", "0.07")
    assert get_min_edge() == 0.07
