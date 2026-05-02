"""HL dry-run executor — espejo de executor.py adaptado para perps.

Diferencias clave vs Polymarket:
  - asset = coin string (BTC/ETH/SOL/...) en vez de condition_id
  - is_buy: 1=long, 0=short (vs binario buy outcome)
  - leverage opcional, default 1× (sin riesgo de liquidación)
  - sin filtro de expiry (perps no expiran)
  - SL/TP/trailing en % de PnL escalado por leverage

DRY-RUN siempre: simulamos fill al mid actual + slippage pesimista.
NO se mandan órdenes reales al exchange en esta fase.
"""
from __future__ import annotations

import json
import logging
import time

from src.config import (
    HL_BASE_USDC,
    HL_CAPITAL_USDC,
    HL_DRY_SLIPPAGE_PCT,
    HL_MAX_LEVERAGE,
    HL_MAX_PER_WALLET_USDC,
    HL_MIN_EXPECTED_PNL_USDC,
    HL_ALLOWED_COINS,
    HL_LIQUIDATION_BUFFER,
)
from src.db.schema import db, tx

log = logging.getLogger(__name__)

EPSILON = 1e-6


def _log_reject(source_wallet, coin, is_buy, price, reason, detail=None):
    try:
        with tx() as conn:
            conn.execute(
                "INSERT INTO hl_rejects (at, source_wallet, coin, is_buy, price, reason, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), source_wallet, coin, is_buy, price, reason, detail),
            )
    except Exception as e:
        log.warning("hl_log_reject failed: %s", e)


def _check_kill_switch(conn) -> bool:
    """Comparte kill_switch con el bot Polymarket."""
    r = conn.execute("SELECT value FROM bot_state WHERE key='kill_switch'").fetchone()
    return bool(r and r["value"] == "active")


def _apply_dry_slippage(is_buy: int, price: float) -> float:
    """LONG paga más al entrar / recibe menos al salir. Idem inverso para SHORT."""
    if price is None or price <= 0:
        return price
    if is_buy:  # entry como LONG
        return price * (1 + HL_DRY_SLIPPAGE_PCT)
    else:  # entry como SHORT
        return price * (1 - HL_DRY_SLIPPAGE_PCT)


def _liquidation_price(entry_price: float, is_buy: int, leverage: float) -> float | None:
    """Aproximación de precio de liquidación.

    LONG: liq = entry × (1 - 0.95/lev)   (cae 95% del margen)
    SHORT: liq = entry × (1 + 0.95/lev)
    Para leverage=1, no hay liq práctica → None.
    """
    if leverage <= 1.01:
        return None
    if is_buy:
        return entry_price * (1 - 0.95 / leverage)
    else:
        return entry_price * (1 + 0.95 / leverage)


def _calc_pnl(entry_price: float, exit_price: float, is_buy: int, size_usdc: float, leverage: float = 1.0) -> float:
    """PnL en USDC. Para LONG: (exit-entry)/entry × size × leverage."""
    if entry_price <= 0:
        return 0.0
    pct = (exit_price - entry_price) / entry_price
    if not is_buy:
        pct = -pct
    return pct * size_usdc * leverage


