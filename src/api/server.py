"""FastAPI app: API REST + sirve la PWA estática."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.copybot.learning import recent_events, summary
from src.copybot.selector import list_active, select_traders
from src.db.schema import db, init_db

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Polymarket Copy-Bot Dashboard", version="0.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    init_db()


# ---------- API REST ----------

@app.get("/api/summary")
def api_summary() -> dict:
    return summary()


@app.get("/api/copying")
def api_copying() -> list[dict]:
    return list_active()


@app.post("/api/select")
def api_select(top: int = Query(10, ge=1, le=50)) -> dict:
    return select_traders(top_n=top)


@app.get("/api/learning/events")
def api_learning(limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    return recent_events(limit=limit)


@app.get("/api/traders/top")
def api_top_traders(limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trader_metrics
            WHERE score > 0
            ORDER BY score DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/traders/{wallet}")
def api_trader_detail(wallet: str) -> dict:
    wallet = wallet.lower()
    with db() as conn:
        m = conn.execute(
            "SELECT * FROM trader_metrics WHERE wallet=?", (wallet,)
        ).fetchone()
        if not m:
            raise HTTPException(404, f"wallet {wallet} no tiene métricas")
        sub = conn.execute(
            "SELECT * FROM copy_subscriptions WHERE wallet=?", (wallet,)
        ).fetchone()
        recent_trades = conn.execute(
            """
            SELECT condition_id, side, outcome, price, size, usdc_value, timestamp
            FROM trades WHERE wallet=?
            ORDER BY timestamp DESC LIMIT 50
            """,
            (wallet,),
        ).fetchall()
        learn = conn.execute(
            """
            SELECT * FROM learning_events
            WHERE wallet=? ORDER BY created_at DESC LIMIT 50
            """,
            (wallet,),
        ).fetchall()
    return {
        "metrics": dict(m),
        "subscription": dict(sub) if sub else None,
        "recent_trades": [dict(t) for t in recent_trades],
        "learning_events": [dict(e) for e in learn],
    }


@app.get("/api/paper-trades")
def api_paper_trades(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    q = "SELECT * FROM paper_trades"
    params: list = []
    if status:
        q += " WHERE status=?"
        params.append(status)
    q += " ORDER BY entry_at DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(q, tuple(params)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/pnl-timeline")
def api_pnl_timeline(bucket: str = Query("hour", regex="^(hour|day)$")) -> dict:
    """Serie temporal de PnL acumulado.

    Devuelve { points: [{t, cum_pnl, count}, ...] } ordenado ascendente.
    """
    seconds = 3600 if bucket == "hour" else 86400
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                (exit_at / {seconds}) * {seconds} as t,
                SUM(COALESCE(pnl_usdc, 0)) as bucket_pnl,
                COUNT(*) as n
            FROM paper_trades
            WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss')
              AND exit_at IS NOT NULL
            GROUP BY t
            ORDER BY t ASC
            """,
        ).fetchall()
    points: list[dict] = []
    cum = 0.0
    for r in rows:
        cum += r["bucket_pnl"] or 0
        points.append(
            {"t": int(r["t"]), "cum_pnl": cum, "delta": r["bucket_pnl"], "count": r["n"]}
        )
    return {"bucket": bucket, "points": points}


