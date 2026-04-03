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

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
if not os.path.exists(config_path):
    logging.error("config.yaml not found. Copy config.example.yaml to config.yaml and fill in your settings.")
    sys.exit(1)
with open(config_path, 'r') as file:
    config = yaml.safe_load(file)

# Configure logging based on debug setting
DEBUG_MODE = config.get('debug', False)
logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
if DEBUG_MODE:
    os.environ['SMARTFRAME_DEBUG'] = '1'
    logging.debug("DEBUG MODE ENABLED: Subprocess output will be visible.")

os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'

mqtt_config = config.get('mqtt', {})
MQTT_BROKER = mqtt_config.get('broker', '[MQTT_SERVER_IP_ADDRESS]')
MQTT_PORT = mqtt_config.get('port', 1883)
MQTT_COMMAND_TOPIC = mqtt_config.get('topic', 'smartframe/set_mode')
MQTT_STATE_TOPIC = mqtt_config.get('state_topic', 'smartframe/mode_state')
MQTT_STATUS_TOPIC = mqtt_config.get('status_topic', 'smartframe/status')
MQTT_AVAILABLE_MODES_TOPIC = mqtt_config.get('available_modes_topic', 'smartframe/modes_available')
MQTT_DISCOVERY_PREFIX = mqtt_config.get('discovery_prefix', 'homeassistant')
MQTT_BRIGHTNESS_COMMAND_TOPIC = mqtt_config.get('brightness_topic', 'smartframe/set_brightness')
MQTT_BRIGHTNESS_STATE_TOPIC = mqtt_config.get('brightness_state_topic', 'smartframe/brightness_state')
MQTT_CONTRAST_COMMAND_TOPIC = mqtt_config.get('contrast_topic', 'smartframe/set_contrast')
MQTT_CONTRAST_STATE_TOPIC = mqtt_config.get('contrast_state_topic', 'smartframe/contrast_state')
MQTT_COLOR_PRESET_COMMAND_TOPIC = mqtt_config.get('color_preset_topic', 'smartframe/set_color_preset')
MQTT_COLOR_PRESET_STATE_TOPIC = mqtt_config.get('color_preset_state_topic', 'smartframe/color_preset_state')
MQTT_INPUT_SOURCE_COMMAND_TOPIC = mqtt_config.get('input_source_topic', 'smartframe/set_input_source')
MQTT_INPUT_SOURCE_STATE_TOPIC = mqtt_config.get('input_source_state_topic', 'smartframe/input_source_state')
MQTT_USER = mqtt_config.get('username')
MQTT_PASS = mqtt_config.get('password')

MODES_DIR = os.path.join(os.path.dirname(__file__), 'modes')

# Globals for state management
current_process = None
current_mode = 'off'
mqtt_client = None
labwc_config_dir = None

# Discovery cache to avoid repeated slow subprocess calls
CACHE_FILE = os.path.join(os.path.dirname(__file__), '.smartframe_cache')
CHROMIUM_PROFILE_DIR = os.path.join(os.path.dirname(__file__), '.chromium_profile')

_working_methods = {
    'session_type': None,
    'hdmi_output': None,
    'labwc_path': None,
    'hardware': [],
    'brightness': 100,
    'contrast': 50,
    'color_preset': '6500 K',
    'input_source': 'HDMI-1',
    'audio_device': None
}

available_modes_cache = []

def get_available_modes():
    """List all available mode names from the modes directory (with memory caching)."""
    global available_modes_cache
    if available_modes_cache:
        return available_modes_cache
    
    modes = ['off']
    if os.path.exists(MODES_DIR):
        for f in os.listdir(MODES_DIR):
            if f.endswith('_mode.py') or f.endswith('_mode.sh'):
                mode_name = f.replace('_mode.py', '').replace('_mode.sh', '')
                if mode_name not in modes:
                    modes.append(mode_name)
    available_modes_cache = sorted(modes)
    return available_modes_cache

