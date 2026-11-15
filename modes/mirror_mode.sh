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
        # Fast shell-based fallback for simple YAML (avoids 1.5s python overhead on Pi Zero 2):
        # the first "url:" inside the magic_mirror: section only.
        MIRROR_URL=$(awk '
            /^[^[:space:]#]/ { in_section = ($0 ~ /^magic_mirror:/) }
            in_section && $1 == "url:" { print $2; exit }
        ' "$CONFIG_FILE" | tr -d '"'\'' ')
    fi
fi

if [ -z "$MIRROR_URL" ]; then
    MIRROR_URL="http://localhost:8080"
fi

echo "Connecting to MagicMirror on: $MIRROR_URL"

# Suppress X11 keyboard warnings. The Wayland cursor is hidden by labwc itself
# (invisible cursor theme set up by main.py).
export XKB_LOG_LEVEL=0

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

    # Wayland fullscreen kiosk — set initial size to match display to avoid
    # EGL buffer mismatch on first frame (default 1024x768 gets discarded)
    export COG_PLATFORM_WL_VIEW_FULLSCREEN=1
    # Size from config.yaml (screen_size, passed by main.py), 1920x1080 by default
    SCREEN_SIZE="${SMARTFRAME_SCREEN_SIZE:-1920x1080}"
    if [[ "$SCREEN_SIZE" =~ ^([0-9]+)x([0-9]+)$ ]]; then
        export COG_PLATFORM_WL_VIEW_WIDTH="${BASH_REMATCH[1]}"
        export COG_PLATFORM_WL_VIEW_HEIGHT="${BASH_REMATCH[2]}"
    else
        echo "Warning: invalid screen_size '$SCREEN_SIZE' (expected e.g. 1920x1080). Using 1920x1080."
        export COG_PLATFORM_WL_VIEW_WIDTH=1920
        export COG_PLATFORM_WL_VIEW_HEIGHT=1080
    fi

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
    #   --bg-color             Black while loading, or transparent when the loading
    #                          picture is shown under the browser (see below)
    #   --enable-page-cache=false  Disable in-memory back/forward cache to save RAM on 512MB device
    # Loading picture: swaybg draws it on the background layer, under the browser
    # window, and Cog starts transparent. The picture then shows from the first
    # second until the page arrives (MagicMirror then paints its black background:
    # its modules show ~10 s later). Wayland only; without swaybg (or the picture),
    # the screen stays black while loading.
    SPLASH_IMAGE="$DIR/../assets/mirror_loading.png"
    COG_BG="black"
    if [ -n "$WAYLAND_DISPLAY" ] && [ -f "$SPLASH_IMAGE" ] && command -v swaybg &> /dev/null; then
        USE_SPLASH=1
        COG_BG="#00000000"
    fi
    COG_FLAGS="--platform=wl --webprocess-failure=restart --bg-color=$COG_BG --enable-page-cache=false"

    # Conditional logging for debugging: Cog core messages (load events, errors, warnings)
    # and the page's console messages (MagicMirror logs, JavaScript errors) in the journal
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        export G_MESSAGES_DEBUG="Cog-Core"
        COG_FLAGS="$COG_FLAGS --enable-write-console-messages-to-stdout=true"
    fi

    LAUNCH_WRAPPER="nice -n 0 ionice -c 2 -n 4"
    read -ra FULL_CMD <<< "$LAUNCH_WRAPPER $BROWSER_CMD $COG_FLAGS"

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
    # Certificate checks stay ON (no --ignore-certificate-errors): a MagicMirror served
    # with a self-signed certificate shows an error page. Serve it over plain HTTP on
    # the Pi itself, or over HTTPS with a certificate the Pi trusts.
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
--memory-pressure-thresholds=1,2 \
--disable-smooth-scrolling --mute-audio --force-device-scale-factor=1 \
--disable-background-timer-throttling \
--disk-cache-size=52428800 --media-cache-size=10485760 \
--disable-policy-cloud-management --no-proxy-server --disable-vulkan"

    echo "Pi Zero 2 mode: GPU disabled (vc4 GLES 2.0 incompatible with Chromium ANGLE). Using CPU rendering."

    # Conditional logging for debugging
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        CHROME_FLAGS="$CHROME_FLAGS --enable-logging=stderr --v=1"
    fi

    LAUNCH_WRAPPER="nice -n 0 ionice -c 2 -n 4"
    # Build the command as an array so --js-flags keeps its space-separated value intact
    read -ra FULL_CMD <<< "$LAUNCH_WRAPPER $BROWSER_CMD $CHROME_FLAGS"
    # Paths go in as separate elements, so a folder name with spaces stays one argument
    FULL_CMD+=("--user-data-dir=$PROFILE_DIR" "--disk-cache-dir=$CACHE_DIR")
    FULL_CMD+=("--js-flags=--max-old-space-size=128 --stack-size=1024")
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
elif [ -n "$DISPLAY" ]; then
    echo "Using X11 display: $DISPLAY"
else
    echo "Error: No Wayland/X11 display environment found."
    echo "Note: This script is intended to be run within an existing session or wrapped by labwc."
    exit 1
fi

PID=""
DISMISS_PID=""

# Loading picture under the browser (see USE_SPLASH above). It stays up while the
# mode runs (a few MB), hidden by the page, and goes with the mode's process group.
SPLASH_PID=""
if [ "${USE_SPLASH:-0}" = "1" ]; then
    swaybg -m fit -c 000000 -i "$SPLASH_IMAGE" &> /dev/null &
    SPLASH_PID=$!
fi

# Start the browser on $1 in the background: sets PID (and DISMISS_PID for Chromium)
start_browser() {
    if [ "$SMARTFRAME_DEBUG" = "1" ]; then
        "${FULL_CMD[@]}" "$1" &
    else
        "${FULL_CMD[@]}" "$1" &> /dev/null &
    fi
    PID=$!

    # Auto-dismiss Chromium's "less than 1GB RAM" modal dialog on headless kiosk.
    # Only needed for Chromium — Cog doesn't show this dialog.
    if [ "$BROWSER_TYPE" = "chromium" ]; then
        (
            _dismiss_dialog() {
                if [ -n "$WAYLAND_DISPLAY" ] && command -v wtype &>/dev/null; then
                    wtype -k Return 2>/dev/null
                elif command -v xdotool &>/dev/null; then
                    xdotool key Return 2>/dev/null
                elif [ -n "$WAYLAND_DISPLAY" ] && command -v ydotool &>/dev/null; then
                    ydotool key 28:1 28:0 2>/dev/null
                fi
            }
            # Try at 8, 12, 16 and 20 s after launch (the browser takes ~10-20 s to show
            # the dialog on Pi Zero 2): wait 8 s, then 4 s between tries.
            for delay in 8 4 4 4; do
                sleep "$delay" &
                wait $!
                _dismiss_dialog
            done
        ) &
        DISMISS_PID=$!
    fi
}

stop_browser() {
    kill $PID ${DISMISS_PID:-} 2>/dev/null
    wait $PID 2>/dev/null
    PID=""
    DISMISS_PID=""
}

# Cleanup browser, dismiss helper, and unclutter on exit (do NOT delete persistent profiles)
trap 'echo "Cleaning up..."; kill ${PID:-} ${DISMISS_PID:-} ${SPLASH_PID:-} $UNCLUTTER_PID 2>/dev/null; wait ${PID:-} 2>/dev/null' EXIT

# ──────────────────────────────────────────────
#  Wait screen while the MagicMirror server does not answer
# ──────────────────────────────────────────────
# Without it, the browser shows an error page and stays there. Only network
# failures count as "not answering" (curl: 6 name not found, 7 refused, 28 timeout,
# 52 empty reply, 56 connection reset). Any HTTP answer, or a TLS problem (which
# waiting would not fix), opens the page right away. No curl: no check.
WAIT_RETRY_SECONDS=5

server_down() {
    command -v curl &> /dev/null || return 1
    curl -s -o /dev/null --connect-timeout 3 --max-time 5 "$MIRROR_URL"
    case $? in
        6|7|28|52|56) return 0 ;;
        *) return 1 ;;
    esac
}

