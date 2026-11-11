import os
import subprocess
import sys
import time
import json
import shutil
import yaml
import paho.mqtt.client as mqtt
import logging
import signal
import socket
import threading
import queue
import math
import pyaudio
import ctypes
from datetime import datetime, timezone

from smartframe import config_check
from smartframe import dsp
from smartframe import ha
from smartframe.audio_input import device_key, find_input_device, pa_format, resolve_device
from smartframe.commands import CommandQueue, parse_command
from smartframe import perf  # PERF
perf.mark("imports_done")  # PERF

# Suppress noisy ALSA errors from PyAudio hardware probing (they spam stderr with
# "Expression 'ret' failed in pa_linux_alsa.c" during device enumeration)
try:
    _alsa_lib = ctypes.cdll.LoadLibrary('libasound.so.2')
    _alsa_err_handler = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                          ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
    @_alsa_err_handler
    def _alsa_noop_handler(filename, line, function, err, fmt):
        pass
    _alsa_lib.snd_lib_error_set_handler(_alsa_noop_handler)
except (OSError, AttributeError):
    pass  # Not on Linux or libasound not available: nothing to silence

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
if not os.path.exists(config_path):
    logging.error(
        "config.yaml not found. Copy config.example.yaml to config.yaml and fill in your settings."
    )
    sys.exit(1)
_config_errors = []
try:
    with open(config_path, "r") as file:
        config = yaml.safe_load(file)
except yaml.YAMLError as e:
    # Keep running with the defaults (screen off, no MQTT) rather than crash-loop.
    config = {}
    _config_errors.append(f"config.yaml is not valid YAML, every setting uses its default: {e}")

MODES_DIR = os.path.join(os.path.dirname(__file__), "modes")
config, _config_warnings = config_check.check(config, config_check.available_modes(MODES_DIR))

# config.yaml holds the MQTT password: keep it readable by its owner only.
try:
    if os.stat(config_path).st_mode & 0o077:
        os.chmod(config_path, 0o600)
except OSError:
    pass  # Best effort (e.g. file owned by another user)

# Configure logging based on debug setting
DEBUG_MODE = config.get("debug", False)
logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
for _message in _config_errors:
    logging.error(_message)
for _message in _config_warnings:
    logging.warning(f"config.yaml: {_message}")
if DEBUG_MODE:
    os.environ["SMARTFRAME_DEBUG"] = "1"
    logging.debug("DEBUG MODE ENABLED: Subprocess output will be visible.")

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

mqtt_config = config.get("mqtt") or {}
# One ID per frame (hostname by default): used in Home Assistant IDs and in every
# topic that contains "{device_id}", so several frames can share one broker.
DEVICE_ID = ha.device_id(mqtt_config.get("device_id"))


def _topic(key, default):
    return ha.expand_topic(mqtt_config.get(key) or default, DEVICE_ID)


MQTT_BROKER = mqtt_config.get("broker", "[MQTT_SERVER_IP_ADDRESS]")
MQTT_PORT = mqtt_config.get("port", 1883)
MQTT_COMMAND_TOPIC = _topic("topic", "smartframe/{device_id}/set_mode")
MQTT_STATE_TOPIC = _topic("state_topic", "smartframe/{device_id}/mode_state")
MQTT_STATUS_TOPIC = _topic("status_topic", "smartframe/{device_id}/status")
MQTT_RUN_STATE_TOPIC = _topic("run_state_topic", "smartframe/{device_id}/run_state")
MQTT_AVAILABLE_MODES_TOPIC = _topic("available_modes_topic", "smartframe/{device_id}/modes_available")
MQTT_DISCOVERY_PREFIX = mqtt_config.get("discovery_prefix", "homeassistant")
MQTT_BRIGHTNESS_COMMAND_TOPIC = _topic("brightness_topic", "smartframe/{device_id}/set_brightness")
MQTT_BRIGHTNESS_STATE_TOPIC = _topic("brightness_state_topic", "smartframe/{device_id}/brightness_state")
MQTT_CONTRAST_COMMAND_TOPIC = _topic("contrast_topic", "smartframe/{device_id}/set_contrast")
MQTT_CONTRAST_STATE_TOPIC = _topic("contrast_state_topic", "smartframe/{device_id}/contrast_state")
MQTT_COLOR_PRESET_COMMAND_TOPIC = _topic("color_preset_topic", "smartframe/{device_id}/set_color_preset")
MQTT_COLOR_PRESET_STATE_TOPIC = _topic("color_preset_state_topic", "smartframe/{device_id}/color_preset_state")
MQTT_INPUT_SOURCE_COMMAND_TOPIC = _topic("input_source_topic", "smartframe/{device_id}/set_input_source")
MQTT_INPUT_SOURCE_STATE_TOPIC = _topic("input_source_state_topic", "smartframe/{device_id}/input_source_state")
MQTT_DBA_STATE_TOPIC = _topic("dba_state_topic", "smartframe/{device_id}/audio/dba")
MQTT_LAFMAX_STATE_TOPIC = _topic("lafmax_state_topic", "smartframe/{device_id}/audio/lafmax")
MQTT_CPU_TEMP_STATE_TOPIC = _topic("cpu_temp_state_topic", "smartframe/{device_id}/cpu_temp")
MQTT_STARTED_STATE_TOPIC = _topic("started_state_topic", "smartframe/{device_id}/started")
MQTT_RESTART_COMMAND_TOPIC = _topic("restart_topic", "smartframe/{device_id}/restart")
MQTT_USER = mqtt_config.get("username")
MQTT_PASS = mqtt_config.get("password")
MQTT_TLS = bool(mqtt_config.get("tls", False))
MQTT_CA_CERT = mqtt_config.get("ca_cert") or None
# Home Assistant publishes "online" here when it (re)starts: discovery is sent again then.
MQTT_HA_STATUS_TOPIC = f"{MQTT_DISCOVERY_PREFIX}/status"

# Start time for the "Started" sensor. The Pi has no clock battery: at boot the wall
# clock may still be wrong until NTP syncs, so the start time is computed from the
# monotonic clock when it is published, and sent again if it moves (started_at()).
_START_MONOTONIC = time.monotonic()
_started_sent = None  # Wall-clock start time last sent to HA (epoch seconds)
CPU_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"
CPU_TEMP_EVERY = 60.0  # Seconds between two CPU temperature readings
# Exit code used by the HA "Restart" button: systemd (Restart=on-failure) starts
# the service again. 75 = EX_TEMPFAIL ("try again later").
RESTART_EXIT_CODE = 75

# Globals for state management
current_process = None
current_mode = "off"
mqtt_client = None
labwc_config_dir = None
_labwc_process = None       # Persistent labwc compositor (reused across mode switches)
_labwc_wayland_display = None  # WAYLAND_DISPLAY name for the persistent labwc session
audio_monitor_thread = None
# Display settings (brightness...) keep only their latest value while they wait.
command_queue = CommandQueue()
# Held by whoever changes current_mode / current_process (worker or watchdog),
# so a mode switch and a crash recovery can never run at the same time.
_mode_lock = threading.Lock()
# Held while talking to the display (DDC/CI, CEC, wlr-randr...). Re-entrant because
# set_display_power(True) restores brightness/contrast/preset while holding it.
_display_lock = threading.RLock()
# Re-entrant: the SIGTERM handler (main thread) may load the cache while that same
# thread is in the middle of saving it.
_cache_lock = threading.RLock()

# Crash recovery (watchdog): relaunch a crashed mode after these delays (seconds),
# then give up and switch to "off". The counter resets once a mode runs this long.
RESTART_DELAYS = (3, 10, 30)
RESTART_STABLE_AFTER = 60.0
_restart_attempts = 0
_pending_restart = None  # (mode, monotonic time) of the next relaunch, or None
_mode_started_at = 0.0

# Mode lifecycle, shown in Home Assistant as a diagnostic sensor (run_state_topic):
#   off -> starting -> running -> (process exits) retrying -> starting -> ...
#   ... -> failed (after the last retry; the mode is then "off" until a new choice)
# The mode selector still shows the user's choice right away; this tells whether it
# actually runs. Changed only by whoever holds _mode_lock.
RUN_OFF, RUN_STARTING, RUN_RUNNING, RUN_RETRYING, RUN_FAILED = ha.RUN_STATES
RUNNING_AFTER = 3.0  # A mode counts as running once its process has lived this long
_run_state = RUN_OFF

