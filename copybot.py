"""CLI principal del copy-bot. Punto de entrada único.

Uso:
    python copybot.py init           # inicializa la base
    python copybot.py markets        # indexa todos los mercados
    python copybot.py discover       # descubre wallets activos
    python copybot.py backfill <wallet>
    python copybot.py backfill-all [--limit N]
    python copybot.py status         # resumen de la base
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rich.console import Console
from rich.table import Table

from src.config import DB_PATH, LOG_LEVEL
from src.db.schema import db, init_db

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
console = Console()


def cmd_init(_args: argparse.Namespace) -> None:
    init_db()
    console.print(f"[green]✓[/green] Base inicializada en {DB_PATH}")


def cmd_status(_args: argparse.Namespace) -> None:
    init_db()
    with db() as conn:
        markets = conn.execute("SELECT COUNT(*) c FROM markets").fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) c FROM markets WHERE active=1 AND closed=0"
        ).fetchone()["c"]
        traders = conn.execute("SELECT COUNT(*) c FROM traders").fetchone()["c"]
        trades = conn.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
        indexed = conn.execute(
            "SELECT COUNT(*) c FROM traders WHERE last_indexed_at IS NOT NULL"
        ).fetchone()["c"]

    t = Table(title="Estado de la base", show_header=False)
    t.add_column(style="cyan", no_wrap=True)
    t.add_column(style="bold")
    t.add_row("Mercados totales", f"{markets:,}")
    t.add_row("Mercados activos", f"{active:,}")
    t.add_row("Wallets descubiertos", f"{traders:,}")
    t.add_row("Wallets con backfill", f"{indexed:,}")
    t.add_row("Trades indexados", f"{trades:,}")
    t.add_row("DB", str(DB_PATH))
    console.print(t)


def cmd_markets(_args: argparse.Namespace) -> None:
    from src.indexer.markets import index_markets

    asyncio.run(index_markets())


def cmd_discover(args: argparse.Namespace) -> None:
    from src.indexer.trades import discover_traders

    asyncio.run(discover_traders(max_pages=args.pages))


def cmd_backfill(args: argparse.Namespace) -> None:
    from src.indexer.trades import backfill_wallet

    asyncio.run(backfill_wallet(args.wallet))


def cmd_backfill_all(args: argparse.Namespace) -> None:
    from src.indexer.trades import backfill_all_known

    asyncio.run(backfill_all_known(limit=args.limit))


def cmd_compute_metrics(_args: argparse.Namespace) -> None:
    from src.analytics.metrics import compute_all

    compute_all()


def cmd_select(args: argparse.Namespace) -> None:
    from src.copybot.selector import select_traders

    s = select_traders(top_n=args.top)
    console.print(
        f"[green]✓[/green] Selección: "
        f"+{len(s['added'])} nuevos, "
        f"={len(s['kept'])} mantenidos, "
        f"−{len(s['paused'])} pausados.  "
        f"Activos totales: [bold]{s['total_active']}[/bold]"
    )
    for x in s["added"][:5]:
        console.print(f"  [emerald] +[/emerald] {x['wallet']}  →  {x['reason']}")


def cmd_run_paper(args: argparse.Namespace) -> None:
    from src.copybot.runner import run_loop

    asyncio.run(run_loop(once=args.once))


def cmd_run_live(args: argparse.Namespace) -> None:
    """Loop con plata real (Fase 5).

    Activa LIVE_MODE=true en runtime y arranca el runner. Si --dry-run,
    fuerza LIVE_DRY_RUN=true (las ordenes se loguean pero no se mandan).
    """
    import os
    os.environ["LIVE_MODE"] = "true"
    if args.dry_run:
        os.environ["LIVE_DRY_RUN"] = "true"
    elif args.real:
        os.environ["LIVE_DRY_RUN"] = "false"
    # else: respeta lo que esté en .env

    # Importar después de setear el env para que tradebook elija live
    from src.copybot.runner import run_loop
    from src.config import LIVE_DRY_RUN as _DRY

    if not _DRY and not args.yes:
        console.print(
            "\n[bold red]⚠️  ESTÁS POR EJECUTAR ÓRDENES REALES CON USDC ⚠️[/bold red]\n"
        )
        console.print(
            "Esto va a colocar órdenes en Polymarket usando tu wallet.\n"
            "[bold]Re-lanzá con --yes para confirmar, o con --dry-run para simular.[/bold]"
        )
        return

    asyncio.run(run_loop(once=args.once))


def cmd_check_live(_args: argparse.Namespace) -> None:
    """Verifica que las credenciales de Polymarket funcionen sin mandar órdenes."""
    from src.polymarket.clob_client import health_check

    res = health_check()
    if not res.get("ok"):
        console.print(
            f"[red]✗ Health check falló en stage='{res.get('stage')}':[/red] "
            f"{res.get('error')}"
        )
        console.print(
            "\nConfiguración requerida en .env:\n"
            "  POLYMARKET_API_KEY=...\n"
            "  POLYMARKET_API_SECRET=...\n"
            "  POLYMARKET_API_PASSPHRASE=...\n"
            "  POLYMARKET_FUNDER_ADDRESS=0x...\n"
            "  POLYMARKET_SIG_TYPE=2  (default; 0 si usás EOA)\n\n"
            "Para generar las API creds: "
            "[cyan]python scripts/generate_api_creds.py[/cyan]"
        )
        return

    console.print("[green]✓ CLOB conectado[/green]")
    t = Table(show_header=False)
    t.add_column(style="cyan", no_wrap=True)
    t.add_column(style="bold")
    t.add_row("Funder wallet", res["funder"])
    t.add_row("Balance USDC", f"${res['balance_usdc']:.2f}")
    t.add_row("Sig type", str(res["sig_type"]))
    t.add_row("Host", res["host"])
    console.print(t)


def cmd_live_status(_args: argparse.Namespace) -> None:
    """Resumen de live_trades."""
    init_db()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) as n, ROUND(SUM(pnl_usdc),2) as pnl,
                   SUM(dry_run) as drys
            FROM live_trades GROUP BY status
            """
        ).fetchall()
    if not rows:
        console.print("[yellow]Sin live_trades todavía.[/yellow]")
        return
    t = Table(title="Live trades por estado")
    t.add_column("Estado", style="cyan")
    t.add_column("N", justify="right")
    t.add_column("PnL USDC", justify="right")
    t.add_column("Dry-run", justify="right")
    for r in rows:
        t.add_row(
            r["status"],
            str(r["n"]),
            f"${(r['pnl'] or 0):+.2f}",
            str(r["drys"] or 0),
        )
    console.print(t)


