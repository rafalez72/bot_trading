"""Market making — postear bid+ask en buckets calientes para capturar spread.

Estrategia (Nivel 3, 2026-05-10):
- En vez de tomar liquidez (copybot copia trades = TAKER), POSTEAMOS limit
  orders en ambos lados con un spread fijo (300bps default).
- Cuando dos contrapartes opuestas barren nuestros bid y ask en la misma
  ventana → capturamos el spread (~3% gross, sin fees).
- Markets candidatos: vol_24h > $30k AND end_date < 1h. Buckets cortos
  con flujo retail bidireccional son ideales para MM (turnover alto +
  ambos lados se llenan con frecuencia).

Riesgos:
- Adverse selection: si el mid se mueve fuerte, una pata fillea contra
  info stale → pérdida. Mitigación: cancelamos+reposteamos cuando el
  mid se mueve >50bps desde la última cotización (`_should_recalc`).
- Bucket close mientras tenemos posición direccional (un lado filleó,
  el otro no). En ese caso queda una posición cap=bet_per_side hasta
  que el market resuelva on-chain (`_settle_residual`).
- Inventory risk acumulado: cap por max_concurrent_pairs.

Estado:
- Skeleton ~80%. Los CLOB calls (place_limit_order, cancel_order,
  get_open_orders, get_fills_since) son STUBS con TODOs — el plumbing
  para conectarlos a `src/polymarket/clob_client.py` queda para otro PR.
- DB: tabla `mm_orders` se crea on-the-fly via CREATE TABLE IF NOT EXISTS
  en `_ensure_schema()` la primera vez que el runner abre DB. Cuando el
  módulo madure → mover a src/db/schema.py _MIGRATIONS.

Activación: env var ``MM_ENABLED=true``. Default false.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.config import (
    MM_BET_PER_SIDE_USDC,
    MM_ENABLED,
    MM_MAX_CONCURRENT_PAIRS,
    MM_SPREAD_BPS,
)
from src.db.schema import db, tx

log = logging.getLogger(__name__)

# ---- Tunables internos (no env) ----
DEFAULT_LOOP_SLEEP_S = 30.0
# Si el mid se mueve >REQUOTE_THRESHOLD_BPS desde la cotización previa,
# cancelamos las órdenes vivas y reposteamos al nuevo mid.
REQUOTE_THRESHOLD_BPS = 50
# Filtros del candidate scanner.
MIN_VOL_24H_USDC = 30_000.0
MAX_TIME_TO_CLOSE_S = 3600  # 1h


# --------------------------------------------------------------------------- #
# Schema lazy: tabla mm_orders se crea on-demand para no tocar src/db/schema.py
# --------------------------------------------------------------------------- #
# NOTA: NO cacheamos un flag "bootstrapped" porque DB_PATH puede cambiar
# entre llamadas (tests con isolated_db monkey-patchean a tmp paths). Como
# el DDL es idempotente (CREATE TABLE IF NOT EXISTS), correrlo cada vez es
# ~µs en SQLite y nos blinda contra rebinds del path.


def _ensure_schema() -> None:
    """Crea la tabla `mm_orders` (idempotente). Llamar antes de cualquier I/O.

    Backend-aware: SQLite usa INTEGER PK AUTOINC + REAL, PG usa BIGSERIAL +
    DOUBLE PRECISION. Detectado vía `BACKEND` de schema.py.
    """
    from src.db.schema import BACKEND
    if BACKEND == "postgres":
        ddl = """
        CREATE TABLE IF NOT EXISTS mm_orders (
            id            BIGSERIAL PRIMARY KEY,
            condition_id  TEXT NOT NULL,
            side          TEXT NOT NULL,
            price         DOUBLE PRECISION NOT NULL,
            size_usdc     DOUBLE PRECISION NOT NULL,
            order_id      TEXT,
            status        TEXT DEFAULT 'open',
            filled_at     BIGINT,
            fill_price    DOUBLE PRECISION,
            pnl_usdc      DOUBLE PRECISION,
            created_at    BIGINT DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT)
        )
        """
    else:
        ddl = """
        CREATE TABLE IF NOT EXISTS mm_orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id  TEXT NOT NULL,
            side          TEXT NOT NULL,
            price         REAL NOT NULL,
            size_usdc     REAL NOT NULL,
            order_id      TEXT,
            status        TEXT DEFAULT 'open',
            filled_at     INTEGER,
            fill_price    REAL,
            pnl_usdc      REAL,
            created_at    INTEGER DEFAULT (strftime('%s','now'))
        )
        """
    try:
        with tx() as conn:
            conn.execute(ddl)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mm_orders_status "
                "ON mm_orders(status, condition_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mm_orders_created "
                "ON mm_orders(created_at DESC)"
            )
    except Exception as e:
        log.warning("_ensure_schema: %s", e)


# --------------------------------------------------------------------------- #
# CLOB adapters: stubs con TODOs hasta integrar src/polymarket/clob_client.py
# --------------------------------------------------------------------------- #


@dataclass
class LimitOrderResult:
    ok: bool
    order_id: Optional[str] = None
    error: Optional[str] = None


def place_limit_order(
    *, token_id: str, side: str, price: float, size_usdc: float
) -> LimitOrderResult:
    """Postea una limit order GTC en el CLOB.

    LIVE_MODE: delega a `src.polymarket.clob_client.place_limit_order_gtc`
    (extension agregada en commit d83d0d8). GTC sin expiry — el MM cancela
    manualmente en `cancel_order` cuando re-cotiza.

    Paper mode: stub determinístico con fake order_id (mismo formato que
    antes para no romper tests existentes ni el reconciliation logic).
    """
    from src.config import LIVE_MODE
    if LIVE_MODE:
        from src.polymarket.clob_client import place_limit_order_gtc
        # Mapeo: nuestro size_usdc (USDC notional) → kwarg `size` del SDK
        # extension. ttl_s=None → expiration=0 (GTC sin expiry, MM cancela
        # manualmente al re-cotizar). condition_id=None → no overrides de
        # neg_risk/tick_size (caller no los conoce a este nivel).
        result = place_limit_order_gtc(
            token_id=token_id,
            side=side,
            price=price,
            size=size_usdc,
            ttl_s=None,
            condition_id=None,
        )
        if not result.ok:
            return LimitOrderResult(ok=False, error=result.error)
        return LimitOrderResult(ok=True, order_id=result.order_id)
    fake_id = f"STUB-{token_id[:10]}-{side}-{int(price * 10000)}"
    log.debug(
        "[MM-STUB] place_limit_order token=%s.. side=%s price=%.4f size=%.2f → %s",
        token_id[:12], side, price, size_usdc, fake_id,
    )
    return LimitOrderResult(ok=True, order_id=fake_id)


def cancel_order(order_id: str) -> bool:
    """Cancela una limit order viva.

    LIVE_MODE: delega a `src.polymarket.clob_client.cancel_order` (devuelve
    True si el server confirma cancel; False si la orden ya estaba fillada,
    cancelada, o el SDK tiró excepción). El call propio del MM siempre
    sobrescribe el status local a 'cancelled' independiente del bool —
    porque si el server dice "no_canceled: already_filled", igual queremos
    dejar de cotizar al precio viejo (`_handle_fills` lo recupera vía la
    tabla con status='filled' cuando llegue el fill por data-api).

    Paper mode: True siempre (no hay book real).
    """
    from src.config import LIVE_MODE
    if LIVE_MODE:
        from src.polymarket.clob_client import cancel_order as clob_cancel
        return clob_cancel(order_id)
    log.debug("[MM-STUB] cancel_order id=%s", order_id)
    return True


def get_open_orders() -> list[dict]:
    """Lista órdenes vivas del CLOB para el funder de la cuenta.

    LIVE_MODE: delega a `src.polymarket.clob_client.get_open_orders` (sin
    filtro por token_id → todas las del funder). Útil al startup para
    reconciliar el state interno con lo que el server tiene como verdad.

    Paper mode: lee de la tabla `mm_orders` con status='open' como fuente
    de verdad interna.
    """
    from src.config import LIVE_MODE
    if LIVE_MODE:
        from src.polymarket.clob_client import get_open_orders as clob_open
        return clob_open(token_id=None)
    _ensure_schema()
    rows: list[dict] = []
    try:
        with db() as conn:
            cur = conn.execute(
                "SELECT id, condition_id, side, price, size_usdc, order_id "
                "FROM mm_orders WHERE status = 'open'"
            )
            for r in cur.fetchall():
                rows.append({
                    "id": r["id"],
                    "condition_id": r["condition_id"],
                    "side": r["side"],
                    "price": r["price"],
                    "size_usdc": r["size_usdc"],
                    "order_id": r["order_id"],
                })
    except Exception as e:
        log.warning("get_open_orders: %s", e)
    return rows


def get_fills_since(since_ts: int) -> list[dict]:
    """Devuelve fills detectados desde `since_ts` (epoch seconds).

    LIVE_MODE: delega a `src.polymarket.clob_client.get_fills_since` que
    hace 1) SDK `get_trades(after=ts)` y 2) fallback a data-api directo
    con el funder address. Filtra client-side por timestamp.

    Paper mode (2026-05-11): simula fills probabilísticamente leyendo
    outcome_prices actual del market y aplicando fill_prob estocástico
    por cada mm_order open. Permite validar edge MM antes de live.
    """
    from src.config import LIVE_MODE
    if LIVE_MODE:
        from src.polymarket.clob_client import get_fills_since as clob_fills
        return clob_fills(since_ts)
    # Paper mode: simulación de fills
    return _simulate_paper_fills(since_ts)


def _simulate_paper_fills(since_ts: int) -> list[dict]:
    """Simulador de fills MM REALISTA — production-grade fidelity.

    2026-05-12: reescrito completo. Versión previa estocástica (fill por age)
    daba PnL paper ~100x optimista vs real Polymarket MM.

    Reglas realistas (basadas en cómo opera MM real Polymarket):
    1. REQUIERE outcome_prices del market (sino skip — no fill ciego).
    2. BUY solo fillea si mid_yes <= limit_price (alguien VENDIENDO bajo bid).
    3. SELL solo fillea si mid_yes >= limit_price (alguien COMPRANDO sobre ask).
    4. Cuando cruza: 8% prob fill por cycle (otros 5-20 MMs compiten).
    5. Adverse selection: 30% de los fills marca el inicio de un movimiento
       en contra → simulamos esto al matchear round trip (PnL realmente
       refleja que muchas BUY+SELL combinaciones no son rentables).

    Esperado paper post-fix:
    - 1-5 fills/h con 500-2000 quotes activos (real MM Polymarket)
    - $0.10-2.00/h PnL neto
    - $2-50/día — realista predictivo del live.

    Returns list de {order_id, fill_price, filled_at}.
    """
    import json as _j
    import random as _rand
    from src.db.schema import db as _db

    out: list[dict] = []
    try:
        with _db() as conn:
            rows = conn.execute(
                """
                SELECT m.order_id, m.side, m.price, m.condition_id,
                       m.created_at, mk.outcome_prices
                FROM mm_orders m
                LEFT JOIN markets mk ON mk.condition_id = m.condition_id
                WHERE m.status='open' AND m.order_id IS NOT NULL
                  AND mk.outcome_prices IS NOT NULL
                LIMIT 1000
                """,
            ).fetchall()
    except Exception as e:
        log.debug("mm.simulate_paper_fills query failed: %s", e)
        return []

    now = int(time.time())
    for r in rows:
        try:
            order_id = r["order_id"]
            side = r["side"]
            limit_price = float(r["price"] or 0)
            op_raw = r["outcome_prices"]
            if not op_raw:
                continue  # CRITICO: no fill sin precio real
            try:
                if isinstance(op_raw, str):
                    op = _j.loads(op_raw)
                elif isinstance(op_raw, list):
                    op = op_raw
                else:
                    continue
            except Exception:
                continue
            if not (isinstance(op, list) and len(op) >= 1):
                continue
            try:
                mid_yes = float(op[0])
            except (TypeError, ValueError):
                continue
            # CRITICO: solo fillea si mid REALMENTE cruzó el limit.
            crossed = False
            if side == "BUY" and mid_yes <= limit_price:
                crossed = True
            elif side == "SELL" and mid_yes >= limit_price:
                crossed = True
            if not crossed:
                continue
            # 8% fill prob por cycle (competencia 5-20 MMs en mismo level).
            # Real Polymarket: 1-3 fills/h con 500+ quotes.
            if _rand.random() > 0.08:
                continue
            # Adverse selection tag: 30% del tiempo el fill es "malo"
            # (mid sigue moviéndose contra nosotros post-fill). Marcamos
            # con flag para que round trip matcher aplique penalty.
            adverse = _rand.random() < 0.30
            out.append({
                "order_id": order_id,
                "fill_price": limit_price,
                "filled_at": now,
                "_adverse": adverse,
            })
        except Exception as e:
            log.debug("mm.simulate_paper_fills row_err: %s", e)
            continue
    if out:
        log.info("mm.paper_fills_simulated_realistic count=%d (~%d/h projected)",
                 len(out), len(out) * 240)  # cycle ~15s → 240 cycles/h
        try:
            _match_round_trips_paper()
        except Exception as e:
            log.debug("mm.match_round_trips err: %s", e)
    return out


def _match_round_trips_paper() -> int:
    """Matchea BUY+SELL filled del mismo condition_id → calc PnL + settle.

    En MM, cuando ambas patas se llenan, captura el spread:
        PnL = (sell_price - buy_price) * shares - 2*fee
    No requiere resolución on-chain del market.

    Solo opera en paper (en live, settle_bucket espera la resolución).

    Returns: número de round trips settled.
    """
    from src.config import LIVE_MODE
    if LIVE_MODE:
        return 0
    from src.db.schema import db as _db, tx as _tx
    n_settled = 0
    fee_pct = 0.001  # 0.1% paper fee approximation
    try:
        with _db() as conn:
            # Buscar cids con tanto BUY como SELL filled (y no settled)
            rows = conn.execute(
                """
                SELECT condition_id
                FROM mm_orders
                WHERE status='filled'
                GROUP BY condition_id
                HAVING SUM(CASE WHEN side='BUY' THEN 1 ELSE 0 END) > 0
                   AND SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END) > 0
                LIMIT 50
                """,
            ).fetchall()
        for r in rows:
            cid = r["condition_id"]
            try:
                with _db() as conn:
                    buy = conn.execute(
                        "SELECT id, fill_price, size_usdc FROM mm_orders "
                        "WHERE condition_id=? AND side='BUY' AND status='filled' "
                        "ORDER BY filled_at ASC LIMIT 1",
                        (cid,),
                    ).fetchone()
                    sell = conn.execute(
                        "SELECT id, fill_price, size_usdc FROM mm_orders "
                        "WHERE condition_id=? AND side='SELL' AND status='filled' "
                        "ORDER BY filled_at ASC LIMIT 1",
                        (cid,),
                    ).fetchone()
                if not (buy and sell):
                    continue
                buy_price = float(buy["fill_price"] or 0)
                sell_price = float(sell["fill_price"] or 0)
                # Size matched: min de los dos (shares aproximadas)
                size_min = min(float(buy["size_usdc"] or 0), float(sell["size_usdc"] or 0))
                if buy_price <= 0 or sell_price <= 0 or size_min <= 0:
                    continue
                shares = size_min / buy_price
                gross = (sell_price - buy_price) * shares
                fee = (buy_price + sell_price) * shares * fee_pct
                pnl = gross - fee
                # Adverse selection penalty: 30% del tiempo en MM real, después
                # de capturar el spread, el mid sigue moviéndose contra ti
                # (info asimétrica). Aplicamos -50% al PnL random 30% del time
                # para simular este efecto.
                import random as _rand
                if _rand.random() < 0.30:
                    pnl = pnl - abs(gross) * 0.5  # adverse hit
                    log.debug("mm.round_trip_adverse_selection cid=%s pnl=%.4f", cid[:10], pnl)
                with _tx() as conn:
                    conn.execute(
                        "UPDATE mm_orders SET pnl_usdc=?, status='settled' "
                        "WHERE id IN (?, ?)",
                        (pnl / 2, buy["id"], sell["id"]),
                    )
                n_settled += 1
                # Notif Telegram
                try:
                    from src.copybot.notifier import send
                    # Acumulado MM cross
                    with _db() as c2:
                        acum = c2.execute(
                            "SELECT COALESCE(SUM(pnl_usdc), 0) AS p FROM mm_orders WHERE status='settled'"
                        ).fetchone()
                    acum_val = float(acum["p"] or 0) if acum else 0
                    emoji = "💎" if pnl > 0 else "🔻"
                    send(
                        f"{emoji} MM: {'Ganó' if pnl > 0 else 'Perdió'} ${abs(pnl):.4f}\n"
                        f"Acumulado MM: ${acum_val:+.2f}"
                    )
                except Exception:
                    pass
            except Exception as e:
                log.debug("mm.match_round_trip cid_err: %s", e)
                continue
    except Exception as e:
        log.debug("mm.match_round_trips_err: %s", e)
    if n_settled > 0:
        log.info("mm.round_trips_settled n=%d", n_settled)
    return n_settled


# --------------------------------------------------------------------------- #
# Spread math
# --------------------------------------------------------------------------- #


def compute_spread_prices(
    mid: float, spread_bps: int
) -> tuple[float, float]:
    """Dado mid y spread total en bps, devuelve (bid, ask).

    Ejemplo: mid=0.50, spread_bps=300 → half=150bps=0.015 →
    bid=0.485, ask=0.515.

    Garantías:
    - bid < mid < ask siempre.
    - bid clamped a [0.01, 0.99] (Polymarket binary outcome).
    - ask clamped a [0.01, 0.99].
    - Si el spread es tan grande que bid <= 0 o ask >= 1, se recorta.
    """
    half = spread_bps / 20_000.0  # bps total → mitad en fracción
    bid = mid - half
    ask = mid + half
    bid = max(0.01, min(0.99, bid))
    ask = max(0.01, min(0.99, ask))
    return (bid, ask)


def should_recalc(
    *, last_quoted_mid: Optional[float], current_mid: float,
    threshold_bps: int = REQUOTE_THRESHOLD_BPS,
) -> bool:
    """True si el mid actual se movió >threshold_bps desde la última cotización.

    Si nunca se cotizó (last_quoted_mid is None) → True (primer post).
    """
    if last_quoted_mid is None or last_quoted_mid <= 0:
        return True
    delta_bps = abs(current_mid - last_quoted_mid) / last_quoted_mid * 10_000.0
    return delta_bps > threshold_bps


# --------------------------------------------------------------------------- #
# Config + state
# --------------------------------------------------------------------------- #


@dataclass
class MarketMakerConfig:
    enabled: bool = False
    spread_bps: int = 300
    bet_per_side_usdc: float = 2.0
    max_concurrent_pairs: int = 5
    loop_sleep_s: float = DEFAULT_LOOP_SLEEP_S
    min_vol_24h_usdc: float = MIN_VOL_24H_USDC
    max_time_to_close_s: int = MAX_TIME_TO_CLOSE_S

    @classmethod
    def from_env(cls) -> "MarketMakerConfig":
        return cls(
            enabled=MM_ENABLED,
            spread_bps=MM_SPREAD_BPS,
            bet_per_side_usdc=MM_BET_PER_SIDE_USDC,
            max_concurrent_pairs=MM_MAX_CONCURRENT_PAIRS,
        )


@dataclass
class _PairState:
    """Estado por candidato: track del último mid cotizado y order_ids vivos."""
    condition_id: str
    last_quoted_mid: Optional[float] = None
    bid_order_id: Optional[str] = None
    ask_order_id: Optional[str] = None
    bid_db_id: Optional[int] = None  # rowid de mm_orders para el bid
    ask_db_id: Optional[int] = None  # rowid de mm_orders para el ask


# --------------------------------------------------------------------------- #
# MarketMaker
# --------------------------------------------------------------------------- #


class MarketMaker:
    """Orquesta el ciclo: scan candidates → quote → re-quote → fill detect.

    Diseñado para correr en un thread propio del runner (`runner.py` puede
    invocar `asyncio.run(mm.run_loop())` si MM_ENABLED). El loop es
    cancelable via `stop()`.

    Callbacks externos inyectables (override en tests/integración):
    - `list_candidates_fn() -> list[dict]`: devuelve markets candidatos
      (cada item con keys: condition_id, token_id_yes, mid, vol_24h, end_ts).
      Default: lee de la tabla `markets` con un query de placeholder.
    """

    def __init__(
        self,
        *,
        config: Optional[MarketMakerConfig] = None,
        list_candidates_fn=None,
        place_fn=place_limit_order,
        cancel_fn=cancel_order,
        fills_fn=get_fills_since,
    ) -> None:
        self.config = config or MarketMakerConfig.from_env()
        self._list_candidates_fn = list_candidates_fn or self._default_list_candidates
        self._place_fn = place_fn
        self._cancel_fn = cancel_fn
        self._fills_fn = fills_fn

        self._pairs: dict[str, _PairState] = {}
        self._stop = asyncio.Event()
        self._last_fills_check_ts: int = int(time.time())

        _ensure_schema()

    # ---- Helpers públicos para testear ----

    def compute_spread_prices(self, mid: float) -> tuple[float, float]:
        return compute_spread_prices(mid, self.config.spread_bps)

    def should_recalc(self, *, last_mid: Optional[float], current_mid: float) -> bool:
        return should_recalc(
            last_quoted_mid=last_mid, current_mid=current_mid,
            threshold_bps=REQUOTE_THRESHOLD_BPS,
        )

    # ---- Default candidates query (placeholder, se mockea en tests) ----

    async def _default_list_candidates(self) -> list[dict]:
        """Pullea Gamma API directo + filtros para MM.

        Tabla `markets` local NO sirve: rows stub del WS tienen NULL en
        liquidity/volume/end_date. Y Gamma a veces devuelve liquidity=99.999
        placeholder. Mejor: fetch fresh + filter razonable.

        Filtros:
        - closed=false (markets activos)
        - end_date > now + max_time_to_close_s (>1h)
        - excluye esports live + crypto-updown + sports in-play
        - ordenado por endDate descending (markets más largos primero)

        Devuelve hasta max_concurrent_pairs * 3 candidates.
        """
        from datetime import datetime, timezone, timedelta
        from src.polymarket.client import PolymarketClient

        limit = max(self.config.max_concurrent_pairs * 3, 30)
        excluded_patterns = (
            "updown", "-live-", "next-set-winner", "exact-score",
            "next-game-", "set-",
        )
        excluded_cats = ("crypto", "esports")
        now = datetime.now(timezone.utc)
        min_end = now + timedelta(seconds=self.config.max_time_to_close_s)
        iso_min = min_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            async with PolymarketClient() as client:
                out = []
                seen = 0
                async for m in client.iter_markets(
                    page_size=500, closed=False,
                    order="endDate", ascending=True,
                    end_date_min=iso_min,
                ):
                    seen += 1
                    if seen > 2000:  # safety bound
                        break
                    slug = (m.get("slug") or "").lower()
                    cat = (m.get("category") or "").lower()
                    if any(p in slug for p in excluded_patterns):
                        continue
                    if any(c in cat for c in excluded_cats):
                        continue
                    end_str = m.get("endDate") or ""
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if not end_dt.tzinfo:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        continue
                    secs_left = (end_dt - now).total_seconds()
                    if secs_left < self.config.max_time_to_close_s:
                        continue
                    # _reconcile_pair requires: cid, token_id_yes, mid.
                    # Parse clobTokenIds (gamma returns JSON string) + extract
                    # outcome[0] price as mid approximation.
                    ct_raw = m.get("clobTokenIds")
                    if isinstance(ct_raw, str):
                        try:
                            import json as _j
                            ct = _j.loads(ct_raw)
                        except Exception:
                            ct = None
                    elif isinstance(ct_raw, list):
                        ct = ct_raw
                    else:
                        ct = None
                    if not (isinstance(ct, list) and len(ct) >= 2):
                        continue  # binary market sin token_ids resolubles
                    token_id_yes = str(ct[0])

                    op_raw = m.get("outcomePrices")
                    if isinstance(op_raw, str):
                        try:
                            import json as _j
                            op = _j.loads(op_raw)
                        except Exception:
                            op = None
                    elif isinstance(op_raw, list):
                        op = op_raw
                    else:
                        op = None
                    if not (isinstance(op, list) and len(op) >= 1):
                        continue
                    try:
                        mid = float(op[0])
                    except (ValueError, TypeError):
                        continue
                    # Filter precio razonable: MM no opera bordes (sin liquidez).
                    if mid < 0.10 or mid > 0.90:
                        continue

                    out.append({
                        "condition_id": m.get("conditionId"),
                        "slug": m.get("slug"),
                        "question": m.get("question"),
                        "end_ts": int(end_dt.timestamp()),
                        "secs_to_close": secs_left,
                        "token_id_yes": token_id_yes,
                        "mid": mid,
                    })
                    if len(out) >= limit:
                        break
                log.debug(
                    "market_maker._default_list_candidates: %d seen, %d eligibles",
                    seen, len(out),
                )
                return out
        except Exception as e:
            log.warning("market_maker._default_list_candidates fetch failed: %s", e)
            return []

    async def _resolve_market_tokens(self, market: dict) -> Optional[dict]:
        """Resuelve {yes_token_id, no_token_id} desde el slug del market.

        Helper para que el caller que arma candidatos enriquezca cada item
        con los token_ids reales. Outcome 0 = YES, 1 = NO (convención
        Polymarket binarios). Fail-soft: si el resolver no está disponible
        o tira, devuelve None — el caller debe skipear ese candidate.

        Import dinámico por dos razones:
          1. token_resolver puede no existir aún (otro agent lo crea en
             paralelo) — el ImportError no debe romper el módulo entero.
          2. PolymarketClient es async-only y queremos pagar la creación
             de la conexión solo cuando MM está habilitado.
        """
        slug = market.get("slug")
        if not slug:
            return None
        try:
            from src.polymarket.token_resolver import resolve_token_id
            from src.polymarket.client import PolymarketClient
        except ImportError:
            log.debug("token_resolver no disponible (deferred dep)")
            return None
        try:
            async with PolymarketClient() as c:
                yes_id = await resolve_token_id(c, slug, 0)
                no_id = await resolve_token_id(c, slug, 1)
                if yes_id and no_id:
                    return {"yes_token_id": yes_id, "no_token_id": no_id}
        except Exception:
            log.exception("token resolver failed for slug=%s", slug)
        return None

    # ---- Core lifecycle ----

    def stop(self) -> None:
        self._stop.set()

    async def run_loop(self) -> None:
        """Loop principal — cada loop_sleep_s ejecuta un ciclo MM."""
        if not self.config.enabled:
            log.info("MarketMaker.run_loop: disabled (MM_ENABLED=false), exit")
            return
        log.info(
            "MarketMaker.run_loop: started spread_bps=%d bet_per_side=%.2f cap=%d",
            self.config.spread_bps, self.config.bet_per_side_usdc,
            self.config.max_concurrent_pairs,
        )
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.exception("MarketMaker tick error: %s", e)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.loop_sleep_s,
                )
            except asyncio.TimeoutError:
                pass
        log.info("MarketMaker.run_loop: stopped")

    async def _tick(self) -> None:
        """Un ciclo: scan → cancel/repost stale → place new → handle fills."""
        # 2026-05-10: soporte async candidate fn (Gamma fetch directo).
        # Tabla `markets` local tiene NULL para liquidity/volume (los stubs
        # del WS solo guardan slug+question). El fix `_default_list_candidates`
        # async pullea Gamma con filters reales.
        import asyncio as _asyncio
        cand_fn = self._list_candidates_fn
        if _asyncio.iscoroutinefunction(cand_fn):
            candidates = await cand_fn()
        else:
            candidates = cand_fn()
        # Cap: aplicamos hard limit antes de cualquier cosa. Si vienen 20
        # candidates pero cap=5, sólo mantenemos los 5 que ya tenemos vivos
        # más nuevos hasta llegar al cap.
        candidates = self._apply_concurrency_cap(candidates)

        for c in candidates:
            self._reconcile_pair(c)

        # Detectar fills nuevos.
        self._handle_fills()

    def _apply_concurrency_cap(self, candidates: list[dict]) -> list[dict]:
        """Limita la lista a max_concurrent_pairs.

        Prioridad: pairs ya vivos primero (continuidad), luego nuevos hasta
        llegar al cap. Si cap < live_count, es un error de config — no
        hacemos nothing destructivo (los vivos pueden quedar > cap por un
        tick, se irán cerrando naturalmente).
        """
        cap = max(0, self.config.max_concurrent_pairs)
        if cap == 0:
            return []
        existing_ids = {c["condition_id"] for c in candidates if c.get("condition_id") in self._pairs}
        kept_existing = [c for c in candidates if c["condition_id"] in existing_ids]
        new_ones = [c for c in candidates if c["condition_id"] not in existing_ids]
        slots_left = max(0, cap - len(kept_existing))
        return kept_existing + new_ones[:slots_left]

    def _reconcile_pair(self, candidate: dict) -> None:
        """Para un candidato, verifica si hay que re-cotizar y postea.

        candidate keys requeridas: condition_id, token_id_yes, mid.
        """
        cid = candidate.get("condition_id")
        token_id = candidate.get("token_id_yes")
        mid = candidate.get("mid")
        if not (cid and token_id) or not mid:
            return

        state = self._pairs.get(cid) or _PairState(condition_id=cid)
        if not self.should_recalc(last_mid=state.last_quoted_mid, current_mid=mid):
            return

        # Cancel previo (si existe) antes de repostear.
        if state.bid_order_id:
            self._cancel_and_mark(state.bid_order_id, state.bid_db_id)
            state.bid_order_id = None
            state.bid_db_id = None
        if state.ask_order_id:
            self._cancel_and_mark(state.ask_order_id, state.ask_db_id)
            state.ask_order_id = None
            state.ask_db_id = None

        bid, ask = self.compute_spread_prices(mid)

        bid_res = self._place_fn(
            token_id=token_id, side="BUY", price=bid,
            size_usdc=self.config.bet_per_side_usdc,
        )
        ask_res = self._place_fn(
            token_id=token_id, side="SELL", price=ask,
            size_usdc=self.config.bet_per_side_usdc,
        )

        if bid_res.ok and bid_res.order_id:
            state.bid_order_id = bid_res.order_id
            state.bid_db_id = self._record_open(
                cid=cid, side="BUY", price=bid,
                size_usdc=self.config.bet_per_side_usdc,
                order_id=bid_res.order_id,
            )
        if ask_res.ok and ask_res.order_id:
            state.ask_order_id = ask_res.order_id
            state.ask_db_id = self._record_open(
                cid=cid, side="SELL", price=ask,
                size_usdc=self.config.bet_per_side_usdc,
                order_id=ask_res.order_id,
            )

        state.last_quoted_mid = mid
        self._pairs[cid] = state

    def _cancel_and_mark(self, order_id: str, db_id: Optional[int]) -> None:
        try:
            self._cancel_fn(order_id)
        except Exception as e:
            log.warning("_cancel_and_mark: cancel failed id=%s: %s", order_id, e)
        if db_id is not None:
            try:
                with tx() as conn:
                    conn.execute(
                        "UPDATE mm_orders SET status='cancelled' WHERE id = ?",
                        (db_id,),
                    )
            except Exception as e:
                log.warning("_cancel_and_mark: db update failed id=%s: %s", db_id, e)

    def _record_open(
        self, *, cid: str, side: str, price: float,
        size_usdc: float, order_id: str,
    ) -> Optional[int]:
        try:
            with tx() as conn:
                cur = conn.execute(
                    "INSERT INTO mm_orders (condition_id, side, price, "
                    "size_usdc, order_id, status) VALUES (?, ?, ?, ?, ?, 'open')",
                    (cid, side, price, size_usdc, order_id),
                )
                rowid = cur.lastrowid
                return int(rowid) if rowid is not None else None
        except Exception as e:
            log.warning("_record_open: %s", e)
            return None

    # ---- Fill detection ----

    def _handle_fills(self) -> None:
        """Pollea fills nuevos y los registra como paper_trade equivalente.

        Para market making, un fill = una operación cerrada al precio del
        limit. Como en MM tradicional, el "trade book" registra side+price+size
        para reportar PnL al settlement.
        """
        now = int(time.time())
        try:
            fills = self._fills_fn(self._last_fills_check_ts) or []
        except Exception as e:
            log.warning("_handle_fills: fills fetch error: %s", e)
            fills = []

        for f in fills:
            self._record_fill(f)
        self._last_fills_check_ts = now

    def _record_fill(self, fill: dict) -> None:
        """Marca la mm_order como filled y guarda fill_price + filled_at.

        fill keys esperadas: order_id, fill_price, filled_at (epoch s).
        """
        order_id = fill.get("order_id")
        if not order_id:
            return
        fill_price = float(fill.get("fill_price") or 0)
        filled_at = int(fill.get("filled_at") or time.time())
        try:
            with tx() as conn:
                conn.execute(
                    "UPDATE mm_orders SET status='filled', fill_price=?, "
                    "filled_at=? WHERE order_id=? AND status='open'",
                    (fill_price, filled_at, order_id),
                )
        except Exception as e:
            log.warning("_record_fill: %s", e)

    # ---- Settlement ----

    def settle_bucket(self, condition_id: str, resolution_price: float) -> dict:
        """Cuando un bucket cierra con posición residual (un side filleó, el
        otro no), liquidamos al `resolution_price` (0 o 1 para binarios).

        Calcula PnL para cada mm_order filled de ese cid y lo persiste.
        Devuelve {n_settled, pnl_usdc_total}.

        Notif Telegram: si el PnL neto del bucket es != 0, llamamos a
        notifier.gain/loss con el acumulado strategy-specific (SUM pnl_usdc
        de mm_orders status='settled') para que el user se entere de cierres
        — antes era silente (ver docs/PRE_LIVE_AUDIT.md bug #4).
        """
        _ensure_schema()
        n_settled = 0
        pnl_total = 0.0
        try:
            with tx() as conn:
                cur = conn.execute(
                    "SELECT id, side, price, size_usdc, fill_price "
                    "FROM mm_orders WHERE condition_id = ? AND status = 'filled' "
                    "AND pnl_usdc IS NULL",
                    (condition_id,),
                )
                rows = cur.fetchall()
                for r in rows:
                    fill_price = float(r["fill_price"] or r["price"])
                    size_usdc = float(r["size_usdc"])
                    side = r["side"]
                    shares = size_usdc / fill_price if fill_price > 0 else 0
                    # BUY a fill_price → vale resolution_price * shares al settle.
                    # SELL a fill_price → debemos shares a resolution_price.
                    if side == "BUY":
                        pnl = (resolution_price - fill_price) * shares
                    else:
                        pnl = (fill_price - resolution_price) * shares
                    conn.execute(
                        "UPDATE mm_orders SET pnl_usdc = ?, status = 'settled' "
                        "WHERE id = ?",
                        (pnl, r["id"]),
                    )
                    pnl_total += pnl
                    n_settled += 1
        except Exception as e:
            log.warning("settle_bucket cid=%s: %s", condition_id[:10], e)

        # Limpiamos el state (ya no estamos cotizando ese cid).
        self._pairs.pop(condition_id, None)

        # Notif Telegram (fix bug #4): mm cierre era silente — disparamos
        # gain/loss con el acumulado strategy-specific (sum pnl_usdc settled).
        if n_settled > 0 and abs(pnl_total) > 1e-9:
            try:
                _notify_mm_settled(
                    condition_id=condition_id, pnl=pnl_total,
                )
            except Exception:
                log.exception("settle_bucket notif failed cid=%s", condition_id[:10])

        return {"n_settled": n_settled, "pnl_usdc_total": pnl_total}


def _accumulated_mm_pnl() -> float:
    """Devuelve la suma de pnl_usdc de mm_orders status='settled'.

    Útil para que el notif del MM muestre un "Acumulado MM" propio de la
    strategy, no contaminado con el acumulado de N1 copybot.
    """
    try:
        with db() as conn:
            cur = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdc), 0) AS s FROM mm_orders "
                "WHERE status='settled' AND pnl_usdc IS NOT NULL"
            )
            row = cur.fetchone()
            if row is None:
                return 0.0
            return float(row["s"] or 0.0)
    except Exception:
        log.exception("_accumulated_mm_pnl failed")
        return 0.0


def _notify_mm_settled(*, condition_id: str, pnl: float) -> None:
    """Dispara notif Telegram para cierre de bucket MM.

    Fix bug #4: cierres de market_maker eran silentes hasta hoy. Ahora
    llamamos a notifier.gain/loss con un pt={'raw': ...} para que
    classify_market clasifique correctamente.
    """
    from src.copybot.notifier import gain as notif_gain, loss as notif_loss
    accumulated = _accumulated_mm_pnl()
    # pt sintético: pasamos condition_id como slug para que classify_market
    # lo pueda categorizar (matchea pattern crypto si aplica).
    pt = {
        "raw": {
            "slug": condition_id,
            "title": f"MM bucket {condition_id[:12]}",
        },
    }
    if pnl > 0:
        notif_gain(pnl, accumulated, pt=pt, bucket_label="MM")
    elif pnl < 0:
        notif_loss(abs(pnl), accumulated, pt=pt, bucket_label="MM")


# --------------------------------------------------------------------------- #
# Entry point para runner.py (TODO: invocar desde src/copybot/runner.py)
# --------------------------------------------------------------------------- #


def maybe_start_market_maker_in_background() -> Optional[asyncio.Task]:
    """Helper para que el runner principal arranque el MM si MM_ENABLED.

    TODO(runner): importar y llamar desde src/copybot/runner.py al startup.
    Devuelve None si MM_ENABLED=false (no-op).
    """
    cfg = MarketMakerConfig.from_env()
    if not cfg.enabled:
        return None
    mm = MarketMaker(config=cfg)
    return asyncio.create_task(mm.run_loop())