def _open_position_validate_hl(conn, *, source_wallet, source_fill_id, coin, is_buy, price, leverage, timestamp):
    """Validador. Devuelve (size_usdc, None) si pasa, o (None, reject_reason)."""
    if _check_kill_switch(conn):
        _log_reject(source_wallet, coin, is_buy, price, "kill_switch")
        return None, "kill_switch"

    sub = conn.execute(
        "SELECT sizing_mult, status FROM hl_subscriptions WHERE wallet=?",
        (source_wallet,),
    ).fetchone()
    if not sub:
        _log_reject(source_wallet, coin, is_buy, price, "no_subscription")
        return None, "no_subscription"
    if sub["status"] != "active":
        _log_reject(source_wallet, coin, is_buy, price, "inactive",
                    detail=json.dumps({"sub_status": sub["status"]}))
        return None, "inactive"
    sm = sub["sizing_mult"]
    sizing = 1.0 if sm is None else float(sm)
    if sizing <= EPSILON:
        return None, "inactive"

    # Coin allowlist
    if HL_ALLOWED_COINS and coin.upper() not in HL_ALLOWED_COINS:
        _log_reject(source_wallet, coin, is_buy, price, "coin_blocked",
                    detail=json.dumps({"coin": coin, "allowed": HL_ALLOWED_COINS}))
        return None, "coin_blocked"

    # Duplicate
    dup = conn.execute(
        "SELECT id FROM hl_trades WHERE source_fill_id=?", (source_fill_id,),
    ).fetchone()
    if dup:
        return None, "duplicate"

    # Leverage cap
    if leverage > HL_MAX_LEVERAGE + EPSILON:
        _log_reject(source_wallet, coin, is_buy, price, "leverage_too_high",
                    detail=json.dumps({"src_lev": leverage, "max": HL_MAX_LEVERAGE}))
        return None, "leverage_too_high"

    size_usdc = HL_BASE_USDC * sizing

    # Min expected pnl (anti-fee)
    expected = size_usdc * 0.05  # asumimos 5% gain promedio en wins
    if expected < HL_MIN_EXPECTED_PNL_USDC:
        _log_reject(source_wallet, coin, is_buy, price, "expected_pnl_too_low",
                    detail=json.dumps({"size": size_usdc, "expected": expected}))
        return None, "expected_pnl_too_low"

    # Per-wallet concentration
    wallet_open = conn.execute(
        "SELECT COALESCE(SUM(entry_size_usdc),0) v FROM hl_trades WHERE source_wallet=? AND status='open'",
        (source_wallet,),
    ).fetchone()["v"]
    if wallet_open + size_usdc > HL_MAX_PER_WALLET_USDC + EPSILON:
        _log_reject(source_wallet, coin, is_buy, price, "wallet_concentration",
                    detail=json.dumps({"open": wallet_open, "size": size_usdc, "cap": HL_MAX_PER_WALLET_USDC}))
        return None, "wallet_concentration"

    # Global capital
    global_open = conn.execute(
        "SELECT COALESCE(SUM(entry_size_usdc),0) v FROM hl_trades WHERE status='open'"
    ).fetchone()["v"]
    if global_open + size_usdc > HL_CAPITAL_USDC + EPSILON:
        _log_reject(source_wallet, coin, is_buy, price, "capital_full",
                    detail=json.dumps({"open": global_open, "size": size_usdc, "cap": HL_CAPITAL_USDC}))
        return None, "capital_full"

    return size_usdc, None


def open_position(*, source_wallet, source_fill_id, coin, is_buy, source_price,
                  leverage=1.0, timestamp, raw=None) -> tuple[int | None, str | None]:
    """Abre una posición SIMULADA en hl_trades.

    Retorna (trade_id, reject_reason). Si reject_reason es None, trade_id es válido.
    """
    is_buy_int = 1 if is_buy else 0
    with tx() as conn:
        size_usdc, reject = _open_position_validate_hl(
            conn,
            source_wallet=source_wallet,
            source_fill_id=source_fill_id,
            coin=coin,
            is_buy=is_buy_int,
            price=source_price,
            leverage=leverage,
            timestamp=timestamp,
        )
        if reject:
            return None, reject

        # Aplicar slippage al entry
        actual_entry = _apply_dry_slippage(is_buy_int, source_price)
        liq_px = _liquidation_price(actual_entry, is_buy_int, leverage)

        cur = conn.execute(
            """
            INSERT INTO hl_trades
                (source_wallet, source_fill_id, coin, is_buy, leverage,
                 entry_at, entry_price, peak_price, entry_size_usdc,
                 liquidation_price, status, dry_run)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
            """,
            (source_wallet, source_fill_id, coin, is_buy_int, leverage,
             timestamp, actual_entry, actual_entry, size_usdc,
             liq_px, "open"),
        )
        trade_id = cur.lastrowid

    log.info("HL OPEN #%d %s %s %s @ %.4f size=$%.2f lev=%.1f%s",
             trade_id, source_wallet[:10], coin, "LONG" if is_buy_int else "SHORT",
             actual_entry, size_usdc, leverage,
             f" liq={liq_px:.4f}" if liq_px else "")
    return trade_id, None


