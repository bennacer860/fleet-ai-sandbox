---
name: trade-stats
description: >-
  Query successful trades, fills, and P&L from the Polymarket bot's SQLite
  database on EC2 via AWS SSM. Use when the user asks about today's trades,
  fill counts, order sources, P&L, trade history, or how many orders were
  matched/filled.
---

# Trade Stats

## Purpose

Run read-only SQL queries against the bot's SQLite database on EC2 to report
trade activity, fill counts by source, and P&L.

## Environment

- AWS profile: `rafik`
- Instance ID: `i-04fb74e5b95fdc098`
- Region: `eu-west-1`
- App path: `/opt/polymarket-bot`
- Database: `data/bot_p2.db`
- Python: `.venv/bin/python`
- No `sqlite3` CLI on the instance — use Python with the venv.

## Technique

The instance has no `sqlite3` binary. SSM shell quoting makes inline Python
fragile. Use the **base64 script** pattern:

1. Write a Python script locally and base64-encode it.
2. Send a single SSM command that decodes and runs it.

```bash
# Step 1 — encode locally
cat << 'PYEOF' | base64 > /tmp/query_b64.txt
<python script here>
PYEOF

# Step 2 — send via SSM (substitute $B64 with the base64 string)
B64="<paste base64>"
AWS_PROFILE=rafik aws ssm send-command \
  --instance-ids "i-04fb74e5b95fdc098" \
  --document-name "AWS-RunShellScript" \
  --parameters "{\"commands\":[\"sudo su - ec2-user -c 'cd /opt/polymarket-bot && echo $B64 | base64 -d > /tmp/q.py && .venv/bin/python /tmp/q.py'\"]}" \
  --region eu-west-1 \
  --output json --query 'Command.CommandId'

# Step 3 — fetch result (wait ~8s)
AWS_PROFILE=rafik aws ssm get-command-invocation \
  --command-id "<COMMAND_ID>" \
  --instance-id "i-04fb74e5b95fdc098" \
  --region eu-west-1 \
  --query '{Status: Status, Output: StandardOutputContent, Error: StandardErrorContent}' \
  --output json
```

All SSM commands require `required_permissions: ["full_network"]`.

## Database Schema

Key tables (see `src/storage/database.py` for full schema):

- **orders**: Every order placed. Key columns: `slug`, `strategy`,
  `submission_source`, `final_status`, `placed_at` (epoch), `price`, `size`,
  `side`, `dry_run`.
- **fills**: Exchange fill confirmations. Key columns: `order_id`, `source`
  (e.g. `ws`), `status` (`FILLED` / `PARTIAL`), `fill_size`, `timestamp`
  (epoch).
- **trades**: Round-trip entries (entry+exit). Key columns: `net_pnl`, `fees`,
  `timestamp_entry` (epoch).
- **positions**: Open positions. Key columns: `token_id`, `quantity`,
  `avg_entry_price`, `realized_pnl`.
- **decisions**: Strategy decision log.

### Status values

| `final_status` (orders) | Meaning |
|--------------------------|---------|
| `FILLED` | Fully filled |
| `PARTIAL` | Partially filled |
| `SUBMITTED` | Still live |
| `EXPIRED_STALE` | Expired unfilled |
| `REJECTED` | Exchange rejected |

### Submission sources

`watched_expiry`, `book_update`, `tick_size_change`, `poll`,
`immediate_tick`, `unknown`.

## Standard Query — Today's Trade Summary

Use this Python script as the base. Adjust the date as needed.

```python
import sqlite3, datetime

conn = sqlite3.connect("data/bot_p2.db")
cur = conn.cursor()

today_start = datetime.datetime(2026, 3, 30).timestamp()  # adjust date
today_end = today_start + 86400

# Fills by source and status
cur.execute("""
    SELECT f.source, f.status, COUNT(*) as cnt, SUM(f.fill_size) as total_size
    FROM fills f
    WHERE f.timestamp >= ? AND f.timestamp < ?
    GROUP BY f.source, f.status
    ORDER BY cnt DESC
""", (today_start, today_end))
print("=== FILLS BY SOURCE & STATUS ===")
for row in cur.fetchall():
    print(f"  source={row[0]}  status={row[1]}  count={row[2]}  total_size={row[3]:.4f}")

# Orders by submission source and status
cur.execute("""
    SELECT submission_source, final_status, COUNT(*) as cnt
    FROM orders
    WHERE placed_at >= ? AND placed_at < ?
    GROUP BY submission_source, final_status
    ORDER BY cnt DESC
""", (today_start, today_end))
print("\n=== ORDERS BY SUBMISSION SOURCE & STATUS ===")
for row in cur.fetchall():
    print(f"  source={row[0]}  status={row[1]}  count={row[2]}")

# Filled orders detail
cur.execute("""
    SELECT o.strategy, o.slug, o.side, o.price, o.size, o.final_status,
           o.submission_source, datetime(o.placed_at, 'unixepoch') as placed
    FROM orders o
    WHERE o.placed_at >= ? AND o.placed_at < ?
    AND o.final_status IN ('FILLED', 'PARTIAL')
    ORDER BY o.placed_at DESC
""", (today_start, today_end))
print("\n=== FILLED ORDERS TODAY ===")
rows = cur.fetchall()
print(f"Total: {len(rows)}")
for row in rows:
    print(f"  {row[7]} | {row[0]} | {row[1][:45]} | {row[2]} @ {row[3]} x {row[4]} | src={row[6]}")

# Trades P&L
cur.execute("""
    SELECT COUNT(*), COALESCE(SUM(net_pnl),0), COALESCE(SUM(fees),0)
    FROM trades
    WHERE timestamp_entry >= ? AND timestamp_entry < ?
""", (today_start, today_end))
pnl = cur.fetchone()
print(f"\n=== TRADES P&L ===")
print(f"  trades={pnl[0]}  net_pnl=${pnl[1]:.4f}  fees=${pnl[2]:.4f}")

conn.close()
```

## Output Format

Present results to the user as:

1. **Fill count** — total fills (FILLED + PARTIAL) and share volume.
2. **Source breakdown** — table of submission sources with fill counts.
3. **Order funnel** — how many orders were placed vs filled vs expired.
4. **P&L** — net P&L and fees from round-trip trades (if any).

## Guardrails

- Read-only queries only — never modify the database.
- Always use `required_permissions: ["full_network"]` for SSM calls.
- Wait ~8 seconds before fetching SSM command output.
- Adjust the date in the script to match the user's request (today, yesterday, date range).
