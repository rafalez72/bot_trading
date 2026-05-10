import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB_API = os.getenv("CLOB_API", "https://clob.polymarket.com")
DATA_API = os.getenv("DATA_API", "https://data-api.polymarket.com")

DB_PATH = ROOT / os.getenv("DB_PATH", "data/copybot.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

COPY_BASE_USDC = float(os.getenv("COPY_BASE_USDC", "5.0"))
COPY_POLL_SECONDS = int(os.getenv("COPY_POLL_SECONDS", "10"))

# Risk management
BOT_CAPITAL_USDC = float(os.getenv("BOT_CAPITAL_USDC", "100.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.30"))

# Adaptive stop-loss por horizonte de market.
# Análisis 2026-05-09 (13h trading): 8/12 losses por SL firing 20-26% en
# markets short-term (crypto-updown 15min, esports live) donde el mid oscila
# ±50% naturalmente pre-resolución. SL único = 0.30 desclasifica trades
# que terminan ganadores. En markets >12h (elections, news) un drop 20-30%
# SÍ es señal real de loss. Solución: 4 buckets por seconds_to_market_end.
# Si end_date es desconocido, fallback a STOP_LOSS_PCT (compat).
# Buckets:
#   < b0  → ULTRASHORT (default 1.00 = effectively disabled, dejamos que resuelva)
#   < b1  → SHORT      (default 0.40)
#   < b2  → MEDIUM     (default 0.30)
#   else  → LONG       (default 0.20)
STOP_LOSS_PCT_ULTRASHORT = float(os.getenv("STOP_LOSS_PCT_ULTRASHORT", "1.00"))
STOP_LOSS_PCT_SHORT = float(os.getenv("STOP_LOSS_PCT_SHORT", "0.40"))
STOP_LOSS_PCT_MEDIUM = float(os.getenv("STOP_LOSS_PCT_MEDIUM", "0.30"))
STOP_LOSS_PCT_LONG = float(os.getenv("STOP_LOSS_PCT_LONG", "0.20"))
# Boundaries (segundos) entre buckets, comma-separated.
# Default: 1800s (30min), 7200s (2h), 43200s (12h).
STOP_LOSS_HORIZON_BUCKETS_S = os.getenv("STOP_LOSS_HORIZON_BUCKETS_S", "1800,7200,43200")

TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.80"))
MAX_PER_MARKET_PCT = float(os.getenv("MAX_PER_MARKET_PCT", "0.20"))
MIN_MARKET_LIQUIDITY_USDC = float(os.getenv("MIN_MARKET_LIQUIDITY_USDC", "5000"))
MIN_MARKET_VOLUME_USDC = float(os.getenv("MIN_MARKET_VOLUME_USDC", "10000"))
DAILY_KILL_SWITCH_PCT = float(os.getenv("DAILY_KILL_SWITCH_PCT", "0.10"))
STOPLOSS_SWEEP_SECONDS = int(os.getenv("STOPLOSS_SWEEP_SECONDS", "60"))

# ---------- Kill switch HARD 3-layer (2026-05-10) ----------
# Tres layers complementarios al DAILY_KILL_SWITCH_PCT (que es % capital
# rolling-24h). Estos son CAPS HARD adicionales y se evalúan en orden:
#   1) Daily $ cap   (DAILY_LOSS_CAP_USDC):       suma pnl del día UTC
#   2) Consecutive   (MAX_CONSECUTIVE_LOSSES):    racha de N losses seguidas
#   3) Drawdown      (MAX_DRAWDOWN_PCT):          peak-to-trough capital
# Cada layer dispara con motivo explícito (notif Telegram detallada).
# Default $10 = 10% del capital paper default ($100). Si el cap del bot es
# menor, ajustar acorde.
DAILY_LOSS_CAP_USDC = float(os.getenv("DAILY_LOSS_CAP_USDC", "10.0"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "0.20"))

# Trailing stop: cuando la posición está +TRAIL_ACTIVATION_PCT en ganancia,
# se activa el trailing. Si el precio cae TRAIL_DROP_PCT desde el peak,
# se fuerza el cierre. Esto deja correr los wins grandes en vez de cortarlos
# en TP=50%.
TRAIL_ACTIVATION_PCT = float(os.getenv("TRAIL_ACTIVATION_PCT", "0.30"))
TRAIL_DROP_PCT = float(os.getenv("TRAIL_DROP_PCT", "0.25"))

# Diversification cap: si un solo wallet hizo > MAX_WALLET_24H_PCT de los
# trades del bot en 24h, rechazamos nuevos copies de ese wallet hasta que
# se diversifique. Guard: solo aplica si total >= 10 (sample chico = ruido).
MAX_WALLET_24H_PCT = float(os.getenv("MAX_WALLET_24H_PCT", "0.50"))

# Filtro inteligente de markets cortos: bloqueamos si el mercado expira en
# menos de N segundos. Reemplaza el filtro lazy por slug pattern (-5m-, -15m-).
# El timestamp de expiry se extrae del slug (formato: 'btc-updown-5m-1777505400').
# Si no se puede parsear, NO bloquea (fail-open) — los slugs sin epoch suelen
# ser markets de eventos largos (deportes, política).
# 2026-05-06 (tarde): bajado de 600 (10min) a 180 (3min). Causa: post discover-now
# el roster de wallets pasó a operar mayoritariamente markets cortos (5m crypto,
# sport in-play). Con 600s el filtro rechazaba 100% de los trades nuevos
# (40 rejects en 2h, 0 opens). Con 180s y STOPLOSS_SWEEP_SECONDS=15 + SL=20%,
# el bot tiene tiempo para 6+ ciclos de SL antes que el market expire.
# Cap defensivo: si el .env sobreescribe con valor >300, lo limito porque
# valores altos demostraron rechazar el 100% del flow.
_min_exp_user = int(os.getenv("MIN_TIME_TO_EXPIRY_SECONDS", "180"))
MIN_TIME_TO_EXPIRY_SECONDS = min(_min_exp_user, 300)  # cap 5min

# Market horizon mínimo: bloqueamos mercados cuyo `end_date` (Gamma API) está a
# menos de N segundos de ahora. Es complementario a MIN_TIME_TO_EXPIRY_SECONDS:
# ese parsea el slug (epoch al final), éste lee el campo end_date de la tabla
# markets. Markets <30min son ruido para SL=20% — el mid se mueve por noise y
# disparamos el stop sin que haya tendencia. Default 30min. Se exenta a
# `crypto_arb` que está diseñado para markets ultra-cortos (5min).
MARKET_HORIZON_MIN_SECS = int(os.getenv("MARKET_HORIZON_MIN_SECS", "1800"))

# Bloqueo de categorías ultra-cortas (esports live, crypto-updown 5/15min, sport
# in-play). Default ON: a corto plazo el bot pierde plata copiando wallets que
# operan estos markets — el spread + slippage + ruido del mid superan al edge.
BLOCK_ULTRASHORT_MARKETS = os.getenv("BLOCK_ULTRASHORT_MARKETS", "true").lower() == "true"

# ---------- Live trading (Fase 5 - plata real) ----------
# LIVE_MODE=false → paper trading (default).
# LIVE_MODE=true  → ejecuta órdenes reales en Polymarket CLOB.
# EMERGENCY_PAPER_LOCK (2026-05-06): forzamos paper mode hasta validar el
# fix del bug de SQLite locks (SELLs perdidos → posiciones que no cierran).
# Para reactivar real: setear FORCE_LIVE_OK=true en .env, validar 24h, después
# remover esta guarda. Default: requiere flag explícito.
_LIVE_REQUESTED = os.getenv("LIVE_MODE", "false").lower() == "true"
_FORCE_LIVE_OK = os.getenv("FORCE_LIVE_OK", "false").lower() == "true"
LIVE_MODE = _LIVE_REQUESTED and _FORCE_LIVE_OK

# LIVE_DRY_RUN=true → loguea las órdenes pero no las manda al CLOB.
# Útil para validar el flujo sin gastar plata. Funciona solo si LIVE_MODE=true.
LIVE_DRY_RUN = os.getenv("LIVE_DRY_RUN", "true").lower() == "true"

# Capital real disponible para trading live (en USDC).
# Es el cap operativo del bot, NO el balance total de la wallet.
LIVE_CAPITAL_USDC = float(os.getenv("LIVE_CAPITAL_USDC", "50.0"))

# Tamaño base por copia en modo live (suele ser menor que paper).
LIVE_BASE_USDC = float(os.getenv("LIVE_BASE_USDC", "2.5"))

# Polymarket CLOB credentials (generadas con scripts/generate_api_creds.py)
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")

# Dirección de la proxy wallet (la que tiene tu USDC en Polymarket)
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")

# Private key de la wallet que controla la proxy.
# OPCIONAL: solo necesaria si necesitas firmar L1 ops o regenerar API creds.
# Para placement de órdenes con creds L2, NO es necesaria.
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")

# Tipo de firma de Polymarket: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE
# Default 2 — es lo que usa la cuenta creada via email/Magic en polymarket.com
POLYMARKET_SIG_TYPE = int(os.getenv("POLYMARKET_SIG_TYPE", "2"))

# ---------- Paper: mejoras simétricas con LIVE ----------
# Antes paper.py no tenía estos filtros que sí tenía executor.py — eso hacía que
# el paper "test" fuera más permisivo que el live "real" y fallara la promesa de
# "test = real". Agregados 2026-05-06.
# Cap por wallet copiado en paper (forzosa diversificación).
PAPER_MAX_PER_WALLET_USDC = float(os.getenv("PAPER_MAX_PER_WALLET_USDC", "8.0"))

# Mínimo de PnL esperado para que el trade paper se abra (anti-fees-comen-todo).
PAPER_MIN_EXPECTED_PNL_USDC = float(os.getenv("PAPER_MIN_EXPECTED_PNL_USDC", "0.30"))

# Máximo de entries simultáneas por (source_wallet, condition_id). Las wallets
# copiadas suelen hacer DCA / pyramiding (varias entries averaging in en el mismo
# market). El cap previo basado en USDC (PAPER_MAX_PER_WALLET_USDC) bloqueaba el
# 2do/3er entry, perdiendo posiciones legítimas. Con N=3 dejamos pasar el patrón
# DCA típico sin permitir over-concentration ilimitada.
MAX_ENTRIES_PER_WALLET_MARKET = int(os.getenv("MAX_ENTRIES_PER_WALLET_MARKET", "3"))

# Edad máxima (segundos) del trade del source cuando lo procesamos. Si el polling
# detecta un trade más viejo que esto → reject 'stale_trade'. Default 300s (5min)
# tolera lag de polling sin abrir entries demasiado tardías que ya perdieron edge.
# El valor antiguo (60s) era muy estricto: el polling RE-procesa los mismos trades
# detectados por WS, generando ráfagas de stale_trade rejects.
STALE_TRADE_MAX_AGE_S = int(os.getenv("STALE_TRADE_MAX_AGE_S", "300"))

# ---------- Live trading: mejoras de fricción ----------
# Cap por wallet copiado en live (forzosa diversificación).
# Default $10 con cap total $30 → max 3 wallets concurrentes con full size.
LIVE_MAX_PER_WALLET_USDC = float(os.getenv("LIVE_MAX_PER_WALLET_USDC", "10.0"))

# Mínimo de PnL esperado para que valga la pena el trade (filtro anti-fees).
# Si el size del trade es tan chico que el PnL esperado no cubre fees, skip.
# Heurística: expected_gross = size * 0.20 (20% gain promedio en wins).
LIVE_MIN_EXPECTED_PNL_USDC = float(os.getenv("LIVE_MIN_EXPECTED_PNL_USDC", "0.50"))

# Slippage máximo aceptable al pre-checkear el orderbook.
# Si la VWAP del orderbook al size que queremos comprar > target_price * (1+X),
# no mandamos la orden (no vale el slippage).
LIVE_MAX_SLIPPAGE_PCT = float(os.getenv("LIVE_MAX_SLIPPAGE_PCT", "0.03"))  # 3%

# Profundidad mínima del orderbook (suma de los primeros 5 niveles del book
# del side relevante, en USDC notional). Markets crypto-updown thin
# (~$50-100 por nivel) generaron pérdidas garantizadas por slippage incluso
# con LIVE_MAX_SLIPPAGE_PCT=4%. Bloquear cualquier market con orderbook total
# < $500 elimina la mayoría de esos casos sin necesidad de excludes por slug.
# 2026-05-10: agregado tras pérdida $76 en primera sesión LIVE.
LIVE_MIN_ORDERBOOK_DEPTH_USDC = float(
    os.getenv("LIVE_MIN_ORDERBOOK_DEPTH_USDC", "500")
)

# Bump de precio para el retry de IOC. Si la primera orden no fillea,
# re-intentamos con price * (1 + X) en BUY (peor para nosotros, mejor chance).
LIVE_RETRY_PRICE_BUMP_PCT = float(os.getenv("LIVE_RETRY_PRICE_BUMP_PCT", "0.01"))  # 1%

# Tipo de orden default para placement live. Opciones:
#   LIMIT_FOK (default): fill-or-kill limit a precio = mid ± max_slippage_pct.
#       Si no hay liquidez completa al limit → la orden se cancela (no fill).
#       Defensa anti-MEV/adversarial: en thin orderbooks el slippage real
#       puede ser 5-10x el VWAP teórico — el limit garantiza que NUNCA
#       paguemos peor que el cap.
#   MARKET: legacy IOC con retry (FAK). Vulnerable a slippage extremo en
#       books thin. Solo usar para markets con depth >> bet size y como
#       opt-in explícito.
# Default LIMIT_FOK desde 2026-05-10 tras research sobre MEV/adversarial fills.
LIVE_ORDER_TYPE = os.getenv("LIVE_ORDER_TYPE", "LIMIT_FOK").strip().upper()

# Slippage pesimista para simulaciones dry-run en modo live. El CLOB devuelve
# avg_price = price (mid optimista) cuando dry_run=True, lo que infla el PnL.
# Aplicamos este % en executor.py para que la simulación se parezca más a un
# fill real (BUY paga más, SELL recibe menos).
LIVE_DRY_SLIPPAGE_PCT = float(os.getenv("LIVE_DRY_SLIPPAGE_PCT", "0.015"))  # 1.5% pesimista

# ---------- Discovery: TopVolume sweep (2026-05-09) ----------
# Una vez por día (configurable), pulleamos top-N wallets por volumen 24h
# desde data-api.polymarket.com/trades y disparamos backfill para los que
# todavía no tenemos en trader_metrics. Diseño: ver
# src/copybot/discovery_topvolume.py.
#
# Sin esto, nuestro universo crece sólo cuando un wallet aparece en el feed
# global durante el discover de N páginas — los wallets de mayor volumen rara
# vez tocan el tope del feed (que va por timestamp). Resultado: 4052 wallets
# vs ~4400 únicos/h reales en Polymarket.
DISCOVERY_TOPVOLUME_ENABLED = os.getenv("DISCOVERY_TOPVOLUME_ENABLED", "true").lower() == "true"
DISCOVERY_TOPVOLUME_LIMIT = int(os.getenv("DISCOVERY_TOPVOLUME_LIMIT", "1000"))
DISCOVERY_TOPVOLUME_INTERVAL_HOURS = int(os.getenv("DISCOVERY_TOPVOLUME_INTERVAL_HOURS", "24"))

# ---------- WebSocket trades listener (Polymarket RTDS) ----------
# Feature flag para activar el listener de la RTDS WebSocket
# (`activity:trades`) en lugar del polling HTTP cada 5s.
# Default: false — el módulo existe pero no se enchufa al runner todavía.
WEBSOCKET_TRADES_ENABLED = os.getenv("WEBSOCKET_TRADES_ENABLED", "false").lower() == "true"

# Shadow watch (pre-promote): además de las wallets activas que copiamos,
# observa N wallets candidatas via WS. Sus trades se persisten en
# ``shadow_trades(mode='pre_promote_watch')`` para análisis previo a promover.
# Default true porque solo agrega observación pasiva (no riesgo operativo).
SHADOW_WATCH_ENABLED = os.getenv("SHADOW_WATCH_ENABLED", "true").lower() == "true"
SHADOW_WATCH_LIMIT = int(os.getenv("SHADOW_WATCH_LIMIT", "200"))

# ---------- Hyperliquid (copy-bot perps en paralelo, dry-run) ----------
# Activación: HL_MODE=true. Si false, el HL runner no arranca y nada cambia.
HL_MODE = os.getenv("HL_MODE", "false").lower() == "true"
HL_CAPITAL_USDC = float(os.getenv("HL_CAPITAL_USDC", "50.0"))
HL_BASE_USDC = float(os.getenv("HL_BASE_USDC", "5.0"))
HL_MAX_PER_WALLET_USDC = float(os.getenv("HL_MAX_PER_WALLET_USDC", "10.0"))
HL_MAX_LEVERAGE = float(os.getenv("HL_MAX_LEVERAGE", "5.0"))
HL_MIN_EXPECTED_PNL_USDC = float(os.getenv("HL_MIN_EXPECTED_PNL_USDC", "0.20"))
HL_STOP_LOSS_PCT = float(os.getenv("HL_STOP_LOSS_PCT", "0.20"))
HL_TAKE_PROFIT_PCT = float(os.getenv("HL_TAKE_PROFIT_PCT", "0.50"))
HL_TRAIL_ACTIVATION_PCT = float(os.getenv("HL_TRAIL_ACTIVATION_PCT", "0.30"))
HL_TRAIL_DROP_PCT = float(os.getenv("HL_TRAIL_DROP_PCT", "0.25"))
HL_DRY_SLIPPAGE_PCT = float(os.getenv("HL_DRY_SLIPPAGE_PCT", "0.005"))
HL_ALLOWED_COINS = [c.strip().upper() for c in os.getenv("HL_ALLOWED_COINS", "BTC,ETH,SOL").split(",") if c.strip()]
HL_SLEEP_SECONDS = int(os.getenv("HL_SLEEP_SECONDS", "5"))
HL_SWEEP_SECONDS = int(os.getenv("HL_SWEEP_SECONDS", "30"))
HL_LIQUIDATION_BUFFER = float(os.getenv("HL_LIQUIDATION_BUFFER", "1.2"))
# Anti-scalper: max fills copiados por wallet en 24h. Si excede, reject;
# si excede 2× → auto-drop. Wallets HFT generan +1000 fills/día con
# pérdidas garantizadas por slippage.
HL_MAX_FILLS_PER_WALLET_24H = int(os.getenv("HL_MAX_FILLS_PER_WALLET_24H", "30"))
# Production parity: HL settlement L1 no cobra gas explícito → 0.0. El
# valor queda configurable por consistencia con DX y por si Hyperliquid
# llegase a cobrar fee de exec (taker fee) que querramos descontar acá.
HL_GAS_PER_FILL_USDC = float(os.getenv("HL_GAS_PER_FILL_USDC", "0.0"))
HL_FUNDING_UPDATE_HOURS = int(os.getenv("HL_FUNDING_UPDATE_HOURS", "1"))
HL_USE_ORDERBOOK_FILL = os.getenv("HL_USE_ORDERBOOK_FILL", "true").lower() == "true"

# ---------- dYdX v4 (3er bot — perps Cosmos chain, dry-run) ----------
DX_MODE = os.getenv("DX_MODE", "false").lower() == "true"
DX_CAPITAL_USDC = float(os.getenv("DX_CAPITAL_USDC", "50.0"))
DX_BASE_USDC = float(os.getenv("DX_BASE_USDC", "5.0"))
DX_MAX_PER_WALLET_USDC = float(os.getenv("DX_MAX_PER_WALLET_USDC", "10.0"))
DX_MAX_LEVERAGE = float(os.getenv("DX_MAX_LEVERAGE", "5.0"))
DX_MIN_EXPECTED_PNL_USDC = float(os.getenv("DX_MIN_EXPECTED_PNL_USDC", "0.20"))
DX_STOP_LOSS_PCT = float(os.getenv("DX_STOP_LOSS_PCT", "0.20"))
DX_TAKE_PROFIT_PCT = float(os.getenv("DX_TAKE_PROFIT_PCT", "0.50"))
DX_TRAIL_ACTIVATION_PCT = float(os.getenv("DX_TRAIL_ACTIVATION_PCT", "0.30"))
DX_TRAIL_DROP_PCT = float(os.getenv("DX_TRAIL_DROP_PCT", "0.25"))
DX_DRY_SLIPPAGE_PCT = float(os.getenv("DX_DRY_SLIPPAGE_PCT", "0.001"))
DX_ALLOWED_TICKERS = [t.strip().upper() for t in os.getenv("DX_ALLOWED_TICKERS", "").split(",") if t.strip()]
DX_SLEEP_SECONDS = int(os.getenv("DX_SLEEP_SECONDS", "5"))
DX_SWEEP_SECONDS = int(os.getenv("DX_SWEEP_SECONDS", "30"))
DX_LIQUIDATION_BUFFER = float(os.getenv("DX_LIQUIDATION_BUFFER", "1.2"))
DX_MAX_FILLS_PER_WALLET_24H = int(os.getenv("DX_MAX_FILLS_PER_WALLET_24H", "30"))
# Production parity: costos reales que simulamos para predecir net PnL real.
DX_GAS_PER_FILL_USDC = float(os.getenv("DX_GAS_PER_FILL_USDC", "0.02"))
DX_FUNDING_UPDATE_HOURS = int(os.getenv("DX_FUNDING_UPDATE_HOURS", "1"))
DX_USE_ORDERBOOK_FILL = os.getenv("DX_USE_ORDERBOOK_FILL", "true").lower() == "true"

# ---------- Market making (Nivel 3, 2026-05-10) ----------
# En vez de tomar liquidez (copy-trades), POSTEAMOS bid+ask en buckets calientes
# con spread fijo. Capturamos spread cuando dos contrapartes opuestas matchean
# nuestras órdenes en el mismo bucket.
#
# Riesgos:
# - Adverse selection: si el mid se mueve fuerte, una pata fillea contra info
#   stale → pérdida. Por eso recalculamos y cancelamos cuando mid se mueve
#   >50bps (ver MarketMaker._should_recalc).
# - Bucket close mientras tenemos posición: settlement on-chain según resolución.
#
# Activación gated. NUNCA arrancar en LIVE sin paper validado 24h.
MM_ENABLED = os.getenv("MM_ENABLED", "false").lower() == "true"
# spread_bps: 300 = 3% spread total. Si mid=0.50 → bid=0.485, ask=0.515.
MM_SPREAD_BPS = int(os.getenv("MM_SPREAD_BPS", "300"))
# USDC por side (bid + ask se postean por separado).
MM_BET_PER_SIDE_USDC = float(os.getenv("MM_BET_PER_SIDE_USDC", "2.0"))
# Cap de pares (bid+ask) concurrentes para limitar exposición total.
MM_MAX_CONCURRENT_PAIRS = int(os.getenv("MM_MAX_CONCURRENT_PAIRS", "5"))

# ---------- Spike arbitrage (Nivel B, 2026-05-10) ----------
# Detect movimiento brusco Binance spot → posicionar limit order en mid
# Polymarket ANTES que ajuste. Si fillea, capturamos el delta. Si no
# fillea en TTL, cancel.
#
# Diferencia vs CRYPTO_ARB: trigger absoluto simple (umbral % en window),
# LIMIT orders only (no market) para evitar slippage en thin orderbooks,
# defensa básica de TTL.
#
# Activación gated por SPIKE_ARB_ENABLED. Validar siempre en paper antes
# de live — ver src/copybot/spike_arb.py.
SPIKE_ARB_ENABLED = os.getenv("SPIKE_ARB_ENABLED", "false").lower() == "true"
SPIKE_ARB_THRESHOLD_PCT = float(os.getenv("SPIKE_ARB_THRESHOLD_PCT", "0.4"))
SPIKE_ARB_WINDOW_S = int(os.getenv("SPIKE_ARB_WINDOW_S", "30"))
SPIKE_ARB_TARGET_USDC = float(os.getenv("SPIKE_ARB_TARGET_USDC", "3.0"))
SPIKE_ARB_LIMIT_TTL_S = int(os.getenv("SPIKE_ARB_LIMIT_TTL_S", "60"))

# ---------- Adversarial dust asks pre-close (Nivel 3, 2026-05-10) ----------
# Estrategia adversarial: cuando un bucket crypto-updown-5m está por cerrar
# (10-60s antes) y la prob implied del lado ganador >= ADVERSARIAL_MIN_LOSER_PROB,
# postear ASK del lado perdedor a precio basura (0.05). Si retail/bot pega a
# market BUY → fill → recibimos $0.05/share. Settle: lado perdedor vale $0
# pero ya cobramos. Default: ENABLED=false + SIGNAL_ONLY=true (solo loggea).
# Implementación live requiere extender clob_client (split_position + GTC)
# — ver docstring de src/copybot/adversarial_asks.py.
ADVERSARIAL_ENABLED = os.getenv("ADVERSARIAL_ENABLED", "false").lower() == "true"
ADVERSARIAL_MAX_SECS_TO_CLOSE = float(os.getenv("ADVERSARIAL_MAX_SECS_TO_CLOSE", "60"))
ADVERSARIAL_MIN_LOSER_PROB = float(os.getenv("ADVERSARIAL_MIN_LOSER_PROB", "0.85"))
ADVERSARIAL_ASK_PRICE = float(os.getenv("ADVERSARIAL_ASK_PRICE", "0.05"))
ADVERSARIAL_SIZE_USDC = float(os.getenv("ADVERSARIAL_SIZE_USDC", "2.0"))

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
(ROOT / "logs").mkdir(parents=True, exist_ok=True)
