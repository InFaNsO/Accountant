#!/bin/bash
# =============================================================================
# Ledger — One-time DigitalOcean Droplet Setup Script
# Run as root on a fresh Ubuntu 22.04 droplet:
#   bash setup.sh <your-domain> <github-repo-url> <secret-key>
# Example:
#   bash setup.sh admin.applestreeabrasives.com https://github.com/InFaNsO/Accountant.git mysecretkey123
# =============================================================================

set -e  # Exit immediately on any error

DOMAIN=${1:-"admin.applestreeabrasives.com"}
REPO=${2:-"https://github.com/InFaNsO/Accountant.git"}
SECRET_KEY=${3:-"change-this-to-a-random-secret"}
APP_USER="ledger"
APP_DIR="/home/$APP_USER/app"
PYTHON="python3"

echo ""
echo "========================================"
echo "  Ledger Deployment Setup"
echo "  Domain : $DOMAIN"
echo "  Repo   : $REPO"
echo "========================================"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/9] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    nginx git ufw certbot python3-certbot-nginx \
    curl wget unzip

# ── 2. Create dedicated app user ──────────────────────────────────────────────
echo "[2/9] Creating app user '$APP_USER'..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$APP_USER"
fi
# Allow ledger to restart its own service without a password
echo "$APP_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart ledger, /bin/systemctl start ledger, /bin/systemctl stop ledger" \
    > /etc/sudoers.d/ledger
chmod 440 /etc/sudoers.d/ledger

# ── 3. Set up SSH for the app user (for GitHub Actions deploy) ────────────────
echo "[3/9] Setting up SSH for deployments..."
mkdir -p /home/$APP_USER/.ssh
chmod 700 /home/$APP_USER/.ssh

# Generate a deploy keypair if one doesn't exist
if [ ! -f /home/$APP_USER/.ssh/deploy_key ]; then
    ssh-keygen -t ed25519 -f /home/$APP_USER/.ssh/deploy_key -N "" -C "ledger-deploy"
    cat /home/$APP_USER/.ssh/deploy_key.pub >> /home/$APP_USER/.ssh/authorized_keys
fi
chown -R $APP_USER:$APP_USER /home/$APP_USER/.ssh
chmod 600 /home/$APP_USER/.ssh/authorized_keys

# ── 4. Clone repository ───────────────────────────────────────────────────────
echo "[4/9] Cloning repository..."
if [ -d "$APP_DIR" ]; then
    echo "  Directory exists — pulling latest..."
    cd "$APP_DIR"
    sudo -u $APP_USER git pull origin main
else
    sudo -u $APP_USER git clone "$REPO" "$APP_DIR"
fi

# ── 5. Python virtual environment & dependencies ──────────────────────────────
echo "[5/9] Setting up Python environment..."
cd "$APP_DIR"
sudo -u $APP_USER $PYTHON -m venv venv
sudo -u $APP_USER venv/bin/pip install --upgrade pip --quiet
sudo -u $APP_USER venv/bin/pip install -r requirements.txt --quiet

# Create data and logs directories
sudo -u $APP_USER mkdir -p "$APP_DIR/data"
sudo -u $APP_USER mkdir -p "$APP_DIR/logs"

# ── 6. Environment file ───────────────────────────────────────────────────────
echo "[6/9] Writing environment config..."
cat > "$APP_DIR/.env" <<EOF
SECRET_KEY=$SECRET_KEY
FLASK_ENV=production
EOF
chown $APP_USER:$APP_USER "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# ── 7. Systemd service ────────────────────────────────────────────────────────
echo "[7/9] Installing systemd service..."
cp "$APP_DIR/deploy/ledger.service" /etc/systemd/system/ledger.service
# Inject the secret key into the service
sed -i "s|SECRET_KEY=changeme|SECRET_KEY=$SECRET_KEY|g" /etc/systemd/system/ledger.service
systemctl daemon-reload
systemctl enable ledger
systemctl start ledger
echo "  Service started — checking status..."
sleep 2
systemctl is-active ledger && echo "  ledger service: RUNNING" || echo "  WARNING: service not running, check: journalctl -u ledger"

# ── 8. Nginx ──────────────────────────────────────────────────────────────────
echo "[8/9] Configuring Nginx..."
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/ledger
sed -i "s|DOMAIN_PLACEHOLDER|$DOMAIN|g" /etc/nginx/sites-available/ledger
ln -sf /etc/nginx/sites-available/ledger /etc/nginx/sites-enabled/ledger
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── 9. Firewall & SSL ─────────────────────────────────────────────────────────
echo "[9/9] Configuring firewall and SSL..."
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

echo ""
echo "  Requesting SSL certificate from Let's Encrypt..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
    --email "admin@$(echo $DOMAIN | cut -d. -f2-)" \
    --redirect || echo "  WARNING: SSL setup failed — DNS may not be pointed yet. Run certbot manually after DNS is set."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo "  Setup complete!"
echo ""
echo "  App URL   : https://$DOMAIN"
echo "  App dir   : $APP_DIR"
echo "  Logs      : journalctl -u ledger -f"
echo ""
echo "  IMPORTANT — Add this private key to GitHub Secrets"
echo "  as DO_SSH_KEY (Settings > Secrets > Actions):"
echo "========================================"
echo ""
cat /home/$APP_USER/.ssh/deploy_key
echo ""
echo "  Also add these GitHub Secrets:"
echo "  DO_HOST = $(curl -s ifconfig.me)"
echo "  DO_USER = $APP_USER"
echo ""
