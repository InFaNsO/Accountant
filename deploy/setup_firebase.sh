#!/bin/bash
# Run this on the production server after downloading Firebase credentials.
# Usage: bash deploy/setup_firebase.sh <path-to-service-account.json>
#
# Example:
#   scp firebase-credentials.json ledger@<server-ip>:/tmp/
#   ssh ledger@<server-ip>
#   cd /home/ledger/app
#   bash deploy/setup_firebase.sh /tmp/firebase-credentials.json

set -e

CREDS_SRC="${1:-}"
APP_DIR="/home/ledger/app"
CREDS_DEST="$APP_DIR/firebase-credentials.json"
ENV_FILE="$APP_DIR/.env"

if [ -z "$CREDS_SRC" ]; then
    echo "Usage: $0 <path-to-firebase-service-account.json>"
    exit 1
fi

if [ ! -f "$CREDS_SRC" ]; then
    echo "File not found: $CREDS_SRC"
    exit 1
fi

echo "==> Copying credentials..."
cp "$CREDS_SRC" "$CREDS_DEST"
chmod 600 "$CREDS_DEST"
echo "    Saved to $CREDS_DEST"

echo "==> Adding FIREBASE_CREDENTIALS_PATH to .env..."
if grep -q "FIREBASE_CREDENTIALS_PATH" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|FIREBASE_CREDENTIALS_PATH=.*|FIREBASE_CREDENTIALS_PATH=$CREDS_DEST|" "$ENV_FILE"
    echo "    Updated existing entry."
else
    echo "FIREBASE_CREDENTIALS_PATH=$CREDS_DEST" >> "$ENV_FILE"
    echo "    Added new entry."
fi

echo "==> Installing new Python dependencies..."
source "$APP_DIR/venv/bin/activate"
pip install firebase-admin APScheduler --quiet
echo "    Done."

echo "==> Restarting ledger service..."
sudo systemctl restart ledger
sleep 3
sudo systemctl status ledger --no-pager | head -10

echo ""
echo "Firebase setup complete."
echo "Test it: journalctl -u ledger -n 30 --no-pager | grep -i firebase"
