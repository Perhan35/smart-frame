#!/usr/bin/env python3
"""
Calibrate the sound level against a reference (sound level meter or phone app).

The script measures the level itself, asks what your reference read during the
same time, then computes audio.calibration_offset_db (and, with a 2nd point,
audio.calibration_slope) and writes them into config.yaml (comments are kept).

How to use it:
  1. Stop the service, which holds the microphone:  sudo systemctl stop smartframe
  2. Make a steady sound around 65 dBA near the frame (pink noise from a speaker,
     a fan, a TV at normal volume). Put the phone right next to the microphone,
     app set to dBA.
  3. Run:  .venv/bin/python scripts/calibrate.py [seconds]
     When it says GO, reset the app's average; when it says STOP, read the average
     (Leq / AVG) and type it in.
  4. Optional 2nd point: a clearly different level (about 40 dBA in a quiet room, or
     about 85 dBA). Only useful if quiet or loud levels are still off.
  5. Measure the noise floor again (scripts/measure_noise_floor.py), then:
     sudo systemctl start smartframe
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from smartframe import config_edit, dsp  # noqa: E402

CONFIG_PATH = ROOT / "config.yaml"
CHUNK = 4096
MIN_POINT_GAP_DB = 15.0  # 2 points closer than this give an unreliable slope
SLOPE_LIMITS = (0.5, 2.0)  # Outside this, something went wrong (unsteady sound, typo...)
MAX_SPREAD_DB = 6.0  # Seconds that differ more than this: the sound was not steady


def solve_calibration(points, old_offset):
    """Offset and slope from 1 or 2 (shown, reference) pairs, in dBA.

    `shown` must be measured with `old_offset` and slope 1.0, without noise floor.
    Model (smartframe.dsp): calibrated = REF + slope * (raw + offset - REF).
    With 2 points both are matched exactly, wherever the points are.
    """
    ref = dsp.CALIBRATION_REF_DBA
    if len(points) == 1:
        shown, phone = points[0]
        return old_offset + (phone - shown), 1.0
    (s1, p1), (s2, p2) = points[:2]
    r1, r2 = s1 - old_offset, s2 - old_offset  # Raw levels (no offset)
    if abs(r2 - r1) < 1e-6:
        raise ValueError("The two measured levels are the same.")
    slope = (p2 - p1) / (r2 - r1)
    if slope <= 0:
        raise ValueError("The reference went down while the measured level went up.")
    offset = ref + (p1 - ref) / slope - r1
    return offset, slope


def parse_db(text):
    """'64,5' or '64.5 dBA' -> 64.5, or None."""
    text = text.strip().lower().replace(",", ".").replace("dba", "").replace("db", "").strip()
    try:
        value = float(text)
    except ValueError:
        return None
    return value if 0 < value < 140 else None


def service_is_running():
    try:
        res = subprocess.run(["systemctl", "is-active", "--quiet", "smartframe"], timeout=5)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ask_db(prompt):
    while True:
        answer = input(prompt)
        if not answer.strip():
            return None
        value = parse_db(answer)
        if value is not None:
            return value
        print("  Please type a number, e.g. 64.5 (or press Enter to cancel).")


class Mic:
    """The microphone opened like the service does (device, channel, 16/32 bits)."""

    def __init__(self, audio):
        import pyaudio
        from smartframe.audio_input import find_input_device, pa_format

        # Hide ALSA/JACK probing noise
        devnull = os.open(os.devnull, os.O_WRONLY)
        old_stderr = os.dup(2)
        os.dup2(devnull, 2)
        try:
            self.p = pyaudio.PyAudio()
        finally:
            os.dup2(old_stderr, 2)
            os.close(old_stderr)
            os.close(devnull)

        index, info = find_input_device(self.p, audio.get("device_index"))
        if info is None:
            self.p.terminate()
            raise RuntimeError("No microphone found.")
        self.bits = dsp.capture_bits(audio)
        self.channel = dsp.channel_index(audio.get("mic_channel", "left"))
        self.channels, self.rate = dsp.input_format(info)
        self.stream = self.p.open(format=pa_format(self.bits), channels=self.channels,
                                  rate=self.rate, input=True, input_device_index=index,
                                  frames_per_buffer=CHUNK)
        print(f"Microphone: {info.get('name')} ({self.channels} ch, {self.rate} Hz, "
              f"{self.bits} bits)")

    def read(self):
        raw = self.stream.read(CHUNK, exception_on_overflow=False)
        return dsp.pick_channel(raw, self.channels, self.channel, self.bits)

    def measure(self, seconds, offset):
        """LAeq over `seconds` (offset applied, slope 1.0, no noise floor) and the spread."""
        meter = dsp.LevelMeter(self.rate, calibration_offset_db=offset,
                               noise_floor_dba=None, calibration_slope=1.0)
        self.read()  # Drop what was buffered while waiting for the user
        per_second, energy = [], 0.0
        print("  GO: reset the app's average now.")
        start = last = time.monotonic()
        while time.monotonic() - start < seconds:
            meter.process(self.read())
            if time.monotonic() - last >= 1.0:
                last = time.monotonic()
                levels = meter.pop_interval()
                if levels:
                    per_second.append(levels[0])
                    energy += 10 ** (levels[0] / 10.0)
                    print(f"  {len(per_second):2d}s  {levels[0]:5.1f} dBA")
        print("  STOP: read the app's average (Leq / AVG).")
        if not per_second:
            raise RuntimeError("No audio captured.")
        laeq = dsp.power_to_db(energy / len(per_second))
        return laeq, max(per_second) - min(per_second)

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.p.terminate()


def measure_point(mic, seconds, offset, label):
    input(f"\n{label} Press Enter when the sound is steady...")
    shown, spread = mic.measure(seconds, offset)
    print(f"  SmartFrame measured: {shown:.1f} dBA")
    if spread > MAX_SPREAD_DB:
        print(f"  Warning: the level moved by {spread:.0f} dB during the measurement. "
              "A steadier sound gives a better calibration.")
    phone = ask_db("  Your reference read (dBA): ")
    return None if phone is None else (shown, phone)


def save(offset, slope):
    text = CONFIG_PATH.read_text()
    for key, value in (("calibration_offset_db", round(offset, 1)),
                       ("calibration_slope", round(slope, 2))):
        new_text = config_edit.set_value(text, "audio", key, value)
        if new_text is None:
            return False
        text = new_text
    CONFIG_PATH.write_text(text)
    return True


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    if not CONFIG_PATH.exists():
        print("config.yaml not found. Run scripts/setup_pi.sh first.")
        return 1
    if service_is_running():
        print("The smartframe service is running and holds the microphone.")
        print("Stop it first:  sudo systemctl stop smartframe")
        return 1

    audio = (yaml.safe_load(CONFIG_PATH.read_text()) or {}).get("audio") or {}
    current = dsp.meter_settings(audio)
    old_offset = current["calibration_offset_db"]
    print(f"Current calibration: offset {old_offset:g} dB, slope {current['calibration_slope']:g}")

    try:
        mic = Mic(audio)
    except Exception as e:
        print(f"Could not open the microphone: {e}")
        return 1

    points = []
    try:
        point = measure_point(mic, seconds, old_offset,
                              "Point 1: a steady sound around 65 dBA.")
        if point is None:
            print("Cancelled. config.yaml was not changed.")
            return 0
        points.append(point)
        if not 50 <= point[1] <= 80:
            print("  Note: a level between 55 and 75 dBA works best for the first point.")

        answer = input("\nAdd a 2nd point (a clearly quieter or louder level)? [y/N] ")
        if answer.strip().lower() == "y":
            point2 = measure_point(mic, seconds, old_offset,
                                   "Point 2: a different steady level (~40 or ~85 dBA).")
            if point2 is not None:
                if abs(point2[1] - point[1]) < MIN_POINT_GAP_DB:
                    print(f"  The 2 points are less than {MIN_POINT_GAP_DB:.0f} dB apart: "
                          "the slope would not be reliable. Keeping 1 point.")
                else:
                    points.append(point2)
    finally:
        mic.close()

    try:
        offset, slope = solve_calibration(points, old_offset)
    except ValueError as e:
        print(f"Could not compute a calibration: {e}")
        return 1
    if len(points) == 2 and not SLOPE_LIMITS[0] <= slope <= SLOPE_LIMITS[1]:
        print(f"The slope ({slope:.2f}) looks wrong (unsteady sound or typo?). Keeping 1 point.")
        offset, slope = solve_calibration(points[:1], old_offset)

    print(f"\nNew calibration: offset {offset:.1f} dB, slope {slope:.2f}"
          f" (was {old_offset:g} dB, {current['calibration_slope']:g})")
    if input("Save it in config.yaml? [y/N] ").strip().lower() != "y":
        print("Not saved.")
        return 0
    if save(offset, slope):
        print("Saved.")
    else:
        print("Could not edit config.yaml automatically. Set it by hand under audio:\n"
              f"  calibration_offset_db: {offset:.1f}\n  calibration_slope: {slope:.2f}")
    if audio.get("noise_floor_dba") is not None:
        print("The noise floor is in calibrated dBA: measure it again now:\n"
              "  .venv/bin/python scripts/measure_noise_floor.py")
    print("Then start the service:  sudo systemctl start smartframe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