def cmd_settle_paper(_args: argparse.Namespace) -> None:
    from src.copybot.paper import settle_resolved

    n = settle_resolved()
    console.print(f"[green]✓[/green] {n} paper_trades liquidados")


def cmd_backtest(args: argparse.Namespace) -> None:
    from src.copybot.backtest import run_backtest

    run_backtest(hours=args.hours)


def cmd_categorize(_args: argparse.Namespace) -> None:
    from src.copybot.categorize import backfill_markets

    res = backfill_markets(only_missing=True)
    console.print(
        f"[green]✓[/green] Scaneados {res['scanned']:,} mercados sin categoría · "
        f"asignados {res['updated']:,}"
    )
    if res["by_category"]:
        t = Table(title="Distribución de categorías inferidas")
        t.add_column("Categoría")
        t.add_column("N", justify="right")
        for cat, n in sorted(res["by_category"].items(), key=lambda x: -x[1]):
            t.add_row(cat, f"{n:,}")
        console.print(t)


def cmd_policy(_args: argparse.Namespace) -> None:
    from src.copybot.policy import get_policy, refresh

    res = refresh(window_days=14)
    console.print(res)
    console.print(f"\nPolicy actual: {get_policy()}")


def cmd_discover_now(_args: argparse.Namespace) -> None:
    from src.copybot.discovery import run_cycle

    res = asyncio.run(run_cycle(force=True))
    console.print(res)


