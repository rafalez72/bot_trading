"""FastAPI app: API REST + sirve la PWA estática."""
from __future__ import annotations

import logging
import os
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


@app.post("/api/admin/kill-switch/reset")
def api_kill_switch_reset(rebaseline_peak: bool = Query(True)) -> dict:
    """Reset manual del kill switch + rebaseline peak balance al capital
    efectivo actual (default true). Sin rebaseline, drawdown legacy puede
    reactivar el kill switch al instante.
    """
    from src.copybot.risk import reset_kill_switch, kill_switch_status
    reset_kill_switch(rebaseline_peak=rebaseline_peak)
    return {"ok": True, "status": kill_switch_status(), "rebaseline_peak": rebaseline_peak}


@app.get("/api/admin/kill-switch/status")
def api_kill_switch_status() -> dict:
    from src.copybot.risk import kill_switch_status
    return kill_switch_status()


@app.get("/api/admin/version")
def api_version() -> dict:
    """Retorna commit SHA y timestamp del build Docker actual.

    GIT_SHA y BUILD_TIME se bakean en la imagen via build-args
    (ver Dockerfile + .github/workflows/docker-publish.yml).
    Útil para confirmar que un deploy bajó la imagen esperada.
    """
    return {
        "git_sha": os.getenv("GIT_SHA", "unknown"),
        "build_time": os.getenv("BUILD_TIME", "unknown"),
    }


@app.get("/api/admin/config")
def api_admin_config() -> dict:
    """Reporta valores runtime de gates de estrategias + si vienen de env
    override o de default code. Útil para diagnosticar discrepancias.
    """
    def _source(env_key: str) -> str:
        return "env" if os.getenv(env_key) is not None else "default"

    from src.config import (
        LIVE_MODE,
        MM_ENABLED,
        ADVERSARIAL_ENABLED,
        SPIKE_ARB_ENABLED,
        LONG_HORIZON_ENABLED,
        HEDGE_ENABLED,
    )
    crypto_arb_enabled = os.getenv("CRYPTO_ARB_ENABLED", "false").lower() == "true"
    return {
        "live_mode": LIVE_MODE,
        "strategies": {
            "crypto_arb": {
                "enabled": crypto_arb_enabled,
                "source": _source("CRYPTO_ARB_ENABLED"),
            },
            "market_maker": {
                "enabled": MM_ENABLED,
                "source": _source("MM_ENABLED"),
            },
            "spike_arb": {
                "enabled": SPIKE_ARB_ENABLED,
                "source": _source("SPIKE_ARB_ENABLED"),
            },
            "adversarial": {
                "enabled": ADVERSARIAL_ENABLED,
                "source": _source("ADVERSARIAL_ENABLED"),
            },
            "long_horizon": {
                "enabled": LONG_HORIZON_ENABLED,
                "source": _source("LONG_HORIZON_ENABLED"),
            },
            "hedge": {
                "enabled": HEDGE_ENABLED,
                "source": _source("HEDGE_ENABLED"),
            },
        },
    }


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


@app.get("/api/crypto-arb-status")
def api_crypto_arb_status() -> dict:
    """Snapshot del bot crypto_arb (Nivel 2).

    Las métricas viven en el proceso runner (otro container). El runner
    persiste el snapshot a `data/crypto_arb_metrics.json` cada 5s; este
    endpoint lo lee. Si el archivo no existe (bot deshabilitado o
    nunca arrancó), devolvemos snapshot vacío con `enabled` derivado del .env.
    """
    from src.copybot.crypto_arb import (
        CryptoArbConfig, metrics as ca_metrics, read_snapshot_from_file,
    )
    cfg = CryptoArbConfig.from_env()
    snap = read_snapshot_from_file()
    if snap is None:
        snap = ca_metrics.snapshot()
        snap["_source"] = "in_proc_empty"
    else:
        snap["_source"] = "runner_file"
    snap["enabled"] = cfg.enabled
    snap["config"] = {
        "check_interval_s": cfg.check_interval_s,
        "momentum_threshold_pct": cfg.momentum_threshold_pct,
        "max_mid_target": cfg.max_mid_target,
        "bet_size_usdc": cfg.bet_size_usdc,
        "symbols": list(cfg.symbols),
    }
    return snap


