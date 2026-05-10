"""Long-horizon crypto arbitrage — pivot del crypto-arb 5min thin.

Estrategia (Nivel 2 pivot, 2026-05-10):
- El crypto-arb 5min (``crypto_arb.py``) opera updown buckets con orderbooks
  thin ($1-5k liquidez) → slippage 50-90% en LIVE → pérdida garantizada.
- Pivoteamos a markets crypto LONG-HORIZON con liquidez decente
  ($30k-$500k): bitcoin-100k-by-end-2026, eth-reaches-X-this-week, etc.
- Edge fuente: cuando spot se mueve fuerte (>=2% en 1h), el mid Polymarket
  lagea HORAS o DÍAS antes de ajustar — markets long-horizon tienen pocos
  participantes activos por minuto.
- Posteamos LIMIT BUY al mid actual (no market — orderbook honesto pero
  limit es más safe). Si fillea, mantenemos hasta:
  (a) market resuelve, o
  (b) mid sube 50%+ del entry (early exit con profit).

Diferencia vs ``crypto_arb.py`` (5min) y ``spike_arb.py``:
- Loop interval 5min (no 15s) — markets long-horizon no se mueven rápido.
- Universo de markets: gamma filtrado por liquidez >= $30k + slug regex
  (btc|eth|sol|xrp|bnb|doge).*-(2026|2027|monthly|weekly|by-) + end_date
  futuro >= 1 día.
- Edge calculation: el spot move 1h se traduce en edge en probability
  points (pp). Para markets de varios días, el edge tiene que ser GRANDE
  (default 5pp = 5% absolute) para superar la fricción (spread + fees +
  capital tied por días/semanas).

Schema:
- Tabla ``long_horizon_trades`` (creada on-demand al arrancar el loop).

Activación: env var ``LONG_HORIZON_ENABLED=true``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)


# --- Defaults ---
DEFAULT_MIN_LIQ_USDC = 30000.0
DEFAULT_MIN_EDGE_PCT = 5.0           # edge mínimo en pp absolute (5pp = 5%)
DEFAULT_BET_USDC = 10.0
DEFAULT_CHECK_INTERVAL_S = 300       # 5 min
DEFAULT_MIN_HORIZON_S = 86400        # end_date debe estar a >= 1 día
DEFAULT_MAX_MID_TARGET = 0.85        # mid del lado a comprar < 0.85 (no ya priced)
DEFAULT_LIMIT_TTL_S = 3600           # GTC 1 hora
DEFAULT_SPOT_LOOKBACK_S = 3600       # ventana de 1h para spot move
DEFAULT_EARLY_EXIT_GAIN_PCT = 0.50   # si mid sube 50% sobre entry → close

# Mapping symbol → underlying short slug match. Mismas pairs que crypto_arb.
SYMBOL_TO_UNDERLYING: dict[str, str] = {
    "BTCUSDT": "btc",
    "ETHUSDT": "eth",
    "SOLUSDT": "sol",
    "XRPUSDT": "xrp",
    "BNBUSDT": "bnb",
    "DOGEUSDT": "doge",
}
UNDERLYING_TO_SYMBOL: dict[str, str] = {v: k for k, v in SYMBOL_TO_UNDERLYING.items()}

# Regex para slugs long-horizon. Reconocemos slugs que:
#  - empiezan con un underlying conocido (btc/bitcoin/eth/ethereum/...)
#  - mencionan año (2026/2027), monthly/weekly/by-* o reach* (anclas
#    típicas de markets long-horizon).
# Ejemplos válidos:
#   bitcoin-200k-by-end-2026
#   eth-reaches-5k-this-month
#   sol-300-by-2026-12-31
#   btc-monthly-up-may
#   ethereum-2027-end-of-year
# NO matchea:
#   btc-updown-5m-1700000000  (ese es el universo del crypto_arb)
SLUG_REGEX = re.compile(
    r"^(btc|bitcoin|eth|ethereum|sol|solana|xrp|bnb|doge|dogecoin)"
    r"(?:-[a-z0-9]+)*?"
    r"-(?:by|reach|reaches|monthly|weekly|end|"
    r"\d{4})"
    r"(?:[-a-z0-9]*)?$"
)
# Excluyentes — slugs de updown (universo de crypto_arb) NO entran aunque
# matcheen el prefijo de underlying.
SLUG_EXCLUDE_REGEX = re.compile(r"-updown-(?:5m|15m|1h)-\d+")


# Underlying alias → key para SYMBOL lookup.
UNDERLYING_ALIAS: dict[str, str] = {
    "btc": "btc",
    "bitcoin": "btc",
    "eth": "eth",
    "ethereum": "eth",
    "sol": "sol",
    "solana": "sol",
    "xrp": "xrp",
    "bnb": "bnb",
    "doge": "doge",
    "dogecoin": "doge",
}


# Type aliases.
# OrderExecutor: dado un signal dict, postea limit order y devuelve resultado.
# Permite testear sin tocar CLOB real.
# SpotProvider: dado un symbol y timestamp (en segundos), devuelve precio
# spot histórico (None si no hay sample).
OrderExecutor = Callable[[dict], Awaitable[dict]]
SpotProvider = Callable[[str, int], Optional[float]]


@dataclass
class LongHorizonConfig:
    enabled: bool = False
    min_liq_usdc: float = DEFAULT_MIN_LIQ_USDC
    min_edge_pct: float = DEFAULT_MIN_EDGE_PCT
    bet_usdc: float = DEFAULT_BET_USDC
    check_interval_s: int = DEFAULT_CHECK_INTERVAL_S
    min_horizon_s: int = DEFAULT_MIN_HORIZON_S
    max_mid_target: float = DEFAULT_MAX_MID_TARGET
    limit_ttl_s: int = DEFAULT_LIMIT_TTL_S
    spot_lookback_s: int = DEFAULT_SPOT_LOOKBACK_S
    early_exit_gain_pct: float = DEFAULT_EARLY_EXIT_GAIN_PCT
    symbols: tuple[str, ...] = field(
        default_factory=lambda: tuple(SYMBOL_TO_UNDERLYING.keys())
    )

    @classmethod
    def from_env(cls) -> "LongHorizonConfig":
        return cls(
            enabled=os.getenv("LONG_HORIZON_ENABLED", "false").lower() == "true",
            min_liq_usdc=float(os.getenv(
                "LONG_HORIZON_MIN_LIQ_USDC", DEFAULT_MIN_LIQ_USDC)),
            min_edge_pct=float(os.getenv(
                "LONG_HORIZON_MIN_EDGE_PCT", DEFAULT_MIN_EDGE_PCT)),
            bet_usdc=float(os.getenv(
                "LONG_HORIZON_BET_USDC", DEFAULT_BET_USDC)),
            check_interval_s=int(os.getenv(
                "LONG_HORIZON_CHECK_INTERVAL_S", DEFAULT_CHECK_INTERVAL_S)),
        )


@dataclass
class _Metrics:
    cycles: int = 0
    markets_seen: int = 0
    markets_filtered: int = 0       # pasaron filtro liq + slug + horizon
    skipped_no_underlying: int = 0
    skipped_no_spot: int = 0
    skipped_low_liq: int = 0
    skipped_low_edge: int = 0
    skipped_overbought: int = 0
    orders_posted: int = 0
    orders_filled: int = 0
    orders_cancelled_ttl: int = 0
    orders_failed: int = 0
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "cycles": self.cycles,
            "markets_seen": self.markets_seen,
            "markets_filtered": self.markets_filtered,
            "skipped_no_underlying": self.skipped_no_underlying,
            "skipped_no_spot": self.skipped_no_spot,
            "skipped_low_liq": self.skipped_low_liq,
            "skipped_low_edge": self.skipped_low_edge,
            "skipped_overbought": self.skipped_overbought,
            "orders_posted": self.orders_posted,
            "orders_filled": self.orders_filled,
            "orders_cancelled_ttl": self.orders_cancelled_ttl,
            "orders_failed": self.orders_failed,
        }


# --- DB schema (on-demand) ---

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS long_horizon_trades (
    id                BIGSERIAL PRIMARY KEY,
    market_slug       TEXT NOT NULL,
    underlying        TEXT,
    side              TEXT,
    entry_mid         DOUBLE PRECISION,
    spot_at_entry     DOUBLE PRECISION,
    spot_1h_ago       DOUBLE PRECISION,
    spot_move_pct     DOUBLE PRECISION,
    edge_estimate_pct DOUBLE PRECISION,
    bet_usdc          DOUBLE PRECISION,
    order_id          TEXT,
    fill_price        DOUBLE PRECISION,
    status            TEXT DEFAULT 'open',
    pnl_usdc          DOUBLE PRECISION,
    opened_at         BIGINT,
    closed_at         BIGINT,
    end_date_market   BIGINT,
    raw               JSONB
)
"""