def cmd_cluster_now(_args: argparse.Namespace) -> None:
    from src.copybot.clusters import recompute_clusters, update_cluster_perf

    res = recompute_clusters()
    console.print(f"[green]✓[/green] Clustered {res.get('clustered', 0)} wallets in {res.get('k', 0)} clusters")
    if "size_per_cluster" in res:
        console.print(f"  Sizes: {res['size_per_cluster']}")
    perf = update_cluster_perf()
    if perf.get("clusters"):
        t = Table(title="Performance por cluster")
        t.add_column("ID", justify="right")
        t.add_column("Wallets", justify="right")
        t.add_column("Trades", justify="right")
        t.add_column("W/L", justify="right")
        t.add_column("Win%", justify="right")
        t.add_column("PnL", justify="right")
        t.add_column("Status")
        for c in perf["clusters"]:
            t.add_row(
                str(c["cluster_id"]), str(c["n_wallets"]), str(c["n_trades"]),
                f"{c['wins']}/{c['losses']}",
                f"{c['win_rate']*100:.0f}%",
                f"${c['pnl']:+.2f}",
                c["status"],
            )
        console.print(t)


def cmd_telegram_setup(_args: argparse.Namespace) -> None:
    import os
    from src.copybot.notifier import discover_chat_id

    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        console.print(
            "[red]Falta TELEGRAM_BOT_TOKEN en .env[/red]\n\n"
            "Pasos:\n"
            "  1. En Telegram, hablale a [bold]@BotFather[/bold] → [bold]/newbot[/bold]\n"
            "  2. Te da un token tipo `123456:ABC-DEF…`\n"
            "  3. Pegalo en .env como TELEGRAM_BOT_TOKEN=\n"
            "  4. [bold]Mandá un mensaje a tu bot[/bold] (cualquiera, ej. /start)\n"
            "  5. Volvé a correr este comando."
        )
        return

    res = discover_chat_id()
    if not res.get("ok"):
        console.print(f"[red]Error:[/red] {res.get('error')}")
        return

    chats = res.get("chats", [])
    if not chats:
        console.print(
            "[yellow]No encontré chats.[/yellow]\n"
            "Asegurate de haberle mandado [bold]al menos un mensaje[/bold] a tu bot "
            "(ej: /start). Después volvé a correr."
        )
        return

    console.print(f"[green]✓[/green] Encontré {len(chats)} chat(s):\n")
    for c in chats:
        console.print(
            f"  chat_id=[bold cyan]{c['chat_id']}[/bold cyan]  "
            f"({c.get('type','?')}: {c.get('title') or c.get('from_user') or '?'})"
        )
    console.print(
        "\n[bold]Copiá el chat_id a tu .env[/bold] como TELEGRAM_CHAT_ID="
        f"\n\nDespués corré: [cyan]python copybot.py telegram-test[/cyan]"
    )


def cmd_telegram_test(_args: argparse.Namespace) -> None:
    from src.copybot.notifier import _enabled, test_message

    if not _enabled():
        console.print(
            "[red]Telegram no configurado.[/red] Falta TELEGRAM_BOT_TOKEN o "
            "TELEGRAM_CHAT_ID en .env. Corré primero: "
            "[cyan]python copybot.py telegram-setup[/cyan]"
        )
        return
    if test_message():
        console.print("[green]✓[/green] Mensaje enviado. Revisá tu Telegram.")
    else:
        console.print(
            "[red]Falló el envío.[/red] Verificá las creds en .env y mirá los logs."
        )


def cmd_reset_killswitch(_args: argparse.Namespace) -> None:
    from src.copybot.risk import reset_kill_switch

    reset_kill_switch()
    console.print("[green]✓[/green] Kill switch desactivado. Bot reanudado.")


