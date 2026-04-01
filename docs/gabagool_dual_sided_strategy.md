# Gabagool: Dual-Sided Continuous Accumulation Variant

## Overview

This document describes an alternative gabagool execution style commonly used by other binary-arb participants on Polymarket: **dual-sided continuous accumulation**. Instead of picking one leg per book update and stopping once profit is locked, this variant simultaneously posts resting limit orders on **both** YES and NO outcomes and keeps accumulating shares as long as the combined spread is profitable.

The core math is identical — `avg_YES + avg_NO < 1.00` guarantees profit at resolution — but the execution loop, position management, and exit conditions differ significantly from our current single-leg-at-a-time approach.

---

## How the Current Strategy Works (Baseline)

For context, our current gabagool implementation:

1. Observes the book for `observation_ticks` updates, then activates if the `TrendDetector` criteria are met.
2. Enters a **probe** phase — places small BUY orders on one side at a time, preferring the lighter leg.
3. Once both legs have at least one fill, transitions to **build** phase with full size.
4. Guards against imbalance (`max_imbalance`) and excess pair cost (`max_pair_cost`).
5. Once `locked_profit > 0` (i.e. `min(qty_yes, qty_no) > cost_yes + cost_no`), the strategy **stops entirely** for that slug.
6. Holds to resolution. No active selling/unwinding.

Key constraint: `pick_side` returns **at most one side** per book update. The strategy never places YES and NO orders simultaneously.

---

## How Dual-Sided Continuous Accumulation Works

### Core Loop

On every book update (or on a fixed interval), the strategy:

1. Reads the best ask for YES and the best ask for NO.
2. If `ask_YES + ask_NO < max_pair_cost`, submits a BUY limit order for **both** YES **and** NO in the same cycle.
3. As fills come in on either side, immediately replaces the filled order with a new one at the current best ask.
4. Repeats indefinitely as long as the combined spread remains under the threshold.

There is no concept of "pick one side." Both sides are always being worked.

### Order Management

| Aspect | Current Strategy | Dual-Sided Variant |
|--------|------------------|--------------------|
| Orders per cycle | 0 or 1 | 0 or 2 |
| Order type | Limit at best ask | Limit at or near best ask (sometimes slightly below) |
| Resting orders | None (fire and forget) | Typically maintains 1 resting order per side |
| Cancel/replace | Not used | Active — stale orders are cancelled and replaced as the book moves |
| Dedup | Per `(slug, token_id)` per day | No dedup — intentionally re-submits on the same token repeatedly |

### Position Management

The dual-sided approach does **not** enforce strict leg-by-leg balance during accumulation. At any point, the position might look like:

```
YES: 47 shares @ avg 0.54
NO:  31 shares @ avg 0.43
```

This is acceptable because:

- The **31 matched pairs** (min of the two legs) have a locked profit of `31 × (1.00 - 0.54 - 0.43) = 31 × 0.03 = $0.93`.
- The **16 excess YES shares** are an unhedged directional bet. If YES wins, they pay out an additional `16 × (1.00 - 0.54) = $7.36`. If NO wins, they lose `16 × 0.54 = $8.64`.
- The strategy keeps working the NO side to narrow the imbalance, but doesn't halt YES accumulation while waiting.

### Phases

The dual-sided variant often uses a simpler phase model:

1. **Accumulate**: Both sides active. This is the only "trading" phase. No probe/build distinction — size is constant or scales with available balance.
2. **Cooldown** (optional): If the spread narrows beyond a threshold (e.g. `ask_YES + ask_NO > 0.995`), pause order placement but keep resting orders alive.
3. **Hold**: Market approaching resolution. Stop placing new orders, let existing resting orders remain until filled or expired.

There is no "locked" phase that halts trading. Profit-locking is a continuous process, not a terminal state.

---

## Detailed Examples

### Example 1: Profitable Accumulation on a BTC 15m Market

**Setup**: `btc-updown-15m-1775000000`, YES ask = 0.55, NO ask = 0.43, combined = 0.98.

