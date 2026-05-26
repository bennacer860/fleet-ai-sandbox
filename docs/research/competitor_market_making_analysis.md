# Competitor Market-Making Strategy: Analysis from Live Trade Data

> **Source:** Live trade data captured from @certova and @ivy56 via `scripts/track_user_trades.py` on 2026-05-24, covering ~1.5 hours of trading across crypto, esports, and sports markets.
>
> **Related:** [gabagool_dual_sided_strategy.md](gabagool_dual_sided_strategy.md) — our implementation of a similar approach.

---

## Strategy Summary

Both @certova and @ivy56 run **continuous two-sided market making** on Polymarket binary markets. They post resting limit BUY orders on both outcomes (Up/Down, Yes/No) simultaneously, collect fills passively, sell excess inventory to rebalance, and hold paired positions to expiration.

The core principle: if you accumulate shares on both sides at a net combined average price below $1.00, you are guaranteed profit at resolution regardless of which side wins.

---

## How It Works: Step by Step

### Step 1 — Post limit orders on both sides

Post resting BUY limits on both outcomes at prices that sum to less than $1.00:

```
Limit BUY Up   @ $0.54    (resting on the book)
Limit BUY Down @ $0.44    (resting on the book)
Combined: $0.98
```

These sit on the order book and wait. You are a **maker**, not a taker.

### Step 2 — Wait for market takers to fill your orders

When someone wants to sell Up urgently (because BTC just dropped), they hit your $0.54 bid. When someone wants to sell Down (because BTC just rallied), they hit your $0.44 bid.

You get filled passively. The fills are random — sometimes one side fills first, sometimes both fill together, sometimes neither fills.

### Step 3 — Rebalance when imbalanced

If one side accumulates too many shares, sell some back to the market. This is not profit-taking — it's inventory management.

Example:
```
Position: 200 Up, 350 Down (imbalanced)
Action: SELL 100 Down at market
New position: 200 Up, 250 Down (closer to balanced)
```

The sell proceeds reduce your net cost for that side, improving the combined price.

### Step 4 — Hold to expiration

At expiration, one outcome pays $1.00 per share, the other pays $0. For the **paired portion** (min of Up, Down), one side always wins:

```
Paired: 200 shares
Payout: 200 × $1.00 = $200.00
Net cost: 200 × $0.98 = $196.00
Profit: $4.00 (guaranteed regardless of outcome)
```

Any **orphan shares** (the imbalanced portion) are a directional bet that may win or lose.

---

## Where the Edge Comes From

The bid-ask spread. At any moment the order book looks like:

```
Up:   bid = $0.53   ask = $0.55
Down: bid = $0.44   ask = $0.46
```

- A **taker** who buys both sides at the ask pays: $0.55 + $0.46 = **$1.01** (loss)
- A **maker** who gets filled at the bid receives: $0.53 + $0.44 = **$0.97** (profit)

The spread is the maker's compensation for providing liquidity.

---

## What To Do When the Market Moves Against You

This is the critical decision point. You posted both sides, but only Down filled. Then BTC rallies — Down drops to $0.20 and Up rises to $0.80.

### Option A — Keep your Up limit alive and wait

Your Up limit is still at $0.54. If BTC reverses, Up may drop back to $0.54 and fill.

- If it fills: Combined = $0.44 + $0.54 = $0.98 (profit)
- If it doesn't fill: Fall back to Option B

### Option B — Sell Down and walk away

Sell Down at the current price ($0.20). Take the $0.25 loss. Start fresh on the next event.

### Option C — Buy Up at the new market price (DO NOT DO THIS)

Buy Up at $0.80. Combined = $0.44 + $0.80 = $1.24. **Guaranteed loss.** This is the trap.

### Option D — Post a new Up limit below the current market

Cancel your old limit, post a new one: Limit BUY Up @ $0.54 (below the current $0.80 price). Wait for a reversal.

**The data shows competitors always use Option A or D.** They never chase. They accept the small loss on events where only one side fills.

---

## Evidence From the Data

### @certova: Crypto 5-min Markets (BTC, ETH, SOL, XRP)

| Metric | Value |
|--------|-------|
| Total trades | 21,420 |
| Events traded | 82 |
| BUY/SELL ratio | 88% / 12% |
| Volume deployed | $139,213 |
| **Paired P&L** | **+$54.34** |
| **Return on volume** | **+0.039%** |

### Net Combined Prices by Market (Using NET positions: BUY - SELL)

| Market | Net Combined | Paired P&L | Result |
|--------|-------------|------------|--------|
| BTC 5-min | 0.9966 | +$323 | Profitable |
| BTC 15-min | 0.9971 | +$38 | Profitable |
| ETH 5-min | 0.9938 | +$54 | Profitable |
| ETH 15-min | 0.9933 | +$34 | Profitable |
| SOL 5-min | 1.0006 | -$2 | Near breakeven |
| SOL 15-min | 0.9995 | +$0.48 | Near breakeven |
| XRP 5-min | 1.0004 | -$6 | Near breakeven |
| XRP 15-min | 0.9991 | +$1 | Profitable |

