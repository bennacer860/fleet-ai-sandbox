# Competitor Strategy Analysis: Portfolio-Hedged Cheap-Side Buying (@certova & @ivy56)

**Analysis Date:** May 25, 2026  
**Data Period:** 20 days (May 6–25, 2026) for P&L; 60 days (Mar 27–May 25, 2026) for position balance  
**Source:** Polymarket `/activity` API + CLOB `last-trade-price` for settlement outcomes  
**Method:** Actual settled P&L computed per position — not estimates. Position balance from net shares held per market event (BUY − SELL).

---

## Executive Summary

Both @certova and @ivy56 run a **portfolio-hedged cheap-side buying** strategy on Polymarket's short-term crypto Up/Down binary markets. They profit by systematically buying contracts at prices well below the fair value of $0.50, maintaining an aggregate 50/50 Up/Down balance across thousands of markets, and letting the law of large numbers do the rest.

Over 20 days:
- **@ivy56:** +$313,939 profit on $1.03M deployed — **30.5% ROI**
- **@certova:** +$273,503 profit on $1.12M deployed — **24.4% ROI**

---

## 20-Day Trading Summary

| Metric | @ivy56 | @certova |
|--------|--------|----------|
| Total trades | 76,497 | 70,793 |
| Unique markets | 10,920 | 10,458 |
| Buy volume | $1,028,665 | $1,123,001 |
| Sell volume | $292,791 | $262,933 |
| **Realized P&L** | **+$313,939** | **+$273,503** |
| **ROI on buy cost** | **30.5%** | **24.4%** |
| Win rate (positions) | 32.8% | 30.3% |
| Up/Down balance | 48/52 | 48/52 |

---

## The Core Strategy: Portfolio-Hedged Cheap-Side Buying

### What they do

1. **Buy the cheap side** of each market — the outcome with the lowest price, which is below its fair value of $0.50
2. **Spread across hundreds of markets per day** so individual outcomes don't matter
3. **Never take a directional view** — 48-52% Up/Down balance across all positions
4. **Occasionally sell early** (~9% of trades) when a position moves sharply in their favor

### Why it works

Short-term crypto Up/Down markets are approximately 50/50 coin flips. Contracts priced below $0.50 have positive expected value:

```
Contract bought at $0.10 → 50% chance pays $1.00
Expected value: $0.50
Expected profit per contract: $0.40 (400% return)

Contract bought at $0.30 → 50% chance pays $1.00
Expected value: $0.50
Expected profit per contract: $0.20 (67% return)
```

By buying thousands of contracts across many markets, variance is smoothed and the edge materializes reliably. They don't need to predict anything — they just need prices to be inefficient.

### Historical validation: Crypto prices are 50/50 coin flips

Analysis of historical price data confirms short-term price movements are statistically indistinguishable from coin flips across all timeframes and cryptos.

**Hourly intervals (1 year, ~10,000 hours per crypto):**

| Crypto | Up | Down | Up % | Z-score | Significant? |
|--------|-----|------|------|---------|--------------|
| **BTC** | 5,000 | 5,005 | 50.0% | -0.05 | No |
| **ETH** | 5,047 | 4,956 | 50.5% | +0.91 | No |
| **SOL** | 5,064 | 4,875 | 51.0% | +1.90 | No |
| **XRP** | 4,751 | 4,807 | 49.7% | -0.57 | No |

**5-minute intervals (~7 days, ~2,000 windows per crypto):**

| Crypto | Up | Down | Up % | Z-score | Significant? |
|--------|-----|------|------|---------|--------------|
| **BTC** | 1,009 | 991 | 50.4% | +0.40 | No |
| **ETH** | 994 | 1,000 | 49.8% | -0.13 | No |
| **SOL** | 957 | 954 | 50.1% | +0.07 | No |
| **XRP** | 672 | 675 | 49.9% | -0.08 | No |

**15-minute intervals (~7 days, ~660 windows per crypto):**

| Crypto | Up | Down | Up % | Z-score | Significant? |
|--------|-----|------|------|---------|--------------|
| **BTC** | 337 | 329 | 50.6% | +0.31 | No |
| **ETH** | 324 | 340 | 48.8% | -0.62 | No |
| **SOL** | 329 | 317 | 50.9% | +0.47 | No |
| **XRP** | 266 | 261 | 50.5% | +0.22 | No |

