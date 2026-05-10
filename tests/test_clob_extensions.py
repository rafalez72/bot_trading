"""Tests para las extensiones de clob_client (GTC, cancel, fills, on-chain ops).

Cubre la API añadida para market_maker (N1) + spike_arb (N2) + adversarial (N3):

  1. ``place_limit_order_gtc`` — happy path: devuelve OrderResult con order_id.
  2. ``cancel_order`` — confirma cancel exitoso vs not_canceled.
  3. ``cancel_all_orders`` — bulk cancel global y filtrado por token_id.
  4. ``get_open_orders`` — parsing de la response del SDK.
  5. ``get_fills_since`` — filtrado client-side por timestamp + fallback http.
  6. ``split_position`` — params correctos del contract call (USDC, partition,
     amount_wei).
  7. ``redeem_position`` — index_set correcto (YES=1, NO=2).
  8. Error handling: 4xx/5xx + excepciones de la SDK no rompen al caller,
     devuelven None / [] / False según firma.

Estrategia de mocks:
  - ``py_clob_client_v2`` no está instalado en el host. Inyectamos stubs en
    ``sys.modules`` antes de importar el módulo bajo test, para que los
    ``from py_clob_client_v2... import ...`` lazy adentro de las funciones
    resuelvan a nuestras clases ficticias.
  - ``get_client()`` se patchea con ``MagicMock`` para controlar las
    responses del SDK directamente.
  - ``web3`` + ``eth_account`` también se mockean para split/redeem.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# -------- Stubs para py_clob_client_v2 (instalados antes de import) --------
#
# Si el SDK real está disponible (CI con Docker), no pisamos. Sino, creamos
# módulos ficticios con las clases mínimas que el código bajo test importa.

def _ensure_pyclob_stubs() -> None:
    if "py_clob_client_v2" in sys.modules:
        return

    pkg = types.ModuleType("py_clob_client_v2")
    types_mod = types.ModuleType("py_clob_client_v2.clob_types")
    builder_pkg = types.ModuleType("py_clob_client_v2.order_builder")
    constants_mod = types.ModuleType("py_clob_client_v2.order_builder.constants")
    client_mod = types.ModuleType("py_clob_client_v2.client")

    class _OrderArgs:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _OrderType:
        GTC = "GTC"
        FOK = "FOK"
        FAK = "FAK"

    class _PartialCreateOrderOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _OpenOrderParams:
        def __init__(self, **kwargs):
            self.asset_id = kwargs.get("asset_id")

    class _TradeParams:
        def __init__(self, **kwargs):
            self.after = kwargs.get("after")

    class _BalanceAllowanceParams:
        def __init__(self, **kwargs):
            pass

    class _AssetType:
        COLLATERAL = "COLLATERAL"

    class _ApiCreds:
        def __init__(self, **kwargs):
            pass

    class _ClobClient:
        def __init__(self, **kwargs):
            pass

    types_mod.OrderArgs = _OrderArgs
    types_mod.OrderType = _OrderType
    types_mod.PartialCreateOrderOptions = _PartialCreateOrderOptions
    types_mod.OpenOrderParams = _OpenOrderParams
    types_mod.TradeParams = _TradeParams
    types_mod.BalanceAllowanceParams = _BalanceAllowanceParams
    types_mod.AssetType = _AssetType
    types_mod.ApiCreds = _ApiCreds
    constants_mod.BUY = "BUY"
    constants_mod.SELL = "SELL"
    client_mod.ClobClient = _ClobClient

    sys.modules["py_clob_client_v2"] = pkg
    sys.modules["py_clob_client_v2.clob_types"] = types_mod
    sys.modules["py_clob_client_v2.order_builder"] = builder_pkg
    sys.modules["py_clob_client_v2.order_builder.constants"] = constants_mod
    sys.modules["py_clob_client_v2.client"] = client_mod


_ensure_pyclob_stubs()

# Ahora sí podemos importar el módulo bajo test.
from src.polymarket import clob_client  # noqa: E402
from src.polymarket.clob_client import (  # noqa: E402
    cancel_all_orders,
    cancel_order,
    get_fills_since,
    get_open_orders,
    place_limit_order_gtc,
    redeem_position,
    split_position,
)


# ============================================================================
# 1. place_limit_order_gtc
# ============================================================================

def test_gtc_happy_path_returns_order_id():
    """SDK acepta create_order + post_order(GTC) → devuelve OrderResult ok=True
    con order_id, status='live' y avg_price=price.
    """
    fake_client = MagicMock()
    fake_client.create_order.return_value = {"signed": True}
    fake_client.post_order.return_value = {
        "orderID": "ord-gtc-123",
        "status": "live",
        "makingAmount": 0,
    }

    with patch.object(clob_client, "get_client", return_value=fake_client), \
         patch.object(clob_client, "_get_market_meta", return_value=None):
        result = place_limit_order_gtc(
            token_id="0xabc",
            side="BUY",
            price=0.50,
            size=10.0,
        )

    assert result.ok is True
    assert result.order_id == "ord-gtc-123"
    assert result.status == "live"
    assert result.avg_price == 0.50
    # post_order fue llamado con OrderType.GTC (string 'GTC' en el stub)
    args = fake_client.post_order.call_args.args
    assert "GTC" in args


def test_gtc_invalid_price_returns_error_no_sdk_call():
    """price <= 0 → OrderResult.ok=False sin tocar el SDK."""
    fake_client = MagicMock()
    with patch.object(clob_client, "get_client", return_value=fake_client):
        result = place_limit_order_gtc(
            token_id="0xabc", side="BUY", price=0.0, size=10.0,
        )
    assert result.ok is False
    assert "precio invalido" in (result.error or "")
    fake_client.create_order.assert_not_called()


def test_gtc_post_failure_returns_error():
    """Si client.post_order tira excepción → OrderResult.ok=False, error con
    motivo. NO se loguea como éxito.
    """
    fake_client = MagicMock()
    fake_client.create_order.return_value = {"signed": True}
    fake_client.post_order.side_effect = Exception("server 500")

    with patch.object(clob_client, "get_client", return_value=fake_client), \
         patch.object(clob_client, "_get_market_meta", return_value=None):
        result = place_limit_order_gtc(
            token_id="0xabc", side="BUY", price=0.50, size=10.0,
        )

    assert result.ok is False
    assert "post_order" in (result.error or "")


# ============================================================================
# 2. cancel_order
# ============================================================================

def test_cancel_order_success():
    """Server responde {canceled: [id]} → True."""
    fake_client = MagicMock()
    fake_client.cancel.return_value = {"canceled": ["ord-1"], "not_canceled": {}}
    with patch.object(clob_client, "get_client", return_value=fake_client):
        ok = cancel_order("ord-1")
    assert ok is True
    fake_client.cancel.assert_called_once_with("ord-1")


def test_cancel_order_not_canceled_returns_false():
    """Server responde con id en not_canceled (ya fillada) → False."""
    fake_client = MagicMock()
    fake_client.cancel.return_value = {
        "canceled": [],
        "not_canceled": {"ord-1": "already filled"},
    }
    with patch.object(clob_client, "get_client", return_value=fake_client):
        ok = cancel_order("ord-1")
    assert ok is False


def test_cancel_order_exception_swallowed():
    """SDK tira excepción (4xx, network) → False, no propaga."""
    fake_client = MagicMock()
    fake_client.cancel.side_effect = Exception("network err")
    with patch.object(clob_client, "get_client", return_value=fake_client):
        ok = cancel_order("ord-1")
    assert ok is False


def test_cancel_order_empty_id_returns_false():
    """order_id vacío → False sin llamar al SDK."""
    fake_client = MagicMock()
    with patch.object(clob_client, "get_client", return_value=fake_client):
        assert cancel_order("") is False
        assert cancel_order(None) is False  # type: ignore[arg-type]
    fake_client.cancel.assert_not_called()


# ============================================================================
# 3. cancel_all_orders
# ============================================================================

def test_cancel_all_orders_global():
    """Sin token_id → llama cancel_all() y devuelve count canceladas."""
    fake_client = MagicMock()
    fake_client.cancel_all.return_value = {
        "canceled": ["a", "b", "c"],
        "not_canceled": {},
    }
    with patch.object(clob_client, "get_client", return_value=fake_client):
        n = cancel_all_orders()
    assert n == 3
    fake_client.cancel_all.assert_called_once()
    fake_client.cancel_market_orders.assert_not_called()


def test_cancel_all_orders_filtered_by_token():
    """Con token_id → llama cancel_market_orders(market=token)."""
    fake_client = MagicMock()
    fake_client.cancel_market_orders.return_value = {
        "canceled": ["a"],
    }
    with patch.object(clob_client, "get_client", return_value=fake_client):
        n = cancel_all_orders(token_id="0xtoken")
    assert n == 1
    fake_client.cancel_market_orders.assert_called_once_with(market="0xtoken")
    fake_client.cancel_all.assert_not_called()


def test_cancel_all_orders_exception_returns_zero():
    """SDK error → 0 (defensivo, no propaga)."""
    fake_client = MagicMock()
    fake_client.cancel_all.side_effect = Exception("boom")
    with patch.object(clob_client, "get_client", return_value=fake_client):
        assert cancel_all_orders() == 0


# ============================================================================
# 4. get_open_orders
# ============================================================================

def test_get_open_orders_returns_list():
    """SDK devuelve lista plana de orders → devolvemos tal cual."""
    fake_client = MagicMock()
    orders = [
        {"id": "o1", "asset_id": "0xt1", "side": "BUY", "price": "0.50"},
        {"id": "o2", "asset_id": "0xt2", "side": "SELL", "price": "0.60"},
    ]
    fake_client.get_orders.return_value = orders
    with patch.object(clob_client, "get_client", return_value=fake_client):
        out = get_open_orders()
    assert out == orders


def test_get_open_orders_filters_by_token_id():
    """Filter client-side por asset_id si la SDK no respeta el filter."""
    fake_client = MagicMock()
    orders = [
        {"id": "o1", "asset_id": "0xt1"},
        {"id": "o2", "asset_id": "0xt2"},
    ]
    fake_client.get_orders.return_value = orders
    with patch.object(clob_client, "get_client", return_value=fake_client):
        out = get_open_orders(token_id="0xt2")
    assert len(out) == 1
    assert out[0]["id"] == "o2"


def test_get_open_orders_dict_response_unwrapped():
    """Algunas versiones del SDK envuelven en {'orders': [...]} → unwrap."""
    fake_client = MagicMock()
    fake_client.get_orders.return_value = {"orders": [{"id": "o1"}]}
    with patch.object(clob_client, "get_client", return_value=fake_client):
        out = get_open_orders()
    assert out == [{"id": "o1"}]


def test_get_open_orders_exception_returns_empty():
    """SDK error → lista vacía, no propaga."""
    fake_client = MagicMock()
    fake_client.get_orders.side_effect = Exception("auth fail")
    with patch.object(clob_client, "get_client", return_value=fake_client):
        out = get_open_orders()
    assert out == []


# ============================================================================
# 5. get_fills_since
# ============================================================================

def test_get_fills_since_filters_by_timestamp():
    """Filter client-side: solo fills con ts >= timestamp_s sobreviven."""
    fake_client = MagicMock()
    fake_client.get_trades.return_value = [
        {"id": "f1", "timestamp": 1000, "size": 10},
        {"id": "f2", "timestamp": 2000, "size": 20},
        {"id": "f3", "timestamp": 3000, "size": 30},
    ]
    with patch.object(clob_client, "get_client", return_value=fake_client):
        out = get_fills_since(2000)
    ids = [f["id"] for f in out]
    assert ids == ["f2", "f3"]


def test_get_fills_since_handles_ms_timestamps():
    """Algunos endpoints devuelven timestamp en ms — los normalizamos a s."""
    fake_client = MagicMock()
    fake_client.get_trades.return_value = [
        {"id": "f1", "timestamp": 1700_000_000_000},  # 2023 en ms
        {"id": "f2", "timestamp": 2000},               # ya en s
    ]
    with patch.object(clob_client, "get_client", return_value=fake_client):
        out = get_fills_since(1500)
    assert len(out) == 2  # ambos pasan tras normalización a seg


def test_get_fills_since_dict_response_unwrapped():
    """SDK devuelve {trades: [...]} → unwrap."""
    fake_client = MagicMock()
    fake_client.get_trades.return_value = {
        "trades": [{"id": "f1", "timestamp": 5000}],
    }
    with patch.object(clob_client, "get_client", return_value=fake_client):
        out = get_fills_since(0)
    assert out == [{"id": "f1", "timestamp": 5000}]


def test_get_fills_since_negative_timestamp_clamped():
    """timestamp_s negativo se clampa a 0 (no rompe)."""
    fake_client = MagicMock()
    fake_client.get_trades.return_value = [{"id": "f1", "timestamp": 100}]
    with patch.object(clob_client, "get_client", return_value=fake_client):
        out = get_fills_since(-9999)
    assert len(out) == 1


# ============================================================================
# 6. split_position
# ============================================================================

def _patch_w3(monkeypatch, w3_mock, account_mock):
    """Helper: simula que _w3_client devuelve (w3, account, default_addr)."""
    monkeypatch.setattr(
        clob_client, "_w3_client",
        lambda: (w3_mock, account_mock, clob_client.CONDITIONAL_TOKENS_POLYGON),
    )


def test_split_position_calls_contract_with_correct_params(monkeypatch):
    """Verifica que splitPosition se llama con (USDC, parent=0x0,
    conditionId, [1,2], amount_wei).
    """
    captured = {}

    def _build_tx(tx_args):
        captured["tx_args"] = tx_args
        return {"to": "0xCT", "data": "0xdeadbeef", "nonce": 0,
                "gas": 300_000, "gasPrice": 1, "from": "0xfunder"}

    fn_split = MagicMock()
    fn_split.return_value.build_transaction = _build_tx

    contract = MagicMock()
    contract.functions.splitPosition = MagicMock(side_effect=lambda *args: types.SimpleNamespace(
        build_transaction=lambda tx: (
            captured.setdefault("split_args", args),
            {"to": "0xCT", "data": "0xdeadbeef", "nonce": 0,
             "gas": 300_000, "gasPrice": 1, "from": "0xfunder"},
        )[1],
    ))

    w3 = MagicMock()
    w3.eth.contract.return_value = contract
    w3.to_checksum_address.side_effect = lambda x: x  # passthrough
    w3.eth.gas_price = 1
    w3.eth.get_transaction_count.return_value = 0
    raw_signed = MagicMock()
    raw_signed.rawTransaction = b"\x00signed"
    w3.eth.send_raw_transaction.return_value.hex.return_value = "0xtxsplit"

    account = MagicMock()
    account.address = "0xfunder"
    account.sign_transaction.return_value = raw_signed

    _patch_w3(monkeypatch, w3, account)
    monkeypatch.setattr(clob_client, "_get_market_meta",
                        lambda cid: {"neg_risk": False, "tick_size": "0.01", "tokens": []})

    cid = "0x" + "ab" * 32
    tx_hex = split_position(cid, size_usdc=5.0)

    assert tx_hex == "0xtxsplit"
    # Args del contract call: (USDC, parent_collection, conditionId, [1,2], amount_wei)
    split_args = captured["split_args"]
    assert split_args[0].lower() == clob_client.USDC_POLYGON.lower()
    assert split_args[1] == b"\x00" * 32
    assert split_args[2] == cid
    assert split_args[3] == [1, 2]
    assert split_args[4] == 5_000_000  # 5.0 USDC * 10^6


def test_split_position_neg_risk_uses_adapter(monkeypatch):
    """Si neg_risk=True (o meta lo indica) usa NegRiskAdapter, no ConditionalTokens."""
    target_seen = {}

    contract = MagicMock()
    fn = MagicMock()
    fn.build_transaction.return_value = {
        "to": "0x0", "data": "0x0", "nonce": 0, "gas": 300_000,
        "gasPrice": 1, "from": "0xfunder",
    }
    contract.functions.splitPosition.return_value = fn

    w3 = MagicMock()

    def _contract(address, abi):
        target_seen["addr"] = address
        return contract

    w3.eth.contract.side_effect = _contract
    w3.to_checksum_address.side_effect = lambda x: x
    w3.eth.gas_price = 1
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.send_raw_transaction.return_value.hex.return_value = "0xnr"

    account = MagicMock()
    account.address = "0xfunder"
    raw_signed = MagicMock()
    raw_signed.rawTransaction = b"\x00s"
    account.sign_transaction.return_value = raw_signed

    _patch_w3(monkeypatch, w3, account)

    cid = "0x" + "cd" * 32
    split_position(cid, size_usdc=1.0, neg_risk=True)

    assert target_seen["addr"].lower() == clob_client.NEG_RISK_ADAPTER_POLYGON.lower()


def test_split_position_invalid_condition_id_returns_none():
    """condition_id mal formateado → None (no entra al SDK)."""
    assert split_position("badcid", size_usdc=5.0) is None
    assert split_position("0xabc", size_usdc=5.0) is None  # demasiado corto


def test_split_position_zero_size_returns_none():
    """size_usdc <= 0 → None (defensivo)."""
    cid = "0x" + "ef" * 32
    assert split_position(cid, size_usdc=0) is None
    assert split_position(cid, size_usdc=-1.0) is None


def test_split_position_no_w3_client_returns_none(monkeypatch):
    """Si _w3_client devuelve None (sin private key) → None."""
    monkeypatch.setattr(clob_client, "_w3_client", lambda: None)
    cid = "0x" + "12" * 32
    assert split_position(cid, size_usdc=5.0) is None


# ============================================================================
# 7. redeem_position
# ============================================================================

def test_redeem_position_yes_uses_index_set_1(monkeypatch):
    """outcome_index=0 (YES) → indexSets=[1]."""
    captured = {}

    contract = MagicMock()

    def _redeem(*args):
        captured["redeem_args"] = args
        ns = types.SimpleNamespace()
        ns.build_transaction = lambda tx: {
            "to": "0x0", "data": "0x0", "nonce": 0, "gas": 250_000,
            "gasPrice": 1, "from": "0xfunder",
        }
        return ns

    contract.functions.redeemPositions = _redeem

    w3 = MagicMock()
    w3.eth.contract.return_value = contract
    w3.to_checksum_address.side_effect = lambda x: x
    w3.eth.gas_price = 1
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.send_raw_transaction.return_value.hex.return_value = "0xredeem_yes"

    account = MagicMock()
    account.address = "0xfunder"
    raw_signed = MagicMock()
    raw_signed.rawTransaction = b"\x00s"
    account.sign_transaction.return_value = raw_signed

    _patch_w3(monkeypatch, w3, account)
    monkeypatch.setattr(clob_client, "_get_market_meta",
                        lambda cid: {"neg_risk": False, "tick_size": "0.01", "tokens": []})

    cid = "0x" + "aa" * 32
    tx_hex = redeem_position(cid, outcome_index=0)

    assert tx_hex == "0xredeem_yes"
    # Args: (USDC, parent, conditionId, indexSets=[1])
    args = captured["redeem_args"]
    assert args[3] == [1], f"YES debe ser indexSet=[1], got {args[3]}"


def test_redeem_position_no_uses_index_set_2(monkeypatch):
    """outcome_index=1 (NO) → indexSets=[2]."""
    captured = {}

    contract = MagicMock()

    def _redeem(*args):
        captured["redeem_args"] = args
        ns = types.SimpleNamespace()
        ns.build_transaction = lambda tx: {
            "to": "0x0", "data": "0x0", "nonce": 0, "gas": 250_000,
            "gasPrice": 1, "from": "0xfunder",
        }
        return ns

    contract.functions.redeemPositions = _redeem

    w3 = MagicMock()
    w3.eth.contract.return_value = contract
    w3.to_checksum_address.side_effect = lambda x: x
    w3.eth.gas_price = 1
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.send_raw_transaction.return_value.hex.return_value = "0xredeem_no"

    account = MagicMock()
    account.address = "0xfunder"
    raw_signed = MagicMock()
    raw_signed.rawTransaction = b"\x00s"
    account.sign_transaction.return_value = raw_signed

    _patch_w3(monkeypatch, w3, account)
    monkeypatch.setattr(clob_client, "_get_market_meta",
                        lambda cid: {"neg_risk": False, "tick_size": "0.01", "tokens": []})

    cid = "0x" + "bb" * 32
    tx_hex = redeem_position(cid, outcome_index=1)

    assert tx_hex == "0xredeem_no"
    args = captured["redeem_args"]
    assert args[3] == [2], f"NO debe ser indexSet=[2], got {args[3]}"


def test_redeem_position_invalid_outcome_index_returns_none():
    """outcome_index fuera de {0,1} → None."""
    cid = "0x" + "cc" * 32
    assert redeem_position(cid, outcome_index=2) is None
    assert redeem_position(cid, outcome_index=-1) is None


def test_redeem_position_invalid_condition_id_returns_none():
    """condition_id mal formado → None."""
    assert redeem_position("notacid", outcome_index=0) is None
    assert redeem_position("0x", outcome_index=0) is None


# ============================================================================
# 8. Error handling 4xx/5xx
# ============================================================================

def test_gtc_no_client_returns_error():
    """get_client() devuelve None (creds inválidas) → OrderResult.ok=False."""
    with patch.object(clob_client, "get_client", return_value=None):
        result = place_limit_order_gtc(
            token_id="0xabc", side="BUY", price=0.50, size=10.0,
        )
    assert result.ok is False
    assert "no configurado" in (result.error or "").lower()


def test_cancel_all_orders_no_client_returns_zero():
    """get_client() None → 0."""
    with patch.object(clob_client, "get_client", return_value=None):
        assert cancel_all_orders() == 0


def test_get_open_orders_no_client_returns_empty():
    """get_client() None → []."""
    with patch.object(clob_client, "get_client", return_value=None):
        assert get_open_orders() == []


def test_get_fills_since_no_client_returns_empty():
    """get_client() None → []."""
    with patch.object(clob_client, "get_client", return_value=None):
        assert get_fills_since(0) == []


def test_split_position_contract_failure_returns_none(monkeypatch):
    """Si build_transaction tira (RPC down, gas estimation fail) → None."""
    contract = MagicMock()
    fn = MagicMock()
    fn.build_transaction.side_effect = Exception("rpc 503")
    contract.functions.splitPosition.return_value = fn

    w3 = MagicMock()
    w3.eth.contract.return_value = contract
    w3.to_checksum_address.side_effect = lambda x: x
    w3.eth.gas_price = 1
    w3.eth.get_transaction_count.return_value = 0

    account = MagicMock()
    account.address = "0xfunder"
    _patch_w3(monkeypatch, w3, account)
    monkeypatch.setattr(clob_client, "_get_market_meta",
                        lambda cid: {"neg_risk": False, "tick_size": "0.01", "tokens": []})

    cid = "0x" + "dd" * 32
    assert split_position(cid, size_usdc=5.0) is None
