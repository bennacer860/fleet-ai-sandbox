# XRP $49.95 Loss — Post-Mortem Analysis

**Date**: March 15, 2026
**Market**: `xrp-updown-15m-1773541800`
**Window**: 10:30:00 PM – 10:45:00 PM EDT (March 14) / 02:30:00 AM – 02:45:00 AM UTC (March 15)

---

## Summary

The bot bought the **DOWN** outcome at $0.999 × 50.0 ($49.95) with approximately 61 seconds remaining before market close. XRP was sitting at $1.4139, which was 0.1201% below the strike price of $1.4156. Although the **Proximity Filter** was active (threshold 0.10%), the price breached the strike in the final minute, resulting in a total loss.

This trade demonstrates that a **0.1% proximity buffer** is insufficient to protect against "sweep" risk in the final 60 seconds of a volatile market.

---

## Trade Details

| Field              | Value                                                              |
|--------------------|--------------------------------------------------------------------|
| Order ID           | `0x175e787ae4611d69fd5cf4affe4d07efb99da8bb48584c8cc08c868c14bf2b5d` |
| Slug               | `xrp-updown-15m-1773541800`                                       |
| Side               | BUY (bet on DOWN)                                                  |
| Price              | $0.999                                                             |
| Size               | 50.0                                                               |
| Cost               | $49.95                                                             |
| Exit Price         | $0.00 (outcome was UP)                                             |
| Net P&L            | **-$49.95**                                                        |
| Strategy           | sweep                                                              |
| Trigger            | tick_size_change                                                   |

---

## Timeline

All times in EDT (UTC-4) based on March 14/15 transition.

| Timestamp              | Epoch              | Event                     |
|------------------------|--------------------|---------------------------|
| 10:30:00.000 PM        | 1773541800.000     | Market opens              |
| 10:43:58.213 PM        | 1773542638.213     | Tick-size signal fires    |
| 10:43:58.213 PM        | 1773542638.213     | Order placed (REST sent)  |
| 10:44:11.741 PM        | 1773542651.741     | Fill confirmed (WS)       |
| 10:45:00.000 PM        | 1773542700.000     | Market window closes      |
| 10:49:14.000 PM        | 1773542954.000     | Market resolved (UP)      |

---

## Latency Metrics

| Metric                        | Value       |
|-------------------------------|-------------|
| Time to expiry at placement   | 61.79 s     |
| Signal → REST response        | 758.03 ms   |
| Signal → Fill                 | 13,528 ms   |
| Fill → Market close           | 48.26 s     |
| Market close → Resolution     | 254.00 s    |

---

## Proximity Analysis

Data captured by the bot's execution engine at the time of trade.

| Metric                              | Value       |
|--------------------------------------|-------------|
| Strike price (XRP at 10:30:00 PM)    | $1.4156     |
| Spot price (XRP at 10:43:58 PM)      | $1.4139     |
| Absolute difference                  | $0.0017     |
| **Proximity** (`|spot-strike|/strike`) | **0.1201%** |
| Direction at entry                   | DOWN (spot < strike) |

### Interpretation

- The spot price was **$0.0017 below the strike**.
- The bot correctly identified **DOWN** as the leading outcome and "swept" the 0.001 spread at $0.999.
- Proximity was **0.1201%**, which was just above the `P2_PROXIMITY_MIN_DISTANCE` threshold of **0.10%** (`0.001`).
- Because it was above the threshold, the filter allowed the trade.
- XRP price reversed and climbed >0.12% in the final 60 seconds, crossing the strike price.

---

## Mitigation

The **Proximity Filter** worked exactly as configured, but the configuration was too aggressive for a volatile asset like XRP with 60 seconds of exposure.

### Current Configuration (Profile 2)

```env
P2_PROXIMITY_FILTER_ENABLED=true
P2_PROXIMITY_MIN_DISTANCE=0.001      # 0.1% threshold
```

### Proposed Adjustment

To avoid similar losses from "last-minute whiplash" on Profile 2 (which uses larger $50 sizes), the proximity threshold should be widened.

1. **Increase Proximity Bound**: Raise `P2_PROXIMITY_MIN_DISTANCE` to `0.002` (0.2%) or `0.0025` (0.25%).
2. **Time-Based Scaling**: (Future Improvement) Consider making the proximity threshold stricter as $TTE$ (Time To Expiry) increases.

---

## Data Source

- **Order data**: `data/bot_p2.db` → `orders` table
- **Decision data**: `data/bot_p2.db` → `decisions` table
- **Trade (P&L) data**: `data/bot_p2.db` → `trades` table (Trade ID: `..._1773542954`)
- **Spot/Strike Reference**: Log entry `2026-03-14 22:49:14` for resolution and order placement details.