@app.get("/api/markets/active")
def api_active_markets(limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT condition_id, question, slug, volume, liquidity, end_date
            FROM markets
            WHERE active=1 AND closed=0
            ORDER BY volume DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/health")
def api_health() -> dict:
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM markets").fetchone()["c"]
    return {"ok": True, "markets": n}


@app.get("/api/ws-status")
def api_ws_status() -> dict:
    """Snapshot del WS bridge.

    Si el WS bridge no arrancó (env ``WEBSOCKET_TRADES_ENABLED!=true``) las
    métricas siguen siendo válidas pero todos los counters están en 0 y
    ``connected=false``. Eso permite al dashboard distinguir "WS off"
    (uptime alto + 0 connect_attempts) de "WS dead" (connect_attempts >0
    pero connected=false hace rato).
    """
    import os
    from src.copybot.ws_metrics import metrics as ws_metrics
    snap = ws_metrics.snapshot()
    snap["enabled"] = (
        os.getenv("WEBSOCKET_TRADES_ENABLED", "false").lower() == "true"
    )
    return snap


# ---------- LIVE (Fase 5) ----------

@app.get("/api/live/summary")
def api_live_summary() -> dict:
    """Resumen del modo live (real o dry-run). Lee de live_trades."""
    import time as _t
    from src.config import (
        LIVE_BASE_USDC, LIVE_CAPITAL_USDC, LIVE_DRY_RUN, LIVE_MODE,
        LIVE_MAX_PER_WALLET_USDC, LIVE_MIN_EXPECTED_PNL_USDC,
        LIVE_MAX_SLIPPAGE_PCT, POLYMARKET_FUNDER_ADDRESS,
    )

    if LIVE_MODE:
        mode = "live_dry" if LIVE_DRY_RUN else "live_real"
    else:
        mode = "off"

    today_ts = int(_t.time()) - 86400
    with db() as conn:
        # Counts por estado
        rows = conn.execute(
            "SELECT status, dry_run, COUNT(*) as n, SUM(pnl_usdc) as pnl "
            "FROM live_trades GROUP BY status, dry_run"
        ).fetchall()

        opens = conn.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(entry_size_usdc), 0) as inv "
            "FROM live_trades WHERE status='open'"
        ).fetchone()

        # Wins/losses totales
        wl = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN status IN ('closed_win','settled_win') THEN 1 ELSE 0 END) as wins, "
            "  SUM(CASE WHEN status IN ('closed_loss','settled_loss') THEN 1 ELSE 0 END) as losses, "
            "  COALESCE(SUM(pnl_usdc), 0) as pnl, "
            "  SUM(CASE WHEN status IN ('closed_win','closed_loss','settled_win','settled_loss') THEN entry_size_usdc ELSE 0 END) as invested "
            "FROM live_trades"
        ).fetchone()

        # Hoy (ultimas 24h)
        today = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN status IN ('closed_win','settled_win') THEN 1 ELSE 0 END) as wins, "
            "  SUM(CASE WHEN status IN ('closed_loss','settled_loss') THEN 1 ELSE 0 END) as losses, "
            "  COALESCE(SUM(pnl_usdc), 0) as pnl "
            "FROM live_trades "
            "WHERE exit_at >= ? "
            "AND status IN ('closed_win','closed_loss','settled_win','settled_loss')",
            (today_ts,),
        ).fetchone()

        # Cuántos son dry vs real
        by_dry = conn.execute(
            "SELECT dry_run, COUNT(*) as n, COALESCE(SUM(pnl_usdc),0) as pnl "
            "FROM live_trades WHERE status IN "
            "('closed_win','closed_loss','settled_win','settled_loss') GROUP BY dry_run"
        ).fetchall()

        # Top wallets en live
        top_wallets = conn.execute(
            "SELECT substr(source_wallet,1,12) as wallet, COUNT(*) as n, "
            "  SUM(CASE WHEN status IN ('closed_win','settled_win') THEN 1 ELSE 0 END) as wins, "
            "  SUM(CASE WHEN status IN ('closed_loss','settled_loss') THEN 1 ELSE 0 END) as losses, "
            "  COALESCE(SUM(pnl_usdc),0) as pnl "
            "FROM live_trades "
            "WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss') "
            "GROUP BY source_wallet ORDER BY pnl DESC LIMIT 5"
        ).fetchall()

    wins = (wl["wins"] or 0) if wl else 0
    losses = (wl["losses"] or 0) if wl else 0
    pnl = float((wl["pnl"] or 0) if wl else 0)
    invested = float((wl["invested"] or 0) if wl else 0)
    win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else 0
    roi = (pnl / invested * 100) if invested > 0 else 0

    counts_by_dry = {int(r["dry_run"]): {"n": r["n"], "pnl": float(r["pnl"] or 0)} for r in by_dry}

    # Balance del CLOB (lazy import para no cargar py-clob-client si no es live)
    balance_usdc = None
    if LIVE_MODE:
        try:
            from src.polymarket.clob_client import get_balance
            balance_usdc = get_balance()
        except Exception:
            pass

    return {
        "mode": mode,
        "dry_run": LIVE_DRY_RUN,
        "config": {
            "capital_usdc": LIVE_CAPITAL_USDC,
            "base_usdc": LIVE_BASE_USDC,
            "max_per_wallet_usdc": LIVE_MAX_PER_WALLET_USDC,
            "min_expected_pnl_usdc": LIVE_MIN_EXPECTED_PNL_USDC,
            "max_slippage_pct": LIVE_MAX_SLIPPAGE_PCT,
        },
        "wallet": {
            "funder": POLYMARKET_FUNDER_ADDRESS,
            "balance_usdc": balance_usdc,
        },
        "open": {
            "n": opens["n"] if opens else 0,
            "invested_usdc": float(opens["inv"] if opens else 0),
        },
        "totals": {
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "pnl_usdc": pnl,
            "roi_pct": roi,
            "invested_usdc": invested,
        },
        "today": {
            "wins": (today["wins"] or 0) if today else 0,
            "losses": (today["losses"] or 0) if today else 0,
            "pnl_usdc": float((today["pnl"] or 0) if today else 0),
        },
        "by_dry_run": counts_by_dry,
        "top_wallets": [dict(r) for r in top_wallets],
    }


