#!/bin/bash
set -euo pipefail

LOG=/var/log/inventory_sync.log
SCRIPT_DIR=/var/www/shopify-router/scripts

# Check VPN is up
if ! ip addr show tun0 &>/dev/null; then
    echo "$(date) [ERROR] VPN tun0 not up, skipping sync" >> "$LOG"
    exit 1
fi

cd "$SCRIPT_DIR"
echo "$(date) [INFO] Starting inventory sync" >> "$LOG"
python3 inventory_sync_v2.py >> "$LOG" 2>&1
echo "$(date) [INFO] Sync complete" >> "$LOG"
