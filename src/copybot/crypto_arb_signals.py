"""Quantified edge model for crypto temporal arbitrage.

Reframes the strategy from reactive momentum chasing to predictive
probability vs Polymarket mid:

1. Given accumulated spot move since bucket_start, secs_left in bucket,
   and per-symbol 1-minute volatility, compute P(close > start) under
   a normal-distribution residual-drift model.
2. Compare against Polymarket mid for the UP side. If implied prob is
   sufficiently above mid → buy UP. If sufficiently below → buy DOWN.
3. Edge is reported in probability points (0.10 = 10pp).

Per-symbol vol defaults are observed 1-min stdev in % (env-overridable
via ``CRYPTO_ARB_VOL_<SYMBOL>``).
"""
from __future__ import annotations

import math
import os

# 1-min stdev in % (typical observed values; env-overridable).
SYMBOL_VOL_PCT_PER_MIN: dict[str, float] = {
    "BTCUSDT": 0.05,
    "ETHUSDT": 0.07,
    "SOLUSDT": 0.10,
    "XRPUSDT": 0.15,
    "BNBUSDT": 0.08,
    "HYPEUSDT": 0.20,
    "DOGEUSDT": 0.18,
}

# Default min edge (probability points) — env override CRYPTO_ARB_MIN_EDGE.
DEFAULT_MIN_EDGE = 0.10


def get_sigma_pct_per_min(symbol: str) -> float:
    """Return per-symbol 1-min stdev in %, with env override.

    Looks for ``CRYPTO_ARB_VOL_<SYMBOL>`` first; falls back to baked-in
    defaults; falls back to a conservative 0.10 if symbol unknown.
    """
    env_key = f"CRYPTO_ARB_VOL_{symbol.upper()}"
    raw = os.getenv(env_key)
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return SYMBOL_VOL_PCT_PER_MIN.get(symbol.upper(), 0.10)


def get_min_edge() -> float:
    """Read CRYPTO_ARB_MIN_EDGE env (probability points). Default 0.10."""
    raw = os.getenv("CRYPTO_ARB_MIN_EDGE")
    if raw:
        try:
            v = float(raw)
            if v >= 0:
                return v
        except ValueError:
            pass
    return DEFAULT_MIN_EDGE


def implied_up_probability(
    *,
    spot_move_pct: float,
    secs_left: float,
    sigma_pct_per_min: float,
) -> float:
    """P(close > start) given current move and remaining drift time.

    Models residual move ~ N(0, sigma * sqrt(secs_left/60)).
    P(final_move > 0) = P(residual > -current_move)
                     = 1 - Phi(-current_move / sigma_remaining)
                     = Phi(current_move / sigma_remaining).
    """
    if secs_left <= 0:
        if spot_move_pct > 0:
            return 1.0
        if spot_move_pct < 0:
            return 0.0
        return 0.5
    sigma_remaining = sigma_pct_per_min * math.sqrt(secs_left / 60.0)
    if sigma_remaining <= 0:
        if spot_move_pct > 0:
            return 1.0
        if spot_move_pct < 0:
            return 0.0
        return 0.5
    z = spot_move_pct / sigma_remaining
    # CDF of standard normal via erf.
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def edge_vs_mid(
    *,
    spot_move_pct: float,
    secs_left: float,
    symbol: str,
    mid_up: float,
    threshold: float | None = None,
) -> tuple[float, str | None, float]:
    """Compute edge vs Polymarket mid.

    Returns (edge, side, p_up):
    - edge: probability points (0.20 = 20pp). 0 if no edge.
    - side: ``"Up"`` if we should buy UP, ``"Down"`` if DOWN, ``None`` if
      no edge meets threshold.
    - p_up: the implied prob of UP closing (for logging).

    Logic:
    - p_up = implied_up_probability(...)
    - If p_up > mid_up + threshold → buy UP, edge = p_up - mid_up.
    - If p_up < mid_up - threshold → buy DOWN, edge = mid_up - p_up
      (equivalently (1 - p_up) - (1 - mid_up)).
    - Else: edge = 0, side = None.
    """
    thr = threshold if threshold is not None else get_min_edge()
    sigma = get_sigma_pct_per_min(symbol)
    p_up = implied_up_probability(
        spot_move_pct=spot_move_pct,
        secs_left=secs_left,
        sigma_pct_per_min=sigma,
    )
    if p_up > mid_up + thr:
        return (p_up - mid_up, "Up", p_up)
    if p_up < mid_up - thr:
        return (mid_up - p_up, "Down", p_up)
    return (0.0, None, p_up)
