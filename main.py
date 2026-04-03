import os
import subprocess
import sys
import time
import json
import yaml
import paho.mqtt.client as mqtt
import logging
import signal
import socket
import threading
import queue
import numpy as np
import pyaudio

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
if not os.path.exists(config_path):
    logging.error(
        "config.yaml not found. Copy config.example.yaml to config.yaml and fill in your settings."
    )
    sys.exit(1)
with open(config_path, "r") as file:
    config = yaml.safe_load(file)

# Configure logging based on debug setting
DEBUG_MODE = config.get("debug", False)
logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
if DEBUG_MODE:
    os.environ["SMARTFRAME_DEBUG"] = "1"
    logging.debug("DEBUG MODE ENABLED: Subprocess output will be visible.")

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

mqtt_config = config.get("mqtt", {})
MQTT_BROKER = mqtt_config.get("broker", "[MQTT_SERVER_IP_ADDRESS]")
MQTT_PORT = mqtt_config.get("port", 1883)
MQTT_COMMAND_TOPIC = mqtt_config.get("topic", "smartframe/set_mode")
MQTT_STATE_TOPIC = mqtt_config.get("state_topic", "smartframe/mode_state")
MQTT_STATUS_TOPIC = mqtt_config.get("status_topic", "smartframe/status")
MQTT_AVAILABLE_MODES_TOPIC = mqtt_config.get(
    "available_modes_topic", "smartframe/modes_available"
)
MQTT_DISCOVERY_PREFIX = mqtt_config.get("discovery_prefix", "homeassistant")
MQTT_BRIGHTNESS_COMMAND_TOPIC = mqtt_config.get(
    "brightness_topic", "smartframe/set_brightness"
)
MQTT_BRIGHTNESS_STATE_TOPIC = mqtt_config.get(
    "brightness_state_topic", "smartframe/brightness_state"
)
MQTT_CONTRAST_COMMAND_TOPIC = mqtt_config.get(
    "contrast_topic", "smartframe/set_contrast"
)
MQTT_CONTRAST_STATE_TOPIC = mqtt_config.get(
    "contrast_state_topic", "smartframe/contrast_state"
)
MQTT_COLOR_PRESET_COMMAND_TOPIC = mqtt_config.get(
    "color_preset_topic", "smartframe/set_color_preset"
)
MQTT_COLOR_PRESET_STATE_TOPIC = mqtt_config.get(
    "color_preset_state_topic", "smartframe/color_preset_state"
)
MQTT_INPUT_SOURCE_COMMAND_TOPIC = mqtt_config.get(
    "input_source_topic", "smartframe/set_input_source"
)
MQTT_INPUT_SOURCE_STATE_TOPIC = mqtt_config.get(
    "input_source_state_topic", "smartframe/input_source_state"
)
MQTT_DBA_STATE_TOPIC = mqtt_config.get("dba_state_topic", "smartframe/audio/dba")
MQTT_USER = mqtt_config.get("username")
MQTT_PASS = mqtt_config.get("password")

MODES_DIR = os.path.join(os.path.dirname(__file__), "modes")

# Globals for state management
current_process = None
current_mode = "off"
mqtt_client = None
labwc_config_dir = None
audio_monitor_thread = None
command_queue = queue.Queue()

# Discovery cache to avoid repeated slow subprocess calls
CACHE_FILE = os.path.join(os.path.dirname(__file__), ".smartframe_cache")
CHROMIUM_PROFILE_DIR = os.path.join(os.path.dirname(__file__), ".chromium_profile")

_working_methods = {
    "session_type": None,
    "hdmi_output": None,
    "labwc_path": None,
    "hardware": [],
    "brightness": 100,
    "contrast": 50,
    "color_preset": "6500 K",
    "input_source": "HDMI-1",
    "audio_device": None,
}

available_modes_cache = []


