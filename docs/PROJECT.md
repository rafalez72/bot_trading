# Polymarket Copy Bot — Master Project Doc

> **Última actualización**: 2026-05-06 (incidente LIVE perdiendo + bug SQLite locked + emergency switch a paper + paridad paper=real + comandos /pause)
> **Propósito**: documento maestro que cualquier asistente AI puede leer al inicio de una nueva conversación para entender el estado completo del proyecto. **Si modificás funcionalidad, ACTUALIZÁ ESTE DOCUMENTO.**

---

## ⚠️ REGLAS PARA EL ASISTENTE AI (LEER PRIMERO)

1. **Este documento es la fuente de verdad** del proyecto. Leelo entero antes de hacer cambios.
2. **Cada cambio funcional debe actualizar este doc** — si agregás un módulo, comando, env var, regla de negocio, tabla de DB → reflejarlo acá.
3. **El usuario** es Rafa (`areadesarrollo@telco.com.ar`), no es desarrollador profesional. Explicale en español plano, no jerga.
4. **Sé honesto sobre incertidumbre**: no prometas ganancias, no exageres efectividad de ML/heurísticas, marcá los caveats.
5. **Plata real ≠ paper trading**. Hay un módulo de "realismo" pero seguimos en paper. **No conectar plata real sin validar 7+ días de live**.
6. **Antes de cambios destructivos** (reset DB, refactor grande): consultar al usuario.
7. **Lenguaje y formato**: respondé en español, claro, con ejemplos concretos, evitá emojis salvo para señalizar tipo de mensaje.

---

## 1. ¿Qué es esto?

Bot que **copia trades** de los mejores wallets de Polymarket (prediction market sobre Polygon). Apuesta réplicas en paper trading con un cap simulado de $100 USDC. Tiene autoaprendizaje multicapa para minimizar pérdidas. La idea última (Fase 5, NO construida aún) es ejecutar con USDC reales en Polymarket.

**Estado actual**: paper trading 24/7 en macOS, migrando a Lenovo Windows con Git + GitHub + Task Scheduler.

---

## 2. Arquitectura

```
┌─────────────────────────┐
│  Mac (desarrollo)        │   ── git push ──►  GitHub (repo privado) ──►
│  /Users/rafalez72/       │                                               │
│  Documents/dev/crondata/ │                                               ▼ (git pull cada 5 min)
│  polymarket_copybot/     │                              ┌──────────────────────────────────┐
└─────────────────────────┘                              │  Lenovo Windows (siempre prendida)│
                                                         │                                    │
                                                         │  ├── FastAPI server (port 8000)    │
                                                         │  ├── Paper Runner (poll cada 10s)  │
                                                         │  ├── Cloudflare Tunnel             │
                                                         │  ├── SQLite DB (data/copybot.db)   │
                                                         │  └── Telegram notifier             │
                                                         └─────────────┬───────────────────────┘
                                                                       │
                                                         ┌─────────────▼───────────┐
                                                         │ iPhone (PWA + Telegram) │
                                                         └─────────────────────────┘
```

---

## 3. Estructura de archivos

### Paths de trabajo (importante para futuras sesiones)

| Máquina | Path | Notas |
|---------|------|-------|
| Mac (dev actual) | `/tmp/bt` | Clone temporal. **Usar este para editar** |
| Mac (original, NO usar) | `~/Documents/dev/crondata/polymarket_copybot` | macOS TCC bloquea acceso a Claude desde aquí. Si Full Disk Access se habilita y Claude se reinicia, podríamos volver — pero `/tmp/bt` funciona y se mantiene |
| Lenovo (prod) | `~/polymarket_copybot` (Git Bash) | Clone permanente. Cron `update_and_restart.bat` cada 5 min pullea + redeploy Docker |
| Repo remoto | `https://github.com/rafalez72/bot_trading` | Único hub de sync Mac↔Lenovo |
| Imagen Docker | `ghcr.io/rafalez72/bot_trading:latest` | GitHub Actions buildea + pushea |

Subdirectorios clave dentro del proyecto:
- `src/api/static/` — PWA dashboard servido en `http://100.98.174.60:8000` (index.html, app.js, sw.js, style.css)
- `src/copybot/` — runners + executors + autoaprendizaje (PM, HL, DX)
- `src/polymarket/` `src/hyperliquid/` `src/dydx/` — clients http async
- `data/copybot.db` — SQLite local de cada máquina (NO sincronizar)
- `logs/` — runtime logs (NO en repo)

### Árbol del proyecto

```
polymarket_copybot/
├── copybot.py                  # CLI principal (entry point)
├── pyproject.toml              # deps Python
├── .env / .env.example         # config
├── data/copybot.db             # SQLite (NO sincronizar a Mac)
├── logs/                       # logs runtime
├── docs/PROJECT.md             # este archivo
├── scripts/
│   ├── setup.sh                # setup Mac/Linux
│   ├── setup.bat               # setup Windows
│   ├── deploy.sh               # rsync Mac → Lenovo (creado en fase migración)
│   ├── gen_icons.py            # genera íconos PWA
│   └── windows/                # batch scripts para Task Scheduler
└── src/
    ├── config.py               # env vars cargadas
    ├── polymarket/client.py    # HTTP client Gamma + Data API
    ├── db/schema.py            # SQLite schema + migraciones
    ├── indexer/
    │   ├── markets.py          # baja todos los mercados (activos+cerrados)
    │   └── trades.py           # baja trades por wallet
    ├── analytics/metrics.py    # PnL, ROI, Sharpe, drawdown, score
    ├── copybot/
    │   ├── selector.py         # filtros estrictos + top N
    │   ├── paper.py            # open/close/settle paper trades
    │   ├── runner.py           # loop principal del bot
    │   ├── learning.py         # autoaprendizaje principal (cierre + drop)
    │   ├── bandit.py           # UCB1 sizing dinámico
    │   ├── auto_filter.py      # auto-tune de thresholds
    │   ├── categories.py       # tracking por categoría + bloqueo
    │   ├── categorize.py       # inferencia de categoría desde slug
    │   ├── policy.py           # self-improvement por bucket (cat/hora/precio)
    │   ├── clusters.py         # K-means de wallets + cross-pollination
    │   ├── discovery.py        # auto-discovery de nuevos traders
    │   ├── risk.py             # stop-loss, take-profit, kill switch
    │   ├── realism.py          # slippage, fees, gas (modo realista)
    │   ├── notifier.py         # Telegram notifs
    │   └── backtest.py         # replay sobre trades históricos
    └── api/
        ├── server.py           # FastAPI app + endpoints REST
        └── static/             # PWA frontend
            ├── index.html      # SPA con tabs: Resumen, Copiando, Top, Mercados
            ├── app.js          # Alpine.js component + Chart.js
            ├── style.css
            ├── manifest.json   # PWA manifest
            ├── sw.js           # service worker
            └── icons/
```

---

## 4. Database schema (SQLite)

| Tabla | Propósito |
|-------|-----------|
| `markets` | Metadata de cada mercado (cid, slug, category, liquidity, volume, outcome_prices) |
| `traders` | Wallets descubiertos (con `last_indexed_at` para tracking de backfill) |
| `trades` | Cada trade individual indexado (idempotente por id sintético) |
| `trader_metrics` | Métricas calculadas: PnL, ROI, Sharpe, win_rate, drawdown, score |
| `copy_subscriptions` | Traders que el bot está copiando (active / paused / dropped) + sizing_mult |
| `paper_trades` | Trades simulados del bot (entry/exit/pnl/status/exit_reason/asset/peak_price) |
| `live_trades` | Trades **reales** en Polymarket CLOB (Fase 5). Mirror de paper_trades + token_id, order_id, tx_hash, fees, dry_run flag, peak_price |
| `live_rejects` | Cada trade rechazado por el validador (at, source_wallet, condition_id, reason, detail JSON). Observabilidad post-2026-04-30 |
| `hl_trades` | Trades del bot Hyperliquid paralelo (dry-run). Mirror de live_trades adaptado a perps (coin, is_buy, leverage, liquidation_price, funding_paid, gas_paid). Post-2026-05-02 |
| `hl_subscriptions` | Wallets HL que el bot copia. status=active/paused/dropped + sizing_mult |
| `hl_rejects` | Rejects del HL bot (espejo de live_rejects para HL) |
| `dx_trades` | Trades del bot dYdX v4 paralelo (dry-run). Mirror de hl_trades para dYdX perps (ticker, is_buy, leverage, liquidation_price, gas_paid, funding_paid). Post-2026-05-03 |
| `dx_subscriptions` | Wallets dYdX que el bot copia. status=active/paused/dropped + sizing_mult |
| `dx_rejects` | Rejects del dYdX bot (espejo de hl_rejects para dYdX) |
| `shadow_trades` | Trades observados de wallets DROPPED (no copiados, solo registrados). Para análisis a posteriori si el threshold de drop fue agresivo |
| `learning_events` | Log de cada decisión de aprendizaje (size_up, size_down, drop, etc.) |
| `category_perf` | Performance acumulada por categoría de mercado + status (allowed/blocked) |
| `cluster_perf` | Performance por cluster (K-means de wallets) |
| `wallet_clusters` | Asignación wallet → cluster_id + features json |
| `bandit_state` | Estado UCB1 por wallet (n_pulls, sum_reward, ucb_score) |
| `filter_thresholds` | Thresholds dinámicos del selector (auto-tuneados) |
| `bot_state` | Key-value: kill_switch, discovery_pending, daily_summary_last, etc. |
| `index_state` | Cursores de indexación por wallet (paper_cursor) |

---

## 5. Configuración (.env)

