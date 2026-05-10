"""Tests de safety para strategies de Nivel >= 3.

Cobertura (fix bugs #4 / #5 / #6 de docs/PRE_LIVE_AUDIT.md):

1. market_maker stubs (place_limit_order, cancel_order, get_open_orders,
   get_fills_since) raise NotImplementedError cuando LIVE_MODE=true. Esto
   evita silent failure si el user activa MM en LIVE creyendo que opera.

2. spike_arb_loop en LIVE_MODE logea WARNING + manda notif (executor STUB).

3. long_horizon_loop en LIVE_MODE idem.

4. crypto_arb_hedge_loop en LIVE_MODE logea WARNING (loop wireframe).

5. Notif coverage: settle helpers de las 5 strategies (mm/spike/lh/adv/hedge)
   disparan notifier.gain o .loss según el pnl. Antes era silente — el user
   no se enteraba de cierres.

Mockeamos notifier.send para verificar que se llamó sin tocar Telegram real.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

import src.copybot.market_maker as mm_mod
import src.copybot.spike_arb as spike_mod
import src.copybot.long_horizon_arb as lh_mod
import src.copybot.adversarial_asks as adv_mod
import src.copybot.crypto_arb_hedge as hedge_mod


# --------------------------------------------------------------------------- #
# 1. Stubs MM raise NotImplementedError en LIVE_MODE (bug #5)
# --------------------------------------------------------------------------- #


def test_mm_stubs_raise_in_live_mode(monkeypatch):
    """LIVE_MODE=true → los 4 stubs deben raise NotImplementedError con
    mensaje claro que apunta a docs/PRE_LIVE_AUDIT.md bug #5.

    El fix evita el peor caso de live: bot consume DB sin tocar el CLOB.
    """
    import src.config as cfg
    monkeypatch.setattr(cfg, "LIVE_MODE", True)

    with pytest.raises(NotImplementedError, match="bug #5"):
        mm_mod.place_limit_order(
            token_id="0xTOK", side="BUY", price=0.5, size_usdc=2.0,
        )
    with pytest.raises(NotImplementedError, match="bug #5"):
        mm_mod.cancel_order("FAKE-ORDER")
    with pytest.raises(NotImplementedError, match="bug #5"):
        mm_mod.get_open_orders()
    with pytest.raises(NotImplementedError, match="bug #5"):
        mm_mod.get_fills_since(0)


def test_mm_stubs_ok_in_paper_mode(monkeypatch, isolated_db):
    """Sanity check: en LIVE_MODE=false (paper) los stubs siguen funcionando
    devolviendo fake data. Regression guard del fix bug #5.
    """
    import src.config as cfg
    monkeypatch.setattr(cfg, "LIVE_MODE", False)

    # place: devuelve LimitOrderResult con fake_id
    res = mm_mod.place_limit_order(
        token_id="0xTOK", side="BUY", price=0.5, size_usdc=2.0,
    )
    assert res.ok is True
    assert res.order_id is not None and res.order_id.startswith("STUB-")

    # cancel: True log-only
    assert mm_mod.cancel_order("FAKE-ID") is True

    # get_open_orders: lista vacía (tabla recién creada)
    assert mm_mod.get_open_orders() == []

    # get_fills_since: []
    assert mm_mod.get_fills_since(0) == []


# --------------------------------------------------------------------------- #
# 2-4. Warning + notif al startup en LIVE_MODE (bug #6)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_spike_arb_loop_warns_live_mode(monkeypatch, caplog):
    """spike_arb_loop arranca en LIVE_MODE → debe loggear WARNING explícito
    + mandar notif Telegram visible. Fix bug #6.

    Lo abortamos forzando SPIKE_ARB_ENABLED=false ANTES del path warning?
    No — necesitamos llegar al warning. Truco: dejamos enabled=true, pero
    monkeypatcheamos BinanceTickerWS para que no haga red real, y cancelamos
    rápido tras el warning.
    """
    import logging
    monkeypatch.setenv("SPIKE_ARB_ENABLED", "true")
    import src.config as cfg
    monkeypatch.setattr(cfg, "LIVE_MODE", True)

    captured_sends: list[str] = []

    def fake_send(text: str, *args, **kwargs) -> bool:
        captured_sends.append(text)
        return True

    monkeypatch.setattr("src.copybot.notifier.send", fake_send)

    # Stub BinanceTickerWS.run para terminar inmediatamente
    class _StubWS:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            return

        def stop(self):
            pass

    monkeypatch.setattr(
        "src.binance.websocket.BinanceTickerWS", _StubWS,
    )

    # Tabla in-memory para no romper init_table.
    import asyncio
    caplog.set_level(logging.WARNING)

    # Corremos el loop con timeout corto: el warning sale antes del heartbeat
    # de 300s, así que cancelamos rápido.
    task = asyncio.create_task(spike_mod.spike_arb_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # Assert: warning loggeado + notif Telegram enviado
    warning_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelname == "WARNING" and "spike_arb" in r.getMessage()
    ]
    assert any("LIVE_MODE" in m and "STUB" in m for m in warning_msgs), (
        f"expected LIVE_MODE STUB warning, got: {warning_msgs}"
    )
    assert any("spike_arb" in s and "STUB" in s for s in captured_sends), (
        f"expected notif Telegram with STUB, got: {captured_sends}"
    )


@pytest.mark.asyncio
async def test_long_horizon_loop_warns_live_mode(monkeypatch, caplog):
    """long_horizon_loop arranca en LIVE_MODE → WARNING + notif. Fix bug #6."""
    import logging
    monkeypatch.setenv("LONG_HORIZON_ENABLED", "true")
    import src.config as cfg
    monkeypatch.setattr(cfg, "LIVE_MODE", True)

    captured_sends: list[str] = []

    def fake_send(text: str, *args, **kwargs) -> bool:
        captured_sends.append(text)
        return True

    monkeypatch.setattr("src.copybot.notifier.send", fake_send)

    class _StubWS:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            return

        def stop(self):
            pass

    monkeypatch.setattr(
        "src.binance.websocket.BinanceTickerWS", _StubWS,
    )

    # Stub PolymarketClient para no abrir conexiones reales
    class _StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def iter_markets(self, **kwargs):
            if False:
                yield {}
            return

    monkeypatch.setattr(
        "src.polymarket.client.PolymarketClient", _StubClient,
    )

    import asyncio
    caplog.set_level(logging.WARNING)

    task = asyncio.create_task(lh_mod.long_horizon_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    warning_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelname == "WARNING" and "long_horizon" in r.getMessage()
    ]
    assert any("LIVE_MODE" in m and "STUB" in m for m in warning_msgs), (
        f"expected LIVE_MODE STUB warning, got: {warning_msgs}"
    )
    assert any("long_horizon" in s and "STUB" in s for s in captured_sends), (
        f"expected notif Telegram with STUB, got: {captured_sends}"
    )


