# Pre-Live Audit Report

**Fecha:** 2026-05-10
**Trigger:** Live deploy del 2026-05-10 perdió $76 reales (29/50 trades phantom_cleanup silentes en orderbooks thin de crypto-updown-5m).
**Goal:** Identificar TODO bug que pueda causar pérdida real con 7 strategies activas.

---

## Resumen ejecutivo

**NO PASAR A LIVE GENERALIZADO.** Solo N1 copybot está realmente listo. 6/7 strategies tienen blockers — 4 son STUBS funcionales pero sin ejecución CLOB real, 1 es WIREFRAME, y la única realmente live (N2 crypto_arb) está GATED en LIVE_MODE por el bug de orderbook thin.

**Capital allocation cross-strategies está ROTO** — `capital_full` solo cuenta `paper_trades`/`live_trades`. 5 tablas de strategies adicionales NO suman al cap. Riesgo de over-allocation 5×–10× del LIVE_CAPITAL_USDC.

**Kill switch CIEGO a 5 strategies** — solo lee `TRADES_TABLE` (= `paper_trades` o `live_trades`). Si MM/spike/adv/long_horizon/hedge pierden $30 en sus tablas separadas, kill switch dice "todo OK".

**Notif Telegram CIEGAS a 5 strategies** — `gain()/loss()` solo se disparan desde `learning.on_paper_trade_closed`, llamado solo desde paper.py/executor.py + crypto_arb. Cierres de mm/spike/adv/long_horizon/hedge son silentes.

---

## 1. Live executor status por strategy

| Strategy | Live executor | NotImplementedError si LIVE? | CLOB calls reales | Live activable hoy? |
|---|---|---|---|---|
| N1 copybot (ws_bridge → tradebook) | ✓ Real (`executor.py`) | No | place_market_order LIMIT_FOK + slippage check + outbox | **SÍ** (ya validado en deploy del 2026-05-10) |
| N2 crypto_arb | ✓ Real vía `tradebook.open_position` | No | mismo path que N1 + estimate_slippage | GATED por `CRYPTO_ARB_ALLOW_LIVE` (default false). Si LIVE_MODE+ALLOW_LIVE → opera, pero orderbooks thin → garantizada pérdida. |
| market_maker (A) | 🔴 STUB | NO (peor) | `place_limit_order` devuelve fake_id determinístico, `cancel_order` log-only, `get_fills_since` devuelve [] | **SÍ pero NO funciona.** Si MM_ENABLED=true en LIVE → 0 órdenes reales, 0 fills detectados, spread del bot solo se mueve en `mm_orders` table como sintético. |
| spike_arb (B) | 🔴 STUB | No (devuelve `ok=False, error='live executor pending market resolution wiring'`) | place_limit_order_gtc EXISTE en clob_client.py pero no está conectado | NO — todos los signals devuelven failed/cancelled |
| adversarial_asks (C) | 🔴 NotImplementedError | **SÍ** (`raise NotImplementedError("live adversarial asks require splitPosition + GTC orders")`) | NINGUNO — falta `split_position()` + GTC. signal_only=true por default. | NO — explícitamente bloqueado |
| long_horizon_arb (D) | 🔴 STUB | No (devuelve `error='live executor pending market token_id wiring'`) | NINGUNO en path live | NO — todos los signals devuelven failed |
| hedge (crypto_arb_hedge) | ⚠️ MEDIO live: poly via tradebook + perp REAL via BinancePerpClient | No | poly path = real (tradebook.open_position) + perp = real Binance market order | **PARCIAL.** El loop principal (`crypto_arb_hedge_loop`) es WIREFRAME — NO detecta signals todavía. Si HEDGE_ENABLED=true → solo cycles vacíos. Pero si alguien lo wirea y dispara `evaluate_and_open` directo → SÍ ejecuta plata real (poly + binance perp short con leverage). |

### 🔴 BLOQUEADORES live

