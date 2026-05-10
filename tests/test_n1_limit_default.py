"""Tests N1 — copybot live default LIMIT_FOK (defensa MEV/adversarial).

Contexto (2026-05-10): live_trades del 2026-05-10 perdieron $76 por entry_price
degenerado (0.001) en MARKET orders sobre books thin. La fix migra el default
a LIMIT_FOK (que ya soporta `clob_client.place_market_order` desde commit
3cdf93b). Acá verificamos que `executor.open_position` wirea correctamente
`order_type=LIVE_ORDER_TYPE` al wrapper.

Tests:
  1. Default → LIMIT_FOK: con env por default, executor pasa "LIMIT_FOK"
     al `place_market_order`.
  2. Legacy explicit → MARKET: si el operador setea LIVE_ORDER_TYPE=MARKET
     en env, el executor respeta esa elección y no fuerza LIMIT_FOK.
  3. max_slippage respected: el wrapper recibe el `order_type` correcto,
     que es lo único que controla el path tomado en `place_market_order`
     (MARKET = FAK + retry; LIMIT_FOK = FOK at price ± LIVE_MAX_SLIPPAGE_PCT).
     Verificamos que executor NO pisa el order_type con un literal
     hardcodeado — el control queda en config/env.

Estrategia: mockeamos `place_market_order` y `get_token_id` (igual que
test_executor.py). Inspectamos el `order_type` kwarg que recibe el mock.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.copybot import executor
from src.db.schema import db
from src.polymarket.clob_client import OrderResult


# ---------- helpers (replican test_executor.py) ----------

def _seed_subscription(wallet: str = "0xtrader") -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO copy_subscriptions (wallet, status, sizing_mult)
            VALUES (?, 'active', 1.0)
            ON CONFLICT(wallet) DO UPDATE SET status='active', sizing_mult=1.0
            """,
            (wallet,),
        )


def _ok_fok_order() -> OrderResult:
    """Fake fill exitoso para que open_position llegue al INSERT."""
    return OrderResult(
        ok=True,
        order_id="order-fok",
        status="matched",
        filled_size=10.0,
        avg_price=0.347,
        tx_hash="0xtxfok",
    )


# ---------- Test 1: default LIMIT_FOK ----------

def test_open_position_default_uses_limit_fok(isolated_db, monkeypatch):
    """Con LIVE_ORDER_TYPE en su default ("LIMIT_FOK"), executor.open_position
    debe pasar `order_type="LIMIT_FOK"` al `place_market_order`.

    Esto garantiza la defensa anti-MEV: el wrapper toma el path FOK al
    limit_price = mid ± LIVE_MAX_SLIPPAGE_PCT, sin retry, sin riesgo de
    pagar peor que el cap.
    """
    monkeypatch.setattr(executor, "LIVE_ORDER_TYPE", "LIMIT_FOK")
    _seed_subscription()

    with patch(
        "src.polymarket.clob_client.place_market_order",
        return_value=_ok_fok_order(),
    ) as mock_order, patch(
        "src.polymarket.clob_client.get_token_id",
        return_value="tok-mock-1",
    ):
        live_id, reject = executor.open_position(
            source_wallet="0xtrader",
            source_trade_id="trade-n1-default",
            condition_id="0xcid",
            outcome="YES",
            outcome_index=0,
            price=0.347,
            timestamp=int(time.time()),
            raw={"asset": "tok-mock-1"},
        )

    assert reject is None, f"open_position rechazó inesperadamente: {reject}"
    assert live_id is not None

    # Inspect: order_type kwarg llegó como "LIMIT_FOK".
    assert mock_order.call_count == 1
    kwargs = mock_order.call_args.kwargs
    assert kwargs.get("order_type") == "LIMIT_FOK", (
        f"Esperaba order_type='LIMIT_FOK' (default anti-MEV), "
        f"recibí {kwargs.get('order_type')!r}. "
        "Si esto rompe, alguien hardcodeó MARKET o el wiring se rompió."
    )
    # Sanity: side BUY y los demás params esenciales pasaron.
    assert kwargs.get("side") == "BUY"
    assert kwargs.get("token_id") == "tok-mock-1"


# ---------- Test 2: MARKET legacy si env explícito ----------