```bash
# Polymarket APIs (públicas)
GAMMA_API=https://gamma-api.polymarket.com
DATA_API=https://data-api.polymarket.com

DB_PATH=data/copybot.db
LOG_LEVEL=INFO

# Capital y trading
BOT_CAPITAL_USDC=96.0            # cap fijo (no compounding) — matchea balance Polymarket
COPY_BASE_USDC=4.0               # apuesta base por copia (paper) — bajado de 5
COPY_POLL_SECONDS=5              # polling paralelo (gather), bajado de 10
MAX_TRADE_AGE_SECONDS=60         # rechaza copies con >60s antigüedad (filtro stale_trade,
                                 # post-2026-05-06: caso LoL Game 2 -$10x2)

# Paper-only filters (paridad con LIVE — post-2026-05-06)
PAPER_MIN_EXPECTED_PNL_USDC=0.30  # rechaza si PnL esperado < umbral
PAPER_MAX_PER_WALLET_USDC=8.0     # cap por wallet en open (paper)

# Live trading (Fase 5)
LIVE_MODE=true                   # activa executor.py — necesita FORCE_LIVE_OK también
FORCE_LIVE_OK=false              # GUARD post-incidente 2026-05-06. Sin esto = paper.
                                 # Setear true SOLO después de validar 24h paper.
LIVE_DRY_RUN=true                # simula órdenes (dry-run)
LIVE_CAPITAL_USDC=100.0
LIVE_BASE_USDC=10.0              # subido de 5 → 10 para justificar fees
LIVE_MAX_PER_WALLET_USDC=20.0    # 2 trades concurrentes por wallet (subido de 10)
LIVE_MIN_EXPECTED_PNL_USDC=0.50
LIVE_DRY_SLIPPAGE_PCT=0.015      # 1.5% pesimista en dry-run

# Risk management
STOP_LOSS_PCT=0.20               # cierre si cae 20% (bajado de 30)
TAKE_PROFIT_PCT=0.50             # cierre si sube 50% (bajado de 80)
MAX_PER_MARKET_PCT=0.20          # max 20% del cap en un solo mercado
MIN_MARKET_LIQUIDITY_USDC=5000   # filtro de liquidez
MIN_MARKET_VOLUME_USDC=10000     # filtro de volumen
DAILY_KILL_SWITCH_PCT=0.10       # pausa si PnL 24h < -10% del cap
STOPLOSS_SWEEP_SECONDS=15        # frecuencia SL/TP (bajado de 60)

# Trailing stop (activa cuando posición en ganancia)
TRAIL_ACTIVATION_PCT=0.30        # +30% gain activa el trailing
TRAIL_DROP_PCT=0.25              # cierra si cae 25% desde el peak

# Diversificación
MAX_WALLET_24H_PCT=0.50          # max 50% de trades de un wallet en 24h

# Filtro inteligente de mercados cortos (reemplaza filtro lazy por slug)
MIN_TIME_TO_EXPIRY_SECONDS=600   # bloquea markets que expiran en <10min

# Proxy Vercel para bypass de geo-block AR (ver changelog 2026-05-01)
CLOB_API=https://bot-trading-lemon.vercel.app/clob   # routea via Vercel edge → Polymarket
                                                     # default: https://clob.polymarket.com (NO
                                                     # accesible desde IPs argentinas)

# Hyperliquid bot paralelo (dry-run — ver changelog 2026-05-02)
HL_MODE=true                          # arranca el HL runner en paralelo
HL_CAPITAL_USDC=50.0                  # cap ficticio
HL_BASE_USDC=5.0                      # base por trade
HL_MAX_PER_WALLET_USDC=10.0
HL_MAX_LEVERAGE=5.0
HL_STOP_LOSS_PCT=0.20
HL_TAKE_PROFIT_PCT=0.50
HL_TRAIL_ACTIVATION_PCT=0.30
HL_TRAIL_DROP_PCT=0.25
HL_DRY_SLIPPAGE_PCT=0.001             # 0.1% (perps tienen spreads apretados)
HL_ALLOWED_COINS=                     # vacío = todas. Default config: BTC,ETH,SOL
HL_SLEEP_SECONDS=5
HL_SWEEP_SECONDS=30
HL_LIQUIDATION_BUFFER=1.2
HL_MIN_EXPECTED_PNL_USDC=0.20
HL_MAX_FILLS_PER_WALLET_24H=30        # anti-scalper (post-2026-05-03)
HL_NOTIF_MIN_PNL=0.30                 # silencia micro-PnL de scalpers
HL_GAS_PER_FILL_USDC=0.0              # HL no cobra gas explícito (settlement L1)
HL_FUNDING_UPDATE_HOURS=1
HL_USE_ORDERBOOK_FILL=true            # parity: walks orderbook, simula latencia

# dYdX v4 bot paralelo (dry-run — ver changelog 2026-05-03)
DX_MODE=true                          # arranca el dx_runner en paralelo
DX_INDEXER=https://indexer.dydx.trade/v4
DX_LCD=https://dydx-lcd.publicnode.com
DX_CAPITAL_USDC=50.0                  # cap ficticio
DX_BASE_USDC=5.0                      # base por trade
DX_MAX_PER_WALLET_USDC=10.0
DX_MAX_LEVERAGE=5.0
DX_STOP_LOSS_PCT=0.20
DX_TAKE_PROFIT_PCT=0.50
DX_TRAIL_ACTIVATION_PCT=0.30
DX_TRAIL_DROP_PCT=0.25
DX_DRY_SLIPPAGE_PCT=0.001             # 0.1% (perps muy líquidos)
DX_ALLOWED_TICKERS=                   # vacío = todos. Default config: BTC-USD,ETH-USD,SOL-USD
DX_SLEEP_SECONDS=5
DX_SWEEP_SECONDS=30
DX_LIQUIDATION_BUFFER=1.2
DX_MIN_EXPECTED_PNL_USDC=0.20
DX_MAX_FILLS_PER_WALLET_24H=30        # anti-scalper
DX_NOTIF_MIN_PNL=0.30
DX_GAS_PER_FILL_USDC=0.02             # parity: gas Cosmos por fill
DX_FUNDING_UPDATE_HOURS=1             # parity: hourly funding update
DX_USE_ORDERBOOK_FILL=true            # parity: walks orderbook real

# Telegram multicast — comma-separated chat_ids para notifs broadcast
TELEGRAM_BOT_TOKEN=...                # NUNCA commitear al git
TELEGRAM_CHAT_ID=<owner_id>,<friend_id1>,<friend_id2>   # primer ID = owner
                                       # solo el primero ejecuta comandos

# Realismo (simula plata real)
REALISTIC_MODE=true
REALISM_ENTRY_SLIP_PCT=0.015     # +1.5% slippage en entries
REALISM_EXIT_SLIP_PCT=0.015      # -1.5% slippage en exits
REALISM_SL_SLIP_MULT=2.0         # x2 slippage en stop-loss
REALISM_LONGSHOT_SLIP_PCT=0.04   # +4% si price <0.15 o >0.85
REALISM_LOWLIQ_SLIP_PCT=0.03     # +3% si liquidity <$10k
REALISM_LOWLIQ_THRESHOLD_USDC=10000
REALISM_FEE_PCT=0.02             # 2% fee Polymarket sobre wins
REALISM_GAS_USDC=0.02            # gas Polygon por tx

# Telegram
TELEGRAM_BOT_TOKEN=8515998173:AAG_etLhmMHgGCqL_Zmeg42cKzHStjADrgQ
TELEGRAM_CHAT_ID=1350329630
```

**⚠️ Token expuesto**: hay que regenerarlo en BotFather (`/token` → `bonny21bot` → Generate new token).

---

## 6. Comandos CLI

```bash
# Setup
python copybot.py init                       # crea/migra DB
python copybot.py status                     # resumen del estado

# Indexación
python copybot.py markets                    # indexa todos los mercados
python copybot.py discover [--pages 50]     # descubre wallets activos
python copybot.py backfill <wallet>         # historial de un wallet (cap 3500)
python copybot.py backfill-all [--limit N]  # backfill de todos los descubiertos

# Análisis y selección
python copybot.py compute-metrics            # calcula métricas por wallet
python copybot.py top [--limit 50]          # ranking
python copybot.py select [--top 20]         # selecciona top N para copiar

# Categorías y clusters
python copybot.py categorize                 # infiere categoría desde slug
python copybot.py categories                 # ver performance por categoría
python copybot.py cluster-now                # re-corre K-means
python copybot.py cluster-status             # ver clusters

# Auto-learning
python copybot.py policy                     # refrescar policy (bucket blocks)
python copybot.py tune [--force]             # auto-tune de thresholds

# Discovery
python copybot.py discover-now               # cycle completo de discovery

# Paper trading
python copybot.py run-paper [--once]         # loop de paper trading
python copybot.py settle-paper               # liquidar mercados resueltos

# Live trading — Fase 5 (plata real)
python copybot.py check-live                 # health check del CLOB de Polymarket
python copybot.py run-live --dry-run         # loop con orden simulada (sin gastar)
python copybot.py run-live --real --yes      # ÓRDENES REALES con USDC
python copybot.py live-status                # resumen de live_trades

# Backtest
python copybot.py backtest [--hours 168]     # replay histórico

# Risk
python copybot.py reset-killswitch           # desactivar kill switch manual
python copybot.py reset-thresholds           # volver thresholds del selector a defaults

# Paper / live mantenimiento (post-2026-05-06)
python copybot.py paper-reset --yes          # archiva paper_trades a backup_<ts>, PnL=$0
python copybot.py live-cleanup               # limpia live_trades fantasma (compara on-chain)
python copybot.py validate-real-readiness --hours 24
                                             # checklist 6-puntos para decidir paso a real

# Dashboard
python copybot.py serve [--port 8000]        # FastAPI + PWA

# Telegram
python copybot.py telegram-setup             # descubre chat_id
python copybot.py telegram-test              # mensaje de prueba
```

---

## 7. Capas de auto-aprendizaje

| Capa | Frecuencia | Acción |
|------|-----------|--------|
| **Stop-loss / Take-profit** | cada 15s | cierra posición individual al -20% / +50% |
| **Trailing stop** | cada 15s | cuando peak ≥ entry × 1.30 → SL pasa a peak × 0.75 (captura más del upside en wins grandes) |
| **Kill switch (con reset high-water + auto-recovery)** | cada ciclo | pausa todo si PnL < -10% del cap en `max(24h, último reset manual)`. **Auto-recovery**: si PnL recupera > threshold, se desactiva solo. **Race protection**: ignora trigger si reset hace <5s. |
| **Bandit UCB1** | cada cierre | recalcula `sizing_mult` de TODOS los activos |
| **Inactivity decay** | cada cierre (post-UCB) | sizing × 0.7 si wallet no operó en >=24h (libera capital de arms dormidas) |
| **Auto-drop por racha** | cada cierre | drop si 3 losses consecutivos |
| **Auto-drop por PnL** | cada cierre | drop si total cerrado <= -$10 con ≥5 trades (independiente de racha) |
| **Auto-drop por reject-clog** | cada ~10 min | drop si ≥50 rejects en 24h con 0 fills (wallet ruidoso sin valor) |
| **Diversification cap** | en open | reject `diversification_cap` si un wallet ya hizo > 50% de los trades en 24h |
| **Smart expiry filter** | en open | parsea timestamp del slug (`-(\d{10,13})$`); reject `expires_too_soon` si `expiry - now < MIN_TIME_TO_EXPIRY_SECONDS` (default 600s = 10 min). Reemplaza al filtro lazy por slug pattern. Fail-open si el slug no tiene epoch (markets largos: deportes, política) |
| **Auto-replace** | inmediato post-drop | re-corre `select_traders`, llena vacante |
| **Discovery on-demand** | si active < 20 | flag `discovery_pending=true` → runner dispara cycle |
| **Cluster perf refresh** | cada 5 min | actualiza `cluster_perf`; bloquea/penaliza clusters |
| **Categoría auto-block** | cada cierre, ≥15 trades | bloquea categoría si win<40% y PnL<0 |
| **Policy bucket** | post-backtest, ≥8 trades | bloquea hora/categoría/rango_precio perdedor |
| **Auto-tune filtros** | cada 6h | endurece/afloja MIN_WIN_RATE, MIN_VOLUME, etc. |
| **Discovery automática** | cada 8h | discover + backfill nuevos + recluster + select |
| **Daily summary** | cada 24h | (DESACTIVADO por pedido) |
| **Health monitor upstream** | cada ~3 min | ping a CLOB/proxy + Data API. Tras 3 fallas consecutivas (~9 min) → alerta Telegram (`outage_alert`). Recovery → notif. Ver `health_monitor.py` |
| **Auto-drop por inactividad on-chain** | cada ~6h | drop wallets cuyo `paper_cursor` no avanzó en >48h (sin actividad). `learning.auto_drop_by_inactivity()` |
| **Shadow tracker** | cada ~1h | pollea wallets dropped, registra sus trades en `shadow_trades` (sin copiar). Post-mortem analysis de drops. Ver `shadow_tracker.py` |
| **Discovery on-idle** | cada ~6h | si 0 trades del bot en 6h, fuerza `discovery_pending=true` para refresh del top |
| **HL bot paralelo (dry-run)** | mismo poll que PM | Polling Hyperliquid en task separado. Validación + simulación de fills + SL/TP/trailing/liquidation buffer. Ver `hl_runner.py`, `hl_executor.py` |
| **dYdX v4 bot paralelo (dry-run)** | mismo poll que HL | Polling indexer.dydx.trade en task separado. Validación + simulación + SL/TP/trailing/liquidation. Ver `dx_runner.py`, `dx_executor.py` |
| **Anti-scalper filter (HL/DX)** | en open | reject `scalper_wallet` si el wallet hizo > `*_MAX_FILLS_PER_WALLET_24H` (default 30) en 24h. Auto-drop si supera 60. Filtra HFT que generan -$0.01 / fill puro slippage cost |
| **Funding update hourly (HL/DX)** | cada 1h (`*_FUNDING_UPDATE_HOURS`) | Por cada posición open, fetch funding rate del market y acumula en `funding_paid`. Se descuenta del PnL al cerrar. Production parity con perps reales |
| **Production parity gas (PM/HL/DX)** | en open + close | Cada fill incrementa `gas_paid`. Al cerrar, PnL se descuenta gas_total + funding_paid. Espeja el costo real |
| **Orderbook fill (HL/DX)** | en open + close | Si `*_USE_ORDERBOOK_FILL=true`, runner fetcha orderbook real y walks levels para nuestro size. Simula latencia ~2s broadcast. Reemplaza al slippage simple |

---

## 8. Notificaciones de Telegram (estado actual)

**Activas:**
- 📈 `gain` — cada trade cerrado con ganancia: "Ganancia / Ganó: $X / Acumulado: $Y"
- 📉 `loss` — cada trade cerrado con pérdida: "Pérdida / Perdió: $X / Acumulado: $Y"
- ⛔ `kill_switch` — cuando el bot se autopausa por safety

**Comandos entrantes (telegram_listener, long polling)**:
- `/status` — modo, kill_switch, PnL 24h, wallets activos, posiciones abiertas
- `/pause` — **ACTIVA** el kill switch (bloquea nuevos opens). Post-2026-05-06.
- `/resume` — desactiva el kill switch. Aliases legacy: `/killswitch`, `/resetkill`
- `/help` — lista de comandos
- Solo responde al PRIMER chat_id de `TELEGRAM_CHAT_ID` (owner). Los amigos
  suscriptos solo reciben notifs (read-only).

**Desactivadas (stubs en el código):**
- `trader_dropped` (eliminada por pedido del usuario)
- `cluster_blocked`
- `big_stop_loss` (>$3)
- `big_take_profit` (>$3)
- `daily_summary`

**Para reactivar**: agregar el nombre al set `ENABLED_NOTIFICATIONS` en `src/copybot/notifier.py`.

---

## 9. Filtros de selección (`src/copybot/selector.py`)

Para que un trader entre al top 20:
- `score >= 0.55` (composite: PnL + win_rate + sharpe + ROI + volume + drawdown)
- `realized_pnl_usdc >= 500`
- `win_rate >= 0.55`
- `total_trades >= 150`
- `total_volume_usdc >= 25000`
- `max_drawdown_pct <= 50`
- `sharpe_proxy >= 0.4`

Con la base actual de ~522 wallets backfilleados, **20 candidatos pasan estrictos**.

Estos thresholds son **dinámicos**: `auto_filter.maybe_tune()` los ajusta cada 6h según rolling win-rate.

---

## 10. Plan de migración Mac → Lenovo Windows (EN CURSO)

### Estrategia de sync: Git + GitHub (reemplaza rsync)