- **MM stubs no levantan exception**: si user pone LIVE_MODE+MM_ENABLED creyendo que va a funcionar, el bot consumirá DB rows y heartbeat sin tocar el CLOB. Silent failure peor que crash. **Fix: agregar `if LIVE_MODE: raise NotImplementedError("market_maker live not wired yet")` en `place_limit_order` o al startup de `MarketMaker.run_loop()`.**
- **spike_arb / long_horizon stubs**: idem — devuelven `ok=False` pero no hay log claro de "live no funciona, solo paper". Riesgo: user piensa que bot opera live y "no detecta nada" porque cada signal devuelve failed. **Fix: log WARNING al startup `if LIVE_MODE: log.warning("spike_arb live executor STUB — todos signals fallan hasta wirear")`.**
- **hedge wireframe**: `crypto_arb_hedge_loop` solo hace `await asyncio.sleep` sin detectar signals. **Fix: documentar explícitamente "HEDGE_ENABLED=true es no-op hasta wirear el detector"** o devolver al startup.

---

## 2. Capital allocation cross-strategies

🔴 **BLOQUEADOR: capital_full check NO cruza tablas**

`validation.run_pre_open_checks` (línea 411-418) hace:
```sql
SELECT SUM(entry_size_usdc) FROM {ctx.trades_table} WHERE status='open'
```
con `trades_table` = `paper_trades` o `live_trades`. **Las otras 5 strategies tienen tablas separadas:**
- `mm_orders` (market_maker)
- `spike_arb_trades` (spike_arb)
- `adversarial_orders` (adversarial_asks)
- `long_horizon_trades` (long_horizon_arb)
- `hedge_trades` (crypto_arb_hedge — pero su pata poly SÍ entra en live_trades vía tradebook.open_position, así que parcial)

### Escenario over-allocation real

LIVE_CAPITAL_USDC=$50, todas las strategies activas en LIVE:
- N1 copy: $7 × 5 wallets = $35 → live_trades open=$35 (capital_full ve esto)
- crypto_arb: $5 × 5 buckets = $25 → live_trades open=$60 → bloqueado (✓)
- Pero si hedge abre con `tradebook.open_position` también suma a live_trades (✓ al menos parcial)
- spike_arb $3 × 10 fills (si funcionara) = $30 → spike_arb_trades open=$30 → NO suma a capital_full → bot abre AUNQUE live_trades ya esté en $60

**Worst case si todas funcionaran**: $35 (N1) + $25 (N2) + $30 (spike) + $20 (adv) + $50 (long_horizon) + $50 (hedge poly leg) = **$210 vs $50 cap**. 4× over-allocation.

### ⚠️ Riesgo medio: capital compartido entre strategies

Cada strategy lee su propio `*_BET_USDC` independiente. No hay un budget orchestrator que reserve capital per strategy. Si 3 strategies pedean $20 simultáneos en el mismo cycle → todas pasan el check porque ninguna ve a las otras.

### Fix obligatorio

Antes de live multi-strategy:
1. Agregar `total_open_across_strategies(trades_table_list)` helper que sume entry_size_usdc de **TODAS** las tablas con status='open'.
2. Modificar `run_pre_open_checks` (capital_full) para usar este helper.
3. O alternativa: budget per-strategy hardcap (`MM_CAP_USDC`, `SPIKE_CAP_USDC`, etc.) que cada strategy chequee localmente — más granular pero más config.

---

## 3. Kill switch interaction

🔴 **BLOQUEADOR: kill switch ciego a 5 strategies**

`risk.py` línea 37: `from src.copybot.tradebook import TABLE as TRADES_TABLE`. Todos los queries del kill switch (3 layers + legacy) hacen:
```sql
SELECT SUM(pnl_usdc), COUNT(*) FROM {TRADES_TABLE} WHERE ...
```
TRADES_TABLE = `paper_trades` o `live_trades` SOLO.

### Implicaciones

- mm_orders pnl_usdc, spike_arb_trades pnl_usdc, hedge_trades pnl_total_usdc, long_horizon_trades pnl_usdc, adversarial_orders pnl → **NO cuentan**.
- Si MM pierde $30 en un día por adverse selection y N1 gana $5 → `paper_trades.pnl_usdc=+$5` → kill switch dice "+$5, todo OK", aunque el net real sea -$25.
- consecutive_losses idem: 5 LOSS seguidos en spike_arb_trades NO disparan el layer.
- drawdown layer idem: peak balance solo refleja realized en live_trades.