def get_available_modes():
    """List all available mode names from the modes directory (with memory caching)."""
    global available_modes_cache
    if available_modes_cache:
        return available_modes_cache

    modes = ["off"]
    if os.path.exists(MODES_DIR):
        for f in os.listdir(MODES_DIR):
            if f.endswith("_mode.py") or f.endswith("_mode.sh"):
                mode_name = f.replace("_mode.py", "").replace("_mode.sh", "")
                if mode_name not in modes:
                    modes.append(mode_name)
    available_modes_cache = sorted(modes)
    return available_modes_cache


def _discover_audio_device():
    """Finds the best audio input device once and caches it (saves 1-2s of hardware probing)."""
    global _working_methods
    if _working_methods.get("audio_device") is not None:
        return _working_methods["audio_device"]

    try:
        import pyaudio

        p = pyaudio.PyAudio()
        found_index = None

        # Priority 1: Specifically look for the I2S hardware
        device_count = p.get_device_count()
        logging.debug(f"Probing {device_count} audio devices for I2S hardware...")
        
        for i in range(device_count):
            try:
                info = p.get_device_info_by_index(i)
                name = info.get('name', '').lower()
                max_in = info.get('maxInputChannels', 0)
                logging.debug(f"Testing device {i}: '{name}' (Max Inputs: {max_in})")
                
                if max_in > 0:
                    if any(x in name for x in ['i2s', 'googlevoicehat', 'mono', 'inmp']):
                        found_index = i
                        logging.info(f"Discovered and matched I2S hardware: {info.get('name')} at index {i}.")
                        break
                    else:
                        logging.debug(f"  - Device '{name}' does not match I2S keywords. Skipping.")
                else:
                    logging.debug(f"  - Device '{name}' has no input channels. Skipping.")
            except Exception as e:
                logging.debug(f"  - Error probing device {i}: {e}")
                continue
                    
        # Priority 2: Fallback to default input
        if found_index is None:
            logging.debug("No direct I2S match found. Attempting to use system default input...")
            try:
                default_info = p.get_default_input_device_info()
                found_index = default_info.get('index')
                logging.info(f"Falling back to system default audio input: {default_info.get('name')} (index {found_index})")
            except Exception as e:
                logging.debug(f"Default input acquisition failed: {e}")
                pass

        p.terminate()
        if found_index is not None:
            _working_methods["audio_device"] = found_index
            _save_cache()
            return found_index
    except ImportError:
        pass
    except Exception as e:
        logging.debug(f"Audio discovery failed: {e}")
    return None