*Data source: CryptoCompare OHLC. Z-score > 1.96 would indicate significant deviation from 50/50 at p<0.05. None of the 12 crypto × timeframe combinations show significant deviation.*

### Why cheap contracts don't always lose

The contract price reflects the *current* market state, not the final outcome. A $0.10 "Down" contract means BTC is currently up — but it can reverse before expiry.

| Buy Price | Win Rate | Why? |
|-----------|----------|------|
| < 5c | 4.7% | Extreme underdog, but reversals still happen |
| 5–10c | 17.7% | ~1 in 6 reverses |
| 10–20c | 27.8% | Moderate underdog |
| 20–30c | 47.0% | Near fair value |
| 30–50c | 54.0% | Slight favorite |

*Data: @ivy56 + @certova positions, May 6–25, 2026.*

**Example timeline (BTC 15-min market):**
```
T-14min: BTC +0.8% from open → "Up" = $0.92, "Down" = $0.08
T-10min: Competitor buys "Down" at $0.08
T-5min:  BTC reverses, now +0.3%
T-2min:  BTC drops more, now -0.1%
T-0:     BTC closes DOWN → "Down" wins → $1.00 payout on $0.08 bet
```

The edge exists because markets misprice contracts as if current trends will continue, but short-term trends are just noise in a random walk.

---

## Per-Market Position Balance (60 Days)

Position type is classified per market event from net shares held (BUY − SELL). **Pure Up/Down** = 100% of net shares on one side. **Mixed** = 1–99% Up. **Aggregate balance** = total net Up shares ÷ total net shares across all markets in that bucket.

Data: `certova_2026-03-27_2026-05-25.csv` (248,072 trades, 24,691 markets), `ivy56_2026-03-27_2026-05-25.csv` (272,302 trades, 29,799 markets).

### @certova

| Position Type | Markets | % |
|---------------|---------|---|
| Pure Down (0% Up) | 11,656 | 47.2% |
| Pure Up (100% Up) | 11,882 | 48.1% |
| Mixed (1–99% Up) | 1,149 | 4.7% |

| Market Type | Pure Up | Pure Down | Both Sides | Aggregate Balance |
|-------------|---------|-----------|------------|-------------------|
| BTC-5min | 49.9% | 48.3% | 1.8% | 50.0% / 50.0% |
| BTC-15min | 45.9% | 46.5% | 7.6% | 48.6% / 51.4% |
| ETH-5min | 52.3% | 46.1% | 1.6% | 52.9% / 47.1% |
| ETH-15min | 48.3% | 45.9% | 5.8% | 50.6% / 49.4% |
| SOL | 45.8% | 50.9% | 3.4% | 45.7% / 54.3% |
| XRP | 48.3% | 47.7% | 4.0% | 49.8% / 50.2% |

### @ivy56

| Position Type | Markets | % |
|---------------|---------|---|
| Pure Down (0% Up) | 13,661 | 45.8% |
| Pure Up (100% Up) | 14,091 | 47.3% |
| Mixed (1–99% Up) | 2,041 | 6.8% |

| Market Type | Pure Up | Pure Down | Both Sides | Aggregate Balance |
|-------------|---------|-----------|------------|-------------------|
| BTC-5min | 49.0% | 47.4% | 3.6% | 51.0% / 49.0% |
| BTC-15min | 44.2% | 43.5% | 12.3% | 49.9% / 50.1% |
| ETH-5min | 51.8% | 45.8% | 2.4% | 52.7% / 47.3% |
| ETH-15min | 47.1% | 43.3% | 9.6% | 50.9% / 49.1% |
| SOL | 44.8% | 50.1% | 5.1% | 45.4% / 54.6% |
| XRP | 48.2% | 45.9% | 5.9% | 49.5% / 50.5% |

**Takeaway:** Over 60 days the pattern holds — ~95% of markets are one-sided (Pure Up or Pure Down), with aggregate share balance near 50/50 across thousands of markets. Two-sided activity concentrates in 15-min markets (5–12% vs 2–4% on 5-min). This confirms portfolio-level hedging, not per-market gabagool-style arbitrage.

