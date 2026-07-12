---
name: analyze-trades
description: >-
  Analyze Polymarket trading data fetched by the collect-trades skill. Ranks
  crypto and market-type profitability, deep-dives BTC dataset stats, and infers
  the trading strategy with pandas + matplotlib charts. Use when the user asks to
  analyze trade data, find the most profitable crypto/market, profile a wallet's
  strategy, or build a report from a collected trades CSV.
---

# Analyze Polymarket Trades

## Purpose

Turn the CSVs produced by the `collect-trades` skill into a profitability
ranking, a BTC-focused dataset profile, and a strategy inference report with
charts. Runs **locally** (data is already downloaded; no EC2 needed).

## Inputs

The three CSVs from `collect-trades` in `data/`:

| File | Used for |
|------|----------|
| `<label>_<start>_<end>.csv` | trade-level stats (size, price, timing) |
| `<label>_<start>_<end>_positions.csv` | per-(market, outcome) P&L |
| `<label>_<start>_<end>_closed_positions.csv` | optional P&L cross-check |

If the files are not in `data/`, run the `collect-trades` skill first.

## Requirements

`pandas` and `matplotlib` (present in the repo `.venv`). matplotlib needs a
writable cache — the script sets `MPLCONFIGDIR=/tmp/mplconfig` automatically.

## Workflow

Copy this checklist and track progress:

```
- [ ] Step 1: Run analyze_trades.py (rankings + BTC stats + charts + report)
- [ ] Step 2: Interpret rankings (most profitable crypto + market type)
- [ ] Step 3: BTC deep dive — read dataset stats + strategy metrics
- [ ] Step 4: Infer the strategy and how to reproduce it
- [ ] Step 5: Summarize findings for the user
```

### Step 1 — Run the analysis script

```bash
cd /Users/W518459/workspace/fleet-ai-sandbox
.venv/bin/python .cursor/skills/analyze-trades/scripts/analyze_trades.py \
  --trades data/<label>_<start>_<end>.csv \
  --positions data/<label>_<start>_<end>_positions.csv \
  --closed data/<label>_<start>_<end>_closed_positions.csv \
  --outdir analysis/<label>
```

The script prints all tables to stdout and writes a self-contained
`analysis/<label>/REPORT.md` that **embeds all charts** (with captions) plus the
PNG files beside it. It computes everything in the sections below.

The report contains 12 charts:

| Chart | Shows |
|-------|-------|
| `pnl_by_crypto.png` | P&L per crypto |
| `pnl_by_crypto_market_type.png` | P&L for every crypto × market-type |
| `btc_pnl_by_market_type.png` | BTC P&L by duration |
| `btc_price_hist.png` | fill price distribution (laddering / bell curve) |
| `btc_size_hist.png` | fill size distribution (clip sizing) |
| `btc_entry_timing.png` | seconds after open per entry |
| `btc_trades_by_hour.png` | activity by hour (UTC) |
| `btc_cumulative_pnl.png` | cumulative P&L across markets |
| `btc_pair_cost_hist.png` | Up+Down pair cost vs $1 fair value |
| `btc_edge_bar.png` | share win rate vs avg price (edge source) |
| `btc_winner_lean_by_trade.png` | winner-side lean by fill index (direction timing) |
| `btc_choppiness_edge.png` | per-share edge by market choppiness |

When reporting to the user, embed the charts inline (the report file already
does) so the final answer includes all graphs.

### Step 2 — List ALL crypto and market-type P&L (auto-computed)

The script prints two complete tables (every row, not just the top):

- **Crypto P&L**: `crypto_profitability` — P&L, ROI, win rate for every crypto
  (BTC/ETH/SOL/XRP/OTHER), sourced from `positions.pnl` (Polymarket
  `realizedPnl`).
- **Crypto × market-type P&L**: `market_type_profitability` — every
  `(crypto, market_type)` combination, where market_type ∈ {5min, 15min, 1hour,
  hourly, other}, sorted by P&L.

Always show the user the full tables so the profitable and unprofitable segments
are both visible.

### Step 3 — BTC deep dive (auto-computed)

Filtered to BTC, per market type: number of markets, trades, avg trades/market,
avg/median/min/max size, avg/min/max price, total volume. Plus
strategy-inference metrics:

| Metric | Reveals |
|--------|---------|
| `buy_pct`, `side_counts` | directional vs market-making |
| `both_sides_pct` | hedged both Up+Down (portfolio-neutral MM) |
| `entry_offset_median_s` | when in the window they enter (0 = at open) |
| `avg_fills_per_leg`, `avg_distinct_prices_per_leg`, `avg_price_span_per_leg` | price laddering |
| `avg_shares_per_leg`, `median_shares_per_leg` | position sizing |
| `sell_to_buy_ratio_pct` | hold-to-expiry (0%) vs active exit |
| `win_rate_pct`, `roi_pct` | edge |
| `avg_buy_price_winners` / `_losers` | cheap-side vs balanced buying |

### Step 4 — Profit theory (how they make money)