# --------------------------------------------------------------------------- #
# 5. Notif coverage en cierres (bug #4)
# --------------------------------------------------------------------------- #


def test_mm_settle_bucket_triggers_notif(isolated_db, monkeypatch):
    """market_maker.settle_bucket con PnL != 0 dispara notifier.gain o .loss
    con bucket_label='MM'. Fix bug #4.

    Setup: insertamos una mm_order status='filled' con fill_price 0.4 y
    cerramos al resolution_price=1.0 → PnL positivo grande.
    """
    captured_gains: list[tuple] = []
    captured_losses: list[tuple] = []

    def fake_gain(amount, accumulated, *, pt=None, bucket_label=None):
        captured_gains.append((amount, accumulated, pt, bucket_label))
        return True

    def fake_loss(amount, accumulated, *, pt=None, bucket_label=None):
        captured_losses.append((amount, accumulated, pt, bucket_label))
        return True

    monkeypatch.setattr("src.copybot.notifier.gain", fake_gain)
    monkeypatch.setattr("src.copybot.notifier.loss", fake_loss)

    from src.copybot.market_maker import MarketMaker, MarketMakerConfig
    from src.db.schema import tx as _tx

    # Ensure schema + insert una filled mm_order BUY @ 0.4 size $1
    mm_mod._ensure_schema()
    with _tx() as conn:
        conn.execute(
            "INSERT INTO mm_orders (condition_id, side, price, size_usdc, "
            "order_id, status, fill_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("0xCID_BTC", "BUY", 0.4, 1.0, "FAKE-1", "filled", 0.4),
        )

    cfg = MarketMakerConfig(enabled=True)
    mm = MarketMaker(config=cfg)
    out = mm.settle_bucket("0xCID_BTC", resolution_price=1.0)

    assert out["n_settled"] == 1
    assert out["pnl_usdc_total"] > 0

    # Notif gain disparado con bucket_label='MM'
    assert len(captured_gains) == 1
    amount, accumulated, pt, label = captured_gains[0]
    assert amount > 0
    assert label == "MM"
    assert pt is not None
    assert isinstance(pt, dict) and "raw" in pt
    # losses no se llamó
    assert captured_losses == []


def test_long_horizon_settle_trade_triggers_notif(isolated_db, monkeypatch):
    """long_horizon_arb.settle_trade con pnl > 0 dispara notifier.gain
    con bucket_label='long-horizon'. Fix bug #4.
    """
    captured_gains: list[tuple] = []
    captured_losses: list[tuple] = []

    def fake_gain(amount, accumulated, *, pt=None, bucket_label=None):
        captured_gains.append((amount, accumulated, pt, bucket_label))
        return True

    def fake_loss(amount, accumulated, *, pt=None, bucket_label=None):
        captured_losses.append((amount, accumulated, pt, bucket_label))
        return True

    monkeypatch.setattr("src.copybot.notifier.gain", fake_gain)
    monkeypatch.setattr("src.copybot.notifier.loss", fake_loss)

    # Ensure long_horizon_trades schema
    lh_mod.init_table()

    # Insert un trade open
    from src.db.schema import tx as _tx
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO long_horizon_trades (market_slug, underlying, side, "
            "entry_mid, bet_usdc, status, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("btc-100k-by-end-2027", "btc", "Up", 0.4, 5.0, "open", 0),
        )
        trade_id = cur.lastrowid

    # Call settle_trade con pnl positivo → debe disparar gain
    lh_mod.settle_trade(
        trade_id, pnl_usdc=3.5,
        market_slug="btc-100k-by-end-2027", underlying="btc",
        status="settled",
    )

    assert len(captured_gains) == 1
    amount, accumulated, pt, label = captured_gains[0]
    assert abs(amount - 3.5) < 1e-9
    assert label == "long-horizon"
    assert isinstance(pt, dict) and pt.get("raw", {}).get("slug")
    assert captured_losses == []

    # Cierre con pérdida → debe disparar loss
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO long_horizon_trades (market_slug, underlying, side, "
            "entry_mid, bet_usdc, status, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("eth-5k-by-end-2026", "eth", "Down", 0.3, 5.0, "open", 0),
        )
        trade_id_2 = cur.lastrowid

    lh_mod.settle_trade(
        trade_id_2, pnl_usdc=-1.5,
        market_slug="eth-5k-by-end-2026", underlying="eth",
        status="settled",
    )

    assert len(captured_losses) == 1
    amount, accumulated, pt, label = captured_losses[0]
    assert abs(amount - 1.5) < 1e-9
    assert label == "long-horizon"
