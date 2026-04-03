#!/bin/bash
# Mirror Mode
# Launcher script for Chromium in Kiosk mode to display the MagicMirror

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Magic Mirror Mode..."

# Find the URL value in config.yaml (from the parent directory)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
CONFIG_FILE="$DIR/../config.yaml"

if [ -z "$MIRROR_URL" ]; then
    if [ -f "$CONFIG_FILE" ]; then
        # Fast shell-based fallback for simple YAML (avoids 1.5s python overhead on Pi Zero 2)
        MIRROR_URL=$(grep 'url:' "$CONFIG_FILE" | head -n 1 | awk '{print $2}' | tr -d '"'\'' ')
    fi
fi

if [ -z "$MIRROR_URL" ]; then
    MIRROR_URL="http://localhost:8080"
fi

echo "Connecting to MagicMirror on: $MIRROR_URL"

# Suppress X11 keyboard warnings and hide cursors (X11/Wayland)
export XKB_LOG_LEVEL=0
export XCURSOR_SIZE=0
export XCURSOR_THEME=None
export COG_PLATFORM_FDO_SHOW_CURSOR=0

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

    # Detect GPU availability (VideoCore / KMS)
    HAS_GPU=0
    if [ -c /dev/dri/card0 ] || [ -c /dev/dri/renderD128 ]; then
        HAS_GPU=1
    fi

    # Performance and suppression flags for Chromium (Deeply optimized for Pi Zero 2)
    # --no-sandbox: fix "Failed global descriptor lookup" on Pi kiosk
    # --use-gl=egl: standard Wayland/GLES interface for Pi
    # --disable-dev-shm-usage: Fixes memory issues on low-RAM devices
    # --use-mock-keychain: Avoids D-Bus/Identity/OSCrypt overhead and log spam
    CHROME_FLAGS="--no-sandbox --noerrdialogs --disable-infobars --kiosk --hide-scrollbars --password-store=basic --use-mock-keychain --check-for-update-interval=31536000 --no-memcheck --enable-low-end-device-mode --disable-site-isolation-trials --test-type --no-pings --disable-notifications --disable-sync --autoplay-policy=no-user-gesture-required --disable-background-networking --disable-component-update --disable-default-apps --disable-domain-reliability --disable-extensions --disable-client-side-phishing-detection --no-first-run --no-default-browser-check --disable-cloud-import --disable-breakpad --metrics-recording-only --disable-gcm-extension --disable-gcm --disable-safe-browsing-extension-api --safebrowsing-disable-auto-update --safebrowsing-disable-download-protection --disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,PrintPreview,OnDeviceModel,OptimizationGuideModelExecution,WebGPU,SkiaGraphite,WebRtcHideLocalIpsWithMdns,SafeBrowsing,GCM,OptimizationGuide,EnterpriseDataProtectionAnalysis,AudioServiceOutOfProcess,BackForwardCache,IsolateOrigins,SitePerProcess,Vulkan,BatteryStatus,NetworkQualityEstimator,PrivacySandboxSettings4,FedCm,InterestFeedContentSuggestions,SegmentationPlatform,PushMessaging,CloudMessaging --disable-variations-safe-mode --disable-dev-shm-usage --disable-gpu-watchdog --enable-zero-copy --use-gl=egl --disable-software-rasterizer --ignore-certificate-errors --allow-running-insecure-content --remote-allow-origins=* --user-data-dir=$PROFILE_DIR --memory-pressure-thresholds=1,2 --js-flags='--max-old-space-size=128 --stack-size=1024' --disable-smooth-scrolling --mute-audio --force-device-scale-factor=1 --disable-background-timer-throttling --disk-cache-dir=/tmp --disk-cache-size=20971520 --media-cache-size=1 --disable-policy-cloud-management --no-proxy-server --disable-gpu-shader-disk-cache --disable-vulkan"

    # Add hardware acceleration flags only if GPU is present
    if [ "$HAS_GPU" = "1" ]; then
        CHROME_FLAGS="$CHROME_FLAGS --enable-gpu-rasterization --ignore-gpu-blocklist --enable-accelerated-2d-canvas --enable-native-gpu-memory-buffers"
        echo "GPU detected: Enabling hardware acceleration flags (GLES2)."
    fi

    # Conditional logging for debugging
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        CHROME_FLAGS="$CHROME_FLAGS --enable-logging=stderr --v=1"
    fi
    
    # Priority: Mirror Mode is the primary focus. Lower nice value (higher priority) during startup.
    LAUNCH_WRAPPER="nice -n 0 ionice -c 2 -n 4"
    FULL_CMD="$LAUNCH_WRAPPER $BROWSER_CMD $CHROME_FLAGS"

else
    # Cog specific setup 
    COG_PROFILE="$DIR/../.cog_profile"
    mkdir -p "$COG_PROFILE/data" "$COG_PROFILE/cache"
    export XDG_DATA_HOME="$COG_PROFILE/data"
    export XDG_CACHE_HOME="$COG_PROFILE/cache"
    
    # Check GPU for Cog
    if [ -c /dev/dri/card0 ]; then
        export WPE_G_P_R_S_M_ALLOW_FORCE_GL=1
        export WPE_G_P_R_S_M_ALLOW_EGL_GLES=1
    fi
    export COG_PLATFORM_FDO_SHOW_CURSOR=0
    
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
    
    # Kiosk trigger: In Wayland, tell the compositor to hide its cursor once the browser has loaded.
    # This is highly effective for labwc without impacting other modes.
    if command -v labwc-msg &> /dev/null; then
        (sleep 3 && labwc-msg action HideCursor) &
    fi
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

# Cleanup browser and unclutter on exit (do NOT delete persistent profiles)
trap 'echo "Cleaning up..."; kill $PID 2>/dev/null; wait $PID 2>/dev/null; kill $UNCLUTTER_PID 2>/dev/null' EXIT


# Wait for Chromium to be killed by the parent Python script
wait $PID