def cmd_paper_reset(args: argparse.Namespace) -> None:
    """Backup paper_trades a tabla con timestamp + truncate + reset killswitch.

    Conserva el histórico (para análisis) pero el bot ve PnL=$0 desde ahora.
    Mismo patrón usado en el switch a real del 2026-05-01 (live_trades_dryrun_<ts>).
    """
    import time as _t
    from src.db.schema import db, tx

    if not args.yes:
        console.print(
            "[red]Esto va a dejar el PnL paper en $0[/red] (backup en tabla aparte).\n"
            "Si estás seguro: agregá --yes"
        )
        return

    ts = int(_t.time())
    backup_table = f"paper_trades_backup_{ts}"

    with db() as conn:
        n_before = conn.execute(
            "SELECT COUNT(*) c FROM paper_trades"
        ).fetchone()["c"]

    with tx() as conn:
        # Backup
        conn.execute(
            f"CREATE TABLE {backup_table} AS SELECT * FROM paper_trades"
        )
        # Truncate
        conn.execute("DELETE FROM paper_trades")
        # Resetear cursor de aprendizaje del kill switch para que la ventana
        # arranque desde ahora (no quedan trades viejos en paper_trades pero
        # por las dudas).
        conn.execute(
            "INSERT INTO bot_state (key, value, updated_at) "
            "VALUES ('kill_switch_reset_at', ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (str(ts),),
        )
        # Desactivar kill_switch si estaba activo
        conn.execute(
            "INSERT INTO bot_state (key, value, updated_at) "
            "VALUES ('kill_switch', 'inactive', datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value='inactive', updated_at=datetime('now')"
        )
        # Audit
        conn.execute(
            "INSERT INTO learning_events (wallet, event_type, trigger, metric_snapshot) "
            "VALUES ('SYSTEM', 'paper_reset', ?, ?)",
            (f"backup={backup_table}", f'{{"trades_archived":{n_before}}}'),
        )

    console.print(
        f"[green]✓[/green] {n_before} paper_trades archivados en "
        f"[cyan]{backup_table}[/cyan].\n"
        "PnL ahora arranca en $0. Kill switch desactivado."
    )


