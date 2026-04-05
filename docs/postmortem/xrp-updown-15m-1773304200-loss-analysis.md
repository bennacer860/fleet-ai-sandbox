# XRP $124.88 Loss — Post-Mortem Analysis

**Date**: March 12, 2026
**Market**: `xrp-updown-15m-1773304200`
**Window**: 4:30:00 AM – 4:45:00 AM EDT (15-minute Up/Down)

---

## Summary

The bot bought the UP outcome at $0.999 × 125 ($124.88) with only 11.84 seconds
remaining before market close. XRP was sitting just $0.0006 below the strike price
— a proximity of 0.0435%. The market resolved DOWN, resulting in a total loss.

This trade is the textbook case the **proximity filter** was designed to prevent.

---

## Trade Details

| Field              | Value                                                              |
|--------------------|--------------------------------------------------------------------|
| Order ID           | `0x45ae0e602f278f305360dcf5accc83a1c606a7b39595bcf6dfb4166607844763` |
| Slug               | `xrp-updown-15m-1773304200`                                       |
| Side               | BUY (bet on UP)                                                    |
| Price              | $0.999                                                             |
| Size               | 125                                                                |
| Cost               | $124.875                                                           |
| Exit Price         | $0.00 (outcome was DOWN)                                           |
| Net P&L            | **-$124.875**                                                      |
| Strategy           | sweep                                                              |
| Trigger            | tick_size_change                                                   |

---

## Timeline

All times in EDT (UTC-4).

| Timestamp              | Epoch              | Event                    |
|------------------------|--------------------|--------------------------|
| 4:30:00.000 AM         | 1773304200.000     | Market opens             |
| 4:44:48.158 AM         | 1773305088.158     | Tick-size signal fires   |
| 4:44:48.158 AM         | 1773305088.158     | Order placed (REST sent) |
| 4:44:48.714 AM         | 1773305088.714     | REST response received   |
| 4:44:57.735 AM         | 1773305097.735     | Fill confirmed (WS)      |
| 4:45:00.000 AM         | 1773305100.000     | Market window closes     |
| 4:49:04.728 AM         | 1773305344.728     | Market resolved (DOWN)   |

---

## Latency Metrics

| Metric                        | Value       |
|-------------------------------|-------------|
| Time to expiry at placement   | 11.84 s     |
| Signal → REST response        | 555.52 ms   |
| Signal → Fill                 | 9,577 ms    |
| Fill → Market close           | 2.26 s      |
| Market close → Resolution     | 244.73 s    |

---

## Proximity Analysis

Prices fetched from Binance US kline API after the fact (not recorded in the DB
at trade time — the spot/strike/proximity columns did not exist yet).

| Metric                              | Value       |
|--------------------------------------|-------------|
| Strike price (XRP at 4:30:00 AM)     | $1.37940    |
| Spot price (XRP at 4:44:48 AM)       | $1.37880    |
| Absolute difference                  | $0.00060    |
| **Proximity** (`|spot-strike|/strike`) | **0.0435%** |
| Direction                            | DOWN (spot < strike) |

### Interpretation

- The spot price was only **$0.0006 below the strike** — essentially noise.
- The bot bet UP, but XRP was fractionally DOWN and stayed there through close.
- Proximity of **0.0435%** is below the default filter threshold of **0.05%**
  (`PROXIMITY_MIN_DISTANCE=0.0005`).

---

## Mitigation

The **proximity filter** (`PROXIMITY_FILTER_ENABLED`) was built specifically to
prevent this class of loss. When enabled, any trade where the spot-to-strike
proximity is below `PROXIMITY_MIN_DISTANCE` (default 0.05%) is blocked.

**With the filter active, this trade would not have been placed.**

### Relevant configuration

```env
PROXIMITY_FILTER_ENABLED=true       # Enable the filter
PROXIMITY_MIN_DISTANCE=0.0005       # Block trades with proximity < 0.05%
```

### What was missing at the time

1. No real-time crypto price feed — `underlying_price` column was empty.
2. No strike price lookup from Binance klines.
3. No proximity calculation or filter logic.

All three have since been implemented via the Binance miniTicker WebSocket
integration and the `fetch_strike_price()` fallback in `src/utils/market_data.py`.

---

## Data Source

- **Order data**: `data/bot.db` → `orders` table
- **Fill data**: `data/bot.db` → `fills` table
- **Decision data**: `data/bot.db` → `decisions` table
- **Trade (P&L) data**: `data/bot.db` → `trades` table
- **Strike price**: Binance US kline API — `XRPUSDT` 1m candle at epoch 1773304200000
- **Spot price**: Binance US kline API — `XRPUSDT` 1m candle at epoch 1773305040000
