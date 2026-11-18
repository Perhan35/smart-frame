#!/bin/bash
# SmartFrame Wi-Fi watchdog: brings the Wi-Fi back when the Pi loses it for good.
#
# Why: when the Wi-Fi drops and the first reconnection attempts fail (the access point
# does not answer, or the Wi-Fi chip is stuck), NetworkManager can end the attempt with
# "no-secrets". It then stops trying for good: the frame stays offline (Home Assistant
# shows it as unavailable) until someone reconnects it by hand or unplugs it.
#
# What it does while the Wi-Fi is down (checked every WATCHDOG_CHECK_SECONDS):
#   1. after WATCHDOG_RECONNECT_SECONDS: asks NetworkManager to connect again
#      (`nmcli device connect`, which also lifts the "no-secrets" block), then again
#      every WATCHDOG_RECONNECT_SECONDS;
#   2. every WATCHDOG_RELOAD_SECONDS: reloads the Wi-Fi driver (resets the chip,
#      about 5 s);
#   3. after WATCHDOG_REBOOT_SECONDS: reboots the Pi, at most once every
#      WATCHDOG_REBOOT_GAP_SECONDS (so a router that is really down does not make the
#      Pi reboot all night). WATCHDOG_REBOOT_SECONDS=0 never reboots.
#
# "Down" means: the Wi-Fi interface has no default route, or the router does not answer
# ARP (works even when the router blocks ping). It never looks at the MQTT broker or any
# server: a server outage must not make the Pi reboot.
#
# Installed and started by scripts/setup_wifi_watchdog.sh. Settings can be changed in
# /etc/default/smartframe-wifi-watchdog. WATCHDOG_DRY_RUN=1 only logs what it would do.

IFACE=${WATCHDOG_IFACE:-wlan0}
CHECK=${WATCHDOG_CHECK_SECONDS:-30}
RECONNECT_AFTER=${WATCHDOG_RECONNECT_SECONDS:-90}
RELOAD_EVERY=${WATCHDOG_RELOAD_SECONDS:-600}
REBOOT_AFTER=${WATCHDOG_REBOOT_SECONDS:-1800}
REBOOT_GAP=${WATCHDOG_REBOOT_GAP_SECONDS:-21600}
DRY_RUN=${WATCHDOG_DRY_RUN:-0}
STATE_DIR=${WATCHDOG_STATE_DIR:-/var/lib/smartframe-wifi-watchdog}
REBOOT_STAMP="$STATE_DIR/last-reboot"

log() { echo "$*"; }

# Seconds since boot: unlike the clock, it never jumps (NTP sync after boot).
uptime_s() { cut -d. -f1 /proc/uptime; }

# Runs a command, or only logs it in dry-run mode.
act() {
    if [ "$DRY_RUN" = "1" ]; then
        log "[dry-run] would run: $*"
    else
        "$@"
    fi
}

gateway() { ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}'; }

# Wi-Fi is up: default route through the interface, and the router answers ARP.
wifi_ok() {
    local gw
    gw=$(gateway)
    [ -n "$gw" ] || return 1
    command -v arping >/dev/null 2>&1 || return 0
    # One probe per second for up to 10 s, stops at the first reply: a weak link that
    # loses a few packets is not "down".
    arping -q -f -w 10 -I "$IFACE" "$gw" >/dev/null 2>&1
}

# Leave the Wi-Fi alone on purpose: radio or networking turned off, or the Pi uses
# another interface (cable) for its network.
should_watch() {
    [ "$(nmcli -t -f WIFI radio 2>/dev/null)" = "enabled" ] || return 1
    [ "$(nmcli networking 2>/dev/null)" = "enabled" ] || return 1
    if [ -z "$(gateway)" ] && ip -4 route show default 2>/dev/null | grep -qv " dev $IFACE "; then
        return 1
    fi
    return 0
}

# Kernel module of the Wi-Fi driver (e.g. brcmfmac). Read while the interface exists:
# it is gone once the driver is unloaded.
DRIVER=""
find_driver() {
    local mod
    mod=$(readlink -f "/sys/class/net/$IFACE/device/driver/module" 2>/dev/null) || return
    [ -n "$mod" ] && [ -d "$mod" ] && DRIVER=$(basename "$mod")
}