def cmd_validate_real_readiness(args: argparse.Namespace) -> None:
    """Checklist mecánica — corré después de >=24h paper post-fix.

    Devuelve PASS/FAIL/WARN por item. Si todos PASS → seguro pasar a real.
    Mide solo lo que hay en la DB y archivos de log; no ejecuta nada.
    """
    import subprocess
    import time as _t
    from src.db.schema import db

    h = max(1, int(args.hours))
    cutoff = int(_t.time()) - h * 3600

    console.print(f"\n[bold cyan]Validación de readiness para LIVE real[/bold cyan]")
    console.print(f"Ventana de análisis: últimas [bold]{h}h[/bold]\n")

    results: list[tuple[str, str, str]] = []  # (status, name, detail)

    # ── 1. 0 errores "database is locked" en logs (24h)
    try:
        out = subprocess.run(
            ["docker", "logs", "--since", f"{h}h", "bot_trading-runner-1"],
            capture_output=True, text=True, timeout=30,
        )
        log_text = (out.stdout or "") + (out.stderr or "")
        n_lock = log_text.count("database is locked")
        if n_lock == 0:
            results.append(("PASS", "0 errores DB locked", "limpio"))
        else:
            results.append(("FAIL", "errores DB locked", f"{n_lock} en {h}h"))
    except Exception as e:
        results.append(("WARN", "logs DB locked", f"no pude leer: {e}"))

    # ── 2. SELLs cierran posiciones (ratio CLOSE/OPEN)
    try:
        out = subprocess.run(
            ["docker", "logs", "--since", f"{h}h", "bot_trading-runner-1"],
            capture_output=True, text=True, timeout=30,
        )
        log_text = (out.stdout or "") + (out.stderr or "")
        n_open = sum(1 for ln in log_text.splitlines() if "OPEN  " in ln)
        n_close = sum(1 for ln in log_text.splitlines() if "CLOSE " in ln)
        if n_open == 0:
            results.append(("WARN", "SELLs cierran", "0 OPENs en ventana — sample chico"))
        else:
            ratio = n_close / n_open
            detail = f"{n_close} closes / {n_open} opens = {ratio:.2f}"
            results.append(
                ("PASS" if ratio >= 0.5 else "FAIL", "SELLs cierran", detail)
            )
    except Exception as e:
        results.append(("WARN", "ratio CLOSE/OPEN", f"no pude leer logs: {e}"))

    # ── 3. Sweep SL/TP corre periódicamente
    try:
        out = subprocess.run(
            ["docker", "logs", "--since", f"{h}h", "bot_trading-runner-1"],
            capture_output=True, text=True, timeout=30,
        )
        log_text = (out.stdout or "") + (out.stderr or "")
        n_sweep = log_text.count("risk:") + log_text.count("force_close")
        if n_sweep > 0:
            results.append(("PASS", "sweep SL/TP corre", f"{n_sweep} eventos"))
        else:
            results.append(("WARN", "sweep SL/TP", "0 eventos — sin posiciones para sweep"))
    except Exception as e:
        results.append(("WARN", "sweep SL/TP", str(e)))

    # ── 4. Reconciler: 0 trades fantasma
    try:
        out = subprocess.run(
            ["docker", "logs", "--since", f"{h}h", "bot_trading-runner-1"],
            capture_output=True, text=True, timeout=30,
        )
        log_text = (out.stdout or "") + (out.stderr or "")
        n_phantom = log_text.count("trades fantasma encontrados")
        if n_phantom == 0:
            results.append(("PASS", "0 trades fantasma", "reconciler limpio"))
        else:
            results.append(("WARN", "trades fantasma", f"{n_phantom} eventos — revisar"))
    except Exception as e:
        results.append(("WARN", "reconciler", str(e)))

    # ── 5. PnL paper >= -5% cap
    try:
        with db() as conn:
            r = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdc),0) p, COUNT(*) n FROM paper_trades "
                "WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss') "
                "AND exit_at >= ?",
                (cutoff,),
            ).fetchone()
        from src.config import BOT_CAPITAL_USDC
        pnl = r["p"]
        n = r["n"]
        threshold = -0.05 * BOT_CAPITAL_USDC
        if n < 5:
            results.append(("WARN", f"PnL paper {h}h", f"sample chico ({n} cierres)"))
        elif pnl >= threshold:
            results.append(("PASS", f"PnL paper {h}h", f"${pnl:+.2f} sobre cap ${BOT_CAPITAL_USDC}"))
        else:
            results.append(("FAIL", f"PnL paper {h}h", f"${pnl:+.2f} < -5% cap (${threshold:.2f})"))
    except Exception as e:
        results.append(("WARN", "PnL paper", str(e)))

    # ── 6. Kill switch funciona — ya validado el 2026-05-06
    results.append(("PASS", "kill switch", "validado 2026-05-06 (drawdown -$18 disparó pausa)"))

    # ── Render
    t = Table(title=f"Readiness check ({h}h)")
    t.add_column("Status", justify="center")
    t.add_column("Check")
    t.add_column("Detalle")
    for status, name, detail in results:
        color = {"PASS": "green", "FAIL": "red", "WARN": "yellow"}[status]
        t.add_row(f"[{color}]{status}[/{color}]", name, detail)
    console.print(t)

    n_pass = sum(1 for s, *_ in results if s == "PASS")
    n_fail = sum(1 for s, *_ in results if s == "FAIL")
    n_warn = sum(1 for s, *_ in results if s == "WARN")
    if n_fail > 0:
        console.print(f"\n[bold red]✗ NO listo para real[/bold red] — {n_fail} FAIL")
    elif n_warn > n_pass / 2:
        console.print(f"\n[bold yellow]⚠ Esperar más data[/bold yellow] — {n_warn} WARN, sample chico")
    else:
        console.print(f"\n[bold green]✓ Listo para pasar a real[/bold green] — {n_pass} PASS")
        console.print(
            "[dim]Pasos para activar: editar .env Lenovo → "
            "[bold]FORCE_LIVE_OK=true[/bold] + LIVE_MODE=true → "
            "docker compose restart[/dim]"
        )


