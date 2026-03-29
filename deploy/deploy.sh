#!/bin/bash
set -e

# Ensure we're running from the project root
cd "$(dirname "$0")/.."

echo "Deploying Polymarket Bot to AWS EC2..."

# Get the instance ID from terraform output
cd terraform
INSTANCE_ID=$(terraform output -raw instance_id)
REGION=$(terraform output -raw aws_region 2>/dev/null || echo "eu-west-1")
cd ..

if [ -z "$INSTANCE_ID" ]; then
    echo "Error: Could not get instance ID from Terraform. Have you run 'terraform apply'?"
    exit 1
fi

echo "Connecting to instance $INSTANCE_ID via SSM to trigger deployment..."

# The commands to run on the remote server
REMOTE_COMMANDS=$(cat << 'EOF'
sudo su - ec2-user -c '
    cd /opt/polymarket-bot
    echo "Pulling latest code..."
    git pull
    
    echo "Updating dependencies..."
    .venv/bin/pip install -r requirements.txt
    
    echo "Installing systemd units..."
    sudo cp deploy/polymarket-bot.service /etc/systemd/system/
    sudo cp deploy/polymarket-bot-p1-gabagool.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable polymarket-bot polymarket-bot-p1-gabagool

    # Clean up any legacy sessions from previous layouts.
    sudo -u ec2-user tmux kill-session -t bot || true
    sudo -u ec2-user tmux -L bot-p2 kill-session -t bot-p2 || true
    sudo -u ec2-user tmux -L bot-p1 kill-session -t bot-p1 || true

    echo "Updating Litestream config..."
    sudo cp deploy/litestream.yml /etc/litestream/litestream.yml
    sudo systemctl restart litestream

    echo "Restarting bot services..."
    sudo systemctl restart polymarket-bot
    sudo systemctl restart polymarket-bot-p1-gabagool
    
    echo "Deployment complete! The bot is restarting."
    echo "To view profile dashboards, connect via SSM and run:"
    echo "  tmux attach -t bot-p2   # post_expiry (profile 2)"
    echo "  tmux attach -t bot-p1   # gabagool (profile 1)"
'
EOF
)

# Execute commands via SSM
aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters "commands=[$REMOTE_COMMANDS]" \
    --region "$REGION" \
    --comment "Deploy Polymarket Bot" \
    --output text

echo "Deployment triggered successfully!"
echo "To connect to the instance and view the dashboard, run:"
echo "aws ssm start-session --target $INSTANCE_ID --region $REGION"
echo "Then type: tmux attach -t bot-p2 (or bot-p1)"