_TABLE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_lh_status ON long_horizon_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_lh_underlying ON long_horizon_trades(underlying)",
    "CREATE INDEX IF NOT EXISTS idx_lh_opened_at ON long_horizon_trades(opened_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_lh_slug ON long_horizon_trades(market_slug)",
]


def init_table() -> None:
    """Crea la tabla ``long_horizon_trades`` si no existe.

    Idempotente. Compatible con SQLite y Postgres (BIGSERIAL + JSONB se
    traducen vía ``_translate_sql_to_pg``; SQLite acepta BIGSERIAL como
    INTEGER y JSONB como TEXT en este wrapper).
    """
    try:
        from src.db.schema import db
        with db() as conn:
            # En sqlite, BIGSERIAL/JSONB no existen; usamos un fallback
            # equivalente. En PG, ambos types son nativos.
            from src.db.schema import BACKEND
            ddl = _TABLE_DDL
            if BACKEND == "sqlite":
                ddl = (
                    ddl
                    .replace("BIGSERIAL PRIMARY KEY",
                             "INTEGER PRIMARY KEY AUTOINCREMENT")
                    .replace("DOUBLE PRECISION", "REAL")
                    .replace("JSONB", "TEXT")
                    .replace("BIGINT", "INTEGER")
                )
            conn.execute(ddl)
            for ix in _TABLE_INDEXES:
                conn.execute(ix)
    except Exception:
        log.exception("long_horizon_arb.init_table failed")