class AudioMonitor(threading.Thread):
    """Background thread that monitors ambient dB levels and reports to MQTT."""

    def __init__(self, config, mq_client):
        super().__init__(daemon=True)
        self.config = config
        self.mqtt_client = mq_client
        self.running = True
        self.chunk = 4096
        self.rate = 48000
        self.audio_config = config.get("audio", {})
        self.dba_topic = config.get("mqtt", {}).get(
            "dba_state_topic", "smartframe/audio/dba"
        )
        self.calibration_offset = self.audio_config.get("calibration_offset_db", 0)
        self.last_publish = 0

    def _get_a_weighting_gains(self, rate, chunk):
        freqs = np.fft.rfftfreq(chunk, 1.0 / rate)
        valid_freqs = np.where(freqs == 0, 1e-10, freqs)
        f_sq = valid_freqs**2
        const = (12194**2) * (f_sq**2)
        den = (
            (f_sq + 20.6**2)
            * np.sqrt((f_sq + 107.7**2) * (f_sq + 737.9**2))
            * (f_sq + 12194**2)
        )
        w = const / den
        w *= 1.2589
        w[0] = 0.0
        return w

    def run(self):
        logging.info("Audio Monitor background thread started.")
        p = pyaudio.PyAudio()
        stream = None
        bridge_file = "/tmp/smartframe_dba"
        a_gains = self._get_a_weighting_gains(self.rate, self.chunk)
        ema_a = 0.0
        alpha = 1.0 - np.exp(-(self.chunk / self.rate) / 0.125)  # Fast integration

        while self.running:
            # 1. First, check if Audio Mode is writing the value to our IPC bridge
            use_bridge = False
            db_a = None
            if os.path.exists(bridge_file):
                try:
                    # Check if the file is recent (less than 5 seconds old)
                    if time.time() - os.path.getmtime(bridge_file) < 5.0:
                        with open(bridge_file, "r") as f:
                            val = f.read().strip()
                            if val:
                                db_a = float(val)
                                use_bridge = True
                                # If we were using the mic, close it to free resources for audio_mode
                                if stream:
                                    try:
                                        stream.stop_stream()
                                        stream.close()
                                    except:
                                        pass
                                    stream = None
                except Exception as e:
                    logging.debug(f"Monitor: Bridge file read failed: {e}")

            if not use_bridge:
                # 2. No bridge file. Check if Audio Mode is active.
                # If Audio Mode is starting/running, we MUST NOT hold the mic.
                if current_mode == "audio":
                    if stream:
                        try:
                            stream.stop_stream()
                            stream.close()
                        except:
                            pass
                        stream = None
                    # Wait for Audio Mode to start and create the bridge file
                    time.sleep(0.2)
                    continue

                # 3. Try to use the microphone directly.
                if not stream:
                    try:
                        idx = _discover_audio_device()
                        if idx is not None:
                            stream = p.open(
                                format=pyaudio.paInt16,
                                channels=1,
                                rate=self.rate,
                                input=True,
                                input_device_index=idx,
                                frames_per_buffer=self.chunk,
                            )
                        else:
                            time.sleep(5)
                            continue
                    except Exception as e:
                        logging.debug(f"Monitor: Failed to open device (busy?): {e}")
                        time.sleep(5)
                        continue

                try:
                    raw_data = stream.read(self.chunk, exception_on_overflow=False)
                    data = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32)

                    fft_complex = np.fft.rfft(data)
                    fft_aw = fft_complex * a_gains
                    data_aw = np.fft.irfft(fft_aw)
                    rms_a = np.sqrt(np.mean(data_aw**2))

                    if ema_a == 0:
                        ema_a = rms_a
                    else:
                        ema_a += alpha * (rms_a - ema_a)

                    db_a = (20 * np.log10(max(1e-9, ema_a))) + self.calibration_offset

                    if db_a < 45:
                        correction = 8.0 * (1.0 - (max(30, db_a) - 30) / 15)
                        db_a -= correction

                except Exception as e:
                    logging.debug(f"Monitor Mic Loop Error: {e}")
                    if stream:
                        try:
                            stream.stop_stream()
                            stream.close()
                        except:
                            pass
                        stream = None
                    time.sleep(2)
                    continue

            # 3. Publish result to MQTT (throttled to 1s for better responsiveness)
            now = time.time()
            if now - self.last_publish >= 1.0 and db_a is not None:
                if self.mqtt_client and self.mqtt_client.is_connected():
                    try:
                        self.mqtt_client.publish(
                            self.dba_topic, f"{db_a:.1f}", retain=False
                        )
                        self.last_publish = now
                    except Exception:
                        pass

            # Simple sleep to reduce CPU usage if we're not waiting on audio stream read
            if use_bridge:
                time.sleep(0.5)

        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except:
                pass
        p.terminate()
        # Cleanup bridge file on exit
        if os.path.exists(bridge_file):
            try:
                os.remove(bridge_file)
            except:
                pass


def _load_cache():
    global _working_methods
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _working_methods.update(data)
                logging.debug("Loaded display discovery cache.")
        except Exception:
            pass


def _save_cache():
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(_working_methods, f)
    except Exception:
        pass


