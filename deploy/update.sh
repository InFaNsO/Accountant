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

echo "[deploy] Restarting services..."
sudo systemctl restart ledger
sudo systemctl restart ledger-mcp

echo "[deploy] Done — $(date)"
systemctl is-active ledger && echo "[deploy] ledger: RUNNING" || echo "[deploy] WARNING: ledger not running"
systemctl is-active ledger-mcp && echo "[deploy] ledger-mcp: RUNNING" || echo "[deploy] WARNING: ledger-mcp not running"