En vez de rsync (que requería instalación extra en Windows), usamos **Git + GitHub**:
- Mac edita código → `git commit` → `git push` a repo privado en GitHub
- Lenovo tiene Task Scheduler que cada 5 minutos corre `update_and_restart.bat`:
  - Detecta si hay commits nuevos (`git fetch` + compara hashes)
  - Si hay cambios: `git pull`, `pip install -e .`, reinicia los servicios
  - Si no hay cambios: no hace nada (no reinicia innecesariamente)

### Estado de las fases

```
FASE 1: Tailscale en ambas máquinas         [✓ COMPLETADO]
        Mac y Lenovo (usuario "melina") visibles via Tailscale
        IP Lenovo: 100.98.174.60

FASE 2: Python + Git + OpenSSH en Windows   [✓ COMPLETADO]
        ✓ Python instalado
        ✓ Git for Windows instalado
        ✓ OpenSSH Server instalado y corriendo
        ✓ Firewall: regla TCP 22 permitida

FASE 3: Git init en Mac + push a GitHub     [✓ COMPLETADO]
        ✓ git init + .gitignore
        ✓ Scripts windows/ creados (start_server, start_runner, update_and_restart)
        ✓ Repo privado en GitHub creado
        ✓ Primer commit y push

FASE 4: Clone + deps + .env en Lenovo       [EN CURSO — pendiente ejecutar]
        → git clone <repo> ~/polymarket_copybot
        → scp .env desde Mac
        → pip install -e .
        → python copybot.py init && python copybot.py status

FASE 5: Task Scheduler en Lenovo            [PENDIENTE]
        → 3 tareas: CopyBot-Server, CopyBot-Runner, CopyBot-Update (cada 5 min)

FASE 6: Cloudflare Tunnel persistente       [PENDIENTE]
FASE 7: (eliminada — deploy.sh reemplazado por git push)
```

### Conexión Mac ↔ Lenovo

- Tailscale instalado en ambos
- Lenovo IP Tailscale: `100.98.174.60`
- Usuario en Windows: `melina`
- SSH desde Mac: `ssh melina@100.98.174.60` (pass guardada localmente, no en repo)


### Comandos para FASE 4 (ejecutar en Lenovo via Git Bash)

```bash
# 1. Clonar (pide usuario GitHub + PAT como contraseña)
git clone https://github.com/<tu-usuario>/polymarket-copybot.git ~/polymarket_copybot

# Guardar credenciales para no pedirlas de nuevo
git config --global credential.helper store

# 2. Copiar .env desde Mac (correr esto en Mac, NO en Lenovo)
# scp /Users/rafalez72/Documents/dev/crondata/polymarket_copybot/.env melina@100.98.174.60:polymarket_copybot/.env

# 3. Instalar deps
cd ~/polymarket_copybot
python -m venv .venv
source .venv/Scripts/activate
pip install -e .

# 4. Init y verificar
python copybot.py init
python copybot.py status
```

### Comandos para FASE 5 (PowerShell ADMIN en Lenovo)

```powershell
# Tarea 1: FastAPI server (arranca con Windows)
$a = New-ScheduledTaskAction -Execute "$env:USERPROFILE\polymarket_copybot\scripts\windows\start_server.bat"
$t = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "CopyBot-Server" -Action $a -Trigger $t -RunLevel Highest -Force

# Tarea 2: Paper runner (arranca con Windows)
$a = New-ScheduledTaskAction -Execute "$env:USERPROFILE\polymarket_copybot\scripts\windows\start_runner.bat"
$t = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "CopyBot-Runner" -Action $a -Trigger $t -RunLevel Highest -Force

# Tarea 3: Auto-update cada 5 minutos
$a = New-ScheduledTaskAction -Execute "$env:USERPROFILE\polymarket_copybot\scripts\windows\update_and_restart.bat"
$t = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 5) -Once -At (Get-Date)
Register-ScheduledTask -TaskName "CopyBot-Update" -Action $a -Trigger $t -Force
```

### Workflow diario una vez instalado

**En Mac** (cuando editás código):
```bash
cd /Users/rafalez72/Documents/dev/crondata/polymarket_copybot
git add .
git commit -m "fix: descripcion del cambio"
git push
# En max 5 minutos la Lenovo aplica los cambios y reinicia el bot
```

**Ver log de deploys en Lenovo:**
```bash
# Via SSH desde Mac:
ssh melina@100.98.174.60 "cat ~/polymarket_copybot/logs/deploy.log"
```

---

## 11. Decisiones clave (decision log)

| Decisión | Fecha | Razón |
|----------|-------|-------|
| Git + GitHub en vez de rsync | 2026-04-27 | rsync requería instalación en Windows; Git ya estaba instalado, agrega historial de cambios y es más robusto |
| SQLite (no Postgres) | 2026-04-26 | Single-file, portable Mac↔Windows |
| Cap fijo $100, NO compounding | 2026-04-27 | Usuario debe poder vivir con esa plata; protege ganancias |
| Cycle 10s (no menos) | 2026-04-27 | Sweet spot vs API throttle, block time Polygon=2s |
| Top 20 traders (era 10) | 2026-04-27 | Coincide con candidatos calificados; matemática $5×20=$100 |
| Filtros estrictos | 2026-04-27 | Usuario priorizó "minimizar pérdidas" sobre frecuencia |
| Modo realista activo | 2026-04-27 | Refleja slippage+fees+gas como plata real |
| K-means K=6 | 2026-04-27 | Suficiente diversidad con ~480 wallets |
| Realismo es heurístico | 2026-04-27 | Calibrar con un trade real cuando lleguemos a Fase 5 |
| Notif solo gain/loss/kill_switch | 2026-04-27 | Pedido del usuario; resto silenciado |
| Auto-discovery en drop sin cooldown | 2026-04-27 | Procesamiento extra es trivial vs beneficio |
| Dry-run del live debe igualar a real | 2026-04-29 | Es la última validación antes de plata real, no puede tener "atajos". Implica: kill_switch y auto_filter cuentan dry-run. |
| Auto-filter lee tabla activa, no paper hard-coded | 2026-04-29 | Para que el módulo aprenda del modo actual; si no, decisiones se basan en evidencia desconectada |
| Min 30 cierres antes de mover thresholds | 2026-04-29 | Evita endurecimiento espurio por ruido estadístico (caso del 29/04 05:22 con 2 dry-run losses) |
| Kill switch con high-water reset (no borra trades) | 2026-04-29 | El reset manual del usuario significa "ya sé del drawdown, déjame seguir"; los trades quedan para histórico pero no vuelven a disparar. Solo nuevas pérdidas reactivan. Permite operar después de un drawdown sin perder evidencia. |

---

## 12. Caveats / Limitaciones conocidas

1. **API cap de 3500 trades por wallet** — Polymarket Data API limita offset ≈3500. Para wallets ULTRA activos, perdemos historial profundo.
2. **API cap de ~250K mercados** — Algunos mercados no se indexan. Mitigado con stub on-the-fly desde raw del trade.
3. **Mercados negRisk** — La Gamma API NO los devuelve por `conditionId`. Stub se construye desde `raw.slug + raw.title` del trade.
4. **Stop-loss en backtest es aproximado** — busca en trades del mismo asset el primer cruce del umbral; no es exacto.
5. **Realismo es heurístico** — los parámetros (1.5% slippage, etc.) son estimados. Necesitan calibración con execution real.
6. **Bandit sample size** — con 5-15 closes/día y 20 traders, cada uno tarda 7-10 días en converger.
7. **Cloudflare Tunnel temporal cambia URL** al reiniciar `cloudflared`. Para fija: Cloudflare account + dominio.
8. **Trader rehabilitado**: si dropped trader vuelve a pasar filtros, se reactiva automáticamente (status='active'); el bandit recuerda su mala racha vía sum_reward negativo, así que arranca con sizing reducido.
9. **No hay ML real con features** (LightGBM/etc.) — pateado a Fase 6c, requiere ≥200 closes acumulados (~30 días).
10. ~~No hay execution path real — bot es 100% paper. Fase 5 sin construir.~~ **CONSTRUIDO** (2026-04-28). Pero default es `LIVE_MODE=false` y `LIVE_DRY_RUN=true`. Para activar requiere setup manual del usuario (API creds + USDC en proxy wallet). Settlement es semi-manual: el bot marca el trade como settled pero el "Redeem" de las shares ganadoras hay que hacerlo desde polymarket.com (1 click).

---

## 13. Pendientes / Backlog (orden sugerido)

| Prioridad | Tarea | Esfuerzo |
|-----------|-------|----------|
| 🔴 Alta | **Ejecutar FASE 4** — clone + deps + .env en Lenovo (Git ya configurado) | 20 min |
| 🔴 Alta | **Ejecutar FASE 5** — Task Scheduler en Lenovo (3 tareas: server, runner, update) | 15 min |
| 🔴 Alta | Cloudflare Tunnel persistente con dominio | 30 min |
| 🟡 Media | Calibrar realismo con execution real (1 trade en plata real) | requiere Fase 5 |
| 🟡 Media | Notif "trader rehabilitado" cuando dropped vuelve | 15 min |
| 🟢 Baja | Más fuentes de discovery (leaderboard público) | 2-3h |
| 🟢 Baja | Fase 6c: feature pipeline + LightGBM (cuando haya 200+ closes) | 2-3 días |
| 🟢 Baja | ~~Fase 5: execution real~~ → **construida 2026-04-28**. Falta setup del usuario (API creds + USDC en cuenta) y validación con dry-run + 1 trade real chico antes de activar normal. |
| 🟢 Baja | Compounding (cuando 30+ días positive) | 1h |

---

## 13.bis Fase 5 — Live Trading (módulo construido, NO activo)

### Arquitectura

El bot tiene un dispatcher en `src/copybot/tradebook.py` que elige paper o live según `LIVE_MODE`:

```
runner.py / risk.py
       │
       ▼
tradebook.py ──► (LIVE_MODE=false) ──► paper.py    ──► tabla paper_trades
                  (LIVE_MODE=true)  ──► executor.py ──► tabla live_trades + CLOB
```

`executor.py` reusa toda la lógica de validación de `paper.py` (kill switch, suscripción, duplicados, market caps, category blocks, policy, extreme price). Solo cambia: el size base (LIVE_BASE_USDC), el cap global (LIVE_CAPITAL_USDC), la persistencia (live_trades) y el "execution" (orden real al CLOB vs INSERT).

### Setup inicial (una vez)

1. **Crear cuenta en Polymarket** (polymarket.com → email signup)
2. **Cargar USDC** (≥$50) en la proxy wallet (MoonPay o transfer desde exchange)
3. **Aprobar contratos** desde la UI de Polymarket (USDC + Conditional Tokens)
4. **Generar API creds**: `python scripts/generate_api_creds.py`
   - Pide tu private key SOLO en memoria (no se guarda)
   - Imprime API_KEY, API_SECRET, API_PASSPHRASE
5. **Pegar al .env de Lenovo**:
   ```
   POLYMARKET_API_KEY=...
   POLYMARKET_API_SECRET=...
   POLYMARKET_API_PASSPHRASE=...
   POLYMARKET_FUNDER_ADDRESS=0x... (proxy wallet)
   POLYMARKET_SIG_TYPE=2
   LIVE_MODE=true
   LIVE_DRY_RUN=true   # ← arrancar SIEMPRE con dry-run primero
   ```

### Flujo de validación recomendado

1. `python copybot.py check-live` → verifica creds + balance
2. `python copybot.py run-live --dry-run` → corre 24h en dry-run, mira `live_status`
3. Si dry-run muestra trades sanos → cambiar `LIVE_DRY_RUN=false` en .env
4. `python copybot.py run-live --real --yes` → ¡plata real!

### Modos de ejecución

| Modo | LIVE_MODE | LIVE_DRY_RUN | Comportamiento |
|------|-----------|--------------|----------------|
| Paper | false | (irrelevante) | INSERT en paper_trades, no toca CLOB |
| Live dry-run | true | true | Loguea órdenes + INSERT en live_trades con dry_run=1, NO toca CLOB |
| Live real | true | false | **Manda órdenes al CLOB, gasta USDC** |

### Settlement

Para mercados resueltos, `settle_resolved()` marca los live_trades como `settled_win/loss` con el payout teórico. **NO redime las shares automáticamente** — eso requiere llamar al contrato CTF de Polygon. El usuario debe hacer el "Redeem" desde polymarket.com (1 click).

### Notificaciones live (Telegram)

Activadas por default en LIVE_MODE: `live_open`, `live_close`, `live_error`. Incluyen tx_hash con link a Polygonscan cuando aplica.

### Mejoras anti-fricción (Tier 1)

Aplicadas para reducir el gap entre paper y plata real:

| Mejora | Dónde | Default |
|--------|-------|---------|
| **IOC + retry escalonado** | `clob_client.place_market_order` cambia FOK→FAK (Fill-And-Kill, IOC); si fillea <50% retry 1 vez con precio +1% peor (BUY) | `LIVE_RETRY_PRICE_BUMP_PCT=0.01` |
| **Pre-check orderbook** | `clob_client.estimate_slippage` consulta el libro antes de mandar orden, calcula VWAP esperada al size deseado, aborta si slippage > umbral | `LIVE_MAX_SLIPPAGE_PCT=0.03` |
| **Cap por wallet** | `executor._open_position_validate` rechaza con `wallet_concentration` si el wallet ya tiene > X open. Forza diversificación entre traders | `LIVE_MAX_PER_WALLET_USDC=10.0` |
| **Filter expected_pnl** | `executor` calcula `realism.expected_net_pnl(size)` y rechaza con `expected_pnl_too_low` si está por debajo del mínimo. Evita trades donde fees+gas se comen el upside | `LIVE_MIN_EXPECTED_PNL_USDC=0.50` |
| **Gas calibrado** | `realism.GAS_PER_TX` subido de $0.02 → $0.05 (más realista para Polymarket en Polygon) | `REALISM_GAS_USDC=0.05` |

---

## 14. Cómo bootstrapear una nueva sesión AI

Si abrís un chat nuevo y el AI no sabe nada del proyecto:

1. **Abrirle este archivo primero**: "Antes de hacer nada, leé `docs/PROJECT.md` entero."
2. Después: "Mostrame `python copybot.py status` y dime qué proponés."
3. **Confirmá que cumple las reglas del § "REGLAS PARA EL ASISTENTE AI"**.
4. Cualquier cambio importante: actualizar la sección 11 (decision log) y 13 (pendientes).

---

## 15. Estado actual operativo (al 2026-04-29)

### Infra
```
✓ Bot dockerizado, imagen en ghcr.io/rafalez72/bot_trading:latest
✓ GitHub Actions buildea + pushea imagen en cada push a main
✓ Lenovo (100.98.174.60) corre Docker Desktop
✓ Task Scheduler "CopyBot-Update" corre cada 5 min: pull + restart si hay imagen nueva
✓ Telegram bot @bonny21bot manda notif en cada deploy/error
✓ Tab "Live" en el dashboard PWA (puerto 8000) — http://100.98.174.60:8000
```

### Modo trading actual
```
LIVE_MODE=true
LIVE_DRY_RUN=true     ← validando antes de plata real
LIVE_CAPITAL_USDC=100
LIVE_BASE_USDC=5.0
LIVE_MAX_PER_WALLET_USDC=10  (default config.py)
LIVE_MIN_EXPECTED_PNL_USDC=0.50  (default config.py)
LOG_LEVEL=INFO
```

> Cap subido de $30 → $100 el 2026-04-29 mediodía: con base=$2.5 y los
> `sizing_mult` < 1.0 del bandit, el filtro `expected_pnl_too_low`
> rechazaba todos los BUYs (expected net ~$0.41 < $0.50 mínimo). Al
> volver al setting del paper (cap=$100, base=$5) el filtro queda
> calibrado y el bot opera normalmente.

### Polymarket account (con problemas — ver caveat #11)
```
Funder (proxy):   0x110345DeA0Ae8A7584D5e43244c1DCd6Ee170E2d
Signer EOA:       (Magic-managed para email rafalezcano72@gmail.com)
API creds:        configuradas en .env de Lenovo
Balance USDC:     $0.00 (sin fundear, NO usar — wallet comprometida)
```

### Resultados acumulados

**Paper (congelado al pasar a live_dry el 2026-04-28):**
```
1140 trades cerrados · 373 wins / 767 losses · win rate 32.7%
PnL acumulado: +$1,470.66 sobre cap $100
```

**Live dry-run (en validación):**
```
2 trades cerrados (todos del wallet 0xa535fabc97, cap=$30 inicial)
2 stop-losses al -54% y -60% · PnL: -$3.99
+ trade #3 abierto el 29/04 12:18 ARG (después del cambio a cap=$100)
  wallet 0x6f84f94cef · BUY @ 0.500 · size $7.83 · open
```

---

## 16. Bitácora de avances (changelog cronológico)

### 2026-05-06 (tarde) — incidente LIVE perdiendo + bug SQLite locked + emergency switch a paper + paridad paper=real

**Hito**: bot LIVE estuvo varias horas operando con un bug que hacía que
solo se procesaran BUYs (los SELLs fallaban con "database is locked" y se
descartaban silenciosamente, dejando posiciones huérfanas que no cerraban).
Usuario detectó y reportó "todo el día solo compro y nunca vendio" + perdiendo
plata real. Resuelto con 6 commits + emergency switch a paper.

#### Diagnóstico (commit ee5c825)
**Bug del SQLite lock**: 3 runners (PM/HL/DX) + sweep + telegram listener +
reconciler + funding update todos abren conexiones SQLite y hacen
`BEGIN IMMEDIATE` en paralelo. Con `busy_timeout=5s` + `_retry_locked` x2
(commit `0d5644a`), el peor caso es ~10.5s. Cuando coincidían varios writers,
algunos excedían el timeout → `sqlite3.OperationalError: database is locked`
→ excepción en `runner._process_wallet` → SELL descartado.

**Bug del cursor**: en `runner._process_wallet`, `last_ts = max(last_ts, ts)`
estaba FUERA del try/except. Cuando la SELL fallaba, igual se avanzaba el
cursor → la SELL se perdía permanentemente → la posición quedaba abierta
indefinidamente. Solo se cerraban por SL/TP/settle eventualmente, pero las
de mercados que cierran en out-of-the-money quedaban a $0.

#### Fix #1: lock + cursor + emergency guard FORCE_LIVE_OK (`ee5c825`)
1. **`src/db/schema.py`**: agregado `threading.RLock` proceso-wide en `tx()`.
   Las escrituras intra-proceso se serializan en Python (~ms) en vez de
   rebotar contra SQLite. `busy_timeout` subido a 15s como red para
   choque inter-proceso (runner ↔ FastAPI server).
2. **`src/copybot/runner.py`**: si la op SYNC tira excepción, NO avanzar
   cursor. La SELL fallida se reintenta en el próximo cycle. `break` del
   for para no procesar trades dependientes después.
3. **`src/config.py`**: nueva guarda `FORCE_LIVE_OK`. `LIVE_MODE` ahora =
   `LIVE_REQUESTED && FORCE_LIVE_OK`. Sin `FORCE_LIVE_OK=true` en .env, el
   bot opera en paper. Es un seguro de "no arranque real por accidente
   post-incidente".

#### Fix #2: nuevos comandos CLI
- **`paper-reset --yes`** (`8274917`): archiva `paper_trades` a
  `paper_trades_backup_<ts>` y trunca. PnL paper arranca en $0. Mismo patrón
  del wipe del 2026-05-01.
- **`validate-real-readiness [--hours 24]`** (`7bdfa4b`): checklist mecánica
  6 puntos sobre logs+DB. PASS/FAIL/WARN. Veredicto final ✓/⚠/✗ para decidir
  si pasar a real. Lee de `docker logs`, mide errores DB locked,
  ratio CLOSE/OPEN, sweep activity, reconciler phantom count, PnL paper N
  horas vs -5% cap.
- **`live-cleanup`** (`49f09fc`): dispara `cleanup_phantom_positions`
  on-demand. Compara `live_trades.status=open` contra
  `data-api/positions` y marca como `closed_external` los que no existen
  on-chain. Antes solo corría auto cada 30min en LIVE; en paper no corría →
  fantasmas se acumulaban.

#### Fix #3: filtro `stale_trade` (`f68eefe`)
Caso real: 2 trades LoL Game 2 entry@0.45 → match terminó minutos después →
contratos perdedores cayeron a $0.001 → -$10 cada uno.
Diagnóstico: el wallet entró tarde al match, el polling agregó 5-7s de
latencia, y el trade ya tenía >60s de antigüedad cuando lo procesamos.
Copiamos un trade donde el edge ya se evaporó.

Filtro nuevo en `paper.open_position` y `executor._open_position_validate`:
si `int(time.time()) - timestamp > MAX_TRADE_AGE_SECONDS` (default 60),
reject `stale_trade`. Override vía `MAX_TRADE_AGE_SECONDS` env.

#### Fix #4: paridad "paper test = LIVE real" + `/pause` Telegram (`14a337b`)
**Hallazgo**: paper.py tenía MENOS filtros que executor.py. El "test paper"
era más permisivo que el real → falsa sensación de "esto va a funcionar
en real" cuando en real esos trades ni se abrían.

4 filtros portados de executor.py a paper.py:
- `expires_too_soon` — smart expiry (parsea slug por epoch/date+hour ET/YYYY-MM-DD)
- `diversification_cap` — rechaza si 1 wallet > 50% trades 24h (guard total>=10)
- `expected_pnl_too_low` — rechaza si PnL esperado < `PAPER_MIN_EXPECTED_PNL_USDC`
  (default 0.30, override vía env)
- `wallet_concentration` — rechaza si wallet ya tiene > `PAPER_MAX_PER_WALLET_USDC`
  open (default 8.0, override vía env)

Refactor: `_parse_slug_expiry` y `_MONTH_MAP` extraídos de executor.py a
**`src/copybot/_slug_expiry.py`** para evitar import circular (executor
importa de paper, no al revés).

**Comandos Telegram nuevos**:
- **`/pause`** — ACTIVA el kill switch (bloquea nuevos opens). Antes no
  había forma de pausar el bot sin SSH.
- **`/resume`** — alias claro de `/killswitch` (que era confuso —
  DESACTIVA, no ACTIVA). `/killswitch` y `/resetkill` siguen como aliases
  legacy.
- Nueva función `pause_bot(reason)` en `risk.py` — espejo de
  `reset_kill_switch()`.

#### Acciones manuales de la sesión (vía SSH a Lenovo)
1. `git pull && docker compose pull && docker compose up -d` →
   containers recreados con imagen nueva post-`f68eefe`.
2. Edit `.env`: `BOT_CAPITAL_USDC=96.0`, `COPY_BASE_USDC=4.0` (matchea el
   real disponible en polymarket.com). Backup en `.env.bak.precap96`.
3. `python copybot.py reset-killswitch` → reanudar tras drawdown -$21.
4. `python copybot.py live-cleanup` → **12 phantom positions limpiadas**.
5. `python copybot.py paper-reset --yes` → **1162 trades archivados** en
   `paper_trades_backup_1778086594`. PnL arranca en $0.

#### Estado post-fix
- Bot operando en **paper mode** (LIVE_MODE forzado a False por guard)
- Cap $96, base $4 (matchea el balance real disponible)
- 20 wallets activos, 25 dropped
- Kill switch desactivado
- Todos los filtros del LIVE están en paper → "test = real" verdadero
- Para pasar a real: setear `FORCE_LIVE_OK=true` + `LIVE_MODE=true` en .env
  + restart. **No tocar real hasta que `validate-real-readiness --hours 24`
  dé 6 PASS verdes.**

#### Lecciones
1. **SQLite con WAL + multi-writer no es free**. busy_timeout solo no
   alcanza con >3 writers concurrentes. Lock Python proceso-wide es
   obligatorio para serializar.
2. **Nunca actualizar el cursor antes de confirmar la op**. Es el patrón
   "optimista" — funciona si la op no falla. Falla en silencio cuando sí.
3. **Paper ≠ LIVE en filtros**. Los pre-checks tienen que ser idénticos
   o el "test" no valida nada. Antes de hoy paper era 4 filtros más
   permisivo que LIVE.
4. **Comandos Telegram con nombres confusos = bug humano**. `/killswitch`
   suena a "activar killswitch" pero hace lo opuesto. `/pause` y `/resume`
   son explícitos.

---

### 2026-05-06 (madrugada) — tuning .env + bug RECONCILED + WS + auto-block n>=10

**Hito**: sesión de optimización tras observar que el bot abría solo 1 trade
real propio en 24h (de 24 opens totales, 15 eran RECONCILED sin atribución
de wallet). Aplicadas mejoras en 3 frentes:

#### Tuning del .env (sin restart de imagen — solo `docker compose restart`)
| Var | Antes | Ahora | Por qué |
|---|---|---|---|
| `LIVE_CAPITAL_USDC` | 100 | 130 | Aprovecha balance USDC ~$144 con buffer |
| `LIVE_BASE_USDC` | 5 | 7 | Mejor ratio fees/upside |
| `LIVE_MAX_PER_WALLET_USDC` | 10 | 15 | Cortar 97 wallet_concentration/24h |
| `LIVE_MAX_SLIPPAGE_PCT` | 0.03 | 0.04 | Cortar 59 order_unmatched/24h |
| `LIVE_MIN_EXPECTED_PNL_USDC` | 0.50 | 0.40 | Con BASE=7 entra más sin fees-trap |
| `TRAIL_ACTIVATION_PCT` | 0.50 | 0.30 | Captura wins de +30% que sino reverten |
| `HL_MAX_FILLS_PER_WALLET_24H` | (default 30) | 60 | Deja swing traders no scalpers |
| `DX_MAX_FILLS_PER_WALLET_24H` | (default 30) | 60 | Idem DX |

#### Manual category block: sports-mlb
`category_perf` lo bloqueó manualmente (UPDATE directo, runner stop+start).
Razón: 0 wins / 13+ losses combinado paper+live, pero n=10 estaba debajo del
umbral n>=15 (ahora bajado a 10 también, ver código).

#### Cambios de código (commit c4bbc94)