_display_env_detected = False


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

    def is_wayland_reachable(display_name):
        """Check if a Wayland socket is actually listening."""
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        socket_path = os.path.join(runtime_dir, display_name)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.connect(socket_path)
            return True
        except Exception:
            return False

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
        if not is_wayland_reachable(os.environ["WAYLAND_DISPLAY"]):
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
                if is_wayland_reachable(name):
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
    except Exception:
        pass
    return "HDMI-A-1"  # Fallback


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
            ["sudo", "ddcutil", "setvcp", "D6", "0x01" if state else "0x04"],
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
            [
                "sudo",
                "sh",
                "-c",
                f"echo {'0' if state else '1'} > /sys/class/graphics/fb0/blank",
            ],
            lambda: os.path.exists("/sys/class/graphics/fb0/blank"),
        ),
    ]

    def run_strategy(name, cmd):
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
        # If turned ON, restore all last known settings
        if state:
            set_display_brightness(_working_methods.get("brightness", 100), force=True)
            set_display_contrast(_working_methods.get("contrast", 50), force=True)
            set_display_color_preset(
                _working_methods.get("color_preset", "Natural (6500 K)")
            )


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


def set_display_color_preset(preset_name: str):
    """Sets display color preset (VCP 14)."""
    global _working_methods
    presets = {
        "sRGB": "01",
        "Natural (6500 K)": "05",
        "Warm (5000 K)": "04",
        "Cool (9300 K)": "08",
    }
    hex_val = presets.get(preset_name)
    if not hex_val:
        return False
    logging.info(f"Setting display color preset to {preset_name} ({hex_val})...")
    if _run_vcp_command("14", "0x" + hex_val):
        _working_methods["color_preset"] = preset_name
        _save_cache()
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_COLOR_PRESET_STATE_TOPIC, preset_name, retain=True)
        return True
    return False


def set_display_input_source(source_name: str):
    """Sets display input source (VCP 60)."""
    global _working_methods
    sources = {"HDMI-1": "11", "HDMI-2": "12", "DisplayPort-1": "0f", "VGA": "01"}
    hex_val = sources.get(source_name)
    if not hex_val:
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
    cmd = ["sudo", "ddcutil", "setvcp", vcp_code, value]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=3.5)
        return res.returncode == 0
    except Exception as e:
        logging.debug(f"ddcutil VCP {vcp_code} failed: {e}")
        return False

    return False


def stop_current_mode():
    global current_process
    if current_process:
        logging.info("Stopping current mode process...")
        try:
            # Kill the entire process group
            os.killpg(os.getpgid(current_process.pid), signal.SIGTERM)
            current_process.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
            try:
                os.killpg(os.getpgid(current_process.pid), signal.SIGKILL)
            except Exception:
                pass
        current_process = None

        # Cleanup temporary config directory if it exists
        global labwc_config_dir
        if labwc_config_dir and os.path.exists(labwc_config_dir):
            import shutil

            try:
                shutil.rmtree(labwc_config_dir)
            except Exception:
                pass
            labwc_config_dir = None

        # Clear specific display environment variables as the session they belonged to is now dead
        if "WAYLAND_DISPLAY" in os.environ:
            del os.environ["WAYLAND_DISPLAY"]
        if "DISPLAY" in os.environ:
            del os.environ["DISPLAY"]

        global _display_env_detected
        _display_env_detected = False  # Allow re-detection for the next mode


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