### Caso real de hoy (2026-05-10)

Live perdió $76 en 50 trades. 29 fueron `phantom_cleanup` (pnl=$0 marcado por executor.py:905). Solo 21 trades dejaron `closed_loss` con pnl_usdc real → kill switch vio solo ~$30 de pérdida realized → no disparó layer1 ($10 cap UTC) hasta tarde.

### Fix obligatorio

Una de:
1. **Recomendado**: `_TRADE_TABLES = ("live_trades", "mm_orders", "spike_arb_trades", "long_horizon_trades", "hedge_trades", "adversarial_orders")` y agregar UNION ALL de pnl_usdc en `_check_daily_loss_cap`, `_check_consecutive_losses`, `_check_drawdown`. Cuidado: cada tabla tiene columna pnl distinta (mm_orders.pnl_usdc, hedge_trades.pnl_total_usdc, etc.) → necesita normalización.
2. Alternativa más simple: `bot_state.kill_switch_strategy_pnl` actualizado por cada strategy en sus close hooks. Layer lee desde ahí.

### ⚠️ phantom_cleanup oculta pérdida real

Hoy mismo: 29 trades cerrados con `pnl_usdc=0` y `exit_reason='phantom_cleanup'` (executor.py:905). Esos $58 de capital atado se "perdieron" porque el bot fue redimido manualmente fuera del bot — pero el bot grabó pnl=0. Esto **camufla** la pérdida real al kill switch.

**Fix**: phantom_cleanup debería estimar la pérdida vía `entry_price - last_seen_mid` o flagear como "external_loss_unknown" con warning explícito al user.

---

## 4. PnL accounting cross-tables

🔴 **BLOQUEADOR: Acumulado Telegram solo cuenta una tabla**

`learning.on_paper_trade_closed` (línea 244-251):
```sql
SELECT SUM(pnl_usdc) FROM {TRADES_TABLE_NOTIF}  -- = paper_trades o live_trades
```
**Nada del PnL de las otras 5 strategies entra al "Acumulado".**

### Daily summary también ciego

`learning.summary()` (línea 552, 565, 590-601) solo lee `paper_trades`. Daily summary Telegram → `_maybe_send_daily_summary` (runner.py:271-310) — el path de LIVE sí lee `live_trades` (líneas 282-294), pero **ninguno de los dos lee mm_orders/spike/hedge/long_horizon/adv**.

### Caso net cross-strategy

mm_orders gana $20 (spread captured) + hedge_trades pierde $30 (perp slippage) = net -$10.
- Telegram Acumulado dice: $0 (solo cuenta paper/live).
- Daily summary dice: $0.
- Kill switch dice: $0.

### Fix obligatorio

`learning.summary()` debe agregar campos `strategies.{mm, spike, adv, long_horizon, hedge}.pnl_usdc` y el daily summary los debe consolidar al `pnl_today` total. Idem el "Acumulado" de gain/loss.

---

## 5. Telegram notif coverage

🔴 **BLOQUEADOR: 5 strategies cierran SILENTAMENTE**

`gain()/loss()` solo se llaman desde `learning.on_paper_trade_closed` (líneas 226-267). Esa función se invoca:
- `paper.py` close_position / settle_resolved (✓ N1 paper)
- `executor.py` no la llama directo (live tiene su propio `live_close` notif que sí está conectado a `force_close` y `settle_resolved` ✓)
- `crypto_arb._settle_crypto_arb_resolved` línea 671-675 (✓)

**NO la llaman:**
- `market_maker.settle_bucket` (línea 541-584) — ningún notif
- `spike_arb` (no settle implementado todavía con notif)
- `adversarial_asks.update_settlement` (línea 251-275) — ningún notif
- `long_horizon_arb` (early_exit + settle sin notif call)
- `crypto_arb_hedge.close_perp_for_hedge` (línea 628-701) — solo metrics, NINGÚN notif

### Caso silente del 2026-05-10 reproducible

Hoy phantom_cleanup grabó 29 trades sin notif (FIX aplicado en commit por executor.py:933-955 → `live_phantom()` con summary >5). Pero **el mismo patrón sigue para mm/spike/adv/long_horizon/hedge close hooks**.

