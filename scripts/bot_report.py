#!/usr/bin/env python3
"""Bot performance report — runs on EC2 via SSM.

Usage (from local machine):
    python3 scripts/p2_report.py                              # profile 2, last 24h
    python3 scripts/p2_report.py --profile 1                  # profile 1, last 24h
    python3 scripts/p2_report.py --hours 168                  # last 7 days
    python3 scripts/p2_report.py --all                        # all time
    python3 scripts/p2_report.py --strategy post_expiry       # filter by strategy
    python3 scripts/p2_report.py --tag v3-test                # filter by tag
    python3 scripts/p2_report.py --profile 1 --strategy sweep --hours 48
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time

AWS_PROFILE = "rafik"
INSTANCE_ID = "i-04fb74e5b95fdc098"
REGION = "eu-west-1"
APP_PATH = "/opt/polymarket-bot"
VENV_PYTHON = ".venv/bin/python"

REMOTE_SCRIPT = r'''
import sqlite3, datetime, re, time

HOURS    = __HOURS__
DB       = "__DB__"
STRATEGY = "__STRATEGY__"
TAG      = "__TAG__"

conn = sqlite3.connect(DB)
cur = conn.cursor()

if HOURS > 0:
    cutoff = time.time() - HOURS * 3600
    time_label = f"last {HOURS}h"
else:
    cutoff = 0
    time_label = "all time"

filters = ["dry_run=0", "placed_at >= ?"]
trade_filters = ["timestamp_entry >= ?"]
params = [cutoff]
trade_params = [cutoff]
filter_parts = []

if STRATEGY:
    filters.append("strategy = ?")
    trade_filters.append("strategy = ?")
    params.append(STRATEGY)
    trade_params.append(STRATEGY)
    filter_parts.append(f"strategy={STRATEGY}")

if TAG:
    filters.append("tag = ?")
    trade_filters.append("tag = ?")
    params.append(TAG)
    trade_params.append(TAG)
    filter_parts.append(f"tag={TAG}")

order_where = " AND ".join(filters)
trade_where = " AND ".join(trade_filters)
filter_label = " | ".join(filter_parts) if filter_parts else "no filters"

W = 42

def crypto_label(slug):
    if not slug: return "OTHER"
    sl = slug.lower()
    for token, lbl in [('btc','BTC'),('eth','ETH'),('sol','SOL'),('xrp','XRP'),
                        ('doge','DOGE'),('hype','HYPE'),('bnb','BNB'),
                        ('ada','ADA'),('avax','AVAX'),('link','LINK')]:
        if token in sl:
            return lbl
    return "OTHER"

def dur_label(slug):
    m = re.search(r'-(\d+)m-', slug or "")
    return m.group(1) + "min" if m else "other"

def pct(n, total):
    return n / total * 100 if total else 0

# ── DB health ────────────────────────────────────────────────────────────

ic = cur.execute("PRAGMA integrity_check").fetchone()[0]
jm = cur.execute("PRAGMA journal_mode").fetchone()[0]
ac = cur.execute("PRAGMA wal_autocheckpoint").fetchone()[0]

print("=" * 60)
print(f"PERFORMANCE REPORT — {DB}")
print(f"Window: {time_label} | {filter_label}")
print(f"Generated: {datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC")
print(f"DB: {ic} | journal={jm} | autocheckpoint={ac}")
print("=" * 60)

# ── strategies in DB ─────────────────────────────────────────────────────

cur.execute("SELECT DISTINCT strategy FROM orders UNION SELECT DISTINCT strategy FROM trades")
all_strategies = [r[0] for r in cur.fetchall()]
print(f"\nStrategies in DB: {', '.join(all_strategies) or 'none'}")

# ── tags in DB ───────────────────────────────────────────────────────────

cur.execute("SELECT DISTINCT tag FROM orders WHERE tag != '' UNION SELECT DISTINCT tag FROM trades WHERE tag != ''")
all_tags = [r[0] for r in cur.fetchall()]
if all_tags:
    print(f"Tags in DB: {', '.join(all_tags)}")

# ── order funnel ─────────────────────────────────────────────────────────

cur.execute(f"""
    SELECT final_status, COUNT(*), COALESCE(SUM(size),0)
    FROM orders WHERE {order_where}
    GROUP BY final_status ORDER BY COUNT(*) DESC