The `profit_decomposition` output gives an **exact** per-share breakdown
(printed for all cryptos and for BTC). Every winning share redeems at $1, so:

```
edge_per_share = share_win_rate − avg_price_per_share   (= total_pnl / total_shares)
```

Read these fields to build the theory:

| Field | Interpretation |
|-------|----------------|
| `avg_price_per_share` | average cost basis per share |
| `pair_cost_up_plus_down` | share-weighted cost of one Up+Down pair; **< $1.00 = hedge bought under fair value**, > $1.00 = hedge alone loses |
| `share_win_rate_pct` | % of bought shares that redeem at $1 |
| `edge_per_share` | the actual per-share profit driver |
| `hedged_share_pct` | how balanced the book is (≈100% = near-fully hedged) |
| `net_long_winner_pct` | does the heavier-bought side win? > 50% = directional tilt toward winners |

**Decide the profit source:**

- **Hedge spread** — `pair_cost_up_plus_down` < $1.00 and `hedged_share_pct` high:
  profit comes from buying both sides below fair value; settlement nets to the
  spread. Win rate ≈ 50%.
- **Directional tilt** — `pair_cost_up_plus_down` ≥ $1.00 but `share_win_rate` >
  `avg_price` and `net_long_winner_pct` > 50%: the hedge alone would lose; profit
  comes from leaning net-long the side that tends to win (e.g. dip-buying the
  cheaper side that mean-reverts into the winner).
- **Mixed** — both contribute.

**Accumulation & direction** (`accumulation_metrics`) tells you *how* any lean
forms — the gabagool22 signatures:

| Field | Interpretation |
|-------|----------------|
| `first_fill_on_winner_pct` | ~50% = no timing edge; ~30% = "starts wrong" (gabagool22) |
| `markets_balanced_pct` | high = symmetric passive ladder |
| `markets_winner_heavy_pct` vs `markets_loser_heavy_pct` | net directional skew |
| `avg_net_winner_shares` | magnitude of the lean (tiny = pure hedger) |

Plus `btc_winner_lean_by_trade.png`: **flat ~50%** = static symmetric ladder;
**rising toward the winner** = active quote-management (gabagool22-style).

**Choppiness vs edge** (`choppiness_table`, `btc_choppiness_edge.png`): shows
whether calm or choppy markets give better *per-share* edge (controls for size).
Choppy markets usually have more fills/bigger P&L but thinner per-share edge.

**Strategy archetypes:**

- **Static GTC cent-ladder** (e.g. @doggystyie): bell-curve fills centered ~$0.50,
  fixed clips, `markets_balanced_pct` high, flat winner-lean, pair cost ~$1.01.
  Passive two-sided liquidity; edge is thin volatility harvesting.
- **Active directional maker** (gabagool22): starts on loser, lean rises to
  ~75%+ by mid-window via tighter refresh on the favored side; edge is directional.

### Step 5 — How the strategy is run + reproduce

From the BTC strategy metrics (Step 3) describe the mechanics:

- **Markets**: crypto + duration (from the rankings)
- **Direction**: `buy_pct`, `both_sides_pct` (hedged vs directional)
- **Entry timing**: `entry_offset_median_s` (seconds after market open)
- **Laddering**: `avg_fills_per_leg`, `avg_distinct_prices_per_leg`,
  `avg_price_span_per_leg` (resting bid ladder shape)
- **Sizing**: `median_shares_per_leg`
- **Exit**: `sell_to_buy_ratio_pct` (0% = hold to expiry)

State how to reproduce it as a concrete recipe (markets, when to enter, ladder
shape, size per leg, exit rule) plus the edge source from Step 4.

### Step 5 — Summarize

Keep it short. Use the template below.

## Report template

```markdown
# <label> — trade analysis (<start> to <end>)

## Crypto P&L (all)
<full table>

## Crypto × market-type P&L (all)
<full table>

## BTC dataset
- <n> markets, <n> trades, <avg> trades/market
- size: median <x>, range <min>–<max>
- price: avg <x>, range <min>–<max>

## Profit theory (how they make money)
- avg price/share $<x>; pair cost $<x> (<below/above> $1)
- share win rate <x>% vs avg price <x>% → edge $<x>/share
- source: <hedge spread / directional tilt / mixed>
- net-long side wins <x>% → <directional tilt or not>

## How the strategy is run + reproduce
- Markets: <crypto> <duration>
- Direction: <hedged both sides / directional>
- Entry: ~<t>s after open
- Ladder: ~<n> bids spanning <span> in price
- Size: ~<n> shares/leg
- Exit: <hold to expiry / sell>

## Charts
[reference the PNGs in analysis/<label>/]
```

## Notes

- P&L always comes from `positions.pnl` (Polymarket `realizedPnl`), never
  reconstructed. Do not re-derive P&L from settlement.
- `market_type` is parsed from `event_slug`; slugs like
  `bitcoin-up-or-down-<date>-<hour>am-et` are classified as `hourly`.
- Charts land in `analysis/<label>/`; embed them when reporting to the user.
- For multi-user comparison, run the script per user and compare the printed
  ranking tables.