def _discover_audio_device():
    """Finds the best audio input device once and caches it (saves 1-2s of hardware probing)."""
    global _working_methods
    if _working_methods.get('audio_device') is not None:
        return _working_methods['audio_device']
        
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        found_index = None
        
        # Priority 1: Specifically look for the I2S hardware
        for i in range(p.get_device_count()):
            try:
                info = p.get_device_info_by_index(i)
                name = info.get('name', '').lower()
                if info.get('maxInputChannels', 0) > 0:
                    if any(x in name for x in ['i2s', 'googlevoicehat', 'mono', 'inmp']):
                        found_index = i
                        logging.info(f"Discovered I2S hardware mic: {info.get('name')} (index {i}).")
                        break
            except Exception:
                continue
                    
        # Priority 2: Fallback to default input
        if found_index is None:
            try:
                found_index = p.get_default_input_device_info().get('index')
            except Exception:
                pass
            
        p.terminate()
        if found_index is not None:
            _working_methods['audio_device'] = found_index
            _save_cache()
            return found_index
    except ImportError:
        pass
    except Exception as e:
        logging.debug(f"Audio discovery failed: {e}")
    return None

def _load_cache():
    global _working_methods
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _working_methods.update(data)
                logging.debug("Loaded display discovery cache.")
        except Exception:
            pass

def _save_cache():
    try:
        with open(CACHE_FILE, 'w') as f:
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
        runtime_dir = os.environ.get('XDG_RUNTIME_DIR', f"/run/user/{uid}")
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
        for pattern in ['Xorg', 'X']:
            if subprocess.run(['pgrep', '-u', str(uid), '-x', pattern], capture_output=True).returncode == 0:
                return True
        return False

    # 1. Validate environment
    if 'WAYLAND_DISPLAY' in os.environ:
        if not is_wayland_reachable(os.environ['WAYLAND_DISPLAY']):
            del os.environ['WAYLAND_DISPLAY']
            _working_methods['session_type'] = None
            needs_save = True

    if 'DISPLAY' in os.environ:
        if not is_x11_active():
            del os.environ['DISPLAY']
            _working_methods['session_type'] = None
            needs_save = True

    # 2. Force default XDG_RUNTIME_DIR if missing
    if 'XDG_RUNTIME_DIR' not in os.environ:
        os.environ['XDG_RUNTIME_DIR'] = f"/run/user/{uid}"

    # 3. Auto-detection using cached session_type hint
    if 'WAYLAND_DISPLAY' not in os.environ and 'DISPLAY' not in os.environ:
        if _working_methods['session_type'] != 'X11':
            for i in range(2):
                name = f'wayland-{i}'
                if is_wayland_reachable(name):
                    os.environ['WAYLAND_DISPLAY'] = name
                    if _working_methods['session_type'] != 'Wayland':
                        _working_methods['session_type'] = 'Wayland'
                        needs_save = True
                    _display_env_detected = True
                    if needs_save:
                        _save_cache()
                    return
        
        if is_x11_active() and os.path.exists('/tmp/.X11-unix/X0'):
            os.environ['DISPLAY'] = ':0'
            if _working_methods['session_type'] != 'X11':
                _working_methods['session_type'] = 'X11'
                needs_save = True

    if needs_save:
        _save_cache()
    _display_env_detected = True

def _get_hdmi_output_name():
    global _working_methods
    # 1. Use memory cache first
    if getattr(_get_hdmi_output_name, 'cached', None):
        return _get_hdmi_output_name.cached
    
    # 2. Use persistent cache second
    if _working_methods.get('hdmi_output'):
        _get_hdmi_output_name.cached = _working_methods['hdmi_output']
        return _working_methods['hdmi_output']
        
    try:
        # 3. Slow discovery if nothing is cached
        output = subprocess.check_output(['wlr-randr'], stderr=subprocess.DEVNULL, timeout=1.5).decode()
        for line in output.split('\n'):
            if 'HDMI' in line and (line.strip() and not line.startswith(' ')):
                name = line.split(' ')[0]
                _get_hdmi_output_name.cached = name
                _working_methods['hdmi_output'] = name
                _save_cache()
                return name
    except Exception:
        pass
    return "HDMI-A-1" # Fallback

