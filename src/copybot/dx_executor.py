"""dYdX v4 dry-run executor — espejo de hl_executor para perps de dYdX.

Igual idea: copia trades de wallets sin mandar órdenes reales, simula
fills al mid actual + slippage pesimista. Distinto exchange = distinta
diversificación temporal y geográfica de actividad.

Datos clave del fill de dYdX v4:
  ticker (BTC-USD), side (BUY/SELL), size, price, type (LIMIT/MARKET/...),
  liquidity (MAKER/TAKER), eventId, transactionHash, createdAt, ...

Convención: usamos `is_buy=1` para BUY (long), 0 para SELL (close).
Como dYdX no expone is_open vs is_close en el fill, lo inferimos por
posición existente (si tenemos open de ese ticker, el siguiente trade
del mismo wallet a contra-side cierra; sino abre).
"""
from __future__ import annotations

import json
import logging
import time

from src.config import (
    DX_BASE_USDC,
    DX_CAPITAL_USDC,
    DX_DRY_SLIPPAGE_PCT,
    DX_MAX_LEVERAGE,
    DX_MAX_PER_WALLET_USDC,
    DX_MIN_EXPECTED_PNL_USDC,
    DX_ALLOWED_TICKERS,
    DX_LIQUIDATION_BUFFER,
    DX_MAX_FILLS_PER_WALLET_24H,
)
from src.db.schema import db, tx

log = logging.getLogger(__name__)

EPSILON = 1e-6


def _log_reject(source_wallet, ticker, is_buy, price, reason, detail=None):
    try:
        with tx() as conn:
            conn.execute(
                "INSERT INTO dx_rejects (at, source_wallet, ticker, is_buy, price, reason, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), source_wallet, ticker, is_buy, price, reason, detail),
            )
    except Exception as e:
        log.warning("dx_log_reject failed: %s", e)


def _check_kill_switch(conn) -> bool:
    """Comparte kill_switch con PM y HL."""
    r = conn.execute("SELECT value FROM bot_state WHERE key='kill_switch'").fetchone()
    return bool(r and r["value"] == "active")


def _apply_dry_slippage(is_buy: int, price: float) -> float:
    if price is None or price <= 0:
        return price
    if is_buy:
        return price * (1 + DX_DRY_SLIPPAGE_PCT)
    return price * (1 - DX_DRY_SLIPPAGE_PCT)


def _liquidation_price(entry_price: float, is_buy: int, leverage: float) -> float | None:
    if leverage <= 1.01:
        return None
    if is_buy:
        return entry_price * (1 - 0.95 / leverage)
    return entry_price * (1 + 0.95 / leverage)


def _calc_pnl(entry_price: float, exit_price: float, is_buy: int, size_usdc: float, leverage: float = 1.0) -> float:
    if entry_price <= 0:
        return 0.0
    pct = (exit_price - entry_price) / entry_price
    if not is_buy:
        pct = -pct
    return pct * size_usdc * leverage


