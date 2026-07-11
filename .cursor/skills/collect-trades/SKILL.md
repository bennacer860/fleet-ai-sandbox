---
name: collect-trades
description: >-
  Collect Polymarket wallet trade history + P&L for a user over a time window.
  Runs on EC2 via AWS SSM. Use when the user asks to collect trades, fetch
  trade data, download wallet history, pull trade CSVs, or get P&L for a
  Polymarket user like @certova, @ivy56, or a 0x wallet address.
---

# Collect Polymarket Trades

## Purpose

Fetch all trades for a Polymarket user over a date range, compute per-position
P&L using the Data API `closed-positions` endpoint (Polymarket's own realized
PnL figure), and upload results to S3 + download locally.

The script must run **on EC2** because the local network blocks Polymarket APIs.

## Parameters

Infer these from the user's request:

| Parameter | Required | Example | Description |
|-----------|----------|---------|-------------|
| `user`    | yes      | `@certova`, `@ivy56`, `0x8d1d…` | Polymarket handle or wallet address |
| `start`   | yes*     | `2026-06-11` | Start date inclusive (YYYY-MM-DD, EST) |
| `end`     | yes*     | `2026-07-11` | End date inclusive (YYYY-MM-DD, EST) |
| `days`    | alt*     | `30` | Lookback days ending today (alternative to start/end) |
| `with_pnl`| default  | `true` | Fetch closed-positions P&L (always do this unless told not to) |

*Either `--start`/`--end` or `--days` is required, not both.

Common mappings:
- "past month" → `--days 30`
- "last week" → `--days 7`
- "since June 1" → `--start 2026-06-01 --end <today>`
- "for May" → `--start 2026-05-01 --end 2026-05-31`

## Known Wallets

Handle resolution via profile-page scraping is unreliable on EC2.
The script has a `KNOWN_WALLETS` map in `fetch_wallet_trades.py` that
short-circuits scraping. Currently known:

| Handle | Wallet |
|--------|--------|
| `@certova` | `0x8d1d5d1c6041b13fc708b5d9f668070e1724ed4a` |
| `@ivy56` | `0xddb062ade7d4e92ef636a3bfb94a4e2feab30310` |

For **new users not in the map**, you must first resolve their wallet address
(try Gamma API on EC2: `GET /public-profile?username=<handle>`) and then either:
1. Add them to `KNOWN_WALLETS` and sync the code, or
2. Pass the raw `0x…` address directly via `--wallet`.

## Environment

- AWS profile: `rafik`
- Instance ID: `i-0431a0aa517edf582`
- Region: `eu-west-1`
- App path: `/opt/polymarket-bot`
- S3 bucket: `polymarket-bot-backups-2c05a8a8`
- Local data dir: `data/`

## Execution Steps

### 0. Check existing data (MANDATORY first step)

Before fetching anything, check what data already exists for this user locally
and in S3. This avoids re-fetching data that has already been collected.

**Local check:**

```bash
ls -lh data/<LABEL>_*.csv 2>/dev/null
```

**S3 check — list all collected files for this user across all date folders:**

```bash
AWS_PROFILE=rafik aws s3 ls s3://polymarket-bot-backups-2c05a8a8/research/wallet-trades/ \
  --region eu-west-1 --recursive | grep '<LABEL>_'
```

Replace `<LABEL>` with the user label (e.g. `certova`, `ivy56`, `0x8d1d5d`).

**Interpret the results:**

- File names follow `<label>_<start>_<end>.csv` — extract the date ranges.
- Compare existing ranges against the requested range.
- If the requested range is **fully covered** by existing files, tell the user
  what data already exists and **ask whether to re-fetch or skip** using the
  `AskQuestion` tool. Present options like:
  - "Use existing data (skip fetch)"
  - "Re-fetch to get latest trades"
- If there is **partial overlap**, tell the user what is already covered and
  what gap remains. Offer to fetch only the missing portion or the full range.
- If there is **no overlap**, proceed directly to Step 1.

**Example output to the user:**

> Found existing data for @certova:
> - `certova_2026-06-11_2026-07-11.csv` (32 MB, 72k trades) — local + S3
> - `certova_2026-04-26_2026-05-30.csv` (62 MB) — S3 only
>
> Your requested range (Jun 11 – Jul 11) is already fully covered locally.

Then use `AskQuestion` to let them decide.

### 1. Sync latest code to EC2

Package the fetcher and upload to S3, then pull on EC2:

```bash
cd /Users/W518459/workspace/fleet-ai-sandbox
tar czf /tmp/fetch_sync.tgz \
  fetch_wallet_trades.py \
  src/trade_fetcher.py \
  src/logging_config.py \
  src/utils/parsing.py \
  src/utils/__init__.py \
  src/markets/fifteen_min.py \
  src/markets/__init__.py \
  src/__init__.py

AWS_PROFILE=rafik aws s3 cp /tmp/fetch_sync.tgz \
  s3://polymarket-bot-backups-2c05a8a8/research/wallet-trades/_deploy/fetch_sync.tgz \
  --region eu-west-1
```

### 2. Write a runner script

Create a local script at `/tmp/collect_trades.sh`:

