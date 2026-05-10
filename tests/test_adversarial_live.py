"""Tests para el live executor de adversarial_asks (Plan B: split + limit SELL).

Cubre:
1. ``_live_executor`` con mocks: happy path → split_position llamado, GTC
   limit SELL posteado, dict ok=True.
2. Fail path 1: market not found en gamma → ok=False reason=market_not_found.
3. Fail path 2: split_position devuelve None → ok=False reason=split_failed.
4. Fail path 3: place_limit_order_gtc devuelve OrderResult ok=False.
5. ``AdversarialAsks.evaluate_and_post`` con LIVE_MODE=True + signal_only=False
   → invoca live_executor inyectado, persiste status=open con order_id.

Mock targets:
  - ``src.polymarket.client.PolymarketClient.__aenter__/__aexit__/_get``
  - ``src.polymarket.token_resolver.resolve_token_id``
  - ``src.polymarket.clob_client.split_position`` y ``place_limit_order_gtc``

Importes try/except por si los modules CLOB no están en el entorno de tests.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.copybot.adversarial_asks import (
    AdversarialAsks,
    AdversarialConfig,
    SIDE_DOWN,
    SIDE_UP,
    STATUS_OPEN,
    _live_executor,
)


# Helper: configura test rápido.
def _mk_config(**overrides) -> AdversarialConfig:
    base = AdversarialConfig(
        enabled=True,
        signal_only=False,  # live mode
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


# Helper: fakea PolymarketClient async ctx manager con _get controlable.
class _FakePolyClient:
    def __init__(self, gamma_response: Any):
        self._gamma = gamma_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _get(self, url, params=None):
        return self._gamma


# =============================================================
# Test 1 — happy path: live_executor compone split + limit SELL
# =============================================================

@pytest.mark.asyncio
async def test_live_executor_happy_path_split_and_limit_sell():
    """Setup completo → split_position invocado, limit SELL GTC posteado."""
    setup = {
        "bucket_slug": "btc-updown-5m-1777505400",
        "loser_side": SIDE_DOWN,
        "ask_price": 0.05,
        "size_usdc": 2.0,
    }

    cid = "0x" + "ab" * 32
    fake_market = [{"conditionId": cid, "slug": setup["bucket_slug"]}]
    fake_client_ctx = _FakePolyClient(fake_market)

    fake_order_result = MagicMock(ok=True, order_id="ORD-LIVE-1", error=None)

    with patch("src.polymarket.client.PolymarketClient",
               return_value=fake_client_ctx), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=AsyncMock(return_value="TOKEN_DOWN_HEX")), \
         patch("src.polymarket.clob_client.split_position",
               return_value="0xsplittxhash") as mock_split, \
         patch("src.polymarket.clob_client.place_limit_order_gtc",
               return_value=fake_order_result) as mock_limit:
        result = await _live_executor(setup)

    assert result["ok"] is True, result
    assert result["order_id"] == "ORD-LIVE-1"
    assert result["split_tx_hash"] == "0xsplittxhash"
    assert result["token_id"] == "TOKEN_DOWN_HEX"

    # split_position llamado con (cid, size_usdc).
    mock_split.assert_called_once()
    args = mock_split.call_args
    assert args.args[0] == cid
    assert args.args[1] == pytest.approx(2.0)

    # place_limit_order_gtc: side='SELL', price=ask_price, GTC (ttl_s=None).
    mock_limit.assert_called_once()
    kwargs = mock_limit.call_args.kwargs
    assert kwargs["token_id"] == "TOKEN_DOWN_HEX"
    assert kwargs["side"] == "SELL"
    assert kwargs["price"] == pytest.approx(0.05)
    assert kwargs["ttl_s"] is None
    assert kwargs["condition_id"] == cid
    # size en SHARES = size_usdc / ask_price = 2.0 / 0.05 = 40.0
    assert kwargs["size"] == pytest.approx(40.0)


# =============================================================
# Test 2 — gamma devuelve vacío → market_not_found
# =============================================================

@pytest.mark.asyncio
async def test_live_executor_market_not_found_returns_error():
    setup = {
        "bucket_slug": "ghost-updown-5m-9999",
        "loser_side": SIDE_UP,
        "ask_price": 0.05,
        "size_usdc": 2.0,
    }
    fake_client_ctx = _FakePolyClient([])  # gamma devuelve lista vacía

    with patch("src.polymarket.client.PolymarketClient",
               return_value=fake_client_ctx), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=AsyncMock(return_value=None)), \
         patch("src.polymarket.clob_client.split_position") as mock_split:
        result = await _live_executor(setup)

    assert result["ok"] is False
    assert result["error"] == "market_not_found"
    # split NO se llama si no hay market.
    mock_split.assert_not_called()


# =============================================================
# Test 3 — split_position falla → ok=False, sin limit post
# =============================================================

@pytest.mark.asyncio
async def test_live_executor_split_failure_returns_error():
    setup = {
        "bucket_slug": "eth-updown-5m-1777505400",
        "loser_side": SIDE_DOWN,
        "ask_price": 0.04,
        "size_usdc": 1.0,
    }
    cid = "0x" + "cd" * 32
    fake_client_ctx = _FakePolyClient([{"conditionId": cid}])

    with patch("src.polymarket.client.PolymarketClient",
               return_value=fake_client_ctx), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=AsyncMock(return_value="TOKEN_X")), \
         patch("src.polymarket.clob_client.split_position",
               return_value=None), \
         patch("src.polymarket.clob_client.place_limit_order_gtc") as mock_limit:
        result = await _live_executor(setup)

    assert result["ok"] is False
    assert result["error"] == "split_failed"
    mock_limit.assert_not_called()


# =============================================================
# Test 4 — limit post falla post-split → ok=False con tx_hash
# =============================================================

@pytest.mark.asyncio
async def test_live_executor_limit_post_failure_after_split():
    setup = {
        "bucket_slug": "sol-updown-5m-1777505400",
        "loser_side": SIDE_UP,
        "ask_price": 0.06,
        "size_usdc": 3.0,
    }
    cid = "0x" + "ef" * 32
    fake_client_ctx = _FakePolyClient([{"conditionId": cid}])
    fake_failed_result = MagicMock(ok=False, order_id=None, error="rejected_400")

    with patch("src.polymarket.client.PolymarketClient",
               return_value=fake_client_ctx), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=AsyncMock(return_value="TOKEN_UP")), \
         patch("src.polymarket.clob_client.split_position",
               return_value="0xspltxhash"), \
         patch("src.polymarket.clob_client.place_limit_order_gtc",
               return_value=fake_failed_result):
        result = await _live_executor(setup)

    assert result["ok"] is False
    assert "limit_post_failed" in result["error"]
    assert "rejected_400" in result["error"]
    # split_tx_hash debe quedar en el resultado pa que el caller sepa que
    # mintearon shares (necesita reconciliation o redeem manual).
    assert result["split_tx_hash"] == "0xspltxhash"
    assert result["token_id"] == "TOKEN_UP"


# =============================================================
# Test 5 — invalid setup variants
# =============================================================

@pytest.mark.asyncio
async def test_live_executor_invalid_setup_short_circuits():
    # Slug vacío.
    r1 = await _live_executor({
        "bucket_slug": "", "loser_side": SIDE_UP,
        "ask_price": 0.05, "size_usdc": 2.0,
    })
    assert r1["ok"] is False
    assert r1["error"] == "invalid_setup"

    # Loser side raro.
    r2 = await _live_executor({
        "bucket_slug": "btc-updown-5m-1", "loser_side": "Maybe",
        "ask_price": 0.05, "size_usdc": 2.0,
    })
    assert r2["ok"] is False
    assert r2["error"] == "invalid_setup"

    # Ask price <= 0.
    r3 = await _live_executor({
        "bucket_slug": "btc-updown-5m-1", "loser_side": SIDE_DOWN,
        "ask_price": 0.0, "size_usdc": 2.0,
    })
    assert r3["ok"] is False
    assert r3["error"] == "invalid_amounts"

    # Size <= 0.
    r4 = await _live_executor({
        "bucket_slug": "btc-updown-5m-1", "loser_side": SIDE_DOWN,
        "ask_price": 0.05, "size_usdc": -1.0,
    })
    assert r4["ok"] is False
    assert r4["error"] == "invalid_amounts"


# =============================================================
# Test 6 — AdversarialAsks.evaluate_and_post llama live_executor
#          cuando LIVE_MODE=True AND signal_only=False
# =============================================================

@pytest.mark.asyncio
async def test_evaluate_and_post_uses_live_executor_when_live_mode(isolated_db):
    """Wire-up: live_path=True → live_executor inyectado dispara, NO post_ask_hook."""
    anchor = 1_777_505_400
    bucket_end = anchor + 30

    captured_setups: list[dict] = []
    captured_hook_calls: list[tuple] = []

    async def fake_live_executor(setup: dict) -> dict:
        captured_setups.append(setup)
        return {
            "ok": True, "order_id": "ORD-LIVE-X",
            "split_tx_hash": "0xtx", "token_id": "TOKEN_DOWN", "error": None,
        }

    async def fake_post_hook(*args, **kwargs):
        captured_hook_calls.append((args, kwargs))
        return "SHOULD_NOT_BE_USED"

    bot = AdversarialAsks(
        config=_mk_config(signal_only=False),
        post_ask_hook=fake_post_hook,
        live_executor=fake_live_executor,
        now_fn=lambda: anchor,
    )
    bot.spot_history["BTCUSDT"] = [
        ((bucket_end - 300) * 1000, 70_000.0),
        (anchor * 1000, 70_700.0),  # +1% → P(Up) ~ 0.99 → loser=Down
    ]

    market = {
        "slug": f"btc-updown-5m-{bucket_end}",
        "end_ts": bucket_end,
        "clobTokenIds": ["TOKEN_UP", "TOKEN_DOWN"],
    }

    # Patcheamos LIVE_MODE → True para forzar el path live.
    with patch("src.config.LIVE_MODE", True):
        decision = await bot.evaluate_and_post(market)

    assert decision is not None
    assert decision.post is True
    assert decision.loser_side == SIDE_DOWN

    # Live executor invocado UNA vez con setup correcto.
    assert len(captured_setups) == 1
    s = captured_setups[0]
    assert s["bucket_slug"] == f"btc-updown-5m-{bucket_end}"
    assert s["loser_side"] == SIDE_DOWN
    assert s["ask_price"] == pytest.approx(0.05)
    assert s["size_usdc"] == pytest.approx(2.0)

    # post_ask_hook NO se llamó (live path bypass del hook tipado).
    assert captured_hook_calls == []

    # DB row con order_id y status='open'.
    from src.db.schema import db
    with db() as conn:
        rows = conn.execute(
            "SELECT order_id, status, loser_side FROM adversarial_orders"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["order_id"] == "ORD-LIVE-X"
    assert rows[0]["status"] == STATUS_OPEN
    assert rows[0]["loser_side"] == SIDE_DOWN


# =============================================================
# Test 7 — live_executor failure NO persiste row (early return)
# =============================================================

@pytest.mark.asyncio
async def test_evaluate_and_post_skips_persist_on_live_failure(isolated_db):
    # init_schema explícito para tabla exista aunque record_signal nunca corra.
    from src.copybot.adversarial_asks import init_schema
    init_schema()

    anchor = 1_777_505_400
    bucket_end = anchor + 30

    async def failing_live_executor(setup: dict) -> dict:
        return {"ok": False, "error": "split_failed", "order_id": None}

    bot = AdversarialAsks(
        config=_mk_config(signal_only=False),
        live_executor=failing_live_executor,
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

    with patch("src.config.LIVE_MODE", True):
        await bot.evaluate_and_post(market)

    # No row porque el live_executor falló antes de _posted.add + record_signal.
    from src.db.schema import db
    with db() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM adversarial_orders"
        ).fetchall()
    assert int(rows[0]["n"]) == 0


# =============================================================
# Test 8 — paper path (signal_only=True) NO llama live_executor
# =============================================================

@pytest.mark.asyncio
async def test_signal_only_path_does_not_call_live_executor(isolated_db):
    anchor = 1_777_505_400
    bucket_end = anchor + 30

    live_calls: list[dict] = []

    async def tracked_live(setup: dict) -> dict:
        live_calls.append(setup)
        return {"ok": True, "order_id": "X"}

    captured_hook: list[tuple] = []

    async def fake_hook(token_id, side, price, size):
        captured_hook.append((token_id, side, price, size))
        return None  # signal_only convention

    bot = AdversarialAsks(
        config=_mk_config(signal_only=True),  # ¡signal_only ON!
        post_ask_hook=fake_hook,
        live_executor=tracked_live,
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

    # Even con LIVE_MODE=True, signal_only domina → no live_executor.
    with patch("src.config.LIVE_MODE", True):
        await bot.evaluate_and_post(market)

    assert live_calls == [], "signal_only=True debe bypassear live_executor"
    assert len(captured_hook) == 1
    assert captured_hook[0] == ("TOKEN_DOWN", "SELL", 0.05, 2.0)