def start_mode(mode):
    global current_process, current_mode, mqtt_client

    mode = mode.lower()
    if mode == current_mode:
        # Always publish state confirmation even if already in mode to keep HA in sync
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
        return

    logging.info(f"Transitioning mode: {current_mode} -> {mode}")

    # 1. Update state variable and publish intent IMMEDIATELY.
    # This ensures that the MQTT thread (or HA) sees the change instantly,
    # even while the hardware/process transitions are happening in the background.
    current_mode = mode
    if mqtt_client and mqtt_client.is_connected():
        mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)

    # 2. Stop the current mode.
    stop_current_mode()

    # Small delay for kernel/TTY/DRM handshake settling
    time.sleep(2.0)

    if mode == "off":
        logging.info("Ensuring display power is OFF.")
        set_display_power(False)
        return  # State already published above
    else:
        # Pre-emptive power ON (so the next mode doesn't start in the dark)
        set_display_power(True)
        # Wait for monitor to wake up and DRM/I2C to be ready
        time.sleep(1.5)

        modes_dir = os.path.join(os.path.dirname(__file__), "modes")
        py_script = os.path.join(modes_dir, f"{mode}_mode.py")
        sh_script = os.path.join(modes_dir, f"{mode}_mode.sh")

        base_cmd = []
        if os.path.exists(sh_script):
            base_cmd = ["bash", sh_script]
        elif os.path.exists(py_script):
            base_cmd = [sys.executable, py_script]
        else:
            available = get_available_modes()
            logging.warning(f"Unknown mode: {mode}. Available modes: {available}")
            current_mode = "off"
            set_display_power(False)
            if mqtt_client and mqtt_client.is_connected():
                mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
            return

        # 3. Intelligent Session Wrapping:
        setup_display_env()
        final_cmd = base_cmd

        if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            try:
                # Use cached path or find it once
                labwc_bin = _working_methods.get("labwc_path")
                if not labwc_bin:
                    labwc_bin = (
                        subprocess.check_output(["which", "labwc"]).decode().strip()
                    )
                    _working_methods["labwc_path"] = labwc_bin
                    _save_cache()

                global labwc_config_dir
                labwc_config_dir = _get_labwc_config()

                env = os.environ.copy()

                # Auto-discover and cache the audio input device
                audio_idx = _discover_audio_device()
                env["SMARTFRAME_AUDIO_DEVICE"] = str(
                    audio_idx if audio_idx is not None else ""
                )
                env["SMARTFRAME_DEBUG"] = "1" if DEBUG_MODE else "0"

                if mode == "mirror":
                    env["MIRROR_URL"] = config.get("magic_mirror", {}).get(
                        "url", "http://localhost:8080"
                    )

                cmd_str = " ".join(base_cmd)
                env["XDG_CONFIG_HOME"] = labwc_config_dir
                env["XCURSOR_SIZE"] = "0"
                env["COG_PLATFORM_FDO_SHOW_CURSOR"] = "0"
                env["MIRROR_URL"] = config.get("magic_mirror", {}).get(
                    "url", "http://localhost:8080"
                )
                env["SMARTFRAME_AUDIO_DEVICE"] = str(
                    config.get("audio", {}).get("device_index", "")
                )

                final_cmd = [labwc_bin, "-s", cmd_str]
                logging.info(
                    f"Wrapping mode '{mode}' in a managed Wayland session (labwc)."
                )
                current_process = subprocess.Popen(
                    final_cmd, env=env, start_new_session=True, stdout=None, stderr=None
                )
                return

            except Exception as e:
                logging.warning(
                    f"labwc session wrapping failed ({e}). Attempting direct launch."
                )

        logging.info(
            f"Executing {mode.capitalize()} mode command: {' '.join(final_cmd)}"
        )
        current_process = subprocess.Popen(final_cmd, start_new_session=True)


