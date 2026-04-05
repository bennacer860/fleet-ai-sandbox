# Reverse-Engineering Gabagool22: External Research Digest

> Summarised from [r/PredictionsMarkets](https://www.reddit.com/r/PredictionsMarkets/comments/1r7oylh/4_months_reverseengineering_gabagool22_what_i/) (posted ~Mar 2026).
> Original author spent 4 months building monitoring tools, collecting thousands of gabagool22's actual trades, and testing every hypothesis.

---

## Background

Gabagool22 is a Polymarket whale who consistently earns ~$10K/day trading crypto binary-options markets (BTC/ETH UP/DOWN across 5m, 15m, 60m, 4h windows). Monthly leaderboard shows $200K–$680K/month for top players. The post is a data-driven attempt to reverse-engineer his edge.

## Confirmed Execution Mechanics

| Property | Finding |
|---|---|
| Side | Maker only — every fill at best bid |
| Order size | ~15 shares per order (occasionally 5–10 partials), round-cent prices |
| Refresh cadence | Every 10–20 seconds; cancels stale, reposts at new best bid |
| Coverage | Buys **both** sides every window, never sells, holds to settlement |
| Fill pattern | Bursts of 3–15 same-side fills (single seller sweeping his resting orders) |
| Activity window | ~30 seconds after open until near settlement |

## The Real Edge

- Ends up **heavier on the winning side ~79%** of the time — statistically impossible by chance.
- A passive maker buying both sides equally would land on the **losing** side ~62% of the time (adverse selection from sellers dumping the side they think is losing).
- **97% of P&L comes from directional exposure** (unmatched shares on the winning side); only 3% from matched-pair spread.
- **First trade is on the losing side 70% of the time** — he starts wrong.
- By ~trade 10 (2–3 min in), he's on the winning side 73% of the time.
- Order sizes are equal on both sides; he gets more fills on his preferred side by **posting tighter bids and refreshing more aggressively** on the favoured side.

## Debunked Myths

### Myth 1: "Buy both sides for under $1.00 — risk-free arb"

Average pair cost is **$1.015**, not under $1.00. Matched pairs lose money. The 3% taker fee makes the true breakeven ~$0.97 combined, which essentially never occurs.

### Myth 2: "He follows Chainlink oracle price movements"

Fill direction vs. Chainlink delta: **49.5%** — a coin flip. A "shadow oracle" using Binance real-time prices performed **worse** than stale Chainlink. Oracle lag exists (~18s average, ~$15 gap), but his fills are uncorrelated with oracle direction.

### Myth 3: "Just copy his trades"

Detection delay kills it. By the time his lean is reliably detectable (~90–120s in), entering as a taker at the ask eats the edge. His lean at t=90s predicts settlement at 55%; after spread costs, P&L is effectively zero.

## Full Hypothesis Graveyard (All Tested Out-of-Sample)

| Hypothesis | In-Sample | Out-of-Sample | Verdict |
|---|---|---|---|
| Spread arbitrage (pair cost < $1.00) | n/a | Pair cost avg $1.015 | Wrong |
| Passive accumulation (cheap side fills more) | n/a | Passive lands on loser | Backwards |
| Oracle delta direction | 60–64% | 33–57% | Unstable |
| Expensive side / market consensus | 65%+ late | Collapses | Too late + overfit |
| Combined delta + expensive side ("Engine 13") | 65–72% | **25%** | Badly overfit |
| Fill velocity toxicity | ~50% | ~50% | Noise |
| Fill price vs best bid | No difference | No difference | Nothing |
| Fill price trajectory | Slight drift up on winner | Too weak/slow | Not actionable |
| Order book depth asymmetry | 44–57% | — | Noise |
| Spread asymmetry | Anti-predictive | Inverts over time | Not actionable |
| Bid-ask imbalance shift | 65–75% corr with his lean | Doesn't predict winner | Effect of his quoting, not cause |
| Burst patterns (direction, size, timing) | 53% | — | Noise |
| Mean reversion | 42% | — | Anti-predictive |
| Momentum / contrarian | 50/50 | — | Nothing |

## Author's Best Guess: How He Does It

1. **Order-flow reading at sub-second resolution** — sitting in the book on both sides, seeing who hits his bids, how fast, at what prices, and which orders sit untouched. Monitoring at 2s snapshots is too slow.
2. **Invisible quote management** — posting tighter bids on the favoured side, wider on the other. Only fills are visible externally, not resting orders.
3. **Infrastructure advantages** — co-located servers, possibly mempool monitoring of pending Chainlink oracle submissions. Professional-grade latency may make oracle lag exploitable at speeds retail can't match.

## Key Practical Barriers for Retail

- **Capital lockup**: positions locked until settlement; trading across multiple windows/assets requires $1K+ working capital.
- **Maker vs taker**: 3% taker fee kills most strategies; must build bot infrastructure to refresh limit orders every 10–20s.
- **Queue priority**: even at the same price, earlier orders get filled first; hard to test in paper trading.
- **Adverse selection**: maker fills disproportionately arrive when that side is losing.

## Notable Insights from Comments

### 500ms Taker Delay Theory

Multiple commenters noted gabagool22 went dark around Feb 18, 2026 when Polymarket removed a 500ms taker delay. Theory: he was posting limit orders and cancelling the stale side within the 500ms window before takers could pick them off — getting maker fills on the good side, avoiding being picked off on the bad side. Remove the delay and the edge disappears.

### Momentum Signal (Confirmed Real, Not Tradeable)

A commenter (self-described retired sports bettor) pointed out that 2 consecutive green 1m candles predict a green 5m close at ~67% (regime-dependent). The OP confirmed this holds statistically but is **not profitably tradeable** — by the time the signal is clear, entry prices have already moved.

### Recommended Approach from Experienced Commenter

- Use Binance 1m candle data bucketed per 5m; add moving averages + regime detection.
- Use first 2 minutes of price action to estimate 5m outcome probability.
- Consider sub-block structure (e.g., a 15m window has three 5m blocks, price behaves differently in each).
- Explore Pyth feed (faster than Chainlink) and differences between native API and RTDS.
- Take-profit at ~$0.95 is mathematically superior to holding to expiry.
- Use ML ensembles: e.g., RFC for entry gated by LogReg — only enter when both agree.
- Higher edge exists on ETH than BTC.
- LTF microstructure has been stable since 2018 regardless of bull/bear.

### GTC Ladder Strategy (Different Whale)

Another researcher described a whale strategy based on GTC limit-order ladders at every cent from floor to ask-1c, with continuous refills as the market oscillates. Produces a bell-curve fill distribution centred around 40–60c. Generates $400–$1,200 per winning round, but has a **directional imbalance problem**: the heavier side wins big, but the wrong-heavy side loses equally big. Win rate 40–55% — near breakeven.

### Merge for Capital Recycling

Merge (burning 1 UP + 1 DOWN for $1.00 back) is useful for freeing capital but creates no profit. Requires builder relayer client with separate credentials — not available via standard CLOB API.

## Relevance to Our Bot

| Finding | Implication for us |
|---|---|
| 79% winner-side lean via quote management | Our gabagool strategy tries to achieve this via dual-sided accumulation |
| Pair cost > $1.00 is expected | Validates that our notional guard and pair-cost tracking are measuring the right thing |
| Adverse selection is the core problem | Our orphan-leg exposure is the same fundamental issue |
| 500ms taker delay was critical to edge | Market structure change may have permanently shifted the landscape |
| Momentum signal real but not tradeable as taker | Reinforces our maker-only approach |
| Sub-block structure + regime detection | Worth exploring for our proximity filter and entry timing |
| ETH has higher edge than BTC | Consider weighting ETH more heavily |
| TP at ~$0.95 > hold to expiry | Worth backtesting early exit vs hold-to-settlement |

---

*Source: [reddit.com/r/PredictionsMarkets](https://www.reddit.com/r/PredictionsMarkets/comments/1r7oylh/4_months_reverseengineering_gabagool22_what_i/)*