```bash
#!/bin/bash
set -euo pipefail
cd /opt/polymarket-bot

aws s3 cp s3://polymarket-bot-backups-2c05a8a8/research/wallet-trades/_deploy/fetch_sync.tgz \
  /tmp/fetch_sync.tgz --region eu-west-1
tar xzf /tmp/fetch_sync.tgz -C /opt/polymarket-bot
mkdir -p data

export S3_BUCKET=polymarket-bot-backups-2c05a8a8
export AWS_DEFAULT_REGION=eu-west-1

.venv/bin/python3 fetch_wallet_trades.py \
  --wallet <USER> \
  --start <START> --end <END> \
  --output data/ \
  --with-pnl \
  --s3-bucket polymarket-bot-backups-2c05a8a8 \
  --s3-region eu-west-1

echo "=== FILES ==="
ls -lh data/<LABEL>_<START>_<END>*.csv
echo "Done $(date -u)"
```

Replace `<USER>`, `<START>`, `<END>`, `<LABEL>` with actual values.
`<LABEL>` is the handle without `@` (e.g. `certova`) or first 8 chars of wallet.

Upload the script:

```bash
AWS_PROFILE=rafik aws s3 cp /tmp/collect_trades.sh \
  s3://polymarket-bot-backups-2c05a8a8/research/wallet-trades/_deploy/collect_trades.sh \
  --region eu-west-1
```

### 3. Run on EC2 via SSM

```bash
CMD_ID=$(AWS_PROFILE=rafik aws ssm send-command \
  --instance-ids "i-0431a0aa517edf582" \
  --document-name "AWS-RunShellScript" \
  --timeout-seconds 7200 \
  --parameters '{"commands":["sudo su - ec2-user -c '\''aws s3 cp s3://polymarket-bot-backups-2c05a8a8/research/wallet-trades/_deploy/collect_trades.sh /tmp/collect_trades.sh --region eu-west-1 && bash /tmp/collect_trades.sh'\''"]}' \
  --region eu-west-1 \
  --comment "Collect trades for <USER>" \
  --output text --query 'Command.CommandId')
echo "CommandId=$CMD_ID"
```

All SSM commands require `required_permissions: ["all"]`.

### 4. Monitor progress

A full month of trades for an active user takes **3-8 minutes**.

Poll status periodically:

```bash
AWS_PROFILE=rafik aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "i-0431a0aa517edf582" \
  --region eu-west-1 \
  --query '{Status: Status}' \
  --output json
```

When `Status` is `Success`, retrieve full output:

```bash
AWS_PROFILE=rafik aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "i-0431a0aa517edf582" \
  --region eu-west-1 \
  --query '{Status: Status, Output: StandardOutputContent, Error: StandardErrorContent}' \
  --output json
```

### 5. Download results locally

```bash
AWS_PROFILE=rafik aws s3 cp \
  s3://polymarket-bot-backups-2c05a8a8/research/wallet-trades/<UTC_DATE>/<LABEL>_<START>_<END>.csv \
  data/ --region eu-west-1

AWS_PROFILE=rafik aws s3 cp \
  s3://polymarket-bot-backups-2c05a8a8/research/wallet-trades/<UTC_DATE>/<LABEL>_<START>_<END>_positions.csv \
  data/ --region eu-west-1

AWS_PROFILE=rafik aws s3 cp \
  s3://polymarket-bot-backups-2c05a8a8/research/wallet-trades/<UTC_DATE>/<LABEL>_<START>_<END>_closed_positions.csv \
  data/ --region eu-west-1
```

`<UTC_DATE>` is the UTC date when the job ran, formatted as `YYYY/MM/DD`.

### 6. Report results

After download, show the user:

1. **Trade count** and file sizes
2. **P&L summary** from the positions CSV — sum of `pnl` column, win/loss count
3. **Market breakdown** — count by market type (btc-5min, btc-15min, eth-15min, etc.)
4. **Sample trades** — a few earliest and latest rows

## Output Files

| File | Description |
|------|-------------|
| `<label>_<start>_<end>.csv` | Raw trades (one row per fill) |
| `<label>_<start>_<end>_positions.csv` | Per-(condition_id, outcome) P&L using `realizedPnl` from closed-positions |
| `<label>_<start>_<end>_closed_positions.csv` | Raw Data API closed-positions rows with Polymarket's own `realizedPnl` |

## P&L Notes

- **Primary source**: Data API `/closed-positions` `realizedPnl` — this matches
  the Polymarket profile "profit" figure closely.
- **Fallback**: Gamma API market resolution (slow, rate-limited) for any positions
  not covered by closed-positions.
- Positions are keyed by `(condition_id, outcome)` — Up and Down are never mixed.
- The old `CLOB last-trade-price` heuristic is removed; do not use it.

## Guardrails

- Always run on EC2 via SSM — local Polymarket API calls will fail.
- Always sync latest `fetch_wallet_trades.py` + `src/trade_fetcher.py` before running.
- Use `required_permissions: ["all"]` for all SSM and S3 commands.
- For unknown handles, resolve wallet first — never pass bare `@handle` to the
  Data API (it silently returns ALL trades unfiltered).
- Closed-positions pagination is capped at 50 per page; a user with many closed
  positions may need several minutes of paging.