---

## P&L by Price Bucket (20 Days, Actual Settled)

This is the clearest view of where the edge comes from:

| Avg Buy Price | @ivy56 Positions | @ivy56 P&L | @certova Positions | @certova P&L | Win Rate |
|---------------|-----------------|------------|-------------------|--------------|----------|
| **< 15c (lottery)** | 6,200 | **+$120,319** | 6,293 | **+$114,103** | 9–10% |
| **15–30c** | 1,209 | **+$133,044** | 1,011 | **+$121,625** | 41–43% |
| **30–50c** | 932 | **+$84,605** | 788 | **+$71,894** | 54–56% |
| 50–70c | 682 | +$217 | 614 | -$10,264 | 55–60% |
| **70c+ (momentum)** | 1,897 | **-$24,246** | 1,752 | **-$23,855** | 84–85% |

**Key finding:** The 70c+ positions are net losers despite an 84% win rate. When a high-conviction bet reverses, the loss ($0.70–0.98 per contract) outweighs many wins. The real edge is in cheap contracts (< 50c) where the math is skewed in their favor.

---

## P&L by Market Type

| Market Type | @ivy56 P&L | @certova P&L | Win Rate | Volume Share |
|-------------|------------|--------------|----------|--------------|
| **15min** | +$172,906 | +$161,262 | 24–28% | 26–27% |
| **5min** | +$54,865 | +$42,947 | 33–35% | 50% |
| **daily (BTC hourly)** | +$46,786 | +$48,264 | 54–58% | 11–13% |
| **1hour** | +$39,382 | +$21,030 | 35–42% | 11–12% |

15-min markets generate the most profit because bid-ask spreads are widest — contracts near expiry in 15-min markets can be bought very cheaply (2–7 cents) creating high convexity.

---

## Average Price Paid Per Share by Market

| Market | Positions | Total Cost | Total Shares | Avg Price |
|--------|-----------|------------|--------------|-----------|
| **SOL-15min** | 1,378 | $35K | 353K | **$0.100** |
| **XRP-15min** | 1,396 | $36K | 323K | **$0.110** |
| **ETH-15min** | 2,439 | $135K | 857K | **$0.157** |
| **BTC-15min** | 3,014 | $369K | 1.8M | **$0.204** |
| SOL-5min | 1,141 | $42K | 195K | $0.217 |
| ETH-5min | 2,684 | $169K | 672K | $0.252 |
| ETH-1hour | 830 | $126K | 497K | $0.253 |
| SOL-1hour | 480 | $57K | 212K | $0.269 |
| XRP-5min | 1,202 | $60K | 201K | $0.297 |
| BTC-5min | 5,526 | $803K | 2M | $0.396 |
| XRP-1hour | 480 | $62K | 155K | $0.400 |
| BTC-hourly | 808 | $257K | 598K | $0.430 |
| **TOTAL** | 21,378 | $2.15M | 7.9M | **$0.272** |

**Key insight:**
- **15-min markets** have the cheapest prices ($0.10–0.20) — best lottery tickets
- **5-min markets** are more expensive ($0.22–0.40) — less time for prices to collapse
- **Hourly markets** are most expensive ($0.25–0.43) — closer to fair value

This explains why 15-min markets have the highest ROI — they're getting contracts at half the price of 5-min/hourly markets.

---

## P&L by Market (Crypto × Duration)

Actual settled P&L over 20 days (May 6–25, 2026) by market type:

