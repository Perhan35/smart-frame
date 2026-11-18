#!/bin/bash
# Installs (or updates) the Wi-Fi watchdog: scripts/wifi_watchdog.sh as a systemd
# service. Safe to run again. Run as the normal user (it calls sudo itself).
#   ./scripts/setup_wifi_watchdog.sh            install / update and start
#   ./scripts/setup_wifi_watchdog.sh --remove   stop and remove it
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
BIN=/usr/local/sbin/smartframe-wifi-watchdog
UNIT=/etc/systemd/system/smartframe-wifi-watchdog.service
DEFAULTS=/etc/default/smartframe-wifi-watchdog

if [ "$1" = "--remove" ]; then
    sudo systemctl disable --now smartframe-wifi-watchdog.service 2>/dev/null || true
    sudo rm -f "$UNIT" "$BIN"
    sudo systemctl daemon-reload
    echo "Wi-Fi watchdog removed ($DEFAULTS kept, if it exists)."
    exit 0
fi

if ! command -v nmcli >/dev/null 2>&1; then
    echo "NetworkManager (nmcli) not found: the Wi-Fi watchdog needs it. Skipped."
    exit 0
fi

# arping checks that the router answers, even when it blocks ping
if ! command -v arping >/dev/null 2>&1; then
    sudo apt install -y iputils-arping
fi

sudo install -m 755 -o root -g root "$DIR/wifi_watchdog.sh" "$BIN"
sudo install -m 644 -o root -g root "$DIR/smartframe-wifi-watchdog.service" "$UNIT"
if [ ! -f "$DEFAULTS" ]; then
    printf '%s\n' \
        "# SmartFrame Wi-Fi watchdog settings (see scripts/wifi_watchdog.sh)." \
        "# Apply changes with: sudo systemctl restart smartframe-wifi-watchdog" \
        "#WATCHDOG_IFACE=wlan0" \
        "#WATCHDOG_RECONNECT_SECONDS=90" \
        "#WATCHDOG_RELOAD_SECONDS=600" \
        "# Reboot after this long without Wi-Fi (0 = never), at most once every GAP seconds" \
        "#WATCHDOG_REBOOT_SECONDS=1800" \
        "#WATCHDOG_REBOOT_GAP_SECONDS=21600" \
        | sudo tee "$DEFAULTS" >/dev/null
fi
sudo systemctl daemon-reload
sudo systemctl enable smartframe-wifi-watchdog.service
sudo systemctl restart smartframe-wifi-watchdog.service
echo "Wi-Fi watchdog installed and started (settings: $DEFAULTS)."
echo "Log: journalctl -u smartframe-wifi-watchdog. Remove: ./scripts/setup_wifi_watchdog.sh --remove"
