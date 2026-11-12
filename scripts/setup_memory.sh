#!/bin/bash
# Memory tuning for the Raspberry Pi Zero 2 (512 MB of RAM). Safe to run again:
# each step checks what is already done.
#
# 1. gpu_mem=32. With the KMS driver (dtoverlay=vc4-kms-v3d), the screen, the audio
#    mode and the browser draw with memory that Linux manages (CMA). gpu_mem only
#    reserves memory for the firmware's video decoder and camera, which SmartFrame
#    does not use. A lower value gives that memory back to Linux.
#    Raise it again if you add hardware video decoding (e.g. a video MagicMirror module).
# 2. zram: compressed swap kept in RAM. It is much faster than swap on the SD card,
#    which made the Mirror mode wait minutes when RAM ran out.
# 3. vm.swappiness=100: use zram early, instead of waiting until RAM is full.
set -e

GPU_MEM=32
# BOOT_CONFIG can be set to test the script on a copy of config.txt
BOOT_CONFIG=${BOOT_CONFIG:-/boot/firmware/config.txt}
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG=/boot/config.txt
REBOOT_NEEDED=0

echo "== 1. GPU memory (gpu_mem) =="
GPU_LINES=()
[ -f "$BOOT_CONFIG" ] && mapfile -t GPU_LINES < <(grep -E '^gpu_mem(_[0-9]+)?=' "$BOOT_CONFIG" || true)
if [ ! -f "$BOOT_CONFIG" ]; then
    echo "No $BOOT_CONFIG found, skipping."
elif ! grep -qE '^dtoverlay=vc4-kms-v3d' "$BOOT_CONFIG"; then
    # Without KMS, the legacy driver draws from gpu_mem: lowering it would break the display
    echo "dtoverlay=vc4-kms-v3d is not enabled in $BOOT_CONFIG: gpu_mem left unchanged."
elif [ "${#GPU_LINES[@]}" -eq 1 ] && [ "${GPU_LINES[0]}" = "gpu_mem=${GPU_MEM}" ]; then
    echo "gpu_mem is already ${GPU_MEM}."
else
    sudo cp "$BOOT_CONFIG" "$BOOT_CONFIG.bak-smartframe"
    # Comment out every existing gpu_mem line (gpu_mem, gpu_mem_512, ...), then set ours
    # in an [all] section at the end so that it applies to every board.
    sudo sed -i -E 's/^(gpu_mem(_[0-9]+)?=.*)$/#\1/' "$BOOT_CONFIG"
    printf '\n[all]\n# SmartFrame (scripts/setup_memory.sh): GPU memory for the firmware video decoder/camera only\ngpu_mem=%s\n' \
        "$GPU_MEM" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    echo "gpu_mem set to ${GPU_MEM} (backup: $BOOT_CONFIG.bak-smartframe)."
    REBOOT_NEEDED=1
fi

echo "== 2. zram (compressed swap in RAM) =="
if grep -q zram /proc/swaps; then
    echo "A zram swap is already active, skipping."
else
    sudo apt install -y systemd-zram-generator
    if [ ! -f /etc/systemd/zram-generator.conf ]; then
        printf '[zram0]\nzram-size = ram / 2\ncompression-algorithm = zstd\nswap-priority = 100\n' \
            | sudo tee /etc/systemd/zram-generator.conf >/dev/null
    fi
    sudo systemctl daemon-reload
    sudo systemctl start systemd-zram-setup@zram0
    echo "zram swap started."
fi

echo "== 3. Swappiness =="
SYSCTL_FILE=/etc/sysctl.d/99-smartframe-zram.conf
if ! grep -q zram /proc/swaps; then
    echo "No zram swap active: swappiness left unchanged (a high value would push to the SD card)."
elif [ -f "$SYSCTL_FILE" ]; then
    echo "$SYSCTL_FILE already exists, skipping."
else
    echo 'vm.swappiness=100' | sudo tee "$SYSCTL_FILE" >/dev/null
    sudo sysctl -q -p "$SYSCTL_FILE"
    echo "vm.swappiness set to 100."
fi

echo ""
swapon --show 2>/dev/null || sudo swapon --show
if [ "$REBOOT_NEEDED" = "1" ]; then
    echo ""
    echo "Reboot to apply the new gpu_mem value: sudo reboot"
fi