### Fix obligatorio

Cada strategy close hook debe llamar a `notifier.gain/loss` (o un wrapper específico tipo `mm_close`, `hedge_close`). Mínimo: `live_close` con `source_wallet="market_maker"`/`"hedge"`/etc. para que aparezca en Telegram con la categoría.

---

## 6. Schema migrations + concurrency

⚠️ **Riesgo medio: tablas dispersas, sin FKs**

### init_table on-demand (no migrations)

- `mm_orders` → `market_maker._ensure_schema()` (no en _MIGRATIONS)
- `spike_arb_trades` → `spike_arb.init_table()` (no en _MIGRATIONS)
- `long_horizon_trades` → `long_horizon_arb.init_table()` (no en _MIGRATIONS)
- `hedge_trades` → `crypto_arb_hedge.init_table()` (no en _MIGRATIONS)
- `adversarial_orders` → `adversarial_asks.init_table()` (no en _MIGRATIONS)

**Race condition al startup** improbable pero posible: 2 strategies arrancan en paralelo y ambas llaman `CREATE TABLE IF NOT EXISTS`. SQLite `CREATE TABLE IF NOT EXISTS` no es thread-safe sin _TX_LOCK — si llaman concurrente desde threads, uno puede ver "table already exists" entre el SELECT y el CREATE. En PG con autocommit es safe.

**Mitigación recomendada:** Mover los DDL a `_MIGRATIONS` en `src/db/schema.py`. Todos los strategies init dependen de `init_db()` corrido al startup del runner — más simple y testeable.

### 🔴 BLOQUEADOR: Foreign keys ausentes

`hedge_trades.poly_trade_id` apunta a `paper_trades.id` o `live_trades.id` según TRADEBOOK_MODE — **sin FK**. Si TRADEBOOK_MODE cambia entre runs (paper → live → paper), `poly_trade_id=42` puede apuntar a un row de la tabla equivocada. Ya hubo bugs así por la migración Postgres.

**Fix**: agregar FK explícita o columna discriminadora `poly_table TEXT` ('paper_trades'/'live_trades').

### Indexes

- `idx_lh_status`, `idx_lh_opened_at` ✓ (long_horizon)
- `idx_spike_arb_status`, `idx_spike_arb_signal_at` ✓
- `idx_hedge_status`, `idx_hedge_bucket`, `idx_hedge_opened_at` ✓
- `mm_orders` → no se ven indexes ⚠️
- `adversarial_orders` → no se ven indexes ⚠️

**Fix menor**: agregar idx `(status)` y `(opened_at DESC)` a las dos faltantes para no degradar `WHERE status='open'`.

---

## 7. Edge cases que ya pasaron + nuevos

| Lección 2026-05-10 | Status fix | Aplica otras strategies? |
|---|---|---|
| entry_price=0.001 dust ask sin slippage check | ✓ FIX en clob_client.py + LIVE_MIN_ORDERBOOK_DEPTH_USDC=$500 + LIMIT_FOK default | ⚠️ MM postea bid/ask sin orderbook_depth check — si bucket tiene book de $50 total un fill nuestro lo MUEVE → adverse selection garantizado |
| tx_hash=NULL pero status=open (zombi) | ✓ FIX en executor.py:418-437 (`phantom_ok_no_fill`) | 🔴 spike_arb/long_horizon NO tienen este guard al insertar `*_trades`. Si live_executor fuera real, mismo bug posible. |
| Gamma `?conditionId` ignora filtro con closed=true | ✓ FIX (no veo el commit pero log dice "BUG-FIX 2026-05-05" en risk.py:520) | Aplica a settle_resolved en general. Strategies usan path propio (crypto_arb._settle_crypto_arb_resolved) — ⚠️ podrían reincidir. |
| sed -i no agrega env vars si no existen | NO afecta runtime | n/a (deploy issue) |
| DDL SQLite crashea PG | ✓ FIX en schema.py wrapper + cada strategy hace `if BACKEND == "postgres"` | ⚠️ PARCIAL: mm_orders y adversarial_orders verifican BACKEND. spike_arb y long_horizon usan `_translate_sql_to_pg` general. **OK pero frágil** — falta test pre-prod que corra `init_table()` con DB_BACKEND=postgres. |

