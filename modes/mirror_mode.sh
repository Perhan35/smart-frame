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

# Suppress X11 keyboard warnings (clipping keycodes) which are noisy on Wayland/XWayland
export XKB_LOG_LEVEL=0

# Support running from SSH or systemd by auto-detecting display environment
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    if [ -n "$(pgrep -x labwc)" ] || [ -n "$(pgrep -x wayfire)" ]; then
        # Default to wayland-0 or wayland-1 which is common on Pi OS Bookworm
        export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
        if [ ! -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]; then
            export WAYLAND_DISPLAY="wayland-1"
        fi
        export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    elif [ -S "/tmp/.X11-unix/X0" ]; then
        export DISPLAY=":0"
    fi
fi

if [ -n "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    # Disable the screen saver (X11)
    xset s noblank 2>/dev/null || true
    xset s off 2>/dev/null || true
    xset -dpms 2>/dev/null || true
fi

# Browser selection: Prefer Cog (ultra-lightweight WPE) then Chromium
if command -v cog &> /dev/null; then
    BROWSER_TYPE="cog"
    BROWSER_CMD="cog -f --bg-color=black"
elif command -v chromium-browser &> /dev/null; then
    BROWSER_TYPE="chromium"
    BROWSER_CMD="chromium-browser"
elif command -v chromium &> /dev/null; then
    BROWSER_TYPE="chromium"
    BROWSER_CMD="chromium"
else
    echo "Error: neither cog nor chromium found."
    sleep 5
    exit 1
fi

echo "Browser selected: $BROWSER_TYPE"

# Performance and suppression flags for Chromium (only applied if using Chromium)
if [ "$BROWSER_TYPE" = "chromium" ]; then
    # --disable-features:
    # OnDeviceModel,OptimizationGuideModelExecution: Stops Chromium from trying to load AI models (fixes "on_device_model service disconnect" error)
    # WebGPU,SkiaGraphite: Disables high-end GPU features that cause warnings on Pi
    # Translate,OptimizationHints,MediaRouter: Removes unnecessary background services
    CHROME_FLAGS="--noerrdialogs --disable-infobars --kiosk --check-for-update-interval=31536000 --disable-dev-shm-usage --no-memcheck --enable-low-end-device-mode --disable-site-isolation-trials --test-type --no-pings --disable-notifications --disable-sync --disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,PrintPreview,OnDeviceModel,OptimizationGuideModelExecution,WebGPU,SkiaGraphite --disable-gpu --user-data-dir=/tmp/chromium_mirror"
    FULL_CMD="$BROWSER_CMD $CHROME_FLAGS"
else
    # Cog specific setup 
    FULL_CMD="$BROWSER_CMD"
fi

# Standardizing for Wayland (preferred on Debian Trixie)
if [ -n "$WAYLAND_DISPLAY" ]; then
    if [ "$BROWSER_TYPE" = "cog" ]; then
        $FULL_CMD "$MIRROR_URL" &> /dev/null &
    else
        $FULL_CMD --ozone-platform=wayland "$MIRROR_URL" &> /dev/null &
    fi
    PID=$!
elif [ -n "$DISPLAY" ] && [ "$BROWSER_TYPE" = "chromium" ]; then
    $FULL_CMD "$MIRROR_URL" &> /dev/null &
    PID=$!
elif command -v labwc &> /dev/null; then
    # No display found, use labwc to launch the browser on the physical screen (KMS)
    echo "No desktop session found. Launching via labwc (Wayland KMS)..."
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    
    # Labwc will run then exit when the browser finishes
    if [ "$BROWSER_TYPE" = "cog" ]; then
        labwc -s "$FULL_CMD $MIRROR_URL" &> /dev/null &
    else
        labwc -s "$FULL_CMD --ozone-platform=wayland $MIRROR_URL" &> /dev/null &
    fi
    PID=$!
else
    echo "Error: No Wayland/X11 display found and labwc is not installed."
    echo "Please run: sudo apt install labwc"
    exit 1
fi

# Wait for Chromium to be killed by the parent Python script
wait $PID