# --- Slug parser / underlying detection ---

def parse_underlying(slug: str) -> Optional[str]:
    """Extrae underlying short (btc/eth/sol/xrp/bnb/doge) de un slug.

    Devuelve None si:
    - slug es None o no matchea la regex de long-horizon
    - el slug es de updown 5m/15m/1h (excluido)
    """
    if not slug:
        return None
    s = slug.strip().lower()
    if SLUG_EXCLUDE_REGEX.search(s):
        return None
    m = SLUG_REGEX.match(s)
    if not m:
        return None
    raw = m.group(1)
    return UNDERLYING_ALIAS.get(raw)


def filter_market(market: dict, *, min_liq: float, min_horizon_s: int,
                  now_ts: int) -> bool:
    """Devuelve True si el market pasa los filtros básicos.

    Filtros:
      - liquidez >= min_liq
      - underlying parseable del slug
      - end_date >= now + min_horizon_s (markets que cierran antes del
        horizonte mínimo no son "long-horizon")
      - closed != True
    """
    if market.get("closed") is True:
        return False
    try:
        liq = float(market.get("liquidity") or 0)
    except (TypeError, ValueError):
        liq = 0.0
    if liq < min_liq:
        return False
    underlying = parse_underlying(market.get("slug") or "")
    if underlying is None:
        return False
    end_iso = market.get("endDate") or ""
    end_ts = _parse_iso_to_epoch(end_iso)
    if end_ts is None:
        return False
    if end_ts - now_ts < min_horizon_s:
        return False
    return True


def _parse_iso_to_epoch(end_iso: str) -> Optional[int]:
    if not end_iso:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


# --- Edge model ---