### Edge cases nuevos no cubiertos

🔴 **Hedge orphan leg**: si Polymarket open OK pero perp Binance falla DESPUÉS del rollback (network, DB locked), `hedge_trades` queda con `status='leg_failed'` y `poly_trade_id` apuntando a un trade ya force_closed. La pérdida del rollback (slippage del SELL inmediato) NO se atribuye al hedge → `hedge_trades.pnl_total_usdc=NULL`.

⚠️ **Idempotencia de spike_arb**: si binance WS reconecta y replay envía mismo tick → mismo signal → posible doble-open. No veo `source_trade_id` UNIQUE en spike_arb_trades.

⚠️ **MM stale quote race**: `should_recalc` lee last_quoted_mid en memoria, no DB. Si runner reinicia mid-bucket, `_pairs` dict se pierde → repostea desde scratch sin cancelar las anteriores → órdenes huérfanas en CLOB que el bot no trackea. Aunque `place_limit_order` es STUB hoy, será problema cuando se wire.

---

## 8. Concurrency bugs

7 async tasks en mismo event loop:
- `_telegram_supervisor`
- `hl_runner`, `dx_runner` (paper-only)
- `ws_run_loop` (WS bridge)
- `crypto_arb_loop`
- `mm.run_loop`, `spike_arb_loop`, `adv.run_loop`, `long_horizon_loop`, `crypto_arb_hedge_loop`
- + el polling loop principal de runner.py

### ✓ OK

- `asyncio.to_thread` usado consistente en hot paths (open_position en crypto_arb, hedge, runner._process_wallet)
- `_TX_LOCK` (process-wide RLock) + `_TX_TLS` (re-entrant) cubren todo `tx()` — race resolvido en commit `84aed6e`
- `binance_ws` + `_safe_process_wallet` con timeouts individuales

### ⚠️ Riesgo medio

- **Strategy loops NO usan watchdog independiente**: si MM loop se cuelga en `_handle_fills` (cuando deje de ser stub), el watchdog principal solo detecta el heartbeat del polling main loop. MM puede colgarse 30min sin que nadie lo note. **Fix: cada strategy loop debe llamar `health.record_heartbeat()` periódicamente**.
- **PolymarketClient compartido**: `runner.py` usa `async with PolymarketClient() as client`, mientras que `crypto_arb_loop` abre **su propia** `PolymarketClient`. Esto está OK por design (clientes httpx independientes), pero duplica conn pool — 7 strategies = 7 connection pools = ~70 sockets vs Polymarket. Riesgo: rate-limit upstream.

### 🔴 Race condition al settle

- `settle_resolved()` global (paper.py / executor.py) toca filas con `status='open'`.
- `crypto_arb._settle_crypto_arb_resolved` también toca paper_trades → mismas filas potencialmente.
- `risk.sweep_stops` puede force_close el mismo row.
- `cleanup_phantom_positions` también UPDATE el mismo row.

**Cuatro paths concurrentes que tocan misma row de paper_trades/live_trades.** Cada uno usa `tx()` con BEGIN IMMEDIATE → SQLite serializa, OK. PG row-locks, OK. Pero la **lógica** puede fallar: si phantom_cleanup setea `closed_external` mientras settle_resolved está esperando el lock, settle_resolved toma el lock y `WHERE status='open'` → 0 rows → no toca. ✓ OK por design.

⚠️ **Pero**: el guard `WHERE status IN ('open', 'waiting_settlement')` está en algunos UPDATEs y no en otros. Verificar coherencia.

---

## 9. Recovery scenarios

### ✓ OK

- **paper_trades zombies recovery**: `reconciler.py` cada 5min detecta BUYs ejecutados sin row local y los inserta. ✓
- **live_trades INSERT outbox**: `executor._persist_live_trade_with_outbox` con retries + jsonl outbox + `drain_live_outbox` al startup. ✓ Garantiza que un fill on-chain real nunca se pierda por DB locked.

### 🔴 BLOQUEADOR: hedge sin recovery

