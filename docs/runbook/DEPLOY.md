# Production Deployment Guide

This guide covers deploying the Polymarket bot to AWS EC2 from scratch, day-to-day operations, and disaster recovery.

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│  AWS eu-west-1 (Ireland)                                 │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ VPC 10.0.0.0/16                                     │ │
│  │                                                     │ │
│  │  ┌───────────────────────────────────────────────┐  │ │
│  │  │ Public Subnet 10.0.1.0/24                     │  │ │
│  │  │                                               │  │ │
│  │  │  ┌──────────────────────────────────────────┐  │  │ │
│  │  │  │ EC2 t4g.nano (ARM64, ~$3/mo)            │  │  │ │
│  │  │  │                                          │  │  │ │
│  │  │  │  systemd → tmux → python main.py run     │  │  │ │
│  │  │  │  Litestream → S3 (continuous DB backup)  │  │  │ │
│  │  │  │  log-sync.timer → S3 (hourly log archive)│  │  │ │
│  │  │  └──────────────────────────────────────────┘  │  │ │
│  │  └───────────────────────────────────────────────┘  │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                          │
│  S3 Bucket ── SQLite replicas + archived logs            │
│  SSM Parameter Store ── secrets (PRIVATE_KEY, etc.)      │
└──────────────────────────────────────────────────────────┘
```

**Cost estimate:** ~$4-5/month (t4g.nano on-demand + S3 storage).

**Access:** No SSH, no inbound ports. All access via AWS SSM Session Manager.

---

## Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| [Terraform](https://developer.hashicorp.com/terraform/downloads) | Provision infrastructure | `brew install terraform` |
| [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | Interact with AWS | `brew install awscli` |
| [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) | Connect to EC2 via SSM | `brew install --cask session-manager-plugin` |
| Git | Push code for the instance to pull | — |

Ensure your AWS credentials are configured:

```bash
aws configure
# or set AWS_PROFILE if using named profiles
```

---

## 1. Store Secrets in SSM Parameter Store

All secrets are stored as `SecureString` parameters in SSM under the `/polymarket-bot/` prefix.

```bash
REGION="eu-west-1"

aws ssm put-parameter --region $REGION --type SecureString \
  --name "/polymarket-bot/PRIVATE_KEY" \
  --value "<your-polygon-private-key>"

aws ssm put-parameter --region $REGION --type SecureString \
  --name "/polymarket-bot/FUNDER" \
  --value "<your-funder-address>"

aws ssm put-parameter --region $REGION --type SecureString \
  --name "/polymarket-bot/POLYGON_RPC_URL" \
  --value "<your-rpc-url>"
```

Add any additional `.env` variables the same way (e.g. `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

To update an existing parameter:

```bash
aws ssm put-parameter --region $REGION --type SecureString \
  --name "/polymarket-bot/DEFAULT_TRADE_SIZE" \
  --value "5" \
  --overwrite
```

After updating SSM parameters, refresh them on the running instance:

```bash
# From your local machine (replace INSTANCE_ID)
aws ssm send-command \
  --instance-ids "$INSTANCE_ID" --region eu-west-1 \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["cd /opt/polymarket-bot && aws ssm get-parameters-by-path --path /polymarket-bot/ --with-decryption --region eu-west-1 --query \"Parameters[*].[Name,Value]\" --output text | while read -r name value; do key=$(basename \"$name\"); sed -i \"/^$key=/d\" .env; echo \"$key=$value\" >> .env; done && sudo systemctl restart polymarket-bot && sudo systemctl restart polymarket-bot-p1-gabagool"]'
```

---

## 2. Provision Infrastructure (First Time)

```bash
cd terraform
terraform init
terraform apply
```

Terraform creates:
- VPC, subnet, internet gateway, route table
- Security group (outbound-only, no inbound ports)
- IAM role with SSM, S3, and Parameter Store access
- S3 bucket for Litestream DB backups and log archives
- EC2 instance (t4g.nano, Amazon Linux 2023 ARM64)

The instance's `user_data.sh` bootstrap script automatically:
1. Installs Python 3.11, tmux, Litestream
2. Clones the repo from GitHub
3. Pulls secrets from SSM into `/opt/polymarket-bot/.env`
4. Creates a Python venv and installs dependencies
5. Restores SQLite databases from S3 (if replicas exist)
6. Starts the bot and Litestream via systemd

Save the outputs — you'll need the instance ID:

```bash
terraform output instance_id
terraform output ssm_connect_command
```

---

## 3. Deploy Code Updates

Push your changes to GitHub, then run the deploy script:

```bash
./deploy/deploy.sh
```

This connects to the instance via SSM and runs:
1. `git pull` to fetch the latest code
2. `pip install -r requirements.txt` to update dependencies
3. `systemctl restart polymarket-bot` and `systemctl restart polymarket-bot-p1-gabagool` to restart both profiles

### Manual deploy (if the script fails)

```bash
# Connect to the instance
aws ssm start-session --target <INSTANCE_ID> --region eu-west-1

# On the instance:
sudo su - ec2-user
cd /opt/polymarket-bot
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart polymarket-bot
sudo systemctl restart polymarket-bot-p1-gabagool
```

---

## 4. View the Live Dashboard

```bash
aws ssm start-session --target <INSTANCE_ID> --region eu-west-1

# Attach to one profile tmux session:
tmux attach -t bot-p2
# or
tmux attach -t bot-p1
```