1. **`reconciler.py` — atribución mejorada**
   Antes: cualquier fill on-chain del proxy sin row en `live_trades` quedaba
   como `source_wallet='RECONCILED'` → bandit/learning/categories ciegos.
   Ahora: cruza contra `trades` (mismo cid+outcome+side=BUY, ±60s, sub
   active/paused) y asigna el wallet más cercano si lo encuentra. Fallback a
   'RECONCILED' como antes.

2. **`executor.py` — outbox para INSERTs fallidos**
   Antes: si el INSERT a live_trades fallaba 5 veces por DB locked, el trade
   real quedaba sin tracking → SL/TP no aplicaba → reconciler lo rescataba
   como RECONCILED. Ahora: 5 retries exponenciales (0.1→1.6s) sobre 30s de
   busy_timeout. Si todos fallan, escribe el payload a
   `data/live_trades_outbox.jsonl` y notifica Telegram. El runner llama
   `drain_live_outbox()` al startup.

3. **`categories.py` — `MIN_TRADES_FOR_BLOCK` 15→10**
   Captura categorías malas más temprano. Unblock sigue requiriendo
   n>=20 (= MIN*2).

4. **`ws_bridge.py` (nuevo) + integración en `runner.py`**
   El módulo `src/polymarket/websocket.py` (PolymarketTradesWS) estaba
   standalone. Bridge nuevo conecta el WS con `tradebook.open_position`/
   `close_position` en paralelo al polling. Reduce latencia de detección de
   ~5-7s polling a <1s push. Idempotencia por `source_trade_id="ws:<txh>"`.
   Refresca watched wallets cada 60s. Activable con
   `WEBSOCKET_TRADES_ENABLED=true` (default false). Polling sigue activo
   como fallback.

#### Activación pendiente del WS
Para encender el WS post-deploy:
```bash
ssh melina@100.98.174.60
cd /c/Users/Melina/polymarket_copybot/bot_trading
echo 'WEBSOCKET_TRADES_ENABLED=true' >> .env
docker compose restart
```
Verificar en logs: `WS bridge: arrancado en paralelo (latencia reducida)`.

