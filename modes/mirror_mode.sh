#!/bin/bash
# Mirror Mode
# Launcher script for kiosk browser to display the MagicMirror
# Preferred: Cog (WPE WebKit) — lightweight, designed for embedded kiosk on Pi Zero 2
# Fallback:  Chromium — heavier, requires --single-process + SW rendering on Pi Zero 2

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

# Browser selection: prefer Cog (lightweight), fall back to Chromium
if command -v cog &> /dev/null; then
    BROWSER_TYPE="cog"
    BROWSER_CMD="cog"
elif command -v chromium-browser &> /dev/null; then
    BROWSER_TYPE="chromium"
    BROWSER_CMD="chromium-browser"
elif command -v chromium &> /dev/null; then
    BROWSER_TYPE="chromium"
    BROWSER_CMD="chromium"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Error: neither cog nor chromium found."
    sleep 5
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Browser selected: $BROWSER_TYPE"

# ──────────────────────────────────────────────
#  Cog (WPE WebKit) — primary browser
# ──────────────────────────────────────────────
if [ "$BROWSER_TYPE" = "cog" ]; then
    COG_PROFILE="$DIR/../.cog_profile"
    mkdir -p "$COG_PROFILE/data" "$COG_PROFILE/cache"

    # Persistent cache & data directories
    export XDG_DATA_HOME="$COG_PROFILE/data"
    export XDG_CACHE_HOME="$COG_PROFILE/cache"

    # Wayland fullscreen kiosk
    export COG_PLATFORM_WL_VIEW_FULLSCREEN=1

    # Pi Zero 2 rendering optimisation:
    # vc4 GPU supports GLES 2.0 which WPE WebKit can use (unlike Chromium ANGLE which needs 3.0).
    # Try GPU-accelerated rendering first; set CPU fallback env var if GPU causes issues.
    if [ -c /dev/dri/card0 ]; then
        # GPU available — let WPE use hardware acceleration
        export WEBKIT_EGL_PIXEL_FORMAT=RGB565   # 16-bit framebuffer saves ~50% VRAM bandwidth
    else
        # No GPU — force CPU rendering
        export WEBKIT_SKIA_ENABLE_CPU_RENDERING=1
    fi

    # Limit painting threads to avoid overloading the 4-core 1GHz CPU
    export WEBKIT_SKIA_CPU_PAINTING_THREADS=2

    # Cog flags:
    #   --platform=wl          Wayland backend (labwc)
    #   --webprocess-failure=restart  Auto-restart on WebProcess crash (up to 5 retries)
    #   --bg-color=black       Black background while loading (matches kiosk aesthetic)
    #   --enable-page-cache=false  Disable in-memory back/forward cache to save RAM on 512MB device
    COG_FLAGS="--platform=wl --webprocess-failure=restart --bg-color=black --enable-page-cache=false"

    # Conditional logging for debugging (route WebKit/WPE messages to stderr)
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        export G_MESSAGES_DEBUG=all
        export WEBKIT_DEBUG=Loading,Network,Process
    fi

    LAUNCH_WRAPPER="nice -n 0 ionice -c 2 -n 4"
    FULL_CMD="$LAUNCH_WRAPPER $BROWSER_CMD $COG_FLAGS"