reconnect() {
    log "Asking NetworkManager to connect $IFACE again."
    act nmcli device connect "$IFACE" 2>&1 \
        || log "nmcli device connect failed (it keeps trying in the background)."
}

reload_driver() {
    find_driver
    if [ -z "$DRIVER" ]; then
        log "Wi-Fi driver unknown: cannot reload it."
        return
    fi
    # Modules that use the driver (e.g. brcmfmac_cyw) must be removed first.
    local holders=() h
    for h in /sys/module/"$DRIVER"/holders/*; do
        [ -e "$h" ] && holders+=("$(basename "$h")")
    done
    log "Reloading the Wi-Fi driver: ${holders[*]} $DRIVER."
    act modprobe -r "${holders[@]}" "$DRIVER" || log "Could not unload $DRIVER."
    sleep 2
    act modprobe "$DRIVER" || log "Could not load $DRIVER."
    local i
    for i in $(seq 1 30); do
        [ -e "/sys/class/net/$IFACE" ] && break
        sleep 1
    done
    [ -e "/sys/class/net/$IFACE" ] || log "$IFACE did not come back after ${i} s."
    reconnect
}

# True when the last watchdog reboot was more than REBOOT_GAP seconds ago. A stamp "in
# the future" means the clock is not set yet (no network, so no NTP): no reboot then.
reboot_allowed() {
    [ -f "$REBOOT_STAMP" ] || return 0
    local last now
    last=$(cat "$REBOOT_STAMP" 2>/dev/null)
    now=$(date +%s)
    [[ "$last" =~ ^[0-9]+$ ]] || return 0
    [ "$last" -le "$now" ] && [ $((now - last)) -ge "$REBOOT_GAP" ]
}

do_reboot() {
    log "Rebooting the Pi: no Wi-Fi for $1 s."
    if [ "$DRY_RUN" != "1" ]; then
        mkdir -p "$STATE_DIR" && date +%s > "$REBOOT_STAMP"
        sync
    fi
    act systemctl reboot
}

log "Watching $IFACE every ${CHECK} s (reconnect after ${RECONNECT_AFTER} s, driver reload every" \
    "${RELOAD_EVERY} s, reboot after ${REBOOT_AFTER} s (0 = never), at most once every ${REBOOT_GAP} s)."
[ "$DRY_RUN" = "1" ] && log "Dry run: actions are only logged."
command -v arping >/dev/null 2>&1 || log "arping not found: only the default route is checked."
find_driver

down_since=0
last_reconnect=0
last_reload=0
reboot_skipped=0
while true; do
    now=$(uptime_s)
    if ! should_watch; then
        down_since=0
    elif wifi_ok; then
        if [ "$down_since" -gt 0 ]; then
            log "Wi-Fi back after $((now - down_since)) s."
            down_since=0
        fi
        [ -n "$DRIVER" ] || find_driver
    else
        if [ "$down_since" -eq 0 ]; then
            down_since=$now
            last_reconnect=$now
            last_reload=$now
            reboot_skipped=0
            log "Wi-Fi down (no route on $IFACE, or the router does not answer)."
        fi
        down=$((now - down_since))
        if [ "$REBOOT_AFTER" -gt 0 ] && [ "$down" -ge "$REBOOT_AFTER" ] && [ "$reboot_skipped" -eq 0 ]; then
            if reboot_allowed; then
                do_reboot "$down"
            else
                log "No reboot: the watchdog already rebooted the Pi less than ${REBOOT_GAP} s ago."
            fi
            reboot_skipped=1
        fi
        if [ "$RELOAD_EVERY" -gt 0 ] && [ $((now - last_reload)) -ge "$RELOAD_EVERY" ]; then
            reload_driver
            last_reload=$(uptime_s)
            last_reconnect=$last_reload
        elif [ $((now - last_reconnect)) -ge "$RECONNECT_AFTER" ]; then
            reconnect
            last_reconnect=$now
        fi
    fi
    sleep "$CHECK"
done
