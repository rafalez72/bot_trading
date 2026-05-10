"""Tests para src/polymarket/clob_client.py — LIMIT_FOK orders.

Cubre:
  1. compute_limit_price: cálculo correcto en BUY y SELL.
  2. place_market_order(order_type="LIMIT_FOK"): cuando FOK no fillea,
     devuelve OrderResult.ok=False con status='cancelled_fok' y NO retry.
  3. LIMIT_FOK respeta LIVE_MAX_SLIPPAGE_PCT — el limit price enviado al
     server matchea el cap configurado (no hay forma de pagar peor).

Mockea por completo `get_client()` y `estimate_slippage()` para no tocar
red, y mockea `_build_and_post` para inyectar respuestas controladas.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.polymarket import clob_client
from src.polymarket.clob_client import compute_limit_price, place_market_order


# ---------- Test 1: compute_limit_price ----------

def test_compute_limit_price_buy_adds_slippage():
    """BUY: limit = target * (1 + max_slippage). Garantía: jamás pagamos
    más que el cap.
    """
    limit = compute_limit_price(target_price=0.50, side="BUY", max_slippage_pct=0.03)
    assert limit == pytest.approx(0.515, abs=1e-9)


def test_compute_limit_price_sell_subtracts_slippage():
    """SELL: limit = target * (1 - max_slippage). Garantía: jamás
    recibimos menos que el cap.
    """
    limit = compute_limit_price(target_price=0.80, side="SELL", max_slippage_pct=0.03)
    assert limit == pytest.approx(0.776, abs=1e-9)


def test_compute_limit_price_invalid_or_zero():
    """Edge: target<=0 → devuelve target sin tocar (guard upstream)."""
    assert compute_limit_price(0.0, "BUY", 0.03) == 0.0
    # max_slippage negativo se trunca a 0 (defensivo)
    limit = compute_limit_price(0.50, "BUY", -0.10)
    assert limit == 0.50


# ---------- Test 2: FOK no-fill cancela sin retry ----------

def test_limit_fok_no_fill_returns_cancelled_no_retry():
    """Cuando el server no puede fillear el 100% al limit, devuelve
    makingAmount=0 (o status=unmatched). place_market_order debe:
      - devolver ok=False con status='cancelled_fok'
      - NO hacer retry (a diferencia del path MARKET que hace bump+retry)

    Verificamos que _build_and_post se llama EXACTAMENTE 1 vez.
    """
    fake_client = MagicMock()
    # Pre-check pasa (slippage ok)
    slip_ok = {"ok": True, "vwap": 0.50, "slippage_pct": 0.01,
               "available_shares": 100.0, "error": None}
    # FOK no fillea: makingAmount=0
    no_fill_resp = {"status": "unmatched", "makingAmount": 0,
                    "orderID": "ord-fok-1"}

    with patch.object(clob_client, "get_client", return_value=fake_client), \
         patch.object(clob_client, "estimate_slippage", return_value=slip_ok), \
         patch.object(clob_client, "_get_market_meta", return_value=None), \
         patch.object(clob_client, "_build_and_post",
                      return_value=(True, no_fill_resp)) as mock_post:
        result = place_market_order(
            token_id="0xabc", side="BUY", size_usdc=10.0, price=0.50,
            order_type="LIMIT_FOK",
        )

    assert result.ok is False, "FOK no-fill DEBE devolver ok=False"
    assert result.status == "cancelled_fok"
    # NO retry: _build_and_post llamado solo 1 vez (a diferencia de MARKET
    # que llama 2 veces — primera + retry bumpeada).
    assert mock_post.call_count == 1, (
        f"FOK no debe hacer retry, llamó {mock_post.call_count} veces"
    )
    # Verificamos que se llamó con order_type_kind="FOK"
    assert mock_post.call_args.kwargs.get("order_type_kind") == "FOK"


# ---------- Test 3: limit_price respeta LIVE_MAX_SLIPPAGE_PCT ----------

def test_limit_fok_respects_max_slippage_cap():
    """El limit_price enviado a _build_and_post DEBE ser
    target * (1 + LIVE_MAX_SLIPPAGE_PCT) en BUY (cap superior estricto).

    Garantía operacional: aún si un MEV/sniper bot mueve el orderbook
    entre el pre-check y el fill, el server cancelará en vez de
    ejecutar a precio peor que el cap.
    """
    fake_client = MagicMock()
    slip_ok = {"ok": True, "vwap": 0.50, "slippage_pct": 0.01,
               "available_shares": 100.0, "error": None}
    fok_full_fill = {
        "status": "matched", "makingAmount": 10.0, "orderID": "ord-fok-2",
        "transactionHash": "0xdeadbeef",
    }

    # Forzamos un slippage cap conocido para que el assert sea determinístico
    target_price = 0.50
    max_slip = 0.04  # 4%
    expected_limit = target_price * (1 + max_slip)  # = 0.52

    captured: dict = {}

    def _capture(*args, **kwargs):
        captured["price"] = kwargs.get("price")
        captured["order_type_kind"] = kwargs.get("order_type_kind")
        return (True, fok_full_fill)

    with patch.object(clob_client, "get_client", return_value=fake_client), \
         patch.object(clob_client, "estimate_slippage", return_value=slip_ok), \
         patch.object(clob_client, "_get_market_meta", return_value=None), \
         patch.object(clob_client, "LIVE_MAX_SLIPPAGE_PCT", max_slip), \
         patch.object(clob_client, "_build_and_post", side_effect=_capture):
        result = place_market_order(
            token_id="0xabc", side="BUY", size_usdc=10.0,
            price=target_price, order_type="LIMIT_FOK",
        )

    assert result.ok is True
    assert captured["order_type_kind"] == "FOK"
    # El limit_price enviado debe ser <= expected_limit (puede haberse
    # redondeado a 2 decimales por el tick del market) y > target_price.
    sent_limit = captured["price"]
    assert sent_limit > target_price, (
        f"LIMIT_FOK BUY debe enviar price > target ({sent_limit} <= {target_price})"
    )
    assert sent_limit <= expected_limit + 1e-9, (
        f"LIMIT_FOK BUY price {sent_limit} excede cap {expected_limit}"
    )
    # Avg price reportado = limit (en FOK fill 100% al limit).
    assert result.avg_price == sent_limit
