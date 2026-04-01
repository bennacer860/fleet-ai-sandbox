---
name: gabagool-perf
description: >-
  Query gabagool pair-arbitrage performance from the bot's SQLite database on
  EC2 via AWS SSM. Use when the user asks about gabagool performance, pair
  profitability, orphan pairs, pair cost, leg prices, gabagool P&L, or
  gabagool ROI.
---

# Gabagool Performance

## Purpose

Run the `pair_perf.py` script on EC2 to report gabagool pair-arbitrage
performance: win rate, pair costs, per-leg prices, orphan count, and P&L.
Filters to **real trades only** (dry_run=0).

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--profile` | `1` | Bot profile number — determines DB file (`data/bot_p{N}.db`) |
| `--hours` | `24` | Lookback window in hours |

Infer values from the user's request. Examples:
- "gabagool perf on profile 2" → `--profile 2`
- "last 6 hours" → `--hours 6`
- "since yesterday" → `--hours 24`
- "all time" → `--hours 8760` (1 year, effectively unbounded)

## Environment

- AWS profile: `rafik`
- Instance ID: `i-04fb74e5b95fdc098`
- Region: `eu-west-1`
- App path: `/opt/polymarket-bot`
- Python: `.venv/bin/python`

## Technique

Use the **base64 script** pattern (no `sqlite3` CLI on the instance):

```bash
# Step 1 — encode the script locally
base64 < .cursor/skills/gabagool-perf/scripts/pair_perf.py > /tmp/gab_b64.txt

# Step 2 — send via SSM
B64=$(cat /tmp/gab_b64.txt | tr -d '\n')
AWS_PROFILE=rafik aws ssm send-command \
  --instance-ids "i-04fb74e5b95fdc098" \
  --document-name "AWS-RunShellScript" \
  --parameters "{\"commands\":[\"sudo su - ec2-user -c 'cd /opt/polymarket-bot && echo $B64 | base64 -d > /tmp/q.py && .venv/bin/python /tmp/q.py --profile PROFILE --hours HOURS'\"]}" \
  --region eu-west-1 \
  --output json --query 'Command.CommandId'

# Step 3 — fetch result (wait ~10s)
AWS_PROFILE=rafik aws ssm get-command-invocation \
  --command-id "<COMMAND_ID>" \
  --instance-id "i-04fb74e5b95fdc098" \
  --region eu-west-1 \
  --query '{Status: Status, Output: StandardOutputContent, Error: StandardErrorContent}' \
  --output json
```

Replace `PROFILE` and `HOURS` with the resolved values.
All SSM commands require `required_permissions: ["full_network"]`.

## Output Format

Present results to the user as:

1. **Summary** — total markets, complete vs orphan pairs.
2. **Pair economics** — win rate, avg/median pair cost, profit per share,
   avg cheap-leg and dear-leg prices, locked profit, ROI.
3. **Pair cost distribution** — histogram of pair costs.
4. **Orphan analysis** — count, capital at risk, per-slug breakdown.
5. **Order funnel** — orders placed vs filled vs rejected.
6. **Overall P&L** — total capital, locked profit, profit/hour.

## Key Concepts

- **Pair cost** = avg_price(leg_A) + avg_price(leg_B). Profitable when < $1.00
  because one side always pays out $1.
- **Locked profit** = min(qty_A, qty_B) × (1.0 − pair_cost).
- **Orphan** = market where only one leg was filled before resolution or the
  bot stopped. Capital is at risk (roughly 50/50 odds at avg price ~$0.46).

## Guardrails

- Read-only queries only — never modify the database.
- Always use `required_permissions: ["full_network"]` for SSM calls.
- Wait ~10 seconds before fetching SSM command output.