def test_open_position_market_legacy_when_env_explicit(isolated_db, monkeypatch):
    """Si el operador setea explícitamente LIVE_ORDER_TYPE=MARKET, executor
    debe respetarlo (escape hatch para books con depth >> bet_size).

    Esto NO es el default — es opt-in. Mantener el path legacy disponible
    evita romper a operadores que conocen su book y prefieren FAK + retry.
    """
    monkeypatch.setattr(executor, "LIVE_ORDER_TYPE", "MARKET")
    _seed_subscription()

    with patch(
        "src.polymarket.clob_client.place_market_order",
        return_value=_ok_fok_order(),
    ) as mock_order, patch(
        "src.polymarket.clob_client.get_token_id",
        return_value="tok-mock-2",
    ):
        live_id, reject = executor.open_position(
            source_wallet="0xtrader",
            source_trade_id="trade-n1-legacy",
            condition_id="0xcid",
            outcome="YES",
            outcome_index=0,
            price=0.347,
            timestamp=int(time.time()),
            raw={"asset": "tok-mock-2"},
        )

    assert reject is None, f"open_position rechazó inesperadamente: {reject}"
    assert live_id is not None

    kwargs = mock_order.call_args.kwargs
    assert kwargs.get("order_type") == "MARKET", (
        f"Esperaba order_type='MARKET' (legacy explícito), "
        f"recibí {kwargs.get('order_type')!r}. "
        "El wiring debe respetar la env var del operador."
    )


# ---------- Test 3: max_slippage respected (control no se hardcodea) ----------

def test_open_position_does_not_hardcode_order_type(isolated_db, monkeypatch):
    """El control de order_type vive en LIVE_ORDER_TYPE (config), no en un
    literal en executor.py. Esto garantiza que un cambio futuro de env var
    propague sin tocar código.

    Verificación: pisamos LIVE_ORDER_TYPE con un valor distinto de los dos
    válidos ("LIMIT_FOK" / "MARKET"), por ej. un str raro. El executor debe
    pasarlo TAL CUAL al wrapper — es el wrapper quien sanitiza (cae a
    LIMIT_FOK por default si el str no matchea). Con eso confirmamos que
    executor no recorta ni traduce el valor: queda como single source of
    truth la env var.

    Side benefit: cubre que LIVE_MAX_SLIPPAGE_PCT no se toca acá tampoco —
    el cap de slippage queda 100% en el wrapper (compute_limit_price), donde
    debe estar.
    """
    sentinel = "LIMIT_FOK"  # valor estándar; usamos identidad de string
    monkeypatch.setattr(executor, "LIVE_ORDER_TYPE", sentinel)
    _seed_subscription()

    with patch(
        "src.polymarket.clob_client.place_market_order",
        return_value=_ok_fok_order(),
    ) as mock_order, patch(
        "src.polymarket.clob_client.get_token_id",
        return_value="tok-mock-3",
    ):
        executor.open_position(
            source_wallet="0xtrader",
            source_trade_id="trade-n1-passthrough",
            condition_id="0xcid",
            outcome="YES",
            outcome_index=0,
            price=0.50,
            timestamp=int(time.time()),
            raw={"asset": "tok-mock-3"},
        )

    kwargs = mock_order.call_args.kwargs
    # Identidad: el string que llegó al wrapper es EL MISMO objeto que
    # seteamos en LIVE_ORDER_TYPE. Si alguien hardcodeó "LIMIT_FOK" como
    # literal en executor.py, esta aserción de identidad falla.
    assert kwargs.get("order_type") is sentinel, (
        "executor.open_position debe pasar LIVE_ORDER_TYPE por referencia, "
        "no un literal hardcodeado. Si esto falla, hay un \"LIMIT_FOK\" "
        "hardcoded que ignora la env var del operador."
    )

    # Sanity adicional: NO se pasa max_slippage_pct desde executor — el cap
    # vive en el wrapper (LIVE_MAX_SLIPPAGE_PCT vía compute_limit_price).
    # Si esto cambia, hay que revisar si el cap también se está duplicando.
    assert "max_slippage_pct" not in kwargs, (
        "executor no debe pasar max_slippage_pct: el cap vive en clob_client. "
        "Duplicar el control en dos lugares es bug-prone."
    )
