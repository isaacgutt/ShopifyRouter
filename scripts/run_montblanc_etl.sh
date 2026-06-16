#!/usr/bin/env bash
# Montblanc ETL runner — called by cron every 30 minutes
LOG=/var/log/montblanc_etl.log
exec >> "$LOG" 2>&1
echo ""
echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---"
cd /var/www/shopify-router
set -a; [ -f .env ] && source .env; set +a
cd scripts
python3 montblanc_etl.py
