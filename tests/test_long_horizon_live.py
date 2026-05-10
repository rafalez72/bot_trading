"""Tests para el live executor de long_horizon_arb (place_limit_order_gtc).

Cubre:
1. Happy path: signal completo → resolve_token_id + place_limit_order_gtc BUY,
   ttl=3600, side correcto, size en SHARES = bet_usdc/entry_mid.
2. Token not found → ok=False con error claro.
3. place_limit_order_gtc devuelve ok=False → propaga error.
4. Signal con bet_usdc=0 / entry_mid<=0 → invalid_signal short-circuit.
5. Filled=False (sin makingAmount) cuando GTC posteada pero no matchea aún.
6. LongHorizonArb.cycle con executor=_live_order_executor + mocks → end-to-end.

Mock targets:
  - ``src.polymarket.client.PolymarketClient`` (async ctx manager)
  - ``src.polymarket.token_resolver.resolve_token_id``
  - ``src.polymarket.clob_client.place_limit_order_gtc``
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.copybot.long_horizon_arb import (
    LongHorizonArb,
    LongHorizonConfig,
    _live_order_executor,
    init_table,
)


# Helper: fake PolymarketClient async ctx manager (no toca red).
class _FakePolyClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _mk_signal(**overrides) -> dict:
    base = {
        "slug": "bitcoin-200k-by-end-2026",
        "market_slug": "bitcoin-200k-by-end-2026",
        "side": "Up",
        "outcome_index": 0,
        "underlying": "btc",
        "entry_mid": 0.40,
        "limit_price": 0.40,
        "bet_usdc": 10.0,
        "size_usdc": 10.0,
        "ttl_s": 3600,
        "edge_pp": 6.0,
    }
    base.update(overrides)
    return base


def _mk_market(slug: str, mid_up: float = 0.40, end_offset_s: int = 180 * 86400,
               liquidity: float = 80_000.0) -> dict:
    end_ts = int(time.time()) + end_offset_s
    from datetime import datetime, timezone
    end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "slug": slug,
        "conditionId": "0xCID",
        "outcomePrices": json.dumps([str(mid_up), str(1.0 - mid_up)]),
        "liquidity": liquidity,
        "volume": 100_000.0,
        "endDate": end_iso,
        "closed": False,
    }


# =============================================================
# Test 1 — happy path: signal Up → BUY GTC token_id=outcome_index 0
# =============================================================

@pytest.mark.asyncio
async def test_live_order_executor_happy_path_buy_up():
    signal = _mk_signal(side="Up", entry_mid=0.40, bet_usdc=10.0, ttl_s=3600)

    fake_result = MagicMock(
        ok=True, order_id="ORD-LH-1", error=None,
        filled_size=0.0, avg_price=0.40, raw={"status": "live"},
    )

    with patch("src.polymarket.client.PolymarketClient",
               return_value=_FakePolyClient()), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=AsyncMock(return_value="TOKEN_UP_HEX")), \
         patch("src.polymarket.clob_client.place_limit_order_gtc",
               return_value=fake_result) as mock_limit:
        out = await _live_order_executor(signal)

    assert out["ok"] is True
    assert out["order_id"] == "ORD-LH-1"
    assert out["token_id"] == "TOKEN_UP_HEX"
    # filled_size=0 → posted but not yet matched → filled=False.
    assert out["filled"] is False
    assert out["fill_price"] is None

    mock_limit.assert_called_once()
    kwargs = mock_limit.call_args.kwargs
    assert kwargs["token_id"] == "TOKEN_UP_HEX"
    assert kwargs["side"] == "BUY"
    assert kwargs["price"] == pytest.approx(0.40)
    assert kwargs["ttl_s"] == 3600
    # size en SHARES = bet_usdc / entry_mid = 10/0.40 = 25.0
    assert kwargs["size"] == pytest.approx(25.0)


# =============================================================
# Test 2 — Down side → outcome_index=1, resuelve token NO
# =============================================================

@pytest.mark.asyncio
async def test_live_order_executor_down_side_resolves_outcome_one():
    signal = _mk_signal(side="Down", outcome_index=1, entry_mid=0.20, bet_usdc=5.0)

    fake_result = MagicMock(
        ok=True, order_id="ORD-LH-2", error=None,
        filled_size=25.0, avg_price=0.20, raw={"status": "matched"},
    )

    resolve_mock = AsyncMock(return_value="TOKEN_DOWN_HEX")
    with patch("src.polymarket.client.PolymarketClient",
               return_value=_FakePolyClient()), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=resolve_mock), \
         patch("src.polymarket.clob_client.place_limit_order_gtc",
               return_value=fake_result):
        out = await _live_order_executor(signal)

    assert out["ok"] is True
    assert out["filled"] is True  # filled_size > 0
    assert out["fill_price"] == pytest.approx(0.20)
    # token_resolver llamado con outcome_index=1.
    args = resolve_mock.call_args
    assert args.args[1] == "bitcoin-200k-by-end-2026"
    assert args.args[2] == 1


# =============================================================
# Test 3 — token not found
# =============================================================

@pytest.mark.asyncio
async def test_live_order_executor_token_id_not_found():
    signal = _mk_signal()

    with patch("src.polymarket.client.PolymarketClient",
               return_value=_FakePolyClient()), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=AsyncMock(return_value=None)), \
         patch("src.polymarket.clob_client.place_limit_order_gtc") as mock_limit:
        out = await _live_order_executor(signal)

    assert out["ok"] is False
    assert out["error"] == "token_id_not_found"
    assert out["filled"] is False
    mock_limit.assert_not_called()


# =============================================================
# Test 4 — place_limit_order_gtc devuelve ok=False → propaga
# =============================================================

@pytest.mark.asyncio
async def test_live_order_executor_limit_post_failure():
    signal = _mk_signal()

    fake_failed = MagicMock(
        ok=False, order_id=None,
        error="precio invalido: 0.40", filled_size=0, avg_price=None, raw=None,
    )

    with patch("src.polymarket.client.PolymarketClient",
               return_value=_FakePolyClient()), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=AsyncMock(return_value="TOKEN_X")), \
         patch("src.polymarket.clob_client.place_limit_order_gtc",
               return_value=fake_failed):
        out = await _live_order_executor(signal)

    assert out["ok"] is False
    assert out["filled"] is False
    assert "precio invalido" in (out["error"] or "")
    assert out["token_id"] == "TOKEN_X"


# =============================================================
# Test 5 — invalid signals
# =============================================================

@pytest.mark.asyncio
async def test_live_order_executor_invalid_signals():
    # entry_mid <= 0
    out1 = await _live_order_executor(_mk_signal(entry_mid=0.0, limit_price=0.0))
    assert out1["ok"] is False
    assert out1["error"] == "invalid_signal"

    # bet_usdc <= 0
    out2 = await _live_order_executor(_mk_signal(bet_usdc=0.0, size_usdc=0.0))
    assert out2["ok"] is False
    assert out2["error"] == "invalid_signal"

    # slug vacío
    out3 = await _live_order_executor(_mk_signal(slug="", market_slug=""))
    assert out3["ok"] is False
    assert out3["error"] == "invalid_signal"


# =============================================================
# Test 6 — exception en place_limit_order_gtc capturada
# =============================================================

@pytest.mark.asyncio
async def test_live_order_executor_handles_clob_exception():
    signal = _mk_signal()

    with patch("src.polymarket.client.PolymarketClient",
               return_value=_FakePolyClient()), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=AsyncMock(return_value="TOKEN_Y")), \
         patch("src.polymarket.clob_client.place_limit_order_gtc",
               side_effect=RuntimeError("CLOB down")):
        out = await _live_order_executor(signal)

    assert out["ok"] is False
    assert "limit_post_exception" in out["error"]
    assert "CLOB down" in out["error"]
    assert out["token_id"] == "TOKEN_Y"


# =============================================================
# Test 7 — LongHorizonArb.cycle wired con _live_order_executor
# =============================================================

@pytest.mark.asyncio
async def test_long_horizon_arb_cycle_with_live_executor(isolated_db):
    init_table()
    cfg = LongHorizonConfig(
        enabled=True, min_liq_usdc=30_000.0, min_edge_pct=2.0,
        bet_usdc=10.0, max_mid_target=0.85,
    )
    market = _mk_market(
        slug="bitcoin-200k-by-end-2026", mid_up=0.40,
        liquidity=80_000.0, end_offset_s=180 * 86400,
    )

    async def _markets_provider() -> list[dict]:
        return [market]

    def _spot(symbol: str, ts: int) -> float | None:
        if symbol != "BTCUSDT":
            return None
        now = int(time.time())
        if (now - ts) >= 1800:
            return 100.0
        return 105.0  # +5% move

    fake_result = MagicMock(
        ok=True, order_id="ORD-LIVE-LH", error=None,
        filled_size=25.0, avg_price=0.40, raw={"status": "matched"},
    )

    with patch("src.polymarket.client.PolymarketClient",
               return_value=_FakePolyClient()), \
         patch("src.polymarket.token_resolver.resolve_token_id",
               new=AsyncMock(return_value="TOKEN_BTC_UP")), \
         patch("src.polymarket.clob_client.place_limit_order_gtc",
               return_value=fake_result):
        arb = LongHorizonArb(
            cfg, markets_provider=_markets_provider,
            spot_provider=_spot, order_executor=_live_order_executor,
        )
        results = await arb.cycle()

    assert len(results) == 1
    res = results[0]
    assert res["result"]["ok"] is True
    assert res["result"]["order_id"] == "ORD-LIVE-LH"
    assert res["result"]["filled"] is True

    # Persist en DB con status='filled' y order_id correcto.
    from src.db.schema import db
    with db() as conn:
        rows = conn.execute(
            "SELECT market_slug, order_id, status, fill_price "
            "FROM long_horizon_trades ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["market_slug"] == "bitcoin-200k-by-end-2026"
    assert r["order_id"] == "ORD-LIVE-LH"
    assert r["status"] == "filled"
    assert r["fill_price"] == pytest.approx(0.40)
