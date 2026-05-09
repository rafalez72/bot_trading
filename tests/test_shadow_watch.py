"""Tests para src/copybot/shadow_watch.py.

Cubre la selección de candidatas y el filtrado por:
- Status en copy_subscriptions (excluir activas).
- Filtros de calidad relajados (pnl, trades, win_rate).
- Ventana de actividad (last_trade_ts).
- Orden por volumen DESC y respeto del LIMIT.
- Inserción idempotente con ``mode='pre_promote_watch'``.
"""
from __future__ import annotations

import time

from src.copybot import shadow_watch
from src.db.schema import db, tx


# ---------- helpers ----------


def _seed_metric(
    wallet: str,
    *,
    pnl: float = 200.0,
    trades: int = 100,
    win_rate: float = 0.60,
    volume: float = 10_000.0,
    last_ts: int | None = None,
    now: int | None = None,
) -> None:
    """Inserta una fila en trader_metrics con valores que pasan/fallan filtros."""
    now = now if now is not None else int(time.time())
    last_ts = last_ts if last_ts is not None else now
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO trader_metrics
                (wallet, total_trades, total_volume_usdc, realized_pnl_usdc,
                 win_rate, last_trade_ts)
            VALUES (?,?,?,?,?,?)
            """,
            (wallet, trades, volume, pnl, win_rate, last_ts),
        )


def _seed_subscription(wallet: str, status: str = "active") -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO copy_subscriptions (wallet, status) VALUES (?,?) "
            "ON CONFLICT(wallet) DO UPDATE SET status=excluded.status",
            (wallet, status),
        )


# ---------- select_shadow_candidates ----------


def test_select_ordena_por_volumen_desc(isolated_db, now_ts):
    """La función debe priorizar wallets con mayor volumen total."""
    _seed_metric("0xLOWVOL", volume=1_000.0, last_ts=now_ts, now=now_ts)
    _seed_metric("0xHIVOL", volume=50_000.0, last_ts=now_ts, now=now_ts)
    _seed_metric("0xMEDVOL", volume=10_000.0, last_ts=now_ts, now=now_ts)

    out = shadow_watch.select_shadow_candidates(limit=10, now=now_ts)

    assert out == ["0xhivol", "0xmedvol", "0xlowvol"], (
        f"Esperaba orden por volumen DESC, recibí {out}"
    )


def test_select_excluye_wallets_activas(isolated_db, now_ts):
    """Wallets ya en copy_subscriptions con status='active' NO deben aparecer."""
    _seed_metric("0xACTIVE", volume=100_000.0, last_ts=now_ts, now=now_ts)
    _seed_metric("0xCANDIDATE", volume=20_000.0, last_ts=now_ts, now=now_ts)
    _seed_metric("0xDROPPED", volume=15_000.0, last_ts=now_ts, now=now_ts)
    _seed_metric("0xPAUSED", volume=10_000.0, last_ts=now_ts, now=now_ts)

    _seed_subscription("0xACTIVE", status="active")
    _seed_subscription("0xDROPPED", status="dropped")
    _seed_subscription("0xPAUSED", status="paused")

    out = shadow_watch.select_shadow_candidates(limit=10, now=now_ts)

    # ACTIVE excluido; DROPPED y PAUSED sí entran (no copian, libre de observar).
    # CANDIDATE entra (nunca visto antes).
    assert "0xactive" not in out
    assert set(out) == {"0xcandidate", "0xdropped", "0xpaused"}


def test_select_respeta_actividad_7d(isolated_db, now_ts):
    """Wallets con último trade > 7d atrás deben ser filtradas."""
    seven_days = 7 * 86400
    _seed_metric("0xFRESH", volume=10_000, last_ts=now_ts - 86400, now=now_ts)  # 1d
    _seed_metric("0xEDGE", volume=20_000, last_ts=now_ts - seven_days + 60, now=now_ts)
    _seed_metric("0xSTALE", volume=50_000, last_ts=now_ts - seven_days - 3600, now=now_ts)

    out = shadow_watch.select_shadow_candidates(limit=10, now=now_ts)

    assert "0xstale" not in out, "Wallet stale (>7d) NO debió entrar"
    assert set(out) == {"0xfresh", "0xedge"}


def test_select_filtra_por_quality_relajado(isolated_db, now_ts):
    """No deben entrar wallets que NO superen el threshold relajado."""
    # Pasa todos los filtros — entra.
    _seed_metric("0xGOOD", pnl=200, trades=100, win_rate=0.60, volume=10_000,
                 last_ts=now_ts, now=now_ts)
    # PnL bajo — no entra.
    _seed_metric("0xLOWPNL", pnl=10, trades=100, win_rate=0.60, volume=10_000,
                 last_ts=now_ts, now=now_ts)
    # Pocos trades — no entra.
    _seed_metric("0xFEWTRADES", pnl=200, trades=10, win_rate=0.60, volume=10_000,
                 last_ts=now_ts, now=now_ts)
    # Win rate bajo — no entra.
    _seed_metric("0xLOWWIN", pnl=200, trades=100, win_rate=0.40, volume=10_000,
                 last_ts=now_ts, now=now_ts)

    out = shadow_watch.select_shadow_candidates(limit=10, now=now_ts)
    assert out == ["0xgood"]


def test_select_respeta_limit(isolated_db, now_ts):
    """Más candidatas que LIMIT → toma los top-LIMIT por volumen."""
    for i, vol in enumerate([1_000, 5_000, 9_000, 7_000, 3_000]):
        _seed_metric(f"0xW{i}", volume=float(vol), last_ts=now_ts, now=now_ts)

    out = shadow_watch.select_shadow_candidates(limit=3, now=now_ts)
    # Top-3 por volumen: W2(9k), W3(7k), W1(5k)
    assert out == ["0xw2", "0xw3", "0xw1"]


def test_select_limit_zero_returns_empty(isolated_db, now_ts):
    """Edge case: limit=0 (shadow disabled) NO debe ejecutar query."""
    _seed_metric("0xANY", volume=10_000, last_ts=now_ts, now=now_ts)
    out = shadow_watch.select_shadow_candidates(limit=0, now=now_ts)
    assert out == []


# ---------- record_shadow_trade ----------


def _trade_payload(**overrides) -> dict:
    base = {
        "proxyWallet": "0xWALLET1",
        "asset": "0xASSET1",
        "conditionId": "0xCID1",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "price": 0.55,
        "side": "BUY",
        "size": 100.0,
        "slug": "fake-market",
        "timestamp": 1778000000,
        "transactionHash": "0xTX1",
    }
    base.update(overrides)
    return base


def test_record_inserta_con_mode_pre_promote(isolated_db):
    """``record_shadow_trade`` debe persistir con mode='pre_promote_watch'."""
    inserted = shadow_watch.record_shadow_trade(_trade_payload())
    assert inserted is True

    with db() as conn:
        row = conn.execute(
            "SELECT wallet, mode, side, price, condition_id "
            "FROM shadow_trades WHERE trade_id IS NOT NULL"
        ).fetchone()
    assert row is not None
    assert row["mode"] == "pre_promote_watch"
    assert row["wallet"] == "0xwallet1"  # normalizado lower
    assert row["side"] == "BUY"
    assert row["price"] == 0.55


def test_record_idempotente_misma_tx(isolated_db):
    """Segunda inserción del mismo trade_id debe ser no-op (UNIQUE constraint)."""
    p = _trade_payload()
    assert shadow_watch.record_shadow_trade(p) is True
    assert shadow_watch.record_shadow_trade(p) is False  # ya existe

    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM shadow_trades").fetchone()["n"]
    assert n == 1


def test_record_payload_invalido(isolated_db):
    """Payloads inválidos deben devolver False sin levantar."""
    assert shadow_watch.record_shadow_trade(_trade_payload(transactionHash=None)) is False
    assert shadow_watch.record_shadow_trade(_trade_payload(timestamp=0)) is False
    assert shadow_watch.record_shadow_trade(_trade_payload(side="INVALID")) is False
    assert shadow_watch.record_shadow_trade(_trade_payload(proxyWallet=None)) is False


def test_record_normaliza_timestamp_ms_a_s(isolated_db):
    """Timestamps en ms (13 dígitos) deben pasarse a segundos antes de guardar."""
    p = _trade_payload(timestamp=1778000000123)  # ms
    shadow_watch.record_shadow_trade(p)

    with db() as conn:
        ts = conn.execute(
            "SELECT timestamp FROM shadow_trades LIMIT 1"
        ).fetchone()["timestamp"]
    assert ts == 1778000000, f"esperaba 1778000000 (s), recibí {ts}"
