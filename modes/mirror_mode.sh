#!/bin/bash
# Mirror Mode
# Launcher script for Chromium in Kiosk mode to display the MagicMirror

echo "Starting Magic Mirror Mode..."

# Find the URL value in config.yaml (from the parent directory)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
CONFIG_FILE="$DIR/../config.yaml"

if [ -f "$CONFIG_FILE" ]; then
    # Use python to robustly parse the YAML since we already have PyYAML installed
    MIRROR_URL=$(python3 -c "import yaml; print(yaml.safe_load(open('$CONFIG_FILE')).get('magic_mirror', {}).get('url', ''))" 2>/dev/null)
fi

if [ -z "$MIRROR_URL" ]; then
    MIRROR_URL="http://localhost:8080"
fi

echo "Connecting to MagicMirror on: $MIRROR_URL"

if [ -n "$DISPLAY" ]; then
    # Disable the screen saver (X11)
    xset s noblank
    xset s off
    xset -dpms
fi

# Launch the task in the background and get the PID
if command -v chromium-browser &> /dev/null; then
    CHROMIUM_CMD="chromium-browser"
elif command -v chromium &> /dev/null; then
    CHROMIUM_CMD="chromium"
else
    echo "Warning: neither chromium nor chromium-browser not found. MagicMirror mode requires it."
    # Sleep so the script doesn't exit immediately and fall back gracefully
    sleep infinity
fi

$CHROMIUM_CMD \
  --noerrdialogs \
  --disable-infobars \
  --kiosk \
  --check-for-update-interval=31536000 \
  "$MIRROR_URL" &

CHROMIUM_PID=$!

# Wait for Chromium to be killed by the parent Python script
wait $CHROMIUM_PID
