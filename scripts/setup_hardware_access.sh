#!/bin/bash
# Gives the SmartFrame service the hardware access it needs WITHOUT sudo at runtime:
#   - loads the i2c-dev kernel module at boot (ddcutil needs /dev/i2c-*)
#   - lets the 'video' group write the framebuffer blank switch (screen-off fallback)
#   - keeps config.yaml (MQTT password) readable by its owner only
#
# Safe to run any number of times: it only changes what is missing.
# Called by setup_pi.sh and by the git post-merge hook.
#
# Usage: ./scripts/setup_hardware_access.sh [--check]
#   --check   only report whether something is missing (exit 1 if so), change nothing

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
ROOT="$DIR/.."

MODULES_FILE="/etc/modules-load.d/smartframe.conf"
MODULES_CONTENT="i2c-dev"
UDEV_FILE="/etc/udev/rules.d/99-smartframe-fb.rules"
UDEV_CONTENT='SUBSYSTEM=="graphics", KERNEL=="fb0", RUN+="/bin/chgrp video /sys/class/graphics/fb0/blank", RUN+="/bin/chmod g+w /sys/class/graphics/fb0/blank"'

# Only meaningful on the Linux device itself (not on a dev machine)
if [ "$(uname -s)" != "Linux" ]; then
    exit 0
fi

modules_ok() { [ "$(cat "$MODULES_FILE" 2>/dev/null)" = "$MODULES_CONTENT" ]; }
udev_ok()    { [ "$(cat "$UDEV_FILE" 2>/dev/null)" = "$UDEV_CONTENT" ]; }

if [ "$1" = "--check" ]; then
    modules_ok && udev_ok
    exit $?
fi

if modules_ok && udev_ok; then
    echo "[hardware-access] Already configured."
else
    echo "== Configuring hardware access (no sudo needed at runtime)... =="

    if ! modules_ok; then
        echo "$MODULES_CONTENT" | sudo tee "$MODULES_FILE" >/dev/null
        echo "Installed $MODULES_FILE"
    fi
    # Load it now too, so DDC/CI works without a reboot
    sudo modprobe i2c-dev || echo "Warning: could not load i2c-dev now (it will load at next boot)."

    if ! udev_ok; then
        echo "$UDEV_CONTENT" | sudo tee "$UDEV_FILE" >/dev/null
        echo "Installed $UDEV_FILE"
        sudo udevadm control --reload-rules
        sudo udevadm trigger --subsystem-match=graphics || true
    fi
fi

# config.yaml holds the MQTT password: readable by its owner only (no sudo needed)
if [ -f "$ROOT/config.yaml" ]; then
    chmod 600 "$ROOT/config.yaml"
fi