# MQTT Callbacks
def publish_discovery_and_status(client):
    modes = get_available_modes()

    # 1. Publish status (online)
    client.publish(MQTT_STATUS_TOPIC, "online", retain=True)

    # 2. Publish list of available modes
    client.publish(MQTT_AVAILABLE_MODES_TOPIC, json.dumps(modes), retain=True)

    # 3. Publish Home Assistant Discovery payload for Mode Selector
    discovery_topic = f"{MQTT_DISCOVERY_PREFIX}/select/smartframe/mode/config"
    discovery_payload = {
        "name": "SmartFrame Mode",
        "unique_id": "smartframe_mode_selector",
        "command_topic": MQTT_COMMAND_TOPIC,
        "state_topic": MQTT_STATE_TOPIC,
        "options": modes,
        "availability": [{"topic": MQTT_STATUS_TOPIC}],
        "device": {
            "identifiers": ["smartframe_orchestrator"],
            "name": "Smart Frame",
            "manufacturer": "Custom",
            "model": "Smart Frame V1",
        },
    }
    client.publish(discovery_topic, json.dumps(discovery_payload), retain=True)

    # 4. Publish Discovery for Brightness, Contrast, Color Preset, and Input
    client.publish(
        f"{MQTT_DISCOVERY_PREFIX}/number/smartframe/brightness/config",
        json.dumps(
            {
                "name": "SmartFrame Brightness",
                "unique_id": "sf_brightness",
                "command_topic": MQTT_BRIGHTNESS_COMMAND_TOPIC,
                "state_topic": MQTT_BRIGHTNESS_STATE_TOPIC,
                "min": 0,
                "max": 100,
                "step": 5,
                "unit_of_measurement": "%",
                "icon": "mdi:brightness-6",
                "availability": [{"topic": MQTT_STATUS_TOPIC}],
                "device": {"identifiers": ["smartframe_orchestrator"]},
            }
        ),
        retain=True,
    )

    client.publish(
        f"{MQTT_DISCOVERY_PREFIX}/number/smartframe/contrast/config",
        json.dumps(
            {
                "name": "SmartFrame Contrast",
                "unique_id": "sf_contrast",
                "command_topic": MQTT_CONTRAST_COMMAND_TOPIC,
                "state_topic": MQTT_CONTRAST_STATE_TOPIC,
                "min": 0,
                "max": 100,
                "step": 5,
                "unit_of_measurement": "%",
                "icon": "mdi:contrast",
                "availability": [{"topic": MQTT_STATUS_TOPIC}],
                "device": {"identifiers": ["smartframe_orchestrator"]},
            }
        ),
        retain=True,
    )

    client.publish(
        f"{MQTT_DISCOVERY_PREFIX}/select/smartframe/color_preset/config",
        json.dumps(
            {
                "name": "SmartFrame Color Profile",
                "unique_id": "sf_color_preset",
                "command_topic": MQTT_COLOR_PRESET_COMMAND_TOPIC,
                "state_topic": MQTT_COLOR_PRESET_STATE_TOPIC,
                "options": [
                    "sRGB",
                    "Natural (6500 K)",
                    "Warm (5000 K)",
                    "Cool (9300 K)",
                ],
                "icon": "mdi:palette",
                "availability": [{"topic": MQTT_STATUS_TOPIC}],
                "device": {"identifiers": ["smartframe_orchestrator"]},
            }
        ),
        retain=True,
    )

    client.publish(
        f"{MQTT_DISCOVERY_PREFIX}/select/smartframe/input_source/config",
        json.dumps(
            {
                "name": "SmartFrame Input",
                "unique_id": "sf_input",
                "command_topic": MQTT_INPUT_SOURCE_COMMAND_TOPIC,
                "state_topic": MQTT_INPUT_SOURCE_STATE_TOPIC,
                "options": ["HDMI-1", "HDMI-2", "DisplayPort-1", "VGA"],
                "icon": "mdi:video-input-hdmi",
                "availability": [{"topic": MQTT_STATUS_TOPIC}],
                "device": {"identifiers": ["smartframe_orchestrator"]},
            }
        ),
        retain=True,
    )

    client.publish(
        f"{MQTT_DISCOVERY_PREFIX}/sensor/smartframe/dba/config",
        json.dumps(
            {
                "name": "SmartFrame Ambient Sound",
                "unique_id": "sf_audio_dba",
                "state_topic": MQTT_DBA_STATE_TOPIC,
                "unit_of_measurement": "dBA",
                "value_template": "{{ value }}",
                "icon": "mdi:ear-hearing",
                "availability": [{"topic": MQTT_STATUS_TOPIC}],
                "device": {"identifiers": ["smartframe_orchestrator"]},
            }
        ),
        retain=True,
    )

    logging.info(
        "Published Home Assistant MQTT discovery payloads (Universal Display Control + Audio)."
    )