def set_display_power(state: bool):
    """Sets display power using discovered strategies, optimizing for speed and reliability."""
    global _working_methods
    target = "ON" if state else "OFF"
    setup_display_env()
    output_name = _get_hdmi_output_name()
    
    # Strategy Definitions
    session_strategies = [
        ("Wayland (wlr-randr)", 
         ['wlr-randr', '--output', output_name, '--on' if state else '--off'], 
         lambda: 'WAYLAND_DISPLAY' in os.environ),
        ("X11 (xset)", 
         ['xset', 'dpms', 'force', 'on' if state else 'off'], 
         lambda: 'DISPLAY' in os.environ)
    ]
    
    hardware_strategies = [
        ("DDC/CI (Fast Off)", 
         ['sudo', 'ddcutil', 'setvcp', 'D6', '0x01' if state else '0x04'], 
         lambda: True),
        ("HDMI-CEC", 
         ['sh', '-c', f'echo "{"on 0" if state else "standby 0"}" | cec-client -s -d 1'], 
         lambda: True),
        ("Legacy (vcgencmd)", 
         ['vcgencmd', 'display_power', '1' if state else '0'], 
         lambda: True),
        ("FB Blanking", 
         ['sudo', 'sh', '-c', f'echo {"0" if state else "1"} > /sys/class/graphics/fb0/blank'], 
         lambda: os.path.exists('/sys/class/graphics/fb0/blank'))
    ]

    def run_strategy(name, cmd):
        try:
            # Short timeout for cached methods, longer for discovery (3.5s is safe for Pi Zero I2C)
            timeout = 3.5
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if res.returncode != 0:
                err = res.stderr.strip()
                if "bus" in err.lower() and ("busy" in err.lower() or "error" in err.lower()):
                    logging.debug(f"Display method '{name}' failed (Bus Busy/Error): {err}")
                else:
                    logging.debug(f"Display method '{name}' failed with code {res.returncode}: {err}")
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
    if _working_methods.get('session_type'):
        # Map cached session type to a specific strategy name
        s_name = "Wayland (wlr-randr)" if _working_methods['session_type'] == 'Wayland' else "X11 (xset)"
        name, cmd_base, condition = next((s for s in session_strategies if s[0] == s_name), (None, None, None))
        if name and condition() and run_strategy(name, cmd_base):
            success_count += 1
        else:
            _working_methods['session_type'] = None
            needs_save = True

    if not success_count:
        for name, cmd, condition in session_strategies:
            if condition() and run_strategy(name, cmd):
                _working_methods['session_type'] = 'Wayland' if "Wayland" in name else 'X11'
                success_count += 1
                needs_save = True
                break

    # 2. Execute Hardware Layer
    hardware_success = False
    
    # Try any previously working hardware methods from cache
    if _working_methods['hardware']:
        for name in list(_working_methods['hardware']):
            name_check, cmd, _ = next((s for s in hardware_strategies if s[0] == name), (None, None, None))
            if name_check and run_strategy(name_check, cmd):
                success_count += 1
                hardware_success = True
            else:
                logging.warning(f"Cached display method '{name}' failed, removing from cache.")
                _working_methods['hardware'].remove(name)
                needs_save = True

    # If no cached hardware methods worked, try discovering and executing ALL possible hardware strategies
    if not hardware_success:
        for name, cmd, condition in hardware_strategies:
            # Skip strategies we *just* tried and failed in the cached block
            if name in _working_methods['hardware']:
                continue
                
            if condition() and run_strategy(name, cmd):
                logging.info(f"Discovered new display control strategy: {name}")
                _working_methods['hardware'].append(name)
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
            set_display_brightness(_working_methods.get('brightness', 100), force=True)
            set_display_contrast(_working_methods.get('contrast', 50), force=True)
            set_display_color_preset(_working_methods.get('color_preset', 'Natural (6500 K)'))

def set_display_brightness(value: int, force=False):
    """Sets display brightness using ddcutil with caching."""
    global _working_methods
    value = max(0, min(100, int(value)))
    if not force and _working_methods.get('brightness') == value:
        return True
    logging.info(f"Setting display brightness to {value}%...")
    if _run_vcp_command('10', str(value)):
        _working_methods['brightness'] = value
        _save_cache()
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_BRIGHTNESS_STATE_TOPIC, str(value), retain=True)
        return True
    return False

def set_display_contrast(value: int, force=False):
    """Sets display contrast using ddcutil (VCP 12)."""
    global _working_methods
    value = max(0, min(100, int(value)))
    if not force and _working_methods.get('contrast') == value:
        return True
    logging.info(f"Setting display contrast to {value}%...")
    if _run_vcp_command('12', str(value)):
        _working_methods['contrast'] = value
        _save_cache()
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_CONTRAST_STATE_TOPIC, str(value), retain=True)
        return True
    return False

def set_display_color_preset(preset_name: str):
    """Sets display color preset (VCP 14)."""
    global _working_methods
    presets = {
        'sRGB': '01',
        'Natural (6500 K)': '05',
        'Warm (5000 K)': '04',
        'Cool (9300 K)': '08'
    }
    hex_val = presets.get(preset_name)
    if not hex_val:
        return False
    logging.info(f"Setting display color preset to {preset_name} ({hex_val})...")
    if _run_vcp_command('14', '0x' + hex_val):
        _working_methods['color_preset'] = preset_name
        _save_cache()
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_COLOR_PRESET_STATE_TOPIC, preset_name, retain=True)
        return True
    return False

