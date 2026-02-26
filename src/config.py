"""Configuration from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

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
MAX_POSITION_PER_MARKET = float(os.getenv("MAX_POSITION_PER_MARKET", "50.0"))
MAX_TOTAL_EXPOSURE = float(os.getenv("MAX_TOTAL_EXPOSURE", "500.0"))
MAX_ORDERS_PER_MINUTE = int(os.getenv("MAX_ORDERS_PER_MINUTE", "30"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "10.0"))

# Health
HEALTH_FILE_PATH = os.getenv("HEALTH_FILE_PATH", "/tmp/polymarket_bot_heartbeat.json")
HEALTH_HTTP_PORT = int(os.getenv("HEALTH_HTTP_PORT", "0")) or None

# Alerts
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
