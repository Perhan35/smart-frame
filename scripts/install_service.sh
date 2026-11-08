#!/bin/bash
set -e

echo "=== SmartFrame Service Installer ==="

# Navigate to the project root (parent of scripts/)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR/.."

# Run this script as the normal user (it calls sudo itself). Under "sudo", $USER is
# root and the service would then run as root.
if [ "$(id -u)" -eq 0 ]; then
    if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
        SERVICE_USER="$SUDO_USER"
        echo "Note: run without sudo next time. Installing for user '$SERVICE_USER'."
    else
        echo "Error: do not run this script as root. Run it as the user that owns the frame:" >&2
        echo "  ./scripts/install_service.sh" >&2
        exit 1
    fi
else
    SERVICE_USER="$(id -un)"
fi

WORKING_DIR="$(pwd)"

echo "Working directory: $WORKING_DIR"
echo "Service user:      $SERVICE_USER"

# Generate the service file from the template (private temp file: no fixed /tmp path)
TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT
sed -e "s|{{WORKING_DIRECTORY}}|${WORKING_DIR}|g" \
    -e "s|{{USER}}|${SERVICE_USER}|g" \
    scripts/smartframe.service > "$TMP_UNIT"

echo ""
echo "Generated service file:"
echo "---"
cat "$TMP_UNIT"
echo "---"
echo ""

# Check if service already exists
ACTION="installed"
if [ -f "/etc/systemd/system/smartframe.service" ]; then
    echo "Existing service found. Updating..."
    ACTION="updated"
fi

# Install or update the service
sudo install -m 644 "$TMP_UNIT" /etc/systemd/system/smartframe.service

sudo systemctl daemon-reload
sudo systemctl enable smartframe.service
sudo systemctl restart smartframe.service

echo "=== Service ${ACTION} and started ==="
echo "Check status with: sudo systemctl status smartframe.service"
