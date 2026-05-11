"""Signal Bot Binance — indicadores RSI/EMA/MACD con alertas Telegram.

Computa indicadores técnicos sobre el WS público de Binance (mismo source
que crypto_arb). Cada vez que un indicador genera señal fuerte (RSI
oversold/overbought, EMA cross, MACD divergence) → notif Telegram.

Diseño:
- Mantiene rolling buffer de prices por símbolo (últimas N=200 muestras).
- Cada N segundos computa indicadores.
- Filtros de noise: signal solo si cumple threshold + cool-down 30min
  entre signals del mismo type/symbol (evita spam).
- Optional auto-execute: SIGNAL_BOT_AUTO_EXECUTE=true ejecuta market en
  paper client (mismo client del grid bot si está corriendo).

Indicadores implementados (puros Python, sin numpy/pandas):
- RSI(14): oversold <30 → BUY signal, overbought >70 → SELL.
- EMA(9) cross EMA(21): golden cross → BUY, death cross → SELL.
- Price move >2%/1h: pump/dump alert (no trade signal, solo notif).

Activación: SIGNAL_BOT_ENABLED=true env.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class SignalConfig:
    enabled: bool = False
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    poll_s: float = 5.0  # más rápido (era 10s)
    rsi_period: int = 14
    rsi_oversold: float = 35.0   # más permisivo (era 30) → más signals
    rsi_overbought: float = 65.0  # más permisivo (era 70)
    ema_short: int = 9
    ema_long: int = 21
    pump_threshold_pct: float = 0.5  # micro-pumps 0.5% (era 2.0)
    cooldown_s: int = 300  # 5min cooldown (era 30min)
    auto_execute: bool = True  # AUTO-TRADE habilitado (paper safe)
    execute_quote_usdt: float = 15.0  # market BUY $15 por signal

    @classmethod
    def from_env(cls) -> "SignalConfig":
        # Defaults agresivos: bot ya tiene predictor (RSI/EMA), usémoslo para tradear.
        symbols_csv = os.getenv("SIGNAL_BOT_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT")
        return cls(
            enabled=os.getenv("SIGNAL_BOT_ENABLED", "true").lower() == "true",
            symbols=tuple(s.strip().upper() for s in symbols_csv.split(",") if s.strip()),
            poll_s=float(os.getenv("SIGNAL_BOT_POLL_S", "5")),
            rsi_period=int(os.getenv("SIGNAL_BOT_RSI_PERIOD", "14")),
            rsi_oversold=float(os.getenv("SIGNAL_BOT_RSI_OVERSOLD", "35")),
            rsi_overbought=float(os.getenv("SIGNAL_BOT_RSI_OVERBOUGHT", "65")),
            ema_short=int(os.getenv("SIGNAL_BOT_EMA_SHORT", "9")),
            ema_long=int(os.getenv("SIGNAL_BOT_EMA_LONG", "21")),
            pump_threshold_pct=float(os.getenv("SIGNAL_BOT_PUMP_PCT", "0.5")),
            cooldown_s=int(os.getenv("SIGNAL_BOT_COOLDOWN_S", "300")),
            auto_execute=os.getenv("SIGNAL_BOT_AUTO_EXECUTE", "true").lower() == "true",
            execute_quote_usdt=float(os.getenv("SIGNAL_BOT_EXECUTE_USDT", "15")),
        )


def _compute_rsi(prices: list[float], period: int = 14) -> Optional[float]:
    """RSI Wilder smoothing. Devuelve None si no hay suficiente data."""
    if len(prices) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    # Primer avg simple
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder smoothing para el resto
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_ema(prices: list[float], period: int) -> Optional[float]:
    """EMA simple. Devuelve None si menos de `period` muestras."""
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


@dataclass
class _SymbolState:
    prices_5s: deque = field(default_factory=lambda: deque(maxlen=200))
    last_pump_alert: float = 0.0
    last_rsi_alert: float = 0.0
    last_ema_alert: float = 0.0
    last_ema_relation: Optional[str] = None  # "above" | "below" — para detectar cross


def _notify(text: str) -> None:
    try:
        from src.copybot.notifier import send
        send(text)
    except Exception as e:
        log.debug("signal_bot notify failed: %s", e)


class SignalBot:
    def __init__(self, *, config: Optional[SignalConfig] = None,
                 paper_client=None) -> None:
        self.config = config or SignalConfig.from_env()
        self.paper_client = paper_client
        self._stop = asyncio.Event()
        self._state: dict[str, _SymbolState] = {s: _SymbolState() for s in self.config.symbols}
        self._ws = None

    async def run_loop(self) -> None:
        if not self.config.enabled:
            log.info("signal_bot: disabled (SIGNAL_BOT_ENABLED!=true)")
            return
        # WS público — no requiere API key
        from src.binance.websocket import BinanceTickerWS
        self._ws = BinanceTickerWS(self.config.symbols)
        ws_task = asyncio.create_task(self._ws.run())
        # Si auto_execute activo y no nos pasaron paper_client, creamos uno.
        # Comparte WS para que los precios estén sync.
        if self.config.auto_execute and self.paper_client is None:
            from src.binance.spot_paper_client import BinanceSpotPaperClient
            self.paper_client = BinanceSpotPaperClient(
                initial_usdt=100.0, ws=self._ws,
            )
            await self.paper_client.__aenter__()
            await self.paper_client.start()
            log.warning("signal_bot: auto_execute ON — paper client $100 USDT virtual")
        log.warning(
            "signal_bot arrancado — symbols=%s RSI(%d) EMA(%d/%d) pump=%.1f%%",
            ",".join(self.config.symbols), self.config.rsi_period,
            self.config.ema_short, self.config.ema_long,
            self.config.pump_threshold_pct,
        )
        _notify(
            "🟪 *Signal Bot* arrancado\n"
            f"Symbols: `{','.join(self.config.symbols)}`\n"
            f"Indicators: RSI({self.config.rsi_period}), "
            f"EMA({self.config.ema_short}/{self.config.ema_long}), "
            f"Pump±{self.config.pump_threshold_pct}%/1h"
        )
        try:
            # Esperar primer tick
            for _ in range(60):
                if any(self._ws.get_price(s) is not None for s in self.config.symbols):
                    break
                await asyncio.sleep(0.5)
            while not self._stop.is_set():
                try:
                    await self._evaluate_all()
                except Exception as e:
                    log.warning("signal_bot.cycle_err: %s", e)
                await asyncio.sleep(self.config.poll_s)
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _evaluate_all(self) -> None:
        now = time.time()
        for sym in self.config.symbols:
            tick = self._ws.get_price(sym)
            if tick is None:
                continue
            price, _ts = tick
            st = self._state[sym]
            st.prices_5s.append(float(price))
            # Pump/dump check: comparar primer vs último en buffer
            if len(st.prices_5s) >= 60 and now - st.last_pump_alert > self.config.cooldown_s:
                first = st.prices_5s[0]
                pct = (price / first - 1) * 100
                if abs(pct) >= self.config.pump_threshold_pct:
                    direction = "🚀 PUMP" if pct > 0 else "💥 DUMP"
                    _notify(
                        f"{direction} `{sym}`\n"
                        f"Δ {pct:+.2f}% en ~{len(st.prices_5s)*self.config.poll_s:.0f}s\n"
                        f"Price: ${price:,.2f}"
                    )
                    st.last_pump_alert = now
            # RSI signal
            prices_list = list(st.prices_5s)
            rsi = _compute_rsi(prices_list, self.config.rsi_period)
            if rsi is not None and now - st.last_rsi_alert > self.config.cooldown_s:
                if rsi <= self.config.rsi_oversold:
                    _notify(
                        f"🟢 RSI BUY `{sym}` — oversold\n"
                        f"RSI({self.config.rsi_period})={rsi:.1f} ≤ {self.config.rsi_oversold}\n"
                        f"Price: ${price:,.2f}"
                    )
                    st.last_rsi_alert = now
                    if self.config.auto_execute and self.paper_client is not None:
                        await self._auto_execute(sym, "BUY", price)
                elif rsi >= self.config.rsi_overbought:
                    _notify(
                        f"🔴 RSI SELL `{sym}` — overbought\n"
                        f"RSI({self.config.rsi_period})={rsi:.1f} ≥ {self.config.rsi_overbought}\n"
                        f"Price: ${price:,.2f}"
                    )
                    st.last_rsi_alert = now
            # EMA cross signal
            ema_s = _compute_ema(prices_list, self.config.ema_short)
            ema_l = _compute_ema(prices_list, self.config.ema_long)
            if ema_s is not None and ema_l is not None:
                relation = "above" if ema_s > ema_l else "below"
                if (st.last_ema_relation is not None and
                        relation != st.last_ema_relation and
                        now - st.last_ema_alert > self.config.cooldown_s):
                    direction = "Golden cross" if relation == "above" else "Death cross"
                    emoji = "🟢" if relation == "above" else "🔴"
                    _notify(
                        f"{emoji} EMA {direction} `{sym}`\n"
                        f"EMA({self.config.ema_short})={ema_s:,.2f} "
                        f"{'>' if relation == 'above' else '<'} "
                        f"EMA({self.config.ema_long})={ema_l:,.2f}\n"
                        f"Price: ${price:,.2f}"
                    )
                    st.last_ema_alert = now
                    if (self.config.auto_execute and self.paper_client is not None
                            and relation == "above"):
                        await self._auto_execute(sym, "BUY", price)
                st.last_ema_relation = relation

    async def _auto_execute(self, symbol: str, side: str, price: float) -> None:
        if not self.paper_client or side != "BUY":
            return
        try:
            res = await self.paper_client.place_market_buy(
                symbol, quote_qty=self.config.execute_quote_usdt,
                client_order_id=f"signal-{int(time.time())}",
            )
            if res.ok:
                _notify(
                    f"⚙️ *Signal auto-execute* `{symbol}` BUY\n"
                    f"Qty: {res.qty} · Notional: ${res.quote_qty:.2f}\n"
                    f"Price: ${price:,.2f}"
                )
        except Exception as e:
            log.warning("signal_bot.auto_execute_fail: %s", e)


async def maybe_start_signal_bot_in_background(paper_client=None) -> Optional[asyncio.Task]:
    cfg = SignalConfig.from_env()
    if not cfg.enabled:
        return None
    bot = SignalBot(config=cfg, paper_client=paper_client)
    return asyncio.create_task(bot.run_loop())
