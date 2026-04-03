#!/bin/bash
set -e

echo "=== SmartFrame Raspberry Pi Setup ==="

# Navigate to the project root (parent of scripts/)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR/.."

# Install system dependencies required for building Python packages and running the display
echo "== Installing system dependencies... =="
sudo apt update
sudo apt install -y python3-venv python3-dev portaudio19-dev libsdl2-dev \
    libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev chromium x11-xserver-utils \
    labwc wlr-randr wayland-utils seatd cog unclutter-xfixes cec-utils ddcutil

# Add user to required groups for hardware access (KMS, Audio, Seat management, I2C)
echo "== Configuring user permissions... =="
for grp in video render seat i2c; do
    if getent group $grp >/dev/null; then
        echo "Adding $USER to $grp group..."
        sudo usermod -a -G $grp $USER
    else
        echo "Group $grp does not exist, skipping."
    fi
done

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "== Creating virtual environment... =="
    python3 -m venv .venv
else
    echo ".venv already exists, skipping."
fi

# Activate and install dependencies
echo "== Installing Python dependencies... =="
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt

# Install post-merge git hook
echo "== Installing git post-merge hook... =="
HOOK_PATH="$(git rev-parse --git-dir)/hooks/post-merge"
cp "$DIR/post-merge.hook" "$HOOK_PATH"
chmod +x "$HOOK_PATH"
echo "Git hook installed."

# Create config.yaml from example if it doesn't exist
if [ ! -f "config.yaml" ]; then
    echo "Creating config.yaml from config.example.yaml..."
    cp config.example.yaml config.yaml
    echo ">>> Please edit config.yaml with your settings before running SmartFrame."
else
    echo "config.yaml already exists, skipping."
fi

echo ""
echo "== Audio Input Configuration =="
echo "SmartFrame can automatically discover your I2S microphone during its first run."
echo "However, if you have multiple microphones or a complex setup, you can select one manually now."
read -rp "Would you like to manually select your audio input device? [y/N] (Default is No) " audio_choice
if [[ "$audio_choice" =~ ^[Yy]$ ]]; then
    python3 scripts/list_audio_devices.py || echo "Warning: Audio device selection failed."
else
    echo "Using Auto-Discovery. SmartFrame will detect the best device on startup."
fi

echo ""
echo "=== Setup complete ==="

# Check if config.yaml still contains placeholder values
if grep -q '\[MQTT_SERVER_IP_ADDRESS\]' config.yaml; then
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  WARNING: config.yaml has not been configured yet!          ║"
    echo "║                                                             ║"
    echo "║  Please edit config.yaml and fill in your settings          ║"
    echo "║  (MQTT broker, MagicMirror URL, etc.) before installing     ║"
    echo "║  the systemd service.                                       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""
    echo "You can edit it now with:  nano config.yaml"
    echo ""
    read -rp "Press Enter once you've configured config.yaml (or Ctrl+C to quit)... "

    # Re-check after the user had a chance to edit
    if grep -q '\[MQTT_SERVER_IP_ADDRESS\]' config.yaml; then
        echo ""
        echo "config.yaml still contains placeholder values. Skipping service installation."
        echo "Once configured, run: ./scripts/install_service.sh"
        echo ""
    else
        echo ""
        read -rp "Would you like to install the SmartFrame systemd service (start on boot)? [y/N] " answer
        if [[ "$answer" =~ ^[Yy]$ ]]; then
            "$DIR/install_service.sh"
        else
            echo "Skipped service installation. You can install it later with: ./scripts/install_service.sh"
        fi
    fi
else
    # Prompt to install the systemd service
    read -rp "Would you like to install the SmartFrame systemd service (start on boot)? [y/N] " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        "$DIR/install_service.sh"
    else
        echo "Skipped service installation. You can install it later with: ./scripts/install_service.sh"
    fi
fi

echo ""
echo "!!! IMPORTANT !!!"
echo "If this is your first time running setup, you MUST REBOOT for group"
echo "permissions (video/render) to take effect:"
echo "sudo reboot"
echo ""