| Time | Action | YES Pos | NO Pos | Matched Pairs | Locked P&L |
|------|--------|---------|--------|----------------|------------|
| T+0 | BUY 5 YES @ 0.55, BUY 5 NO @ 0.43 | — | — | 0 | $0.00 |
| T+1 | NO fills (5 @ 0.43) | 0 | 5 | 0 | $0.00 |
| T+2 | YES fills (5 @ 0.55). Repost: BUY 5 NO @ 0.44 | 5 | 5 | 5 | $0.10 |
| T+3 | Book moves. YES ask = 0.53, NO ask = 0.44. BUY 5 YES @ 0.53, keep NO order alive | 5 | 5 | 5 | $0.10 |
| T+4 | YES fills (5 @ 0.53). NO fills (5 @ 0.44) | 10 | 10 | 10 | $0.30 |
| T+5 | Repost both. YES ask = 0.56, NO ask = 0.43. Combined = 0.99 > 0.98 threshold | 10 | 10 | 10 | $0.30 |
| T+5 | Spread too tight — **cooldown**, no new orders | 10 | 10 | 10 | $0.30 |
| T+8 | Spread widens again. YES ask = 0.54, NO ask = 0.44. Resume | 10 | 10 | 10 | $0.30 |
| T+9 | Both fill (5 each) | 15 | 15 | 15 | $0.60 |

**Resolution**: YES wins. Payout = 15 × $1.00 = $15.00. Total cost = `15×0.54 + 15×0.4367 = $8.10 + $6.55 = $14.65`. **P&L = +$0.35** (before fees).

Note: the strategy kept accumulating after the first pair locked profit. Our current strategy would have stopped at T+2.

### Example 2: Imbalanced Accumulation with Directional Upside

**Setup**: Same market. YES fills are faster than NO fills because the YES book is thicker.

| Time | YES Pos | NO Pos | Excess | Matched P&L | Directional Exposure |
|------|---------|--------|--------|-------------|---------------------|
| T+2 | 10 | 5 | 5 YES | $0.10 | 5 × $0.55 = $2.75 unhedged |
| T+5 | 20 | 10 | 10 YES | $0.30 | 10 × $0.55 = $5.50 unhedged |
| T+8 | 25 | 20 | 5 YES | $0.70 | 5 × $0.55 = $2.75 unhedged |

If YES wins: P&L = matched profit ($0.70) + excess profit (5 × $0.45) = **+$2.95**.
If NO wins: P&L = matched profit ($0.70) − excess loss (5 × $0.55) = **−$2.05**.

The matched-pair profit cushions the directional loss, but a large enough imbalance can result in a net loss.

### Example 3: Spread Collapse (Adverse Scenario)

**Setup**: Market starts with 3-cent spread, then compresses to 0 as resolution approaches.

| Time | YES Ask | NO Ask | Combined | Action |
|------|---------|--------|----------|--------|
| T+0 | 0.54 | 0.43 | 0.97 | Buy both |
| T+3 | 0.55 | 0.44 | 0.99 | Cooldown |
| T+6 | 0.58 | 0.42 | 1.00 | No trade (at par) |
| T+9 | 0.62 | 0.39 | 1.01 | No trade (negative edge) |

Here the strategy only accumulated during T+0 through T+3. The spread collapsed before significant size was built. This is the typical "market is too efficient" scenario — low volume, small profit.

---

## When Does It Exit?

The dual-sided strategy has **no active exit** (no selling into the book). All positions are held to resolution. This is the same as our current approach.

Exit is passive:

| Event | What Happens |
|-------|-------------|
| **Market resolves YES** | YES shares pay $1 each. NO shares pay $0. Net P&L = `qty_yes × $1 − total_cost`. |
| **Market resolves NO** | NO shares pay $1 each. YES shares pay $0. Net P&L = `qty_no × $1 − total_cost`. |
| **Resolution timing** | Polymarket binary markets (e.g. BTC 15m up/down) resolve on a fixed schedule. No decision needed — you just hold. |

