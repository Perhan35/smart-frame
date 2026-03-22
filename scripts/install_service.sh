#!/bin/bash
set -e

echo "=== SmartFrame Service Installer ==="

# Navigate to the project root (parent of scripts/)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR/.."

# Read values from config.yaml using the venv Python (PyYAML is already installed)
WORKING_DIR=$(.venv/bin/python3 -c "
import yaml
with open('config.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('service', {}).get('working_directory', '/home/pi/smart-frame'))
")

SERVICE_USER=$(.venv/bin/python3 -c "
import yaml
with open('config.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('service', {}).get('user', 'pi'))
")

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

# Install the service
sudo cp /tmp/smartframe.service /etc/systemd/system/smartframe.service
rm /tmp/smartframe.service

sudo systemctl daemon-reload
sudo systemctl enable smartframe.service
sudo systemctl restart smartframe.service

echo "=== Service installed and started ==="
echo "Check status with: sudo systemctl status smartframe.service"
