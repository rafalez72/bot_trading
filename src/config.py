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
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.80"))
MAX_PER_MARKET_PCT = float(os.getenv("MAX_PER_MARKET_PCT", "0.20"))
MIN_MARKET_LIQUIDITY_USDC = float(os.getenv("MIN_MARKET_LIQUIDITY_USDC", "5000"))
MIN_MARKET_VOLUME_USDC = float(os.getenv("MIN_MARKET_VOLUME_USDC", "10000"))
DAILY_KILL_SWITCH_PCT = float(os.getenv("DAILY_KILL_SWITCH_PCT", "0.10"))
STOPLOSS_SWEEP_SECONDS = int(os.getenv("STOPLOSS_SWEEP_SECONDS", "60"))

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

# Bump de precio para el retry de IOC. Si la primera orden no fillea,
# re-intentamos con price * (1 + X) en BUY (peor para nosotros, mejor chance).
LIVE_RETRY_PRICE_BUMP_PCT = float(os.getenv("LIVE_RETRY_PRICE_BUMP_PCT", "0.01"))  # 1%

# Slippage pesimista para simulaciones dry-run en modo live. El CLOB devuelve
# avg_price = price (mid optimista) cuando dry_run=True, lo que infla el PnL.
# Aplicamos este % en executor.py para que la simulación se parezca más a un
# fill real (BUY paga más, SELL recibe menos).
LIVE_DRY_SLIPPAGE_PCT = float(os.getenv("LIVE_DRY_SLIPPAGE_PCT", "0.015"))  # 1.5% pesimista

# ---------- WebSocket trades listener (Polymarket RTDS) ----------
# Feature flag para activar el listener de la RTDS WebSocket
# (`activity:trades`) en lugar del polling HTTP cada 5s.
# Default: false — el módulo existe pero no se enchufa al runner todavía.
WEBSOCKET_TRADES_ENABLED = os.getenv("WEBSOCKET_TRADES_ENABLED", "false").lower() == "true"

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

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
(ROOT / "logs").mkdir(parents=True, exist_ok=True)
