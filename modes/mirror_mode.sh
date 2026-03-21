#!/bin/bash
# Mirror Mode
# Launcher script for Chromium in Kiosk mode to display the MagicMirror

echo "Starting Magic Mirror Mode..."

# Find the URL value in config.yaml (from the parent directory)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
CONFIG_FILE="$DIR/../config.yaml"

if [ -f "$CONFIG_FILE" ]; then
    # Note: Users could install 'yq' for a cleaner parse, 
    # but grep provides a simple standalone solution
    MIRROR_URL=$(grep -A 1 "magic_mirror:" "$CONFIG_FILE" | grep "url:" | awk -F '"' '{print $2}')
else
    MIRROR_URL="http://localhost:8080"
fi

if [ -z "$MIRROR_URL" ]; then
    MIRROR_URL="http://localhost:8080"
fi

echo "Connecting to MagicMirror on: $MIRROR_URL"

# Disable the screen saver (X11)
xset s noblank
xset s off
xset -dpms

# Launch the task in the background and get the PID
# Using Chromium-browser (default on Raspberry Pi OS with Desktop)
chromium-browser \
  --noerrdialogs \
  --disable-infobars \
  --kiosk \
  --check-for-update-interval=31536000 \
  "$MIRROR_URL" &

CHROMIUM_PID=$!

# Wait for Chromium to be killed by the parent Python script
wait $CHROMIUM_PID