def estimate_edge_pp(
    *, spot_move_pct: float, mid_up: float,
    secs_to_end: int, underlying: str,
) -> tuple[float, str]:
    """Estima edge en probability points (pp absolute) y side a comprar.

    Modelo simple para markets long-horizon: el mid Polymarket implícito
    representa P(YES). Si spot se movió a favor del lado YES (Up) por un
    monto significativo y el mid no lo absorbió todavía, hay edge.

    Heurística:
      - sign de spot_move_pct define side (Up si >0, Down si <0)
      - magnitud absoluta de move se mapea linealmente a un delta de prob
        atenuado por el horizonte (markets más largos absorben menos por
        mover relativo del spot — un +2% no garantiza que en 6 meses el
        target $200k se cumpla, pero sí mueve la prob algunos pp).
      - cap del impact: max 10pp por mover unitario, modulado por el
        horizonte. Para markets cortos (<7 días) el spot move tiene más
        peso; para markets largos (>30 días), menos.
      - el edge final = (impact estimado) - (gap entre prob teórica e mid).

    Devuelve (edge_pp, side_label):
      - edge_pp: en puntos porcentuales absolutos (5.0 = 5pp). Positivo
        si hay edge a favor del side recomendado.
      - side_label: "Up" o "Down" (el side del cual buyear).
    """
    side = "Up" if spot_move_pct >= 0 else "Down"
    abs_move = abs(spot_move_pct)
    # Atenuador por horizonte. 1.0 si <= 7 días, 0.3 si >= 90 días, lineal.
    days_to_end = max(secs_to_end, 0) / 86400.0
    if days_to_end <= 7:
        attenuator = 1.0
    elif days_to_end >= 90:
        attenuator = 0.3
    else:
        # Interpolación lineal entre 7d (1.0) y 90d (0.3).
        attenuator = 1.0 - (days_to_end - 7) * (0.7 / 83.0)
    # Impact: magnitud del move % traducido a pp de prob, max 10pp.
    impact_pp = min(abs_move * 2.0, 10.0) * attenuator
    # El "gap" depende del side. Si side=Up, el mid del YES (mid_up) ya
    # debería reflejar parte del move. El edge real = impact - (cuánto
    # ya está priced-in). Aproximamos el priced-in con un baseline 0.5
    # — si mid_up=0.5 (neutral), todo el impact_pp es edge fresco. Si
    # mid_up=0.7, el market ya descontó algo → edge se reduce.
    if side == "Up":
        priced_in_pp = max(mid_up - 0.5, 0.0) * 100.0
    else:
        priced_in_pp = max(0.5 - mid_up, 0.0) * 100.0
    edge_pp = impact_pp - priced_in_pp
    return edge_pp, side


def compute_implied_prob(mid_up: float, side: str) -> float:
    """Devuelve la probabilidad implícita del side."""
    return float(mid_up) if side == "Up" else float(1.0 - mid_up)


# --- Decision evaluation ---

def evaluate_market(
    market: dict,
    *,
    config: LongHorizonConfig,
    spot_now: Optional[float],
    spot_lookback: Optional[float],
    now_ts: int,
) -> Optional[dict]:
    """Evalúa un market y devuelve dict de decisión (o None si skip).

    Decision dict::

        {
          "action": "buy",
          "side": "Up" | "Down",
          "outcome_index": 0 | 1,
          "underlying": str,
          "entry_mid": float,
          "edge_pp": float,
          "spot_now": float,
          "spot_1h_ago": float,
          "spot_move_pct": float,
          "size_usdc": float,
          "secs_to_end": int,
          "end_ts": int,
          "reason": str,
        }

    Esta función es PURA (sin red, sin DB). Permite testear lógica.
    """
    slug = market.get("slug") or ""
    underlying = parse_underlying(slug)
    if underlying is None:
        return None
    if spot_now is None or spot_lookback is None or spot_lookback <= 0:
        return None
    end_iso = market.get("endDate") or ""
    end_ts = _parse_iso_to_epoch(end_iso) or 0
    secs_to_end = max(end_ts - now_ts, 0)
    if secs_to_end < config.min_horizon_s:
        return None
    # Mid del lado UP (outcome_index 0). gamma devuelve outcomePrices como
    # JSON string o list según endpoint.
    mid_up = _market_mid_up(market.get("outcomePrices"))
    if mid_up is None:
        return None
    spot_move_pct = (spot_now / spot_lookback - 1.0) * 100.0
    edge_pp, side = estimate_edge_pp(
        spot_move_pct=spot_move_pct, mid_up=mid_up,
        secs_to_end=secs_to_end, underlying=underlying,
    )
    if edge_pp < config.min_edge_pct:
        return None
    outcome_index = 0 if side == "Up" else 1
    side_mid = mid_up if outcome_index == 0 else (1.0 - mid_up)
    if side_mid > config.max_mid_target:
        return None
    return {
        "action": "buy",
        "side": side,
        "outcome_index": outcome_index,
        "underlying": underlying,
        "entry_mid": float(side_mid),
        "edge_pp": float(edge_pp),
        "spot_now": float(spot_now),
        "spot_1h_ago": float(spot_lookback),
        "spot_move_pct": float(spot_move_pct),
        "size_usdc": float(config.bet_usdc),
        "secs_to_end": int(secs_to_end),
        "end_ts": int(end_ts),
        "reason": (
            f"spot {underlying} {spot_move_pct:+.2f}% (1h) · "
            f"side={side} mid={side_mid:.3f} edge={edge_pp:.2f}pp "
            f"horizon={secs_to_end // 3600}h"
        ),
    }


