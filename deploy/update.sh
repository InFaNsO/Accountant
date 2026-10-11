#!/bin/bash
# =============================================================================
# Ledger — Deployment update script
# Called by GitHub Actions on every push to main.
# Run as user 'ledger' on the droplet.
# =============================================================================

set -e

APP_DIR="/home/ledger/app"
cd "$APP_DIR"

# Self-heal: a stray `git` run as root leaves root-owned objects under .git that
# block this pull (which runs as 'ledger') with
#   "error: insufficient permission for adding an object to repository database".
# Repair ownership first. NOPASSWD-allowed for exactly this command in
# deploy/setup.sh; the `|| true` keeps deploys working if the rule is absent.
echo "[deploy] Ensuring repo ownership..."
sudo chown -R ledger:ledger "$APP_DIR" 2>/dev/null || true

echo "[deploy] Pulling latest code..."
# Retry up to 3 times in case of transient GitHub network issues
for _attempt in 1 2 3; do
    git pull origin main && break
    if [ "$_attempt" -eq 3 ]; then
        echo "[deploy] git pull failed after 3 attempts — aborting."
        exit 1
    fi
    echo "[deploy] git pull failed (attempt $_attempt), retrying in 5s..."
    sleep 5
done

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
