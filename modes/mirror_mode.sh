#!/bin/bash
# Mirror Mode
# Launcher script for Chromium in Kiosk mode to display the MagicMirror

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Magic Mirror Mode..."

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
export XCURSOR_SIZE=0

# Support running from SSH or systemd by auto-detecting display environment
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    # Try to find a runtime directory
    if [ -z "$XDG_RUNTIME_DIR" ]; then
        export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    fi

    if [ -n "$(pgrep -x labwc)" ] || [ -n "$(pgrep -x wayfire)" ]; then
        # Check if wayland-0 or wayland-1 exists in our runtime dir
        if [ -S "$XDG_RUNTIME_DIR/wayland-0" ]; then
            export WAYLAND_DISPLAY="wayland-0"
        elif [ -S "$XDG_RUNTIME_DIR/wayland-1" ]; then
            export WAYLAND_DISPLAY="wayland-1"
        elif [ "$(id -u)" -eq 0 ] && [ -S "/run/user/1000/wayland-0" ]; then
            # If root, try to use user 1000's session
            export XDG_RUNTIME_DIR="/run/user/1000"
            export WAYLAND_DISPLAY="wayland-0"
        fi
    fi

    if [ -z "$WAYLAND_DISPLAY" ] && [ -S "/tmp/.X11-unix/X0" ]; then
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
    BROWSER_CMD="cog --bg-color=black"
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

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Browser selected: $BROWSER_TYPE"

# Performance and suppression flags for Chromium (only applied if using Chromium)
if [ "$BROWSER_TYPE" = "chromium" ]; then
    # --disable-features:
    # OnDeviceModel,OptimizationGuideModelExecution: Stops Chromium from trying to load AI models (fixes "on_device_model service disconnect" error)
    # WebGPU,SkiaGraphite: Disables high-end GPU features that cause warnings on Pi
    # Translate,OptimizationHints,MediaRouter: Removes unnecessary background services
    CHROME_FLAGS="--noerrdialogs --disable-infobars --kiosk --hide-scrollbars --password-store=basic --check-for-update-interval=31536000 --disable-dev-shm-usage --no-memcheck --enable-low-end-device-mode --disable-site-isolation-trials --test-type --no-pings --disable-notifications --disable-sync --disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,PrintPreview,OnDeviceModel,OptimizationGuideModelExecution,WebGPU,SkiaGraphite --disable-gpu --user-data-dir=/tmp/chromium_mirror"
    FULL_CMD="$BROWSER_CMD $CHROME_FLAGS"
else
    # Cog specific setup 
    export WPE_BACKEND=fdo
    export COG_PLATFORM=fdo
    export COG_PLATFORM_FDO_VIEW_FULLSCREEN=1
    export GDK_BACKEND=wayland
    # Create isolated folders for each session to prevent GLib-GObject critical errors
    export XDG_DATA_HOME="/tmp/cog-data-$USER-$RANDOM"
    export XDG_CACHE_HOME="/tmp/cog-cache-$USER-$RANDOM"
    mkdir -p "$XDG_DATA_HOME" "$XDG_CACHE_HOME"
    # On some Pi versions, this helps with GL/EGL initialization
    export WPE_G_P_R_S_M_ALLOW_FORCE_GL=1
    FULL_CMD="$BROWSER_CMD"
fi

# Run unclutter in the background as a fallback for X11/XWayland cursors
UNCLUTTER_PID=""
if [ -n "$DISPLAY" ]; then
    if command -v unclutter-xfixes &> /dev/null; then
        unclutter-xfixes --idle 0.1 --fork &
        UNCLUTTER_PID=$!
    elif command -v unclutter &> /dev/null; then
        unclutter -idle 0.1 -root &
        UNCLUTTER_PID=$!
    fi
fi

# Standardizing for Wayland (preferred on Debian Trixie)
if [ -n "$WAYLAND_DISPLAY" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Using Wayland display: $WAYLAND_DISPLAY"
    # Show output of wlr-randr for resolution investigation
    if command -v wlr-randr &> /dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Display Info: $(wlr-randr | grep -m 1 'res' | xargs)"
    fi
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        if [ "$BROWSER_TYPE" = "cog" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Cog (Debug) with URL: $MIRROR_URL"
            $FULL_CMD "$MIRROR_URL" 2>&1 | tee /tmp/cog_error.log &
        else
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Chromium (Debug Mode) with URL: $MIRROR_URL"
            $FULL_CMD --ozone-platform=wayland "$MIRROR_URL" &
        fi
    else
        if [ "$BROWSER_TYPE" = "cog" ]; then
            # Always log Cog errors as it is currently being debugged
            $FULL_CMD "$MIRROR_URL" 2>/tmp/cog_error.log &
        else
            $FULL_CMD --ozone-platform=wayland "$MIRROR_URL" &> /dev/null &
        fi
    fi
    PID=$!
elif [ -n "$DISPLAY" ] && [ "$BROWSER_TYPE" = "chromium" ]; then
    echo "Using X11 display: $DISPLAY"
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        $FULL_CMD "$MIRROR_URL" &
    else
        $FULL_CMD "$MIRROR_URL" &> /dev/null &
    fi
    PID=$!
elif command -v labwc &> /dev/null; then
    # No display found, use labwc to launch the browser on the physical screen (KMS)
    echo "No desktop session found. Launching via labwc (Wayland KMS)..."
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    
    # Create a temporary config to hide the cursor in labwc
    LABWC_CONFIG_DIR=$(mktemp -d /tmp/labwc-XXXXXX)
    mkdir -p "$LABWC_CONFIG_DIR/labwc"
    cat <<EOF > "$LABWC_CONFIG_DIR/labwc/rc.xml"
<labwc_config>
  <core>
    <cursor>
      <timeout>1</timeout>
    </cursor>
  </core>
</labwc_config>
EOF

    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        set -x
        # In debug mode, don't hide output from labwc session
        if [ "$BROWSER_TYPE" = "cog" ]; then
            labwc -c "$LABWC_CONFIG_DIR/labwc" -s "$FULL_CMD $MIRROR_URL" &
        else
            labwc -c "$LABWC_CONFIG_DIR/labwc" -s "$FULL_CMD --ozone-platform=wayland $MIRROR_URL" &
        fi
    else
        if [ "$BROWSER_TYPE" = "cog" ]; then
            labwc -c "$LABWC_CONFIG_DIR/labwc" -s "$FULL_CMD $MIRROR_URL" &> /dev/null &
        else
            labwc -c "$LABWC_CONFIG_DIR/labwc" -s "$FULL_CMD --ozone-platform=wayland $MIRROR_URL" &> /dev/null &
        fi
    fi
    PID=$!
else
    echo "Error: No Wayland/X11 display found and labwc is not installed."
    echo "Please run: sudo apt install labwc"
    exit 1
fi

# Cleanup browser, isolated dirs, and unclutter on exit
trap 'echo "Cleaning up..."; kill $PID 2>/dev/null; wait $PID 2>/dev/null; kill $UNCLUTTER_PID 2>/dev/null; rm -rf $LABWC_CONFIG_DIR $XDG_DATA_HOME $XDG_CACHE_HOME' EXIT

# Wait for Chromium to be killed by the parent Python script
wait $PID
