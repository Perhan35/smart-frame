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
    labwc wlr-randr wayland-utils seatd cog unclutter-xfixes cec-utils ddcutil \
    fonts-noto-color-emoji

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

# Hardware access without sudo at runtime (i2c-dev, framebuffer) + config.yaml permissions
"$DIR/setup_hardware_access.sh"

# Memory tuning for the Pi Zero 2 (gpu_mem, zram, swappiness): see scripts/setup_memory.sh
echo ""
echo "== Memory tuning (recommended on a Pi Zero 2) =="
echo "Gives the GPU memory that SmartFrame does not use back to Linux (gpu_mem=32),"
echo "and adds zram, a fast compressed swap in RAM, used before the swap on the SD card."
read -rp "Apply the memory tuning? [Y/n] " mem_answer
if [[ "$mem_answer" =~ ^[Nn]$ ]]; then
    echo "Skipped. You can run it later with: ./scripts/setup_memory.sh"
else
    "$DIR/setup_memory.sh"
fi

# Optional: skip cloud-init at boot. On recent Raspberry Pi OS images it applies the
# Raspberry Pi Imager settings (user, Wi-Fi, hostname) at the first boot, then has
# nothing left to do but still runs at every boot.
if [ -d /etc/cloud ] && [ ! -f /etc/cloud/cloud-init.disabled ] \
        && cloud-init status 2>/dev/null | grep -q "status: done"; then
    echo ""
    echo "== cloud-init (optional) =="
    echo "cloud-init has already applied your Raspberry Pi Imager settings. Disabling it saves"
    echo "about 5 s at each boot. The Wi-Fi and settings already saved on the Pi keep working,"
    echo "but later edits to user-data / network-config on the boot partition are no longer applied."
    read -rp "Disable cloud-init? [y/N] " cloud_answer
    if [[ "$cloud_answer" =~ ^[Yy]$ ]]; then
        sudo touch /etc/cloud/cloud-init.disabled
        echo "cloud-init disabled (undo: sudo rm /etc/cloud/cloud-init.disabled)."
    fi
fi

# Optional: keep the system journal across reboots. Raspberry Pi OS keeps it in RAM
# only, so the logs of a freeze or crash are gone once the Pi is restarted.
JOURNAL_CONF=/etc/systemd/journald.conf.d/90-smartframe-persistent.conf
if [ ! -f "$JOURNAL_CONF" ]; then
    echo ""
    echo "== Persistent journal (optional) =="
    echo "Keeps the logs on the SD card (50 MB max), so that after a freeze and a reboot you can"
    echo "still read what happened (journalctl -b -1). Costs a few more writes to the SD card."
    read -rp "Keep the journal across reboots? [y/N] " journal_answer
    if [[ "$journal_answer" =~ ^[Yy]$ ]]; then
        sudo mkdir -p /etc/systemd/journald.conf.d
        printf '%s\n' \
            "# SmartFrame (scripts/setup_pi.sh): keep the journal across reboots" \
            "# (overrides Raspberry Pi OS's volatile, RAM-only journal)" \
            "[Journal]" "Storage=persistent" "SystemMaxUse=50M" "SyncIntervalSec=15s" \
            | sudo tee "$JOURNAL_CONF" >/dev/null
        sudo systemctl restart systemd-journald
        echo "Persistent journal enabled (undo: sudo rm $JOURNAL_CONF)."
    fi
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
read -rp "Would you like to install the custom Smart Frame MOTD? [y/N] " motd_answer
if [[ "$motd_answer" =~ ^[Yy]$ ]]; then
    echo "== Installing custom MOTD... =="
    # Remove default MOTD
    sudo rm -f /etc/motd
    # Create the directory if it doesn't exist
    sudo mkdir -p /etc/update-motd.d
    # Copy our script to the update-motd.d directory
    sudo cp scripts/motd.sh /etc/update-motd.d/50-smartframe
    sudo chmod +x /etc/update-motd.d/50-smartframe
    # Ensure PrintMotd is enabled in SSH (sometimes people disable it)
    # if grep -q "PrintMotd" /etc/ssh/sshd_config; then
    #     sudo sed -i 's/PrintMotd .*/PrintMotd yes/' /etc/ssh/sshd_config
    # else
    #     echo "PrintMotd yes" | sudo tee -a /etc/ssh/sshd_config
    # fi
    echo "Custom MOTD installed. It will appear on your next login!"
fi

echo ""
echo "!!! IMPORTANT !!!"
echo "If this is your first time running setup, you MUST REBOOT for group"
echo "permissions (video/render) to take effect:"
echo "sudo reboot"
echo ""
