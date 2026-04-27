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