# Keep labwc running in "off" (screen still switched off): the next mode starts
# without the 6-8 s cold start, at the cost of some RAM. Off by default.
KEEP_COMPOSITOR_WHEN_OFF = bool(config.get("keep_compositor_when_off", False))
# Send the saved brightness, contrast and color profile again each time the screen
# turns on. Turn it off if your screen keeps its settings while off: switching on is
# then faster, but changes made with the screen's own buttons are no longer undone.
RESTORE_ON_POWER_ON = config.get("restore_display_settings_on_power_on", True) is not False
# Screen size given to the Mirror browser (Cog) for its first frame
SCREEN_SIZE = str(config.get("screen_size") or "1920x1080")

# Levels sent by the audio mode through its private pipe: (LAeq, LAFmax) once per
# second. The pipe only exists between main.py and that one process: no file, so
# nothing else can write fake values, and EOF tells us the process is gone.
_external_levels = queue.Queue(maxsize=5)

AUDIO_CONFIG = config.get("audio") or {}

# audio.preload: keep an audio mode process ready in the background, with its slow
# imports (pygame, numpy) already done. Switching to "audio" then only has to open
# the screen and the mic. Costs the RAM of that waiting process. Off by default.
AUDIO_PRELOAD = bool(AUDIO_CONFIG.get("preload", False))
PRELOAD_AFTER = 20.0  # Seconds after a mode switch before preloading (leave the CPU to it)
PRELOAD_RETRY_MAX = 600.0  # A preload that keeps failing is retried at most this rarely
_preloaded_audio = None  # Popen of the waiting process, or None
_preload_failures = 0  # Preloads in a row that died before being used
_preload_next_try = 0.0  # monotonic time before which no new preload is started
_last_switch_at = time.monotonic()  # No preload right at boot: the first mode starts first

# Discovery cache to avoid repeated slow subprocess calls
CACHE_FILE = os.path.join(os.path.dirname(__file__), ".smartframe_cache")
CHROMIUM_PROFILE_DIR = os.path.join(os.path.dirname(__file__), ".chromium_profile")

COLOR_PRESETS = {
    "sRGB": "01",
    "Natural (6500 K)": "05",
    "Warm (5000 K)": "04",
    "Cool (9300 K)": "08",
}
DEFAULT_COLOR_PRESET = "Natural (6500 K)"
INPUT_SOURCES = {"HDMI-1": "11", "HDMI-2": "12", "DisplayPort-1": "0f", "VGA": "01"}

