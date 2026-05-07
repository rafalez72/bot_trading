"""Tests para src/copybot/ws_bridge.py — bridge WS → tradebook.

Cubre los failure modes del callback `_handle_trade`:
- Idempotencia cross-source (WS y polling generan el MISMO source_trade_id)
- Filtros de payload (wallet no activo, side inválido, campos faltantes)
- Robustez (payload malformado no crashea, excepciones no propagan)
- Cursor monotónico (no retrocede al avanzar paper_cursor)

Estrategia: testeamos `_make_handle_trade` aisladamente con mocks de
open_position/close_position. Para idempotencia testeamos la integración
real con `paper.open_position` contra una DB de prueba (`isolated_db`
fixture de conftest.py).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.copybot import ws_bridge
from src.db.schema import db, tx
from src.indexer.trades import _trade_id


# ---------- helpers ----------


def _make_payload(**overrides) -> dict:
    """Payload realista de un trade emitido por el WS RTDS de Polymarket."""
    base = {
        "proxyWallet": "0xWALLET1",
        "asset": "0xASSET1",
        "conditionId": "0xCID1",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "price": 0.55,
        "side": "BUY",
        "size": 100.0,
        "slug": "fake-market-2026",
        "timestamp": 1778000000,
        "title": "Fake Market 2026",
        "transactionHash": "0xTX1",
    }
    base.update(overrides)
    return base


def _seed_subscription(
    wallet: str = "0xwallet1",
    status: str = "active",
    sizing_mult: float = 1.0,
) -> None:
    """Crea una copy_subscription con el wallet en lower case."""
    with tx() as conn:
        conn.execute(
            "INSERT INTO copy_subscriptions (wallet, status, sizing_mult) "
            "VALUES (?, ?, ?) ON CONFLICT(wallet) DO UPDATE SET "
            "status=excluded.status, sizing_mult=excluded.sizing_mult",
            (wallet, status, sizing_mult),
        )


def _run(coro):
    """Helper sync para correr un coroutine."""
    return asyncio.run(coro)


# ---------- tests del callback con mocks ----------


def test_payload_sin_proxyWallet_no_llama_nada():
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position") as op, \
         patch.object(ws_bridge, "close_position") as cp:
        _run(handle(_make_payload(proxyWallet=None)))
    assert op.call_count == 0
    assert cp.call_count == 0


def test_payload_proxyWallet_no_string_no_llama_nada():
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position") as op:
        _run(handle(_make_payload(proxyWallet=12345)))
    assert op.call_count == 0


def test_wallet_no_en_active_set_no_llama_nada():
    handle = ws_bridge._make_handle_trade({"0xotrowallet"})
    with patch.object(ws_bridge, "open_position") as op:
        _run(handle(_make_payload()))  # proxyWallet=0xWALLET1
    assert op.call_count == 0


def test_wallet_en_set_match_es_case_insensitive():
    """El payload viene 0xWALLET1 (mayúsculas), set tiene 0xwallet1 (minúsculas)."""
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position", return_value=(99, None)) as op, \
         patch.object(ws_bridge, "_set_cursor"):
        _run(handle(_make_payload(proxyWallet="0xWALLET1")))
    assert op.call_count == 1
    # source_wallet pasado al executor también en lowercase
    assert op.call_args.kwargs["source_wallet"] == "0xwallet1"


@pytest.mark.parametrize("side", ["", "INVALID", "buyy", None])
def test_side_invalido_skip(side):
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position") as op, \
         patch.object(ws_bridge, "close_position") as cp:
        _run(handle(_make_payload(side=side)))
    assert op.call_count == 0
    assert cp.call_count == 0


def test_conditionId_faltante_skip():
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position") as op:
        _run(handle(_make_payload(conditionId=None)))
    assert op.call_count == 0


def test_transactionHash_faltante_skip():
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position") as op:
        _run(handle(_make_payload(transactionHash=None)))
    assert op.call_count == 0


@pytest.mark.parametrize("bad_price", ["foo", None])
def test_price_no_parseable_skip(bad_price):
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position") as op:
        _run(handle(_make_payload(price=bad_price)))
    assert op.call_count == 0


@pytest.mark.parametrize("bad_ts", ["foo", None, 0, -1])
def test_timestamp_invalido_skip(bad_ts):
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position") as op:
        _run(handle(_make_payload(timestamp=bad_ts)))
    assert op.call_count == 0


def test_BUY_valida_llama_open_position_con_id_compatible_polling():
    """CRÍTICO: el source_trade_id que genera el WS debe ser IDÉNTICO al que
    el polling genera vía indexer.trades._trade_id, así el guard de duplicate
    de open_position los reconoce como el mismo evento."""
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    payload = _make_payload(side="BUY")
    with patch.object(ws_bridge, "open_position", return_value=(42, None)) as op, \
         patch.object(ws_bridge, "_set_cursor") as sc:
        _run(handle(payload))

    expected_id = _trade_id(payload)
    assert op.call_count == 1
    kwargs = op.call_args.kwargs
    assert kwargs["source_trade_id"] == expected_id, (
        f"WS id={kwargs['source_trade_id']!r} != polling id={expected_id!r} "
        "→ guard de duplicate fallaría → DOBLE POSICIÓN"
    )
    assert kwargs["condition_id"] == "0xCID1"
    assert kwargs["outcome_index"] == 0
    assert kwargs["price"] == 0.55
    assert kwargs["timestamp"] == 1778000000
    assert sc.call_count == 1
    assert sc.call_args.args == ("0xwallet1", 1778000000)


def test_SELL_valido_llama_close_position():
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "close_position", return_value=42) as cp, \
         patch.object(ws_bridge, "_set_cursor"):
        _run(handle(_make_payload(side="SELL")))
    assert cp.call_count == 1
    assert cp.call_args.kwargs["condition_id"] == "0xCID1"
    assert cp.call_args.kwargs["price"] == 0.55


def test_outcomeIndex_None_id_correcto():
    """Si outcomeIndex viene None, el id debe coincidir con el del polling
    para ese mismo trade (donde indexer._trade_id pone string vacío)."""
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    payload = _make_payload(outcomeIndex=None)
    with patch.object(ws_bridge, "open_position", return_value=(1, None)) as op, \
         patch.object(ws_bridge, "_set_cursor"):
        _run(handle(payload))
    assert op.call_args.kwargs["source_trade_id"] == _trade_id(payload)


def test_open_position_excepcion_no_propaga():
    """Si open_position tira excepción, el callback debe loggear y seguir vivo."""
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position", side_effect=RuntimeError("boom")), \
         patch.object(ws_bridge, "_set_cursor"):
        # No debe levantar
        _run(handle(_make_payload()))


def test_set_cursor_excepcion_no_propaga():
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position", return_value=(1, None)), \
         patch.object(ws_bridge, "_set_cursor", side_effect=RuntimeError("locked")):
        _run(handle(_make_payload()))


def test_BUY_con_reject_duplicate_no_loggea_error():
    """Si open_position devuelve (None, 'duplicate'), no debe loggear como error
    (es esperado en re-entrega de WS o race con polling)."""
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "open_position", return_value=(None, "duplicate")) as op, \
         patch.object(ws_bridge, "_set_cursor"):
        _run(handle(_make_payload()))
    assert op.call_count == 1
    # No assertion sobre logs — lo importante es que no levante


# ---------- tests del cursor monotónico ----------


def test_set_cursor_no_retrocede(isolated_db):
    """index_state debe quedarse con MAX(value, ts) — si el polling ya avanzó
    a ts=200 y el WS llega tarde con ts=100, el cursor debe quedar en 200."""
    ws_bridge._set_cursor("0xwallet1", 200)
    ws_bridge._set_cursor("0xwallet1", 100)  # llega tarde, debe ignorarse

    with db() as conn:
        row = conn.execute(
            "SELECT value FROM index_state WHERE key=?",
            ("paper_cursor:0xwallet1",),
        ).fetchone()
    assert int(row["value"]) == 200, (
        "Cursor retrocedió → polling reprocesaría trades viejos al próximo ciclo"
    )


def test_set_cursor_avanza_si_es_mayor(isolated_db):
    ws_bridge._set_cursor("0xwallet1", 100)
    ws_bridge._set_cursor("0xwallet1", 200)
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM index_state WHERE key=?",
            ("paper_cursor:0xwallet1",),
        ).fetchone()
    assert int(row["value"]) == 200


# ---------- test de integración: idempotencia cross-source ----------


def test_idempotencia_WS_primero_polling_segundo(isolated_db, monkeypatch):
    """Caso normal: WS llega primero, polling después con el mismo trade.
    El polling debe ver el guard de duplicate y rechazar.

    Esto es el invariante CRÍTICO que justifica usar el mismo source_trade_id
    en ambos paths. Si fallara → DOBLE POSICIÓN con plata real.
    """
    from src.copybot import paper

    # Permitir que el filtro stale_trade no rechace nuestro fixture
    monkeypatch.setattr(paper, "MAX_TRADE_AGE_SECONDS", 10**9)

    _seed_subscription("0xwallet1")

    payload = _make_payload(side="BUY", timestamp=1778000000)

    # 1) WS abre primero — usando el callback real
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "_set_cursor"):  # no nos importa el cursor acá
        _run(handle(payload))

    # 2) Polling intenta abrir el mismo trade después
    polling_tid = _trade_id(payload)
    pid, reason = paper.open_position(
        source_wallet="0xwallet1",
        source_trade_id=polling_tid,
        condition_id=payload["conditionId"],
        outcome=payload["outcome"],
        outcome_index=payload["outcomeIndex"],
        price=payload["price"],
        timestamp=payload["timestamp"],
        raw=payload,
    )
    assert reason == "duplicate", (
        f"polling abrió un segundo paper_trade del mismo evento "
        f"(reason={reason!r}, pid={pid}). Esto sería DOBLE POSICIÓN "
        f"si ocurre con LIVE_MODE=true."
    )

    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM paper_trades WHERE condition_id=?",
            (payload["conditionId"],),
        ).fetchone()["n"]
    assert n == 1, f"Esperaba 1 paper_trade, encontré {n}"


def test_idempotencia_polling_primero_WS_segundo(isolated_db, monkeypatch):
    """Caso reverso: polling abrió primero, WS llega después (re-entrega tras
    un reconnect, o race en la otra dirección). El WS debe ver el guard."""
    from src.copybot import paper

    monkeypatch.setattr(paper, "MAX_TRADE_AGE_SECONDS", 10**9)

    _seed_subscription("0xwallet1")
    payload = _make_payload(side="BUY", timestamp=1778000000)

    # 1) Polling abre primero
    polling_tid = _trade_id(payload)
    pid_first, reason_first = paper.open_position(
        source_wallet="0xwallet1",
        source_trade_id=polling_tid,
        condition_id=payload["conditionId"],
        outcome=payload["outcome"],
        outcome_index=payload["outcomeIndex"],
        price=payload["price"],
        timestamp=payload["timestamp"],
        raw=payload,
    )
    assert pid_first is not None, f"polling debió abrir, reason={reason_first}"

    # 2) WS llega después con el mismo trade
    handle = ws_bridge._make_handle_trade({"0xwallet1"})
    with patch.object(ws_bridge, "_set_cursor"):
        _run(handle(payload))

    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM paper_trades WHERE condition_id=?",
            (payload["conditionId"],),
        ).fetchone()["n"]
    assert n == 1, (
        f"WS abrió un duplicado tras polling. n={n}. Bug del source_trade_id "
        f"divergente — el WS debe usar el mismo formato que indexer._trade_id."
    )
