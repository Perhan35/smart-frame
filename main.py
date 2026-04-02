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
MQTT_USER = mqtt_config.get('username')
MQTT_PASS = mqtt_config.get('password')

current_process = None
current_mode = None
mqtt_client = None
labwc_config_dir = None

# Discovery cache to avoid repeated slow subprocess calls
_working_methods = {
    'session': None, # Cached 'Wayland (wlr-randr)' or 'X11 (xset)'
    'hardware': []    # List of low-level methods (vcgencmd, fb0, etc) that work
}

def setup_display_env():
    """Detect and validate the current display environment (Wayland/X11)."""
    uid = os.getuid()
    
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

    # 1. Validate existing environment - delete if stale
    if 'WAYLAND_DISPLAY' in os.environ:
        if not is_wayland_reachable(os.environ['WAYLAND_DISPLAY']):
            logging.debug(f"Cleaning stale WAYLAND_DISPLAY: {os.environ['WAYLAND_DISPLAY']}")
            del os.environ['WAYLAND_DISPLAY']
            _working_methods['session'] = None

    if 'DISPLAY' in os.environ:
        if not is_x11_active():
            logging.debug(f"Cleaning stale DISPLAY: {os.environ['DISPLAY']}")
            del os.environ['DISPLAY']
            _working_methods['session'] = None

    # 2. Force default XDG_RUNTIME_DIR if missing (critical for Wayland)
    if 'XDG_RUNTIME_DIR' not in os.environ:
        os.environ['XDG_RUNTIME_DIR'] = f"/run/user/{uid}"

    # 3. Auto-detection: Search for running sessions
    if 'WAYLAND_DISPLAY' not in os.environ and 'DISPLAY' not in os.environ:
        # Check standard Wayland sockets
        for i in range(2):
            name = f'wayland-{i}'
            if is_wayland_reachable(name):
                os.environ['WAYLAND_DISPLAY'] = name
                logging.info(f"Auto-detected Wayland: {name}")
                return
        
        # Check standard X11 socket
        if is_x11_active() and os.path.exists('/tmp/.X11-unix/X0'):
            os.environ['DISPLAY'] = ':0'
            logging.info("Auto-detected X11: :0")

def _get_hdmi_output_name():
    # Cache the output name to avoid calling wlr-randr every time
    if getattr(_get_hdmi_output_name, 'cached', None):
        return _get_hdmi_output_name.cached
        
    try:
        # Avoid hanging if wlr-randr isn't available or compositor isn't responding
        output = subprocess.check_output(['wlr-randr'], stderr=subprocess.DEVNULL, timeout=1.5).decode()
        for line in output.split('\n'):
            if 'HDMI' in line and (line.strip() and not line.startswith(' ')):
                name = line.split(' ')[0]
                _get_hdmi_output_name.cached = name
                return name
    except Exception:
        pass
    return "HDMI-A-1" # Raspberry Pi standard fallback

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
        ("Legacy (vcgencmd)", 
         ['vcgencmd', 'display_power', '1' if state else '0'], 
         lambda: True),
        ("FB Blanking", 
         ['sudo', 'sh', '-c', f'echo {"0" if state else "1"} > /sys/class/graphics/fb0/blank'], 
         lambda: os.path.exists('/sys/class/graphics/fb0/blank'))
    ]

    def run_strategy(name, cmd):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            return res.returncode == 0
        except Exception:
            return False

    success_count = 0
    
    # 1. Execute Session Layer
    if _working_methods['session']:
        name, cmd_base, _ = next((s for s in session_strategies if s[0] == _working_methods['session']), (None, None, None))
        if name and run_strategy(name, cmd_base):
            success_count += 1
            logging.debug(f" - {name}: Success (cached)")
        else:
            _working_methods['session'] = None # Invalidate stale cache

    if not success_count:
        # Discovery Phase for Session
        for name, cmd, condition in session_strategies:
            if condition() and run_strategy(name, cmd):
                _working_methods['session'] = name
                success_count += 1
                logging.debug(f" - {name}: Discovered and working")
                break

    # 2. Execute Hardware Layer
    # We run hardware strategies to ensure persistent blanking/power states
    if _working_methods['hardware']:
        for name in _working_methods['hardware']:
            name_check, cmd, _ = next((s for s in hardware_strategies if s[0] == name), (None, None, None))
            if name_check and run_strategy(name_check, cmd):
                success_count += 1
                logging.debug(f" - {name}: Success (cached)")
    else:
        # Discovery Phase for Hardware
        for name, cmd, condition in hardware_strategies:
            if condition() and run_strategy(name, cmd):
                _working_methods['hardware'].append(name)
                success_count += 1
                logging.debug(f" - {name}: Discovered and working")

    if success_count == 0:
        logging.error(f"Failed to set display power to {target}.")
    else:
        logging.info(f"Display set to {target} (Methods: {success_count})")

def stop_current_mode():
    global current_process
    if current_process:
        logging.info(f"Stopping current mode: {current_mode}")
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

def get_available_modes():
    modes = ['off']
    modes_dir = os.path.join(os.path.dirname(__file__), 'modes')
    if os.path.isdir(modes_dir):
        for f in os.listdir(modes_dir):
            if f.endswith('_mode.py') or f.endswith('_mode.sh'):
                mode_name = f.replace('_mode.py', '').replace('_mode.sh', '')
                if mode_name not in modes:
                    modes.append(mode_name)
    return sorted(modes)

