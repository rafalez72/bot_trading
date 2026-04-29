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

# ---------- Live trading (Fase 5 - plata real) ----------
# LIVE_MODE=false → paper trading (default).
# LIVE_MODE=true  → ejecuta órdenes reales en Polymarket CLOB.
LIVE_MODE = os.getenv("LIVE_MODE", "false").lower() == "true"

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

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
(ROOT / "logs").mkdir(parents=True, exist_ok=True)
