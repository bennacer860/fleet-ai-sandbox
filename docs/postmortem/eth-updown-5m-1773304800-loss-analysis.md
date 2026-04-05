# ETH $124.88 Loss — Post-Mortem Analysis

**Date**: March 12, 2026
**Market**: `eth-updown-5m-1773304800`
**Window**: 4:40:00 AM – 4:45:00 AM EDT (5-minute Up/Down)

---

## Summary

The bot bought the DOWN outcome at $0.999 × 125 ($124.88) with only 8.77 seconds
remaining before market close. ETH's final price was $2,046.05 — just **$0.01
above** the strike of $2,046.04. The market resolved UP, and the DOWN token went
to $0.00, resulting in a total loss.

Proximity was **0.0005%** — the outcome was decided by a single cent on a
$2,046 asset. The proximity filter would have blocked this trade.

---

## Trade Details

| Field              | Value                                                              |
|--------------------|--------------------------------------------------------------------|
| Order ID           | `0x53e6c983622dd9685dd32bb25eda3836c85afdc8f8ca08997c457d83a54083a5` |
| Slug               | `eth-updown-5m-1773304800`                                        |
| Side               | BUY (bought the DOWN token)                                        |
| Price              | $0.999                                                             |
| Size               | 125                                                                |
| Cost               | $124.875                                                           |
| Exit Price         | $0.00 (DOWN token resolved to zero)                                |
| Net P&L            | **-$124.875**                                                      |
| Strategy           | sweep                                                              |
| Trigger            | tick_size_change                                                   |
| Best Bid / Ask     | 0.998 / 0.999                                                      |

---

## Timeline

All times in EDT (UTC-4).

| Timestamp              | Epoch              | Event                    |
|------------------------|--------------------|--------------------------|
| 4:40:00.000 AM         | 1773304800.000     | Market opens             |
| 4:44:51.232 AM         | 1773305091.232     | Tick-size signal fires   |
| 4:44:51.232 AM         | 1773305091.232     | Order placed (REST sent) |
| 4:44:51.678 AM         | 1773305091.678     | REST response received   |
| 4:44:57.876 AM         | 1773305097.876     | Fill confirmed (WS)      |
| 4:45:00.000 AM         | 1773305100.000     | Market window closes     |
| 4:50:09.755 AM         | 1773305409.755     | Market resolved (UP)     |

---

## Latency Metrics

| Metric                        | Value       |
|-------------------------------|-------------|
| Time to expiry at placement   | 8.77 s      |
| Signal → REST response        | 445.48 ms   |
| Signal → Fill                 | 6,644 ms    |
| Fill → Market close           | 2.12 s      |
| Market close → Resolution     | 309.75 s    |

---

## Proximity Analysis

### Polymarket Oracle Data (correct source)

| Metric                                | Value         |
|---------------------------------------|---------------|
| Price to beat (strike)                | **$2,046.04** |
| Final price                           | **$2,046.05** |
| Absolute difference                   | $0.01         |
| **Proximity** (`|final-strike|/strike`) | **0.0005%**  |
| Outcome                               | UP (final > strike by $0.01) |

The outcome was decided by **one cent** on a $2,046 asset — a ratio of
1:204,604. The proximity filter threshold of 0.05% (0.0005) would have blocked
this trade.

### Binance US vs Polymarket Discrepancy

| Source          | Price at market open |
|-----------------|---------------------|
| Polymarket      | $2,046.04           |
| Binance US      | $2,046.12           |
| **Discrepancy** | **$0.08**           |

Binance US is **not** the oracle Polymarket uses for ETH price resolution.
The `fetch_strike_price()` function in `src/utils/market_data.py` currently
queries Binance (global/US) klines — this $0.08 gap shows that the strike
price the bot computes may differ from Polymarket's actual price-to-beat.

For accurate proximity filtering, the bot should prefer the Gamma API's
`priceToBeat` field when available, falling back to Binance only when Gamma
does not provide it.

---

## Correlation with XRP Loss

This trade occurred within the **same minute** as the XRP loss
(`xrp-updown-15m-1773304200`):

| Trade   | Signal Time      | Fill Time        | Loss      |
|---------|------------------|------------------|-----------|
| XRP 15m | 4:44:48.158 AM   | 4:44:57.735 AM   | -$124.88  |
| ETH 5m  | 4:44:51.232 AM   | 4:44:57.876 AM   | -$124.88  |

Both signals fired within 3 seconds of each other, and both fills landed within
141 ms of each other. Combined loss: **-$249.75** in under 10 seconds.

Both trades had proximity well below the 0.05% filter threshold.

---

## Mitigation

### 1. Proximity filter (immediate)

```env
PROXIMITY_FILTER_ENABLED=true
PROXIMITY_MIN_DISTANCE=0.0005       # Block trades with proximity < 0.05%
```

### 2. Strike price accuracy (improvement)

The Gamma API `priceToBeat` should be the primary source for strike price,
since it reflects the actual oracle value Polymarket uses for resolution.
Binance klines are a reasonable fallback but can diverge by several cents,
which matters when the proximity is measured in cents.

---

## Data Source

- **Order data**: `data/bot.db` → `orders` table
- **Fill data**: `data/bot.db` → `fills` table
- **Decision data**: `data/bot.db` → `decisions` table
- **Trade (P&L) data**: `data/bot.db` → `trades` table
- **Polymarket oracle**: User-provided from Polymarket UI (price to beat: $2,046.04, final: $2,046.05)
- **Binance US klines**: `ETHUSDT` 1m candle at epoch 1773304800000 (showed $2,046.12 — does not match Polymarket)
