# Gabagool Notional Guard Incident (Postmortem)

## Summary

During live `gabagool` trading on profile 1, multiple BUY orders were rejected by Polymarket with:

- `invalid amount for a marketable BUY order (...), min size: $1`

These rejections increased orphan exposure because one leg could fail while the other leg later filled.

## What Happened

Polymarket enforces a minimum notional for marketable BUY orders:

- `price * size >= $1.00`

Observed rejects in live trading included:

- `$0.32 x 2.50 = $0.80` (rejected)
- `$0.37 x 2.50 = $0.925` (rejected)
- `$0.50 x 1.25 = $0.625` (rejected)

The strategy already had a min-notional resize helper, but live data showed orders still reached the exchange at unadjusted size in some paths/configurations.

## Impact

- 12 rejections in the 24h sample
- 11/12 due to min notional
- 1/12 due to invalid price (`1.0` > max allowed `0.999`)
- Rejections were strongly correlated with orphan outcomes (single-leg exposure)

## Concrete Timeline Examples

## Example A — `btc-updown-15m-1774996200`

1. **22:31:29 UTC**
  BUY `@ $0.3200 x 2.50` (`$0.8000` notional) -> **REJECTED**  
   Error: min notional `$1`.
2. **22:41:02 UTC**
  BUY `@ $0.5000 x 2.50` (`$1.2500` notional) -> **FILLED**
3. Outcome: one token filled, opposite token rejected -> **orphan leg**.

Expected resize for step 1:

- Required size = `1.00 / 0.3200 = 3.1250`
- Corrected order would be `3.1250` shares (`$1.0000` notional)

## Example B — `btc-updown-15m-1774998900`

1. **23:15:34 UTC**
  BUY `@ $0.3700 x 2.50` (`$0.9250` notional) -> **REJECTED**
2. **23:16:52 UTC**
  BUY `@ $0.5000 x 2.50` (`$1.2500` notional) -> **FILLED**
3. Outcome: failed cheap leg + filled expensive leg -> **orphan leg**.

Expected resize for step 1:

- Required size = `1.00 / 0.3700 = 2.702703`
- Corrected order would be `2.702703` shares (`$1.0000` notional)

## Example C — `btc-updown-15m-1774985400`

1. **19:30:07 UTC**
  BUY `@ $0.5000 x 1.25` (`$0.6250`) -> **REJECTED**
2. **19:31:01 UTC**
  BUY `@ $0.4800 x 1.25` (`$0.6000`) -> **REJECTED**
3. Outcome: both legs rejected, no exposure, but lost profitable opportunity.

## Root Cause

Primary issue: strategy-level min-notional protection was not sufficient as a sole guardrail in live execution.

Even though `gabagool` had min-notional resize logic, rejected orders demonstrated that under real runtime conditions/config drift, under-$1 BUY orders could still be submitted.

## Fix Implemented

A **defensive second-layer guard** was added in `OrderManager.submit()`:

- For `gabagool` BUY intents only:
  - If `price * size < 1.0`, auto-resize to `ceil((1.0 / price) * 1e6) / 1e6`
- This normalization runs **before risk check and REST submission**
- It uses immutable intent replacement to ensure submitted and persisted intent size are aligned

This ensures sub-$1 marketable BUYs do not reach the exchange, even if strategy config/path drifts.

## Regression Tests Added

File: `tests/test_order_manager_notional.py`

- `test_submit_resizes_gabagool_buy_under_min_notional`
- `test_submit_does_not_resize_non_gabagool_strategy`
- `test_submit_does_not_resize_gabagool_sell`

These tests fail if the normalization behavior regresses.

## Follow-Ups

- Add a validation check on startup for `P1_GABAGOOL_MIN_ORDER_NOTIONAL_USD` and log effective value.
- Add a counter metric for "intent resized for min notional" by strategy.
- Add alerting when rejection reason contains `min size: $1`.

