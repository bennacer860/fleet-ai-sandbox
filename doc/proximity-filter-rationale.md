# Proximity Filter Rationale — Why 0.10%

## Problem

The bot trades binary crypto Up/Down markets on Polymarket. It buys the leading
outcome at $0.999 near expiry, earning $0.001 per unit when the market resolves
in its favor. When the spot price is too close to the strike, the outcome is
effectively a coin flip — and a loss means losing the entire $0.999 per unit.

Between March 2 and March 12, 2026, the bot recorded **4 losses totaling
$259.76**, all on trades where the crypto spot price was within a razor-thin
margin of the strike price at trade time.

---

## Loss History

| # | Market | Date | P&L | Proximity | TTE |
|---|--------|------|-----|-----------|-----|
| 1 | `xrp-updown-15m-1773304200` | Mar 12, 4:44 AM | -$124.88 | 0.0435% | 11.84s |
| 2 | `eth-updown-5m-1773304800`  | Mar 12, 4:44 AM | -$124.88 | 0.0000% | 8.77s |
| 3 | `eth-updown-15m-1772476200` | Mar 2, 1:44 PM  | -$5.00   | 0.0875% | 8.29s |
| 4 | `btc-updown-5m-1772472300`  | Mar 2, 1:25 PM  | -$5.00   | 0.0109% | — |

**Proximity** = `|spot_price - strike_price| / strike_price`

All four losses occurred when proximity was below **0.10%**.

---

## Threshold Analysis

### At 0.05% (`PROXIMITY_MIN_DISTANCE=0.0005`)

| Trade | Proximity | Blocked? |
|-------|-----------|----------|
| XRP 15m (Mar 12) | 0.0435% | Yes |
| ETH 5m (Mar 12) | 0.0000% | Yes |
| ETH 15m (Mar 2) | 0.0875% | **No** |
| BTC 5m (Mar 2) | 0.0109% | Yes |

Blocks 3 of 4 losses. The Mar 2 ETH trade (proximity 0.0875%) slips through
because it was above the 0.05% threshold despite being a losing trade.

**Losses saved: $254.76 / $259.76**

### At 0.10% (`PROXIMITY_MIN_DISTANCE=0.001`)

| Trade | Proximity | Blocked? |
|-------|-----------|----------|
| XRP 15m (Mar 12) | 0.0435% | Yes |
| ETH 5m (Mar 12) | 0.0000% | Yes |
| ETH 15m (Mar 2) | 0.0875% | Yes |
| BTC 5m (Mar 2) | 0.0109% | Yes |

Blocks **all 4 losses**.

**Losses saved: $259.76 / $259.76**

---

## What 0.10% Means in Dollar Terms

| Asset | Price | 0.10% distance |
|-------|-------|----------------|
| BTC | ~$71,600 | $71.60 |
| ETH | ~$2,125 | $2.13 |
| XRP | ~$1.41 | $0.0014 |
| SOL | ~$140 | $0.14 |

At 0.10%, the filter requires the spot price to be at least this far from the
strike for the trade to proceed. For BTC, that means the price must have moved
at least ~$72 from the open — a meaningful directional signal. For XRP, it's
$0.0014 — still very tight, but enough to filter out pure noise.

---

## Trade-off: Blocked Wins

A higher threshold will also block some trades that would have won. These are
cases where the spot was close to the strike but still ended up on the right
side. However:

- The bot earns **$0.001 per unit** on a win ($0.125 on a 125-unit trade).
- The bot loses **$0.999 per unit** on a loss ($124.875 on a 125-unit trade).
- The risk/reward ratio is **1:999** — one loss wipes out 999 wins of equal size.

This asymmetry means blocking a few marginal wins is a negligible cost compared
to preventing even one loss. A trade blocked at 0.10% proximity that would have
won only earns $0.125. A trade allowed at 0.10% proximity that loses costs
$124.875. You need to miss **999 winning trades** to equal the cost of one loss.

---

## Configuration

```env
PROXIMITY_FILTER_ENABLED=true
PROXIMITY_MIN_DISTANCE=0.001
```

Or via CLI (if added):

```bash
python main.py run --proximity-filter --proximity-min-distance 0.001
```

### Post-expiry bypass

The proximity filter is **automatically bypassed** for post-expiry trades
(where `time_to_expiry < 0`). After the market has expired, the outcome is
already determined, so proximity is irrelevant and the trade is safe.

---

## Price Data Sources

The proximity calculation depends on two values:

1. **Strike price**: Sourced from Polymarket's Gamma API `eventMetadata.priceToBeat`
   field. This is the exact Chainlink Data Streams price used for market resolution.
   Falls back to Binance kline open price if Gamma doesn't provide it (rare).

2. **Spot price**: Real-time Binance WebSocket via `@trade` and `@miniTicker`
   combined streams. Sub-second freshness during active trading hours.

### Known discrepancy

Polymarket resolves using **Chainlink Data Streams**, not Binance directly.
The Gamma API `priceToBeat` matches the Chainlink oracle exactly. Binance
spot prices can diverge by a few cents to a few dollars depending on the asset
and market conditions (observed: $0.08 gap on ETH, ~$57 on BTC).

Since the strike comes from Gamma (Chainlink) and the spot comes from Binance,
there is an inherent small discrepancy. The 0.10% threshold provides enough
margin to absorb this difference.

---

## Detailed Loss Analyses

- [`xrp-updown-15m-1773304200-loss-analysis.md`](xrp-updown-15m-1773304200-loss-analysis.md)
- [`eth-updown-5m-1773304800-loss-analysis.md`](eth-updown-5m-1773304800-loss-analysis.md)
