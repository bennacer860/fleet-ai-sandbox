#!/bin/bash
set -e

# Redirect all output to a log file for debugging
exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1

echo "Starting Polymarket Bot Bootstrap..."

# 1. Install dependencies
dnf update -y
dnf install -y git python3.11 python3.11-pip tmux jq

# 2. Install Litestream (ARM64)
echo "Installing Litestream..."
curl -L https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-arm64.tar.gz -o litestream.tar.gz
tar -xzf litestream.tar.gz -C /usr/local/bin/
rm litestream.tar.gz

# 3. Setup application directory
APP_DIR="/opt/polymarket-bot"
mkdir -p $APP_DIR
chown ec2-user:ec2-user $APP_DIR

# 4. Clone repository
echo "Cloning repository..."
sudo -u ec2-user git clone -b feature/aws-deployment https://github.com/bennacer860/polymarket-bot-sample.git $APP_DIR

# 5. Fetch secrets from SSM Parameter Store and create .env
echo "Fetching secrets from SSM..."
touch $APP_DIR/.env
aws ssm get-parameters-by-path \
  --path "${ssm_prefix}" \
  --with-decryption \
  --region "${region}" \
  --query "Parameters[*].[Name,Value]" \
  --output text | while read -r name value; do
    # Extract the key name from the path (e.g., /polymarket-bot/PRIVATE_KEY -> PRIVATE_KEY)
    key=$(basename "$name")
    echo "$key=$value" >> $APP_DIR/.env
done

chown ec2-user:ec2-user $APP_DIR/.env
chmod 600 $APP_DIR/.env

# 6. Setup Python virtual environment
echo "Setting up Python venv..."
cd $APP_DIR
sudo -u ec2-user python3.11 -m venv .venv
sudo -u ec2-user .venv/bin/pip install -r requirements.txt

# 7. Copy systemd services and litestream config
echo "Configuring services..."
cp $APP_DIR/deploy/polymarket-bot.service /etc/systemd/system/
cp $APP_DIR/deploy/polymarket-bot-p1-gabagool.service /etc/systemd/system/
cp $APP_DIR/deploy/litestream.service /etc/systemd/system/
mkdir -p /etc/litestream
cp $APP_DIR/deploy/litestream.yml /etc/litestream/litestream.yml

# Replace placeholders in litestream config
sed -i "s/\$${S3_BUCKET}/${s3_bucket}/g" /etc/litestream/litestream.yml

# 7b. Log archival to S3 (hourly timer)
echo "Configuring log archival..."
cp $APP_DIR/deploy/log-sync.service /etc/systemd/system/
cp $APP_DIR/deploy/log-sync.timer /etc/systemd/system/
sed -i "s/\$${S3_BUCKET}/${s3_bucket}/g" /etc/systemd/system/log-sync.service
sed -i "s/\$${REGION}/${region}/g" /etc/systemd/system/log-sync.service

# 8. Restore DBs from Litestream (if exists)
echo "Attempting to restore databases from S3..."
mkdir -p $APP_DIR/data
chown ec2-user:ec2-user $APP_DIR/data
sudo -u ec2-user litestream restore -config /etc/litestream/litestream.yml -if-replica-exists $APP_DIR/data/bot.db || echo "No existing replica found for bot.db."
sudo -u ec2-user litestream restore -config /etc/litestream/litestream.yml -if-replica-exists $APP_DIR/data/bot_p1.db || echo "No existing replica found for bot_p1.db."
sudo -u ec2-user litestream restore -config /etc/litestream/litestream.yml -if-replica-exists $APP_DIR/data/bot_p2.db || echo "No existing replica found for bot_p2.db."

# 9. Enable and start services
echo "Starting services..."
systemctl daemon-reload
systemctl enable litestream
systemctl start litestream
systemctl enable log-sync.timer
systemctl start log-sync.timer
systemctl enable polymarket-bot
systemctl start polymarket-bot
systemctl enable polymarket-bot-p1-gabagool
systemctl start polymarket-bot-p1-gabagool

echo "Bootstrap complete!"