@app.get("/api/strategies/status")
def api_strategies_status() -> dict:
    """Snapshot agregado de las 7 strategies del bot.

    Por cada strategy devuelve:
        - enabled: si está activa según .env
        - open: cantidad de trades/orders abiertos
        - pnl_24h: PnL realizado últimas 24h
        - pnl_total: PnL realizado total
        - last_activity_at: epoch del último evento (open/close)
        - meta: dict opcional con extras (in-proc metrics si aplican)

    Diseñado para falla blanda: si una tabla no existe (porque la strategy
    nunca corrió), devuelve zeros. No tira 500.
    """
    import os
    import time as _t

    today_ts = int(_t.time()) - 86400

    def _safe_query(q: str, params: tuple = ()) -> dict:
        """Devuelve {'open': n, 'pnl_24h': x, 'pnl_total': y, 'last_at': ts}
        o zeros si la tabla no existe."""
        try:
            with db() as conn:
                row = conn.execute(q, params).fetchone()
            if not row:
                return {"open": 0, "pnl_24h": 0.0, "pnl_total": 0.0, "last_at": None}
            return {
                "open": int(row["open_n"] or 0),
                "pnl_24h": float(row["pnl_24h"] or 0),
                "pnl_total": float(row["pnl_total"] or 0),
                "last_at": int(row["last_at"]) if row["last_at"] else None,
            }
        except Exception:
            return {"open": 0, "pnl_24h": 0.0, "pnl_total": 0.0, "last_at": None}

    # 1. N1 copybot — paper_trades sin source_wallet 'crypto_arb*'
    n1 = _safe_query(
        """
        SELECT
          SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_n,
          COALESCE(SUM(CASE WHEN status IN ('closed_win','closed_loss','settled_win','settled_loss')
                              AND exit_at >= ? THEN pnl_usdc ELSE 0 END), 0) AS pnl_24h,
          COALESCE(SUM(CASE WHEN status IN ('closed_win','closed_loss','settled_win','settled_loss')
                              THEN pnl_usdc ELSE 0 END), 0) AS pnl_total,
          MAX(COALESCE(exit_at, entry_at)) AS last_at
        FROM paper_trades
        WHERE source_wallet NOT IN ('crypto_arb', 'crypto_arb_hedge')
        """,
        (today_ts,),
    )
    n1["enabled"] = True

    # 2. crypto_arb — paper_trades con source_wallet='crypto_arb' + metrics file
    ca = _safe_query(
        """
        SELECT
          SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_n,
          COALESCE(SUM(CASE WHEN status IN ('closed_win','closed_loss','settled_win','settled_loss')
                              AND exit_at >= ? THEN pnl_usdc ELSE 0 END), 0) AS pnl_24h,
          COALESCE(SUM(CASE WHEN status IN ('closed_win','closed_loss','settled_win','settled_loss')
                              THEN pnl_usdc ELSE 0 END), 0) AS pnl_total,
          MAX(COALESCE(exit_at, entry_at)) AS last_at
        FROM paper_trades
        WHERE source_wallet='crypto_arb'
        """,
        (today_ts,),
    )
    ca["enabled"] = os.getenv("CRYPTO_ARB_ENABLED", "false").lower() == "true"
    try:
        from src.copybot.crypto_arb import read_snapshot_from_file
        snap = read_snapshot_from_file()
        if snap:
            ca["meta"] = {
                "cycles": snap.get("cycles"),
                "opens_yes": snap.get("opens", {}).get("yes"),
                "opens_no": snap.get("opens", {}).get("no"),
            }
    except Exception:
        pass

    # 3. market_maker — mm_orders
    mm = _safe_query(
        """
        SELECT
          SUM(CASE WHEN status IN ('open','filled') THEN 1 ELSE 0 END) AS open_n,
          COALESCE(SUM(CASE WHEN status='closed' AND filled_at >= ?
                              THEN pnl_usdc ELSE 0 END), 0) AS pnl_24h,
          COALESCE(SUM(CASE WHEN status='closed' THEN pnl_usdc ELSE 0 END), 0) AS pnl_total,
          MAX(COALESCE(filled_at, created_at)) AS last_at
        FROM mm_orders
        """,
        (today_ts,),
    )
    mm["enabled"] = os.getenv("MARKET_MAKER_ENABLED", "false").lower() == "true"

    # 4. spike_arb — spike_arb_trades
    sa = _safe_query(
        """
        SELECT
          SUM(CASE WHEN status IN ('open','posted','filled') THEN 1 ELSE 0 END) AS open_n,
          COALESCE(SUM(CASE WHEN status LIKE 'closed%' AND closed_at >= ?
                              THEN pnl_usdc ELSE 0 END), 0) AS pnl_24h,
          COALESCE(SUM(CASE WHEN status LIKE 'closed%' THEN pnl_usdc ELSE 0 END), 0) AS pnl_total,
          MAX(COALESCE(closed_at, filled_at, signal_at)) AS last_at
        FROM spike_arb_trades
        """,
        (today_ts,),
    )
    sa["enabled"] = os.getenv("SPIKE_ARB_ENABLED", "false").lower() == "true"

    # 5. adversarial_asks — adversarial_orders
    ad = _safe_query(
        """
        SELECT
          SUM(CASE WHEN status IN ('detected','posted','filled') THEN 1 ELSE 0 END) AS open_n,
          COALESCE(SUM(CASE WHEN status LIKE 'closed%' AND filled_at >= ?
                              THEN pnl_usdc ELSE 0 END), 0) AS pnl_24h,
          COALESCE(SUM(CASE WHEN status LIKE 'closed%' THEN pnl_usdc ELSE 0 END), 0) AS pnl_total,
          MAX(COALESCE(filled_at, signal_at)) AS last_at
        FROM adversarial_orders
        """,
        (today_ts,),
    )
    ad["enabled"] = os.getenv("ADVERSARIAL_ASKS_ENABLED", "false").lower() == "true"

    # 6. long_horizon — long_horizon_trades
    lh = _safe_query(
        """
        SELECT
          SUM(CASE WHEN status IN ('open','posted','filled') THEN 1 ELSE 0 END) AS open_n,
          COALESCE(SUM(CASE WHEN status LIKE 'closed%' AND closed_at >= ?
                              THEN pnl_usdc ELSE 0 END), 0) AS pnl_24h,
          COALESCE(SUM(CASE WHEN status LIKE 'closed%' THEN pnl_usdc ELSE 0 END), 0) AS pnl_total,
          MAX(COALESCE(closed_at, opened_at)) AS last_at
        FROM long_horizon_trades
        """,
        (today_ts,),
    )
    lh["enabled"] = os.getenv("LONG_HORIZON_ENABLED", "false").lower() == "true"

    # 7. hedge — hedge_trades (pnl_total_usdc)
    hd = _safe_query(
        """
        SELECT
          SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_n,
          COALESCE(SUM(CASE WHEN status='closed' AND closed_at >= ?
                              THEN pnl_total_usdc ELSE 0 END), 0) AS pnl_24h,
          COALESCE(SUM(CASE WHEN status='closed' THEN pnl_total_usdc ELSE 0 END), 0) AS pnl_total,
          MAX(COALESCE(closed_at, opened_at)) AS last_at
        FROM hedge_trades
        """,
        (today_ts,),
    )
    hd["enabled"] = os.getenv("CRYPTO_ARB_HEDGE_ENABLED", "false").lower() == "true"

    return {
        "_ts": int(_t.time()),
        "n1_copybot": n1,
        "crypto_arb": ca,
        "market_maker": mm,
        "spike_arb": sa,
        "adversarial": ad,
        "long_horizon": lh,
        "hedge": hd,
    }


@app.get("/api/ws-status")
def api_ws_status() -> dict:
    """Snapshot del WS bridge.

    El WS bridge corre en el contenedor *runner* (otro proceso). Las
    métricas viven in-memory ahí, así que no podemos leerlas directamente
    desde el server. El runner las persiste cada 5s a
    ``data/ws_metrics.json`` (volume compartido) y acá las leemos.

    Si el archivo no existe → el WS no arrancó nunca. ``enabled=false``
    (env var) lo refleja explícitamente. Si existe pero ``_persisted_at``
    es viejo, ``stale_s`` lo señala.
    """
    import os
    import time
    from src.copybot.ws_metrics import read_snapshot_from_file

    enabled = os.getenv("WEBSOCKET_TRADES_ENABLED", "false").lower() == "true"
    snap = read_snapshot_from_file()
    if snap is None:
        return {
            "enabled": enabled,
            "snapshot_present": False,
            "msg": "snapshot file not found — runner WS bridge not started yet",
        }
    persisted_at = snap.pop("_persisted_at", None)
    if persisted_at:
        snap["stale_s"] = round(time.time() - float(persisted_at), 1)
    snap["enabled"] = enabled
    snap["snapshot_present"] = True
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
