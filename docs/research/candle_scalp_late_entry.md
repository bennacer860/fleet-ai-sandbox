# Candle Scalp / Late-Entry Strategy

## Summary

A directional scalping strategy for 15-minute crypto up/down markets. The core idea: wait until the last ~2 minutes of a candle, confirm strong directional movement (large body, high volume, no doji), then buy the corresponding YES token expecting resolution at $1.

Source: community video describing manual execution of this approach on Polymarket. The question is whether it can be automated and whether the edge survives after accounting for market-price convergence in the final minutes.

---

## Strategy Rules

### Entry Timing

- Only enter in the **last 2 minutes** of the 15-minute candle (i.e. `time_remaining <= 120s`).
- Avoid the final ~10 seconds — spread widens and fill probability drops.
- The late entry maximizes confirmation: by minute 13, the candle shape is mostly settled.

### Candle Quality Filter

| Filter | Rule | Rationale |
|--------|------|-----------|
| Doji rejection | `body_ratio < 0.3` → skip | Small body relative to range = indecision, risk of sudden flip |
| Minimum body size | `abs(close - open) < X bps` → skip | Tiny moves don't produce meaningful YES/NO price separation |
| Volume confirmation | Below rolling average → skip | Low-volume candles are more prone to reversal |

Where `body_ratio = abs(close - open) / (high - low)`.

### Direction & Token Selection

- Green candle (close > open) → buy **UP** YES token
- Red candle (close < open) → buy **DOWN** YES token

### Target Return

- Scalp 10–30% per trade by buying YES tokens in the 70–90¢ range.
- Binary resolution at $1 if correct; the edge is the gap between entry price and $1.

---

## Architecture Fit

The existing bot already trades these exact markets. Key building blocks:

| Component | Status | Notes |
|-----------|--------|-------|
| 15-min market slug generation | ✅ exists | `src/markets/fifteen_min.py` — `get_market_slug()`, `extract_market_end_ts()` |
| Real-time crypto spot price | ✅ exists | `CryptoWebSocket` streams Binance miniTicker for BTC/ETH/SOL/XRP/DOGE/HYPE/BNB |
| Strategy base class | ✅ exists | `StrategyContext` provides `crypto_prices`, `crypto_price_ts`, `best_prices` |
| Strategy registry | ✅ exists | `register_strategy()` in `src/strategy/registry.py` |
| Order placement | ✅ exists | Pure `OrderIntent` return, handled by `OrderManager` |
| Paper trading | ✅ exists | `--dry-run --fill-mode book` with `FillSimulator` |
| **OHLC candle aggregation** | ❌ missing | Need to aggregate `CryptoWebSocket` ticks into 15-min candles |
| **Historical backtest harness** | ❌ missing | No walk-forward loop over candles; only scenario-based tests exist |

### What Needs Building

1. **Candle aggregator** — Maintain rolling OHLC from `crypto_prices` / `crypto_price_ts` snapshots in `StrategyContext`. Track open (first price of interval), high, low, close (latest), and tick count as a volume proxy.

2. **Strategy class** (`CandleScalpStrategy`) — Implement `poll()` to check timing window, apply candle filters, and emit `OrderIntent` for the directional YES token.

3. **Backtest harness** — Pull historical 15-min klines from Binance REST API, pair with historical Polymarket YES/NO prices (if available from collector data), and simulate P&L across hundreds of candles.

---

## Pseudo-Implementation

```python
class CandleScalpStrategy(Strategy):
    async def poll(self, ctx: StrategyContext) -> list[OrderIntent] | None:
        slug = get_market_slug("BTC", duration_minutes=15)
        end_ts = extract_market_end_ts(slug)
        remaining = end_ts - time.time()

        if remaining > 120 or remaining < 10:
            return None

        candle = self._build_candle(ctx.crypto_prices, ctx.crypto_price_ts)

        if candle.is_doji(threshold=0.3):
            return None
        if candle.body_size < self.min_body_size:
            return None

        side = "up" if candle.close > candle.open else "down"
        token_id = self._resolve_token(slug, side)

        return [OrderIntent(
            token_id=token_id,
            price=ctx.best_prices[token_id].ask,
            size=self.position_size,
            side=Side.BUY,
            strategy="candle_scalp",
            slug=slug,
        )]
```

---

## Key Risks & Open Questions

### Price Convergence

The biggest risk: by the 2-minute mark, if the candle direction is obvious, the Polymarket YES token may already be priced at 95–98¢. The remaining 2–5% upside may not justify the risk of a reversal. Need to quantify the **spread between spot conviction and market price** in the final minutes — this is essentially what proximity already measures for other strategies.

### Reversal Risk

Even with doji filtering, a large candle can reverse in the final 2 minutes. Crypto volatility spikes (liquidation cascades, news) can flip a green candle red in seconds. The strategy has no stop-loss mechanism — binary markets resolve at $0 or $1.

### Liquidity & Slippage

Late-candle liquidity on Polymarket 15-min markets may be thin. If the book only has $50 at the best ask, sizing is constrained. Need to check typical book depth in the final 2 minutes.

### Multi-Asset Expansion

The video focuses on BTC, but the bot already supports ETH, SOL, XRP, DOGE, HYPE, BNB. Running across multiple assets increases opportunity but also increases exposure to correlated moves.

### Interaction with Existing Strategies

If sweep or gabagool is already active on the same market, candle_scalp orders could conflict. Would need coordination via `OrderManager` or mutual exclusion rules.

---

## Testing Plan

1. **Historical backtest** — Pull Binance 15-min klines (months of data), apply the candle filters, check what the hypothetical YES token price would have been at entry (requires Polymarket historical data or assumptions about price-vs-spot relationship). Estimate win rate and average return.

2. **Dry-run live** — `--dry-run --fill-mode book --strategy candle_scalp`. Watch the dashboard, log every decision (enter / skip-doji / skip-small / skip-timing). Run for a week across BTC + ETH.

3. **Small-size live** — $1–2 per trade, real orders. Track actual fill rate, slippage, and P&L in the SQLite DB.

---

## Status

**Stage: Idea / Research** — Not yet implemented. Documenting for future exploration.