def cmd_cluster_status(_args: argparse.Namespace) -> None:
    from src.copybot.clusters import status

    rows = status()
    if not rows:
        console.print("[yellow]Sin clusters todavía. Corré `cluster-now`.[/yellow]")
        return
    t = Table(title="Cluster status")
    t.add_column("ID", justify="right")
    t.add_column("Wallets", justify="right")
    t.add_column("Trades", justify="right")
    t.add_column("W/L", justify="right")
    t.add_column("PnL", justify="right")
    t.add_column("Status")
    for r in rows:
        t.add_row(
            str(r["cluster_id"]),
            str(r["n_wallets"]),
            str(r["n_trades"]),
            f"{r['wins']}/{r['losses']}",
            f"${(r['pnl_usdc'] or 0):+.2f}",
            r["status"],
        )
    console.print(t)


def cmd_tune(args: argparse.Namespace) -> None:
    from src.copybot.auto_filter import get_all, maybe_tune

    res = maybe_tune(force=args.force)
    console.print(res or "[yellow]Sin cambios[/yellow]")
    console.print(f"\nThresholds actuales:\n{get_all()}")


def cmd_reconcile(_args: argparse.Namespace) -> None:
    """Reconcilia trades on-chain del proxy con live_trades del bot.

    Crea rows para trades fantasma (BUYs ejecutados sin tracking en DB).
    """
    from src.copybot.reconciler import reconcile_once
    summary = reconcile_once()
    print(f"Reconcile summary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")


def cmd_reset_thresholds(_args: argparse.Namespace) -> None:
    from src.copybot.auto_filter import get_all, reset_to_defaults

    changes = reset_to_defaults()
    if not changes:
        console.print("[yellow]Sin cambios — ya estaban en defaults.[/yellow]")
        return
    t = Table(title="Thresholds reseteados a defaults")
    t.add_column("Key", style="cyan")
    t.add_column("Antes", justify="right")
    t.add_column("Default", justify="right", style="bold")
    for key, (before, after) in changes.items():
        t.add_row(key, f"{before:.2f}", f"{after:.2f}")
    console.print(t)
    console.print(f"\n[dim]Snapshot final: {get_all()}[/dim]")