Detach without stopping the bot: press `Ctrl+B`, then `D`.

---

## 5. Systemd Services

The bot runs as four systemd services:

| Service | Description | Config file |
|---------|-------------|-------------|
| `polymarket-bot` | Profile 2 (`post_expiry`) inside tmux session `bot-p2` | `deploy/polymarket-bot.service` |
| `polymarket-bot-p1-gabagool` | Profile 1 (`gabagool`) inside tmux session `bot-p1` | `deploy/polymarket-bot-p1-gabagool.service` |
| `litestream` | Continuous SQLite replication to S3 | `deploy/litestream.service` |
| `log-sync.timer` | Hourly archival of rotated logs to S3 | `deploy/log-sync.timer` |

Common systemd commands (run on the instance):

```bash
# Check status
sudo systemctl status polymarket-bot
sudo systemctl status polymarket-bot-p1-gabagool
sudo systemctl status litestream

# Restart
sudo systemctl restart polymarket-bot
sudo systemctl restart polymarket-bot-p1-gabagool

# View logs (systemd journal)
sudo journalctl -u polymarket-bot --since "30 min ago" -f
sudo journalctl -u polymarket-bot-p1-gabagool --since "30 min ago" -f

# View bot's own log file (more detailed)
tail -100 /opt/polymarket-bot/data/bot_p2.log
```

---

## 6. Database Backups (Litestream)

Litestream continuously replicates SQLite databases to S3 with ~1 second lag.

**Replicated databases:**
- `bot.db` → `s3://<bucket>/bot.db`
- `bot_p2.db` → `s3://<bucket>/bot_p2.db`

The config lives at `/etc/litestream/litestream.yml` on the instance (source: `deploy/litestream.yml`).

### Verify replication is working

```bash
# On the instance:
sudo systemctl status litestream

# Check the S3 bucket from your local machine:
BUCKET=$(cd terraform && terraform output -raw s3_bucket_name)
aws s3 ls s3://$BUCKET/ --recursive | head -20
```

### Manual restore (disaster recovery)

If you need to restore databases on a fresh instance:

```bash
# On the instance:
litestream restore -config /etc/litestream/litestream.yml \
  -if-replica-exists /opt/polymarket-bot/data/bot.db

litestream restore -config /etc/litestream/litestream.yml \
  -if-replica-exists /opt/polymarket-bot/data/bot_p2.db
```

This happens automatically during bootstrap (`user_data.sh`), but can be run manually if needed.

---

## 7. Bot Configuration

Profile 2 runtime configuration is set in `deploy/polymarket-bot.service`:

```
python main.py run \
  --markets BTC ETH SOL XRP DOGE HYPE BNB \
  --durations 5 15 \
  --price-threshold 0.99 \
  --profile 2 \
  --strategy post_expiry \
  --claim 60 \
  --dashboard
```

| Flag | Description |
|------|-------------|
| `--markets` | Crypto assets to monitor |
| `--durations` | Market durations in minutes |
| `--price-threshold` | Min bid price to trigger a trade |
| `--profile 2` | Namespaces DB/log files (`bot_p2.db`, `bot_p2.log`) |
| `--strategy` | Trading strategy (`sweep`, `post_expiry`, `aggressive_post_expiry`) |
| `--claim` | Auto-claim winning positions every N seconds |
| `--dashboard` | Enable the Rich TUI dashboard |

Profile 1 (`gabagool`) runtime configuration is set in `deploy/polymarket-bot-p1-gabagool.service`.
To change either profile, edit the relevant service file, commit, push, and redeploy.

---

## 8. Monitoring & Troubleshooting

### Health check

```bash
# On the instance:
/opt/polymarket-bot/.venv/bin/python main.py health
```

### Stats

```bash
/opt/polymarket-bot/.venv/bin/python main.py stats
```

### Common log searches

```bash
# Recent tick size changes (sweep signal)
grep "TICK_SIZE" /opt/polymarket-bot/data/bot_p2.log | tail -20

# Strategy decisions
grep "POST_EXPIRY" /opt/polymarket-bot/data/bot_p2.log | tail -20

# Order submissions
grep "ORDER" /opt/polymarket-bot/data/bot_p2.log | tail -20

# Errors
grep -iE "error|exception|traceback" /opt/polymarket-bot/data/bot_p2.log | tail -20
```

### Instance terminated unexpectedly

If the instance is terminated (e.g. spot reclamation — now disabled by default):

1. Run `terraform apply` to create a new instance.
2. Litestream will auto-restore both databases from S3 during bootstrap.
3. The bot will resume trading with full order/fill history intact.

---

## 9. Terraform Variables

Customize the deployment by overriding variables in `terraform/terraform.tfvars`:

```hcl
aws_region           = "eu-west-1"
instance_type        = "t4g.nano"
use_spot_instance    = false      # true saves ~60% but risks termination
ssm_parameter_prefix = "/polymarket-bot/"
log_retention_days   = 30
```

Or pass them inline:

```bash
terraform apply -var="instance_type=t4g.micro"
```

---

## 10. Full Teardown

To remove all AWS resources:

```bash
cd terraform
terraform destroy
```

This destroys the instance, VPC, S3 bucket (including all backups), and IAM resources. The SSM parameters are **not** managed by Terraform and must be deleted manually if needed.
