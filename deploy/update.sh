#!/bin/bash
# =============================================================================
# Ledger — Deployment update script
# Called by GitHub Actions on every push to main.
# Run as user 'ledger' on the droplet.
# =============================================================================

set -e

APP_DIR="/home/ledger/app"
cd "$APP_DIR"

echo "[deploy] Pulling latest code..."
git pull origin main

echo "[deploy] Installing/updating dependencies..."
source venv/bin/activate
pip install -r requirements.txt --quiet

echo "[deploy] Creating logs directory if missing..."
mkdir -p /home/ledger/app/logs
chown ledger:ledger /home/ledger/app/logs 2>/dev/null || true

echo "[deploy] Restarting service..."
sudo systemctl restart ledger

echo "[deploy] Done — $(date)"
systemctl is-active ledger && echo "[deploy] Service is RUNNING" || echo "[deploy] WARNING: service not running"