| Market | @ivy56 W/L | @ivy56 Win% | @ivy56 P&L | @ivy56 ROI | @certova W/L | @certova Win% | @certova P&L | @certova ROI |
|--------|------------|-------------|------------|------------|--------------|---------------|--------------|--------------|
| **BTC-15min** | 490/968 | 33.6% | +$124,136 | 70.3% | 478/1,078 | 30.7% | +$122,544 | 63.6% |
| **BTC-5min** | 1,129/1,725 | 39.6% | +$35,169 | 9.1% | 1,003/1,669 | 37.5% | +$26,798 | 6.4% |
| **BTC-hourly** | 221/165 | 57.3% | +$46,786 | 41.0% | 230/192 | 54.5% | +$48,264 | 33.8% |
| **ETH-15min** | 308/933 | 24.8% | +$29,505 | 43.7% | 266/932 | 22.2% | +$28,840 | 42.9% |
| **ETH-1hour** | 172/226 | 43.2% | +$35,324 | 56.7% | 162/270 | 37.5% | +$33,296 | 52.4% |
| **ETH-5min** | 413/1,083 | 27.6% | +$13,348 | 16.0% | 307/881 | 25.8% | +$11,052 | 12.9% |
| **SOL-15min** | 146/542 | 21.2% | +$9,650 | 51.2% | 114/576 | 16.5% | +$5,047 | 30.5% |
| **SOL-1hour** | 66/153 | 30.1% | -$243 | -0.9% | 56/205 | 21.5% | -$9,527 | -30.6% |
| **SOL-5min** | 171/456 | 27.3% | +$3,054 | 14.2% | 116/398 | 22.6% | +$2,312 | 11.1% |
| **XRP-15min** | 150/547 | 21.5% | +$9,615 | 52.5% | 135/564 | 19.3% | +$4,831 | 28.0% |
| **XRP-1hour** | 102/121 | 45.7% | +$4,301 | 15.9% | 108/149 | 42.0% | -$2,738 | -7.8% |
| **XRP-5min** | 218/415 | 34.4% | +$3,293 | 12.1% | 194/375 | 34.1% | +$2,785 | 8.5% |
| **TOTAL** | 3,586/7,334 | 32.8% | +$313,939 | 30.5% | 3,169/7,289 | 30.3% | +$273,503 | 24.4% |

**Key findings:**

- **BTC-15min** is the highest-profit market: +$246,681 combined (64–70% ROI)
- **BTC-5min** is the highest-volume market: 5,526 positions, +$61,968 P&L
- **SOL-1hour** is the only consistently losing market: -$9,770 combined
- **ETH-1hour** has the highest ROI: 52–57%

---

## Asset Mix

Both traders concentrate heavily on BTC, with similar allocations:

| Asset | @ivy56 Buy Volume | @certova Buy Volume |
|-------|-------------------|---------------------|
| BTC | $676,423 (66%) | $752,798 (67%) |
| ETH | $213,225 (21%) | $216,739 (19%) |
| XRP | $72,572 (7%) | $84,971 (8%) |
| SOL | $66,445 (6%) | $68,493 (6%) |

---

## Position Sizing (Shares)

Metrics below use **shares**, not USDC. For each market event, **total size** is the sum of all shares traded in that market (all BUY and SELL fills combined). **Net size** is BUY shares minus SELL shares — what remains held into settlement.

### Total shares per market event (60 Days)

Sum of all share volume (BUY + SELL fills) per market event. Data: Mar 27–May 25, 2026.

| Market Type | @certova Avg | @certova Median | @ivy56 Avg | @ivy56 Median |
|-------------|-------------|-----------------|------------|---------------|
| **BTC-5min** | 519 | 400 | 380 | 300 |
| **BTC-15min** | 816 | 799 | 631 | 598 |
| **ETH-5min** | 335 | 374 | 235 | 202 |
| **ETH-15min** | 500 | 400 | 395 | 300 |
| **SOL-5min** | 258 | 150 | 157 | 100 |
| **SOL-15min** | 309 | 270 | 248 | 207 |
| **XRP-5min** | 225 | 111 | 142 | 90 |
| **XRP-15min** | 299 | 249 | 240 | 200 |

#### By crypto × duration (60 days, avg total shares / market)

| Crypto | 5-min | 15-min |
|--------|-------|--------|
| **BTC** | 380–519 | 631–816 |
| **ETH** | 235–335 | 395–500 |
| **SOL** | 157–258 | 207–309 |
| **XRP** | 90–225 | 200–299 |

**Takeaway (60 days):** Sizing is stable vs the 20-day window — @certova runs slightly larger clips (median **400 shares** on BTC-5min, **~800 on BTC-15min**). @ivy56 is leaner on altcoins (median **100 shares** on SOL/XRP 5-min vs 150–111 for @certova). BTC-15min markets see the heaviest deployment (~600–800 total shares per event).

