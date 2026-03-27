---
name: deploy-bot
description: >-
  Deploy the Polymarket bot to production via AWS SSM. Use when the user
  asks to deploy, push to prod, ship, release, or restart the bot on EC2.
---

# Deploy Polymarket Bot

## Prerequisites

- AWS CLI configured with `AWS_PROFILE=rafik`
- Instance ID: `i-04fb74e5b95fdc098`
- Region: `eu-west-1`
- Bot runs at `/opt/polymarket-bot` as `ec2-user`
- Service: `polymarket-bot.service` (systemd → tmux)

## Deployment Steps

### 1. Ensure changes are committed and pushed

Verify the current branch is pushed to origin before deploying.
Use `git status` and `git push` if needed.

### 2. Deploy via SSM send-command

Run the deployment as a non-interactive SSM command (requires `required_permissions: ["full_network"]`):

```bash
AWS_PROFILE=rafik aws ssm send-command \
  --instance-ids "i-04fb74e5b95fdc098" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["sudo su - ec2-user -c '\''cd /opt/polymarket-bot && git pull && .venv/bin/pip install -r requirements.txt -q && sudo systemctl restart polymarket-bot && echo Deploy complete && sleep 3 && sudo systemctl status polymarket-bot --no-pager'\''"]' \
  --region eu-west-1 \
  --comment "Deploy from Cursor" \
  --output json --query 'Command.CommandId'
```

Save the returned CommandId for verification.

### 3. Verify deployment

Wait 10-15 seconds, then check the result:

```bash
AWS_PROFILE=rafik aws ssm get-command-invocation \
  --command-id "<COMMAND_ID>" \
  --instance-id "i-04fb74e5b95fdc098" \
  --region eu-west-1 \
  --query '{Status: Status, Output: StandardOutputContent, Error: StandardErrorContent}' \
  --output json
```

### 4. Confirm success

Check for:
- `"Status": "Success"` in the response
- `Active: active (running)` in the systemctl output
- No errors in `StandardErrorContent` (git fetch messages in stderr are normal)

Report the deployment result to the user.

## Branch Handling

The instance may be on any branch. `git pull` updates the current branch.
To deploy a specific branch, replace `git pull` with:

```bash
git fetch origin && git checkout <branch-name> && git pull
```

## Running Ad-Hoc Commands

For any command on the instance (DB queries, log checks, status), use:

```bash
AWS_PROFILE=rafik aws ssm send-command \
  --instance-ids "i-04fb74e5b95fdc098" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["sudo su - ec2-user -c '\''<COMMAND>'\''"]' \
  --region eu-west-1 \
  --output json --query 'Command.CommandId'
```

Then retrieve output with `get-command-invocation` as in step 3.

## Troubleshooting

- **Instance not responding**: Check instance status with `aws ec2 describe-instance-status`
- **InvocationDoesNotExist**: SSM agent hasn't picked up the command yet — wait and retry
- **Command timeout**: The default timeout is 3600s; increase if running long operations