def _market_mid_up(outcome_prices: Any) -> Optional[float]:
    """Lee outcomePrices[0] (=YES = Up) tolerando list o JSON string."""
    if not outcome_prices:
        return None
    try:
        if isinstance(outcome_prices, str):
            data = json.loads(outcome_prices)
        else:
            data = outcome_prices
        if isinstance(data, list) and data:
            return float(data[0])
    except Exception:
        return None
    return None


# --- Persistence helpers (importable from tests) ---

def _persist_trade(decision: dict, market: dict, *, opened_at: int) -> Optional[int]:
    """Inserta una row en ``long_horizon_trades`` con status='open' (pre-fill).

    Devuelve el id (autoinc) o None si la inserción falló. La columna
    ``raw`` guarda payload completo (decision + slug) como JSON.
    """
    try:
        from src.db.schema import tx
    except Exception:
        log.exception("long_horizon_arb.persist: schema import failed")
        return None
    raw_payload = {
        "slug": market.get("slug"),
        "condition_id": market.get("conditionId"),
        "decision": {k: v for k, v in decision.items() if k != "raw"},
        "market_liquidity": market.get("liquidity"),
        "market_volume": market.get("volume"),
    }
    try:
        with tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO long_horizon_trades (
                    market_slug, underlying, side, entry_mid,
                    spot_at_entry, spot_1h_ago, spot_move_pct,
                    edge_estimate_pct, bet_usdc, status,
                    opened_at, end_date_market, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    market.get("slug") or "",
                    decision["underlying"],
                    decision["side"],
                    float(decision["entry_mid"]),
                    float(decision["spot_now"]),
                    float(decision["spot_1h_ago"]),
                    float(decision["spot_move_pct"]),
                    float(decision["edge_pp"]),
                    float(decision["size_usdc"]),
                    int(opened_at),
                    int(decision["end_ts"]),
                    json.dumps(raw_payload, default=str),
                ),
            )
            row_id = cur.lastrowid if hasattr(cur, "lastrowid") else None
            return row_id
    except Exception:
        log.exception("long_horizon_arb.persist insert failed")
        return None


def _update_trade(
    trade_id: Optional[int],
    *,
    status: Optional[str] = None,
    fields: Optional[dict] = None,
) -> None:
    """UPDATE long_horizon_trades — fields whitelisted."""
    if trade_id is None:
        return
    allowed = {"order_id", "fill_price", "status", "pnl_usdc", "closed_at"}
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return
    params.append(int(trade_id))
    sql = f"UPDATE long_horizon_trades SET {', '.join(sets)} WHERE id = ?"
    try:
        from src.db.schema import tx
        with tx() as conn:
            conn.execute(sql, tuple(params))
    except Exception:
        log.exception("long_horizon_arb.update failed id=%s", trade_id)


# --- Order executors ---

async def _paper_order_executor(signal: dict) -> dict:
    """Executor "paper": simula post limit GTC sin red.

    Devolvemos ``filled=True`` con fill_price = entry_mid (paper "perfecto"
    de short-circuit — el mid es la mejor estimación pre-fill). El paper
    realista que descuenta orderbook lo hace tradebook.open_position
    cuando se enchufe vía ``LongHorizonArb.run_loop`` el path tradebook.
    """
    return {
        "ok": True,
        "order_id": f"PAPER-LH-{signal['underlying']}-{int(time.time())}",
        "filled": True,
        "fill_price": float(signal["entry_mid"]),
        "error": None,
        "raw": {"mode": "paper"},
    }