### Total shares per market event (20 Days)

| Market Type | @certova Avg | @certova Median | @ivy56 Avg | @ivy56 Median |
|-------------|-------------|-----------------|------------|---------------|
| **BTC-5min** | 417 | 400 | 363 | 300 |
| **BTC-15min** | 704 | 563 | 640 | 600 |
| **ETH-5min** | 291 | 378 | 231 | 291 |
| **ETH-15min** | 397 | 400 | 349 | 300 |
| **SOL-5min** | 199 | 121 | 152 | 93 |
| **SOL-15min** | 268 | 213 | 260 | 290 |
| **XRP-5min** | 190 | 88 | 155 | 94 |
| **XRP-15min** | 250 | 184 | 228 | 203 |

#### By crypto × duration (20 days, avg total shares / market)

| Crypto | 5-min | 15-min |
|--------|-------|--------|
| **BTC** | 363–417 | 640–704 |
| **ETH** | 231–291 | 349–397 |
| **SOL** | 152–199 | 260–268 |
| **XRP** | 155–190 | 228–250 |

### Net position size (after sells)

| Market Type | @certova Net | @ivy56 Net |
|-------------|-------------|------------|
| BTC-5min | 378 | 317 |
| BTC-15min | 568 | 489 |
| ETH-5min | 276 | 218 |
| ETH-15min | 361 | 300 |
| SOL-15min | 254 | 242 |
| XRP-15min | 238 | 211 |

### Per-fill size (individual trade)

Most fills are small; a few large clips pull the average up. Median fill size is ~10 shares on 5-min markets and ~20–27 on 15-min markets.

| Market Type | Avg Shares/Fill | Median Shares/Fill |
|-------------|-----------------|-------------------|
| BTC-5min | 46–55 | 10 |
| BTC-15min | 78–97 | 21–23 |
| ETH-5min | 65–81 | 18–19 |
| ETH-15min | 63–72 | 25–27 |
| SOL/XRP 15min | 31–39 | 12 |

**Takeaway:** Both traders deploy roughly **300–700 shares per BTC market**, **230–400 for ETH**, and **150–270 for SOL/XRP**. The recurring median net position of **~400 shares** on BTC/ETH suggests a standard clip size they scale into over several fills (~4–8 trades per market on BTC).

---

## Entry Timing

Both traders use identical timing patterns across all market types:

| Time to Expiry | % of Buys | Avg Price |
|----------------|-----------|-----------|
| **< 2 min** | 34% | 2–8 cents |
| **2–5 min** | 28–29% | 15–45 cents |
| **> 5 min** | 36–38% | 40–90 cents |

The < 2 min window is the highest-volume lottery ticket zone: prices collapse as expiry approaches and the outcome becomes apparent to directional traders, leaving heavily mispriced contracts for those willing to absorb variance.

---

## Sell Strategy

Sells represent 9–10% of total trades and serve one purpose: locking in gains before settlement risk.

- **Avg sell price:** $0.80–0.97
- **Avg time to expiry at sell:** 1–5 minutes
- **When they sell:** When a position moves from < $0.15 to > $0.90 before settlement

This is not market making — they are not posting both sides. They buy, hold, and occasionally exit winners early.

---

## What This Strategy Is NOT

- **Not arbitrage** — they rarely buy both sides of the same market simultaneously (5–7% of markets over 60 days)
- **Not market making** — they do not post limit orders on both sides; they take liquidity
- **Not directional trading** — the 48/52 Up/Down balance proves they have no price view
- **Not momentum trading** — the 70c+ buys consistently lose money, suggesting they are not following trends effectively

---

## Replication Requirements

To run this strategy profitably:

1. **Capital:** Minimum ~$50K deployed simultaneously for law-of-large-numbers to work
2. **Speed:** Must execute within seconds of identifying cheap contracts — prices move fast near expiry
3. **Scale:** Needs 200–500 markets per day; requires automated execution
4. **Balance tracking:** Must maintain 50/50 aggregate balance; no concentrated directional exposure
5. **Market selection:** Focus on 15-min BTC/ETH markets — highest edge per dollar deployed
6. **Fee awareness:** Trading fees directly reduce the spread edge; must trade above breakeven size per position