def set_display_input_source(source_name: str):
    """Sets display input source (VCP 60)."""
    global _working_methods
    sources = {
        'HDMI-1': '11',
        'HDMI-2': '12',
        'DisplayPort-1': '0f',
        'VGA': '01'
    }
    hex_val = sources.get(source_name)
    if not hex_val:
        return False
    logging.info(f"Switching input source to {source_name} ({hex_val})...")
    if _run_vcp_command('60', '0x' + hex_val):
        _working_methods['input_source'] = source_name
        _save_cache()
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(MQTT_INPUT_SOURCE_STATE_TOPIC, source_name, retain=True)
        return True
    return False

def _run_vcp_command(vcp_code, value):
    """Helper to run a ddcutil setvcp command."""
    cmd = ['sudo', 'ddcutil', 'setvcp', vcp_code, value]
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
        if 'WAYLAND_DISPLAY' in os.environ:
            del os.environ['WAYLAND_DISPLAY']
        if 'DISPLAY' in os.environ:
            del os.environ['DISPLAY']
        
        global _display_env_detected
        _display_env_detected = False # Allow re-detection for the next mode


def _get_labwc_config():
    """Create a temporary labwc config to hide the cursor and optimize for kiosk mode."""
    config_dir = subprocess.check_output(['mktemp', '-d', '/tmp/labwc-orchestrator-XXXXXX']).decode().strip()
    # Using XDG_CONFIG_HOME expects a /labwc subfolder
    os.makedirs(os.path.join(config_dir, 'labwc'), exist_ok=True)
    rc_xml = os.path.join(config_dir, 'labwc', 'rc.xml')
    with open(rc_xml, 'w') as f:
        f.write('<labwc_config>\n'
                '  <windowRules>\n'
                '    <windowRule identifier="*">\n'
                '      <action name="Maximize" />\n'
                '    </windowRule>\n'
                '  </windowRules>\n'
                '</labwc_config>')

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

    # 1. Stop the current mode first.
    stop_current_mode()
    
    # 2. Update state variable and publish intent immediately
    # This fixes the 'previous state' glitch by confirming the change before the sleep
    current_mode = mode
    if mqtt_client and mqtt_client.is_connected():
        mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
    
    # Small delay for kernel/TTY/DRM handshake settling
    time.sleep(2.0)

    if mode == 'off':
        logging.info("Ensuring display power is OFF.")
        set_display_power(False)
        return # State already published above
    else:
        # Pre-emptive power ON (so the next mode doesn't start in the dark)
        set_display_power(True)
        # Wait for monitor to wake up and DRM/I2C to be ready
        time.sleep(1.5)
        
        modes_dir = os.path.join(os.path.dirname(__file__), 'modes')
        py_script = os.path.join(modes_dir, f'{mode}_mode.py')
        sh_script = os.path.join(modes_dir, f'{mode}_mode.sh')
        
        base_cmd = []
        if os.path.exists(sh_script):
            base_cmd = ['bash', sh_script]
        elif os.path.exists(py_script):
            base_cmd = [sys.executable, py_script]
        else:
            available = get_available_modes()
            logging.warning(f"Unknown mode: {mode}. Available modes: {available}")
            current_mode = 'off'
            set_display_power(False)
            if mqtt_client and mqtt_client.is_connected():
                mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
            return

        # 3. Intelligent Session Wrapping:
        setup_display_env()
        final_cmd = base_cmd
        
        if not os.environ.get('WAYLAND_DISPLAY') and not os.environ.get('DISPLAY'):
            try:
                # Use cached path or find it once
                labwc_bin = _working_methods.get('labwc_path')
                if not labwc_bin:
                    labwc_bin = subprocess.check_output(['which', 'labwc']).decode().strip()
                    _working_methods['labwc_path'] = labwc_bin
                    _save_cache()

                global labwc_config_dir
                labwc_config_dir = _get_labwc_config()
                
                env = os.environ.copy()
                
                # Auto-discover and cache the audio input device
                audio_idx = _discover_audio_device()
                env['SMARTFRAME_AUDIO_DEVICE'] = str(audio_idx if audio_idx is not None else '')
                env['SMARTFRAME_DEBUG'] = '1' if DEBUG_MODE else '0'
                
                if mode == 'mirror':
                    env['MIRROR_URL'] = config.get('magic_mirror', {}).get('url', 'http://localhost:8080')
                
                cmd_str = ' '.join(base_cmd)
                env['XDG_CONFIG_HOME'] = labwc_config_dir
                env['XCURSOR_SIZE'] = '0'
                env['COG_PLATFORM_FDO_SHOW_CURSOR'] = '0'
                env['MIRROR_URL'] = config.get('magic_mirror', {}).get('url', 'http://localhost:8080')
                env['SMARTFRAME_AUDIO_DEVICE'] = str(config.get('audio', {}).get('device_index', ''))
                
                final_cmd = [labwc_bin, '-s', cmd_str]
                logging.info(f"Wrapping mode '{mode}' in a managed Wayland session (labwc).")
                current_process = subprocess.Popen(final_cmd, env=env, start_new_session=True, stdout=None, stderr=None)
                return

            except Exception as e:
                logging.warning(f"labwc session wrapping failed ({e}). Attempting direct launch.")


        logging.info(f"Executing {mode.capitalize()} mode command: {' '.join(final_cmd)}")
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
            "model": "Smart Frame V1"
        }
    }
    client.publish(discovery_topic, json.dumps(discovery_payload), retain=True)

    # 4. Publish Discovery for Brightness, Contrast, Color Preset, and Input
    client.publish(f"{MQTT_DISCOVERY_PREFIX}/number/smartframe/brightness/config", json.dumps({
        "name": "SmartFrame Brightness", "unique_id": "sf_brightness",
        "command_topic": MQTT_BRIGHTNESS_COMMAND_TOPIC, "state_topic": MQTT_BRIGHTNESS_STATE_TOPIC,
        "min": 0, "max": 100, "step": 5, "unit_of_measurement": "%", "icon": "mdi:brightness-6",
        "availability": [{"topic": MQTT_STATUS_TOPIC}], "device": {"identifiers": ["smartframe_orchestrator"]}
    }), retain=True)

    client.publish(f"{MQTT_DISCOVERY_PREFIX}/number/smartframe/contrast/config", json.dumps({
        "name": "SmartFrame Contrast", "unique_id": "sf_contrast",
        "command_topic": MQTT_CONTRAST_COMMAND_TOPIC, "state_topic": MQTT_CONTRAST_STATE_TOPIC,
        "min": 0, "max": 100, "step": 5, "unit_of_measurement": "%", "icon": "mdi:contrast",
        "availability": [{"topic": MQTT_STATUS_TOPIC}], "device": {"identifiers": ["smartframe_orchestrator"]}
    }), retain=True)

    client.publish(f"{MQTT_DISCOVERY_PREFIX}/select/smartframe/color_preset/config", json.dumps({
        "name": "SmartFrame Color Profile", "unique_id": "sf_color_preset",
        "command_topic": MQTT_COLOR_PRESET_COMMAND_TOPIC, "state_topic": MQTT_COLOR_PRESET_STATE_TOPIC,
        "options": ['sRGB', 'Natural (6500 K)', 'Warm (5000 K)', 'Cool (9300 K)'],
        "icon": "mdi:palette", "availability": [{"topic": MQTT_STATUS_TOPIC}],
        "device": {"identifiers": ["smartframe_orchestrator"]}
    }), retain=True)

    client.publish(f"{MQTT_DISCOVERY_PREFIX}/select/smartframe/input_source/config", json.dumps({
        "name": "SmartFrame Input", "unique_id": "sf_input",
        "command_topic": MQTT_INPUT_SOURCE_COMMAND_TOPIC, "state_topic": MQTT_INPUT_SOURCE_STATE_TOPIC,
        "options": ['HDMI-1', 'HDMI-2', 'DisplayPort-1', 'VGA'],
        "icon": "mdi:video-input-hdmi", "availability": [{"topic": MQTT_STATUS_TOPIC}],
        "device": {"identifiers": ["smartframe_orchestrator"]}
    }), retain=True)
    
    logging.info("Published Home Assistant MQTT discovery payloads (Universal Display Control).")

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
        client.publish(MQTT_BRIGHTNESS_STATE_TOPIC, str(_working_methods.get('brightness', 100)), retain=True)
        client.publish(MQTT_CONTRAST_STATE_TOPIC, str(_working_methods.get('contrast', 50)), retain=True)
        client.publish(MQTT_COLOR_PRESET_STATE_TOPIC, _working_methods.get('color_preset', 'Natural (6500 K)'), retain=True)
        client.publish(MQTT_INPUT_SOURCE_STATE_TOPIC, _working_methods.get('input_source', 'HDMI-1'), retain=True)
    else:
        logging.error(f"Failed to connect, reason code: {reason_code}")
        if reason_code in ["Not authorized", "Bad user name or password"]:
            logging.error("MQTT authentication failed. Check your config.yaml.")
            client.disconnect()

