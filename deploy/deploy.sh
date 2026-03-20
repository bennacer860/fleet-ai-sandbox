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
    
    echo "Restarting bot service..."
    sudo systemctl restart polymarket-bot
    
    echo "Deployment complete! The bot is restarting."
    echo "To view the dashboard, connect via SSM and run: tmux attach -t bot"
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
echo "Then type: tmux attach -t bot"
