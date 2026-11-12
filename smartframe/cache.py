"""What the frame found out about its hardware, kept across restarts (.smartframe_cache):
the display methods that work, the last brightness/contrast/color/input, the mic.

`working_methods` is shared by several threads: change its values in place (never
rebind it), and call save() after a change.
"""

import json
import logging
import os
import threading

from smartframe import settings
from smartframe.settings import COLOR_PRESETS, DEFAULT_COLOR_PRESET

CACHE_FILE = settings.CACHE_FILE

working_methods = {
    "session_type": None,
    "hdmi_output": None,
    "hardware": [],
    "brightness": 100,
    "contrast": 50,
    "color_preset": DEFAULT_COLOR_PRESET,
    "input_source": "HDMI-1",
    "audio_device": None,
    "audio_device_name": None,  # Checked against the index: ALSA can renumber cards
}

# Re-entrant: the SIGTERM handler (main thread) may load the cache while that same
# thread is in the middle of saving it.
_cache_lock = threading.RLock()


def load():
    if not os.path.exists(CACHE_FILE):
        return
    try:
        with _cache_lock, open(CACHE_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.pop("labwc_path", None)  # Older versions cached it: now looked up each time
            working_methods.update(data)
        if working_methods.get("color_preset") not in COLOR_PRESETS:
            # Older versions cached the invalid default "6500 K".
            working_methods["color_preset"] = DEFAULT_COLOR_PRESET
        logging.debug("Loaded display discovery cache.")
    except Exception as e:
        logging.debug(f"Could not load {CACHE_FILE}: {e}")


def save():
    """Write the cache atomically: a crash or a second thread never leaves broken JSON."""
    tmp = CACHE_FILE + ".tmp"
    try:
        with _cache_lock:
            snapshot = {
                k: list(v) if isinstance(v, list) else v
                for k, v in list(working_methods.items())
            }
            with open(tmp, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp, CACHE_FILE)
    except Exception as e:
        logging.debug(f"Could not save {CACHE_FILE}: {e}")


# Load discovery cache at startup (avoids disk reads on first mode transition)
load()
