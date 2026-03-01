# Multi-Event Monitoring

This document describes the multi-event monitoring capabilities added to the Polymarket bot.

## Overview

The bot now supports:
1. **Multi-event monitoring**: Monitor multiple event slugs simultaneously
2. **Continuous 5/15/60-minute market monitoring**: Automatically track current crypto markets for various durations
3. **Market status tracking**: Automatically detect when markets end and close WebSocket connections

## New Features

### 1. Multiple Event Slug Monitoring

Monitor multiple markets at the same time via WebSocket. The monitor will:
- Subscribe to all token IDs across multiple markets
- Track market status periodically
- Automatically close the WebSocket when all markets have ended
- Save all bid data to a single CSV file

**Usage:**
```bash
python monitor_multi_events.py multi --slugs slug1 slug2 slug3
```

**Example:**
```bash
python monitor_multi_events.py multi \
  --slugs btc-15m-1707523200 eth-15m-1707523200 \
  --output my_bids.csv
```

### 2. Continuous 15-Minute Market Monitoring

Automatically monitor 15-minute markets for selected cryptocurrencies with proactive subscription management. The monitor will:
- **Proactively subscribe to NEXT 15-minute markets** before the current ones end
- Monitor both current and upcoming markets simultaneously
- Automatically check for new markets to subscribe to every 30 seconds
- Automatically unsubscribe from markets that have ended
- Maintain continuous monitoring without gaps
- Continue indefinitely until stopped

**Key Features:**
- No gap in monitoring between market periods
- Smooth transition from one market period to the next
- Automatic cleanup of ended markets to reduce resource usage
- Real-time subscription updates while running

**Usage:**
```bash
python monitor_multi_events.py continuous-15min --markets BTC ETH SOL
```

**Supported Markets:**
- `BTC` - Bitcoin 5, 15 and 60-minute markets
- `ETH` - Ethereum 5, 15 and 60-minute markets  
- `SOL` - Solana 5, 15 and 60-minute markets
- `XRP` - Ripple 5, 15 and 60-minute markets

**Example:**
```bash
# Monitor BTC and ETH 15-minute markets continuously
python monitor_multi_events.py continuous-15min --markets BTC ETH
```

**How It Works:**
1. On startup, subscribes to both CURRENT and NEXT 15-minute markets
2. Every 30 seconds, checks if new upcoming markets need to be added
3. Automatically subscribes to new markets before current ones end
4. Removes markets that have ended (5-minute grace period after market end)
5. Continues this cycle indefinitely

### 3. Market Status Tracking

The monitors now track market status and automatically:
- Check if markets are still active every 60 seconds (configurable)
- Detect when markets have ended (via `ended` or `closed` flags)
- Dynamically add new markets to the subscription
- Dynamically remove ended markets from the subscription
- Log market status changes

## Code Organization

The code has been reorganized into logical modules:

```
src/
├── monitors/              # WebSocket monitoring logic
│   ├── multi_event_monitor.py       # Multi-event monitor
│   └── continuous_15min_monitor.py  # Continuous 15-min monitor
├── markets/               # Market-specific utilities
│   └── fifteen_min.py               # 15-minute market utilities
└── utils/                 # General utilities
```

### Key Modules

#### `src/markets/fifteen_min.py`

Utilities for working with 15-minute crypto markets:
- `get_current_interval_utc(duration)`: Get current interval timestamp block
- `get_next_interval_utc(duration)`: Get next interval timestamp block
- `get_market_slug()`: Generate market slug for a given crypto and timestamp
- `MARKET_IDS`: Mapping of crypto symbols to market ID prefixes

#### `src/monitors/multi_event_monitor.py`

WebSocket monitor for multiple events:
- Fetches token IDs for each event slug
- Subscribes to all tokens via WebSocket
- **`add_markets()`**: Dynamically add new markets while running (NEW)
- **`remove_markets()`**: Dynamically remove ended markets while running (NEW)
- Tracks market status and closes when all markets end
- Processes and logs orderbook updates at target price (0.999)

#### `src/monitors/continuous_15min_monitor.py`

Continuous monitor for 15-minute markets:
- Generates slugs for current AND next 15-minute periods
- Uses `MultiEventMonitor` to monitor markets continuously
- **`manage_subscriptions()`**: Periodically checks for new markets and removes old ones (NEW)
- **Tracks monitored timestamps** per market to avoid duplicate subscriptions (NEW)
- Automatically subscribes to upcoming markets before current ones end
- Tracks market status and closes when all markets end
- Processes and logs orderbook updates at target price (0.999)