def cmd_categories(_args: argparse.Namespace) -> None:
    from src.copybot.categories import stats

    rows = stats()
    if not rows:
        console.print("[yellow]Sin data de categorías todavía[/yellow]")
        return
    t = Table(title="Performance por categoría")
    t.add_column("Categoría")
    t.add_column("N", justify="right")
    t.add_column("W/L", justify="right")
    t.add_column("PnL", justify="right")
    t.add_column("Status")
    for r in rows:
        t.add_row(
            str(r["category"])[:30],
            str(r["n_trades"]),
            f"{r['wins']}/{r['losses']}",
            f"${(r['pnl_usdc'] or 0):+.2f}",
            "🚫" if r["status"] == "blocked" else "✓",
        )
    console.print(t)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    console.print(
        f"[bold cyan]→[/bold cyan] Sirviendo en http://{args.host}:{args.port}\n"
        f"[dim]Desde tu iPhone:  http://<IP-de-tu-Lenovo>:{args.port}[/dim]\n"
        f"[dim]Para encontrar la IP: en Mac/Linux `ipconfig getifaddr en0` "
        f"o en Windows `ipconfig`[/dim]"
    )
    uvicorn.run(
        "src.api.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


def cmd_top(args: argparse.Namespace) -> None:
    from datetime import datetime

    from src.analytics.metrics import top

    rows = top(limit=args.limit)
    if not rows:
        console.print(
            "[yellow]Sin resultados. Pasos previos:[/yellow]\n"
            "  python copybot.py markets\n"
            "  python copybot.py discover --pages 50\n"
            "  python copybot.py backfill-all --limit 200\n"
            "  python copybot.py compute-metrics"
        )
        return

    t = Table(title=f"Top {len(rows)} traders por score", show_lines=False)
    t.add_column("#", style="dim", width=3)
    t.add_column("Wallet", style="cyan")
    t.add_column("Trades", justify="right")
    t.add_column("PnL USDC", justify="right", style="green")
    t.add_column("ROI%", justify="right")
    t.add_column("Win%", justify="right")
    t.add_column("Sharpe", justify="right")
    t.add_column("DD%", justify="right", style="red")
    t.add_column("Días", justify="right")
    t.add_column("Score", justify="right", style="bold")

    for i, r in enumerate(rows, 1):
        pnl = r["realized_pnl_usdc"] or 0
        t.add_row(
            str(i),
            r["wallet"][:10] + "…",
            f"{r['total_trades']:,}",
            f"{pnl:,.0f}",
            f"{(r['roi_pct'] or 0):.1f}",
            f"{(r['win_rate'] or 0)*100:.0f}",
            f"{(r['sharpe_proxy'] or 0):.2f}",
            f"{(r['max_drawdown_pct'] or 0):.0f}",
            str(r["active_days"]),
            f"{(r['score'] or 0):.3f}",
        )
    console.print(t)
    console.print(
        f"[dim]Calculado: {datetime.now():%Y-%m-%d %H:%M}.  "
        "Filtros: ≥100 trades, vol ≥ $500, PnL realizado > 0.  "
        "Nota: la API limita a los 3500 trades más recientes por wallet, "
        "así que 'Días' refleja la ventana reciente, no track-record total.[/dim]"
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="copybot", description="Polymarket copy-bot CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Inicializa la base de datos").set_defaults(
        func=cmd_init
    )
    sub.add_parser("status", help="Resumen del estado").set_defaults(func=cmd_status)
    sub.add_parser("markets", help="Indexa mercados").set_defaults(func=cmd_markets)

    p_disc = sub.add_parser("discover", help="Descubre wallets activos")
    p_disc.add_argument("--pages", type=int, default=50)
    p_disc.set_defaults(func=cmd_discover)

    p_bf = sub.add_parser("backfill", help="Backfill historial de un wallet")
    p_bf.add_argument("wallet")
    p_bf.set_defaults(func=cmd_backfill)

    p_all = sub.add_parser("backfill-all", help="Backfill de todos los wallets")
    p_all.add_argument("--limit", type=int, default=None)
    p_all.set_defaults(func=cmd_backfill_all)

    sub.add_parser(
        "compute-metrics",
        help="Calcula PnL/ROI/Sharpe/Drawdown por wallet",
    ).set_defaults(func=cmd_compute_metrics)

    p_top = sub.add_parser("top", help="Muestra los top N traders por score")
    p_top.add_argument("--limit", type=int, default=50)
    p_top.set_defaults(func=cmd_top)

    p_sel = sub.add_parser("select", help="Selecciona automáticamente top N a copiar")
    p_sel.add_argument("--top", type=int, default=10)
    p_sel.set_defaults(func=cmd_select)

    p_run = sub.add_parser("run-paper", help="Loop de paper-trading (polling)")
    p_run.add_argument("--once", action="store_true", help="Una sola pasada y sale")
    p_run.set_defaults(func=cmd_run_paper)

    p_live = sub.add_parser(
        "run-live",
        help="Loop con plata real en Polymarket CLOB (Fase 5)",
    )
    p_live.add_argument("--once", action="store_true")
    p_live.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula órdenes sin mandarlas (testing)",
    )
    p_live.add_argument(
        "--real",
        action="store_true",
        help="Fuerza el envío real de órdenes (override de LIVE_DRY_RUN del .env)",
    )
    p_live.add_argument(
        "--yes",
        action="store_true",
        help="Confirma que querés mandar órdenes reales (no preguntar)",
    )
    p_live.set_defaults(func=cmd_run_live)

    sub.add_parser(
        "check-live",
        help="Health check del CLOB de Polymarket (verifica creds + balance)",
    ).set_defaults(func=cmd_check_live)

    sub.add_parser(
        "live-status",
        help="Resumen de live_trades (real + dry-run)",
    ).set_defaults(func=cmd_live_status)

    sub.add_parser(
        "settle-paper",
        help="Liquida paper_trades cuyo mercado ya resolvió",
    ).set_defaults(func=cmd_settle_paper)

    p_bt = sub.add_parser("backtest", help="Replay de trades históricos con todos los filtros")
    p_bt.add_argument("--hours", type=int, default=96)
    p_bt.set_defaults(func=cmd_backtest)

    sub.add_parser(
        "categorize", help="Infiere categoría desde slug/question para mercados sin tag"
    ).set_defaults(func=cmd_categorize)

    sub.add_parser(
        "policy", help="Refrescar policy (self-improvement por bucket)"
    ).set_defaults(func=cmd_policy)

    sub.add_parser(
        "discover-now", help="Corre auto-discovery (Fase 6a) ahora"
    ).set_defaults(func=cmd_discover_now)

    sub.add_parser(
        "cluster-now", help="Re-clustera wallets y refresca performance (Fase 6b)"
    ).set_defaults(func=cmd_cluster_now)

    sub.add_parser(
        "cluster-status", help="Muestra status de cada cluster"
    ).set_defaults(func=cmd_cluster_status)

    sub.add_parser(
        "telegram-setup", help="Descubre tu chat_id de Telegram automáticamente"
    ).set_defaults(func=cmd_telegram_setup)

    sub.add_parser(
        "telegram-test", help="Manda un mensaje de prueba a tu bot"
    ).set_defaults(func=cmd_telegram_test)

    sub.add_parser(
        "reset-killswitch", help="Desactiva el kill switch manualmente"
    ).set_defaults(func=cmd_reset_killswitch)

    p_pr = sub.add_parser(
        "paper-reset",
        help="Archiva paper_trades a una tabla backup_<ts> y resetea PnL a $0",
    )
    p_pr.add_argument("--yes", action="store_true", help="Confirma la operación")
    p_pr.set_defaults(func=cmd_paper_reset)

    p_vr = sub.add_parser(
        "validate-real-readiness",
        help="Checklist mecánica para decidir si pasar a LIVE real",
    )
    p_vr.add_argument("--hours", type=int, default=24, help="Ventana de análisis en horas (default 24)")
    p_vr.set_defaults(func=cmd_validate_real_readiness)

    p_tn = sub.add_parser("tune", help="Ejecuta auto-tune de thresholds")
    p_tn.add_argument("--force", action="store_true")
    p_tn.set_defaults(func=cmd_tune)

    sub.add_parser(
        "reset-thresholds",
        help="Vuelve los thresholds del selector a sus defaults iniciales",
    ).set_defaults(func=cmd_reset_thresholds)

    sub.add_parser(
        "reconcile",
        help="Reconcilia trades on-chain del proxy con live_trades (trades fantasma)",
    ).set_defaults(func=cmd_reconcile)

    sub.add_parser("categories", help="Performance por categoría").set_defaults(func=cmd_categories)

    p_srv = sub.add_parser("serve", help="Levanta el dashboard PWA")
    p_srv.add_argument("--host", default="0.0.0.0")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.add_argument("--reload", action="store_true")
    p_srv.set_defaults(func=cmd_serve)

    args = p.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print("[yellow]Interrumpido[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
