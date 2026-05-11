"""Grid Bot Binance Spot — captura volatilidad sideways BTC/ETH USDT.

Diseño:
- N niveles de BUY equiespaciados en rango [low, high].
- Por cada BUY filled, postea SELL al nivel superior (grid_step + take_profit).
- Por cada SELL filled, postea BUY al nivel inferior.
- Captura swing entre niveles → profit por cada round trip = grid_step%.

Ejemplo BTC/USDT:
    range:  $95,000 – $110,000
    levels: 10 (cada $1,500)
    bet/level: $40 USDT
    → 10 BUYs posteadas a $95k, $96.5k, $98k, $99.5k, ..., $108.5k.
    Si BTC baja a $95k → BUY[0] fillea → posteo SELL[0] a $96.5k.
    Si BTC sube a $96.5k → SELL[0] fillea → profit ~1.5% (-fees 0.2%) ≈ 1.3%.

Edge: requiere mercado lateral (sideways). Si BTC sale del rango (trend
fuerte), grid pierde — todos los BUYs ejecutados pero no hay sells arriba.

Risk management:
- max_concurrent_buys: cap exposure si rango muy bajista.
- daily_loss_cap: si pnl_24h < -X, pause.
- min_spread_after_fees: 0.5% para que cubrir fees+slippage.

Activación: GRID_BOT_ENABLED=true env. Sin BINANCE_API_KEY no arranca.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.binance.spot_client import BinanceSpotClient, BinanceSpotError, SpotOrderResult
from src.db.schema import db, tx

log = logging.getLogger(__name__)


@dataclass
class GridConfig:
    enabled: bool = False
    symbol: str = "BTCUSDT"
    low_price: float = 0.0
    high_price: float = 0.0
    n_levels: int = 20  # más niveles (era 10) → más fills
    quote_per_level_usdt: float = 20.0  # bet más chico (era 40) para más concurrencia
    poll_interval_s: float = 8.0  # más rápido (era 15)
    max_concurrent_buys: int = 20
    daily_loss_cap_usdt: float = 40.0
    auto_range: bool = True
    auto_range_pct: float = 0.04  # ±4% del mid (era 8%) — grid más denso
    paper_mode: bool = False
    initial_usdt_paper: float = 400.0

    @classmethod
    def from_env(cls) -> "GridConfig":
        # Paper-safe defaults: si LIVE_MODE=false → arranca en paper con
        # 3 symbols + $400 virtual. En live mode sigue gateado por .env explícito.
        try:
            from src.config import LIVE_MODE as _LIVE
        except Exception:
            _LIVE = False
        _enable_default = "false" if _LIVE else "true"
        _paper_default = "false" if _LIVE else "true"
        return cls(
            enabled=os.getenv("GRID_BOT_ENABLED", _enable_default).lower() == "true",
            symbol=os.getenv("GRID_BOT_SYMBOL", "BTCUSDT"),
            low_price=float(os.getenv("GRID_BOT_LOW_PRICE", "0") or 0),
            high_price=float(os.getenv("GRID_BOT_HIGH_PRICE", "0") or 0),
            n_levels=int(os.getenv("GRID_BOT_LEVELS", "20")),
            quote_per_level_usdt=float(os.getenv("GRID_BOT_QUOTE_PER_LEVEL", "20")),
            poll_interval_s=float(os.getenv("GRID_BOT_POLL_S", "8")),
            max_concurrent_buys=int(os.getenv("GRID_BOT_MAX_BUYS", "20")),
            daily_loss_cap_usdt=float(os.getenv("GRID_BOT_DAILY_LOSS_CAP", "40")),
            auto_range=os.getenv("GRID_BOT_AUTO_RANGE", "true").lower() == "true",
            auto_range_pct=float(os.getenv("GRID_BOT_AUTO_RANGE_PCT", "0.04")),
            paper_mode=os.getenv("GRID_BOT_PAPER", _paper_default).lower() == "true",
            initial_usdt_paper=float(os.getenv("GRID_BOT_PAPER_USDT", "400")),
        )


@dataclass
class _GridState:
    # nivel index → exchange_order_id de la BUY/SELL posteada en ese nivel
    buy_orders: dict[int, str] = field(default_factory=dict)
    sell_orders: dict[int, dict] = field(default_factory=dict)  # idx → {order_id, buy_db_id, buy_price, qty}
    levels: list[float] = field(default_factory=list)
    grid_step: float = 0.0


def _record_order(*, strategy: str, symbol: str, side: str, order_type: str,
                  client_order_id: Optional[str], exchange_order_id: Optional[str],
                  price: Optional[float], qty: Optional[float],
                  quote_qty: Optional[float], status: str,
                  posted_at: int, notes: Optional[str] = None) -> int:
    with tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO binance_orders
                (strategy, symbol, side, order_type, client_order_id,
                 exchange_order_id, price, qty, quote_qty, status,
                 posted_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (strategy, symbol, side, order_type, client_order_id,
             exchange_order_id, price, qty, quote_qty, status, posted_at, notes),
        )
        try:
            return int(cur.lastrowid)
        except Exception:
            return 0


def _update_order_filled(*, exchange_order_id: str, fill_price: float,
                         fill_qty: float, filled_at: int,
                         pnl_usdc: Optional[float] = None) -> None:
    with tx() as conn:
        conn.execute(
            """
            UPDATE binance_orders
            SET status='FILLED', fill_price=?, fill_qty=?, filled_at=?,
                pnl_usdc=COALESCE(pnl_usdc, ?)
            WHERE exchange_order_id=?
            """,
            (fill_price, fill_qty, filled_at, pnl_usdc, exchange_order_id),
        )


def _update_order_canceled(*, exchange_order_id: str, canceled_at: int) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE binance_orders SET status='CANCELED', canceled_at=? WHERE exchange_order_id=?",
            (canceled_at, exchange_order_id),
        )


def _daily_pnl_grid(symbol: str) -> float:
    """Suma pnl_usdc de orders FILLED last 24h para `symbol`."""
    since = int(time.time()) - 86400
    with db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl_usdc), 0) AS p
            FROM binance_orders
            WHERE strategy='grid_bot' AND symbol=? AND status='FILLED'
              AND filled_at >= ? AND pnl_usdc IS NOT NULL
            """,
            (symbol, since),
        ).fetchone()
    try:
        return float(row["p"] or 0)
    except Exception:
        return 0.0


