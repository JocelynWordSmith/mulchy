#!/bin/bash
# Mulchy installer for Raspberry Pi 3B
# Run once as pi user: bash install.sh

set -e
INSTALL_DIR="$HOME/mulchy"

echo "=== Mulchy Setup ==="

# ── Dependencies ──────────────────────────────────────────────────────────────
echo "Installing Python packages..."
pip install --break-system-packages \
    picamera2 \
    sounddevice \
    scipy \
    numpy \
    flask \
    python-dotenv

# Enable audio output on 3.5mm jack (not HDMI)
# Force audio to headphone jack
echo "Configuring audio output to 3.5mm jack..."
sudo amixer cset numid=3 1   # 0=auto, 1=headphones, 2=hdmi
# Persist across reboots
if ! grep -q "dtparam=audio=on" /boot/config.txt; then
    echo "dtparam=audio=on" | sudo tee -a /boot/config.txt
fi

# ── Env file ──────────────────────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "Creating .env from .env.example — set your WIFI_PASSWORD inside"
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi

# ── Systemd service ───────────────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/mulchy.service"
echo "Creating systemd service at $SERVICE_FILE..."

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
ExecStart=/usr/bin/python3 $INSTALL_DIR/main.py
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
echo "Run manually: python3 $INSTALL_DIR/main.py"
echo "Try a preset: python3 $INSTALL_DIR/main.py --preset glitchy"
