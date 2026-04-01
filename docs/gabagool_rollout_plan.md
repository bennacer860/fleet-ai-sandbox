# Gabagool Live Rollout Plan

## Current State

- **Dry-run results**: 203 paired markets, **$11.98 P&L**, 98.5% win rate over ~2.5 days
- **Edge**: Combined spread averages 0.97-0.98, yielding **2-3 cents per $1** per market
- **Size**: Probe phase only ($2.50/side = `base_order_size` 10 x `probe_size_factor` 0.25)
- **Scope**: BTC 15m markets only, profile 1
- **Key gap**: `fee_bps=0` in config -- fees are not modeled

---

## Critical Pre-flight: Fee Viability

Polymarket charges **0 bps maker / ~100-200 bps taker**. With a 2-3 cent edge per $1 and two taker fills per market:

- At **100 bps**: fees eat ~1 cent of a 2-3 cent edge (still positive, ~$0.025/market)
- At **200 bps**: fees eat ~2 cents -- edge nearly wiped out

**Action before going live**: Set `fee_bps` to match actual taker fee rate and re-run the dry-run simulation to confirm the edge survives. If marginal, the strategy must post **maker** limit orders instead of lifting asks, which changes fill dynamics.

In [gabagool_adapter.py](src/strategy/gabagool_adapter.py) line 32, change `fee_bps: int = 0` to the actual fee rate (e.g. `100`), then re-observe the dry-run for 24h to validate that `pick_side` and `should_buy` still allow enough trades.

---

## Phase 1: Minimum-Size Live (1-2 days)

**Goal**: Confirm real fills match simulated fills in rate and price.

**Changes**:

- Remove `--dry-run` from the gabagool service file ([deploy/polymarket-bot-p1-gabagool.service](deploy/polymarket-bot-p1-gabagool.service))
- Remove `--fill-mode book` (live mode uses real UserWS fills)
- Keep everything else identical: BTC only, 15m only, profile 1
- Set conservative risk limits in `.env`:

```
P1_MAX_POSITION_PER_MARKET=25
P1_MAX_TOTAL_EXPOSURE=150
P1_MAX_ORDERS_PER_MINUTE=10
P1_MAX_DAILY_LOSS=10
P1_DEFAULT_TRADE_SIZE=5
```

- Set `fee_bps` to actual Polymarket taker fee in the code
- Keep `base_order_size=10` and `probe_size_factor=0.25` (probe stays at $2.50/side)

**Service command becomes**:

```
main.py run --markets BTC --durations 15 --profile 1 --strategy gabagool --dashboard
```

**Monitor**: Compare live fill rate, avg spread, and P&L vs the dry-run baseline. Key metrics:

- Paired fill rate (should be near 100% for completed pairs)
- Avg combined spread (should be ~0.97-0.98)
- Actual vs simulated P&L per market

---

## Phase 2: Scale Size (3-5 days)

**Goal**: Increase size while verifying the edge holds and market impact is minimal.

**Changes** (after Phase 1 confirms the edge):

- Bump `base_order_size` to **25** (probe = $6.25/side)
- If probe phase consistently locks profit, allow **build phase** to kick in (full $25/side)
- Raise risk limits:

```
P1_MAX_POSITION_PER_MARKET=100
P1_MAX_TOTAL_EXPOSURE=200
```

**Watch for**: Spread compression as order sizes increase (your orders moving the book), fill latency degradation, or increased partial fills.

---

## Phase 3: Multi-Asset Expansion

**Goal**: Extend to ETH and SOL 15m markets, then all 7 assets.

**Changes**:

- `--markets BTC ETH SOL` (start with the most liquid)
- Raise `MAX_TOTAL_EXPOSURE` proportionally
- Monitor per-asset spread distribution -- smaller-cap assets (HYPE, DOGE) may have wider spreads (more edge) but worse fill rates

---

## Phase 4: Multi-Duration (Optional)

**Goal**: Test whether the edge exists at other durations (5m, 60m).

- 5m markets turn over faster (more trades/day) but may have tighter spreads
- 60m markets turn over slower but might have wider spreads
- Run dry-run first with `--durations 5 15 60` to validate before going live

---

## Code Changes Required

1. **[src/strategy/gabagool_adapter.py](src/strategy/gabagool_adapter.py)**: Make `GabagoolConfig` fields env-driven (read from `P1_GABAGOOL`_* env vars) so you can tune without redeploying code:
  - `base_order_size`, `max_pair_cost`, `max_imbalance`, `probe_size_factor`, `fee_bps`
2. **[src/execution/risk_manager.py](src/execution/risk_manager.py)**: Wire `record_fill()` into the live trading loop -- currently `MAX_DAILY_LOSS` circuit breaker is **never triggered** because `record_fill` is never called. This is a safety gap that must be fixed before live.
3. **[deploy/polymarket-bot-p1-gabagool.service](deploy/polymarket-bot-p1-gabagool.service)**: Update the ExecStart command to remove `--dry-run` and `--fill-mode book`.

---

## Risk Checklist

- `fee_bps` set to actual rate and validated in dry-run
- `MAX_DAILY_LOSS` circuit breaker wired (currently dead code)
- Profile 1 `.env` overrides set (`P1_MAX_POSITION_PER_MARKET`, etc.)
- Telegram alerts enabled for profile 1 (`P1_TELEGRAM_NOTIFICATIONS_ENABLED=true`)
- Verify USDC balance on the wallet is sufficient but not excessive
- Confirm gabagool and post_expiry (profile 2) don't clash on same markets (they won't -- gabagool is BTC 15m, post_expiry handles all durations but doesn't trade 15m BTC in the same way)
