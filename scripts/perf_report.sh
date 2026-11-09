#!/bin/bash
# Optional performance report (see smartframe/perf.py). Run on the Pi.
#
#   scripts/perf_report.sh on       # turn measurements on (restarts the service)
#   scripts/perf_report.sh collect  # write a report file after testing
#   scripts/perf_report.sh off      # turn measurements off (restarts the service)
set -e

SERVICE=smartframe
DROPIN_DIR=/etc/systemd/system/$SERVICE.service.d
DROPIN=$DROPIN_DIR/perf.conf

case "$1" in
on)
    sudo mkdir -p "$DROPIN_DIR"
    printf '[Service]\nEnvironment=SMARTFRAME_PERF=1\n' | sudo tee "$DROPIN" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl restart "$SERVICE"
    echo "Measurements ON. Now, from Home Assistant:"
    echo "  1. wait for the first mode to show"
    echo "  2. switch audio -> mirror -> audio a few times (wait ~30 s in each)"
    echo "  3. leave audio mode running 1 min with some sound"
    echo "Then run: $0 collect"
    ;;
collect)
    out="$HOME/smartframe-perf-$(date +%Y%m%d-%H%M%S).txt"
    {
        echo "=== System ==="
        tr -d '\0' </proc/device-tree/model 2>/dev/null; echo
        uname -r
        free -m
        command -v vcgencmd >/dev/null && vcgencmd get_throttled && vcgencmd measure_temp
        echo
        echo "=== Boot ==="
        systemd-analyze 2>/dev/null || true
        systemd-analyze blame 2>/dev/null | head -15 || true
        echo
        echo "=== Service start (since boot) ==="
        systemctl show "$SERVICE" -p ExecMainStartTimestampMonotonic -p ActiveEnterTimestamp
        echo
        echo "=== CPU per process (30 s sample, while the current mode runs) ==="
        top -b -d 5 -n 7 -o %CPU | awk '/^top -/{n++} n==7' | head -20
        echo
        echo "=== [PERF] lines (current service run) ==="
        journalctl -u "$SERVICE" -b --no-pager -o short-monotonic \
            --since "$(systemctl show "$SERVICE" -p ActiveEnterTimestamp --value)" \
            | grep '\[PERF\]' || echo "(none: did you run '$0 on'?)"
    } >"$out" 2>&1
    echo "Report written: $out"
    ;;
off)
    sudo rm -f "$DROPIN"
    sudo rmdir "$DROPIN_DIR" 2>/dev/null || true
    sudo systemctl daemon-reload
    sudo systemctl restart "$SERVICE"
    echo "Measurements OFF."
    ;;
*)
    echo "Usage: $0 on|collect|off" >&2
    exit 1
    ;;
esac
