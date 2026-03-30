---
name: ws-stale-triage
description: >-
  Investigate frozen dashboard prices or stale websocket feeds for the
  Polymarket bot using AWS SSM. Use when the user reports prices not updating,
  websocket staleness, feed freezes that recover after restart, or asks to
  diagnose market/user/crypto WS health on EC2.
---

# WS Stale Triage

## Purpose

Run a fast, read-only incident workflow to determine whether the bot is truly
stale, which websocket channel is stale, and whether restart is needed.

## Environment

- AWS profile: `rafik`
- Instance ID: `i-04fb74e5b95fdc098`
- Region: `eu-west-1`
- App path: `/opt/polymarket-bot`
- Service: `polymarket-bot` (profile 2)
- Dashboard tmux session: `bot-p2`
- Log file: `/opt/polymarket-bot/data/bot_p2.log`

## Step 1 - Baseline health

Run via SSM:

```bash
AWS_PROFILE=rafik aws ssm send-command \
  --instance-ids "i-04fb74e5b95fdc098" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["sudo su - ec2-user -c '\''cd /opt/polymarket-bot && sudo systemctl status polymarket-bot --no-pager -l | sed -n \"1,90p\"'\''"]' \
  --region eu-west-1 \
  --output json --query 'Command.CommandId'
```

Then fetch output with `get-command-invocation`.

Check:
- Service is `Active: active (running)`.
- No crash loop or repeated restarts.

## Step 2 - Dashboard feed snapshot (twice)

Capture two snapshots 15-30s apart:

```bash
AWS_PROFILE=rafik aws ssm send-command \
  --instance-ids "i-04fb74e5b95fdc098" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["sudo su - ec2-user -c '\''tmux capture-pane -pt bot-p2:0 -S -120 | grep -E \"WS Market:|WS User:|WS Crypto:|Market channels:|Spot:\"'\''"]' \
  --region eu-west-1 \
  --output json --query 'Command.CommandId'
```

Compare snapshots:
- `Spot:` values should move.
- `WS Market Last msg` should not keep rising.
- `Market channels:` (`book`, `price_change`, `tick_size`) should refresh.

## Step 3 - Log evidence

Fetch recent logs:

```bash
AWS_PROFILE=rafik aws ssm send-command \
  --instance-ids "i-04fb74e5b95fdc098" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["sudo su - ec2-user -c '\''tail -n 260 /opt/polymarket-bot/data/bot_p2.log'\''"]' \
  --region eu-west-1 \
  --output json --query 'Command.CommandId'
```

Look for:
- `[HEARTBEAT] ... forcing reconnect`
- `[WS_MARKET] Disconnected ... reconnect`
- `[MARKET_WS_SUB] Re-subscribed ...`

## Step 4 - Decision

- **Healthy / not stale**:
  - WS connected, channel ages refreshing, spot moves across snapshots.
  - Report likely low-activity rows or market microstructure effect.

- **Stale but self-healing**:
  - Channel ages spike then reconnect logs appear and recover.
  - Report transient upstream/network stall.

- **Stale and not recovering**:
  - Channel ages keep growing, spot frozen, no successful reconnect.
  - Recommend controlled restart:
    - `sudo systemctl restart polymarket-bot`
  - Re-check Step 2 immediately after restart.

## Output format

Return:
1. Current service state.
2. Snapshot comparison (what moved vs frozen).
3. Channel-age and reconnect evidence.
4. Root-cause confidence and next action.

## Guardrails

- Do read-only triage first.
- Do not restart automatically unless user asks.
- Do not claim "fixed" without post-action verification snapshot.