#### Hallazgo importante: 15 trades RECONCILED bloqueando $68 del cap
A 2026-05-06 00:30 UTC, había 15 posiciones live `source_wallet='RECONCILED'`
sumando ~$68. Son trades del bot ejecutados (proxyWallet del usuario en raw)
pero con source_wallet perdido por bug INSERT (ver fix #2 arriba). Mayoría
e-sports (LoL, CS2, Valorant) y MLB NRFI — categorías que NO estaban
auto-bloqueadas en su momento. Pendiente: settle/redeem manualmente en
polymarket.com para liberar capital del cap del bot.

---

### 2026-05-05 — incidente trades fantasma + 9 fixes críticos LIVE

**Hito**: día caótico de debug en producción real. El bot venía operando
LIVE desde el 2026-05-04 pero descubrimos múltiples bugs concurrentes
que provocaron pérdidas de ~$50 USDC en posiciones sin tracking, sin
stop_loss y sin notificaciones. **9 fixes pusheados (`755c838` →
`ce077cd`)**, sistema blindado contra los modos de falla observados.

#### Bugs descubiertos y fixes

1. **Cluster auto-block self-fulfilling** (`755c838`)
   `cluster_perf` evalúa solo con `paper_trades`. Cuando el bot copió
   mal en paper, los whales (PnL real >$300k) quedaron `blocked` para
   siempre. Fix: env `CLUSTER_BLOCK_DISABLED=true`.

2. **`order_version_mismatch` en TODO BUY** (`2cce1b6`)
   Polymarket migró el CLOB a v2 fines de abril. El SDK
   `py-clob-client@0.34.6` firma con domain version vieja. Fix: migrar a
   `py-clob-client-v2@1.0.0`. (issues GitHub #335 #336 #337).

3. **Bug redondeo shares para tick=0.001** (`f40ac03`)
   `maker_amount` con >2 decimales rechazado por server. Fix: cuantizar
   shares a múltiplos de 10 (resp. 100) según tick.

4. **`DISCOVERY_TOP_N` hardcoded** (`c4aa9dd`)
   Discovery cada hora reseteaba 40 → 20 wallets. Fix: env configurable.

5. **Errores transient SDK floodeaban Telegram** (`b85f4c8`)
   Logs ERROR del SDK CLOB v2 (timeouts, FAK no_match, 404) triggereaban
   notifs spam. Fix: subir level del logger del SDK a CRITICAL.

6. **TRADES FANTASMA — bug central** (`4480d2f`)
   Bot ejecutó ~17 BUYs on-chain pero solo 5 quedaron en `live_trades`.
   12 posiciones (~$80) **sin tracking → sin SL/TP → -$50+ perdidos**.
   Causas concurrentes:
   - SQLite "database is locked" con 3 runners paralelos
   - SDK CLOB devolvía exception cuando la orden SÍ filleó parcialmente
   - Sin auditoría persistente de qué BUYs se intentaron

   Fixes:
   - `db/schema.py`: WAL mode + `busy_timeout=30s` + `_retry_locked()` para BEGIN/COMMIT
   - `polymarket/clob_client.py`: `_outbox_log()` JSONL persistente en
     `data/orders_outbox.jsonl` + `_resp_indicates_fill()` que detecta
     tx_hash, makingAmount>0, status=matched/filled (recupera success
     aunque el SDK tire exception)
   - `copybot/reconciler.py` (NUEVO): cada 5 min compara trades on-chain
     del proxy via Data API contra `live_trades`. Auto-INSERTa los
     fantasma con `source_wallet='RECONCILED'`. Notif Telegram cuando
     encuentra. Sin esto, máximo 5 min de gap de tracking.
   - `runner.py`: hook `reconcile_once()` cada 5 min en loop principal
   - `copybot.py`: comando `reconcile` para correr manual

7. **Telegram listener async se colgaba** (`2c6d35f`, `a98816d`)
   Listener arrancaba pero NO consumía updates ni respondía a `/status`.
   La task moría silenciosamente porque `asyncio.create_task` no propaga
   excepciones. Múltiples intentos con `asyncio.wait_for` no funcionaron
   — `httpx.AsyncClient` se colgaba indefinidamente, probablemente por
   pool exhaustion / starvation con HL/DX/PM compitiendo en el event
   loop.

   Fix definitivo: refactor a **sync mode** con `requests` library
   ejecutado en thread separado vía `asyncio.to_thread()`. Aislamiento
   completo del event loop.

8. **Retry en `_log_reject`** (`a98816d`)
   Aún con WAL+30s, edge cases de "database is locked". Fix: retry
   hasta 5 veces con backoff antes de loggear failure.

9. **`_last_price` ignoraba el filtro asset → SL nunca disparaba** (`ce077cd`)
   `data-api.polymarket.com/trades?asset=...` IGNORA el parámetro asset
   y devuelve trades aleatorios del proxy. Resultado: TODAS las
   posiciones open recibían el mismo precio fake (~0.79) → drop
   calculado siempre negativo → **stop_loss y trailing NUNCA disparaban**.
   Bug catastrófico. Esto explica HAVU cayendo al 0% sin que el bot
   hiciera nada, NY Yankees a -44% sin SL, etc.

   Fix: usar `clob.polymarket.com/midpoint?token_id=...` (filtra correcto
   por token_id) con fallback a `/price?side=SELL`.

#### Mejoras de configuración aplicadas (en `.env` de Lenovo)

| Var | De | A |
|---|---|---|
| `LIVE_BASE_USDC` | 10 | 5 |
| `LIVE_MAX_PER_WALLET_USDC` | 20 | 10 |
| `HL_SLEEP_SECONDS` / `SWEEP` | 5/30 | 10/60 |
| `DX_MIN_EXPECTED_PNL_USDC` | 0.20 | 0.40 |
| `MIN_TIME_TO_EXPIRY_SECONDS` | 600 | 60 |
| `TRAIL_DROP_PCT` | 0.25 | 0.35 |
| `TRAIL_ACTIVATION_PCT` | 0.30 | 0.50 |
| `HL_CAPITAL_USDC` / `BASE` | 50/5 | 100/10 |
| `DX_CAPITAL_USDC` / `BASE` | 50/5 | 100/10 |
| `DISCOVERY_TOP_N` | 20 | 40 |
| `CLUSTER_BLOCK_DISABLED` | (no) | true |
| `LIVE_DRY_RUN` | true | **false** (LIVE real) |

Filtros del selector relajados en `filter_thresholds`:
- `MIN_SCORE` 0.65 → 0.45
- `MIN_WIN_RATE` 0.65 → 0.45
- `MIN_TOTAL_TRADES` 198 → 50
- `MIN_VOLUME` 36k → 5k
- → candidatos disponibles: 20 → **239**

#### Estado al cierre del día

- ✅ Bot operando LIVE real con SDK v2
- ✅ 40 wallets activos (whales + scalpers mixto)
- ✅ Reconciler cada 5min auto-recupera trades fantasma
- ✅ DB con WAL + retry — robusta contra locks
- ✅ Outbox audit log persistente
- ✅ `_last_price` con endpoint correcto — SL/TP empiezan a disparar
- ✅ Telegram listener (sync mode) responde comandos
- ✅ Notifs OUT (gain/loss/startup/error) funcionan

#### Cuenta

- Cartera: $143.48 (post depósito $120 USDC durante el día)
- Disponible operar: $139.10
- Pérdidas del día: ~$50 (trades fantasma sin SL antes del fix `ce077cd`)
- 2 posiciones residuales con tracking activo

#### Próximos pasos

- Validar que SL/TP disparen con `_last_price` arreglado
- Acumular 50+ trades para análisis real
- Considerar bloquear categorías perdedoras
- Auditar `data/orders_outbox.jsonl` semanalmente vs `live_trades`

---

### 2026-05-04 — fix LIVE: API creds inválidas + tuning HL/DX/PM live

**Hito**: el modo LIVE real estaba **roto desde el 2026-05-01**. Todos los
BUY del bot fallaban con `order_version_mismatch` y nunca se ejecutó
ninguna orden real. Identificada la causa raíz, regeneradas las creds
y aplicadas mejoras de rate limiting + tamaños conservadores.

#### Problema (root cause)
- En el `.env` de la Lenovo había `POLYMARKET_API_KEY/SECRET/PASSPHRASE`
  **inválidas** (probablemente expiradas o de otra cuenta). Devolvían 401
  en `/balance-allowance`, `/orders`, `/trades` y derivados.
- En `/order` (post de órdenes), las creds pasaban auth parcial pero el
  server validaba el `signer` field contra el dueño de las creds y
  rechazaba con `order_version_mismatch` (HTTP 400).
- Síntomas observados: `live_summary.balance_usdc=null`, `live_trades=0`,
  13 BUY rejects en 7 días, ningún trade real ejecutado nunca.

#### Diagnóstico
- Verificado que la EOA derivada del `POLYMARKET_PRIVATE_KEY`
  (`0x17BBf714cc...58bce7`) **es** la owner Magic Link de la cuenta
  `rafalezcano72@gmail.com` (proxy `0xC44a79BC...8Db9C` con $99.19).
- Verificado on-chain que la wallet opera CLOB perfecto desde la UI
  (3 trades CONFIRMED en últimos 2 días).
- El allowance ERC20 USDC del proxy reporta $0 — pero **es falso
  positivo**: Polymarket POLY_PROXY usa meta-tx via relayer, no
  allowance ERC20 tradicional. No requiere fix.

#### Fix
1. Regeneradas las creds API vía el proxy Vercel
   (`bot-trading-lemon.vercel.app/clob`) que bypassa el WAF de Polymarket.
   Sig type confirmado = `1` (POLY_PROXY).
2. Reemplazadas las 3 vars en `.env` de Lenovo + backup
   `.env.bak.precfix`.
3. `docker compose restart` en bot_trading-{runner,server}-1.
4. Verificado: `live_summary.balance_usdc=$97.15` (ya no null), 0 errores
   `order_version_mismatch` post-restart.

#### Mejoras aplicadas en la misma ventana
| Var | De | A | Por qué |
|---|---|---|---|
| `LIVE_BASE_USDC` | 10.0 | **5.0** | Conservador para 1ras 24-48h en real |
| `LIVE_MAX_PER_WALLET_USDC` | 20.0 | **10.0** | Idem (cap exposure por wallet) |
| `HL_SLEEP_SECONDS` | (default 5) | **10** | 904 errores 429/h en HL — saturación |
| `HL_SWEEP_SECONDS` | (default 30) | **60** | Idem |
| `DX_MIN_EXPECTED_PNL_USDC` | 0.20 | **0.40** | DX en -$1.52 hoy con sample chico — más estricto |

#### Siguiente
- Monitorear las 24-48h en LIVE real. Si el bot ejecuta y los rejects
  siguen en 0, subir gradualmente `LIVE_BASE_USDC` hacia 10.
- Si DX sigue rojo después de 100 trades de sample, considerar pausar
  con `DX_MODE=false`.
- Telegram va a notificar cada cierre con 🟢 GANADO / 🔴 PERDIDO + PnL
  acumulado + link a polygonscan tx.

#### Notas operativas
- SSH a Lenovo: `ssh melina@100.98.174.60` (Tailscale). Pass en sección
  "Conexión Mac ↔ Lenovo" de este doc.
- El bot corre como 2 contenedores: `bot_trading-runner-1` (loop) y
  `bot_trading-server-1` (FastAPI). `docker compose restart` los reinicia
  en ~5s.
- El cron `update_and_restart.bat` cada 5 min hace `git pull` + redeploy,
  así que cualquier commit a `main` propaga al bot en <5min.

---

### 2026-05-03 — 3er bot dYdX v4 + dashboard 3 tabs + production parity

**Hito**: agregamos el **3er bot** (dYdX v4) en paralelo a PM y HL, en
dry-run. Plan: validar 7d en dry-run con **parity total** vs producción
y pasar a real **el martes**. Esto requirió cerrar todos los gaps de
realismo que el HL bot tenía abiertos (gas, funding, fill price).

#### Bot dYdX v4 (paralelo, dry-run)
- `src/dydx/client.py`: async wrapper de `https://indexer.dydx.trade/v4/`
  (perpetual_markets, fills, positions, orderbook, trades,
  clearinghouse_state, candles). dYdX **NO geo-bloquea** desde AR.
- `src/copybot/dx_executor.py`: dry-run open/close/force_close mirror de
  hl_executor. Validaciones: kill_switch (compartido con PM/HL), sub
  status, ticker allowlist, anti-scalper, duplicate, leverage cap,
  expected_pnl, wallet/global capital.
- `src/copybot/dx_runner.py`: `dx_run_loop()` async — polling parallel
  (asyncio.gather) + sweep periódico SL/TP/trailing/liquidation. Cursor
  `dx_cursor:<wallet>` en milisegundos.
- `_classify_fill` infiere open/close por presencia de posición existente
  en el wallet (dYdX fills no traen flag de open vs close).
- 3 tablas nuevas: `dx_trades`, `dx_subscriptions`, `dx_rejects`. Schema
  migrado idempotente.
- 17 env vars `DX_*` con defaults seguros. `DX_MODE=false` por default.
- Notif `dx_close` con prefix `🟣 [DX]`. Threshold `DX_NOTIF_MIN_PNL=0.30`.

#### Discovery automática de wallets dYdX
Usuario: "no quiero hacer el trabajo manual de entrar, copiar etc."
Caminos fallidos antes de la solución:
- Endpoint `/trades` del indexer: NO devuelve addresses (solo size/price).
- `tx_search` RPC del Cosmos: timeout en 50+ segundos.
- Numia, Mintscan, DefiLlama: requieren auth, paywall, o no traen users.
- Parsing de bloques con protobuf: demasiado complejo (Cosmos SDK msg
  schemas para `MsgPlaceOrder` requieren protoc + bindings custom).

**Solución**: endpoint LCD `/dydxprotocol/subaccounts/subaccount`:
```python
async with httpx.AsyncClient() as client:
    r = await client.get(f"{LCD}/dydxprotocol/subaccounts/subaccount", params=params)
```
Paginado 50 pages → 937 candidatos encontrados. Top 10 por equity
insertados en `dx_subscriptions` (rango $7.2M down to $9k).

#### Dashboard 3 tabs (Poly / Hyperliquid / dYdX)
- `src/api/server.py`: nuevos endpoints `/api/hl/summary`, `/api/hl/trades`,
  `/api/dx/summary`, `/api/dx/trades`. Mirror de los `/api/live/*`.
- `src/api/static/app.js` reescrito: `tabData` getter switcheable entre
  los 3 bots. Fetch parallel de los 7 endpoints. Refresh c/10s.
- `src/api/static/index.html` reescrito: 3 botones tab (Poly/Hyperliquid/dYdX)
  con estilos color-coded (emerald/blue/purple). Layout idéntico por tab
  reusando `tabData` reactivo.
- SW bumpeado a v10 (`copybot-v10-multi-tab`) para invalidar cache vieja.

#### Production parity (cierra el gap dry-run vs real)
Usuario: "haz los cambios necesarios para que no haya diferencia entre
nuestras pruebas y el entorno real, que sea exactamente igual, el martes
pasamos al entorno real."

3 gaps cerrados (HL y DX en paralelo):
1. **Gas explícito por fill**:
   - Schema: `dx_trades.gas_paid REAL DEFAULT 0` y `hl_trades.gas_paid` (idem).
   - `open_position` graba `gas_paid = *_GAS_PER_FILL_USDC` al abrir.
   - `close_position` suma otro fill al cerrar y descuenta del PnL net.
   - DX: `DX_GAS_PER_FILL_USDC=0.02` (Cosmos gas average por tx).
   - HL: `HL_GAS_PER_FILL_USDC=0.0` (no cobra gas explícito, settlement L1).
2. **Funding rate hourly accrual**:
   - Schema: `dx_trades.funding_paid REAL DEFAULT 0` (HL ya lo tenía).
   - Módulo nuevo `src/copybot/dx_funding.py`: task async cada
     `DX_FUNDING_UPDATE_HOURS` (default 1). Por cada posición open
     fetcha funding rate del market y acumula
     `funding_incremental = size_usdc × funding_rate × elapsed_h / 8`.
     Hookeado en `dx_run_loop`.
   - Mismo módulo paralelo `hl_funding.py` para HL.
   - Al cerrar: `pnl_net = pnl_gross - gas_total - funding_paid`.
3. **Orderbook-based fill price**:
   - `dx_runner._process_wallet` antes de `open_position` fetcha
     orderbook (`/orderbooks/perpetualMarket/{ticker}`) y walks levels
     para size = `DX_BASE_USDC × sizing_mult`. Computa VWAP real.
   - Simula `await asyncio.sleep(2.0)` antes del open: es el delay típico
     entre que detectamos el fill del trader y nuestro tx llega al
     mempool. Refetch orderbook tras la latencia para que el precio
     refleje los movimientos del libro durante esos 2s.
   - `realistic_entry_price` se pasa a `open_position` como kwarg
     opcional. Si vacío → fallback a slippage simple (compat).
   - Ídem `close_position` con `realistic_exit_price`.
   - Toggle vía `DX_USE_ORDERBOOK_FILL=true` (default on).

**Justificación**: martes pasamos plata real. Si el dry-run no descuenta
gas+funding y usa precios "perfectos" del fill del trader original, el
PnL teórico está sobreestimado. La parity garantiza que un trade que
sale +$0.30 en dry-run sale ~+$0.30 en real (con margen de variabilidad
del orderbook entre ticks).

#### Anti-scalper filter (HL + DX)
Hallazgo: wallets HFT (`0x010461c14e..` en HL, varios en DX por LCD top
equity) generan ~-$0.01 / fill puro slippage cost y spammean Telegram.
Ejemplos: 4 wallets HL con 2143/278/220/132 fills/24h causaron -$30 en
24h.

- Reject `scalper_wallet` si fills 24h > `*_MAX_FILLS_PER_WALLET_24H=30`.
- Auto-drop si supera 60 (umbral más alto que reject para no tocar
  wallets que están "raspando" el threshold).
- Threshold `*_NOTIF_MIN_PNL=0.30` para silenciar notifs de micro-PnL.

#### PM cluster_blocked false positive (cleanup)
- Investigación: 24h con 0 trades del bot, 101 rejects `cluster_blocked`.
  Diagnóstico mostró que TODOS venían de un solo wallet
  (`0x2eb8b11603f9..`) que ya estaba dropped. La regla de cluster era
  correcta pero la métrica era cosmética porque ese wallet no entraba.
- Real issues: `low_liquidity` en mercados thin, `expires_too_soon` en
  binarios cortos, `sport_only` en wallets que solo trabajan deportes.
- **Fixes**:
  - Liquidity threshold `MIN_MARKET_LIQUIDITY_USDC=5000 → 1500` para
    permitir entrar en mercados intermedios.
  - Sizing dinámico `0.5×` en mercados con liquidez `$1500-3000` (no usar
    full size).
  - Drop manual de 3 wallets sport-only (clog del top sin actividad real
    en politics/crypto).

#### Activación de la parity en lenovo (post-deploy `7a85c82`)
Para encender las nuevas funciones agregar al `.env` de lenovo:
```env
HL_USE_ORDERBOOK_FILL=true
DX_USE_ORDERBOOK_FILL=true
HL_FUNDING_UPDATE_HOURS=1
DX_FUNDING_UPDATE_HOURS=1
```
Los defaults en `config.py` ya están seteados (`orderbook=true`, gas y
funding accrual on), así que si NO editás el `.env` igual arranca con
los nuevos comportamientos. Las vars del `.env` solo son necesarias si
querés overridear (ej. apagar el ob walk durante un debugging:
`*_USE_ORDERBOOK_FILL=false`).

Verificación post-deploy:
- `docker logs bot_trading --tail 50 | grep "DX funding\|HL funding"` —
  debería loguear `arrancando (cada 3600s)` al startup de cada runner.
- Tras 1h de uptime con posiciones open: el log muestra
  `DX funding: N posiciones actualizadas` (idem HL).
- Al cerrar un trade: `pnl_usdc` en `dx_trades`/`hl_trades` ya
  trae descontados gas + funding.

### 2026-05-02 — Bot Hyperliquid paralelo + mejoras PM

**Hito**: bot Hyperliquid corriendo **en paralelo** al PM real, en dry-run
para validar producción con plata ficticia.

#### Hyperliquid bot (paralelo, dry-run)
- Nuevo módulo `src/hyperliquid/client.py`: async wrapper de `/info` (meta,
  allMids, userFills, clearinghouseState, candles). HL **NO geo-bloquea**
  desde AR — operamos directo desde lenovo, sin proxy.
- `src/copybot/hl_executor.py`: dry-run open/close/force_close con todas
  las validaciones (kill_switch compartido con PM, sub status, coin
  allowlist, leverage cap, expected_pnl, wallet/global capital, duplicate
  source_fill_id). Slippage pesimista, liquidation buffer.
- `src/copybot/hl_runner.py`: `hl_run_loop()` async — polling paralelo
  (gather) + sweep periódico SL/TP/trailing/liquidation. Hookeado en
  `runner.run_loop` cuando `HL_MODE=true`.
- 3 tablas nuevas: `hl_trades`, `hl_subscriptions`, `hl_rejects`. Schema
  migrado idempotente.
- 16 env vars `HL_*` con defaults seguros. `HL_MODE=false` por default.
- Notif `hl_close` con prefix `🔵 [HL]` para distinguir de PM. Threshold
  `HL_NOTIF_MIN_PNL=0.30` para suprimir micro-trades de scalpers.

**Lecciones operativas con HL**:
- Slippage **0.5% es muy alto para perps** — Hyperliquid tiene spreads
  apretadísimos. Bajado a 0.1% (`HL_DRY_SLIPPAGE_PCT=0.001`).
- Allowlist hardcoded `BTC,ETH,SOL` rechazaba 99% del volumen (los wallets
  operan mayormente memecoins). Vaciada (`HL_ALLOWED_COINS=`) para permitir
  todas.
- Seed wallets de internet random (sin verificar) eran inútiles: 9/10
  inactivas. Discovery on-the-fly via `recentTrades` de BTC/ETH/SOL/HYPE
  encontró 18 wallets reales con 2000+ fills/24h.
- Wallets HFT scalpers (ej. `0x010461c14e..`) generan -$0.01 por trade
  (puro slippage cost) y spammean Telegram. **Drop manual** de scalpers +
  threshold de notif `>=$0.30` para silenciarlos en background.

#### Multicast Telegram
- `notifier.send`: parsea `TELEGRAM_CHAT_ID` por coma para multicast.
  Owner + amigos pueden suscribirse. Si un chat falla, los demás siguen.
- `telegram_listener._authorized_chat_id`: solo el PRIMER chat_id puede
  ejecutar comandos (`/status`, `/killswitch`). Los amigos reciben
  notifs read-only.

#### Bot token comprometido y regenerado
- Bot `bonny21bot` fue **hijackeado** por un atacante que le seteó un
  webhook a `webhook.sherlock.st` (servicio ruso de búsqueda de personas).
  Síntoma: `/start` y `/killswitch` no respondían (webhook robaba updates),
  pero `sendMessage` funcionaba (notifs salían normal).
- Detectado vía `getWebhookInfo` que devolvió URL del attacker.
- **Fix**: `deleteWebhook` + revocar token vía BotFather + nuevo token al
  `.env`. **No commitear nunca el token a git** — solo en `.env` lenovo.

#### Mejoras Polymarket bot
- **Auto-drop por inactividad**: `learning.auto_drop_by_inactivity()` —
  drop wallets activos cuyo `paper_cursor` no se movió en >48h. El cursor
  avanza on-poll cuando hay /trades nuevos; si está flat = wallet sin
  actividad on-chain = drop. Hook cada ~6h en runner.
- **Shadow tracker**: nueva tabla `shadow_trades` + módulo
  `shadow_tracker.py`. Pollea wallets dropped cada ~1h y registra sus
  trades observados (sin copiar). Para análisis a posteriori — si en 1
  semana descubrimos que un wallet dropped reapareció y tradeó bien,
  sabemos que el threshold de inactividad fue agresivo.
- **Discovery on-idle**: si 0 trades del bot en últimas 6h, fuerza
  `discovery_pending=true` para refrescar el top.
- **Drops permanentes**: confirmado el comportamiento — una vez dropped
  (por cualquier razón), el wallet no se reactiva. Fue clave para
  estabilizar el sistema.

### 2026-04-27 — Migración Mac → Lenovo
- Decisión: Git + GitHub + GHCR en vez de rsync (más simple, audit trail)
- Mac → GitHub privado `rafalez72/bot_trading`
- Lenovo: Docker Desktop + clone repo + scp del .env + scp de la DB inicial
- Task Scheduler con `update_and_restart.bat` cada 5 min

### 2026-04-28 — Fase 5: live trading construido
- `src/polymarket/clob_client.py`: wrapper py-clob-client con health_check, place_market_order (FOK/FAK), get_balance
- `src/copybot/executor.py`: mirror de paper.py para órdenes reales en CLOB
- `src/copybot/tradebook.py`: dispatcher que elige paper/executor según `LIVE_MODE`
- Tabla `live_trades` con token_id, order_id, tx_hash, fees_usdc, dry_run
- CLI nuevo: `run-live`, `check-live`, `live-status`
- Notifs: `live_open`, `live_close`, `live_error` con tx hash

### 2026-04-28 (tarde) — 4 mejoras Tier 1 anti-fricción
- IOC + retry escalonado (FAK con precio +1% si <50% fill)
- Pre-check orderbook con VWAP estimada (aborta si slippage > 3%)
- Cap por wallet ($10/wallet sobre $30 cap → max 3 wallets concurrentes)
- Filter expected_pnl > $0.50 (evita trades donde fees comen upside)
- Gas calibrado: $0.02 → $0.05 (más realista para CTF Exchange)

### 2026-04-28 (noche) — Tab "Live" en dashboard
- 3 endpoints `/api/live/*` (summary, trades, pnl-timeline)
- Tab "Live" en PWA con banner de modo, stats wins/losses, dry vs real, top wallets
- Bottom nav reorganizado a 5 columnas (Paper / Copiando / Live / Top / Mercados)
- Service worker bumpeado a v6 para invalidar cache vieja

### 2026-04-29 (madrugada) — Switch a live_dry + correcciones
- `.env` de Lenovo configurado con `LIVE_MODE=true LIVE_DRY_RUN=true` + creds
- `check-live` OK: balance $0.00 (esperado, wallet sin fundear)
- Primer trade dry-run: -$2.09 (stop-loss 60%)
- Segundo dry-run: -$1.90 (stop-loss 54%)
- **Bug encontrado**: kill switch contaba dry-run trades como reales → activación incorrecta
- **Fix** (`ac48539`): kill switch ignora `dry_run=1` en live mode
- Notif live REAL ahora con formato simple tipo paper: "📈 Ganancia REAL / Ganó: $X / Acumulado: $Y"

### 2026-04-29 (mañana) — Principio "dry-run = real" + telegram listener + min-sample auto-tune
- **Decisión nueva**: el dry-run del live es la última prueba antes de plata real,
  por lo tanto debe comportarse IDÉNTICO. Esto invalida el fix de `ac48539`.
- **Revert lógico de `ac48539`**: `risk.check_kill_switch` vuelve a contar
  trades dry-run en el rolling 24h. Si los dry-run pierden mucho, el bot
  se autopausa exactamente como pasaría en real (validación legítima).
- **`auto_filter` ahora lee de la TABLA ACTIVA** (`live_trades` en live,
  `paper_trades` en paper) en vez de paper hard-coded — si no, el módulo
  no aprendía de los trades del live y los thresholds se movían por
  evidencia desconectada del modo actual.
- **Nuevo umbral mínimo de muestra**: `MIN_SAMPLE_FOR_TUNE = 30` cierres
  antes de mover thresholds (era 20). Evita que 2-3 trades disparen un
  endurecimiento desproporcionado, problema observado el 29/04 05:22 UTC
  cuando un auto-tune basado en una muestra contaminada subió MIN_SCORE
  de 0.55 a 0.85 y dejó solo 3 wallets activas.
- **Comando CLI nuevo `reset-thresholds`** para volver al baseline
  (DEFAULTS) cuando un auto-tune fue espurio. Aplica DELETE del cooldown
  y agrega un evento de `learning_events` para auditoría.
- **Telegram listener** (`src/copybot/telegram_listener.py`): long polling
  como tarea async dentro del runner. Comandos `/status`, `/killswitch`,
  `/resetkill`, `/help`. Solo responde al `TELEGRAM_CHAT_ID` configurado.
- Reset puntual del 29/04: thresholds volvieron a `0.55/0.55/$25k/150` y
  `select --top 20` reabrió ~20 suscripciones. Esto es un one-time fix
  porque la muestra (2 trades) era estadísticamente ruido, no evidencia.

### 2026-04-29 (mediodía) — Kill switch con high-water mark al reset
- Validación del listener: usuario probó `/killswitch` desde Telegram,
  el bot respondió OK pero al toque se reactivó porque el rolling 24h
  seguía mostrando -$3.99 (los 2 trades viejos del setup inicial).
- Comportamiento técnicamente correcto bajo "dry = real" pero impráctico
  para validación: el bot quedaba pausado hasta que el rolling 24h se
  vaciara solo (~12-15h).
- **Cambio**: `risk._set_kill(False, …)` ahora persiste un timestamp
  `bot_state.kill_switch_reset_at`. `check_kill_switch` evalúa la
  ventana `[max(now-24h, reset_at), now]`. Los trades viejos no se
  borran (siguen en `live_trades`/`paper_trades` para histórico) pero
  no vuelven a disparar el kill switch.
- Si después del reset hay nuevas pérdidas que superan -10% del cap,
  se reactiva con razón `"PnL desde reset $-X.XX <= -10%..."`.
- Cubre tanto reset por CLI (`reset-killswitch`) como por Telegram
  (`/killswitch`) — ambos pasan por la misma función.

### 2026-04-29 (tarde) — Diagnóstico "examined>0 pero 0 OPEN" + cap $30→$100
- Síntoma: post deploy del listener + reset thresholds, el bot tenía
  20 wallets activas, los cursores avanzaban, pero `live_trades`
  seguía con solo los 2 trades viejos. Cero aperturas nuevas durante
  ~30 min.
- Verificación contra Data API: 8 de los 20 wallets activos hicieron
  47 BUYs en los últimos 30 min. El bot SÍ los veía (cursores
  actualizados) pero los rechazaba todos.
- Diagnóstico con `LOG_LEVEL=DEBUG`: causa principal era
  `expected_pnl_too_low`. Con `LIVE_BASE_USDC=2.5` y los `sizing_mult`
  del bandit (mayoría < 1.0), el `realism.expected_net_pnl(size_eff)`
  devolvía ~$0.41, debajo del threshold $0.50 → todos los BUYs
  rechazados silenciosamente. Causa secundaria: `cluster_blocked`
  (algunos clusters siguen bloqueados por mala perf histórica).
- **Solución (sin código nuevo, solo .env)**: subir cap a $100 y base
  a $5 — los valores que validamos en paper. Math:
  ```
  size_eff = 5 * 0.78 = 3.9
  net = 1.06 - 0.05 - 0.10 = ~0.92  ✓ pasa $0.50
  ```
- Resultado: trade #3 abierto a las 15:21 UTC, wallet `0x6f84f94c…`,
  BUY @ 0.500, size $7.83 (= 5 × 1.57 sizing_mult).
- Lección: el threshold `LIVE_MIN_EXPECTED_PNL_USDC` no escala con
  `LIVE_BASE_USDC`. Si en algún momento bajamos cap o base, hay que
  ajustar el threshold proporcionalmente para no quedar en zombie mode.

### 2026-04-29 (tarde) — Bug: notif Telegram silenciosa en SL/TP/settle live
- Síntoma: usuario reporta 4 trades cerrados (3 closed_loss, 1 closed_win
  +$8.44 take_profit_112pct) pero CERO notificaciones de Telegram al
  cerrarse.
- Causa: `executor.close_position` SÍ llamaba `notifier.live_close()`,
  pero `executor.force_close()` (que dispara los SL/TP del sweep) y
  `executor.settle_resolved()` (cierre por mercado resuelto) NO lo
  hacían. Como casi todos los cierres reales en dry-run son por SL/TP
  (los wallets copiados rara vez hacen SELL exacto que matchee el flow
  de `close_position`), el usuario nunca recibía notif.
- Fix: agregar la llamada a `live_close()` en ambas funciones, con el
  mismo cálculo de `accumulated` y lookup del `slug` que ya hacía
  `close_position`. `settle_resolved` ahora itera y manda una notif
  por cada trade settleado.
- Bug equivalente en paper: `paper.force_close` y `paper.settle_resolved`
  llaman a `on_paper_trade_closed()` que SÍ manda `gain`/`loss` desde
  `learning.py`. Por eso el paper nunca tuvo este bug — solo el live.
- Detalle: en live también se podría centralizar via un hook similar
  (ej. `on_live_trade_closed`) pero lo mantengo simple — son 6 líneas
  duplicadas en 3 sitios.

### 2026-04-30 (madrugada) — Latencia, observabilidad y tests
- **Polling paralelo (`fd3710f`)**: `runner.run_loop` polea los 20 wallets con
  `asyncio.gather` en vez de secuencial. Ciclo cae de ~10s → ~1s. Habilita
  bajar `COPY_POLL_SECONDS=10→5` con efecto real.
- **`live_rejects` table + `_log_reject` helper**: cada path de rechazo en
  `_open_position_validate` registra reason + detail JSON. Antes el bot
  rechazaba trades en silencio, ahora hay traza para diagnóstico.
- **Slippage pesimista en dry-run** (`LIVE_DRY_SLIPPAGE_PCT=0.015`):
  BUY ↑1.5%, SELL ↓1.5%, clamp [0.01, 0.99]. Los números del dry-run
  ahora proyectan lo que realmente sale en real.
- **Suite pytest inicial** en `tests/`: 16 tests para risk + executor con
  fixture `isolated_db` (monkeypatch DB_PATH a tmpfile). Cubre kill switch
  lifecycle, rejects, force_close.
- **WebSocket client RTDS standalone** (`src/polymarket/websocket.py`):
  foundation para latencia <1s vs 5-7s polling. Filter client-side por
  `proxyWallet`. Flag `WEBSOCKET_TRADES_ENABLED=false` — no integrado aún.
- **Investigación webhooks Polymarket**: `wss://ws-live-data.polymarket.com`,
  topic `activity:trades` es global (sin filtro por wallet server-side).

### 2026-04-30 (mañana) — Análisis post-deploy y bugs críticos
**Hallazgo brutal del análisis**: tras 8h en `live_dry`, PnL real = **-$26.42**
(no el +$17 que parecía con posiciones abiertas mark-to-market). WR=21%,
9 wins / 34 losses. **2 wallets concentraban 56% de las pérdidas**.
- **Mercados `*-5m-*` matando todo**: 10/10 trades con SL ≥70% fueron
  binarios crypto de 5min (`btc-updown-5m-...`, `eth-updown-5m-...`). Estos
  expiran rápido y la posición perdedora va a $0 antes que dispare el SL.
  - **Fix**: nuevo reject `short_duration_market` en `_open_position_validate`
    que bloquea slugs matching `-(?:1|5|10|15)m-`.
- **Auto-drop NO funcionaba en live (bug crítico)**: `learning.py` y
  `bandit.py` querían literalmente `paper_trades`. En `live_dry` los trades
  van a `live_trades` → la lógica de drop NUNCA disparaba en live. Por eso
  `0x54542e00..` con 19 closes -$40 seguía activo.
  - **Fix**: ambos archivos usan ahora `tradebook.TABLE` (la tabla activa
    según `LIVE_MODE`). `DROP_AFTER_LOSSES` bajado 5→3. Nuevo trigger
    `cumulative_pnl`: si total cerrado <= -$10 con ≥5 trades → drop.
- **Tightening de risk params**:
  - `STOPLOSS_SWEEP_SECONDS=60→15` (4× más rápido)
  - `STOP_LOSS_PCT=0.30→0.20` (cap pérdidas más temprano)
  - `TAKE_PROFIT_PCT=0.80→0.50` (más wins, más chicos)
  - `LIVE_BASE_USDC=5→10` (justifica fees, ROI absoluto mayor)
- **Drops manuales** de `0x54542e00..` y `0xd28d57ae..` (ya estaban dropped
  por el auto-drop fix, pero se forzaron para limpiar).
- **Bug del kill switch race**: tras reset, había un race entre el commit
  del reset y el siguiente `check_kill_switch` del runner que reactivaba
  con datos viejos. **Fix** (`1ee0327`):
  - `RESET_GRACE_SECONDS=5`: si reset hace <5s, no reactivar.
  - **Auto-recovery**: si `pnl > threshold` y kill switch activo, lo
    desactiva automáticamente (antes era manual-only y atascaba).

### 2026-05-01 (tarde) — 🚀 LIVE REAL ACTIVADO
**Día épico** de descubrimientos y pivots para llegar a operar plata real.
Resumen de lo aprendido y resuelto:

#### Bloqueo geográfico de Polymarket
Polymarket bloquea con Cloudflare WAF **agresivamente** cualquier IP
de Argentina + datacenters conocidos para los endpoints autenticados
del CLOB (`/auth/api-key`, `/balance-allowance`, `/order`, etc.).
Probado y bloqueado:
- IPs argentinas (Telecom, etc.)
- Cloudflare WARP (sale por AR)
- ProtonVPN free (M247 pool — tanto US como RO)
- Cloudflare Workers (todo el pool de outbound IPs del usuario)
- GitHub Actions (Linux/Win/Mac/ARM — todo Azure)
- Codespaces (Azure)

**Solución que funcionó**: **Vercel Edge Functions** (subdomain `bot-trading-lemon.vercel.app`)
con `vercel.json` rewrite `/clob/:path*` → `/api/clob?p=:path*` para
soportar multi-segment paths (workaround a la limitación del catch-all
`[...path]` que no resuelve correctamente con segmentos múltiples).

Bot configurado vía `CLOB_API` env var: `https://bot-trading-lemon.vercel.app/clob`.
Cada request del bot va por proxy → Vercel edge (US) → Polymarket
(intra-CF, sin geo-block).

#### Descubrimiento del bug del funder
La address que Polymarket muestra en **Profile → Settings → Dirección**
(con el cartel "Esta dirección es solo para uso de API") **NO es la misma**
que la del Deposit screen (que dice "Your deposit address"):
- **Deposit address** (`0xC76730D81B...`): inbox que recibe USDC. NO usar
  para API.
- **API address** (`0xC44a79BCe3...`): la wallet de trading real que el
  CLOB consulta para balance/orders. Esta es la que va en `POLYMARKET_FUNDER_ADDRESS`.

Si pasamos la deposit address, el balance API devuelve siempre $0 (porque
la deposit address NO es la wallet de trading, solo el receiving inbox).

#### Descubrimiento del sig_type
La cuenta del usuario está en **POLY_PROXY antiguo (sig_type=1)**, no en
Gnosis Safe (sig_type=2). Esto explica por qué con sig_type=2 daba 401
"Unauthorized/Invalid api key" aún con creds válidas — la firma EIP-712
no matcheaba el binding del usuario en Polymarket.

Test confirmatorio (probando los 3 sig_types contra la API address):
- sig_type=2 + API addr: balance=$0 (bind incorrecto, devuelve 0 en vez de error)
- sig_type=1 + API addr: **balance=$99.19 con allowances unlimited** ✅
- sig_type=0 + API addr: balance=$0

#### Configuración final que funcionó
```env
POLYMARKET_PRIVATE_KEY=0xbc380...   # PK de Polymarket oficial (no la vieja 0x0535ec...)
POLYMARKET_FUNDER_ADDRESS=0xC44a79BCe3805A3af056522Ae9BD805F6Da8Db9C  # API addr del Profile
POLYMARKET_SIG_TYPE=1               # POLY_PROXY (NO 2)
POLYMARKET_API_KEY=e82b2454-...
POLYMARKET_API_SECRET=Zo0xSdTl...
POLYMARKET_API_PASSPHRASE=3c62f4ac...
LIVE_MODE=true
LIVE_DRY_RUN=false                  # ← REAL
LIVE_CAPITAL_USDC=100.0
LIVE_BASE_USDC=10.0
CLOB_API=https://bot-trading-lemon.vercel.app/clob   # ← proxy Vercel
```

`check-live` confirma:
```
✓ CLOB conectado
Funder wallet: 0xC44a79BCe3805A3af056522Ae9BD805F6Da8Db9C
Balance USDC:  $99.19
Sig type:      1
Host:          https://bot-trading-lemon.vercel.app/clob
```

#### Wipe del PnL para monitoreo limpio
Antes de switch a real, hicimos:
- `live_trades` → backup tabla `live_trades_dryrun_<ts>`
- `live_rejects` → backup tabla `live_trades_dryrun_<ts>_rejects`
- `live_trades` y `live_rejects` truncadas (PnL=$0 fresh)
- `kill_switch` reseteado a inactive
- Las tablas de aprendizaje (`bandit_state`, `learning_events`,
  `copy_subscriptions`, `category_perf`, etc.) **NO se tocaron** —
  el bot conserva todo el aprendizaje del dry-run.

Pre-real PnL acumulado (dry-run, ahora archivado): **+$136.04 sobre 119 cierres**
con WR 47% en 24h. Meta para real: ≥+5% mensual sobre cap $100 con WR sostenido.

#### Latencia agregada por el proxy
Vercel edge (US) → Polymarket (intra-CF): ~50ms.
Lenovo (AR) → Vercel edge: ~150-200ms RTT.
**Total extra por call CLOB: ~250-300ms.**
Bot hace ~10-30 calls CLOB/h → ~3-9s extra/h. Despreciable para no-HFT.

#### Aprendizajes operativos clave
1. **Polymarket bloquea por país + datacenter ranges**, no solo por IP
   individual. Workers/Actions/Codespaces todos terminan flaggeados.
2. **El "Deposit address" ≠ "API address"** en cuentas Polymarket. El
   Profile → Dirección es la real.
3. **Old POLY_PROXY (sig_type=1) sigue activo** para muchas cuentas, no
   asumir Gnosis Safe (sig_type=2) por default.
4. **Vercel free tier alcanza** para nuestro volumen (~22k invocations/mes
   << 1M límite). Pero es frágil si Polymarket bloquea Vercel también.
   Plan B: Mullvad VPN ($5/mes) en la lenovo Windows.

### 2026-05-01 (madrugada) — Smart expiry filter
**Problema detectado**: con el filtro `short_duration_market` por slug pattern
(bloqueaba `*-(1|5|10|15)m-*`), las últimas 6h pasaron de operar a estar
quietas. Análisis: 66% de los rejects eran `short_duration_market`. Los
top traders de Polymarket scalpean masivamente markets cortos.

**Filtro inteligente nuevo** reemplaza al de slug:
- Parsea el epoch del final del slug: `btc-updown-5m-1777505400` → 1777505400
- Calcula `time_left = expiry - now`
- Si `time_left < MIN_TIME_TO_EXPIRY_SECONDS` (default 600s = 10 min) → reject `expires_too_soon`
- Si el slug NO tiene timestamp parseable (sports, política, eventos) →
  fail-open (no bloquea — esos markets son típicamente largos)

Beneficio: un `-15m-` recién abierto (15 min restantes) ahora pasa, antes
se bloqueaba. Un `-1h-` con 3 min restantes ahora se rechaza, antes pasaba.
Captura el riesgo real (tiempo) en vez del bucket nominal.

### 2026-04-30 (tarde) — Bug del auto-drop deshecho por selector + compound decay
**Análisis 3.1h post-deploy del mediodía**: WR 75%, PnL +$130 (con caveat:
3 outliers explican $113). Trailing stop capturó +245% y +476% peaks (#89, #92).
Pero: hallazgo crítico — `0x2e3c40fa..` que había sido auto-droppeado a las
10:43 por reject_clog, **abrió 3 trades nuevos** (los 3 winners de $113).

- **Bug en `selector.py`**: cuando `auto_replace` corre `select_traders(top_n=20)`
  tras un drop, el código en líneas 110-118 hacía `UPDATE status='active'` sin
  filtrar el estado previo, **deshaciendo silenciosamente los auto-drops**
  (loss_streak, cumulative_pnl, reject_clog). Un wallet con buen score pero
  recientemente dropped por mala perfor era reactivado al instante.
  - **Fix**: `select_traders` ahora **respeta `status='dropped'` como permanente**.
    Solo reactiva `paused`. Drops aparecen en `summary.skipped_dropped`.
- **Compound inactivity decay**: la decay anterior aplicaba ×0.7 una vez,
  arms muertas se quedaban en mult≈0.7 indefinidamente. Ahora el decay se
  aplica una vez por cada bloque `INACTIVITY_HOURS` sin actividad (cap 6
  períodos para no overflow). Ej. 48h → ×0.49, 96h → ×0.24, 168h → ×0.083.
- **`LIVE_MAX_PER_WALLET_USDC`**: 10 → **20**. Antes con base=$10 + cap=$10,
  cualquier 2do trade del mismo wallet rechazaba con `wallet_concentration`
  (37 rejects en 3h). Ahora wallets buenos pueden apilar 2 posiciones.

### 2026-04-30 (mediodía) — Mejoras de diversificación y trailing
**Análisis 8h post-deploy**: PnL flipped **-$26.75 → +$7.26** (+$34 swing),
WR 21%→45%, 0 mercados 5m colándose. Pero 22/22 trades vinieron de UN
solo wallet (concentración 100%). 5 mejoras aplicadas en paralelo:
1. **Auto-drop por reject-clog** (`learning.auto_drop_by_rejects`):
   wallets con ≥50 rejects en 24h y 0 fills → status='dropped'. Hookeado
   en runner cada ~10min. Resuelve el caso `0x2e3c40fa47..` (90 rejects
   en 8h, 0 fills, clogeando la pipeline).
2. **Inactivity decay** en `bandit.recompute_sizings`:
   `INACTIVITY_HOURS=24, INACTIVITY_DECAY=0.7`. Si un wallet no operó
   en >=24h, su sizing se multiplica por 0.7. Libera capital de "arms
   dormidas" con score histórico alto pero sin trade reciente.
3. **Trailing stop** en `risk.sweep_stops`: nueva columna `peak_price`
   en live_trades + paper_trades (default NULL, set a entry_price al
   abrir). Cuando peak >= entry × (1 + `TRAIL_ACTIVATION_PCT`=0.30),
   se activa el trailing: SL pasa a peak × (1 - `TRAIL_DROP_PCT`=0.25).
   Captura más del upside en wins grandes (algunos fueron +100-343%).
4. **Diversification cap** en `_open_position_validate`: si un wallet
   ya hizo > `MAX_WALLET_24H_PCT`=0.50 de los trades en 24h, reject
   con reason `diversification_cap`. Guard `total>=10` para evitar
   rechazos en sample chico.
5. **#5 — Esperar 24-48h** más datos antes de tocar más params.

Tests: 20 passing (12 executor + 4 risk + 4 nuevos en test_learning).

### 2026-04-29 (tarde) — Yak shave del Docker credential helper
- El cron `update_and_restart.bat` falló al hacer `docker compose pull`
  con `error getting credentials - "A specified logon session does not
  exist"`. Causa: helper de Docker Desktop (wincred) acumula sesiones
  expiradas tras varios días sin reiniciar Docker Desktop. Esto bloquea
  CUALQUIER pull, incluso de imágenes públicas como `python:3.11-slim`.
- Workaround aplicado: editar `~/.docker/config.json` para inyectar
  `auths.ghcr.io.auth` directamente como base64 (skip helper). Funcionó
  para destrabar el deploy.
- Pendiente recomendado:
  1. Hacer público el package GHCR
     (https://github.com/users/rafalez72/packages/container/bot_trading/settings
     → "Change visibility" → Public). Así el cron funciona sin login.
  2. Limpiar el `auths` del config para no dejar el PAT en plaintext.
- El PAT usado para el destrabe (`ghp_zHs7…`) debe revocarse en
  github.com/settings/tokens.

---

## 17. Pendientes inmediatos (post-validación dry-run)

| # | Acción | Razón |
|---|--------|-------|
| 1 | Crear wallet **limpia** (NO `0x110345...`) | La actual tiene private key comprometida (compartida en chat) |
| 2 | Generar API creds nuevas con esa wallet | El bot necesita firmar con clave que vos solo conozcas |
| 3 | Fundear ~$100 USDC en la nueva wallet | Cap actualizado a $100 (espejo del paper) |
| 4 | Cambiar `.env` de Lenovo con creds + funder nuevos | Switch a la wallet limpia |
| 5 | Dejar acumular ≥30 trades en dry-run con wallet limpia | Tener muestra estadísticamente válida antes de real (auto_filter mínimo es 30) |
| 6 | `LIVE_DRY_RUN=false` → live real | Arranque |
| 7 | Hacer **público** el package GHCR (1 click) | Para que el cron auto-update no se rompa con el credential helper de Docker Desktop. URL: github.com/users/rafalez72/packages/container/bot_trading/settings → Change visibility → Public |
| 8 | Revocar PAT `ghp_zHs7…` | Quedó en el chat para destrabar el deploy del 29/04 |
| 9 | Limpiar `auths` del `~/.docker/config.json` de Lenovo | Después del paso 7, ya no necesita login para pullear |

### Caveat #11: wallet 0x110345... comprometida (NO usar para real)
La private key de esa wallet circuló en el chat de configuración y debe considerarse pública. Cualquier USDC depositado ahí puede ser robado. **Solo usar para dry-run sin fondos** mientras se valida el código.

### Tunings sugeridos antes de plata real
- Considerar `LIVE_MAX_PER_WALLET_USDC=2.5` (= 1 trade concurrente por wallet en lugar de hasta 4) — el primer dry-run mostró 2 losses del mismo wallet
- O subir el threshold de stop-loss (actualmente 30%) — los stop-losses tempranos al -54%/-60% comieron mucho de cap

---

> **Recordatorio final**: cada cambio funcional → actualizar este doc.
