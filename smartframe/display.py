"""The screen itself: power on/off (DDC/CI, CEC, wlr-randr...), brightness, contrast,
color profile and input source. Commands run one at a time (_display_lock)."""

import logging
import os
import subprocess
import threading

from smartframe import cache, compositor, mqtt_link
from smartframe.settings import (
    COLOR_PRESETS,
    DEFAULT_COLOR_PRESET,
    INPUT_SOURCES,
    MQTT_BRIGHTNESS_STATE_TOPIC,
    MQTT_COLOR_PRESET_STATE_TOPIC,
    MQTT_CONTRAST_STATE_TOPIC,
    MQTT_INPUT_SOURCE_STATE_TOPIC,
    RESTORE_ON_POWER_ON,
)

# Held while talking to the display (DDC/CI, CEC, wlr-randr...). Re-entrant because
# set_display_power(True) restores brightness/contrast/preset while holding it.
_display_lock = threading.RLock()

FB_BLANK_PATH = "/sys/class/graphics/fb0/blank"


def _get_hdmi_output_name():
    working_methods = cache.working_methods
    # 1. Use memory cache first
    if getattr(_get_hdmi_output_name, "cached", None):
        return _get_hdmi_output_name.cached

    # 2. Use persistent cache second
    if working_methods.get("hdmi_output"):
        _get_hdmi_output_name.cached = working_methods["hdmi_output"]
        return working_methods["hdmi_output"]

    try:
        # 3. Slow discovery if nothing is cached
        output = subprocess.check_output(
            ["wlr-randr"], stderr=subprocess.DEVNULL, timeout=1.5
        ).decode()
        for line in output.split("\n"):
            if "HDMI" in line and (line.strip() and not line.startswith(" ")):
                name = line.split(" ")[0]
                _get_hdmi_output_name.cached = name
                working_methods["hdmi_output"] = name
                cache.save()
                return name
    except Exception as e:
        logging.debug(f"wlr-randr output detection failed: {e}")
    return "HDMI-A-1"  # Fallback


def _with_display_lock(func):
    """Run display commands one at a time: two threads sending DDC/CI commands on the
    same I2C bus (or editing the cached strategy list) would race."""
    def wrapper(*args, **kwargs):
        with _display_lock:
            return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def _write_sysfs(path, value):
    """Write a value to a sysfs file. Returns True on success."""
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError as e:
        logging.debug(f"Writing '{value}' to {path} failed: {e}")
        return False


@_with_display_lock
def set_display_power(state: bool):
    """Sets display power using discovered strategies, optimizing for speed and reliability."""
    working_methods = cache.working_methods
    target = "ON" if state else "OFF"
    compositor.setup_display_env()
    output_name = _get_hdmi_output_name()

    # Strategy Definitions
    session_strategies = [
        (
            "Wayland (wlr-randr)",
            ["wlr-randr", "--output", output_name, "--on" if state else "--off"],
            lambda: "WAYLAND_DISPLAY" in os.environ,
        ),
        (
            "X11 (xset)",
            ["xset", "dpms", "force", "on" if state else "off"],
            lambda: "DISPLAY" in os.environ,
        ),
    ]

    hardware_strategies = [
        (
            "DDC/CI (Fast Off)",
            ["ddcutil", "setvcp", "D6", "0x01" if state else "0x04"],
            lambda: True,
        ),
        (
            "HDMI-CEC",
            [
                "sh",
                "-c",
                f'echo "{"on 0" if state else "standby 0"}" | cec-client -s -d 1',
            ],
            lambda: True,
        ),
        (
            "Legacy (vcgencmd)",
            ["vcgencmd", "display_power", "1" if state else "0"],
            lambda: True,
        ),
        (
            "FB Blanking",
            # Written directly (no sudo): setup_pi.sh installs a udev rule that makes
            # this file writable by the 'video' group.
            lambda: _write_sysfs(FB_BLANK_PATH, "0" if state else "1"),
            lambda: os.access(FB_BLANK_PATH, os.W_OK),
        ),
    ]

    def run_strategy(name, cmd):
        if callable(cmd):
            return cmd()
        try:
            # Short timeout for cached methods, longer for discovery (3.5s is safe for Pi Zero I2C)
            timeout = 3.5
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if res.returncode != 0:
                err = res.stderr.strip()
                if "bus" in err.lower() and (
                    "busy" in err.lower() or "error" in err.lower()
                ):
                    logging.debug(
                        f"Display method '{name}' failed (Bus Busy/Error): {err}"
                    )
                else:
                    logging.debug(
                        f"Display method '{name}' failed with code {res.returncode}: {err}"
                    )
            return res.returncode == 0
        except subprocess.TimeoutExpired:
            logging.debug(f"Display method '{name}' timed out after {timeout}s")
            return False
        except Exception as e:
            logging.debug(f"Display method '{name}' Exception: {e}")
            return False

    success_count = 0
    needs_save = False

    # 1. Execute Session Layer
    if working_methods.get("session_type"):
        # Map cached session type to a specific strategy name
        s_name = (
            "Wayland (wlr-randr)"
            if working_methods["session_type"] == "Wayland"
            else "X11 (xset)"
        )
        name, cmd_base, condition = next(
            (s for s in session_strategies if s[0] == s_name), (None, None, None)
        )
        if name and condition() and run_strategy(name, cmd_base):
            success_count += 1
        else:
            working_methods["session_type"] = None
            needs_save = True

    if not success_count:
        for name, cmd, condition in session_strategies:
            if condition() and run_strategy(name, cmd):
                working_methods["session_type"] = (
                    "Wayland" if "Wayland" in name else "X11"
                )
                success_count += 1
                needs_save = True
                break

    # 2. Execute Hardware Layer
    hardware_success = False

    # Try any previously working hardware methods from cache
    if working_methods["hardware"]:
        for name in list(working_methods["hardware"]):
            name_check, cmd, _ = next(
                (s for s in hardware_strategies if s[0] == name), (None, None, None)
            )
            if name_check and run_strategy(name_check, cmd):
                success_count += 1
                hardware_success = True
            else:
                logging.warning(
                    f"Cached display method '{name}' failed, removing from cache."
                )
                working_methods["hardware"].remove(name)
                needs_save = True

    # If no cached hardware methods worked, try discovering and executing ALL possible hardware strategies
    if not hardware_success:
        for name, cmd, condition in hardware_strategies:
            # Skip strategies we *just* tried and failed in the cached block
            if name in working_methods["hardware"]:
                continue

            if condition() and run_strategy(name, cmd):
                logging.info(f"Discovered new display control strategy: {name}")
                working_methods["hardware"].append(name)
                success_count += 1
                hardware_success = True
                needs_save = True
                # Break early only for premium methods (DDC/CI or CEC) to avoid double-processing
                if name in ["DDC/CI (Fast Off)", "HDMI-CEC"]:
                    break

    if needs_save:
        cache.save()

    if success_count == 0:
        logging.error(f"Failed to set display power to {target}.")
    else:
        logging.info(f"Display set to {target} (Methods: {success_count})")
        # If turned ON, restore all last known settings (unless the screen keeps them)
        if state and RESTORE_ON_POWER_ON:
            set_display_brightness(working_methods.get("brightness", 100), force=True)
            set_display_contrast(working_methods.get("contrast", 50), force=True)
            set_display_color_preset(
                working_methods.get("color_preset", DEFAULT_COLOR_PRESET)
            )


