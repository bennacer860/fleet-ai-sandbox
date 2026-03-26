#!/bin/bash
# Archive rotated bot log files to S3, organized by date.
#
# Rotated logs (*.log.1, *.log.2, ...) are compressed, uploaded,
# and removed locally.  The active *.log file is left untouched.
# RotatingFileHandler recreates .1 on the next rotation — no data loss.
#
# Called hourly by the log-sync.timer systemd unit.

set -euo pipefail

APP_DIR="/opt/polymarket-bot"
LOG_DIR="$APP_DIR/data"
S3_BUCKET="${LOG_SYNC_S3_BUCKET:?LOG_SYNC_S3_BUCKET not set}"
REGION="${LOG_SYNC_REGION:-eu-west-1}"

DATE_PREFIX=$(date -u +%Y/%m/%d)
TIMESTAMP=$(date -u +%H%M%S)
uploaded=0

for f in "$LOG_DIR"/*.log.[1-9]*; do
    [ -f "$f" ] || continue
    base=$(basename "$f")

    gzip -c "$f" > "/tmp/${base}.gz"
    aws s3 cp "/tmp/${base}.gz" \
        "s3://${S3_BUCKET}/logs/${DATE_PREFIX}/${base}.${TIMESTAMP}.gz" \
        --region "$REGION" --only-show-errors
    rm -f "/tmp/${base}.gz"

    rm -f "$f"
    uploaded=$((uploaded + 1))
done

if [ "$uploaded" -gt 0 ]; then
    echo "Archived $uploaded rotated log file(s) to s3://${S3_BUCKET}/logs/${DATE_PREFIX}/"
fi
