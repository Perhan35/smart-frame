"""Settings read once at startup: config.yaml, logging, MQTT topics and constants.

Importing this module loads and checks config.yaml and sets up logging, so it is
imported first (by main.py). Everything here is read-only afterwards, except
DEBUG_MODE (main.py turns it on with --debug): read it as settings.DEBUG_MODE.
"""

import logging
import os
import sys

import yaml

from smartframe import config_check, ha

# The repository folder (config.yaml, modes/, caches), wherever the service runs from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load configuration. SMARTFRAME_CONFIG / SMARTFRAME_CACHE point elsewhere (tests).
config_path = os.environ.get("SMARTFRAME_CONFIG") or os.path.join(ROOT, "config.yaml")
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

MODES_DIR = os.path.join(ROOT, "modes")
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

CPU_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"
CPU_TEMP_EVERY = 60.0  # Seconds between two CPU temperature readings
# Exit code used by the HA "Restart" button: systemd (Restart=on-failure) starts
# the service again. 75 = EX_TEMPFAIL ("try again later").
RESTART_EXIT_CODE = 75

# Keep labwc running in "off" (screen still switched off): the next mode starts
# without the 6-8 s cold start, at the cost of some RAM.
KEEP_COMPOSITOR_WHEN_OFF = bool(config.get("keep_compositor_when_off", True))
# Send the saved brightness, contrast and color profile again each time the screen
# turns on. Turn it off if your screen keeps its settings while off: switching on is
# then faster, but changes made with the screen's own buttons are no longer undone.
RESTORE_ON_POWER_ON = config.get("restore_display_settings_on_power_on", True) is not False
# Screen size given to the Mirror browser (Cog) for its first frame
SCREEN_SIZE = str(config.get("screen_size") or "1920x1080")

AUDIO_CONFIG = config.get("audio") or {}

# Discovery cache to avoid repeated slow subprocess calls
CACHE_FILE = os.environ.get("SMARTFRAME_CACHE") or os.path.join(ROOT, ".smartframe_cache")
CHROMIUM_PROFILE_DIR = os.path.join(ROOT, ".chromium_profile")

COLOR_PRESETS = {
    "sRGB": "01",
    "Natural (6500 K)": "05",
    "Warm (5000 K)": "04",
    "Cool (9300 K)": "08",
}
DEFAULT_COLOR_PRESET = "Natural (6500 K)"
INPUT_SOURCES = {"HDMI-1": "11", "HDMI-2": "12", "DisplayPort-1": "0f", "VGA": "01"}
