"""Lightweight spot-price fetcher for underlying crypto assets.

Prefers prices from the CryptoWebSocket (if available and fresh);
falls back to the Binance public ticker HTTP API (no auth required).
"""

from __future__ import annotations

import time
from typing import Optional

import requests

from ..logging_config import get_logger

logger = get_logger(__name__)

_BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"

_SYMBOL_MAP: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}

_cache: dict[str, tuple[float, float]] = {}
_CACHE_TTL_S = 2.0
_WS_STALE_S = 10.0

_ws_prices: dict[str, float] = {}
_ws_timestamps: dict[str, float] = {}


def set_ws_prices(
    prices: dict[str, float],
    timestamps: dict[str, float],
) -> None:
    """Called by the bot to inject the CryptoWebSocket price snapshot."""
    _ws_prices.update(prices)
    _ws_timestamps.update(timestamps)


def get_spot_price(asset: str, timeout: float = 3.0) -> Optional[float]:
    """Return the current USDT spot price for *asset* (e.g. ``"XRP"``).

    Tries the WebSocket cache first; falls back to HTTP if the WS price
    is stale (>10 s) or unavailable.  Returns ``None`` on any failure.
    """
    key = asset.upper()

    ws_ts = _ws_timestamps.get(key)
    if ws_ts is not None and (time.monotonic() - ws_ts) < _WS_STALE_S:
        price = _ws_prices.get(key)
        if price is not None:
            return price

    symbol = _SYMBOL_MAP.get(key)
    if not symbol:
        logger.debug("[PRICE] Unknown asset %r — no Binance symbol mapped", asset)
        return None

    now = time.monotonic()
    cached = _cache.get(symbol)
    if cached and (now - cached[1]) < _CACHE_TTL_S:
        return cached[0]

    try:
        resp = requests.get(
            _BINANCE_TICKER,
            params={"symbol": symbol},
            timeout=timeout,
        )
        resp.raise_for_status()
        price = float(resp.json()["price"])
        _cache[symbol] = (price, now)
        logger.debug("[PRICE] %s = $%.6f (http)", symbol, price)
        return price
    except Exception:
        logger.debug("[PRICE] Failed to fetch %s", symbol, exc_info=True)
        return None
