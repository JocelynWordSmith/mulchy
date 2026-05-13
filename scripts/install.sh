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
# picamera2 has to come from apt because its python3-libcamera native dep is
# only packaged that way. libportaudio2 is the C library that sounddevice
# (pure-Python ctypes wrapper, installed into the venv via pyproject.toml)
# loads at runtime.
echo "Installing Pi system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-picamera2 libportaudio2 portaudio19-dev

# ── Python dependencies (via uv) ──────────────────────────────────────────────
# The venv MUST be built from the system Python (Trixie's /usr/bin/python3,
# currently 3.13) AND have system-site-packages enabled — otherwise it can't
# see picamera2 from apt. Without this the service silently falls back to
# the test-pattern source.
echo "Installing Python dependencies via uv..."
cd "$INSTALL_DIR"
rm -rf .venv
uv venv --system-site-packages --python /usr/bin/python3
uv sync

# Sanity check: the venv really needs to be able to import both, or the
# service will start successfully but fall back to test-pattern + silent.
echo "Verifying picamera2 + sounddevice are visible to the venv..."
.venv/bin/python -c "import picamera2; print('  picamera2 OK')"
.venv/bin/python -c "import sounddevice; print('  sounddevice OK')"

# ── Audio output configuration ────────────────────────────────────────────────
# PCM is left at 100% (no headroom lost at the ALSA stage). The real volume
# boost comes from PipeWire via wpctl at service start — see ExecStartPre
# below.
#
# Note: legacy Pi OS installs used `amixer cset numid=3 1` here to force the
# 3.5mm jack route. Trixie exposes HDMI and Headphones as two separate ALSA
# cards instead (no shared numid=3 control), and the legacy command errors
# out with "Host is down". Routing is now PipeWire's job — by default it
# auto-picks the active sink, and if a monitor is plugged into HDMI you can
# override with `wpctl set-default <sink-id-for-Headphones>`.
echo "Configuring audio output..."

# /boot/firmware/config.txt is the Trixie/Bookworm path; /boot/config.txt is
# the legacy Bullseye-and-earlier path. Write to whichever exists.
BOOT_CONFIG=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
    if [ -f "$candidate" ]; then
        BOOT_CONFIG="$candidate"
        break
    fi
done

if [ -n "$BOOT_CONFIG" ]; then
    # dtparam=audio=on enables the audio peripheral at all.
    # audio_pwm_mode=2 swaps the Pi 3's headphone-jack PWM to MASH mode —
    # noticeably better SNR on the analog output, at no runtime cost.
    # disable_audio_dither=1 removes the constant low-level dither hash
    # that otherwise lives under everything coming out of the aux jack.
    for line in "dtparam=audio=on" "audio_pwm_mode=2" "disable_audio_dither=1"; do
        if ! grep -qxF "$line" "$BOOT_CONFIG"; then
            echo "$line" | sudo tee -a "$BOOT_CONFIG" > /dev/null
            echo "  added '$line' to $BOOT_CONFIG (takes effect on reboot)"
        fi
    done
else
    echo "WARNING: no /boot/firmware/config.txt or /boot/config.txt found — skipping aux-jack quality tweaks"
fi

# PipeWire's user session only stays up while the user is logged in unless
# linger is enabled — without it, the service starts before PipeWire and
# `wpctl set-volume` at ExecStartPre time fails silently.
echo "Enabling user-session linger for pi (keeps PipeWire alive across logins)..."
sudo loginctl enable-linger pi 2>/dev/null || \
    echo "  (skipped — likely already enabled, or systemd-logind unavailable)"

# ── Env file ──────────────────────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "Creating .env from .env.example — set your WIFI_PASSWORD inside"
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi

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
# XDG_RUNTIME_DIR is required for wpctl to find the user's PipeWire session
# when invoked from a system service. 1000 is the default UID for the `pi`
# user on Raspberry Pi OS images.
Environment=XDG_RUNTIME_DIR=/run/user/1000
ExecStartPre=/bin/sleep 3
# Keep ALSA's PCM at unity — no headroom thrown away at the hardware stage.
# `-c 1` targets the bcm2835 Headphones card; the default control device on
# Trixie exposes Master/Capture but NOT PCM, so this fails without -c. The
# leading `-` lets the service start anyway if card numbering ever shifts
# (e.g. HDMI not connected → Headphones becomes card 0).
ExecStartPre=-/usr/bin/amixer -c 1 sset PCM 100%
# Software-boost the PipeWire sink to 0.8 (80%). 100% sounds clean on this
# Pi 3B's aux jack but starts distorting past ~0.8 once driven into a
# powered speaker. The leading '-' makes systemd ignore failures so the
# service still comes up if wpctl/PipeWire isn't available.
ExecStartPre=-/usr/bin/wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.8
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
echo "Test pattern: uv run mulchy --source test --no-audio"