""", params)
rows = cur.fetchall()
total = sum(r[1] for r in rows)

print(f"\n{'='*W}\nORDER FUNNEL (total: {total})\n{'='*W}")
for s, c, v in rows:
    print(f"  {(s or 'NULL'):20s}  {c:5d} ({pct(c,total):5.1f}%)  vol={v:.0f}")

# ── fills by crypto ──────────────────────────────────────────────────────

fill_where = order_where.replace("placed_at", "f.timestamp")
cur.execute(f"""
    SELECT o.slug, COUNT(*), COALESCE(SUM(f.fill_size),0)
    FROM fills f JOIN orders o ON f.order_id = o.order_id
    WHERE {fill_where}
    GROUP BY o.slug
""", params)
crypto, dur, total_fills = {}, {}, 0
for slug, fills, vol in cur.fetchall():
    total_fills += fills
    cl = crypto_label(slug)
    crypto.setdefault(cl, {"fills":0,"vol":0.0})
    crypto[cl]["fills"] += fills; crypto[cl]["vol"] += vol
    dl = dur_label(slug)
    dur.setdefault(dl, {"fills":0,"vol":0.0})
    dur[dl]["fills"] += fills; dur[dl]["vol"] += vol

print(f"\n{'='*W}\nFILLS BY CRYPTO (total: {total_fills})\n{'='*W}")
for k in sorted(crypto, key=lambda x: -crypto[x]["fills"]):
    v = crypto[k]
    print(f"  {k:6s}  fills={v['fills']:4d} ({pct(v['fills'],total_fills):5.1f}%)  vol={v['vol']:>8.0f}")

# ── fills by duration ────────────────────────────────────────────────────

print(f"\n{'='*W}\nFILLS BY DURATION\n{'='*W}")
for k in sorted(dur, key=lambda x: -dur[x]["fills"]):
    v = dur[k]
    print(f"  {k:10s}  fills={v['fills']:4d} ({pct(v['fills'],total_fills):5.1f}%)  vol={v['vol']:>8.0f}")

# ── fills by 4h time window ─────────────────────────────────────────────

cur.execute(f"""
    SELECT f.timestamp, f.fill_size
    FROM fills f JOIN orders o ON f.order_id = o.order_id
    WHERE {fill_where}
""", params)
windows = [["00-04",0,0.0],["04-08",0,0.0],["08-12",0,0.0],
           ["12-16",0,0.0],["16-20",0,0.0],["20-24",0,0.0]]
for ts, sz in cur.fetchall():
    if not ts: continue
    idx = datetime.datetime.utcfromtimestamp(ts).hour // 4
    windows[idx][1] += 1; windows[idx][2] += sz or 0

best_idx = max(range(6), key=lambda i: windows[i][1])
print(f"\n{'='*W}\nFILLS BY TIME OF DAY (UTC)\n{'='*W}")
for i, (lbl, f, v) in enumerate(windows):
    bar = "#" * int(pct(f, total_fills) / 2)
    tag = " <<<" if i == best_idx and f > 0 else ""
    print(f"  {lbl} UTC  fills={f:4d} ({pct(f,total_fills):5.1f}%)  vol={v:>8.0f}  {bar}{tag}")

# ── fills by day ─────────────────────────────────────────────────────────

cur.execute(f"""
    SELECT date(f.timestamp, 'unixepoch'), COUNT(*), COALESCE(SUM(f.fill_size),0)
    FROM fills f JOIN orders o ON f.order_id = o.order_id
    WHERE {fill_where}
    GROUP BY 1 ORDER BY 1
""", params)
print(f"\n{'='*W}\nFILLS BY DAY\n{'='*W}")
for day, f, v in cur.fetchall():
    print(f"  {day}  fills={f:4d}  vol={v:>8.0f}")

# ── P&L ──────────────────────────────────────────────────────────────────

cur.execute(f"""
    SELECT COUNT(*),
           COALESCE(SUM(net_pnl),0), COALESCE(SUM(gross_pnl),0), COALESCE(SUM(fees),0),
           SUM(CASE WHEN net_pnl>=0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN net_pnl<0  THEN 1 ELSE 0 END),
           COALESCE(SUM(size),0)
    FROM trades WHERE {trade_where}