For the **matched pairs** (`min(qty_yes, qty_no)` shares), the outcome doesn't matter. One side always pays $1, and the combined cost was < $1, so profit is locked regardless.

For the **excess shares** (the imbalance), the outcome matters. This is directional risk.

### Why No Active Exit?

1. **Selling costs money.** Polymarket charges taker fees on sells. Selling both legs to flatten would cost ~2× fees and destroy the edge.
2. **No need.** The arb profit is realized at resolution. Selling early only makes sense if you believe the spread will go negative (combined > $1.00), which shouldn't happen if you bought correctly.
3. **Liquidity.** On short-duration markets (15m), resolution is minutes away. There's no time to unwind, and the book may be thin.

---

## Risks

### 1. Imbalance Risk (Primary)

If fills on one side consistently outpace the other, the excess shares become a naked directional bet.

**Severity**: Medium-High. On a binary market, a directional bet has a ~50% chance of losing (before any edge from pricing).

**Mitigation**:
- Set a `max_imbalance` ratio (e.g. 3:1). If one side is 3× the other, stop buying that side until the other catches up.
- Use smaller order sizes on the heavier side.
- Monitor fill rates per side and adjust posting aggressiveness.

**Quantified worst case**: With 100 YES at avg 0.55 and 30 NO at avg 0.43:
- Matched profit: `30 × (1.00 − 0.55 − 0.43) = $0.60`
- Excess YES if NO wins: `−70 × 0.55 = −$38.50`
- **Net: −$37.90**

### 2. Fee Erosion

Every fill incurs taker fees. With a 2-3 cent edge per pair and 100 bps fees on each leg:

- Fee per pair: `0.55 × 0.01 + 0.43 × 0.01 = $0.0098`
- Edge per pair: `1.00 − 0.55 − 0.43 = $0.02`
- Net edge after fees: `$0.02 − $0.0098 = $0.0102`

Fees consume ~49% of the gross edge. At 200 bps taker, fees consume ~98% and the strategy is unviable.

**Mitigation**: Post limit orders slightly below the best ask to potentially earn maker rebates (0 bps on Polymarket) instead of paying taker fees. This reduces fill rate but preserves edge.

### 3. Adverse Selection

The book moves against you because someone with better information is trading. You buy YES at 0.55, then the true value shifts to 0.60 — the NO side now costs 0.41, but your avg YES is too expensive.

**Severity**: Low on 15m binary markets (short lifespan, price is anchored to time-weighted probability). Higher on longer-duration or event-driven markets.

**Mitigation**: Only trade markets with mean-reverting price behavior (15m crypto up/down markets are ideal). Avoid markets with strong directional catalysts.

### 4. Partial Fill / Orphan Leg Risk

You post both sides. YES fills immediately (marketable). NO rests and the book moves away — NO never fills. You're left with a naked YES position.

**Severity**: Medium. This is structurally similar to imbalance risk but happens at the individual-order level.

**Mitigation**:
- Cancel unfilled resting orders if the other leg fills and the spread moves adversely.
- Track per-slug orphan rate. If a market consistently produces orphans, reduce size or skip it.
- Our existing `OrderManager` min-notional guard prevents the most common orphan cause (sub-$1 rejects on the cheap leg).

### 5. Inventory / Capital Lock-Up

Shares are locked until resolution. With continuous accumulation, capital deployed grows linearly with time and number of active markets.

**Severity**: Low-Medium. On 15m markets, capital is returned quickly. On longer markets, this can tie up significant USDC.

**Quantified**: At 5 shares/side, $0.55 avg price, across 50 concurrent markets = `50 × 2 × 5 × 0.50 = $250` deployed at any time. Scales linearly with size and market count.

### 6. Exchange Rejection / Rate Limiting

Posting 2 orders per cycle across many markets can exceed `MAX_ORDERS_PER_MINUTE` or trigger Polymarket's rate limiter.

**Severity**: Medium. Rejections create orphan legs (one side submitted, other rejected).

**Mitigation**:
- Stagger submissions across markets (not all at once).
- Respect `MAX_ORDERS_PER_MINUTE` globally.
- Use cancel/replace instead of new-order for resting orders (fewer API calls).