def _daily_pnl_grid_all() -> float:
    """Suma pnl_usdc 24h grid_bot ALL symbols (Binance total)."""
    since = int(time.time()) - 86400
    with db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl_usdc), 0) AS p
            FROM binance_orders
            WHERE strategy='grid_bot' AND status='FILLED'
              AND filled_at >= ? AND pnl_usdc IS NOT NULL
            """,
            (since,),
        ).fetchone()
    try:
        return float(row["p"] or 0)
    except Exception:
        return 0.0


def _total_pnl_grid(symbol: str | None = None) -> float:
    """Suma pnl_usdc total grid_bot. Si symbol=None → todos."""
    with db() as conn:
        if symbol:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(pnl_usdc), 0) AS p
                FROM binance_orders
                WHERE strategy='grid_bot' AND symbol=? AND status='FILLED'
                  AND pnl_usdc IS NOT NULL
                """,
                (symbol,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(pnl_usdc), 0) AS p
                FROM binance_orders
                WHERE strategy='grid_bot' AND status='FILLED' AND pnl_usdc IS NOT NULL
                """,
            ).fetchone()
    try:
        return float(row["p"] or 0)
    except Exception:
        return 0.0


def _notify(text: str) -> None:
    try:
        from src.copybot.notifier import send
        send(text)
    except Exception as e:
        log.debug("grid_bot notify failed: %s", e)


def _compute_levels(low: float, high: float, n: int) -> tuple[list[float], float]:
    if n < 2 or high <= low:
        return [], 0.0
    step = (high - low) / (n - 1)
    return [round(low + i * step, 8) for i in range(n)], step


async def _resolve_range(client: BinanceSpotClient, cfg: GridConfig) -> tuple[float, float]:
    """Si auto_range, calcula range alrededor del mid actual.
    Si explícito (low/high > 0), retorna esos.
    """
    if not cfg.auto_range and cfg.low_price > 0 and cfg.high_price > cfg.low_price:
        return cfg.low_price, cfg.high_price
    ticker = await client.get_book_ticker(cfg.symbol)
    try:
        bid = float(ticker.get("bidPrice", 0))
        ask = float(ticker.get("askPrice", 0))
        mid = (bid + ask) / 2
    except (TypeError, ValueError):
        raise BinanceSpotError(f"book_ticker inválido para {cfg.symbol}: {ticker}")
    if mid <= 0:
        raise BinanceSpotError(f"mid inválido para {cfg.symbol}: bid={bid} ask={ask}")
    return mid * (1 - cfg.auto_range_pct), mid * (1 + cfg.auto_range_pct)


class GridBot:
    """Grid bot operacional para un símbolo Binance Spot."""

    def __init__(self, *, config: Optional[GridConfig] = None,
                 client: Optional[BinanceSpotClient] = None) -> None:
        self.config = config or GridConfig.from_env()
        self.client = client
        self.state = _GridState()
        self._stop = asyncio.Event()
        self._paused = False

    async def __aenter__(self) -> "GridBot":
        if self.client is None:
            self.client = BinanceSpotClient()
            await self.client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.client is not None:
            await self.client.__aexit__(exc_type, exc, tb)

    async def _init_grid(self) -> None:
        """Resolver range + computar levels + persistir."""
        if self.client is None:
            raise RuntimeError("client no inicializado")
        low, high = await _resolve_range(self.client, self.config)
        levels, step = _compute_levels(low, high, self.config.n_levels)
        self.state.levels = levels
        self.state.grid_step = step
        log.info(
            "grid_bot.init symbol=%s low=%.2f high=%.2f levels=%d step=%.2f bet=$%.2f",
            self.config.symbol, low, high, len(levels), step,
            self.config.quote_per_level_usdt,
        )
        _notify(
            f"🟦 *Grid Bot* arrancado\n"
            f"Symbol: `{self.config.symbol}`\n"
            f"Range: ${low:.2f}–${high:.2f}\n"
            f"Levels: {len(levels)} (step ${step:.2f})\n"
            f"Bet/level: ${self.config.quote_per_level_usdt:.2f} USDT"
        )

    async def _post_initial_buys(self) -> None:
        """Postea BUY en cada nivel <= mid (debajo del precio actual)."""
        if self.client is None or not self.state.levels:
            return
        ticker = await self.client.get_book_ticker(self.config.symbol)
        try:
            mid = (float(ticker["bidPrice"]) + float(ticker["askPrice"])) / 2
        except (KeyError, TypeError, ValueError):
            log.error("grid_bot: mid no resoluble, skip initial buys")
            return
        # postear BUY en niveles strictamente < mid (los demás esperan caída)
        posted = 0
        for idx, lvl in enumerate(self.state.levels):
            if lvl >= mid:
                break
            if idx in self.state.buy_orders:
                continue
            if posted >= self.config.max_concurrent_buys:
                break
            await self._place_buy_at(idx, lvl)
            posted += 1

    async def _place_buy_at(self, level_idx: int, price: float) -> None:
        if self.client is None:
            return
        cid = f"grid-buy-{level_idx}-{uuid.uuid4().hex[:6]}"
        res = await self.client.place_limit_buy(
            self.config.symbol, price=price,
            quote_qty=self.config.quote_per_level_usdt,
            client_order_id=cid,
        )
        if not res.ok or not res.order_id:
            log.warning("grid_bot.buy_fail lvl=%d price=%.2f err=%s",
                        level_idx, price, res.error)
            return
        _record_order(
            strategy="grid_bot", symbol=res.symbol, side="BUY",
            order_type="LIMIT", client_order_id=cid,
            exchange_order_id=res.order_id,
            price=res.price, qty=res.qty, quote_qty=res.quote_qty,
            status=res.status or "NEW", posted_at=int(time.time()),
            notes=f"level={level_idx}",
        )
        self.state.buy_orders[level_idx] = res.order_id
        log.info("grid_bot.buy_posted lvl=%d price=%.2f order=%s",
                 level_idx, price, res.order_id)

    async def _place_sell_at(self, level_idx: int, price: float, qty: float,
                             buy_price: float) -> None:
        if self.client is None or qty <= 0:
            return
        cid = f"grid-sell-{level_idx}-{uuid.uuid4().hex[:6]}"
        res = await self.client.place_limit_sell(
            self.config.symbol, price=price, qty=qty, client_order_id=cid,
        )
        if not res.ok or not res.order_id:
            log.warning("grid_bot.sell_fail lvl=%d price=%.2f err=%s",
                        level_idx, price, res.error)
            return
        _record_order(
            strategy="grid_bot", symbol=res.symbol, side="SELL",
            order_type="LIMIT", client_order_id=cid,
            exchange_order_id=res.order_id,
            price=res.price, qty=res.qty,
            quote_qty=(res.price or price) * (res.qty or qty),
            status=res.status or "NEW", posted_at=int(time.time()),
            notes=f"level={level_idx} buy_price={buy_price}",
        )
        self.state.sell_orders[level_idx] = {
            "order_id": res.order_id,
            "buy_price": buy_price,
            "qty": qty,
            "sell_price": price,
        }
        log.info("grid_bot.sell_posted lvl=%d price=%.2f qty=%.6f",
                 level_idx, price, qty)

    async def _reconcile_fills(self) -> None:
        """Pulla openOrders + detecta los que NO están abiertos (filled o canceled).
        Para cada BUY filled: postea SELL al nivel superior.
        Para cada SELL filled: postea BUY al nivel inferior + calc PnL.
        """
        if self.client is None:
            return
        open_orders = await self.client.get_open_orders(self.config.symbol)
        open_ids = {str(o.get("orderId")) for o in open_orders}

        # BUYs que ya no están abiertos
        for idx, oid in list(self.state.buy_orders.items()):
            if oid not in open_ids:
                # Asumimos filled (TODO: distinguir vs canceled via /allOrders).
                buy_price = self.state.levels[idx]
                qty = self.config.quote_per_level_usdt / buy_price if buy_price > 0 else 0
                now = int(time.time())
                _update_order_filled(
                    exchange_order_id=oid, fill_price=buy_price,
                    fill_qty=qty, filled_at=now,
                )
                del self.state.buy_orders[idx]
                # Acumulados Binance (cross-symbols) para visibilidad
                bin_24h = _daily_pnl_grid_all()
                bin_total = _total_pnl_grid()
                _notify(
                    f"🟢 *Grid BUY filled*\n"
                    f"Symbol: `{self.config.symbol}`\n"
                    f"Level: {idx} · Price: ${buy_price:.2f}\n"
                    f"Qty: {qty:.6f} · Notional: ${self.config.quote_per_level_usdt:.2f}\n"
                    f"PnL trade: $0 (pendiente sell)\n"
                    f"Acumulado Binance 24h: ${bin_24h:+.2f} · Total: ${bin_total:+.2f}"
                )
                # Postear SELL al nivel siguiente arriba
                if idx + 1 < len(self.state.levels):
                    sell_price = self.state.levels[idx + 1]
                    await self._place_sell_at(idx + 1, sell_price, qty, buy_price)

        # SELLs que ya no están abiertos
        for idx, info in list(self.state.sell_orders.items()):
            oid = info["order_id"]
            if oid not in open_ids:
                now = int(time.time())
                buy_price = info["buy_price"]
                sell_price = info["sell_price"]
                qty = info["qty"]
                pnl = (sell_price - buy_price) * qty
                # Fee aproximado 0.1% × 2 (BUY + SELL)
                fee = (buy_price + sell_price) * qty * 0.001
                pnl_net = pnl - fee
                _update_order_filled(
                    exchange_order_id=oid, fill_price=sell_price,
                    fill_qty=qty, filled_at=now, pnl_usdc=pnl_net,
                )
                del self.state.sell_orders[idx]
                emoji = "💰" if pnl_net > 0 else "🔻"
                # Acumulado Binance global (cross-symbols) — incluye este fill
                bin_24h = _daily_pnl_grid_all()
                bin_total = _total_pnl_grid()
                sym_24h = _daily_pnl_grid(self.config.symbol)
                _notify(
                    f"{emoji} *Grid round trip cerrado*\n"
                    f"Symbol: `{self.config.symbol}` lvl {idx-1}→{idx}\n"
                    f"Buy ${buy_price:.2f} → Sell ${sell_price:.2f}\n"
                    f"PnL trade: ${pnl_net:+.4f} (gross ${pnl:+.4f} - fee ${fee:.4f})\n"
                    f"{self.config.symbol} 24h: ${sym_24h:+.2f}\n"
                    f"Acumulado Binance 24h: ${bin_24h:+.2f} · Total: ${bin_total:+.2f}"
                )
                # Re-postear BUY al nivel original
                await self._place_buy_at(idx - 1, buy_price)

    async def _check_kill_switch(self) -> bool:
        pnl_24h = _daily_pnl_grid(self.config.symbol)
        if pnl_24h < -abs(self.config.daily_loss_cap_usdt):
            if not self._paused:
                self._paused = True
                _notify(
                    f"⛔ *Grid Bot PAUSED*\n"
                    f"Symbol: `{self.config.symbol}`\n"
                    f"PnL 24h: ${pnl_24h:.2f} <= -${self.config.daily_loss_cap_usdt:.2f}"
                )
                log.warning("grid_bot.paused pnl_24h=%.2f cap=%.2f",
                            pnl_24h, self.config.daily_loss_cap_usdt)
            return True
        return False

    async def run_loop(self) -> None:
        if not self.config.enabled:
            log.info("grid_bot: disabled (GRID_BOT_ENABLED!=true)")
            return
        if not self.config.paper_mode and not (
            os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_API_SECRET")
        ):
            log.warning("grid_bot: BINANCE_API_KEY/SECRET ausentes — no arrancado")
            return
        # Setup client (paper o real)
        try:
            if self.client is None:
                if self.config.paper_mode:
                    from src.binance.spot_paper_client import BinanceSpotPaperClient
                    from src.binance.websocket import BinanceTickerWS
                    ws = BinanceTickerWS([self.config.symbol])
                    self._ws = ws  # type: ignore[attr-defined]
                    ws_task = asyncio.create_task(ws.run())
                    self._ws_task = ws_task  # type: ignore[attr-defined]
                    # Esperar primer tick — max 90s (más tolerante).
                    for i in range(180):
                        if ws.get_price(self.config.symbol) is not None:
                            log.info(
                                "grid_bot %s WS first tick recibido tras %.1fs",
                                self.config.symbol, i * 0.5,
                            )
                            break
                        await asyncio.sleep(0.5)
                    else:
                        log.error(
                            "grid_bot %s: WS no entregó tick en 90s — reintento en 60s",
                            self.config.symbol,
                        )
                        await asyncio.sleep(60)
                        return
                    self.client = BinanceSpotPaperClient(  # type: ignore[assignment]
                        initial_usdt=self.config.initial_usdt_paper, ws=ws,
                    )
                    await self.client.__aenter__()  # type: ignore[attr-defined]
                    await self.client.start()  # type: ignore[attr-defined]
                    log.warning(
                        "grid_bot %s arrancado en PAPER MODE — $%.2f virtual",
                        self.config.symbol, self.config.initial_usdt_paper,
                    )
                else:
                    self.client = BinanceSpotClient()
                    await self.client.__aenter__()
        except Exception as e:
            log.exception("grid_bot %s setup_client failed: %s", self.config.symbol, e)
            return

        # Init grid + post buys con retry (no morir el task si falla la 1ra vez)
        for attempt in range(5):
            try:
                await self._init_grid()
                await self._post_initial_buys()
                log.info("grid_bot %s init OK (attempt %d)", self.config.symbol, attempt + 1)
                break
            except Exception as e:
                log.warning(
                    "grid_bot %s init_attempt %d failed: %s",
                    self.config.symbol, attempt + 1, e,
                )
                await asyncio.sleep(15 * (attempt + 1))
        else:
            log.error("grid_bot %s init failed 5 attempts — exit", self.config.symbol)
            return

        while not self._stop.is_set():
            try:
                if await self._check_kill_switch():
                    await asyncio.sleep(60)
                    continue
                await self._reconcile_fills()
            except Exception as e:
                log.warning("grid_bot.cycle_err: %s", e)
            await asyncio.sleep(self.config.poll_interval_s)

    def stop(self) -> None:
        self._stop.set()


async def maybe_start_grid_bot_in_background() -> Optional[asyncio.Task]:
    """Arranca grid(s) — soporta multi-symbol via GRID_BOT_SYMBOLS env CSV.

    Si GRID_BOT_SYMBOLS está definido (ej. "BTCUSDT,ETHUSDT,SOLUSDT"),
    arranca un grid separado por símbolo con budget proporcional.
    Sino, usa GRID_BOT_SYMBOL (single symbol legacy).
    """
    base_cfg = GridConfig.from_env()
    if not base_cfg.enabled:
        return None
    if not base_cfg.paper_mode and not (
        os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_API_SECRET")
    ):
        log.warning("grid_bot: GRID_BOT_ENABLED=true pero falta key — no arrancado")
        return None

    # Default multi-symbol cuando paper (sin .env override).
    default_symbols = "BTCUSDT,ETHUSDT,SOLUSDT" if base_cfg.paper_mode else base_cfg.symbol
    symbols_csv = os.getenv("GRID_BOT_SYMBOLS", default_symbols)
    if symbols_csv:
        symbols = [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]
    else:
        symbols = [base_cfg.symbol]

    if len(symbols) == 1:
        cfg = base_cfg
        cfg.symbol = symbols[0]
        bot = GridBot(config=cfg)
        return asyncio.create_task(bot.run_loop())

    # Multi-symbol: split budget + levels entre symbols.
    from dataclasses import replace
    total_usdt = base_cfg.initial_usdt_paper if base_cfg.paper_mode else (
        base_cfg.quote_per_level_usdt * base_cfg.n_levels
    )
    per_sym_usdt = total_usdt / len(symbols)
    per_sym_levels = max(4, base_cfg.n_levels // len(symbols) + 4)
    per_sym_bet = per_sym_usdt / per_sym_levels

    tasks: list[asyncio.Task] = []
    for sym in symbols:
        sym_cfg = replace(
            base_cfg,
            symbol=sym, n_levels=per_sym_levels,
            quote_per_level_usdt=per_sym_bet,
            initial_usdt_paper=per_sym_usdt,
            max_concurrent_buys=per_sym_levels,
        )
        bot = GridBot(config=sym_cfg)
        tasks.append(asyncio.create_task(bot.run_loop()))
        log.info(
            "grid_bot multi: %s budget=$%.2f levels=%d bet=$%.2f",
            sym, per_sym_usdt, per_sym_levels, per_sym_bet,
        )

    # Wrapper task que espera a todos. Caller solo recibe uno (compat).
    async def _wait_all() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
    return asyncio.create_task(_wait_all())
