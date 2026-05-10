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

    TODO(market_maker): integrar con `src.polymarket.clob_client`. Hoy el
    helper `place_market_order` solo soporta MARKET / LIMIT_FOK (ambos
    IOC). Para MM necesitamos GTC: la orden se queda en el book hasta
    que matchee o la cancelemos.

    En LIMIT_FOK la orden se cancela atómicamente si no fillea 100% al
    instante → useless para MM. Hay que extender clob_client con
    `OrderType.GTC` y devolver el `order_id` para tracking.

    Hasta entonces este stub devuelve un fake order_id determinístico.
    """
    fake_id = f"STUB-{token_id[:10]}-{side}-{int(price * 10000)}"
    log.debug(
        "[MM-STUB] place_limit_order token=%s.. side=%s price=%.4f size=%.2f → %s",
        token_id[:12], side, price, size_usdc, fake_id,
    )
    return LimitOrderResult(ok=True, order_id=fake_id)


def cancel_order(order_id: str) -> bool:
    """Cancela una limit order viva.

    TODO(market_maker): integrar `client.cancel_order(order_id)` del SDK
    py-clob-client-v2. El SDK lo soporta — solo hay que wrappearlo con
    el manejo de error estándar (return bool, log on failure).
    """
    log.debug("[MM-STUB] cancel_order id=%s", order_id)
    return True


def get_open_orders() -> list[dict]:
    """Lista órdenes vivas del CLOB para el funder de la cuenta.

    TODO(market_maker): integrar `client.get_orders()` del SDK. Devuelve
    items {orderId, market, side, price, size, status}. Por ahora
    leemos de la tabla `mm_orders` con status='open' como fuente de verdad
    interna — está bien para el skeleton, pero en producción hay que
    reconciliar contra el server (la orden puede haber sido cancelada
    en el book sin que nuestro DB lo sepa).
    """
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

    TODO(market_maker): integrar polling de `data-api/trades?user=funder`
    o el WS `activity:trades` filtrado por funder address. Los fills
    contra nuestras limit orders aparecen ahí con counterparty=funder.

    Por ahora devuelve []: el caller debe asumir que el fill detection
    no está conectado y los reconciles vienen via la tabla `mm_orders`
    cuando manualmente se marca status='filled'. NO usar en producción.
    """
    log.debug("[MM-STUB] get_fills_since since=%d → []", since_ts)
    return []


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

    def _default_list_candidates(self) -> list[dict]:
        """Lee de tabla `markets` los buckets que cumplen filtros.

        TODO(market_maker): hoy la tabla `markets` no guarda `vol_24h` ni
        token_id_yes. Hay que enriquecer con el data-api o cachear desde
        Gamma. Por ahora devuelve [] como fail-safe.
        """
        return []

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
        candidates = self._list_candidates_fn()
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

        return {"n_settled": n_settled, "pnl_usdc_total": pnl_total}


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
