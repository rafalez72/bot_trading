# Polymarket Copy Bot — Master Project Doc

> **Última actualización**: 2026-04-30 (paralelización polling, observabilidad, auto-pause, trailing stop, diversification cap)
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
BOT_CAPITAL_USDC=100.0           # cap fijo (no compounding)
COPY_BASE_USDC=5.0               # apuesta base por copia (paper)
COPY_POLL_SECONDS=5              # polling paralelo (gather), bajado de 10

# Live trading (Fase 5)
LIVE_MODE=true                   # activa executor.py
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
| **Short-duration market filter** | en open | reject `short_duration_market` para slugs `*-(1\|5\|10\|15)m-*` (binarios que expiran rápido) |
| **Auto-replace** | inmediato post-drop | re-corre `select_traders`, llena vacante |
| **Discovery on-demand** | si active < 20 | flag `discovery_pending=true` → runner dispara cycle |
| **Cluster perf refresh** | cada 5 min | actualiza `cluster_perf`; bloquea/penaliza clusters |
| **Categoría auto-block** | cada cierre, ≥15 trades | bloquea categoría si win<40% y PnL<0 |
| **Policy bucket** | post-backtest, ≥8 trades | bloquea hora/categoría/rango_precio perdedor |
| **Auto-tune filtros** | cada 6h | endurece/afloja MIN_WIN_RATE, MIN_VOLUME, etc. |
| **Discovery automática** | cada 8h | discover + backfill nuevos + recluster + select |
| **Daily summary** | cada 24h | (DESACTIVADO por pedido) |

---

## 8. Notificaciones de Telegram (estado actual)

**Activas:**
- 📈 `gain` — cada trade cerrado con ganancia: "Ganancia / Ganó: $X / Acumulado: $Y"
- 📉 `loss` — cada trade cerrado con pérdida: "Pérdida / Perdió: $X / Acumulado: $Y"
- ⛔ `kill_switch` — cuando el bot se autopausa por safety

**Comandos entrantes (telegram_listener, long polling)**:
- `/status` — modo, kill_switch, PnL 24h, wallets activos, posiciones abiertas
- `/killswitch` o `/resetkill` — desactiva el kill switch
- `/help` — lista de comandos
- Solo responde al `TELEGRAM_CHAT_ID` configurado en `.env` (otros chat_ids se ignoran).

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
- SSH desde Mac: `ssh melina@100.98.174.60`

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