_working_methods = {
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

available_modes_cache = []


def get_available_modes(refresh=False):
    """Mode names from the modes directory, cached. refresh=True scans it again (a
    mode added after startup is then accepted without restarting the service)."""
    global available_modes_cache
    if refresh or not available_modes_cache:
        available_modes_cache = config_check.available_modes(MODES_DIR)
    return available_modes_cache


def _discover_audio_device():
    """Index of the microphone to use: audio.device_index from config.yaml if set,
    otherwise auto-detected once and cached (saves 1-2s of hardware probing)."""
    configured = AUDIO_CONFIG.get("device_index")
    if configured is not None:
        try:
            return int(configured)  # audio_mode / AudioMonitor report it if unusable
        except (TypeError, ValueError):
            logging.warning(f"Ignoring invalid audio.device_index: {configured!r}")

    if _working_methods.get("audio_device") is not None:
        return _working_methods["audio_device"]

    try:
        p = pyaudio.PyAudio()
        try:
            found_index, info = find_input_device(p)
        finally:
            p.terminate()
        if found_index is not None:
            _remember_audio_device(found_index, info)
            return found_index
    except Exception as e:
        logging.debug(f"Audio discovery failed: {e}")
    return None


def _remember_audio_device(index, info):
    """Cache the auto-detected mic: its index, and its name to check that index later."""
    name = device_key(info.get("name")) if info else None
    if (_working_methods.get("audio_device"), _working_methods.get("audio_device_name")) != (index, name):
        _working_methods["audio_device"] = index
        _working_methods["audio_device_name"] = name
        _save_cache()


def _resolve_audio_device(p):
    """(index, info) of the mic to open with PyAudio instance `p`, or (None, None).

    A cached index is checked against the cached device name: if ALSA renumbered the
    cards (e.g. a USB device was plugged in), the mic is found again by name, or
    detected again. A busy mic is not "wrong": the caller just retries later.
    """
    index = _discover_audio_device()
    if index is None:
        return None, None
    configured = AUDIO_CONFIG.get("device_index") is not None
    name = None if configured else _working_methods.get("audio_device_name")
    found, info = resolve_device(p, index, name)
    if found is None:
        if not configured:
            logging.warning(
                f"Cached microphone (index {index}, {name!r}) not found any more: detecting it again."
            )
        found, info = find_input_device(p, index if configured else None)
    if found is not None and not configured:
        _remember_audio_device(found, info)
    return found, info


def _read_level_pipe(fd):
    """Read '<LAeq> <LAFmax>' lines sent by the audio mode until it exits (EOF)."""
    try:
        with os.fdopen(fd, "r", encoding="ascii", errors="replace") as pipe:
            for line in pipe:
                try:
                    levels = tuple(float(v) for v in line.split()[:2])
                except ValueError:
                    continue
                if len(levels) != 2 or not all(math.isfinite(v) for v in levels):
                    continue
                try:
                    _external_levels.put_nowait(levels)
                except queue.Full:
                    pass  # Nobody is reading (e.g. MQTT offline): drop it
    except Exception as e:
        logging.debug(f"Audio level pipe closed with an error: {e}")


class AudioMonitor(threading.Thread):
    """Publishes the ambient sound level to MQTT once per second, in every mode.

    Outside audio mode it reads the microphone itself. In audio mode it releases the
    mic and forwards the values the audio mode sends through its pipe. Both sides use
    smartframe.dsp.LevelMeter, so the sensor does not jump when the mode changes.
    """

    CHUNK = 4096  # Frames per read (~85 ms at 48 kHz): few wake-ups, cheap

    def __init__(self, config, mq_client):
        super().__init__(daemon=True)
        self.mqtt_client = mq_client
        self.running = True
        self.meter_settings = dsp.meter_settings(AUDIO_CONFIG)
        self.bits = dsp.capture_bits(AUDIO_CONFIG)
        self.channel = dsp.channel_index(AUDIO_CONFIG.get("mic_channel", "left"))
        self._warned_silent = False

    def _publish(self, levels):
        laeq, lafmax = levels
        silent_db = self.meter_settings["calibration_offset_db"] - 100
        if lafmax < silent_db and not self._warned_silent:
            # A whole second of digital zeros: no real room is that quiet.
            logging.warning(
                "Microphone sends only zeros. If its L/R pin goes to 3.3V, set "
                "audio.mic_channel: \"right\" in config.yaml (or check the wiring)."
            )
            self._warned_silent = True
        if self.mqtt_client and self.mqtt_client.is_connected():
            try:
                self.mqtt_client.publish(MQTT_DBA_STATE_TOPIC, f"{laeq:.1f}")
                self.mqtt_client.publish(MQTT_LAFMAX_STATE_TOPIC, f"{lafmax:.1f}")
            except Exception as e:
                logging.debug(f"Monitor: publish failed: {e}")

    def _open(self, p):
        """Open the mic with its own channel count and rate. Returns (stream, channels, meter)."""
        idx, info = _resolve_audio_device(p)
        if idx is None:
            return None, 0, None
        channels, rate = dsp.input_format(info)
        stream = p.open(
            format=pa_format(self.bits),
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=idx,
            frames_per_buffer=self.CHUNK,
        )
        meter = dsp.LevelMeter(rate, **self.meter_settings)
        logging.info(
            f"Monitor: microphone open (index {idx}, {channels} ch, {rate} Hz, {self.bits}-bit)."
        )
        return stream, channels, meter

    @staticmethod
    def _close(stream):
        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except Exception as e:
                logging.debug(f"Monitor: closing the stream failed: {e}")

    def run(self):
        logging.info("Audio Monitor background thread started.")
        p = pyaudio.PyAudio()
        stream, channels, meter = None, 0, None
        last_publish = time.monotonic()

        while self.running:
            # 1. Audio mode owns the mic (also while it starts or waits for a retry):
            #    release it and forward the values it sends through its pipe.
            if current_mode == "audio":
                if stream:
                    self._close(stream)
                    stream, meter = None, None
                try:
                    self._publish(_external_levels.get(timeout=0.5))
                except queue.Empty:
                    pass
                continue

            # Values left over from an audio session are stale now.
            while not _external_levels.empty():
                try:
                    _external_levels.get_nowait()
                except queue.Empty:
                    break

            # 2. Read the microphone directly.
            if not stream:
                try:
                    stream, channels, meter = self._open(p)
                except Exception as e:
                    logging.debug(f"Monitor: failed to open the microphone (busy?): {e}")
                    stream = None
                if not stream:
                    time.sleep(5)
                    continue
                last_publish = time.monotonic()

            try:
                raw = stream.read(self.CHUNK, exception_on_overflow=False)
                meter.process(dsp.pick_channel(raw, channels, self.channel, self.bits))
            except Exception as e:
                logging.debug(f"Monitor: microphone read error: {e}")
                self._close(stream)
                stream, meter = None, None
                time.sleep(2)
                continue

            # 3. Publish LAeq / LAFmax of the last second.
            now = time.monotonic()
            if now - last_publish >= 1.0:
                levels = meter.pop_interval()
                last_publish = now
                if levels:
                    self._publish(levels)

        self._close(stream)
        p.terminate()


def _load_cache():
    global _working_methods
    if not os.path.exists(CACHE_FILE):
        return
    try:
        with _cache_lock, open(CACHE_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.pop("labwc_path", None)  # Older versions cached it: now looked up each time
            _working_methods.update(data)
        if _working_methods.get("color_preset") not in COLOR_PRESETS:
            # Older versions cached the invalid default "6500 K".
            _working_methods["color_preset"] = DEFAULT_COLOR_PRESET
        logging.debug("Loaded display discovery cache.")
    except Exception as e:
        logging.debug(f"Could not load {CACHE_FILE}: {e}")


def _save_cache():
    """Write the cache atomically: a crash or a second thread never leaves broken JSON."""
    tmp = CACHE_FILE + ".tmp"
    try:
        with _cache_lock:
            snapshot = {
                k: list(v) if isinstance(v, list) else v
                for k, v in list(_working_methods.items())
            }
            with open(tmp, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp, CACHE_FILE)
    except Exception as e:
        logging.debug(f"Could not save {CACHE_FILE}: {e}")


# Load discovery cache at startup (avoids disk reads on first mode transition)
_load_cache()

_display_env_detected = False


def _is_wayland_reachable(display_name):
    """Check if a Wayland socket is actually listening."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    socket_path = os.path.join(runtime_dir, display_name)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            s.connect(socket_path)
        return True
    except Exception:
        return False


def setup_display_env(force=False):
    """Detect and validate the current display environment (Wayland/X11)."""
    global _display_env_detected
    if _display_env_detected and not force:
        return

    _load_cache()

    # Ensure a persistent chromium profile exists for speed
    if not os.path.exists(CHROMIUM_PROFILE_DIR):
        os.makedirs(CHROMIUM_PROFILE_DIR, exist_ok=True)
        logging.info(f"Created persistent Chromium profile at: {CHROMIUM_PROFILE_DIR}")

    uid = os.getuid()
    needs_save = False

    def is_x11_active():
        """Check if X server is running by looking for the process."""
        for pattern in ["Xorg", "X"]:
            if (
                subprocess.run(
                    ["pgrep", "-u", str(uid), "-x", pattern], capture_output=True
                ).returncode
                == 0
            ):
                return True
        return False

    # 1. Validate environment
    if "WAYLAND_DISPLAY" in os.environ:
        if not _is_wayland_reachable(os.environ["WAYLAND_DISPLAY"]):
            del os.environ["WAYLAND_DISPLAY"]
            _working_methods["session_type"] = None
            needs_save = True

    if "DISPLAY" in os.environ:
        if not is_x11_active():
            del os.environ["DISPLAY"]
            _working_methods["session_type"] = None
            needs_save = True

    # 2. Force default XDG_RUNTIME_DIR if missing
    if "XDG_RUNTIME_DIR" not in os.environ:
        os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    # 3. Auto-detection using cached session_type hint
    if "WAYLAND_DISPLAY" not in os.environ and "DISPLAY" not in os.environ:
        if _working_methods["session_type"] != "X11":
            for i in range(2):
                name = f"wayland-{i}"
                if _is_wayland_reachable(name):
                    os.environ["WAYLAND_DISPLAY"] = name
                    if _working_methods["session_type"] != "Wayland":
                        _working_methods["session_type"] = "Wayland"
                        needs_save = True
                    _display_env_detected = True
                    if needs_save:
                        _save_cache()
                    return

        if is_x11_active() and os.path.exists("/tmp/.X11-unix/X0"):
            os.environ["DISPLAY"] = ":0"
            if _working_methods["session_type"] != "X11":
                _working_methods["session_type"] = "X11"
                needs_save = True

    if needs_save:
        _save_cache()
    _display_env_detected = True


def _get_hdmi_output_name():
    global _working_methods
    # 1. Use memory cache first
    if getattr(_get_hdmi_output_name, "cached", None):
        return _get_hdmi_output_name.cached

    # 2. Use persistent cache second
    if _working_methods.get("hdmi_output"):
        _get_hdmi_output_name.cached = _working_methods["hdmi_output"]
        return _working_methods["hdmi_output"]

    try:
        # 3. Slow discovery if nothing is cached
        output = subprocess.check_output(
            ["wlr-randr"], stderr=subprocess.DEVNULL, timeout=1.5
        ).decode()
        for line in output.split("\n"):
            if "HDMI" in line and (line.strip() and not line.startswith(" ")):
                name = line.split(" ")[0]
                _get_hdmi_output_name.cached = name
                _working_methods["hdmi_output"] = name
                _save_cache()
                return name
    except Exception as e:
        logging.debug(f"wlr-randr output detection failed: {e}")
    return "HDMI-A-1"  # Fallback


FB_BLANK_PATH = "/sys/class/graphics/fb0/blank"


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
    global _working_methods
    target = "ON" if state else "OFF"
    setup_display_env()
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
    if _working_methods.get("session_type"):
        # Map cached session type to a specific strategy name
        s_name = (
            "Wayland (wlr-randr)"
            if _working_methods["session_type"] == "Wayland"
            else "X11 (xset)"
        )
        name, cmd_base, condition = next(
            (s for s in session_strategies if s[0] == s_name), (None, None, None)
        )
        if name and condition() and run_strategy(name, cmd_base):
            success_count += 1
        else:
            _working_methods["session_type"] = None
            needs_save = True

    if not success_count:
        for name, cmd, condition in session_strategies:
            if condition() and run_strategy(name, cmd):
                _working_methods["session_type"] = (
                    "Wayland" if "Wayland" in name else "X11"
                )
                success_count += 1
                needs_save = True
                break

    # 2. Execute Hardware Layer
    hardware_success = False

    # Try any previously working hardware methods from cache
    if _working_methods["hardware"]:
        for name in list(_working_methods["hardware"]):
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
                _working_methods["hardware"].remove(name)
                needs_save = True

    # If no cached hardware methods worked, try discovering and executing ALL possible hardware strategies
    if not hardware_success:
        for name, cmd, condition in hardware_strategies:
            # Skip strategies we *just* tried and failed in the cached block
            if name in _working_methods["hardware"]:
                continue

            if condition() and run_strategy(name, cmd):
                logging.info(f"Discovered new display control strategy: {name}")
                _working_methods["hardware"].append(name)
                success_count += 1
                hardware_success = True
                needs_save = True
                # Break early only for premium methods (DDC/CI or CEC) to avoid double-processing
                if name in ["DDC/CI (Fast Off)", "HDMI-CEC"]:
                    break

    if needs_save:
        _save_cache()

    if success_count == 0:
        logging.error(f"Failed to set display power to {target}.")
    else:
        logging.info(f"Display set to {target} (Methods: {success_count})")
        # If turned ON, restore all last known settings (unless the screen keeps them)
        if state and RESTORE_ON_POWER_ON:
            set_display_brightness(_working_methods.get("brightness", 100), force=True)
            set_display_contrast(_working_methods.get("contrast", 50), force=True)
            set_display_color_preset(
                _working_methods.get("color_preset", DEFAULT_COLOR_PRESET)
            )


@_with_display_lock
def set_display_brightness(value: int, force=False):
    """Sets display brightness using ddcutil with caching."""
    global _working_methods
    value = max(0, min(100, int(value)))
    if not force and _working_methods.get("brightness") == value:
        return True
    logging.info(f"Setting display brightness to {value}%...")
    if _run_vcp_command("10", str(value)):
        _working_methods["brightness"] = value
        _save_cache()
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_BRIGHTNESS_STATE_TOPIC, str(value), retain=True)
        return True
    return False


@_with_display_lock
def set_display_contrast(value: int, force=False):
    """Sets display contrast using ddcutil (VCP 12)."""
    global _working_methods
    value = max(0, min(100, int(value)))
    if not force and _working_methods.get("contrast") == value:
        return True
    logging.info(f"Setting display contrast to {value}%...")
    if _run_vcp_command("12", str(value)):
        _working_methods["contrast"] = value
        _save_cache()
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_CONTRAST_STATE_TOPIC, str(value), retain=True)
        return True
    return False


@_with_display_lock
def set_display_color_preset(preset_name: str):
    """Sets display color preset (VCP 14)."""
    global _working_methods
    hex_val = COLOR_PRESETS.get(preset_name)
    if not hex_val:
        logging.warning(
            f"Unknown color preset {preset_name!r}. Valid: {list(COLOR_PRESETS)}"
        )
        return False
    logging.info(f"Setting display color preset to {preset_name} ({hex_val})...")
    if _run_vcp_command("14", "0x" + hex_val):
        _working_methods["color_preset"] = preset_name
        _save_cache()
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_COLOR_PRESET_STATE_TOPIC, preset_name, retain=True)
        return True
    return False


@_with_display_lock
def set_display_input_source(source_name: str):
    """Sets display input source (VCP 60)."""
    global _working_methods
    hex_val = INPUT_SOURCES.get(source_name)
    if not hex_val:
        logging.warning(f"Unknown input source {source_name!r}. Valid: {list(INPUT_SOURCES)}")
        return False
    logging.info(f"Switching input source to {source_name} ({hex_val})...")
    if _run_vcp_command("60", "0x" + hex_val):
        _working_methods["input_source"] = source_name
        _save_cache()
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_INPUT_SOURCE_STATE_TOPIC, source_name, retain=True)
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


def _kill_own(names):
    """Fallback cleanup: kill this user's processes whose name is exactly one of
    `names`. (pkill -f would also hit any command line that merely contains the
    word, e.g. "cog" in "recognizer.py".) Normal cleanup kills the process group.
    One pkill for all names: with -x, "a|b" must match a whole name."""
    try:
        subprocess.run(
            ["pkill", "-x", "-u", str(os.getuid()), "|".join(names)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except Exception as e:
        logging.debug(f"pkill {names} failed: {e}")


def _kill_group(proc, grace=2.0):
    """SIGTERM the process group started for `proc`, then SIGKILL if it lingers
    more than `grace` seconds."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=grace)
        perf.mark("stop: exited after SIGTERM")  # PERF
    except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
        perf.mark("stop: not gone after SIGTERM, sending SIGKILL")  # PERF
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass  # Already gone
        except Exception as e:
            logging.debug(f"SIGKILL of process group {proc.pid} failed: {e}")


def stop_current_mode():
    """Stop the current mode's client process. The labwc compositor is kept alive for fast switching."""
    global current_process
    if current_process:
        logging.info("Stopping current mode process...")
        # Only the shell modes (mirror) start a browser: no browser cleanup after a
        # Python mode (audio), which saves a pkill run on every switch.
        started_browser = any(str(arg).endswith(".sh") for arg in current_process.args)
        # The mode runs in its own session (start_new_session=True), so killing its
        # group also stops the browser it started. A kiosk browser has nothing to
        # save and needs more than 2 s to shut down cleanly on a Pi Zero 2: it only
        # gets 0.5 s (Chromium runs with --hide-crash-restore-bubble).
        _kill_group(current_process, grace=0.5 if started_browser else 2.0)
        current_process = None

        # Browsers that escaped the group (NOT labwc: it is reused). "chromium-browse"
        # is "chromium-browser" cut to the 15-character process name limit.
        if started_browser:
            _kill_own(["chromium", "chromium-browse", "cog"])
        perf.mark("stop: browser cleanup done")  # PERF

        # Brief settle for the client process to release Wayland resources
        time.sleep(0.1)


def _get_labwc_config():
    """Create a temporary labwc config to hide the cursor and optimize for kiosk mode."""
    config_dir = (
        subprocess.check_output(["mktemp", "-d", "/tmp/labwc-orchestrator-XXXXXX"])
        .decode()
        .strip()
    )
    # Using XDG_CONFIG_HOME expects a /labwc subfolder
    os.makedirs(os.path.join(config_dir, "labwc"), exist_ok=True)
    rc_xml = os.path.join(config_dir, "labwc", "rc.xml")
    with open(rc_xml, "w") as f:
        f.write(
            "<labwc_config>\n"
            "  <windowRules>\n"
            '    <windowRule identifier="*">\n'
            '      <action name="Maximize" />\n'
            "    </windowRule>\n"
            "  </windowRules>\n"
            "</labwc_config>"
        )

    return config_dir


def _ensure_labwc():
    """Start a persistent labwc compositor if not already running. Returns env dict for clients."""
    global _labwc_process, _labwc_wayland_display, labwc_config_dir, _display_env_detected

    # Check if existing labwc is still alive and its socket reachable
    if _labwc_process and _labwc_process.poll() is None:
        if _labwc_wayland_display and _is_wayland_reachable(_labwc_wayland_display):
            logging.debug("Reusing persistent labwc compositor.")
            return _labwc_wayland_display
        # Process alive but socket dead — tear down
        logging.warning("labwc process alive but socket unreachable, restarting.")
        _stop_labwc()

    # Looked up on PATH each time (cheap, no subprocess): a path read back from the
    # cache file would be run as-is, whatever that file says.
    labwc_bin = shutil.which("labwc")
    if not labwc_bin:
        raise RuntimeError("labwc not found on PATH (sudo apt install labwc)")

    # Create config directory (reuse if still exists)
    if not labwc_config_dir or not os.path.exists(labwc_config_dir):
        labwc_config_dir = _get_labwc_config()

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = labwc_config_dir
    env["XCURSOR_SIZE"] = "0"
    env["COG_PLATFORM_FDO_SHOW_CURSOR"] = "0"

    # Clean up stale wayland sockets so labwc can claim wayland-0 cleanly.
    # On Pi Zero 2, stale socket files from previous crashed sessions block labwc from
    # creating its own socket (name collision), causing the socket detection to time out.
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        for f in os.listdir(runtime_dir):
            if f.startswith("wayland-") and not f.endswith(".lock"):
                if not _is_wayland_reachable(f):
                    stale = os.path.join(runtime_dir, f)
                    try:
                        os.remove(stale)
                        logging.debug(f"Removed stale Wayland socket: {stale}")
                    except OSError:
                        pass
                    # Also remove the companion .lock file
                    lock = stale + ".lock"
                    if os.path.exists(lock):
                        try:
                            os.remove(lock)
                        except OSError:
                            pass
    except OSError:
        pass

    # Launch labwc with a long-lived no-op startup command.
    # labwc requires -s to fully initialize its Wayland socket on DRM/KMS backends (Pi Zero 2).
    # "sleep infinity" keeps labwc alive as a bare compositor ready to accept client connections.
    _labwc_process = subprocess.Popen(
        [labwc_bin, "-s", "sleep infinity"], env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    logging.info(f"Started persistent labwc compositor (PID {_labwc_process.pid}).")

    # Wait for labwc's Wayland socket to become reachable (max ~10s).
    # First cold-start on Pi Zero 2 can take 6-8s (shared libs, DRM init, shader compile).
    # Subsequent starts are ~2s thanks to warm page cache.
    for attempt in range(100):
        if _labwc_process.poll() is not None:
            rc = _labwc_process.returncode
            _labwc_process = None
            raise RuntimeError(f"labwc exited during startup with code {rc}")

        for name in ["wayland-0", "wayland-1", "wayland-2"]:
            if _is_wayland_reachable(name):
                _labwc_wayland_display = name
                os.environ["WAYLAND_DISPLAY"] = name
                _display_env_detected = True
                logging.info(f"labwc Wayland socket ready: {name} (after {(attempt+1)*0.1:.1f}s)")
                threading.Thread(target=_labwc_hide_cursor, daemon=True).start()
                return name

        if attempt % 10 == 9:
            logging.debug(f"labwc socket poll #{attempt+1}: still waiting...")
        time.sleep(0.1)

    raise RuntimeError("labwc started but no Wayland socket detected within 10s")


def _labwc_hide_cursor():
    """Tell the persistent labwc to hide its cursor (fire-and-forget, best-effort)."""
    try:
        subprocess.run(
            ["labwc-msg", "action", "HideCursor"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except Exception as e:
        logging.debug(f"labwc-msg HideCursor failed: {e}")


def _stop_labwc():
    """Tear down the persistent labwc compositor (only called for 'off' mode or error recovery)."""
    global _labwc_process, _labwc_wayland_display, labwc_config_dir, _display_env_detected

    if _labwc_process:
        logging.info("Stopping persistent labwc compositor.")
        _kill_group(_labwc_process)
        _labwc_process = None

    _labwc_wayland_display = None

    if labwc_config_dir and os.path.exists(labwc_config_dir):
        try:
            shutil.rmtree(labwc_config_dir)
        except Exception as e:
            logging.debug(f"Could not remove {labwc_config_dir}: {e}")
        labwc_config_dir = None

    # Orphaned labwc processes of this user (exact name only)
    _kill_own(["labwc"])

    if "WAYLAND_DISPLAY" in os.environ:
        del os.environ["WAYLAND_DISPLAY"]
    if "DISPLAY" in os.environ:
        del os.environ["DISPLAY"]
    _display_env_detected = False

    time.sleep(0.3)  # Brief settle for DRM/kernel resource release


def _publish(topic, payload, retain=False):
    """Publish if MQTT is connected (state updates are best effort)."""
    client = mqtt_client
    if client and client.is_connected():
        try:
            client.publish(topic, payload, retain=retain)
        except Exception as e:
            logging.debug(f"MQTT publish to {topic} failed: {e}")


def _set_run_state(state):
    """Change the run state and publish it (call with _mode_lock held)."""
    global _run_state
    if state != _run_state:
        logging.info(f"Run state: {_run_state} -> {state}")
    _run_state = state
    _publish(MQTT_RUN_STATE_TOPIC, state, retain=True)


def _go_off(run_state):
    """Screen off, mode "off" (call with _mode_lock held). labwc is kept only for a
    normal "off" with keep_compositor_when_off: after a failure it restarts fresh."""
    global current_mode
    stop_current_mode()
    if run_state != RUN_OFF or not KEEP_COMPOSITOR_WHEN_OFF:
        _stop_labwc()
    set_display_power(False)
    current_mode = "off"
    _publish(MQTT_STATE_TOPIC, current_mode, retain=True)
    _set_run_state(run_state)


def _mode_failed(reason):
    """The mode process could not start or has exited (call with _mode_lock held).

    Relaunch the same mode after a growing delay (RESTART_DELAYS), keeping the
    screen and labwc as they are; after the last try, give up and switch off.
    """
    global _restart_attempts, _pending_restart
    mono = time.monotonic()
    if _mode_started_at and mono - _mode_started_at > RESTART_STABLE_AFTER:
        _restart_attempts = 0  # It had been running fine: start counting again
    if _restart_attempts < len(RESTART_DELAYS):
        delay = RESTART_DELAYS[_restart_attempts]
        _restart_attempts += 1
        logging.error(
            f"FAIL-SAFE: Mode '{current_mode}' {reason}. Relaunching in {delay}s "
            f"(try {_restart_attempts}/{len(RESTART_DELAYS)})."
        )
        _pending_restart = (current_mode, mono + delay)
        _set_run_state(RUN_RETRYING)
    else:
        logging.error(
            f"FAIL-SAFE: Mode '{current_mode}' {reason} after {len(RESTART_DELAYS)} "
            "relaunches. Resetting to 'off'."
        )
        _restart_attempts = 0
        _pending_restart = None
        _go_off(RUN_FAILED)


def supervise_mode():
    """Watchdog tick, from the main loop (call with _mode_lock held): notice a crashed
    mode process, mark a mode as running once it has lived a moment, fire retries."""
    global current_process, _pending_restart
    proc = current_process  # Snapshot: the global can change
    if current_mode != "off" and proc is not None:
        code = proc.poll()
        if code is not None:
            current_process = None
            _mode_failed(f"exited (code {code})")
        elif _run_state == RUN_STARTING and time.monotonic() - _mode_started_at >= RUNNING_AFTER:
            _set_run_state(RUN_RUNNING)

    if _pending_restart and time.monotonic() >= _pending_restart[1]:
        retry_mode = _pending_restart[0]
        _pending_restart = None
        command_queue.put(("restart", retry_mode))


def start_mode(mode, restart=False):
    """Switch to `mode` (call with _mode_lock held). restart=True is the watchdog
    relaunching a crashed mode."""
    global current_process, current_mode
    global _restart_attempts, _pending_restart, _mode_started_at, _last_switch_at

    mode = str(mode).lower()
    # Same mode and still running: nothing to do. (Same mode with no process means
    # it crashed and waits for a retry: choosing it again relaunches it now.)
    if mode == current_mode and (mode == "off" or current_process is not None):
        _publish(MQTT_STATE_TOPIC, current_mode, retain=True)
        return
    _last_switch_at = time.monotonic()

    if not restart:
        # A choice made by the user (or at startup) cancels any pending crash retry.
        _restart_attempts = 0
        _pending_restart = None

    if mode != "off" and mode not in get_available_modes() and mode not in get_available_modes(refresh=True):
        logging.warning(f"Unknown mode: {mode}. Available modes: {get_available_modes()}")
        _go_off(RUN_FAILED)
        return

    previous_mode = current_mode
    logging.info(f"Transitioning mode: {previous_mode} -> {mode}")
    perf.begin_switch(mode)  # PERF

    # 1. Publish the choice IMMEDIATELY so the HA selector reacts at once. Whether the
    #    mode really runs is published separately (run state: starting -> running).
    current_mode = mode
    _publish(MQTT_STATE_TOPIC, current_mode, retain=True)

    if mode == "off":
        logging.info("Ensuring display power is OFF.")
        _go_off(RUN_OFF)
        perf.mark("old_mode_stopped")  # PERF
        return

    _set_run_state(RUN_STARTING)
    _mode_started_at = time.monotonic()

    # 2. Stop the current mode's client process (labwc compositor stays alive).
    stop_current_mode()
    perf.mark("old_mode_stopped")  # PERF

    try:
        current_process = _launch_mode(mode, previous_mode)
    except Exception as e:
        current_process = None
        _mode_failed(f"could not start ({e})")
        return
    _mode_started_at = time.monotonic()
    perf.mark("process_launched")  # PERF


def _launch_mode(mode, previous_mode):
    """Turn the screen on, make sure labwc runs, start the mode process. Returns it."""
    # 1. Start display power ON and labwc compositor IN PARALLEL.
    #    On Pi Zero 2 cold boot, labwc takes 6-8s to create its Wayland socket.
    #    Overlapping with display power-on (~3s) hides most of that latency.
    display_thread = None
    if previous_mode == "off":
        display_thread = threading.Thread(target=set_display_power, args=(True,), daemon=True)
        display_thread.start()

    setup_display_env()
    labwc_thread = None
    _labwc_start_error = [None]  # Mutable container for thread result
    if _labwc_process is not None or (
        not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY")
    ):
        def _start_labwc_parallel():
            try:
                _ensure_labwc()
            except Exception as e:
                _labwc_start_error[0] = e
        labwc_thread = threading.Thread(target=_start_labwc_parallel, daemon=True)
        labwc_thread.start()

    # 2. Resolve mode script (runs while display + labwc warm up).
    py_script = os.path.join(MODES_DIR, f"{mode}_mode.py")
    sh_script = os.path.join(MODES_DIR, f"{mode}_mode.sh")
    if os.path.exists(sh_script):
        base_cmd = ["bash", sh_script]
    elif os.path.exists(py_script):
        base_cmd = [sys.executable, py_script]
    else:
        raise RuntimeError(f"no script for mode '{mode}' in {MODES_DIR}")

    # 3. Wait for display power ON and labwc to be ready.
    if display_thread:
        display_thread.join(timeout=4.0)
    if labwc_thread:
        labwc_thread.join(timeout=12.0)
    perf.mark("display_and_labwc_ready")  # PERF

    # 4. Prepare mode-specific environment variables.
    env = os.environ.copy()
    audio_idx = _discover_audio_device()
    env["SMARTFRAME_AUDIO_DEVICE"] = str(audio_idx if audio_idx is not None else "")
    # Lets the audio mode check that the index still points to the same mic
    if AUDIO_CONFIG.get("device_index") is None and _working_methods.get("audio_device_name"):
        env["SMARTFRAME_AUDIO_DEVICE_NAME"] = _working_methods["audio_device_name"]
    else:
        env.pop("SMARTFRAME_AUDIO_DEVICE_NAME", None)
    env["SMARTFRAME_DEBUG"] = "1" if DEBUG_MODE else "0"
    # Python buffers print() when not writing to a terminal: without this, the mode's
    # messages only reach journald when it stops.
    env["PYTHONUNBUFFERED"] = "1"
    env["SMARTFRAME_SCREEN_SIZE"] = SCREEN_SIZE
    if mode == "mirror":
        env["MIRROR_URL"] = (config.get("magic_mirror") or {}).get(
            "url", "http://localhost:8080"
        )

    # Apply labwc result (sets WAYLAND_DISPLAY for the mode process).
    if labwc_thread:
        if _labwc_start_error[0]:
            logging.warning(
                f"Persistent labwc session failed ({_labwc_start_error[0]}). Attempting direct launch."
            )
        elif _labwc_wayland_display:
            env["WAYLAND_DISPLAY"] = _labwc_wayland_display

    # 5. Audio with audio.preload: wake up the process that is already waiting.
    if mode == "audio":
        proc = _start_preloaded_audio(env)
        if proc is not None:
            logging.info(f"Launching 'audio' mode (preloaded process, PID {proc.pid}).")
            return proc

    # 6. Launch the mode process. The audio mode gets the write end of a private
    #    pipe for its sound levels (only that process: pass_fds, not inherited by
    #    anything else). We close our copy so EOF arrives when it exits.
    level_r = level_w = None
    if mode == "audio":
        level_r, level_w = os.pipe()
        env["SMARTFRAME_LEVEL_FD"] = str(level_w)
    logging.info(f"Launching '{mode}' mode (cmd: {' '.join(base_cmd)}).")
    try:
        proc = subprocess.Popen(
            base_cmd, env=env, start_new_session=True, stdout=None, stderr=None,
            pass_fds=(level_w,) if level_w is not None else (),
        )
    except Exception:
        if level_r is not None:
            os.close(level_r)
        raise
    finally:
        if level_w is not None:
            os.close(level_w)
    if level_r is not None:
        threading.Thread(target=_read_level_pipe, args=(level_r,), daemon=True).start()
    return proc


def _spawn_audio_preload():
    """Start audio_mode.py ahead of time (audio.preload). It does its slow imports,
    then waits on stdin for the start signal (_start_preloaded_audio), without
    touching the screen or the mic. Call with _mode_lock held."""
    global _preloaded_audio
    script = os.path.join(MODES_DIR, "audio_mode.py")
    if not os.path.exists(script):
        return
    level_r, level_w = os.pipe()
    env = os.environ.copy()
    env["SMARTFRAME_PRELOAD"] = "1"
    env["SMARTFRAME_LEVEL_FD"] = str(level_w)
    env["PYTHONUNBUFFERED"] = "1"  # Read when Python starts: must be set now
    try:
        _preloaded_audio = subprocess.Popen(
            [sys.executable, script], env=env, start_new_session=True,
            stdin=subprocess.PIPE, stdout=None, stderr=None, pass_fds=(level_w,),
        )
    except Exception as e:
        os.close(level_r)
        _preload_failed(f"could not start it ({e})")
        return
    finally:
        os.close(level_w)
    threading.Thread(target=_read_level_pipe, args=(level_r,), daemon=True).start()
    logging.info(f"Audio mode preloaded in the background (PID {_preloaded_audio.pid}).")


def _start_preloaded_audio(env):
    """Hand the mode's environment to the waiting audio process and let it start.
    Returns the process, or None if there is none ready (normal launch then)."""
    global _preloaded_audio, _preload_failures
    proc, _preloaded_audio = _preloaded_audio, None
    if proc is None:
        return None
    if proc.poll() is None:
        start_env = {k: v for k, v in env.items() if k != "SMARTFRAME_LEVEL_FD"}
        try:
            proc.stdin.write((json.dumps(start_env) + "\n").encode("utf-8"))
            proc.stdin.close()
            _preload_failures = 0
            return proc
        except OSError as e:
            logging.warning(f"Preloaded audio process unusable ({e}): normal launch.")
    else:
        logging.warning(f"Preloaded audio process had exited (code {proc.returncode}): normal launch.")
    _kill_group(proc, grace=0.5)
    return None


def _stop_audio_preload():
    """Stop the waiting audio process, if any (call with _mode_lock held)."""
    global _preloaded_audio
    proc, _preloaded_audio = _preloaded_audio, None
    if proc is not None and proc.poll() is None:
        _kill_group(proc, grace=0.5)


def _preload_failed(reason):
    """A preload could not start or died while waiting (broken install, out of RAM...):
    wait longer before each new try (40 s, 80 s... up to PRELOAD_RETRY_MAX)."""
    global _preload_failures, _preload_next_try
    _preload_failures += 1
    delay = min(PRELOAD_RETRY_MAX, PRELOAD_AFTER * 2 ** _preload_failures)
    _preload_next_try = time.monotonic() + delay
    logging.warning(f"Audio preload failed: {reason}. Next try in {delay:.0f} s.")


def _maybe_preload_audio():
    """Main-loop tick (call with _mode_lock held): keep one audio process waiting
    while another mode (or "off") is shown, once that mode has settled."""
    global _preloaded_audio
    if not AUDIO_PRELOAD or current_mode == "audio":
        return
    if _preloaded_audio is not None:
        if _preloaded_audio.poll() is None:
            return
        _preload_failed(f"the waiting process exited (code {_preloaded_audio.returncode})")
        _preloaded_audio = None
        return
    now = time.monotonic()
    if (_run_state in (RUN_OFF, RUN_RUNNING) and now - _last_switch_at >= PRELOAD_AFTER
            and now >= _preload_next_try):
        _spawn_audio_preload()


# MQTT Callbacks
DISCOVERY_DELAY = 3.0  # Seconds between clearing the old entities and announcing the new ones


def _discovery_topics():
    return {
        "command": MQTT_COMMAND_TOPIC,
        "state": MQTT_STATE_TOPIC,
        "status": MQTT_STATUS_TOPIC,
        "run_state": MQTT_RUN_STATE_TOPIC,
        "dba": MQTT_DBA_STATE_TOPIC,
        "lafmax": MQTT_LAFMAX_STATE_TOPIC,
        "cpu_temp": MQTT_CPU_TEMP_STATE_TOPIC,
        "started": MQTT_STARTED_STATE_TOPIC,
        "restart": MQTT_RESTART_COMMAND_TOPIC,
        "brightness_cmd": MQTT_BRIGHTNESS_COMMAND_TOPIC,
        "brightness_state": MQTT_BRIGHTNESS_STATE_TOPIC,
        "contrast_cmd": MQTT_CONTRAST_COMMAND_TOPIC,
        "contrast_state": MQTT_CONTRAST_STATE_TOPIC,
        "color_preset_cmd": MQTT_COLOR_PRESET_COMMAND_TOPIC,
        "color_preset_state": MQTT_COLOR_PRESET_STATE_TOPIC,
        "input_source_cmd": MQTT_INPUT_SOURCE_COMMAND_TOPIC,
        "input_source_state": MQTT_INPUT_SOURCE_STATE_TOPIC,
    }


def clear_legacy_discovery(client):
    """Remove the entities of older versions (fixed IDs, node "smartframe"): an empty
    retained config tells HA to delete them. Harmless when they are already gone."""
    for topic in ha.legacy_discovery_topics(MQTT_DISCOVERY_PREFIX):
        client.publish(topic, "", retain=True)


def publish_discovery_and_status(client):
    if not client.is_connected():
        return
    modes = get_available_modes()
    client.publish(MQTT_STATUS_TOPIC, "online", retain=True)
    client.publish(MQTT_AVAILABLE_MODES_TOPIC, json.dumps(modes), retain=True)
    for topic, payload in ha.discovery_messages(
        MQTT_DISCOVERY_PREFIX, DEVICE_ID, _discovery_topics(), modes,
        COLOR_PRESETS, INPUT_SOURCES,
    ):
        client.publish(topic, json.dumps(payload), retain=True)
    logging.info(
        f"Published Home Assistant MQTT discovery payloads (device '{DEVICE_ID}')."
    )


def _publish_states(client):
    """Every state, retained. States are published when they change: this is only
    for a new connection or a Home Assistant restart."""
    if not client.is_connected():
        return
    client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
    client.publish(MQTT_RUN_STATE_TOPIC, _run_state, retain=True)
    client.publish(MQTT_BRIGHTNESS_STATE_TOPIC, str(_working_methods.get("brightness", 100)), retain=True)
    client.publish(MQTT_CONTRAST_STATE_TOPIC, str(_working_methods.get("contrast", 50)), retain=True)
    client.publish(
        MQTT_COLOR_PRESET_STATE_TOPIC,
        _working_methods.get("color_preset", DEFAULT_COLOR_PRESET), retain=True,
    )
    client.publish(MQTT_INPUT_SOURCE_STATE_TOPIC, _working_methods.get("input_source", "HDMI-1"), retain=True)
    _publish_started(client, force=True)
    if _last_cpu_temp is not None:
        client.publish(MQTT_CPU_TEMP_STATE_TOPIC, f"{_last_cpu_temp:.1f}", retain=True)


def _announce_later(client):
    """Discovery (then the states) after DISCOVERY_DELAY seconds, in a timer thread."""
    def announce():
        publish_discovery_and_status(client)
        _publish_states(client)
    timer = threading.Timer(DISCOVERY_DELAY, announce)
    timer.daemon = True
    timer.start()


def on_connect(client, userdata, _connect_flags, reason_code, _properties):
    if not reason_code.is_failure:
        logging.info("Connected to MQTT broker.")
        perf.mark("mqtt_connected")  # PERF
        for topic in _command_routes():
            client.subscribe(topic)
        client.subscribe(MQTT_HA_STATUS_TOPIC)
        logging.info(f"Subscribed to the command topics (device '{DEVICE_ID}').")

        client.publish(MQTT_STATUS_TOPIC, "online", retain=True)
        _publish_states(client)
        # Old entities first; the new ones a moment later, so HA has deleted the old
        # ones and gives the new ones the same entity_id (dashboards keep working).
        clear_legacy_discovery(client)
        _announce_later(client)
    else:
        # No disconnect(): it would stop paho's automatic reconnection for good. paho
        # retries with a growing delay (reconnect_delay_set), so a fixed password or
        # ACL on the broker is picked up without restarting the frame.
        _log_connect_failure(f"MQTT connection refused by the broker: {reason_code}.")
        if reason_code in ["Not authorized", "Bad user name or password"]:
            _log_connect_failure("MQTT authentication failed. Check mqtt.username / "
                                 "mqtt.password in config.yaml (and the broker's users).")


_last_conn_fail_log = 0.0
_refused_logged_at = 0.0
_refused_logged = set()


def _log_connect_failure(message):
    """Log a refused connection at most once a minute (paho retries every few seconds:
    the same error would fill journald, which writes to the SD card)."""
    global _refused_logged_at
    now = time.time()
    if now - _refused_logged_at >= 60:
        _refused_logged_at = now
        _refused_logged.clear()
    if message not in _refused_logged:
        _refused_logged.add(message)
        logging.error(message)


def on_connect_fail(client, userdata):
    """paho only reports TCP-level failures at DEBUG level: probe the broker and say why."""
    global _last_conn_fail_log
    now = time.time()
    if now - _last_conn_fail_log < 60:  # paho retries every few seconds: don't flood the log
        return
    _last_conn_fail_log = now
    try:
        socket.getaddrinfo(MQTT_BROKER, MQTT_PORT, type=socket.SOCK_STREAM)
    except OSError as e:
        logging.error(f"MQTT: cannot resolve broker name '{MQTT_BROKER}': {e}")
        return
    try:
        socket.create_connection((MQTT_BROKER, MQTT_PORT), timeout=5).close()
        logging.error(
            f"MQTT: TCP connection to {MQTT_BROKER}:{MQTT_PORT} works but the MQTT "
            "handshake failed (TLS setting or port mismatch?)."
        )
    except OSError as e:
        logging.error(f"MQTT: cannot reach {MQTT_BROKER}:{MQTT_PORT}: {e}")


def on_disconnect(client, userdata, _flags, reason_code, _properties):
    logging.warning(f"MQTT disconnected ({reason_code}). Will retry automatically.")


def command_worker():
    """Background worker that executes slow hardware or process commands sequentially
    to avoid blocking the main MQTT loop thread."""
    while True:
        try:
            cmd, payload = command_queue.get()
            if cmd == "mode":
                with _mode_lock:
                    start_mode(payload)
            elif cmd == "restart":
                # Watchdog retry: only if nothing else happened in the meantime.
                with _mode_lock:
                    if current_mode == payload and current_process is None:
                        logging.info(f"Relaunching crashed mode '{payload}'.")
                        start_mode(payload, restart=True)
            elif cmd == "display_off":
                # Startup in "off". With keep_compositor_when_off, labwc is started now
                # (screen still off), as if a mode had run: the first switch then skips
                # its 5-6 s cold start, like every later switch from "off".
                with _mode_lock:
                    if KEEP_COMPOSITOR_WHEN_OFF and current_mode == "off":
                        setup_display_env()
                    # Same rule as _launch_mode: no labwc inside an existing desktop session
                    if (KEEP_COMPOSITOR_WHEN_OFF and current_mode == "off"
                            and not os.environ.get("WAYLAND_DISPLAY")
                            and not os.environ.get("DISPLAY")):
                        try:
                            _ensure_labwc()
                        except Exception as e:
                            logging.warning(f"Could not pre-start labwc ({e}). "
                                            "It will start with the first mode.")
                    set_display_power(False)
            elif cmd == "brightness":
                set_display_brightness(payload)
            elif cmd == "contrast":
                set_display_contrast(payload)
            elif cmd == "color_preset":
                set_display_color_preset(payload)
            elif cmd == "input_source":
                set_display_input_source(payload)
            elif cmd == "service_restart":
                restart_service()
        except Exception:
            logging.exception("Command worker error")


def restart_service():
    """HA "Restart" button: stop everything, then exit with an error code so that
    systemd (Restart=on-failure) starts the service again ~10 s later. The service
    has no sudo, so it cannot ask systemd to restart it in any other way."""
    if not os.environ.get("INVOCATION_ID"):
        logging.warning("Restart requested, but this process does not run under systemd: "
                        "it will exit and nothing will start it again.")
    logging.warning(f"Restart requested from Home Assistant: exiting (code {RESTART_EXIT_CODE}), "
                    "systemd starts the service again.")
    with _mode_lock:
        if audio_monitor_thread:
            audio_monitor_thread.running = False
        stop_current_mode()
        _stop_audio_preload()
        _stop_labwc()
    client = mqtt_client
    if client and client.is_connected():
        try:
            # A clean disconnect() does not send the "offline" last will: send it here.
            client.publish(MQTT_STATUS_TOPIC, "offline", retain=True).wait_for_publish(2)
            client.disconnect()
        except Exception as e:
            logging.debug(f"MQTT goodbye failed: {e}")
    logging.shutdown()
    os._exit(RESTART_EXIT_CODE)


def _command_routes():
    """Command topic -> command name for the worker."""
    return {
        MQTT_COMMAND_TOPIC: "mode",
        MQTT_BRIGHTNESS_COMMAND_TOPIC: "brightness",
        MQTT_CONTRAST_COMMAND_TOPIC: "contrast",
        MQTT_COLOR_PRESET_COMMAND_TOPIC: "color_preset",
        MQTT_INPUT_SOURCE_COMMAND_TOPIC: "input_source",
        MQTT_RESTART_COMMAND_TOPIC: "service_restart",
    }


def on_message(client, userdata, msg):
    # Any exception escaping this callback would stop paho's network thread for good
    # (the frame would go offline until restarted). Never let one bad message do that.
    try:
        _handle_message(msg)
    except Exception:
        logging.exception(f"Error while handling MQTT message on {msg.topic!r}")


def _handle_message(msg):
    if msg.topic == MQTT_HA_STATUS_TOPIC:
        # Home Assistant (re)started: it may have lost the entities, so announce them
        # again. Its own retained "online" also arrives on every connection: skipped,
        # on_connect already announces.
        if msg.payload.strip() == b"online" and not msg.retain:
            logging.info("Home Assistant is back online: sending discovery again.")
            _announce_later(mqtt_client)
        return
    cmd = _command_routes().get(msg.topic)
    if cmd is None:
        return
    if cmd == "service_restart" and msg.retain:
        # A retained "PRESS" comes back on every connection: obeying it would restart
        # the service forever. A real button press is never retained.
        logging.warning(
            f"Ignoring a retained restart request on {msg.topic}. Clear it with: "
            f"mosquitto_pub -t '{msg.topic}' -r -n"
        )
        return
    # Debug level: an HA slider sends many messages (journald writes to the SD card)
    logging.debug(f"Message received on {msg.topic}: {msg.payload[:100]!r}")
    command = parse_command(cmd, msg.payload, get_available_modes())
    if command is None and cmd == "mode":
        # Maybe a mode added since startup: look at the modes directory again.
        known = get_available_modes()
        command = parse_command(cmd, msg.payload, get_available_modes(refresh=True))
        if get_available_modes() != known:
            logging.info(f"Modes changed: {get_available_modes()}.")
            _announce_later(mqtt_client)
    if command is None:
        logging.warning(
            f"Invalid {cmd} payload on {msg.topic}: {msg.payload[:100]!r}"
            + (f" (available modes: {get_available_modes()})" if cmd == "mode" else "")
        )
        return
    command_queue.put(command)


_last_cpu_temp = None  # Last CPU temperature sent to HA (°C), or None


def _publish_started(client, force=False):
    """Send the start time (ISO 8601). Without `force`, only if the wall clock moved
    by more than a minute since the last value (NTP sync after boot)."""
    global _started_sent
    started = time.time() - (time.monotonic() - _START_MONOTONIC)
    if not force and _started_sent is not None and abs(started - _started_sent) < 60:
        return
    if client and client.is_connected():
        stamp = datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds")
        client.publish(MQTT_STARTED_STATE_TOPIC, stamp, retain=True)
        _started_sent = started


def read_cpu_temp():
    """CPU temperature in °C, or None if this system does not report it."""
    try:
        with open(CPU_TEMP_PATH) as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def publish_cpu_temp():
    """Send the CPU temperature if it moved by 0.5 °C or more since the last value."""
    global _last_cpu_temp
    temp = read_cpu_temp()
    if temp is None:
        return
    if _last_cpu_temp is None or abs(temp - _last_cpu_temp) >= 0.5:
        _last_cpu_temp = temp
        _publish(MQTT_CPU_TEMP_STATE_TOPIC, f"{temp:.1f}", retain=True)


def signal_handler(sig, frame):
    logging.info(f"Signal {sig} received. Shutting down orchestrator...")
    global audio_monitor_thread
    if audio_monitor_thread:
        audio_monitor_thread.running = False
    stop_current_mode()
    _stop_audio_preload()
    _stop_labwc()
    sys.exit(0)


if __name__ == "__main__":
    # Parse command line arguments
    import argparse

    parser = argparse.ArgumentParser(description="SmartFrame Orchestrator")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    if args.debug:
        DEBUG_MODE = True
        os.environ["SMARTFRAME_DEBUG"] = "1"
        logging.getLogger().setLevel(logging.DEBUG)
        logging.debug("CLI DEBUG FLAG DETECTED: Full process logs enabled.")

    # Register handlers for clean exit
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    # Never let an exception in a callback kill paho's network thread. paho then only
    # reports callback errors through its logger, so attach one.
    mqtt_client.suppress_exceptions = True
    mqtt_client.enable_logger(logging.getLogger("paho"))
    # After a lost connection or a refusal (wrong password...): retry after 1 s,
    # then double the wait each time, up to 2 minutes.
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=120)

    has_credentials = bool(MQTT_USER) and MQTT_USER != "[MQTT_USERNAME]"
    if has_credentials:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    else:
        logging.warning(
            "MQTT: no username/password configured. Anyone on the network who can "
            "reach the broker can control this frame. Set mqtt.username and "
            "mqtt.password in config.yaml (and require auth on the broker)."
        )

    if MQTT_TLS:
        try:
            mqtt_client.tls_set(ca_certs=MQTT_CA_CERT)
            logging.info("MQTT: TLS enabled.")
        except Exception as e:
            logging.error(
                f"MQTT: TLS setup failed ({e}). Check mqtt.ca_cert in config.yaml. "
                "Starting in OFFLINE mode."
            )
            MQTT_BROKER = "[MQTT_SERVER_IP_ADDRESS]"  # Skip connecting below
    elif has_credentials:
        logging.warning(
            "MQTT: TLS is disabled, so the password and all commands travel in clear "
            "text on the network. Set mqtt.tls: true (usually port 8883) if your "
            "broker supports it."
        )

    mqtt_client.will_set(MQTT_STATUS_TOPIC, "offline", retain=True)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_connect_fail = on_connect_fail
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message

    # Note: the i2c-dev kernel module (needed by ddcutil) is loaded at boot through
    # /etc/modules-load.d/smartframe.conf, installed by scripts/setup_pi.sh.
    if sys.platform.startswith("linux") and not any(
        f.startswith("i2c-") for f in os.listdir("/dev")
    ):
        logging.warning(
            "No /dev/i2c-* device found: DDC/CI display control will not work. "
            "Re-run scripts/setup_pi.sh (or: sudo modprobe i2c-dev)."
        )

    logging.info("--- SmartFrame Orchestrator starting ---")

    # Pre-discover audio device in background (avoids 1-2s probe on first mode switch)
    threading.Thread(target=_discover_audio_device, daemon=True).start()

    # 2. Start command worker thread (Handle all transitions sequentially)
    worker = threading.Thread(target=command_worker, daemon=True)
    worker.start()
    logging.debug("Command worker thread started.")

    # 3. Connect to MQTT with automatic retry
    try:
        if MQTT_BROKER != "[MQTT_SERVER_IP_ADDRESS]":
            # We use connect_async + loop_start to ensure startup isn't blocked by the broker
            mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_client.loop_start()
            logging.info(f"MQTT network loop started (Broker: {MQTT_BROKER}).")
        else:
            logging.warning("MQTT broker IP not configured. Starting in OFFLINE mode.")
    except Exception as e:
        logging.error(f"Error initiating MQTT connection: {e}")

    # 4. Request initial state from configuration via the worker queue
    # This prevents the initial mode from racing with MQTT retained 'set_mode' messages.
    # The screen is only switched off when the first mode is "off": done through the
    # same queue, so it can never land after (and undo) a mode that turns it on.
    default_mode = config.get("default_mode", "off")
    if str(default_mode).lower() == "off":
        command_queue.put(("display_off", None))
    else:
        command_queue.put(("mode", default_mode))
    logging.info(f"Queued initial mode request: {default_mode}")

    # 5. Start background dBA monitor thread
    audio_monitor_thread = AudioMonitor(config, mqtt_client)
    audio_monitor_thread.start()

    last_cpu_read = 0.0

    try:
        # --- GLOBAL FAIL-SAFE & SYNC LOOP ---
        while True:
            now = time.time()
            # One bad iteration must never stop the whole orchestrator.
            try:
                # 1. Watchdog: crashed mode -> retry / off, starting -> running.
                # Skip this tick if a mode switch is in progress: the worker is
                # stopping/starting processes and the state is in between.
                if _mode_lock.acquire(blocking=False):
                    try:
                        supervise_mode()
                        _maybe_preload_audio()
                    finally:
                        _mode_lock.release()

                # 2. CPU temperature (diagnostic sensor), sent only when it changes.
                # States are retained and published when they change, and discovery is
                # sent again when HA restarts (homeassistant/status): no periodic resend.
                if now - last_cpu_read >= CPU_TEMP_EVERY:
                    last_cpu_read = now
                    publish_cpu_temp()
                    _publish_started(mqtt_client)  # Only if the clock jumped (NTP)
            except Exception:
                logging.exception("Orchestrator loop iteration failed (continuing).")

            time.sleep(1)

    except KeyboardInterrupt:
        logging.info("Shutting down...")
    except Exception as e:
        logging.critical(f"FATAL: Orchestrator loop crashed: {e}")
    finally:
        stop_current_mode()
        _stop_audio_preload()
        _stop_labwc()
        set_display_power(False)
        if mqtt_client:
            mqtt_client.loop_stop()
        logging.info("--- SmartFrame Orchestrator terminated ---")