def on_message(client, userdata, msg):
    payload = msg.payload.decode('utf-8').strip().lower()
    logging.info(f"Message received on {msg.topic}: {payload}")
    available_modes = get_available_modes()
    if msg.topic == MQTT_COMMAND_TOPIC:
        if payload in available_modes:
            start_mode(payload)
        else:
            logging.warning(f"Invalid payload '{payload}'. Available modes: {available_modes}")
    elif msg.topic == MQTT_BRIGHTNESS_COMMAND_TOPIC:
        try:
            set_display_brightness(int(payload))
        except ValueError:
            logging.warning(f"Invalid brightness payload: {payload}")
    elif msg.topic == MQTT_CONTRAST_COMMAND_TOPIC:
        try:
            set_display_contrast(int(payload))
        except ValueError:
            logging.warning(f"Invalid contrast payload: {payload}")
    elif msg.topic == MQTT_COLOR_PRESET_COMMAND_TOPIC:
        set_display_color_preset(payload)
    elif msg.topic == MQTT_INPUT_SOURCE_COMMAND_TOPIC:
        set_display_input_source(payload)

def signal_handler(sig, frame):
    logging.info(f"Signal {sig} received. Shutting down orchestrator...")
    stop_current_mode()
    sys.exit(0)

if __name__ == '__main__':
    # Parse command line arguments
    import argparse
    parser = argparse.ArgumentParser(description='SmartFrame Orchestrator')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()

    if args.debug:
        DEBUG_MODE = True
        os.environ['SMARTFRAME_DEBUG'] = '1'
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
        subprocess.run(['sudo', 'modprobe', 'i2c-dev'], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    logging.info("--- SmartFrame Orchestrator starting ---")
    
    # 1. Initial State: Screen off until a mode starts
    set_display_power(False)

    # 2. Connect to MQTT with automatic retry
    try:
        if MQTT_BROKER != "[MQTT_SERVER_IP_ADDRESS]":
            # Set shorter keepalive and infinite reconnection delay
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        else:
            logging.warning("MQTT broker IP not configured. Starting in OFFLINE mode.")
    except Exception as e:
        logging.error(f"Error connecting to MQTT broker: {e}. Orchestrator will retry in background.")

    # 3. Start default mode from config
    default_mode = config.get('default_mode', 'off')
    start_mode(default_mode)

    try:
        if MQTT_BROKER != "[MQTT_SERVER_IP_ADDRESS]":
            # Use loop_start (non-blocking thread) to allow the main thread to monitor processes
            mqtt_client.loop_start()
            logging.debug("MQTT background thread started.")
        
        # --- GLOBAL FAIL-SAFE LOOP ---
        # This keeps the main thread alive and monitors the active mode process (heartbeat/watchdog)
        while True:
            # 1. Check if the current mode process has crashed unexpectedly
            if current_mode != 'off' and current_process:
                poll_res = current_process.poll()
                if poll_res is not None:
                    logging.error(f"FAIL-SAFE: Mode process '{current_mode}' exited unexpectedly with code {poll_res}.")
                    logging.info("Forcing display power OFF to prevent showing broken state.")
                    # Force display off and set mode to 'off' to prevent repeated attempts
                    set_display_power(False)
                    current_mode = 'off'
                    if mqtt_client and mqtt_client.is_connected():
                        mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
            
            time.sleep(1)
            
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt: Shutting down...")
    except Exception as e:
        logging.critical(f"FATAL SOFTWARE ERROR: Orchestrator main loop crashed: {e}")
        # This is the 'software bug' scenario mentioned by the user
    finally:
        logging.info("--- TRIGGERING GLOBAL FAIL-SAFE EXIT ---")
        # Ensure we always attempt to kill any child processes and turn off the screen on exit
        stop_current_mode()
        set_display_power(False)
        if mqtt_client:
            mqtt_client.loop_stop()
            logging.info("MQTT loop stopped.")
        logging.info("--- SmartFrame Orchestrator terminated ---")