async def _live_order_executor(signal: dict) -> dict:
    """Executor "live": stub — la integración real con CLOB requiere
    resolver token_id desde el market. El mid_resolver del live debe
    entregar token_id junto al mid, o el caller del run_loop debe
    pasarlo al signal. Para evitar acoplamiento aquí, devolvemos
    failed con motivo claro (mismo patrón que spike_arb).
    """
    return {
        "ok": False,
        "order_id": None,
        "filled": False,
        "fill_price": None,
        "error": "live executor pending market token_id wiring",
        "raw": None,
    }


# --- Settlement helper ---

def settlement_action(
    *, current_mid: float, entry_mid: float, market_closed: bool,
    payout: Optional[float], early_exit_gain_pct: float,
) -> Optional[dict]:
    """Decide el siguiente paso para una posición open.

    Devuelve dict::

        {"action": "settle"|"early_exit"|"hold", "reason": str,
         "pnl_usdc_factor": float | None}

    - "settle": el market resolvió (closed=True con payout 0/1).
    - "early_exit": current_mid >= entry_mid * (1 + early_exit_gain_pct).
    - "hold": ninguna de las anteriores.

    ``pnl_usdc_factor`` es la fracción de gain/loss respecto al bet:
    +1.0 si full win (payout=1), -1.0 si full loss, etc. None si hold.
    """
    if market_closed and payout is not None:
        # PnL factor: si payout=1 (gana), gain = (1-entry)/entry.
        # Si payout=0, loss = -1.
        if entry_mid > 0:
            factor = (payout - entry_mid) / entry_mid
        else:
            factor = 0.0
        return {
            "action": "settle",
            "reason": f"market resolved payout={payout}",
            "pnl_usdc_factor": factor,
        }
    if entry_mid > 0 and current_mid >= entry_mid * (1.0 + early_exit_gain_pct):
        factor = (current_mid - entry_mid) / entry_mid
        return {
            "action": "early_exit",
            "reason": (
                f"mid {entry_mid:.3f}→{current_mid:.3f} "
                f"(+{factor * 100:.1f}%) ≥ early_exit_gain"
            ),
            "pnl_usdc_factor": factor,
        }
    return {"action": "hold", "reason": "neither resolved nor +50%",
            "pnl_usdc_factor": None}


# --- Core: LongHorizonArb (testable, no red) ---

