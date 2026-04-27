"""Cálculo de métricas por wallet a partir de los trades indexados.

Modelo de PnL (pragmático, no contable-perfecto):

  Una "posición" = (wallet, condition_id, outcome_index).

  Para cada posición se acumula:
    - bought_shares, bought_cost     (BUY)
    - sold_shares,   sold_proceeds   (SELL)
    - net_shares = bought_shares - sold_shares

  PnL realizado:
    - Si net_shares ≈ 0 → posición cerrada → pnl = sold_proceeds - bought_cost
    - Si net_shares > 0 y el mercado está RESUELTO (outcome_prices = [1,0] o [0,1]):
        valor_residual = net_shares * outcome_prices[outcome_index]
        cost_basis_restante = bought_cost - sold_proceeds
        pnl_resuelto = valor_residual - cost_basis_restante
    - Si net_shares < 0 (raro: short selling) → tratamos cost basis como 0
    - En cualquier otro caso → posición abierta, no contribuye a PnL realizado

  Métricas a nivel wallet:
    - total_volume_usdc        sum |price * size| de todos los trades
    - realized_pnl_usdc        sum de pnl de posiciones cerradas/resueltas
    - roi_pct                  realized_pnl / total_buy_cost
    - win_rate                 wins / closed_positions
    - avg_position_size        media de bought_cost por posición
    - max_drawdown_pct         peor caída desde un pico de cum_pnl
    - sharpe_proxy             mean(pnl) / stddev(pnl) de posiciones cerradas
    - active_days              días entre primer y último trade
    - first_trade_ts / last_trade_ts
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from src.db.schema import db, init_db, tx

log = logging.getLogger(__name__)
console = Console()

EPSILON = 1e-6


@dataclass
class WalletMetrics:
    wallet: str
    total_trades: int
    total_volume_usdc: float
    realized_pnl_usdc: float
    unrealized_pnl_usdc: float
    roi_pct: float
    win_rate: float
    avg_position_size: float
    max_drawdown_pct: float
    sharpe_proxy: float
    active_days: int
    first_trade_ts: int
    last_trade_ts: int

    def as_row(self, score: float) -> tuple:
        return (
            self.wallet,
            self.total_trades,
            self.total_volume_usdc,
            self.realized_pnl_usdc,
            self.unrealized_pnl_usdc,
            self.roi_pct,
            self.win_rate,
            self.avg_position_size,
            self.max_drawdown_pct,
            self.sharpe_proxy,
            self.active_days,
            self.first_trade_ts,
            self.last_trade_ts,
            score,
        )


# ---------------- núcleo de cálculo ----------------

def _resolved_payout(outcome_prices_json: str | None, outcome_index: int | None) -> float | None:
    """Devuelve el payout (0..1) si el mercado tiene outcome_prices y un índice válido."""
    if not outcome_prices_json or outcome_index is None:
        return None
    try:
        prices = json.loads(outcome_prices_json)
    except Exception:
        return None
    if not isinstance(prices, list) or outcome_index >= len(prices):
        return None
    try:
        v = float(prices[outcome_index])
    except (TypeError, ValueError):
        return None
    # Resuelto = los precios suman ~1 y son 0/1 (o muy cercanos)
    try:
        total = sum(float(p) for p in prices)
    except (TypeError, ValueError):
        return None
    if abs(total - 1.0) > 0.05:
        return None
    return v


def _drawdown_pct(cum_pnls: list[float]) -> float:
    """Max drawdown como % desde el pico. Devuelve un número >=0."""
    if not cum_pnls:
        return 0.0
    peak = cum_pnls[0]
    max_dd = 0.0
    for v in cum_pnls:
        peak = max(peak, v)
        if peak > 0:
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def compute_for_wallet(wallet: str) -> WalletMetrics | None:
    wallet = wallet.lower()
    with db() as conn:
        trades = conn.execute(
            """
            SELECT condition_id, side, outcome_index, price, size, usdc_value, timestamp
            FROM trades
            WHERE wallet = ?
            ORDER BY timestamp ASC
            """,
            (wallet,),
        ).fetchall()
        if not trades:
            return None

        # Mapa de mercados resueltos
        cids = {t["condition_id"] for t in trades}
        placeholders = ",".join("?" * len(cids))
        rows = conn.execute(
            f"SELECT condition_id, closed, outcome_prices FROM markets "
            f"WHERE condition_id IN ({placeholders})",
            tuple(cids),
        ).fetchall()
    market_meta = {
        r["condition_id"]: (bool(r["closed"]), r["outcome_prices"]) for r in rows
    }

    # Acumular por posición
    pos: dict[tuple[str, int | None], dict] = defaultdict(
        lambda: {"buy_sh": 0.0, "buy_cost": 0.0, "sell_sh": 0.0, "sell_proc": 0.0}
    )
    total_volume = 0.0
    first_ts = trades[0]["timestamp"]
    last_ts = trades[-1]["timestamp"]

    # Para cum-pnl temporal usamos un timeline aproximado: marcamos el PnL cuando
    # una posición se cierra (net=0 después del trade que la cierra)
    pnl_timeline: list[tuple[int, float]] = []  # (timestamp, realized_delta)

    # Estado por-posición para detectar cierres durante el recorrido
    state: dict[tuple[str, int | None], dict] = defaultdict(
        lambda: {"shares": 0.0, "cost_basis": 0.0}
    )

    for t in trades:
        cid = t["condition_id"]
        oi = t["outcome_index"]
        side = (t["side"] or "").upper()
        price = float(t["price"] or 0)
        size = float(t["size"] or 0)
        value = price * size
        total_volume += value

        key = (cid, oi)
        p = pos[key]
        s = state[key]

        if side == "BUY":
            p["buy_sh"] += size
            p["buy_cost"] += value
            s["shares"] += size
            s["cost_basis"] += value
        elif side == "SELL":
            p["sell_sh"] += size
            p["sell_proc"] += value
            # PnL marginal: vender a `price` shares cuyo costo medio era cost_basis/shares
            if s["shares"] > EPSILON:
                avg_cost = s["cost_basis"] / s["shares"]
                shares_sold = min(size, s["shares"])
                pnl_delta = (price - avg_cost) * shares_sold
                pnl_timeline.append((t["timestamp"], pnl_delta))
                s["cost_basis"] -= avg_cost * shares_sold
                s["shares"] -= shares_sold

    # Settlement final por posición (incluye mercados resueltos)
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    closed_positions: list[float] = []  # PnL por cada posición liquidable
    total_buy_cost = 0.0
    total_buy_cost_closed = 0.0

    for key, p in pos.items():
        cid, oi = key
        bsh, bcost = p["buy_sh"], p["buy_cost"]
        ssh, sproc = p["sell_sh"], p["sell_proc"]
        net = bsh - ssh
        total_buy_cost += bcost

        # Caso 1: cerrada o sobrevendida (vendió/redimió >= lo comprado en la ventana)
        # Para net<0 asumimos que el cost-basis previo está cubierto por shares
        # de antes del cap de 3500 trades.
        if net <= EPSILON:
            pnl = sproc - bcost
            realized_pnl += pnl
            closed_positions.append(pnl)
            total_buy_cost_closed += bcost
            continue

        # Caso 2: abierta pero mercado resuelto → settle con outcome_prices
        closed_meta, prices_json = market_meta.get(cid, (False, None))
        payout = _resolved_payout(prices_json, oi) if closed_meta else None
        if payout is not None:
            residual_value = net * payout
            pnl = (sproc + residual_value) - bcost
            realized_pnl += pnl
            closed_positions.append(pnl)
            total_buy_cost_closed += bcost
            continue

        # Caso 3: abierta sin resolución → no aporta a PnL realizado
        # (sin precio actual del CLOB, mejor no estimar)

    # Métricas derivadas
    n_closed = len(closed_positions)
    wins = sum(1 for x in closed_positions if x > 0)
    win_rate = wins / n_closed if n_closed else 0.0
    avg_pos_size = (
        sum(p["buy_cost"] for p in pos.values()) / len(pos) if pos else 0.0
    )
    roi_pct = (realized_pnl / total_buy_cost_closed * 100.0) if total_buy_cost_closed else 0.0
    sharpe = (
        (sum(closed_positions) / len(closed_positions)) / _stdev(closed_positions)
        if len(closed_positions) >= 2 and _stdev(closed_positions) > EPSILON
        else 0.0
    )

    # Drawdown sobre cum-pnl ordenado por tiempo
    pnl_timeline.sort(key=lambda x: x[0])
    cum = 0.0
    cum_series: list[float] = []
    for _, delta in pnl_timeline:
        cum += delta
        cum_series.append(cum)
    max_dd = _drawdown_pct(cum_series) * 100.0  # en %

    active_days = max(1, (last_ts - first_ts) // 86400)

    return WalletMetrics(
        wallet=wallet,
        total_trades=len(trades),
        total_volume_usdc=total_volume,
        realized_pnl_usdc=realized_pnl,
        unrealized_pnl_usdc=unrealized_pnl,
        roi_pct=roi_pct,
        win_rate=win_rate,
        avg_position_size=avg_pos_size,
        max_drawdown_pct=max_dd,
        sharpe_proxy=sharpe,
        active_days=active_days,
        first_trade_ts=first_ts,
        last_trade_ts=last_ts,
    )


# ---------------- score compuesto ----------------

def composite_score(m: WalletMetrics) -> float:
    """Score que penaliza survivorship bias y traders ruidosos.

    Filtros duros:
      - <100 trades     → score = 0
      - <180 días activo → score = 0
      - <50 posiciones cerradas → score = 0

    Caso contrario: combinación sigmoide-ish de ROI%, win_rate y sharpe.
    """
    # Filtros duros. Nota: el cap de 3500 trades de la API nos da los más
    # recientes, así que active_days representa "ventana reciente" no
    # "track record total". Mejor mirar volumen y PnL absoluto.
    if m.total_trades < 100:
        return 0.0
    if m.realized_pnl_usdc <= 0:                  # solo ganadores
        return 0.0
    if m.total_volume_usdc < 500:                 # ruido si vol < $500
        return 0.0
    # ROI confiable solo si hubo cost basis razonable (>= $200 invertido en cerradas).
    # Si no, el ROI viene inflado por shares con cost-basis pre-cap (incompleto).
    # En ese caso usamos solo PnL absoluto + sharpe + win_rate para rankear.

    # Componentes en [0..1]
    # ROI capped a 200% (cualquier valor mayor probablemente es artefacto del cap)
    roi_c = max(min(m.roi_pct / 200.0, 1.0), 0.0)
    win_c = m.win_rate                                          # 0..1
    sh_c = max(min(m.sharpe_proxy / 2.0, 1.0), 0.0)             # 0..1
    dd_penalty = max(0.0, 1.0 - m.max_drawdown_pct / 100.0)     # 1 si dd=0
    # PnL absoluto normalizado vía log10 ($100→0.4, $1k→0.6, $10k→0.8, $100k→1.0)
    pnl_c = min(math.log10(max(m.realized_pnl_usdc, 1)) / 5.0, 1.0)
    # Volumen: más volumen = más confianza ($1k→0.3, $10k→0.4, $100k→0.5, $1M→0.6)
    vol_c = min(math.log10(max(m.total_volume_usdc, 1)) / 7.0, 1.0)

    # Pesos: PnL 35%, win 20%, sharpe 15%, ROI 15%, vol 10%, dd 5%
    score = (
        0.35 * pnl_c
        + 0.20 * win_c
        + 0.15 * sh_c
        + 0.15 * roi_c
        + 0.10 * vol_c
        + 0.05 * dd_penalty
    )
    return score


# ---------------- batch / persistencia ----------------

UPSERT_METRICS = """
INSERT INTO trader_metrics (
    wallet, total_trades, total_volume_usdc, realized_pnl_usdc, unrealized_pnl_usdc,
    roi_pct, win_rate, avg_position_size, max_drawdown_pct, sharpe_proxy,
    active_days, first_trade_ts, last_trade_ts, score, computed_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
ON CONFLICT(wallet) DO UPDATE SET
    total_trades        = excluded.total_trades,
    total_volume_usdc   = excluded.total_volume_usdc,
    realized_pnl_usdc   = excluded.realized_pnl_usdc,
    unrealized_pnl_usdc = excluded.unrealized_pnl_usdc,
    roi_pct             = excluded.roi_pct,
    win_rate            = excluded.win_rate,
    avg_position_size   = excluded.avg_position_size,
    max_drawdown_pct    = excluded.max_drawdown_pct,
    sharpe_proxy        = excluded.sharpe_proxy,
    active_days         = excluded.active_days,
    first_trade_ts      = excluded.first_trade_ts,
    last_trade_ts       = excluded.last_trade_ts,
    score               = excluded.score,
    computed_at         = datetime('now');
"""


def compute_all(wallets: Iterable[str] | None = None) -> int:
    init_db()
    with db() as conn:
        if wallets is None:
            rows = conn.execute(
                "SELECT wallet FROM traders WHERE last_indexed_at IS NOT NULL"
            ).fetchall()
            wallets_list = [r["wallet"] for r in rows]
        else:
            wallets_list = list(wallets)

    if not wallets_list:
        console.print("[yellow]No hay wallets con backfill. Corré primero `backfill-all`.[/yellow]")
        return 0

    n = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        t = prog.add_task(f"Calculando métricas… 0/{len(wallets_list)}", total=None)
        batch: list[tuple] = []
        for i, w in enumerate(wallets_list, 1):
            try:
                m = compute_for_wallet(w)
                if m is None:
                    continue
                s = composite_score(m)
                batch.append(m.as_row(s))
            except Exception as e:
                log.exception("metrics failed for %s: %s", w, e)
            if len(batch) >= 200:
                with tx() as conn:
                    conn.executemany(UPSERT_METRICS, batch)
                n += len(batch)
                batch.clear()
            prog.update(t, description=f"Calculando métricas… {i}/{len(wallets_list)}")

        if batch:
            with tx() as conn:
                conn.executemany(UPSERT_METRICS, batch)
            n += len(batch)

    console.print(f"[green]✓[/green] Métricas guardadas para {n} wallets")
    return n


def top(limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trader_metrics
            WHERE score > 0
            ORDER BY score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