def on_connect(client, userdata, _connect_flags, reason_code, _properties):
    global current_mode
    if not reason_code.is_failure:
        logging.info("Connected to MQTT broker.")
        client.subscribe(MQTT_COMMAND_TOPIC)
        client.subscribe(MQTT_BRIGHTNESS_COMMAND_TOPIC)
        client.subscribe(MQTT_CONTRAST_COMMAND_TOPIC)
        client.subscribe(MQTT_COLOR_PRESET_COMMAND_TOPIC)
        client.subscribe(MQTT_INPUT_SOURCE_COMMAND_TOPIC)
        logging.info("Subscribed to all universal display control topics.")

        publish_discovery_and_status(client)

        # Publish current state on connect
        if current_mode:
            client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)

        # Publish current display settings on connect
        client.publish(
            MQTT_BRIGHTNESS_STATE_TOPIC,
            str(_working_methods.get("brightness", 100)),
            retain=True,
        )
        client.publish(
            MQTT_CONTRAST_STATE_TOPIC,
            str(_working_methods.get("contrast", 50)),
            retain=True,
        )
        client.publish(
            MQTT_COLOR_PRESET_STATE_TOPIC,
            _working_methods.get("color_preset", "Natural (6500 K)"),
            retain=True,
        )
        client.publish(
            MQTT_INPUT_SOURCE_STATE_TOPIC,
            _working_methods.get("input_source", "HDMI-1"),
            retain=True,
        )
    else:
        logging.error(f"Failed to connect, reason code: {reason_code}")
        if reason_code in ["Not authorized", "Bad user name or password"]:
            logging.error("MQTT authentication failed. Check your config.yaml.")
            client.disconnect()


def command_worker():
    """Background worker that executes slow hardware or process commands sequentially
    to avoid blocking the main MQTT loop thread."""
    while True:
        try:
            cmd, payload = command_queue.get()
            if cmd == "mode":
                start_mode(payload)
            elif cmd == "brightness":
                set_display_brightness(payload)
            elif cmd == "contrast":
                set_display_contrast(payload)
            elif cmd == "color_preset":
                set_display_color_preset(payload)
            elif cmd == "input_source":
                set_display_input_source(payload)
            command_queue.task_done()
        except Exception as e:
            logging.error(f"Command worker error: {e}")


def on_message(client, userdata, msg):
    # For general commands, we want to preserve case (especially for presets)
    payload_raw = msg.payload.decode("utf-8").strip()
    payload_lower = payload_raw.lower()

    logging.info(f"Message received on {msg.topic}: {payload_raw}")

    if msg.topic == MQTT_COMMAND_TOPIC:
        available_modes = get_available_modes()
        if payload_lower in available_modes:
            command_queue.put(("mode", payload_lower))
        else:
            logging.warning(
                f"Invalid mode payload '{payload_raw}'. Available modes: {available_modes}"
            )

    elif msg.topic == MQTT_BRIGHTNESS_COMMAND_TOPIC:
        try:
            command_queue.put(("brightness", int(payload_raw)))
        except ValueError:
            logging.warning(f"Invalid brightness payload: {payload_raw}")

    elif msg.topic == MQTT_CONTRAST_COMMAND_TOPIC:
        try:
            command_queue.put(("contrast", int(payload_raw)))
        except ValueError:
            logging.warning(f"Invalid contrast payload: {payload_raw}")

    elif msg.topic == MQTT_COLOR_PRESET_COMMAND_TOPIC:
        # Use raw payload to match preset names exactly
        command_queue.put(("color_preset", payload_raw))

    elif msg.topic == MQTT_INPUT_SOURCE_COMMAND_TOPIC:
        # Use raw payload to match source names exactly
        command_queue.put(("input_source", payload_raw))


