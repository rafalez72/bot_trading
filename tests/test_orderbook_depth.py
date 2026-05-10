"""Tests for the minimum orderbook depth check in clob_client.

Background (2026-05-10): live operation showed catastrophic slippage in
crypto-updown markets with thin orderbooks (~$50-100 per level). Even with
LIVE_MAX_SLIPPAGE_PCT=4%, real fills were 50-90% off target because the size
walked through 5+ price levels.

Fix: estimate_slippage now requires the top-5 levels of the relevant book
side to sum to at least LIVE_MIN_ORDERBOOK_DEPTH_USDC (default $500). Below
that, the market is rejected as `orderbook_too_thin`.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.polymarket import clob_client
from src.polymarket.clob_client import estimate_slippage


def _fake_client_returning_orderbook(orderbook: dict):
    """Mock minimal del client.get_order_book."""
    client = MagicMock()
    client.get_order_book.return_value = orderbook
    return client


def test_estimate_slippage_rejects_thin_orderbook(monkeypatch):
    """Si los primeros 5 levels suman < LIVE_MIN_ORDERBOOK_DEPTH_USDC,
    estimate_slippage debe devolver ok=False con error 'orderbook_too_thin'.

    Construimos un book donde top5 suman <$500 (thin):
      - 10 shares @ 0.4  = $4
      - 20 shares @ 0.45 = $9
      - 30 shares @ 0.5  = $15
      - 10 shares @ 0.55 = $5.5
      - 10 shares @ 0.6  = $6
      Total top5 ~ $39.50 → muy lejos de $500.
    """
    monkeypatch.setattr(clob_client, "LIVE_MIN_ORDERBOOK_DEPTH_USDC", 500.0)
    thin = {
        "asks": [
            {"price": "0.4", "size": "10"},
            {"price": "0.45", "size": "20"},
            {"price": "0.5", "size": "30"},
            {"price": "0.55", "size": "10"},
            {"price": "0.6", "size": "10"},
        ],
        "bids": [],
    }
    fake = _fake_client_returning_orderbook(thin)
    with patch.object(clob_client, "get_client", return_value=fake):
        res = estimate_slippage(
            token_id="tok-thin",
            side="BUY",
            target_size_shares=5.0,
            target_price=0.4,
        )
    assert res["ok"] is False
    assert "orderbook_too_thin" in (res.get("error") or "")
    assert res.get("depth_usdc_top5", 999) < 500


def test_estimate_slippage_accepts_deep_orderbook(monkeypatch):
    """Con un book denso (top5 > $500) y slippage razonable, debe pasar."""
    monkeypatch.setattr(clob_client, "LIVE_MIN_ORDERBOOK_DEPTH_USDC", 500.0)
    deep = {
        # top5: 500*0.5 + 500*0.51 + 500*0.52 + 500*0.53 + 500*0.54 ≈ $1300
        "asks": [
            {"price": "0.5", "size": "500"},
            {"price": "0.51", "size": "500"},
            {"price": "0.52", "size": "500"},
            {"price": "0.53", "size": "500"},
            {"price": "0.54", "size": "500"},
        ],
        "bids": [],
    }
    fake = _fake_client_returning_orderbook(deep)
    with patch.object(clob_client, "get_client", return_value=fake):
        res = estimate_slippage(
            token_id="tok-deep",
            side="BUY",
            target_size_shares=10.0,
            target_price=0.5,
        )
    assert res["ok"] is True
    assert res["error"] is None
    # VWAP debe rondar el primer nivel para size chico
    assert res["vwap"] == pytest.approx(0.5, rel=1e-6)


def test_estimate_slippage_depth_check_disabled_when_zero(monkeypatch):
    """LIVE_MIN_ORDERBOOK_DEPTH_USDC=0 desactiva el guard (back-compat)."""
    monkeypatch.setattr(clob_client, "LIVE_MIN_ORDERBOOK_DEPTH_USDC", 0.0)
    thin = {
        "asks": [
            {"price": "0.5", "size": "10"},
            {"price": "0.55", "size": "10"},
        ],
        "bids": [],
    }
    fake = _fake_client_returning_orderbook(thin)
    with patch.object(clob_client, "get_client", return_value=fake):
        res = estimate_slippage(
            token_id="tok-thin",
            side="BUY",
            target_size_shares=5.0,
            target_price=0.5,
        )
    # Sin chequeo de depth, solo importa slippage. 5 shares @ 0.5 hay liquidez,
    # VWAP=0.5, slippage=0 → ok.
    assert res["ok"] is True