class LongHorizonArb:
    """Bot long-horizon. Pulleo de markets + decision + execute via callable.

    Diseño testeable:
    - ``markets_provider``: callable que devuelve list de markets crypto
      filtrables. En test pasa lista mock; en prod, gamma client.
    - ``spot_provider``: callable (symbol, ts_seconds) → price_or_None.
      En test pasa función simple; en prod, history del Binance WS.
    - ``order_executor``: dispara la orden (mockeable).
    """

    def __init__(
        self,
        config: Optional[LongHorizonConfig] = None,
        *,
        markets_provider: Optional[Callable[[], Awaitable[list[dict]]]] = None,
        spot_provider: Optional[SpotProvider] = None,
        order_executor: Optional[OrderExecutor] = None,
    ) -> None:
        self.config = config or LongHorizonConfig()
        self._markets_provider = markets_provider
        self._spot_provider = spot_provider
        self._order_executor = order_executor
        self.metrics = _Metrics()

    async def cycle(self) -> list[dict]:
        """Una pasada del loop: pull markets → evaluate → execute.

        Devuelve lista de dicts con resultado de cada decisión disparada
        (testable). Los skips no aparecen en la lista — sólo en metrics.
        """
        self.metrics.cycles += 1
        if self._markets_provider is None:
            log.debug("long_horizon_arb.cycle: no markets_provider configured")
            return []
        try:
            markets = await self._markets_provider()
        except Exception:
            log.exception("long_horizon_arb.cycle: markets_provider failed")
            return []
        self.metrics.markets_seen += len(markets)
        now_ts = int(time.time())
        results: list[dict] = []
        for m in markets:
            if not filter_market(
                m, min_liq=self.config.min_liq_usdc,
                min_horizon_s=self.config.min_horizon_s, now_ts=now_ts,
            ):
                self.metrics.skipped_low_liq += 1
                continue
            self.metrics.markets_filtered += 1
            underlying = parse_underlying(m.get("slug") or "")
            if underlying is None:
                self.metrics.skipped_no_underlying += 1
                continue
            symbol = UNDERLYING_TO_SYMBOL.get(underlying)
            if symbol is None:
                self.metrics.skipped_no_underlying += 1
                continue
            spot_now = None
            spot_back = None
            if self._spot_provider is not None:
                spot_now = self._spot_provider(symbol, now_ts)
                spot_back = self._spot_provider(
                    symbol, now_ts - self.config.spot_lookback_s,
                )
            if spot_now is None or spot_back is None:
                self.metrics.skipped_no_spot += 1
                continue
            decision = evaluate_market(
                m, config=self.config,
                spot_now=spot_now, spot_lookback=spot_back, now_ts=now_ts,
            )
            if decision is None:
                # Fail granular: distinguir overbought vs low_edge se hace
                # dentro de evaluate_market mediante chequeos. Acá sumamos
                # genérico — el detalle quedará en logs.
                self.metrics.skipped_low_edge += 1
                continue
            executed = await self._open_trade(decision, m, now_ts=now_ts)
            if executed is not None:
                results.append(executed)
        return results

    async def _open_trade(
        self, decision: dict, market: dict, *, now_ts: int,
    ) -> Optional[dict]:
        """Persist + execute. Devuelve dict resumen del intento."""
        trade_id = _persist_trade(decision, market, opened_at=now_ts)
        if self._order_executor is None:
            log.info(
                "long_horizon_arb.dry slug=%s side=%s edge=%.2fpp",
                market.get("slug"), decision["side"], decision["edge_pp"],
            )
            return {"trade_id": trade_id, "decision": decision,
                    "executor": None}
        signal = {
            "slug": market.get("slug"),
            "condition_id": market.get("conditionId"),
            "side": decision["side"],
            "outcome_index": decision["outcome_index"],
            "underlying": decision["underlying"],
            "entry_mid": decision["entry_mid"],
            "limit_price": decision["entry_mid"],
            "size_usdc": decision["size_usdc"],
            "ttl_s": self.config.limit_ttl_s,
            "edge_pp": decision["edge_pp"],
        }
        try:
            result = await self._order_executor(signal)
        except Exception as e:
            log.exception(
                "long_horizon_arb.executor raised slug=%s", market.get("slug"),
            )
            self.metrics.orders_failed += 1
            _update_trade(trade_id, status="failed",
                          fields={"closed_at": now_ts})
            return {"trade_id": trade_id, "decision": decision,
                    "result": {"ok": False, "error": str(e)[:200]}}
        if not result or not result.get("ok"):
            self.metrics.orders_failed += 1
            _update_trade(trade_id, status="failed",
                          fields={"closed_at": now_ts})
            return {"trade_id": trade_id, "decision": decision, "result": result}
        order_id = result.get("order_id")
        if result.get("filled"):
            self.metrics.orders_filled += 1
            fill_price = float(
                result.get("fill_price") or decision["entry_mid"]
            )
            _update_trade(
                trade_id, status="filled",
                fields={"order_id": order_id, "fill_price": fill_price},
            )
            log.info(
                "long_horizon_arb.fill id=%s slug=%s side=%s mid=%.3f "
                "fill=%.3f edge=%.2fpp",
                trade_id, market.get("slug"), decision["side"],
                decision["entry_mid"], fill_price, decision["edge_pp"],
            )
        else:
            self.metrics.orders_cancelled_ttl += 1
            _update_trade(
                trade_id, status="cancelled",
                fields={"order_id": order_id, "closed_at": now_ts},
            )
            log.info(
                "long_horizon_arb.ttl_cancel slug=%s side=%s mid=%.3f",
                market.get("slug"), decision["side"], decision["entry_mid"],
            )
        self.metrics.orders_posted += 1
        return {"trade_id": trade_id, "decision": decision, "result": result}


# --- Loop wiring (live) ---