""", trade_params)
t, net, gross, fees, w, l, vol = cur.fetchone()
wr = pct(w, (w or 0)+(l or 0))
print(f"\n{'='*W}\nP&L SUMMARY\n{'='*W}")
print(f"  Trades: {t}   Wins: {w or 0}   Losses: {l or 0}   WR: {wr:.1f}%")
print(f"  Gross: ${gross:.4f}   Net: ${net:.4f}   Fees: ${fees:.4f}")
if t:
    print(f"  Volume: {vol:.0f} shares   Avg P&L: ${net/t:.4f}")

# ── daily P&L ────────────────────────────────────────────────────────────

cur.execute(f"""
    SELECT date(timestamp_entry, 'unixepoch'), COUNT(*), SUM(net_pnl),
           SUM(CASE WHEN net_pnl>=0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN net_pnl<0  THEN 1 ELSE 0 END)
    FROM trades WHERE {trade_where}
    GROUP BY 1 ORDER BY 1
""", trade_params)
rows = cur.fetchall()
if rows:
    print(f"\n{'='*W}\nDAILY P&L\n{'='*W}")
    cum = 0
    for day, n, pnl, w2, l2 in rows:
        cum += pnl or 0
        print(f"  {day}  trades={n:3d}  W={w2 or 0} L={l2 or 0}  pnl=${pnl:.4f}  cum=${cum:.4f}")

# ── losing trades ────────────────────────────────────────────────────────

cur.execute(f"""
    SELECT slug, entry_price, exit_price, size, net_pnl,
           datetime(timestamp_entry, 'unixepoch')
    FROM trades WHERE net_pnl < 0 AND {trade_where}
    ORDER BY net_pnl
""", trade_params)
losers = cur.fetchall()
if losers:
    print(f"\n{'='*W}\nLOSING TRADES ({len(losers)})\n{'='*W}")
    for slug, ep, xp, sz, pnl, ts in losers:
        print(f"  {ts}  {(slug or '?')[:45]:45s}  e={ep:.4f} x={xp or 0:.4f}  sz={sz:.0f}  pnl=${pnl:.4f}")

# ── rejections ───────────────────────────────────────────────────────────

rej_where = order_where + " AND final_status IN ('REJECTED','FAILED')"
cur.execute(f"""
    SELECT rejection_reason, COUNT(*)
    FROM orders WHERE {rej_where}
    GROUP BY rejection_reason ORDER BY COUNT(*) DESC
""", params)
rej = cur.fetchall()
if rej:
    print(f"\n{'='*W}\nREJECTIONS\n{'='*W}")
    for reason, cnt in rej:
        print(f"  {cnt:5d}  {(reason or 'unknown')[:70]}")

# ── submission source ────────────────────────────────────────────────────

cur.execute(f"""
    SELECT submission_source, COUNT(*),
           SUM(CASE WHEN final_status IN ('FILLED','PARTIAL') THEN 1 ELSE 0 END),
           SUM(CASE WHEN final_status='EXPIRED_STALE' THEN 1 ELSE 0 END)
    FROM orders WHERE {order_where}
    GROUP BY submission_source ORDER BY 3 DESC
""", params)
print(f"\n{'='*W}\nSUBMISSION SOURCES\n{'='*W}")
for src, tot, filled, stale in cur.fetchall():
    print(f"  {(src or '?'):20s}  total={tot:5d}  filled={filled:4d} ({pct(filled,tot):5.1f}%)  stale={stale:4d}")

# ── top markets ──────────────────────────────────────────────────────────

cur.execute(f"""
    SELECT o.slug, COUNT(*), COALESCE(SUM(f.fill_size),0)
    FROM fills f JOIN orders o ON f.order_id = o.order_id
    WHERE {fill_where}
    GROUP BY o.slug ORDER BY COUNT(*) DESC LIMIT 10
""", params)
rows = cur.fetchall()
if rows:
    print(f"\n{'='*W}\nTOP MARKETS BY FILLS\n{'='*W}")
    for slug, f, v in rows:
        print(f"  {(slug or '?')[:50]:50s}  fills={f:3d}  vol={v:>8.0f}")

# ── per-strategy breakdown (if no strategy filter) ───────────────────────

if not STRATEGY:
    cur.execute(f"""
        SELECT o.strategy, COUNT(*),
               SUM(CASE WHEN o.final_status IN ('FILLED','PARTIAL') THEN 1 ELSE 0 END),
               SUM(CASE WHEN o.final_status='EXPIRED_STALE' THEN 1 ELSE 0 END)
        FROM orders o WHERE {order_where}
        GROUP BY o.strategy ORDER BY 3 DESC
    """, params)
    strat_rows = cur.fetchall()
    if strat_rows:
        print(f"\n{'='*W}\nPER-STRATEGY BREAKDOWN\n{'='*W}")
        for strat, tot, filled, stale in strat_rows:
            print(f"  {(strat or '?'):25s}  orders={tot:5d}  filled={filled:4d} ({pct(filled,tot):5.1f}%)  stale={stale:4d}")

# ── uptime ───────────────────────────────────────────────────────────────

cur.execute(f"""
    SELECT MIN(placed_at), MAX(placed_at), COUNT(*)
    FROM orders WHERE {order_where}