def close_position(*, source_wallet, coin, is_buy, source_price, timestamp) -> int | None:
    """Cierra el trade open matching cuando el wallet origen cierra."""
    is_buy_int = 1 if is_buy else 0
    with tx() as conn:
        # Buscar trade open del mismo wallet+coin con MISMO is_buy (long-close cierra long, etc.)
        # En HL: 'Close Long' implica que abrió LONG → buscamos is_buy=1
        # Si dir='Close Long', el is_buy del CLOSE es 0 (vende), pero matchea trades is_buy=1
        # Lo gestionamos en el classifier del runner.
        row = conn.execute(
            "SELECT * FROM hl_trades WHERE source_wallet=? AND coin=? AND is_buy=? AND status='open' "
            "ORDER BY entry_at LIMIT 1",
            (source_wallet, coin, is_buy_int),
        ).fetchone()
        if not row:
            return None

        actual_exit = _apply_dry_slippage(0 if is_buy_int else 1, source_price)
        pnl = _calc_pnl(row["entry_price"], actual_exit, is_buy_int,
                       row["entry_size_usdc"], row["leverage"])
        new_status = "closed_win" if pnl > 0 else "closed_loss"
        conn.execute(
            "UPDATE hl_trades SET exit_at=?, exit_price=?, exit_size_usdc=?, "
            "pnl_usdc=?, status=?, exit_reason=? WHERE id=?",
            (timestamp, actual_exit, row["entry_size_usdc"], pnl, new_status,
             "source_close", row["id"]),
        )

    log.info("HL CLOSE #%d %s pnl=$%.2f", row["id"], new_status, pnl)
    try:
        from src.copybot.notifier import hl_close
        # PnL acumulado HL
        with db() as c:
            tot = c.execute(
                "SELECT COALESCE(SUM(pnl_usdc),0) p FROM hl_trades WHERE status LIKE 'closed_%'"
            ).fetchone()["p"]
        hl_close(source_wallet=source_wallet, coin=coin, is_buy=is_buy_int,
                 pnl_usdc=pnl, accumulated=tot, exit_reason="source_close")
    except Exception as e:
        log.warning("hl_close notif failed: %s", e)
    return row["id"]


def force_close(hl_trade_id: int, exit_price: float, *, reason: str) -> None:
    """SL/TP/trailing/liquidation force close."""
    with tx() as conn:
        row = conn.execute("SELECT * FROM hl_trades WHERE id=? AND status='open'", (hl_trade_id,)).fetchone()
        if not row:
            return
        is_buy_int = row["is_buy"]
        actual_exit = _apply_dry_slippage(0 if is_buy_int else 1, exit_price)
        pnl = _calc_pnl(row["entry_price"], actual_exit, is_buy_int,
                       row["entry_size_usdc"], row["leverage"])
        new_status = "closed_win" if pnl > 0 else "closed_loss"
        conn.execute(
            "UPDATE hl_trades SET exit_at=?, exit_price=?, exit_size_usdc=?, "
            "pnl_usdc=?, status=?, exit_reason=? WHERE id=?",
            (int(time.time()), actual_exit, row["entry_size_usdc"], pnl,
             new_status, reason, hl_trade_id),
        )

    log.info("HL FORCE_CLOSE #%d %s pnl=$%.2f reason=%s", hl_trade_id, new_status, pnl, reason)
    try:
        from src.copybot.notifier import hl_close
        with db() as c:
            tot = c.execute(
                "SELECT COALESCE(SUM(pnl_usdc),0) p FROM hl_trades WHERE status LIKE 'closed_%'"
            ).fetchone()["p"]
        hl_close(source_wallet=row["source_wallet"], coin=row["coin"], is_buy=is_buy_int,
                 pnl_usdc=pnl, accumulated=tot, exit_reason=reason)
    except Exception as e:
        log.warning("hl_close notif failed: %s", e)