def _open_position_validate_dx(conn, *, source_wallet, source_fill_id, ticker, is_buy, price, leverage):
    if _check_kill_switch(conn):
        _log_reject(source_wallet, ticker, is_buy, price, "kill_switch")
        return None, "kill_switch"

    sub = conn.execute(
        "SELECT sizing_mult, status FROM dx_subscriptions WHERE wallet=?",
        (source_wallet,),
    ).fetchone()
    if not sub:
        _log_reject(source_wallet, ticker, is_buy, price, "no_subscription")
        return None, "no_subscription"
    if sub["status"] != "active":
        _log_reject(source_wallet, ticker, is_buy, price, "inactive",
                    detail=json.dumps({"sub_status": sub["status"]}))
        return None, "inactive"
    sm = sub["sizing_mult"]
    sizing = 1.0 if sm is None else float(sm)
    if sizing <= EPSILON:
        return None, "inactive"

    # Ticker allowlist (vacío = todos)
    if DX_ALLOWED_TICKERS and ticker.upper() not in DX_ALLOWED_TICKERS:
        _log_reject(source_wallet, ticker, is_buy, price, "ticker_blocked",
                    detail=json.dumps({"ticker": ticker, "allowed": DX_ALLOWED_TICKERS}))
        return None, "ticker_blocked"

    # Anti-scalper: limita fills por wallet/24h
    recent = conn.execute(
        "SELECT COUNT(*) c FROM dx_trades WHERE source_wallet=? AND entry_at >= ?",
        (source_wallet, int(time.time()) - 86400),
    ).fetchone()["c"]
    if recent >= DX_MAX_FILLS_PER_WALLET_24H:
        _log_reject(source_wallet, ticker, is_buy, price, "scalper_limit",
                    detail=json.dumps({"recent_24h": recent, "max": DX_MAX_FILLS_PER_WALLET_24H}))
        if recent >= DX_MAX_FILLS_PER_WALLET_24H * 2:
            try:
                conn.execute(
                    "UPDATE dx_subscriptions SET status='dropped', "
                    "stopped_at=datetime('now'), notes='hft_auto_drop' WHERE wallet=?",
                    (source_wallet,),
                )
                log.info("DX auto-drop scalper: %s.. (%d fills/24h)", source_wallet[:14], recent)
            except Exception:
                pass
        return None, "scalper_limit"

    # Duplicate
    dup = conn.execute(
        "SELECT id FROM dx_trades WHERE source_fill_id=?", (source_fill_id,),
    ).fetchone()
    if dup:
        return None, "duplicate"

    if leverage > DX_MAX_LEVERAGE + EPSILON:
        _log_reject(source_wallet, ticker, is_buy, price, "leverage_too_high",
                    detail=json.dumps({"src_lev": leverage, "max": DX_MAX_LEVERAGE}))
        return None, "leverage_too_high"

    size_usdc = DX_BASE_USDC * sizing

    expected = size_usdc * 0.05
    if expected < DX_MIN_EXPECTED_PNL_USDC:
        _log_reject(source_wallet, ticker, is_buy, price, "expected_pnl_too_low",
                    detail=json.dumps({"size": size_usdc, "expected": expected}))
        return None, "expected_pnl_too_low"

    wallet_open = conn.execute(
        "SELECT COALESCE(SUM(entry_size_usdc),0) v FROM dx_trades WHERE source_wallet=? AND status='open'",
        (source_wallet,),
    ).fetchone()["v"]
    if wallet_open + size_usdc > DX_MAX_PER_WALLET_USDC + EPSILON:
        _log_reject(source_wallet, ticker, is_buy, price, "wallet_concentration",
                    detail=json.dumps({"open": wallet_open, "size": size_usdc, "cap": DX_MAX_PER_WALLET_USDC}))
        return None, "wallet_concentration"

    global_open = conn.execute(
        "SELECT COALESCE(SUM(entry_size_usdc),0) v FROM dx_trades WHERE status='open'"
    ).fetchone()["v"]
    if global_open + size_usdc > DX_CAPITAL_USDC + EPSILON:
        _log_reject(source_wallet, ticker, is_buy, price, "capital_full",
                    detail=json.dumps({"open": global_open, "size": size_usdc, "cap": DX_CAPITAL_USDC}))
        return None, "capital_full"

    return size_usdc, None


def open_position(*, source_wallet, source_fill_id, ticker, is_buy, source_price,
                  leverage=1.0, timestamp, raw=None) -> tuple[int | None, str | None]:
    is_buy_int = 1 if is_buy else 0
    with tx() as conn:
        size_usdc, reject = _open_position_validate_dx(
            conn,
            source_wallet=source_wallet, source_fill_id=source_fill_id,
            ticker=ticker, is_buy=is_buy_int, price=source_price, leverage=leverage,
        )
        if reject:
            return None, reject

        actual_entry = _apply_dry_slippage(is_buy_int, source_price)
        liq_px = _liquidation_price(actual_entry, is_buy_int, leverage)

        cur = conn.execute(
            """
            INSERT INTO dx_trades
                (source_wallet, source_fill_id, ticker, is_buy, leverage,
                 entry_at, entry_price, peak_price, entry_size_usdc,
                 liquidation_price, status, dry_run)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
            """,
            (source_wallet, source_fill_id, ticker, is_buy_int, leverage,
             timestamp, actual_entry, actual_entry, size_usdc,
             liq_px, "open"),
        )
        trade_id = cur.lastrowid

    log.info("DX OPEN #%d %s %s %s @ %.4f size=$%.2f lev=%.1f",
             trade_id, source_wallet[:14], ticker,
             "LONG" if is_buy_int else "SHORT", actual_entry, size_usdc, leverage)
    return trade_id, None