#### `src/monitors/continuous_15min_monitor.py`

Continuous monitor for 15-minute markets:
- Generates slugs for current 15-minute period
- Uses `MultiEventMonitor` to monitor current markets
- Automatically transitions to next period when markets end

## Configuration

New configuration options in `src/config.py`:

```python
# Monitor settings
MARKET_STATUS_CHECK_INTERVAL = 60  # How often to check if markets are still active (seconds)
MONITOR_TARGET_PRICE = 0.999       # Price level to monitor for bids
```

## Examples

### Example 1: Monitor specific event slugs
```bash
python monitor_multi_events.py multi \
  --slugs will-btc-close-higher-on-january-25-2025-0515-pm-est-than-on-january-25-2025-0500-pm-est \
  --output btc_monitor.csv
```

### Example 2: Continuously monitor BTC 15-minute markets
```bash
python monitor_multi_events.py continuous-15min \
  --markets BTC \
  --output btc_15min.csv
```

### Example 3: Monitor multiple crypto 15-minute markets
```bash
python monitor_multi_events.py continuous-15min \
  --markets BTC ETH SOL XRP \
  --output crypto_15min.csv
```

### Example 4: Custom WebSocket URL
```bash
python monitor_multi_events.py multi \
  --slugs slug1 slug2 \
  --ws-url wss://custom-ws-endpoint.com/ws
```

## CSV Output Format

The monitors output CSV files with the following columns:

| Column | Description |
|--------|-------------|
| `timestamp_ms` | Unix timestamp in milliseconds |
| `timestamp_iso` | ISO timestamp in UTC (with Z suffix) |
| `timestamp_est` | ISO timestamp in EST timezone |
| `price` | Price level of the order |
| `size` | Size of the order |
| `size_change` | Change in size from previous update |
| `side` | Whether this is a BID or ASK order |
| `best_bid` | Best bid price at the time of this update |
| `best_ask` | Best ask price at the time of this update |
| `token_id` | Token ID (market identifier) |
| `event_slug` | Event slug for the market |

## Implementation Details

### 15-Minute Market Slug Generation

The slug format for 15-minute markets is: `{market_base}-{timestamp}`

Where:
- `market_base` is the crypto-specific prefix (e.g., "btc-15m")
- `timestamp` is the Unix timestamp rounded down to the nearest 15-minute interval

Example: `btc-15m-1707523200`

### Market Status Checking

The monitor checks market status by:
1. Fetching the event from Gamma API
2. Checking the first market's `ended` or `closed` flags
3. Marking the market as inactive if either flag is true
4. **NEW: Dynamically removing inactive markets from subscriptions**

### Dynamic Subscription Management (NEW)

The continuous 15-minute monitor now supports dynamic subscription management:

**On Startup:**
- Subscribes to BOTH current and next 15-minute markets immediately
- Tracks which timestamp periods are being monitored

**Every 30 seconds:**
- Checks if the next 15-minute market needs to be added (proactive subscription)
- Checks if current markets are not yet subscribed (catch-up mechanism)
- Identifies ended markets (>5 minutes after market end time)
- Adds new markets via WebSocket `subscribe` message
- Removes ended markets via WebSocket `unsubscribe` message

**Key Benefits:**
- No gaps between market periods
- Smooth transitions without reconnecting WebSocket
- Automatic cleanup of resources
- Reduced latency when markets change

### WebSocket Subscription

The monitor subscribes to multiple token IDs in a single WebSocket connection:

```python
{
    "type": "subscribe",
    "assets_ids": [token_id_1, token_id_2, ...],
    "custom_feature_enabled": False
}
```

**NEW: Dynamic unsubscription** is also supported for removing ended markets:

```python
{
    "type": "unsubscribe",
    "assets_ids": [token_id_1, token_id_2, ...],
}
```

This allows the monitor to add and remove markets dynamically without reconnecting.

## Troubleshooting

### WebSocket Connection Issues

If the WebSocket fails to connect:
- Check the `--ws-url` parameter
- Verify network connectivity
- Check logs for specific error messages

### Market Not Found

If a market slug is not found:
- Verify the slug format matches Polymarket's expected format
- Check that the event exists on Polymarket
- For 15-minute markets, ensure the timestamp is for a valid market period

### No Token IDs Found

If token IDs cannot be fetched:
- Verify the market exists and has been created
- Check the Gamma API endpoint is accessible
- Review logs for API errors
