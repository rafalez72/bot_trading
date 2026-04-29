# Polymarket Copy Bot — Master Project Doc

> **Última actualización**: 2026-04-27 (migración Lenovo en curso, paso rsync)
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
| `paper_trades` | Trades simulados del bot (entry/exit/pnl/status/exit_reason/asset) |
| `live_trades` | Trades **reales** en Polymarket CLOB (Fase 5). Mirror de paper_trades + token_id, order_id, tx_hash, fees, dry_run flag |
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
COPY_BASE_USDC=5.0               # apuesta base por copia
COPY_POLL_SECONDS=10             # polling de wallets

# Risk management
STOP_LOSS_PCT=0.30               # cierre si cae 30%
TAKE_PROFIT_PCT=0.80             # cierre si sube 80%
MAX_PER_MARKET_PCT=0.20          # max 20% del cap en un solo mercado
MIN_MARKET_LIQUIDITY_USDC=5000   # filtro de liquidez
MIN_MARKET_VOLUME_USDC=10000     # filtro de volumen
DAILY_KILL_SWITCH_PCT=0.10       # pausa si PnL 24h < -10% del cap
STOPLOSS_SWEEP_SECONDS=60        # frecuencia del check de SL/TP

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
| **Stop-loss / Take-profit** | cada 60s | cierra posición individual al -30% / +80% |
| **Bandit UCB1** | cada cierre | recalcula `sizing_mult` de TODOS los activos |
| **Auto-drop** | cada cierre | drop si 5 losses consecutivos |
| **Auto-replace** | inmediato post-drop | re-corre `select_traders`, llena vacante |
| **Discovery on-demand** | si active < 20 | flag `discovery_pending=true` → runner dispara cycle |
| **Cluster perf refresh** | cada 5 min | actualiza `cluster_perf`; bloquea/penaliza clusters |
| **Categoría auto-block** | cada cierre, ≥15 trades | bloquea categoría si win<40% y PnL<0 |
| **Policy bucket** | post-backtest, ≥8 trades | bloquea hora/categoría/rango_precio perdedor |
| **Auto-tune filtros** | cada 6h | endurece/afloja MIN_WIN_RATE, MIN_VOLUME, etc. |
| **Discovery automática** | cada 8h | discover + backfill nuevos + recluster + select |
| **Kill switch** | cada ciclo | pausa todo si PnL 24h < -10% del cap |
| **Daily summary** | cada 24h | (DESACTIVADO por pedido) |

---

## 8. Notificaciones de Telegram (estado actual)

**Activas:**
- 📈 `gain` — cada trade cerrado con ganancia: "Ganancia / Ganó: $X / Acumulado: $Y"
- 📉 `loss` — cada trade cerrado con pérdida: "Pérdida / Perdió: $X / Acumulado: $Y"
- ⛔ `kill_switch` — cuando el bot se autopausa por safety

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
LIVE_CAPITAL_USDC=30
LIVE_BASE_USDC=2.5
```

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
2 trades cerrados (todos del wallet 0xa535fabc97)
2 stop-losses al -54% y -60% · PnL: -$3.99
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

---

## 17. Pendientes inmediatos (post-validación dry-run)

| # | Acción | Razón |
|---|--------|-------|
| 1 | Crear wallet **limpia** (NO `0x110345...`) | La actual tiene private key comprometida (compartida en chat) |
| 2 | Generar API creds nuevas con esa wallet | El bot necesita firmar con clave que vos solo conozcas |
| 3 | Fundear $30 USDC en la nueva wallet | Plata real para arrancar |
| 4 | Cambiar `.env` de Lenovo con creds + funder nuevos | Switch a la wallet limpia |
| 5 | Reset kill switch + dejar 1-2h en dry-run con wallet limpia | Confirmar cero issues |
| 6 | `LIVE_DRY_RUN=false` → live real | Arranque |

### Caveat #11: wallet 0x110345... comprometida (NO usar para real)
La private key de esa wallet circuló en el chat de configuración y debe considerarse pública. Cualquier USDC depositado ahí puede ser robado. **Solo usar para dry-run sin fondos** mientras se valida el código.

### Tunings sugeridos antes de plata real
- Considerar `LIVE_MAX_PER_WALLET_USDC=2.5` (= 1 trade concurrente por wallet en lugar de hasta 4) — el primer dry-run mostró 2 losses del mismo wallet
- O subir el threshold de stop-loss (actualmente 30%) — los stop-losses tempranos al -54%/-60% comieron mucho de cap

---

> **Recordatorio final**: cada cambio funcional → actualizar este doc.