# The page is passed as a data: URL (no file to read: the browser sandbox may not
# see this folder). It shows the server's host:port, never the full URL (it may
# hold a token). The moving dots and the timer also tell screen_watch that the
# browser is alive.
wait_page_url() {
    local host
    host=$(printf '%s' "$MIRROR_URL" | sed -E 's|^[a-zA-Z]+://||; s|[/?#].*$||; s|^.*@||; s/[^A-Za-z0-9.:_\[\]-]//g')
    local html='<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#000;color:#9aa0a6;font:32px sans-serif}
body{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:24px}
h1{margin:0;font-weight:300;font-size:56px;color:#e8eaed}
.dots span{display:inline-block;width:18px;height:18px;margin:0 8px;border-radius:50%;background:#e8eaed;animation:p 1.4s infinite ease-in-out}
.dots span:nth-child(2){animation-delay:.2s}.dots span:nth-child(3){animation-delay:.4s}
@keyframes p{0%,80%,100%{opacity:.15}40%{opacity:1}}
</style></head><body><h1>Waiting for MagicMirror</h1><div class="dots"><span></span><span></span><span></span></div>
<div>HOST does not answer. Trying again every few seconds.</div><div id="t"></div>
<script>var s=Date.now();setInterval(function(){var m=Math.floor((Date.now()-s)/60000);document.getElementById("t").textContent=m?"Waiting for "+m+" min":""},5000)</script>
</body></html>'
    printf 'data:text/html;base64,%s' "$(printf '%s' "${html//HOST/$host}" | base64 | tr -d '\n')"
}

if server_down; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] MagicMirror does not answer: showing the wait screen, trying again every ${WAIT_RETRY_SECONDS} s."
    start_browser "$(wait_page_url)"
    while server_down; do
        if ! kill -0 "$PID" 2>/dev/null; then
            start_browser "$(wait_page_url)"  # The wait screen itself crashed
        fi
        sleep "$WAIT_RETRY_SECONDS" &
        wait $!
    done
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] MagicMirror answers: opening it."
    stop_browser
fi

start_browser "$MIRROR_URL"

# Wait for browser to be killed by the parent Python script
wait $PID
