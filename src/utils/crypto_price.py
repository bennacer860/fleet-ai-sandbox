"""WebSocket-backed spot-price cache for crypto assets.

Prices are injected by the bot's metrics loop via ``set_ws_prices()``
from the ``CryptoWebSocket``.  Callers read with ``get_spot_price()``,
which is a pure dict lookup — no I/O, no blocking.
"""

from __future__ import annotations

import time
from typing import Optional

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


def get_spot_price(asset: str) -> Optional[float]:
    """Return the latest WS spot price for *asset*, or None if stale/missing."""
    key = asset.upper()
    ts = _ws_timestamps.get(key)
    if ts is not None and (time.monotonic() - ts) < _WS_STALE_S:
        return _ws_prices.get(key)
    return None
