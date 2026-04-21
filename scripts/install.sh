#!/bin/bash
# Mulchy installer for Raspberry Pi 3B
# Run once as pi user: bash scripts/install.sh

set -e
INSTALL_DIR="$HOME/mulchy"

echo "=== Mulchy Setup ==="

# ── uv ────────────────────────────────────────────────────────────────────────
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# ── Pi-specific system packages ───────────────────────────────────────────────
# picamera2 requires system libraries that cannot be cleanly resolved via pip.
echo "Installing Pi system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-picamera2

# ── Python dependencies (via uv) ──────────────────────────────────────────────
echo "Installing Python dependencies via uv..."
cd "$INSTALL_DIR"
uv sync

# ── Audio output configuration ────────────────────────────────────────────────
# Bookworm moved config.txt to /boot/firmware; Bullseye and earlier use /boot.
echo "Configuring audio output..."
BOOT_CONFIG=/boot/firmware/config.txt
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG=/boot/config.txt
if [ -f "$BOOT_CONFIG" ] && ! grep -q "dtparam=audio=on" "$BOOT_CONFIG"; then
    echo "dtparam=audio=on" | sudo tee -a "$BOOT_CONFIG"
fi

# ── Env file ──────────────────────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "Creating .env from .env.example — set your WIFI_PASSWORD inside"
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi

# ── Sudoers: let pi user power off via web UI ─────────────────────────────────
SUDOERS_SHUTDOWN="/etc/sudoers.d/mulchy-shutdown"
echo "Installing sudoers entry for shutdown..."
sudo install -m 0440 "$INSTALL_DIR/scripts/sudoers-mulchy-shutdown" "$SUDOERS_SHUTDOWN"

# ── Systemd service ───────────────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/mulchy.service"
echo "Creating systemd service at $SERVICE_FILE..."
UV_BIN="$HOME/.local/bin/uv"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Mulchy - camera to soundscape
After=sound.target NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
User=pi
WorkingDirectory=$INSTALL_DIR
ExecStartPre=/bin/sleep 3
ExecStartPre=/usr/bin/amixer -c 1 sset PCM 100%
ExecStart=$UV_BIN run --directory $INSTALL_DIR mulchy
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mulchy.service

echo ""
echo "=== Done ==="
echo "Edit $INSTALL_DIR/.env to set your WIFI_PASSWORD"
echo ""
echo "Start now:    sudo systemctl start mulchy"
echo "Stop:         sudo systemctl stop mulchy"
echo "Logs:         journalctl -u mulchy -f"
echo "Run manually: uv run mulchy"
echo "Test pattern: uv run mulchy --source test"