### Event-Level BTC 5-min Breakdown

| Event | Net Combined | Paired P&L |
|-------|-------------|------------|
| 12:35 | 1.0021 | -$27 |
| 12:40 | 1.0108 | -$129 |
| 12:45 | 0.9905 | +$62 |
| 12:50 | 0.9910 | +$48 |
| 12:55 | 0.9926 | +$57 |
| 13:00 | 0.9967 | +$11 |
| 13:05 | 1.0004 | -$3 |
| 13:10 | 1.0000 | $0 |
| 13:15 | 0.9974 | +$22 |
| 13:20 | 1.0018 | -$10 |
| 13:25 | 0.9997 | +$1 |
| 13:30 | 0.9980 | +$15 |
| 13:35 | 0.9884 | +$47 |

Winners: 8 events, avg profit +$33. Losers: 4 events, avg loss -$42. Net: **+$95**.

### Cross-User Comparison: @certova vs @ivy56

| Metric | @certova | @ivy56 |
|--------|----------|--------|
| Crypto 5-min trades | 21,420 | 36,995 |
| Sell ratio | 11.9% | 12.1% |
| Volume | $139k | $238k |
| Paired P&L | +$54 | -$618 |
| Return | +0.039% | -0.260% |

Both users trade identical markets at the same times with the same strategy. @certova achieves slightly better fill prices, suggesting a faster or smarter bot. They are likely competing for the same liquidity.

---

## Why It Only Works at Scale

A single event has two possible outcomes: the paired P&L (small, deterministic) and the orphan P&L (potentially large, random). On any individual event, the random component dominates.

Across many events, the orphan P&L washes out (50% win, 50% lose) and the paired P&L accumulates:

```
1 event:    P&L = $33 or -$42 (coin flip)
10 events:  P&L ≈ +$90 ± $120 (noisy)
100 events: P&L ≈ +$900 ± $400 (edge visible)
1000 events: P&L ≈ +$9,000 ± $1,200 (consistent)
```

This is why the competitors run on every 5-minute crypto window, every esports match, and every sports event simultaneously.

---

## Why Esports Markets Are Attractive

| Property | Crypto 5-min | Esports (Dota/LoL) | Sports (EPL) |
|----------|-------------|---------------------|--------------|
| Duration | 5 min | 45-60 min | 90+ min |
| Price swing | 0.70 | 0.66 | 0.22 |
| Volume per event | $12k | $104k | $67k |

Esports games produce constant price-moving action (kills, objectives, team fights) over 45-60 minutes. This creates wide spreads and high fill rates for market makers. Sports events have sparser action (goals happen every ~30 minutes).

---

## Key Risks

### 1. Imbalance / Orphan Risk
Only one side fills, leaving a naked directional bet. Mitigated by running across many events so directional risk diversifies away.

### 2. Fee Erosion
With a ~0.04% gross edge, even small fees can destroy profitability. Posting limit orders (maker) is critical to avoid taker fees.

### 3. Adverse Selection
Informed traders (who know BTC direction from faster data feeds) trade against your stale limits. The 5-minute window limits exposure since the outcome is imminent.

### 4. Competition
Multiple bots compete for the same spread. As more bots enter, the spread compresses and the edge shrinks. Evidence: @ivy56 trades 1.7x more volume than @certova but loses money, suggesting the market is near capacity.

---

## Accounting for SELL Trades

The SELLs in the data are not profit-taking — they are **rebalancing trades** that reduce directional exposure. When analyzing P&L, you must use NET positions:

```
Net position = BUY size - SELL size
Net cost = BUY cost - SELL proceeds
Net combined = (Net Up cost / Net Up size) + (Net Down cost / Net Down size)
```

Ignoring SELLs and treating all trades as BUYs produces wildly incorrect results (e.g., BTC 5-min appears to lose $4,500 when it actually makes +$323).

---

## Data Limitations

This analysis is based on ~1.5 hours of forward-captured trade data. Key gaps:

1. **Sample size**: 17 BTC 5-min events is insufficient for statistical confidence. Need 200+ events.
2. **No outcome data**: We don't know which side won, so orphan P&L is unknown. Only paired P&L (guaranteed portion) is calculated.
3. **No maker/taker flag**: We infer market making from the pattern, but can't confirm order types.
4. **No fee data**: Fee rates are not populated in the trade data. True net P&L may differ.
5. **Historical data needed**: Use `fetch_wallet_trades.py` to pull multi-day history for robust validation.