def _get_labwc_config():
    """Create a temporary labwc config to hide the cursor and optimize for kiosk mode."""
    config_dir = subprocess.check_output(['mktemp', '-d', '/tmp/labwc-orchestrator-XXXXXX']).decode().strip()
    # Using XDG_CONFIG_HOME expects a /labwc subfolder
    os.makedirs(os.path.join(config_dir, 'labwc'), exist_ok=True)
    rc_xml = os.path.join(config_dir, 'labwc', 'rc.xml')
    with open(rc_xml, 'w') as f:
        f.write('<labwc_config>\n'
                '  <core>\n'
                '    <cursor>\n'
                '      <timeout>1</timeout>\n'
                '    </cursor>\n'
                '  </core>\n'
                '  <windowRules>\n'
                '    <windowRule identifier="*">\n'
                '      <action name="Maximize" />\n'
                '    </windowRule>\n'
                '  </windowRules>\n'
                '</labwc_config>')

    return config_dir

def start_mode(mode):
    global current_process
    global current_mode
    global mqtt_client
    
    if mode == current_mode:
        return
    
    logging.info(f"Transitioning to mode: {mode}")

    # 1. Stop the current mode first.
    # This releases DRM master, X11 sockets, and TTY resources.
    stop_current_mode()
    
    # Small delay for kernel/TTY/DRM handshake settling
    time.sleep(1.5)

    current_mode = mode
    
    if mode == 'off':
        logging.info("Ensuring display power is OFF.")
        set_display_power(False)
    else:
        # Pre-emptive power ON (so the next mode doesn't start in the dark)
        # Note: If labwc is about to start, this might use legacy/fb methods, which is fine.
        set_display_power(True)
        
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
            return

        # 2. Intelligent Session Wrapping:
        setup_display_env()
        final_cmd = base_cmd
        
        if not os.environ.get('WAYLAND_DISPLAY') and not os.environ.get('DISPLAY'):
            try:
                subprocess.check_call(['which', 'labwc'], stdout=subprocess.DEVNULL)
                global labwc_config_dir
                labwc_config_dir = _get_labwc_config()
                
                # Faster path: Pass URLs to scripts directly via env vars
                env = os.environ.copy()
                if mode == 'mirror':
                    env['MIRROR_URL'] = config.get('magic_mirror', {}).get('url', 'http://localhost:8080')
                
                cmd_str = ' '.join(base_cmd)
                # Use XDG_CONFIG_HOME for robust config isolation across all labwc versions
                env['XDG_CONFIG_HOME'] = labwc_config_dir
                final_cmd = ['labwc', '-s', cmd_str]
                logging.info(f"Wrapping mode '{mode}' in a managed Wayland session (labwc) with isolated XDG_CONFIG_HOME.")
                current_process = subprocess.Popen(final_cmd, env=env, start_new_session=True, stdout=None, stderr=None)
                
                # Update MQTT state before returning
                if mqtt_client and current_mode:
                    mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
                    logging.info(f"Published state: {current_mode} to {MQTT_STATE_TOPIC}")
                return

            except Exception as e:
                logging.warning(f"labwc session wrapping failed ({e}). Attempting direct launch.")


        logging.info(f"Executing {mode.capitalize()} mode command: {' '.join(final_cmd)}")
        current_process = subprocess.Popen(final_cmd, start_new_session=True)

    # 4. Synchronize state with MQTT
    if mqtt_client and current_mode:
        mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
        logging.info(f"Published state: {current_mode} to {MQTT_STATE_TOPIC}")

# MQTT Callbacks
def publish_discovery_and_status(client):
    modes = get_available_modes()
    
    # 1. Publish status (online)
    client.publish(MQTT_STATUS_TOPIC, "online", retain=True)
    
    # 2. Publish list of available modes
    client.publish(MQTT_AVAILABLE_MODES_TOPIC, json.dumps(modes), retain=True)

    # 3. Publish Home Assistant Discovery payload
    discovery_topic = f"{MQTT_DISCOVERY_PREFIX}/select/smartframe/mode/config"
    discovery_payload = {
        "name": "SmartFrame Mode",
        "unique_id": "smartframe_mode_selector",
        "command_topic": MQTT_COMMAND_TOPIC,
        "state_topic": MQTT_STATE_TOPIC,
        "options": modes,
        "availability": [
            {
                "topic": MQTT_STATUS_TOPIC
            }
        ],
        "device": {
            "identifiers": ["smartframe_orchestrator"],
            "name": "Smart Frame",
            "manufacturer": "Custom",
            "model": "Smart Frame V1"
        }
    }
    client.publish(discovery_topic, json.dumps(discovery_payload), retain=True)
    logging.info("Published Home Assistant MQTT discovery payload and online status.")

def on_connect(client, userdata, _connect_flags, reason_code, _properties):
    global current_mode
    if not reason_code.is_failure:
        logging.info("Connected to MQTT broker.")
        client.subscribe(MQTT_COMMAND_TOPIC)
        logging.info(f"Subscribed to command topic: {MQTT_COMMAND_TOPIC}")
        
        publish_discovery_and_status(client)
        
        # Publish current state on connect
        if current_mode:
            client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
    else:
        logging.error(f"Failed to connect, reason code: {reason_code}")
        if reason_code in ["Not authorized", "Bad user name or password"]:
            logging.error("MQTT authentication failed. Check your config.yaml.")
            client.disconnect()

def on_message(client, userdata, msg):
    payload = msg.payload.decode('utf-8').strip().lower()
    logging.info(f"Message received on {msg.topic}: {payload}")
    available_modes = get_available_modes()
    if payload in available_modes:
        start_mode(payload)
    else:
        logging.warning(f"Invalid payload '{payload}'. Available modes: {available_modes}")

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
            # Set shorter keepalive and infinite reconnection delay (managed by loop_forever)
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
            # loop_forever handles automatic reconnections and persists the process
            mqtt_client.loop_forever()
        else:
            # Keeps the main thread alive in offline mode
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_current_mode()