def signal_handler(sig, frame):
    logging.info(f"Signal {sig} received. Shutting down orchestrator...")
    global audio_monitor_thread
    if audio_monitor_thread:
        audio_monitor_thread.running = False
    stop_current_mode()
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
    if MQTT_USER and MQTT_USER != "[MQTT_USERNAME]":
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

    mqtt_client.will_set(MQTT_STATUS_TOPIC, "offline", retain=True)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    # Ensure kernel modules (optional but helpful for future I2C/DDC support)
    try:
        subprocess.run(
            ["sudo", "modprobe", "i2c-dev"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    logging.info("--- SmartFrame Orchestrator starting ---")

    # 1. Initial State: Screen off until a mode starts
    # We run this in a thread because ddcutil can occasionally hang if the I2C bus is locked,
    # and we don't want to block the entire orchestrator startup.
    threading.Thread(target=set_display_power, args=(False,), daemon=True).start()

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
    # This prevents the initial mode from racing with MQTT retained 'set_mode' messages
    default_mode = config.get("default_mode", "off")
    command_queue.put(("mode", default_mode))
    logging.info(f"Queued initial mode request: {default_mode}")

    # 5. Start background dBA monitor thread
    audio_monitor_thread = AudioMonitor(config, mqtt_client)
    audio_monitor_thread.start()

    last_sync_time = time.time()
    last_discovery_time = time.time()

    try:
        # --- GLOBAL FAIL-SAFE & SYNC LOOP (State Guardian) ---
        while True:
            # Explicitly declare globals to ensure we are syncing the correct state
            global current_mode, current_process
            now = time.time()

            # 1. Watchdog: Check if the current mode process has crashed unexpectedly
            if current_mode != "off" and current_process:
                poll_res = current_process.poll()
                if poll_res is not None:
                    # SMART CHECK: If dBA bridge is still being updated, don't declare crash yet
                    bridge_file = "/tmp/smartframe_dba"
                    is_active = False
                    if os.path.exists(bridge_file) and (now - os.path.getmtime(bridge_file) < 5.0):
                        is_active = True
                        
                    if not is_active:
                        logging.error(f"FAIL-SAFE: Mode process '{current_mode}' exited (code {poll_res}). Resetting to 'off'.")
                        set_display_power(False)
                        current_mode = "off"
                        current_process = None
                        if mqtt_client and mqtt_client.is_connected():
                            mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)

            # 2. Smart Detection: Sync state if we see 'phantom' activity
            bridge_file = "/tmp/smartframe_dba"
            if os.path.exists(bridge_file):
                try:
                    if now - os.path.getmtime(bridge_file) < 5.0:
                        if current_mode == "off":
                            logging.warning("GUARDIAN: Audio bridge active but state is 'off'. Syncing to 'audio'.")
                            current_mode = "audio"
                            if mqtt_client and mqtt_client.is_connected():
                                mqtt_client.publish(MQTT_STATE_TOPIC, "audio", retain=True)
                except Exception:
                    pass

            # 3. Aggressive UI Sync: Re-publish state every 5s to keep HA dashboard accurate
            if now - last_sync_time > 5.0:
                if mqtt_client and mqtt_client.is_connected():
                    mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
                    mqtt_client.publish(MQTT_BRIGHTNESS_STATE_TOPIC, str(_working_methods.get("brightness", 100)), retain=True)
                last_sync_time = now
                
            # 4. Global Re-announcement: Re-publish discovery every 5 mins
            if now - last_discovery_time > 300.0:
                if mqtt_client and mqtt_client.is_connected():
                    publish_discovery_and_status(mqtt_client)
                    last_discovery_time = now

            time.sleep(1)

    except KeyboardInterrupt:
        logging.info("Shutting down...")
    except Exception as e:
        logging.critical(f"FATAL: Orchestrator loop crashed: {e}")
    finally:
        stop_current_mode()
        set_display_power(False)
        if mqtt_client:
            mqtt_client.loop_stop()
        logging.info("--- SmartFrame Orchestrator terminated ---")
