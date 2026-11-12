"""Checks config.yaml once, at startup, and lists what is wrong in plain words.

It never stops the frame: a frame that does not boot is worse than one running
with a default value. An invalid value is removed, so the code falls back to its
own default for it, and a warning says which key and why.

Only depends on the standard library (tested in CI, imported by main.py and the
audio mode).
"""

import os
import re

THEMES = ("neon", "eink")
FONTS = ("default", "barlow")
SECTIONS = ("mqtt", "magic_mirror", "audio")

_SCREEN_SIZE = re.compile(r"^\s*\d{2,5}\s*x\s*\d{2,5}\s*$")

# (section or None for top level, key)
_BOOLEANS = (
    (None, "debug"),
    (None, "keep_compositor_when_off"),
    (None, "restore_display_settings_on_power_on"),
    ("mqtt", "tls"),
    ("audio", "show_debug"),
    ("audio", "preload"),
)

# (section, key, minimum, maximum, null allowed). A number written as text ("37")
# is accepted and converted, as the code always did with float().
_NUMBERS = (
    ("mqtt", "port", 1, 65535, False),
    ("audio", "threshold_db_warning", 0, 200, False),
    ("audio", "threshold_db_error", 0, 200, False),
    ("audio", "calibration_offset_db", -100, 200, True),
    ("audio", "calibration_slope", 0.1, 10, True),
    ("audio", "noise_floor_dba", 0, 120, True),
    ("audio", "render_scale", 0.25, 1.0, True),
    ("audio", "pixel_shift_minutes", 0, 1440, True),
)

# (section, key, allowed values, compared in lower case as text)
_CHOICES = (
    ("audio", "mic_channel", ("left", "right")),
    ("audio", "capture_bits", ("16", "32")),
    ("audio", "theme", THEMES),
    ("audio", "font", FONTS),
)


def available_modes(modes_dir):
    """Mode names found in `modes_dir` ("xxx_mode.py" / "xxx_mode.sh"), plus "off"."""
    modes = {"off"}
    try:
        names = os.listdir(modes_dir)
    except OSError:
        names = []
    for name in names:
        for suffix in ("_mode.py", "_mode.sh"):
            if name.endswith(suffix) and len(name) > len(suffix):
                modes.add(name[: -len(suffix)])
    return sorted(modes)


_INTEGERS = frozenset({("mqtt", "port")})
_TRUE = ("true", "yes", "on")
_FALSE = ("false", "no", "off")


def _as_number(value):
    """int/float, or a number written as text -> the number; anything else -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return int(number) if number.is_integer() and "." not in value else number
    return None


def check(config, modes=None):
    """(clean config, warnings). The input is never modified.

    `modes`: the available mode names, to check default_mode (skipped if None).
    """
    warnings = []
    if config is None:
        config = {}
    if not isinstance(config, dict):
        warnings.append(
            "config.yaml must be a list of 'key: value' settings. "
            "It is ignored: every setting uses its default."
        )
        return {}, warnings

    clean = dict(config)
    for name in SECTIONS:
        section = clean.get(name)
        if section is None:
            continue
        if isinstance(section, dict):
            clean[name] = dict(section)
        else:
            warnings.append(f"'{name}:' must contain indented 'key: value' lines. "
                            "It is ignored (defaults used).")
            clean[name] = {}

    def where(section):
        return clean if section is None else clean.get(section) or {}

    def drop(section, key, reason):
        name = key if section is None else f"{section}.{key}"
        warnings.append(f"{name}: {reason}. Using the default value.")
        where(section).pop(key, None)

    for section, key in _BOOLEANS:
        values = where(section)
        value = values.get(key)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip().lower()
        if text in _TRUE or text in _FALSE:
            values[key] = text in _TRUE  # "false" in quotes must not count as true
        else:
            drop(section, key, f"{value!r} is not true or false")

    for section, key, low, high, nullable in _NUMBERS:
        values = where(section)
        if key not in values:
            continue
        value = values[key]
        if value is None:
            if not nullable:
                drop(section, key, "is empty")
            continue
        number = _as_number(value)
        if number is None:
            drop(section, key, f"{value!r} is not a number")
        elif not low <= number <= high:
            drop(section, key, f"{value} is outside {low}..{high}")
        elif (section, key) in _INTEGERS and number != int(number):
            drop(section, key, f"{value} is not a whole number")
        else:
            values[key] = int(number) if (section, key) in _INTEGERS else number

    for section, key, allowed in _CHOICES:
        values = where(section)
        value = values.get(key)
        if value is not None and str(value).strip().lower() not in allowed:
            drop(section, key, f"{value!r} is not one of {', '.join(allowed)}")

    audio = where("audio")
    index = audio.get("device_index")
    if index is not None:
        number = _as_number(index)
        if number is None or number < 0 or number != int(number):
            drop("audio", "device_index", f"{index!r} is not a device number (0, 1, 2...) or null")
        else:
            audio["device_index"] = int(number)

    size = clean.get("screen_size")
    if size is not None and not _SCREEN_SIZE.match(str(size)):
        drop(None, "screen_size", f"{size!r} is not like \"1920x1080\"")

    mode = clean.get("default_mode")
    if mode is not None and modes is not None and str(mode).strip().lower() not in modes:
        drop(None, "default_mode", f"{mode!r} is not one of {', '.join(modes)}")

    return clean, warnings