Si runner muere DESPUÉS del Polymarket open pero ANTES del perp Binance:
- `paper_trades` (o `live_trades`) tiene la pata long con status='open'
- `hedge_trades` row NO existe todavía (se persist DESPUÉS del perp)
- Restart: bot abre Polymarket de nuevo (idempotency por source_trade_id ✓), perp tampoco existe, **NO HAY HEDGE**.

**Worst case**: bot muere DESPUÉS del perp open, ANTES de _persist_hedge:
- Polymarket: open, tracked en paper/live_trades
- Binance perp: open, NO tracked en hedge_trades → orphan SHORT en Binance
- Restart: bot no sabe del SHORT, no lo cierra. Sale `hedge_trades` con status='leg_failed' por phantom.

### 🔴 BLOQUEADOR: mm orphan orders

Si runner muere mid-`_reconcile_pair`:
- `place_limit_order` mandó la orden al CLOB (cuando deje de ser stub)
- `_record_open` NO insertó en mm_orders
- Restart: bot ve el book con sus propias órdenes pero `_pairs` dict vacío → no las cancela ni las trackea → fills aleatorios sin contabilidad.

**Fix**: outbox pattern como executor.py para mm_orders + reconciliación inicial vía `client.get_orders()` filtrado por funder (cuando MM se wire real).

### ⚠️ spike_arb / long_horizon similar

Mismo problema de los stubs: cuando se wire la ejecución real, falta atomicity entre "orden mandada al CLOB" y "row insertada en *_trades". Hoy es stub y filled=False → no es issue. Será issue cuando se active.

---

## 10. Test coverage gaps

Tests existentes razonablemente buenos para unit logic (signals, edge calc, settlement). Pero **NO cubren**:

🔴 **Cross-strategy capital allocation**: 0 tests.
🔴 **Cross-strategy kill switch**: 0 tests. `test_kill_switch_hard.py` solo valida que dispare con paper_trades, no con strategies múltiples.
🔴 **PnL Telegram cross-tables**: 0 tests. `test_daily_summary.py` valida formato pero no agregación cross-strategy.
🔴 **Failure modes**:
- CLOB 5xx — no veo test
- Binance perp rate limit (`-1015`) — no veo test
- DB lock cascada (4 paths concurrentes en misma row) — no veo test integration
- Network down mid-trade — no veo test

⚠️ **Recovery del kill switch**: `test_kill_switch_hard.py` valida disparo, no auto-recovery con strategies múltiples cerrando wins.

⚠️ **PG-specific tests**: la mayoría de tests usan `isolated_db` (SQLite). Falta CI step `DB_BACKEND=postgres pytest`. Riesgo: bug de `_translate_sql_to_pg` solo se ve en deploy.

⚠️ **test_executor flakiness mencionado**: `pytest tests/ -k "not test_executor"` — el live executor tiene tests pero son skip flaky → deuda técnica oculta.

---

## Tabla resumen — Antes de pasar a LIVE

