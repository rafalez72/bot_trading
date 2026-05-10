"""Tests for the realistic dry-run fill in clob_client.

Background (2026-05-10): live operation showed 0% win-rate while dry-run
reported 86%. The cause was that `place_market_order(... dry_run=True)`
returned the midpoint as `avg_price` — the simulated PnL was calculated as
if every fill landed exactly at the target price, while real fills walked
into 50-90% slippage in thin orderbooks.

Fix: when dry_run=True the function now consults the real orderbook
(`estimate_slippage`) and uses the VWAP of the walk as the simulated
fill price. The dry-run is now predictive of live performance.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.polymarket import clob_client
from src.polymarket.clob_client import OrderResult, place_market_order


def _fake_client_returning_orderbook(orderbook: dict):
    """Mock minimal del client.get_order_book."""
    client = MagicMock()
    client.get_order_book.return_value = orderbook
    return client


def test_dry_run_uses_real_orderbook_vwap_not_midpoint():
    """En dry_run, place_market_order debe leer el orderbook real
    (estimate_slippage) y devolver avg_price=VWAP del book, NO el target.

    Pre-2026-05-10: dry_run devolvía avg_price=price (midpoint), inflando los
    PnL simulados. La diferencia entre dry-run y live causó que el user
    perdiera $76 confiando en una simulación que no era predictiva.
    """
    # Book: para BUY caminamos asks. 5 shares @ 0.5, 10 shares @ 0.505.
    # Si pedimos 10 shares: VWAP = (5*0.5 + 5*0.505)/10 = 0.5025 (slippage 0.5%)
    # Sub LIVE_MAX_SLIPPAGE_PCT=3% → pasa, podemos ver el VWAP en avg_price.
    book = {
        "asks": [
            {"price": "0.5", "size": "5"},
            {"price": "0.505", "size": "10"},
        ],
        "bids": [],
    }
    fake = _fake_client_returning_orderbook(book)
    with patch.object(clob_client, "get_client", return_value=fake):
        # 10 shares @ target=0.5 → size_usdc = 5.0
        res = place_market_order(
            token_id="tok-vwap",
            side="BUY",
            size_usdc=5.0,
            price=0.5,
            dry_run=True,
            condition_id=None,
        )
    assert res.ok is True
    # avg_price ya NO es 0.5 (midpoint) — es el VWAP real del book
    assert res.avg_price == pytest.approx(0.5025, rel=1e-3), (
        f"Esperaba VWAP=0.5025, got {res.avg_price}. Si es 0.5 (target), "
        "el dry-run sigue devolviendo midpoint y no es predictivo."
    )


def test_dry_run_aborts_when_no_liquidity():
    """Si dry_run lee el orderbook y no alcanza la liquidez al target shares,
    debe devolver ok=False igual que el path de live. Sin esto, dry-run
    aprobaría trades que en live se rechazan, perpetuando la disonancia.
    """
    # Book con SOLO 2 shares disponibles, pidiendo 10 → liquidez insuficiente
    insuf = {
        "asks": [{"price": "0.5", "size": "2"}],
        "bids": [],
    }
    fake = _fake_client_returning_orderbook(insuf)
    with patch.object(clob_client, "get_client", return_value=fake):
        res = place_market_order(
            token_id="tok-empty",
            side="BUY",
            size_usdc=5.0,
            price=0.5,
            dry_run=True,
        )
    assert res.ok is False
    # Cualquier mensaje que mencione el pre-check / liquidez es válido
    assert "pre-check" in (res.error or "") or "liquidez" in (res.error or "")


def test_dry_run_skip_slippage_check_falls_back_to_target():
    """skip_slippage_check=True bypass el orderbook lookup en dry-run y
    devuelve avg_price=target (back-compat para callers que no querían el
    nuevo comportamiento).
    """
    # client never queried because skip_slippage_check=True
    with patch.object(clob_client, "get_client") as get_client_mock:
        get_client_mock.side_effect = AssertionError("should not be called")
        res = place_market_order(
            token_id="tok",
            side="BUY",
            size_usdc=5.0,
            price=0.42,
            dry_run=True,
            skip_slippage_check=True,
        )
    assert res.ok is True
    assert res.avg_price == pytest.approx(0.42)
