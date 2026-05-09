"""Tests para discovery_topvolume — el sweep diario de wallets top-volumen.

Cubre:
- `_aggregate_by_wallet`: suma price*size por proxyWallet, filtrando 24h.
- `_top_n_by_volume`: orden DESC y truncado a N.
- `_fetch_24h_trades` (mockeando `client.trades`): corta cuando timestamps
  bajan del cutoff de 24h.
- `discover_top_volume_wallets(limit=10)` end-to-end con httpx mockeado y
  backfill stubeado: devuelve los 10 wallets correctos (ranked por volumen)
  y dispara backfill sólo para los que no están en trader_metrics.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.copybot import discovery_topvolume as topvol
from src.db.schema import db, tx


# ---------- helpers ----------

def _mk_trade(wallet: str, price: float, size: float, ts: int) -> dict:
    """Construye un trade JSON con la shape mínima que parsea el módulo."""
    return {
        "proxyWallet": wallet,
        "price": price,
        "size": size,
        "timestamp": ts,
        "transactionHash": f"0x{wallet[-4:]}{ts}",
        "asset": "tok",
        "side": "BUY",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "conditionId": "cond1",
    }


# ---------- _aggregate_by_wallet ----------

def test_aggregate_sums_price_times_size_por_wallet():
    now = int(time.time())
    trades = [
        _mk_trade("0xa", 0.50, 100, now - 10),     # 50
        _mk_trade("0xa", 0.40, 200, now - 20),     # 80
        _mk_trade("0xb", 0.90, 50, now - 30),      # 45
        _mk_trade("0xc", 0.10, 1000, now - 40),    # 100
    ]
    agg = topvol._aggregate_by_wallet(trades, now_ts=now)
    assert agg["0xa"] == pytest.approx(130.0)
    assert agg["0xb"] == pytest.approx(45.0)
    assert agg["0xc"] == pytest.approx(100.0)


def test_aggregate_filtra_fuera_de_24h():
    now = int(time.time())
    trades = [
        _mk_trade("0xa", 0.50, 100, now - 100),                # in
        _mk_trade("0xa", 0.50, 100, now - (25 * 3600)),        # out (>24h)
        _mk_trade("0xb", 1.00, 10, now - (24 * 3600 + 1)),     # out edge
    ]
    agg = topvol._aggregate_by_wallet(trades, now_ts=now)
    assert agg["0xa"] == pytest.approx(50.0)
    assert "0xb" not in agg


def test_aggregate_skip_wallet_vacio_y_size_cero():
    now = int(time.time())
    trades = [
        _mk_trade("", 0.5, 100, now),
        {"proxyWallet": "0xa", "price": 0, "size": 100, "timestamp": now},
        {"proxyWallet": "0xa", "price": "bad", "size": 100, "timestamp": now},
        _mk_trade("0xa", 0.5, 100, now),  # ok
    ]
    agg = topvol._aggregate_by_wallet(trades, now_ts=now)
    assert agg == {"0xa": pytest.approx(50.0)}


# ---------- _top_n_by_volume ----------

def test_top_n_devuelve_orden_desc_truncado():
    agg = {"0xa": 100.0, "0xb": 300.0, "0xc": 200.0, "0xd": 50.0}
    top = topvol._top_n_by_volume(agg, 2)
    assert [w for w, _ in top] == ["0xb", "0xc"]
    assert top[0][1] == 300.0


def test_top_n_clamp_si_n_mayor_que_total():
    agg = {"0xa": 1.0, "0xb": 2.0}
    top = topvol._top_n_by_volume(agg, 100)
    assert len(top) == 2


# ---------- _fetch_24h_trades (httpx mockeado) ----------

class _FakeClient:
    """Stub mínimo de PolymarketClient — sólo expone `.trades(limit, offset)`.

    Devuelve `pages` (lista de listas) en orden. Cuando se queda sin páginas,
    devuelve lista vacía (señal de fin).
    """

    def __init__(self, pages: list[list[dict]]):
        self._pages = list(pages)
        self.calls = 0

    async def trades(self, *, limit: int, offset: int) -> list[dict]:
        self.calls += 1
        if self.calls > len(self._pages):
            return []
        return self._pages[self.calls - 1]


def test_fetch_24h_corta_cuando_timestamps_bajan_de_cutoff():
    now = int(time.time())
    cutoff = now - 24 * 3600
    page1 = [_mk_trade(f"0x{i}", 0.5, 10, now - (i * 60)) for i in range(5)]
    # Página 2: el último ya está fuera de los 24h → debe cortar.
    page2 = [
        _mk_trade("0xx", 0.5, 10, now - 1000),
        _mk_trade("0xy", 0.5, 10, cutoff - 10),  # fuera
    ]
    page3_should_not_be_called = [_mk_trade("0xz", 0.5, 10, now)]
    fc = _FakeClient([page1, page2, page3_should_not_be_called])

    trades = asyncio.run(
        topvol._fetch_24h_trades(fc, now_ts=now, page_size=5, throttle=0)
    )
    # 5 de page1 + 1 in-window de page2.
    assert len(trades) == 6
    assert fc.calls == 2  # nunca llamó la página 3


def test_fetch_24h_para_si_pagina_devuelve_menos_que_page_size():
    now = int(time.time())
    page1 = [_mk_trade(f"0x{i}", 0.5, 10, now - i) for i in range(3)]
    fc = _FakeClient([page1])
    trades = asyncio.run(
        topvol._fetch_24h_trades(fc, now_ts=now, page_size=500, throttle=0)
    )
    assert len(trades) == 3
    assert fc.calls == 1


# ---------- discover_top_volume_wallets end-to-end ----------

def _seed_metric(wallet: str) -> None:
    """Inserta una fila mínima en trader_metrics para simular wallet ya backfilleado."""
    with tx() as conn:
        conn.execute(
            "INSERT INTO traders (wallet, first_seen_at, last_indexed_at, total_trades) "
            "VALUES (?, datetime('now'), datetime('now'), 0) "
            "ON CONFLICT(wallet) DO NOTHING",
            (wallet,),
        )
        conn.execute(
            "INSERT INTO trader_metrics (wallet, total_trades, total_volume_usdc, "
            "realized_pnl_usdc, unrealized_pnl_usdc, roi_pct, win_rate, "
            "avg_position_size, max_drawdown_pct, sharpe_proxy, active_days, "
            "first_trade_ts, last_trade_ts, score) "
            "VALUES (?, 0,0,0,0,0,0,0,0,0,0,0,0,0)",
            (wallet,),
        )


def test_discover_top_volume_wallets_end_to_end(isolated_db, monkeypatch):
    """Mock httpx + backfill + compute_for_wallet — verifica top-N sorting,
    skip de wallets ya conocidos y trigger de backfill sólo para los nuevos.
    """
    now = int(time.time())

    # Construimos 12 wallets con volúmenes conocidos (w_i tiene volumen 100*i):
    # w12=1200, w11=1100, w10=1000, ..., w1=100. Top-10 (DESC) = w12..w3.
    # Pre-seedeamos w11 en trader_metrics → debe skipearse → backfill={w12,w10..w3}.
    pages: list[list[dict]] = []
    page = []
    for i in range(1, 13):
        # Cada wallet tiene exactamente i trades de $100 cada uno → volumen = 100*i.
        # Distribuimos los trades en una página para simplificar.
        for k in range(i):
            page.append(_mk_trade(f"0xw{i:02d}", 1.0, 100.0, now - (k * 60)))
    # Una sola página, length < page_size → el fetch corta natural.
    pages.append(page)

    # Mockear PolymarketClient para que use _FakeClient.
    class _CtxFake:
        def __init__(self):
            self._inner = _FakeClient(pages)

        async def __aenter__(self):
            return self._inner

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(topvol, "PolymarketClient", lambda: _CtxFake())

    # Stub backfill_wallet y compute_for_wallet — sólo nos interesa que se llamen
    # con los wallets correctos. No queremos pegarle a la API real.
    backfilled: list[str] = []

    async def _fake_backfill(w: str) -> int:
        backfilled.append(w)
        return 0

    computed: list[str] = []

    def _fake_compute(w: str):
        computed.append(w)
        return None  # devolver None hace skip del UPSERT_METRICS

    import src.indexer.trades as trades_mod
    import src.analytics.metrics as metrics_mod
    monkeypatch.setattr(trades_mod, "backfill_wallet", _fake_backfill)
    monkeypatch.setattr(metrics_mod, "compute_for_wallet", _fake_compute)

    # Pre-pueblo trader_metrics con w11 (el top) — debería skipearse.
    _seed_metric("0xw11")

    res = asyncio.run(
        topvol.discover_top_volume_wallets(limit=10, force=True, time_budget_s=60)
    )

    # Verificaciones:
    # - candidates = 10 (top-N por volumen sobre los 12 wallets vistos).
    assert res["candidates"] == 10
    # - 1 skipped (w11 estaba en trader_metrics).
    assert res["skipped_known"] == 1
    # - 9 backfilleados.
    assert res["backfilled"] == 9
    # - El backfill list contiene los wallets esperados (w10..w2), NO w11.
    assert "0xw11" not in backfilled
    expected = {f"0xw{i:02d}" for i in (12, 10, 9, 8, 7, 6, 5, 4, 3)}
    assert set(backfilled) == expected
    # - bot_state quedó marcado.
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key='topvolume_last_run'"
        ).fetchone()
    assert r is not None and int(r["value"]) > 0


def test_discover_skip_si_ya_corrio_recien(isolated_db, monkeypatch):
    """Si DISCOVERY_TOPVOLUME_INTERVAL_HOURS no transcurrió, hace skip sin tocar nada."""
    # Seedeamos last_run a "hace 1h" — interval default es 24h → debe skipear.
    one_hour_ago = int(time.time()) - 3600
    with tx() as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value, updated_at) "
            "VALUES ('topvolume_last_run', ?, datetime('now'))",
            (str(one_hour_ago),),
        )

    # PolymarketClient no debe ser llamado — si lo es, fallamos.
    def _boom():
        raise AssertionError("no debería instanciarse el client")
    monkeypatch.setattr(topvol, "PolymarketClient", _boom)

    res = asyncio.run(topvol.discover_top_volume_wallets(limit=10))
    assert res["skipped"] is True
    assert res["reason"] == "ya corrió hace poco"