| # | Issue | Strategy afectada | Severity | Fix mínimo | Estimate |
|---|---|---|---|---|---|
| 1 | capital_full ignora 5 tablas → over-allocation 4× | TODAS | 🔴 BLOCK | UNION ALL en run_pre_open_checks o per-strategy hardcap | 4h |
| 2 | kill_switch ciego a 5 tablas | TODAS | 🔴 BLOCK | UNION ALL en _check_daily_loss_cap/_check_consecutive_losses/_check_drawdown | 3h |
| 3 | Telegram Acumulado ciego a 5 tablas | TODAS | 🔴 BLOCK | learning.summary + on_paper_trade_closed agregar UNION pnl | 2h |
| 4 | mm/spike/adv/long_horizon/hedge close hooks NO disparan notif | mm/spike/adv/lh/hedge | 🔴 BLOCK | wrapper notif por strategy llamado desde cada close path | 3h |
| 5 | MM stubs NO levantan exception en LIVE_MODE — silent failure | market_maker | 🔴 BLOCK | `if LIVE_MODE: raise NotImplementedError` o log WARNING al startup | 30min |
| 6 | spike_arb/long_horizon stubs idem (sin warning visible) | spike, long_horizon | 🔴 BLOCK | log WARNING al startup si LIVE_MODE | 30min |
| 7 | hedge crypto_arb_hedge_loop es WIREFRAME — no detecta signals | hedge | ✅ FIXED 2026-05-10 | `crypto_arb_hedge_loop` ahora arranca `BinanceTickerWS` + lista markets vía `PolymarketClient` + `_hedge_evaluate_setup` reusa `edge_vs_mid` con threshold del hedge. Dispatch a `evaluate_and_open` por cada market en ventana de pre-close. | done |
| 8 | hedge orphan leg si runner muere entre Polymarket open y perp open | hedge | ✅ FIXED 2026-05-10 | `recover_orphan_perps()` corre al startup del loop: query rows `status='leg_failed' AND perp_order_id IS NOT NULL` → `get_position` on-chain → close si qty>0 → status='recovered' + notif Telegram. `_persist_hedge` ahora escribe a `HEDGE_OUTBOX_PATH` si el INSERT falla (mismo pattern que executor.LIVE_OUTBOX). `drain_hedge_outbox()` reinsta al startup. | done |
| 9 | hedge_trades.poly_trade_id sin FK ni discriminador de tabla | hedge | ✅ FIXED 2026-05-10 | columna `poly_trade_table TEXT` agregada vía `_MIGRATIONS` (ALTER TABLE idempotente, ignora "duplicate column"). `_persist_hedge` setea el discriminador desde `tradebook.TABLE` ('paper_trades' o 'live_trades'). | done |
| 10 | phantom_cleanup setea pnl=0 — oculta pérdidas reales | live N1 | 🔴 BLOCK | estimar pérdida vía mid último visto + flag `external_loss_unknown` | 2h |
| 11 | mm orphan orders al restart (cuando MM se wire) | market_maker | ⚠️ MED | outbox + reconcile_open_orders al startup | 4h |
| 12 | strategies sin heartbeat propio (watchdog ciego) | mm/spike/adv/lh/hedge | ⚠️ MED | record_heartbeat per loop iteration | 1h |
| 13 | spike_arb sin source_trade_id UNIQUE → idempotency | spike_arb | ⚠️ MED | UNIQUE INDEX + check pre-insert | 1h |
| 14 | mm_orders sin orderbook_depth check | market_maker | ⚠️ MED | reuso de estimate_slippage antes de quote | 2h |
| 15 | DDL on-demand fuera de _MIGRATIONS — race posible | mm/spike/adv/lh/hedge | ⚠️ MED | mover a _MIGRATIONS + init_db al startup | 1h |
| 16 | mm_orders/adversarial_orders sin idx status/opened_at | mm/adv | ⚠️ MED | CREATE INDEX | 15min |
| 17 | Sin CI test con DB_BACKEND=postgres | TODAS | ⚠️ MED | docker-compose CI + pytest | 3h |
| 18 | Cross-strategy capital/kill switch sin tests | TODAS | ⚠️ MED | tests/test_cross_strategy_caps.py | 4h |
| 19 | Failure mode tests (CLOB 5xx, Binance rate limit, network down) | TODAS | ⚠️ MED | mocks fault injection | 4h |
| 20 | settle_resolved guards inconsistentes (status open vs waiting_settlement) | live N1 | ⚠️ MED | audit + uniformar todos los UPDATEs | 1h |

### Total estimate fix BLOQUEADORES: ~22h

### Recomendación

**Mantener LIVE solo en N1 copybot** (ya tiene phantom_cleanup notif del 2026-05-10, slippage check, outbox, kill switch funciona para él). Los bugs 1-3 (capital/kill/notif cross-strategy) NO afectan a N1 solo si las otras strategies están **off** vía env var.

**Para activar strategies adicionales en LIVE**: completar al menos issues 1-10 antes de levantar cualquiera de mm/spike/adv/long_horizon/hedge en LIVE_MODE.

**Pivot N2 a long_horizon_arb (markets $30k+ liq)**: es la dirección correcta del config (`long_horizon_arb.py` ya existe), pero hoy el live_executor es STUB. Antes de dispatch al LIVE: completar issues 1-7 + wirear el live_order_executor real.
