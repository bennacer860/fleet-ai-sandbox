"""Configuration from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

# --- PROFILE OVERRIDE ---
active_profile = os.getenv("ACTIVE_PROFILE")
if active_profile:
    prefix = f"P{active_profile}_"
    print(f"[PROFILE] Loading config overrides from prefix {prefix}")
    for key, value in list(os.environ.items()):
        if key.startswith(prefix):
            base_key = key[len(prefix):]
            os.environ[base_key] = value

# Wallet / CLOB
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER = os.getenv("FUNDER", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
CHAIN_ID = 137



# API endpoints
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "data/bot.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))

# Bot settings
RESOLUTION_BUFFER_SECONDS = 60  # Wait after endDate before polling for resolution
POLL_INTERVAL_SECONDS = 5
POST_RESOLUTION_ORDER_PRICE = 0.999
POST_RESOLUTION_ORDER_SIZE = 1.0

# Monitor settings
MARKET_STATUS_CHECK_INTERVAL = 60  # How often to check if markets are still active (seconds)
MONITOR_TARGET_PRICE = 0.999  # Price level to monitor for bids

# ── v1 Architecture settings ─────────────────────────────────────────────

# Database
DB_PATH = os.getenv("DB_PATH", "data/bot.db")

# Dry-run
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

# Risk limits
DEFAULT_TRADE_SIZE = float(os.getenv("DEFAULT_TRADE_SIZE", "5.0"))
POST_EXPIRY_MULTIPLIER = float(os.getenv("POST_EXPIRY_MULTIPLIER", "2.0"))
MAX_POSITION_PER_MARKET = float(os.getenv("MAX_POSITION_PER_MARKET", "50.0"))
MAX_TOTAL_EXPOSURE = float(os.getenv("MAX_TOTAL_EXPOSURE", "500.0"))
MAX_ORDERS_PER_MINUTE = int(os.getenv("MAX_ORDERS_PER_MINUTE", "30"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "10.0"))

# Health
HEALTH_FILE_PATH = os.getenv("HEALTH_FILE_PATH", "/tmp/polymarket_bot_heartbeat.json")
HEALTH_HTTP_PORT = int(os.getenv("HEALTH_HTTP_PORT", "0")) or None

# Alerts
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_NOTIFICATIONS_ENABLED = os.getenv("TELEGRAM_NOTIFICATIONS_ENABLED", "true").lower() in ("true", "1", "yes")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and TELEGRAM_NOTIFICATIONS_ENABLED)

# Proximity filter (Binance WS spot price vs. Polymarket strike price)
PROXIMITY_FILTER_ENABLED = os.getenv("PROXIMITY_FILTER_ENABLED", "false").lower() in ("true", "1", "yes")
PROXIMITY_MIN_DISTANCE = float(os.getenv("PROXIMITY_MIN_DISTANCE", "0.0005"))