def close_position(*, source_wallet, ticker, is_buy, source_price, timestamp) -> int | None:
    """Cierra trade open matching cuando wallet original cierra.

    En dYdX el fill direction (buy/sell) es la del wallet, no nuestra. Si
    abrió LONG (is_buy=1), después cierra con is_buy=0. Para matchear el
    trade open con is_buy_int correcto, invertimos: si recibimos is_buy=0
    (sell), buscamos open con is_buy=1 (long).
    """
    is_buy_int = 1 if is_buy else 0
    # Match trade abierto que tenga is_buy OPUESTO al fill que cierra
    matching_is_buy = 1 - is_buy_int
    with tx() as conn:
        row = conn.execute(
            "SELECT * FROM dx_trades WHERE source_wallet=? AND ticker=? AND is_buy=? AND status='open' "
            "ORDER BY entry_at LIMIT 1",
            (source_wallet, ticker, matching_is_buy),
        ).fetchone()
        if not row:
            return None

        # Slippage en la salida (lado opuesto al original)
        actual_exit = _apply_dry_slippage(is_buy_int, source_price)
        pnl = _calc_pnl(row["entry_price"], actual_exit, matching_is_buy,
                       row["entry_size_usdc"], row["leverage"])
        new_status = "closed_win" if pnl > 0 else "closed_loss"
        conn.execute(
            "UPDATE dx_trades SET exit_at=?, exit_price=?, exit_size_usdc=?, "
            "pnl_usdc=?, status=?, exit_reason=? WHERE id=?",
            (timestamp, actual_exit, row["entry_size_usdc"], pnl, new_status,
             "source_close", row["id"]),
        )

    log.info("DX CLOSE #%d %s pnl=$%.2f", row["id"], new_status, pnl)
    try:
        from src.copybot.notifier import dx_close
        with db() as c2:
            tot = c2.execute(
                "SELECT COALESCE(SUM(pnl_usdc),0) p FROM dx_trades WHERE status LIKE 'closed_%'"
            ).fetchone()["p"]
        dx_close(source_wallet=source_wallet, ticker=ticker, is_buy=matching_is_buy,
                 pnl_usdc=pnl, accumulated=tot, exit_reason="source_close")
    except Exception as e:
        log.warning("dx_close notif failed: %s", e)
    return row["id"]


def force_close(dx_trade_id: int, exit_price: float, *, reason: str) -> None:
    with tx() as conn:
        row = conn.execute("SELECT * FROM dx_trades WHERE id=? AND status='open'", (dx_trade_id,)).fetchone()
        if not row:
            return
        is_buy_int = row["is_buy"]
        # Salida = side opuesto al entry
        actual_exit = _apply_dry_slippage(0 if is_buy_int else 1, exit_price)
        pnl = _calc_pnl(row["entry_price"], actual_exit, is_buy_int,
                       row["entry_size_usdc"], row["leverage"])
        new_status = "closed_win" if pnl > 0 else "closed_loss"
        conn.execute(
            "UPDATE dx_trades SET exit_at=?, exit_price=?, exit_size_usdc=?, "
            "pnl_usdc=?, status=?, exit_reason=? WHERE id=?",
            (int(time.time()), actual_exit, row["entry_size_usdc"], pnl,
             new_status, reason, dx_trade_id),
        )

    log.info("DX FORCE_CLOSE #%d %s pnl=$%.2f reason=%s",
             dx_trade_id, new_status, pnl, reason)
    try:
        from src.copybot.notifier import dx_close
        with db() as c2:
            tot = c2.execute(
                "SELECT COALESCE(SUM(pnl_usdc),0) p FROM dx_trades WHERE status LIKE 'closed_%'"
            ).fetchone()["p"]
        dx_close(source_wallet=row["source_wallet"], ticker=row["ticker"],
                 is_buy=is_buy_int, pnl_usdc=pnl, accumulated=tot, exit_reason=reason)
    except Exception as e:
        log.warning("dx_close notif failed: %s", e)