async def long_horizon_loop() -> None:
    """Loop principal del bot long-horizon. Activado por LONG_HORIZON_ENABLED.

    - Crea la tabla on-demand
    - Conecta al WS Binance para spot history
    - Cada CHECK_INTERVAL_S corre un cycle
    """
    config = LongHorizonConfig.from_env()
    if not config.enabled:
        log.info("long_horizon_arb: disabled (LONG_HORIZON_ENABLED!=true)")
        return

    init_table()

    # Importes diferidos (no pagar costo si está disabled).
    from src.binance.websocket import BinanceTickerWS
    from src.config import LIVE_MODE
    from src.polymarket.client import PolymarketClient

    log.info(
        "long_horizon_arb: arrancando — interval=%ss min_liq=$%.0f "
        "min_edge=%.2fpp bet=$%.2f mode=%s",
        config.check_interval_s, config.min_liq_usdc, config.min_edge_pct,
        config.bet_usdc, "live" if LIVE_MODE else "paper",
    )

    # Spot history por símbolo: (ts_ms, price). Cap ~7200 (~2h, 1msg/s).
    spot_history: dict[str, list[tuple[int, float]]] = {
        s: [] for s in config.symbols
    }
    HISTORY_CAP = 7200

    async def _on_tick(symbol: str, price: float, ts_ms: int) -> None:
        h = spot_history.setdefault(symbol, [])
        h.append((ts_ms, price))
        if len(h) > HISTORY_CAP:
            del h[: len(h) - HISTORY_CAP]

    binance_ws = BinanceTickerWS(symbols=config.symbols, on_tick=_on_tick)
    binance_task = asyncio.create_task(
        binance_ws.run(), name="binance-ws-long-horizon",
    )

    def _spot_at(symbol: str, ts_seconds: int) -> Optional[float]:
        h = spot_history.get(symbol) or []
        if not h:
            return None
        target_ms = ts_seconds * 1000
        # Si pedimos ts >= último sample, devolvemos último.
        if target_ms >= h[-1][0]:
            return h[-1][1]
        # Si pedimos ts < primer sample, no tenemos history suficiente.
        if target_ms < h[0][0]:
            return None
        # Linear scan (history es chico, ~7200 entries max).
        best = None
        best_dt = float("inf")
        for ts_ms, p in h:
            dt = abs(ts_ms - target_ms)
            if dt < best_dt:
                best_dt = dt
                best = p
            else:
                # h ordenada → si delta crece, ya pasamos el closest.
                break
        # Toleramos hasta 60s de error en el lookup.
        return best if best_dt <= 60_000 else None

    # Markets provider: pull gamma con filtros.
    async def _list_markets(client) -> list[dict]:
        from datetime import datetime, timezone
        now = int(time.time())
        # Pull markets con liquidity desc, closed=false, end_date >= +1d.
        iso_min = datetime.fromtimestamp(
            now + config.min_horizon_s, tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[dict] = []
        try:
            async for m in client.iter_markets(
                page_size=500, closed=False, end_date_min=iso_min,
            ):
                # Filtro client-side por slug (gamma no soporta regex).
                slug = (m.get("slug") or "").lower()
                if parse_underlying(slug) is None:
                    continue
                out.append(m)
                if len(out) >= 200:
                    break
        except Exception:
            log.exception("long_horizon_arb.list_markets failed")
        return out

    # Espera inicial: necesitamos ~1h de history para spot_lookback.
    # En vez de bloquear 1h, arrancamos sin spot_back y dejamos que
    # los primeros ciclos sean no-ops por skipped_no_spot.
    log.info(
        "long_horizon_arb: warming up — spot history necesita "
        "%ss para edge calc",
        config.spot_lookback_s,
    )

    try:
        async with PolymarketClient() as client:
            executor = _live_order_executor if LIVE_MODE else _paper_order_executor
            arb = LongHorizonArb(
                config=config,
                markets_provider=lambda: _list_markets(client),
                spot_provider=_spot_at,
                order_executor=executor,
            )
            HEARTBEAT_EVERY = max(int(3600 / config.check_interval_s), 1)
            while True:
                try:
                    await asyncio.wait_for(arb.cycle(), timeout=120)
                except asyncio.TimeoutError:
                    log.warning("long_horizon_arb.cycle timeout — sigo")
                except Exception:
                    log.exception("long_horizon_arb.cycle error")
                if arb.metrics.cycles % HEARTBEAT_EVERY == 0:
                    log.info(
                        "long_horizon_arb.heartbeat %s",
                        arb.metrics.snapshot(),
                    )
                await asyncio.sleep(config.check_interval_s)
    finally:
        binance_ws.stop()
        binance_task.cancel()
        try:
            await binance_task
        except (asyncio.CancelledError, Exception):
            pass
        log.info("long_horizon_arb: loop terminado")