### 7. Spread Compression at Scale

Your own orders can move the book. Posting large BUYs on both sides narrows the spread for everyone, including you.

**Severity**: Low on liquid markets (BTC), Medium-High on illiquid markets (DOGE, HYPE).

**Mitigation**: Cap order size relative to book depth. If the best ask is only 10 shares deep and you want 25, your fill will walk the book and worsen your avg price.

---

## Pros and Cons

### Pros

| Advantage | Explanation |
|-----------|-------------|
| **Higher throughput** | Working both sides simultaneously means 2× the fill opportunities per cycle. Over a 15m market lifespan, this can mean 5-10 completed pairs vs 1-2 with single-leg. |
| **Captures fleeting spreads** | A momentary dip on YES won't be missed while the strategy is busy filling NO. Both sides are always fishing. |
| **No wasted observation** | Removes the probe→build→locked lifecycle overhead. Starts accumulating immediately when the spread is right. |
| **Scales naturally** | More markets, more pairs, more profit. No per-slug terminal state. The strategy is stateless enough to run across hundreds of markets. |
| **Directional upside** | The imbalanced (excess) shares are free optionality. On average, across many markets, the directional component is noise that washes out. The matched-pair profit is the signal. |
| **Simpler logic** | No `TrendDetector`, no `PhaseManager`, no one-sided-first rules. Just: is the spread under threshold? Buy both. |

### Cons

| Disadvantage | Explanation |
|--------------|-------------|
| **Directional risk per-slug** | Any individual market can lose money if the imbalance is large and the wrong side wins. Requires diversification across many markets to smooth this out. |
| **Higher fee burden** | 2× the orders means 2× the fees per cycle. With thin edges, fees can eat the entire profit. |
| **More complex order management** | Requires cancel/replace logic, resting order tracking, and handling partial fills — our current `OrderManager` with per-day dedup would need significant rework. |
| **Capital intensive** | Continuous accumulation ties up more USDC than a strategy that stops after locking one pair. |
| **Orphan exposure** | If only one side fills per cycle, the orphan rate can be high. Needs active monitoring and position reconciliation. |
| **Harder to reason about P&L** | With matched pairs + directional exposure, per-market P&L is non-deterministic until resolution. Makes real-time risk monitoring harder. |

---

## Comparison Summary

| Dimension | Current (Single-Leg) | Dual-Sided Continuous |
|-----------|---------------------|-----------------------|
| Orders per update | 0 or 1 | 0 or 2 |
| Sides worked | One at a time | Both simultaneously |
| Terminal condition | Profit locked → stop | Spread gone → cooldown (resume if spread returns) |
| Imbalance tolerance | Strict (`max_imbalance` enforced) | Relaxed (tolerated, managed via soft caps) |
| Fills needed for profit | Both legs must fill at good prices | Every matched pair adds profit incrementally |
| P&L per slug | Deterministic once locked | Partially deterministic (matched) + stochastic (excess) |
| Best for | Conservative, low-volume, few markets | Aggressive, high-volume, many markets |
| Complexity | Lower (pure functions, no order tracking) | Higher (resting orders, cancel/replace, fill reconciliation) |

---

## Implementation Considerations

If moving toward dual-sided accumulation, the following components would need changes:

1. **`pick_side` → `pick_sides`**: Return both sides when both are viable, not just the best one.
2. **Remove one-sided-first guard**: Allow buying both sides from the start.
3. **Remove profit-locked stop**: Replace with a cooldown when the spread tightens, but resume if it widens.
4. **`OrderManager` dedup**: Disable or rework the per-day `(slug, token_id)` dedup for gabagool to allow repeated submissions.
5. **Resting order tracking**: Add state to track which orders are currently resting per side, and cancel/replace stale ones instead of posting duplicates.
6. **Imbalance soft cap**: Keep a `max_imbalance` but as a throttle (reduce size on the heavy side) rather than a hard block.
7. **Per-market capital budget**: Add a max notional per slug to prevent unbounded accumulation.
