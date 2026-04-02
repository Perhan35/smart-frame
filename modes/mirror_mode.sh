#!/bin/bash
# Mirror Mode
# Launcher script for Chromium in Kiosk mode to display the MagicMirror

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Magic Mirror Mode..."

# Find the URL value in config.yaml (from the parent directory)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
CONFIG_FILE="$DIR/../config.yaml"

if [ -z "$MIRROR_URL" ]; then
    if [ -f "$CONFIG_FILE" ]; then
        # Use python only as a fallback
        MIRROR_URL=$(python3 -c "import yaml; print(yaml.safe_load(open('$CONFIG_FILE')).get('magic_mirror', {}).get('url', ''))" 2>/dev/null)
    fi
fi

if [ -z "$MIRROR_URL" ]; then
    MIRROR_URL="http://localhost:8080"
fi

echo "Connecting to MagicMirror on: $MIRROR_URL"

# Suppress X11 keyboard warnings (clipping keycodes) which are noisy on Wayland/XWayland
export XKB_LOG_LEVEL=0
export XCURSOR_SIZE=0

# Browser selection
if command -v chromium-browser &> /dev/null; then
    BROWSER_TYPE="chromium"
    BROWSER_CMD="chromium-browser"
elif command -v chromium &> /dev/null; then
    BROWSER_TYPE="chromium"
    BROWSER_CMD="chromium"
elif command -v cog &> /dev/null; then
    BROWSER_TYPE="cog"
    BROWSER_CMD="cog --bg-color=black"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Error: neither chromium nor cog found."
    sleep 5
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Browser selected: $BROWSER_TYPE"

# Performance and suppression flags for Chromium
if [ "$BROWSER_TYPE" = "chromium" ]; then
    # Persistent profile directory to enable caching (immensely speeds up subsequent starts)
    PROFILE_DIR="$DIR/../.chromium_profile"
    mkdir -p "$PROFILE_DIR"

    # --no-sandbox: fix "Failed global descriptor lookup" on Pi kiosk
    # --use-gl=egl: Resolve "eglCreateContext ES 3.0 failed" errors by forcing EGL
    # --disable-client-side-phishing-detection: skip the "URL to scan" enterprise check delay
    # --no-first-run --no-default-browser-check: skip initial setup logic
    # --disable-features=...: Thoroughly disabling background bloat, scans, and cloud services (GCM, SafeBrowsing, etc.)
    CHROME_FLAGS="--no-sandbox --noerrdialogs --disable-infobars --kiosk --hide-scrollbars --password-store=basic --check-for-update-interval=31536000 --no-memcheck --enable-low-end-device-mode --disable-site-isolation-trials --test-type --no-pings --disable-notifications --disable-sync --autoplay-policy=no-user-gesture-required --disable-background-networking --disable-component-update --disable-default-apps --disable-domain-reliability --disable-extensions --disable-client-side-phishing-detection --no-first-run --no-default-browser-check --disable-cloud-import --disable-breakpad --metrics-recording-only --disable-gcm-extension --disable-safe-browsing-extension-api --safebrowsing-disable-auto-update --safebrowsing-disable-download-protection --disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,PrintPreview,OnDeviceModel,OptimizationGuideModelExecution,WebGPU,SkiaGraphite,WebRtcHideLocalIpsWithMdns,SafeBrowsing,GCM,OptimizationGuide,EnterpriseDataProtectionAnalysis --enable-gpu-rasterization --enable-zero-copy --use-gl=egl --ignore-certificate-errors --allow-running-insecure-content --remote-allow-origins=* --user-data-dir=$PROFILE_DIR --memory-pressure-thresholds=1,2 --js-flags='--max-old-space-size=128 --stack-size=1024' --disable-smooth-scrolling"

    # Conditional logging for debugging
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        CHROME_FLAGS="$CHROME_FLAGS --enable-logging=stderr --v=1"
    fi
    
    # Priority: Mirror Mode is the primary focus. Lower nice value (higher priority) during startup.
    # We use nice -n 0 (default) or even -5 if we want it to grab resources during load.
    # ionice -c 2 -n 4: Balanced I/O priority.
    LAUNCH_WRAPPER="nice -n 0 ionice -c 2 -n 4"
    FULL_CMD="$LAUNCH_WRAPPER $BROWSER_CMD $CHROME_FLAGS"

else
    # Cog specific setup 
    export WPE_BACKEND=fdo
    export COG_PLATFORM=fdo
    export COG_PLATFORM_FDO_VIEW_FULLSCREEN=1
    export GDK_BACKEND=wayland
    export XDG_DATA_HOME="/tmp/cog-data-$USER-$RANDOM"
    export XDG_CACHE_HOME="/tmp/cog-cache-$USER-$RANDOM"
    mkdir -p "$XDG_DATA_HOME" "$XDG_CACHE_HOME"
    export WPE_G_P_R_S_M_ALLOW_FORCE_GL=1
    
    LAUNCH_WRAPPER="nice -n 15 ionice -c 3"
    FULL_CMD="$LAUNCH_WRAPPER $BROWSER_CMD"
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

# Determine display strategy
if [ -n "$WAYLAND_DISPLAY" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Using Wayland display: $WAYLAND_DISPLAY"
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        if [ "$BROWSER_TYPE" = "cog" ]; then
            WAYLAND_DEBUG=1 $FULL_CMD "$MIRROR_URL" &
        else
            $FULL_CMD --ozone-platform=wayland "$MIRROR_URL" &
        fi
    else
        if [ "$BROWSER_TYPE" = "cog" ]; then
            $FULL_CMD "$MIRROR_URL" 2>/dev/null &
        else
            $FULL_CMD --ozone-platform=wayland "$MIRROR_URL" &> /dev/null &
        fi
    fi
    PID=$!
elif [ -n "$DISPLAY" ]; then
    echo "Using X11 display: $DISPLAY"
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        $FULL_CMD "$MIRROR_URL" &
    else
        $FULL_CMD "$MIRROR_URL" &> /dev/null &
    fi
    PID=$!
else
    echo "Error: No Wayland/X11 display environment found."
    echo "Note: This script is intended to be run within an existing session or wrapped by labwc."
    exit 1
fi

# Cleanup browser, isolated dirs, and unclutter on exit
trap 'echo "Cleaning up..."; kill $PID 2>/dev/null; wait $PID 2>/dev/null; kill $UNCLUTTER_PID 2>/dev/null; rm -rf $LABWC_CONFIG_DIR $XDG_DATA_HOME $XDG_CACHE_HOME' EXIT


# Wait for Chromium to be killed by the parent Python script
wait $PID