@_with_display_lock
def set_display_brightness(value: int, force=False):
    """Sets display brightness using ddcutil with caching."""
    working_methods = cache.working_methods
    value = max(0, min(100, int(value)))
    if not force and working_methods.get("brightness") == value:
        return True
    logging.info(f"Setting display brightness to {value}%...")
    if _run_vcp_command("10", str(value)):
        working_methods["brightness"] = value
        cache.save()
        mqtt_link.publish(MQTT_BRIGHTNESS_STATE_TOPIC, str(value), retain=True)
        return True
    return False


@_with_display_lock
def set_display_contrast(value: int, force=False):
    """Sets display contrast using ddcutil (VCP 12)."""
    working_methods = cache.working_methods
    value = max(0, min(100, int(value)))
    if not force and working_methods.get("contrast") == value:
        return True
    logging.info(f"Setting display contrast to {value}%...")
    if _run_vcp_command("12", str(value)):
        working_methods["contrast"] = value
        cache.save()
        mqtt_link.publish(MQTT_CONTRAST_STATE_TOPIC, str(value), retain=True)
        return True
    return False


@_with_display_lock
def set_display_color_preset(preset_name: str):
    """Sets display color preset (VCP 14)."""
    hex_val = COLOR_PRESETS.get(preset_name)
    if not hex_val:
        logging.warning(
            f"Unknown color preset {preset_name!r}. Valid: {list(COLOR_PRESETS)}"
        )
        return False
    logging.info(f"Setting display color preset to {preset_name} ({hex_val})...")
    if _run_vcp_command("14", "0x" + hex_val):
        cache.working_methods["color_preset"] = preset_name
        cache.save()
        mqtt_link.publish(MQTT_COLOR_PRESET_STATE_TOPIC, preset_name, retain=True)
        return True
    return False


@_with_display_lock
def set_display_input_source(source_name: str):
    """Sets display input source (VCP 60)."""
    hex_val = INPUT_SOURCES.get(source_name)
    if not hex_val:
        logging.warning(f"Unknown input source {source_name!r}. Valid: {list(INPUT_SOURCES)}")
        return False
    logging.info(f"Switching input source to {source_name} ({hex_val})...")
    if _run_vcp_command("60", "0x" + hex_val):
        cache.working_methods["input_source"] = source_name
        cache.save()
        mqtt_link.publish(MQTT_INPUT_SOURCE_STATE_TOPIC, source_name, retain=True)
        return True
    return False


def _run_vcp_command(vcp_code, value):
    """Helper to run a ddcutil setvcp command."""
    # No sudo: the service user is in the 'i2c' group, which owns /dev/i2c-*.
    cmd = ["ddcutil", "setvcp", vcp_code, value]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=3.5)
        return res.returncode == 0
    except Exception as e:
        logging.debug(f"ddcutil VCP {vcp_code} failed: {e}")
        return False
