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

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
(ROOT / "logs").mkdir(parents=True, exist_ok=True)