""", params)
mn, mx, cnt = cur.fetchone()
if mn and mx:
    hrs = (mx - mn) / 3600
    print(f"\n{'='*W}\nOPERATING RANGE\n{'='*W}")
    print(f"  From: {datetime.datetime.utcfromtimestamp(mn)}  To: {datetime.datetime.utcfromtimestamp(mx)}")
    if hrs > 0:
        print(f"  Uptime: {hrs:.1f}h ({hrs/24:.1f}d)   Orders/hr: {cnt/hrs:.1f}")

conn.close()
'''


def db_path_for_profile(profile: int) -> str:
    return f"data/bot_p{profile}.db"


def run_remote(profile: int, hours: int, strategy: str, tag: str) -> str:
    db = db_path_for_profile(profile)
    script = (
        REMOTE_SCRIPT
        .replace("__HOURS__", str(hours))
        .replace("__DB__", db)
        .replace("__STRATEGY__", strategy)
        .replace("__TAG__", tag)
    )
    b64 = base64.b64encode(script.encode()).decode()

    cmd = [
        "aws", "ssm", "send-command",
        "--instance-ids", INSTANCE_ID,
        "--document-name", "AWS-RunShellScript",
        "--parameters", json.dumps({
            "commands": [
                f"sudo su - ec2-user -c 'cd {APP_PATH} && echo {b64} | base64 -d > /tmp/_report.py && {VENV_PYTHON} /tmp/_report.py'"
            ]
        }),
        "--region", REGION,
        "--output", "json",
        "--query", "Command.CommandId",
    ]
    env_with_profile = dict(os.environ, AWS_PROFILE=AWS_PROFILE)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env_with_profile)
    if result.returncode != 0:
        print(f"SSM send-command failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    command_id = json.loads(result.stdout)
    print(f"SSM command: {command_id}  — waiting for result…", file=sys.stderr)

    for attempt in range(12):
        time.sleep(5)
        get_cmd = [
            "aws", "ssm", "get-command-invocation",
            "--command-id", command_id,
            "--instance-id", INSTANCE_ID,
            "--region", REGION,
            "--output", "json",
        ]
        r = subprocess.run(get_cmd, capture_output=True, text=True, env=env_with_profile)
        if r.returncode != 0:
            continue
        data = json.loads(r.stdout)
        status = data.get("Status", "")
        if status in ("Success", "Failed"):
            output = data.get("StandardOutputContent", "")
            error = data.get("StandardErrorContent", "")
            if error:
                print(error, file=sys.stderr)
            return output
        print(f"  … status={status} (attempt {attempt+1})", file=sys.stderr)

    print("Timed out waiting for SSM command", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Bot performance report via SSM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python3 scripts/p2_report.py                              # profile 2, last 24h
  python3 scripts/p2_report.py --profile 1                  # profile 1
  python3 scripts/p2_report.py --hours 168                  # last 7 days
  python3 scripts/p2_report.py --all                        # all time
  python3 scripts/p2_report.py --strategy post_expiry       # filter by strategy
  python3 scripts/p2_report.py --tag v3-test                # filter by tag
  python3 scripts/p2_report.py -p 1 -s sweep --hours 48    # combined filters
""")
    parser.add_argument("-p", "--profile", type=int, default=2,
                        help="Profile number (default: 2) → data/bot_p<N>.db")
    parser.add_argument("--hours", type=int, default=24,
                        help="Lookback window in hours (default: 24)")
    parser.add_argument("--all", action="store_true",
                        help="Show all-time data (overrides --hours)")
    parser.add_argument("-s", "--strategy", type=str, default="",
                        help="Filter by strategy name (e.g. post_expiry, sweep, gabagool)")
    parser.add_argument("-t", "--tag", type=str, default="",
                        help="Filter by session tag")
    args = parser.parse_args()

    hours = 0 if args.all else args.hours
    output = run_remote(args.profile, hours, args.strategy, args.tag)
    print(output)


if __name__ == "__main__":
    main()
