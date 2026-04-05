# Polymarket Arbitrage Strategies

> **Source:** [People quietly making a fortune through arbitrage on Polymarket](https://www.bitget.com/news/detail/12560605016888)
> **Published:** 2025-10-15 — Odaily BlockBeats via Bitget News / Mars Finance
> **Original Source:** https://news.marsbit.co/20251014144407454603.html

## Strategy: Endgame Sweep (Sweeping the Closing Market)

### Concept
When an event's outcome is essentially decided and the market price rises to **0.99**, buy in at 0.999 and wait for official settlement, pocketing the remaining spread as near-certain profit. If the tick size changes but the bid hasn't reached 0.99 yet, the bot monitors the market until it does.

### Core Logic
**"Trading time for certainty."** After an event has occurred (e.g., an election result is clear, a sports event has ended) but the market hasn't officially settled, the price hovers close to — but not exactly at — 1.00. Retail investors often can't wait for settlement and sell at 0.997–0.999, leaving arbitrage space.

### Key Data Points
- ~90% of large orders (>$10,000) on Polymarket are executed at prices above 0.95
- Each trade earns ~0.1% profit, but with enough capital and frequency, gains compound significantly

### Risk: Black Swan Events & Whale Manipulation
- **Black swan risk**: Something that seems certain suddenly reverses (e.g., a match result is invalidated, a political scandal overturns an outcome)
- **Whale manipulation loop**:
  1. When price nears 0.99, whales dump large orders pushing price down to ~0.9
  2. They spread FUD in comments and social media to amplify retail panic
  3. After retail panic-sells, whales buy chips at low prices
  4. Once the event settles, whales pocket the 0.9→1.00 spread plus retail's lost profits

### Risk Management
- **Never allocate more than 1/10 of total position to any single market**
- Prioritize markets that are about to settle (within a few hours) and have prices above 0.997
- Shorter time windows mean smaller black swan exposure
-
### wallet example

Strategy bet only with 0.99 probability
https://polymarket.com/@IsraeliCowboy?tab=activity
https://polymarketanalytics.com/traders/0xb77b6ef359f02192fb077227499f9f853d4870a5#trades
