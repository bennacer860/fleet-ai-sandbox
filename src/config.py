"""Configuration from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

# Wallet / CLOB
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER = os.getenv("FUNDER", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
CHAIN_ID = 137

from .core.models import ProfileConfig

def _load_profiles() -> list[ProfileConfig]:
    profiles = []
    
    # 1. Try to load prefixed profiles (P1_NAME, P2_NAME, ...)
    i = 1
    while True:
        prefix = f"P{i}_"
        name = os.getenv(f"{prefix}NAME")
        if not name:
            # Check if we should stop or if we skipped a number (stop at first gap)
            break
            
        profiles.append(ProfileConfig(
            name=name,
            private_key=os.getenv(f"{prefix}PRIVATE_KEY", ""),
            funder=os.getenv(f"{prefix}FUNDER", ""),
            signature_type=int(os.getenv(f"{prefix}SIGNATURE_TYPE", "1")),
            api_key=os.getenv(f"{prefix}POLY_API_KEY"),
            api_secret=os.getenv(f"{prefix}POLY_SECRET"),
            api_passphrase=os.getenv(f"{prefix}POLY_PARAPHRASE"),
            trade_size_override=float(os.getenv(f"{prefix}TRADE_SIZE", "0")) or None,
            max_position_override=float(os.getenv(f"{prefix}MAX_POSITION", "0")) or None,
        ))
        i += 1
        
    # 2. If no prefixed profiles found, fall back to legacy single profile
    if not profiles and PRIVATE_KEY and FUNDER:
        profiles.append(ProfileConfig(
            name="default",
            private_key=PRIVATE_KEY,
            funder=FUNDER,
            signature_type=SIGNATURE_TYPE,
            api_key=os.getenv("POLY_API_KEY"),
            api_secret=os.getenv("POLY_SECRET"),
            api_passphrase=os.getenv("POLY_PARAPHRASE"),
        ))
        
    return profiles

PROFILES = _load_profiles()

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
TELEGRAM_NOTIFICATIONS_ENABLED = os.getenv("TELEGRAM_NOTIFICATIONS_ENABLED", "false").lower() in ("true", "1", "yes")
