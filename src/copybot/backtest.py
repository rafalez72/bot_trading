"""Backtest: replay de los últimos N días de trades reales aplicando
TODOS los filtros y módulos de aprendizaje.

Cómo funciona:
1. Reset de paper_trades / learning_events / sizing_mults / category_perf /
   bandit_state / filter_thresholds / kill_switch.
2. Determinar el corte temporal (now - hours).
3. Selección inicial de traders con los thresholds default.
4. Para cada trade en `trades` (timestamp >= corte) de wallets activos,
   en orden cronológico: open o close vía paper.py. Esto dispara
   el pipeline de aprendizaje real (categorías, bandit, kill switch).
5. settle_resolved() al final para liquidar mercados ya resueltos.
6. Un auto_filter.maybe_tune(force=True) intermedio cada ~6h simuladas.
7. Devolver reporte.

NOTA: el stop-loss en backtest es aproximado. Para hacerlo bien necesitamos
el price feed de cada `asset` en el tiempo. Acá lo simulamos vía:
  - Para cada open paper_trade, miramos si en `trades` hay alguna venta
    posterior del MISMO asset a un precio que dispare el stop-loss; si sí,
    lo cerramos a ese precio en ese timestamp.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Iterable

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from src.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT
from src.copybot.paper import close_position, force_close, open_position, settle_resolved
from src.copybot.selector import select_traders
from src.db.schema import db, init_db, tx
from src.indexer.trades import _trade_id

log = logging.getLogger(__name__)
console = Console()


def _reset_state() -> None:
    with tx() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM learning_events")
        conn.execute("DELETE FROM index_state WHERE key LIKE 'paper_cursor:%'")
        conn.execute("DELETE FROM bandit_state")
        conn.execute("DELETE FROM category_perf")
        conn.execute("DELETE FROM filter_thresholds")
        conn.execute("DELETE FROM bot_state")
        conn.execute(
            "UPDATE copy_subscriptions SET sizing_mult=1.0, status="
            "CASE WHEN status='dropped' THEN 'paused' ELSE status END"
        )


def _active_wallets() -> set[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT wallet FROM copy_subscriptions WHERE status='active'"
        ).fetchall()
    return {r["wallet"] for r in rows}


def _check_simulated_stops(
    trades_window: list[dict],
    sim_now: int,
) -> int:
    """Para cada open paper_trade, busca si algún trade del mismo asset hasta
    `sim_now` cruzó el umbral de SL/TP. Si sí, lo cierra al precio del cruce.
    """
    closed = 0
    with db() as conn:
        opens = conn.execute(
            """
            SELECT id, asset, entry_price, entry_at FROM paper_trades
            WHERE status='open' AND asset IS NOT NULL
            """,
        ).fetchall()

    if not opens:
        return 0

    # Index por asset → trades posteriores
    by_asset: dict[str, list[dict]] = defaultdict(list)
    for t in trades_window:
        a = t.get("asset") if isinstance(t, dict) else t["asset"]
        if a:
            by_asset[a].append(t)

    for o in opens:
        asset = o["asset"]
        entry = o["entry_price"] or 0
        entry_at = o["entry_at"] or 0
        if entry <= 0:
            continue
        sl_thresh = entry * (1 - STOP_LOSS_PCT)
        tp_thresh = entry * (1 + TAKE_PROFIT_PCT) if TAKE_PROFIT_PCT > 0 else 1e9

        # Buscar el primer trade después de entry_at que cruce
        for t in by_asset.get(asset, []):
            ts = int(t.get("timestamp") or 0)
            if ts <= entry_at or ts > sim_now:
                continue
            try:
                px = float(t.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if px <= sl_thresh:
                force_close(o["id"], px, reason=f"stop_loss_{int(STOP_LOSS_PCT*100)}pct")
                closed += 1
                break
            if px >= tp_thresh:
                force_close(o["id"], px, reason=f"take_profit_{int(TAKE_PROFIT_PCT*100)}pct")
                closed += 1
                break
    return closed


def run_backtest(*, hours: int = 96, sample_period_hours: int = 6) -> dict:
    """Ejecuta backtest de las últimas `hours` horas."""
    init_db()
    console.print(f"[bold cyan]→[/bold cyan] Backtest de {hours} horas")

    _reset_state()
    sel = select_traders(top_n=20)
    console.print(
        f"[green]✓[/green] Selección inicial: "
        f"{len(sel['added'])} nuevos · {len(sel['kept'])} mantenidos · "
        f"total activos {sel['total_active']}"
    )

    cutoff = int(time.time()) - hours * 3600
    wallets = _active_wallets()
    if not wallets:
        console.print(
            "[red]Sin wallets activos. Bajá los filtros o corré compute-metrics.[/red]"
        )
        return {"error": "no_active_wallets"}

    placeholders = ",".join("?" * len(wallets))
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT id, wallet as proxyWallet, condition_id as conditionId,
                   side, outcome, outcome_index as outcomeIndex,
                   price, size, timestamp, raw
            FROM trades
            WHERE wallet IN ({placeholders})
              AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (*wallets, cutoff),
        ).fetchall()

    console.print(f"  Procesando [bold]{len(rows):,}[/bold] trades del período…")
    # Para mercados negRisk que no estén en `markets`, paper.open_position
    # crea un stub usando slug/title del raw del trade. No hace falta fetch.

    # Índice de trades por asset para SL/TP simulado (incluye TODOS los trades
    # de los mercados involucrados, no sólo de los wallets copiados)
    cids_in_window = list({r["conditionId"] for r in rows})
    asset_trades: list[dict] = []
    if cids_in_window:
        cid_ph = ",".join("?" * len(cids_in_window))
        with db() as conn:
            ar = conn.execute(
                f"""
                SELECT raw, condition_id, price, timestamp
                FROM trades
                WHERE condition_id IN ({cid_ph})
                  AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (*cids_in_window, cutoff),
            ).fetchall()
        # Extraemos el `asset` desde `raw` JSON
        import json
        for r in ar:
            try:
                raw = json.loads(r["raw"]) if r["raw"] else {}
                asset = raw.get("asset")
                if asset:
                    asset_trades.append({
                        "asset": asset,
                        "price": r["price"],
                        "timestamp": r["timestamp"],
                    })
            except Exception:
                continue

    n_open = 0
    n_close_source = 0
    n_open_rejected = 0
    next_sweep = cutoff + sample_period_hours * 3600

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("Replay…", total=len(rows))
        for r in rows:
            import json as _j
            raw = {}
            if r["raw"]:
                try:
                    raw = _j.loads(r["raw"])
                except Exception:
                    pass

            ts = r["timestamp"]
            # Sweeps simulados cada `sample_period_hours`
            while ts >= next_sweep:
                _check_simulated_stops(asset_trades, next_sweep)
                next_sweep += sample_period_hours * 3600

            side = (r["side"] or "").upper()
            cid = r["conditionId"]
            oi = r["outcomeIndex"]
            try:
                price = float(r["price"] or 0)
            except (TypeError, ValueError):
                prog.advance(task)
                continue

            tid = _trade_id({
                "transactionHash": (raw.get("transactionHash") or r["id"]),
                "asset": raw.get("asset", ""),
                "side": side,
                "outcomeIndex": oi,
            })

            if side == "BUY":
                pid, reason = open_position(
                    source_wallet=r["proxyWallet"],
                    source_trade_id=tid,
                    condition_id=cid,
                    outcome=r["outcome"],
                    outcome_index=oi,
                    price=price,
                    timestamp=ts,
                    raw=raw,
                )
                if pid:
                    n_open += 1
                else:
                    n_open_rejected += 1
            elif side == "SELL":
                pid = close_position(
                    source_wallet=r["proxyWallet"],
                    condition_id=cid,
                    outcome_index=oi,
                    price=price,
                    timestamp=ts,
                )
                if pid:
                    n_close_source += 1

            prog.advance(task)

    # Sweep final + settle
    n_sl = _check_simulated_stops(asset_trades, int(time.time()))
    n_settled = settle_resolved()

    # Self-improvement: aprender de los closes y persistir reglas
    from src.copybot.policy import refresh as policy_refresh
    pol = policy_refresh(window_days=max(1, hours // 24))
    if pol and not pol.get("skipped"):
        ab = pol.get("added_blocks", {})
        nb = sum(len(v) for v in ab.values())
        rb = sum(len(v) for v in pol.get("removed_blocks", {}).values())
        console.print(
            f"[bold magenta]policy:[/bold magenta] {pol['n_analyzed']} trades analizados · "
            f"+{nb} bloqueos · -{rb} desbloqueos"
        )

    return _report(n_open, n_open_rejected, n_close_source, n_sl, n_settled, hours, policy=pol)


def _report(
    n_open: int, n_rejected: int, n_close_src: int, n_sl: int, n_settled: int,
    hours: int, policy: dict | None = None,
) -> dict:
    from src.copybot.bandit import status as bandit_status
    from src.copybot.categories import stats as category_stats
    from src.copybot.learning import summary

    s = summary()
    bandit = bandit_status()
    cats = category_stats()

    # Tabla resumen
    t = Table(title=f"Backtest {hours}h — Resultados", show_lines=False)
    t.add_column("Métrica", style="cyan")
    t.add_column("Valor", style="bold")
    t.add_row("Aperturas exitosas", f"{n_open:,}")
    t.add_row("Aperturas rechazadas (filtros)", f"{n_rejected:,}")
    t.add_row("Cierres por SELL del source", f"{n_close_src:,}")
    t.add_row("Cierres por stop-loss/TP simulado", f"{n_sl:,}")
    t.add_row("Cierres por resolución de mercado", f"{n_settled:,}")
    t.add_row("", "")
    t.add_row("Wins / Losses", f"{s['paper']['wins']} / {s['paper']['losses']}")
    t.add_row("Win rate", f"{s['paper']['win_rate']*100:.1f}%")
    t.add_row("PnL realizado", f"${s['paper']['realized_pnl_usdc']:+.2f}")
    t.add_row("ROI sobre invertido cerrado", f"{s['paper']['roi_pct']:+.1f}%")
    t.add_row("", "")
    t.add_row("Capital usado / total", f"${s['capital']['in_open_positions_usdc']:.2f} / ${s['capital']['total_usdc']:.0f}")
    t.add_row("Posiciones abiertas al final", f"{s['paper']['open']}")
    t.add_row("Kill switch", "ACTIVO" if s['kill_switch']['active'] else "OK")
    t.add_row("Categorías bloqueadas", str(sum(1 for c in cats if c["status"] == "blocked")))
    console.print(t)

    if cats:
        ct = Table(title="Performance por categoría", show_lines=False)
        ct.add_column("Categoría")
        ct.add_column("N", justify="right")
        ct.add_column("W/L", justify="right")
        ct.add_column("Win%", justify="right")
        ct.add_column("PnL", justify="right")
        ct.add_column("Status")
        for c in cats[:15]:
            wr = (c["wins"] / c["n_trades"] * 100) if c["n_trades"] else 0
            ct.add_row(
                str(c["category"])[:25],
                str(c["n_trades"]),
                f"{c['wins']}/{c['losses']}",
                f"{wr:.0f}%",
                f"${c['pnl_usdc']:+.2f}",
                "🚫 blocked" if c["status"] == "blocked" else "✓",
            )
        console.print(ct)

    if bandit:
        bt = Table(title="Bandit UCB1 — sizing por trader", show_lines=False)
        bt.add_column("Wallet")
        bt.add_column("size×", justify="right")
        bt.add_column("trades", justify="right")
        bt.add_column("UCB", justify="right")
        for b in bandit:
            bt.add_row(
                b["wallet"][:14] + "…",
                f"{b['sizing_mult']:.2f}",
                str(b["n_pulls"]),
                f"{b['ucb_score']:.2f}",
            )
        console.print(bt)

    # Tabla de policy si hubo cambios
    if policy and not policy.get("skipped"):
        cur_pol = policy.get("current_policy", {})
        if any(cur_pol.values()):
            pt = Table(title="Reglas aprendidas (auto-policy)")
            pt.add_column("Tipo")
            pt.add_column("Bloqueados")
            pt.add_row("Categorías", ", ".join(cur_pol.get("category_block", []) or ["—"]))
            pt.add_row("Horas (UTC)", ", ".join(cur_pol.get("hour_block", []) or ["—"]))
            pt.add_row("Rangos de precio", ", ".join(cur_pol.get("price_block", []) or ["—"]))
            console.print(pt)

    return {
        "open": n_open,
        "rejected": n_rejected,
        "close_source": n_close_src,
        "stop_loss": n_sl,
        "settled": n_settled,
        "summary": s,
        "categories": cats,
        "bandit": bandit,
        "policy": policy,
    }
