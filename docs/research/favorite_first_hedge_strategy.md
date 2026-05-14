# Favorite-First Hedge Strategy

## Summary

The collector replay suggests that pure gabagool-style pair accumulation is not the best fit for the observed BTC/ETH 5-minute markets. Immediate taker pair entry almost never starts, and passive two-sided maker entry is fragile because one orphan leg can erase many small locked-pair edges.

The most promising replayed variant is a **favorite-first hedge**:

1. Quote only the strong favorite at the bid.
2. If the favorite fills, try to add the underdog only when the completed pair is profitable.
3. If the underdog never fills at a profitable price, hold the favorite to resolution.
4. Limit exposure to one favorite fill per market.

This is not pure market-neutral arbitrage. It is a directional favorite strategy with an opportunistic gabagool hedge.

---

## Data Set

Replay used local collector snapshots from `data/collectors`:

- `btc_eth_5m_20260404T205415Z_N1.jsonl.gz`
- `btc_eth_5m_20260404T205911Z_N20.jsonl.gz`
- `btc_eth_5m_N100.jsonl.gz`

The collector records top-of-book snapshots for BTC/ETH 5-minute markets:

- `best_bid`
- `best_ask`
- outcome token (`Up` / `Down`)
- market slug and sample sequence

Important limitation: the data does **not** include real trade prints, order queue position, resting size, or our actual fill priority. Fill assumptions are approximations from top-of-book movement.

---

## Baseline Finding

### Taker Pair Entry Does Not Start

Using the strategy's effective ask rule (`ask` if positive, otherwise fallback to `bid`), the collected data showed:

| Condition | Result |
|-----------|--------|
| `YES ask + NO ask < 0.98` | 0 samples |
| `YES ask + NO ask < 1.00` | 0 samples |
| median effective ask sum | ~1.01 |

So an ask-taking pair strategy with `max_pair_cost=0.98` or `0.99` will mostly sit idle.

### Passive Two-Sided Maker Entry Has Orphan Risk

The bid side looks much more attractive:

| Condition | Result |
|-----------|--------|
| `YES bid + NO bid <= 0.98` | common |
| `YES bid + NO bid <= 0.99` | nearly always |

But replaying a practical maker strategy still lost money when orphan fills were allowed. The matched-pair edge is often only `$0.01` to `$0.04`, while a single unpaired leg around `$0.45` to `$0.50` can wipe out many good pairs.

Best practical two-sided replay after safety filters:

```text
target=0.98
orphan budget=$0.50
only open when TTE >= 120s
only open if max(individual bid) <= 0.50

traded markets: 19
fills: 76
matched pairs: 36
open orphan lots: 4
locked profit: +$1.44
final P&L: -$0.51 before fees
final P&L: -$0.78 at 100 bps
```

Conclusion: "quote both sides and rebalance later" is not enough. The strategy must either avoid orphans almost entirely, or make orphan inventory directionally favorable.

---

## Proposed Algorithm

The improved approach uses the market's directional signal first and the hedge second.

```text
For each active 5-minute market:
  favorite = side with higher best_bid
  underdog = opposite side

  Only open when:
    favorite_bid >= 0.70
    favorite_bid - underdog_bid >= 0.60
    no existing position for this market

  Place one resting BUY on the favorite at favorite_bid.

  If favorite fills:
    Try to buy the underdog at bid only if:
      favorite_fill_price + underdog_bid <= 0.97

  If underdog fills:
    Position becomes a locked profitable pair.

  If underdog does not fill:
    Hold the favorite to resolution.

  Do not add more than one favorite fill per market.
```

The key change is that the first leg is intentionally the likely winner. An orphan is no longer a random naked leg; it is a small directional favorite position. The hedge is opportunistic and only taken when it improves the position into a profitable pair.

---

## Replay Results

Best replayed configuration:

```text
favorite_bid >= 0.70
favorite_bid - underdog_bid >= 0.60
hedge only if favorite_fill_price + underdog_bid <= 0.97
max 1 favorite fill per market
```

Result:

```text
markets traded: 55
hedged pairs: 53
cost: $52.53
P&L before fees: +$2.47
W/L: 55/0
```

Fee sensitivity:

```text
0 bps:   +$2.47
25 bps:  +$2.64
50 bps:  +$2.51
100 bps: +$2.25
200 bps: +$2.29
```

The fee sensitivity remains positive in this replay because higher fees cause some marginal hedges to be skipped.

Other nearby configurations also showed positive results:

| Favorite threshold | Lead threshold | Hedge target | Markets | Hedges | P&L |
|--------------------|----------------|--------------|---------|--------|-----|
| `0.65` | `0.60` | `0.97` | 55 | 53 | `+$2.47` |
| `0.70` | `0.60` | `0.97` | 55 | 53 | `+$2.47` |
| `0.75` | `0.60` | `0.97` | 55 | 53 | `+$2.47` |
| `0.80` | `0.60` | `0.97` | 55 | 52 | `+$1.66` |

---

## Why This Looks Better

Pure pair-arb depends on both legs filling. If only one leg fills, the bot is left with a directional risk that may be unfavorable.

Favorite-first changes the risk profile:

- A first-leg orphan is intentionally the market favorite.
- The strategy avoids paying the ask to hedge.
- The underdog is added only when the pair is profitable.
- The maximum loss surface is limited by one fill per market.
- Most value comes from correctly identifying strong near-resolution favorites, not from tiny pair spread alone.

In short:

```text
old model: pair edge first, orphan risk second
new model: favorite edge first, hedge edge second
```

---

## Execution Assumptions

The replay used top-of-book movement to approximate maker fills. A bid was treated as filled when the next snapshot crossed or moved down through that bid:

```text
next_ask <= our_bid
or
next_bid < our_bid
```

This is not guaranteed in live execution. Real performance depends on:

- queue position at the best bid,
- resting size ahead of us,
- whether trades actually occurred,
- latency between snapshots,
- order cancellation and replacement timing,
- Polymarket API rate limits,
- fees and wallet/order-management behavior.

The replay should be treated as a research signal, not proof of live profitability.

---

## Risks

### Directional Risk

This strategy is no longer market-neutral at entry. If a strong favorite reverses before resolution and the hedge never fills, the position can lose.

Mitigations:

- cap to one fill per market,
- require a large favorite lead,
- avoid opening too early when the favorite can still reverse,
- consider closing a favorite if the favorite lead collapses.

### Replay Bias

The same data was used to discover and score the thresholds. The `0.70 / 0.60 / 0.97` configuration may be overfit.

Mitigations:

- validate on additional collector windows,
- test BTC and ETH separately,
- run a dry-run strategy that logs hypothetical fills before live trading.

### Fill Assumption Risk

The model assumes a likely fill when the next best bid drops through our bid. In reality, our order may not have been at the front of the queue.

Mitigations:

- collect L2 depth or trade prints if available,
- log submitted order timestamps and real fills in dry-run/live,
- start with very small size.

---

## Suggested Next Test

Implement a dry-run-only strategy that emits the favorite-first hedge intents and records:

- favorite quote submitted,
- favorite quote filled or missed,
- underdog hedge quote submitted,
- underdog hedge filled or missed,
- final market winner,
- P&L if held,
- P&L if hedged,
- fill latency by side,
- quote lead at entry,
- time to expiry at entry.

Initial candidate parameters:

```text
favorite_min_bid = 0.70
favorite_min_lead = 0.60
hedge_max_pair_cost = 0.97
max_favorite_fills_per_market = 1
markets = BTC, ETH
duration = 5m
```

The live/dry-run goal should be to validate the fill model first. If real maker fills are much worse than replayed fills, the apparent edge may disappear.