@app.get("/api/live/trades")
def api_live_trades(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """Lista live_trades. Filtrable por status."""
    q = "SELECT * FROM live_trades"
    params: list = []
    if status:
        q += " WHERE status=?"
        params.append(status)
    q += " ORDER BY entry_at DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(q, tuple(params)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/live/pnl-timeline")
def api_live_pnl_timeline(bucket: str = Query("hour", regex="^(hour|day)$")) -> dict:
    """Serie temporal de PnL acumulado en live_trades."""
    seconds = 3600 if bucket == "hour" else 86400
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                (exit_at / {seconds}) * {seconds} as t,
                SUM(COALESCE(pnl_usdc, 0)) as bucket_pnl,
                COUNT(*) as n
            FROM live_trades
            WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss')
              AND exit_at IS NOT NULL
            GROUP BY t
            ORDER BY t ASC
            """,
        ).fetchall()
    points: list[dict] = []
    cum = 0.0
    for r in rows:
        cum += r["bucket_pnl"] or 0
        points.append(
            {"t": int(r["t"]), "cum_pnl": cum, "delta": r["bucket_pnl"], "count": r["n"]}
        )
    return {"bucket": bucket, "points": points}


# ---------- Hyperliquid (HL bot dry-run) ----------
@app.get("/api/hl/summary")
def api_hl_summary() -> dict:
    """Summary del bot HL (dry-run, perps Hyperliquid)."""
    import time as _t
    from src.config import HL_CAPITAL_USDC, HL_BASE_USDC, HL_MAX_PER_WALLET_USDC

    today_ts = int(_t.time()) - 86400
    with db() as conn:
        wl = conn.execute(
            "SELECT SUM(CASE WHEN status='closed_win' THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN status='closed_loss' THEN 1 ELSE 0 END) losses, "
            "COALESCE(SUM(pnl_usdc),0) pnl FROM hl_trades"
        ).fetchone()
        opens = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(entry_size_usdc),0) inv FROM hl_trades WHERE status='open'"
        ).fetchone()
        today = conn.execute(
            "SELECT SUM(CASE WHEN status='closed_win' THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN status='closed_loss' THEN 1 ELSE 0 END) losses, "
            "COALESCE(SUM(pnl_usdc),0) pnl FROM hl_trades "
            "WHERE exit_at >= ? AND status LIKE 'closed_%'",
            (today_ts,),
        ).fetchone()
        wallets_n = conn.execute("SELECT COUNT(*) n FROM hl_subscriptions WHERE status='active'").fetchone()["n"]
        wallets_drop = conn.execute("SELECT COUNT(*) n FROM hl_subscriptions WHERE status='dropped'").fetchone()["n"]
        top = conn.execute(
            "SELECT source_wallet wallet, COUNT(*) n, "
            "SUM(CASE WHEN status='closed_win' THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN status='closed_loss' THEN 1 ELSE 0 END) losses, "
            "COALESCE(SUM(pnl_usdc),0) pnl FROM hl_trades "
            "WHERE status LIKE 'closed_%' GROUP BY source_wallet ORDER BY pnl DESC LIMIT 5"
        ).fetchall()
    wins = (wl["wins"] or 0) if wl else 0
    losses = (wl["losses"] or 0) if wl else 0
    pnl = float((wl["pnl"] or 0) if wl else 0)
    win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else 0
    return {
        "config": {"capital_usdc": HL_CAPITAL_USDC, "base_usdc": HL_BASE_USDC,
                   "max_per_wallet_usdc": HL_MAX_PER_WALLET_USDC},
        "open": {"n": opens["n"] if opens else 0, "invested_usdc": float(opens["inv"] if opens else 0)},
        "totals": {"wins": wins, "losses": losses, "win_rate": win_rate, "pnl_usdc": pnl},
        "today": {"wins": (today["wins"] or 0) if today else 0,
                  "losses": (today["losses"] or 0) if today else 0,
                  "pnl_usdc": float((today["pnl"] or 0) if today else 0)},
        "wallets": {"active": wallets_n, "dropped": wallets_drop},
        "top_wallets": [dict(r) for r in top],
    }


@app.get("/api/hl/trades")
def api_hl_trades(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM hl_trades ORDER BY entry_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- dYdX v4 (DX bot dry-run) ----------
@app.get("/api/dx/summary")
def api_dx_summary() -> dict:
    """Summary del bot DX (dry-run, perps dYdX v4)."""
    import time as _t
    from src.config import DX_CAPITAL_USDC, DX_BASE_USDC, DX_MAX_PER_WALLET_USDC

    today_ts = int(_t.time()) - 86400
    with db() as conn:
        wl = conn.execute(
            "SELECT SUM(CASE WHEN status='closed_win' THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN status='closed_loss' THEN 1 ELSE 0 END) losses, "
            "COALESCE(SUM(pnl_usdc),0) pnl FROM dx_trades"
        ).fetchone()
        opens = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(entry_size_usdc),0) inv FROM dx_trades WHERE status='open'"
        ).fetchone()
        today = conn.execute(
            "SELECT SUM(CASE WHEN status='closed_win' THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN status='closed_loss' THEN 1 ELSE 0 END) losses, "
            "COALESCE(SUM(pnl_usdc),0) pnl FROM dx_trades "
            "WHERE exit_at >= ? AND status LIKE 'closed_%'",
            (today_ts,),
        ).fetchone()
        wallets_n = conn.execute("SELECT COUNT(*) n FROM dx_subscriptions WHERE status='active'").fetchone()["n"]
        wallets_drop = conn.execute("SELECT COUNT(*) n FROM dx_subscriptions WHERE status='dropped'").fetchone()["n"]
        top = conn.execute(
            "SELECT source_wallet wallet, COUNT(*) n, "
            "SUM(CASE WHEN status='closed_win' THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN status='closed_loss' THEN 1 ELSE 0 END) losses, "
            "COALESCE(SUM(pnl_usdc),0) pnl FROM dx_trades "
            "WHERE status LIKE 'closed_%' GROUP BY source_wallet ORDER BY pnl DESC LIMIT 5"
        ).fetchall()
    wins = (wl["wins"] or 0) if wl else 0
    losses = (wl["losses"] or 0) if wl else 0
    pnl = float((wl["pnl"] or 0) if wl else 0)
    win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else 0
    return {
        "config": {"capital_usdc": DX_CAPITAL_USDC, "base_usdc": DX_BASE_USDC,
                   "max_per_wallet_usdc": DX_MAX_PER_WALLET_USDC},
        "open": {"n": opens["n"] if opens else 0, "invested_usdc": float(opens["inv"] if opens else 0)},
        "totals": {"wins": wins, "losses": losses, "win_rate": win_rate, "pnl_usdc": pnl},
        "today": {"wins": (today["wins"] or 0) if today else 0,
                  "losses": (today["losses"] or 0) if today else 0,
                  "pnl_usdc": float((today["pnl"] or 0) if today else 0)},
        "wallets": {"active": wallets_n, "dropped": wallets_drop},
        "top_wallets": [dict(r) for r in top],
    }


@app.get("/api/dx/trades")
def api_dx_trades(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM dx_trades ORDER BY entry_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Estáticos / PWA ----------
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/manifest.json")
def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{path:path}")
def spa_fallback(path: str) -> FileResponse:
    f = STATIC_DIR / path
    if f.is_file():
        return FileResponse(f)
    return FileResponse(STATIC_DIR / "index.html")