# ──────────────────────────────────────────────
#  Chromium — fallback browser
# ──────────────────────────────────────────────
elif [ "$BROWSER_TYPE" = "chromium" ]; then
    # Persistent profile + disk cache directory (survives reboots for fast subsequent starts)
    PROFILE_DIR="$DIR/../.chromium_profile"
    CACHE_DIR="$PROFILE_DIR/DiskCache"
    mkdir -p "$PROFILE_DIR" "$CACHE_DIR"
    # Purge stale GPU caches only (NOT the disk/HTTP cache — that must persist for performance)
    rm -rf "$PROFILE_DIR/GPUCache" "$PROFILE_DIR/ShaderCache" "$PROFILE_DIR/GrShaderCache" 2>/dev/null

    # Pi Zero 2 WH: vc4 GPU only supports GLES 2.0 but Chromium ANGLE requires GLES 3.0.
    # No hardware GL backend works, so we use CPU-based Skia software rendering (--disable-gpu).
    #
    # Key flags:
    # --disable-gpu: Skip GPU/ANGLE entirely, use Skia software rasterizer (mandatory for vc4)
    # --disable-gpu-compositing: All compositing on CPU (avoids fallback GL attempts)
    # --single-process: run renderer/network/utilities in one process — eliminates
    #   "Failed global descriptor lookup" shared memory bug on Pi Zero 2 (white screen fix)
    # --no-sandbox: required alongside --single-process on Pi kiosk
    # --disable-dev-shm-usage: Fixes memory issues on low-RAM devices
    # --use-mock-keychain: Avoids D-Bus/Identity/OSCrypt overhead and log spam
    # --disk-cache-dir=$CACHE_DIR: Persistent cache (NOT /tmp which is wiped on reboot)
    # --disable-field-trial-config + --disable-crash-reporter: kill all Google phoning-home
    CHROME_FLAGS="--single-process --no-sandbox --noerrdialogs --disable-infobars --kiosk --hide-scrollbars --no-memcheck \
--password-store=basic --use-mock-keychain --check-for-update-interval=31536000 \
--enable-low-end-device-mode --disable-site-isolation-trials --test-type --no-pings \
--disable-notifications --disable-sync --autoplay-policy=no-user-gesture-required \
--disable-background-networking --disable-component-update --disable-default-apps \
--disable-domain-reliability --disable-extensions --disable-client-side-phishing-detection \
--no-first-run --no-default-browser-check --disable-breakpad --disable-crash-reporter \
--disable-field-trial-config --disable-variations-safe-mode \
--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,PrintPreview,OnDeviceModel,OptimizationGuideModelExecution,WebGPU,SkiaGraphite,WebRtcHideLocalIpsWithMdns,SafeBrowsing,GCM,OptimizationGuide,EnterpriseDataProtectionAnalysis,AudioServiceOutOfProcess,BackForwardCache,IsolateOrigins,SitePerProcess,Vulkan,BatteryStatus,NetworkQualityEstimator,PrivacySandboxSettings4,FedCm,InterestFeedContentSuggestions,SegmentationPlatform,PushMessaging,CloudMessaging,Reporting,AutofillServerCommunication,SigninInterception,ChromeBrowserCloudManagement,WebBluetooth,WebBluetoothScanning \
--disable-dev-shm-usage \
--disable-gpu --disable-gpu-compositing \
--num-raster-threads=2 --renderer-process-limit=1 \
--disable-session-crashed-bubble --hide-crash-restore-bubble \
--ignore-certificate-errors --allow-running-insecure-content --remote-allow-origins=* \
--user-data-dir=$PROFILE_DIR \
--memory-pressure-thresholds=1,2 --js-flags='--max-old-space-size=128 --stack-size=1024' \
--disable-smooth-scrolling --mute-audio --force-device-scale-factor=1 \
--disable-background-timer-throttling \
--disk-cache-dir=$CACHE_DIR --disk-cache-size=52428800 --media-cache-size=10485760 \
--disable-policy-cloud-management --no-proxy-server --disable-vulkan"

    echo "Pi Zero 2 mode: GPU disabled (vc4 GLES 2.0 incompatible with Chromium ANGLE). Using CPU rendering."

    # Conditional logging for debugging
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        CHROME_FLAGS="$CHROME_FLAGS --enable-logging=stderr --v=1"
    fi

    LAUNCH_WRAPPER="nice -n 0 ionice -c 2 -n 4"
    FULL_CMD="$LAUNCH_WRAPPER $BROWSER_CMD $CHROME_FLAGS"
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
        $FULL_CMD "$MIRROR_URL" &
    else
        $FULL_CMD "$MIRROR_URL" &> /dev/null &
    fi
    PID=$!

    # Auto-dismiss Chromium's "less than 1GB RAM" modal dialog on headless kiosk.
    # Only needed for Chromium — Cog doesn't show this dialog.
    if [ "$BROWSER_TYPE" = "chromium" ]; then
        (
            _dismiss_dialog() {
                if command -v wtype &>/dev/null; then
                    wtype -k Return 2>/dev/null
                elif command -v xdotool &>/dev/null; then
                    xdotool key Return 2>/dev/null
                elif command -v ydotool &>/dev/null; then
                    ydotool key 28:1 28:0 2>/dev/null
                fi
            }
            # Try at 8s, 12s, 16s (browser takes ~10-20s to show the dialog on Pi Zero 2)
            for delay in 8 12 16 20; do
                sleep "$delay" &
                wait $!
                _dismiss_dialog
            done
        ) &
        DISMISS_PID=$!
    fi

    # Kiosk trigger: In Wayland, tell the compositor to hide its cursor once the browser has loaded.
    if command -v labwc-msg &>/dev/null; then
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
    # Auto-dismiss for Chromium on X11
    if [ "$BROWSER_TYPE" = "chromium" ]; then
        (
            for delay in 8 12 16 20; do
                sleep "$delay" &
                wait $!
                if command -v xdotool &>/dev/null; then
                    xdotool key Return 2>/dev/null
                fi
            done
        ) &
        DISMISS_PID=$!
    fi
else
    echo "Error: No Wayland/X11 display environment found."
    echo "Note: This script is intended to be run within an existing session or wrapped by labwc."
    exit 1
fi

# Cleanup browser, dismiss helper, and unclutter on exit (do NOT delete persistent profiles)
trap 'echo "Cleaning up..."; kill $PID ${DISMISS_PID:-} $UNCLUTTER_PID 2>/dev/null; wait $PID 2>/dev/null' EXIT

# Wait for browser to be killed by the parent Python script
wait $PID
