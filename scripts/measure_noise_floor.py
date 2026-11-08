#!/usr/bin/env python3
"""
Measure the microphone's noise floor and store it as audio.noise_floor_dba.

SmartFrame removes this floor (in energy, not in dB) from every reading, so quiet
rooms read closer to the truth instead of sticking to the mic's own hiss.

How to use it:
  1. Calibrate first (audio.calibration_offset_db): the floor is stored in calibrated
     dBA, so measure it again if you change the calibration later.
  2. Stop the service, which holds the microphone:  sudo systemctl stop smartframe
  3. Make it as quiet as possible (night, windows closed, no fan). The script measures
     the mic AND the room: a noisy room gives a floor that is too high, and quiet
     readings then come out too low.
  4. Run:  .venv/bin/python scripts/measure_noise_floor.py [seconds]
  5. Start the service again:  sudo systemctl start smartframe

If you cannot get a quiet enough room, use the INMP441 datasheet value instead
(about 33 dBA: 94 dB SPL reference - 61 dBA SNR) and type it in config.yaml.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pyaudio
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from smartframe import dsp  # noqa: E402
from smartframe.audio_input import find_input_device  # noqa: E402

CONFIG_PATH = ROOT / "config.yaml"
CHUNK = 4096


def service_is_running():
    try:
        res = subprocess.run(["systemctl", "is-active", "--quiet", "smartframe"], timeout=5)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def write_floor(value):
    """Set audio.noise_floor_dba in config.yaml, editing only that line (keeps comments)."""
    text = CONFIG_PATH.read_text()
    pattern = re.compile(r"^(\s+noise_floor_dba:[ \t]*)[^#\n]*?([ \t]*#.*)?$", re.MULTILINE)
    if not pattern.search(text):
        return False
    new_text = pattern.sub(lambda m: f"{m.group(1)}{value:.1f}{m.group(2) or ''}", text, count=1)
    if (yaml.safe_load(new_text) or {}).get("audio", {}).get("noise_floor_dba") != round(value, 1):
        return False
    CONFIG_PATH.write_text(new_text)
    return True


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    if not CONFIG_PATH.exists():
        print("config.yaml not found. Run scripts/setup_pi.sh first.")
        return 1
    if service_is_running():
        print("The smartframe service is running and holds the microphone.")
        print("Stop it first:  sudo systemctl stop smartframe")
        return 1

    config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    audio = config.get("audio") or {}
    offset = audio.get("calibration_offset_db", 0) or 0
    channel = dsp.channel_index(audio.get("mic_channel", "left"))
    if not offset:
        print("Warning: audio.calibration_offset_db is 0. Calibrate first, or the floor "
              "will not match the levels you see.")

    # Hide ALSA/JACK probing noise
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    os.dup2(devnull, 2)
    try:
        p = pyaudio.PyAudio()
    finally:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)
        os.close(devnull)

    try:
        index, info = find_input_device(p, audio.get("device_index"))
        if info is None:
            print("No microphone found.")
            return 1
        channels, rate = dsp.input_format(info)
        stream = p.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                        input_device_index=index, frames_per_buffer=CHUNK)
        print(f"Microphone: {info.get('name')} ({channels} ch, {rate} Hz)")
        print("Silence please... starting in 3 s.")
        time.sleep(3)
        stream.read(CHUNK, exception_on_overflow=False)  # Drop the start-up click

        meter = dsp.LevelMeter(rate, offset)
        per_second = []
        start = last = time.monotonic()
        while time.monotonic() - start < seconds:
            meter.process(dsp.pick_channel(
                stream.read(CHUNK, exception_on_overflow=False), channels, channel))
            if time.monotonic() - last >= 1.0:
                last = time.monotonic()
                levels = meter.pop_interval()
                if levels:
                    per_second.append(levels[0])
                    print(f"  {len(per_second):2d}s  LAeq {levels[0]:5.1f} dBA")
        stream.stop_stream()
        stream.close()
    finally:
        p.terminate()

    if not per_second:
        print("No audio captured.")
        return 1
    # The quietest second is the best estimate of the floor (ignores a passing noise).
    floor = min(per_second)
    print(f"\nNoise floor: {floor:.1f} dBA (quietest second; average {sum(per_second) / len(per_second):.1f})")

    answer = input("Save it as audio.noise_floor_dba in config.yaml? [y/N] ").strip().lower()
    if answer != "y":
        print("Not saved.")
        return 0
    if write_floor(floor):
        print("Saved. Restart the service:  sudo systemctl start smartframe")
    else:
        print(f"Could not edit config.yaml automatically. Set it by hand under audio:\n"
              f"  noise_floor_dba: {floor:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
