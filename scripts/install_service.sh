#!/bin/bash
set -e

echo "=== SmartFrame Service Installer ==="

# Navigate to the project root (parent of scripts/)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR/.."

# Automatically determine working directory and service user
WORKING_DIR="$(pwd)"
SERVICE_USER="$USER"

echo "Working directory: $WORKING_DIR"
echo "Service user:      $SERVICE_USER"

# Generate the service file from the template
sed -e "s|{{WORKING_DIRECTORY}}|${WORKING_DIR}|g" \
    -e "s|{{USER}}|${SERVICE_USER}|g" \
    scripts/smartframe.service > /tmp/smartframe.service

echo ""
echo "Generated service file:"
echo "---"
cat /tmp/smartframe.service
echo "---"
echo ""

# Check if service already exists
ACTION="installed"
if [ -f "/etc/systemd/system/smartframe.service" ]; then
    echo "Existing service found. Updating..."
    ACTION="updated"
fi

# Install or update the service
sudo cp /tmp/smartframe.service /etc/systemd/system/smartframe.service
rm /tmp/smartframe.service

sudo systemctl daemon-reload
sudo systemctl enable smartframe.service
sudo systemctl restart smartframe.service

echo "=== Service ${ACTION} and started ==="
echo "Check status with: sudo systemctl status smartframe.service"
