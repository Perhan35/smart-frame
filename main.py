import os
import subprocess
import sys
import time
import json
import yaml
import paho.mqtt.client as mqtt
import logging
import signal

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

def setup_display_env():
    """Detect and set up environment variables for Wayland/X11 access."""
    # 1. Validate existing environment
    if 'WAYLAND_DISPLAY' in os.environ:
        runtime_dir = os.environ.get('XDG_RUNTIME_DIR', f"/run/user/{os.getuid()}")
        socket_path = os.path.join(runtime_dir, os.environ['WAYLAND_DISPLAY'])
        if not os.path.exists(socket_path):
            logging.debug(f"Invalid WAYLAND_DISPLAY '{os.environ['WAYLAND_DISPLAY']}' (no socket at {socket_path}). Clearing.")
            del os.environ['WAYLAND_DISPLAY']

    if 'DISPLAY' in os.environ:
        x_socket = f"/tmp/.X11-unix/X{os.environ['DISPLAY'].replace(':', '')}"
        if not os.path.exists(x_socket):
            logging.debug(f"Invalid DISPLAY '{os.environ['DISPLAY']}' (no socket at {x_socket}). Clearing.")
            del os.environ['DISPLAY']

    # 2. Attempt auto-detection if none found or invalid
    if 'DISPLAY' not in os.environ and 'WAYLAND_DISPLAY' not in os.environ:
        try:
            # Set default XDG_RUNTIME_DIR if missing
            if 'XDG_RUNTIME_DIR' not in os.environ:
                os.environ['XDG_RUNTIME_DIR'] = f"/run/user/{os.getuid()}"

            # Check for common Wayland compositors on Raspberry Pi
            pgrep = subprocess.run(['pgrep', '-x', 'labwc'], capture_output=True)
            if pgrep.returncode != 0:
                pgrep = subprocess.run(['pgrep', '-x', 'wayfire'], capture_output=True)
            
            if pgrep.returncode == 0:
                runtime_dir = os.environ.get('XDG_RUNTIME_DIR')
                
                # Check current user's runtime dir
                if os.path.exists(os.path.join(runtime_dir, 'wayland-0')):
                    os.environ['WAYLAND_DISPLAY'] = 'wayland-0'
                elif os.path.exists(os.path.join(runtime_dir, 'wayland-1')):
                    os.environ['WAYLAND_DISPLAY'] = 'wayland-1'
                elif os.getuid() == 0:
                    # If root, failover to search for common user 1000's session
                    if os.path.exists('/run/user/1000/wayland-0'):
                        os.environ['XDG_RUNTIME_DIR'] = '/run/user/1000'
                        os.environ['WAYLAND_DISPLAY'] = 'wayland-0'
                        logging.info("Root user using session for UID 1000")
                
                if 'WAYLAND_DISPLAY' in os.environ:
                    logging.info(f"Auto-detected Wayland environment: {os.environ['WAYLAND_DISPLAY']} in {os.environ['XDG_RUNTIME_DIR']}")
            elif os.path.exists('/tmp/.X11-unix/X0'):
                os.environ['DISPLAY'] = ':0'
                logging.info("Auto-detected X11 environment: :0")
        except Exception as e:
            logging.error(f"Environment detection helper failed: {e}")

def set_display_power(state: bool):
    try:
        setup_display_env()
        
        # Auto-detect HDMI output for wlr-randr
        output_name = "HDMI-A-1" # Default fallback
        try:
            # Try to find the connected HDMI output
            wlr_output = subprocess.check_output(['wlr-randr'], stderr=subprocess.DEVNULL).decode()
            for line in wlr_output.split('\n'):
                if 'HDMI' in line and ' ' in line:
                    output_name = line.split(' ')[0]
                    break
            logging.info(f"Detected HDMI output: {output_name}")
        except Exception:
            logging.warning(f"Could not auto-detect HDMI output using wlr-randr, using fallback: {output_name}")

        success = False
        method_results = []
        
        target = "ON" if state else "OFF"
        logging.info(f"Attempting to set display power to {target}...")

        if state:
            # 1. Wayland method (preferred)
            if os.environ.get('WAYLAND_DISPLAY'):
                res = subprocess.run(['wlr-randr', '--output', output_name, '--on'], capture_output=True, text=True)
                method_results.append(f"Wayland (wlr-randr): {res.returncode} (err: {res.stderr.strip()})")
                if res.returncode == 0:
                    success = True
            
            # 2. Legacy method
            res = subprocess.run(['vcgencmd', 'display_power', '1'], capture_output=True, text=True)
            method_results.append(f"Legacy (vcgencmd): {res.returncode} (err: {res.stderr.strip()})")
            if res.returncode == 0:
                success = True
            
            # 3. DPMS method
            if os.environ.get('DISPLAY'):
                res = subprocess.run(['xset', 'dpms', 'force', 'on'], capture_output=True, text=True)
                method_results.append(f"X11 (xset): {res.returncode} (err: {res.stderr.strip()})")
                if res.returncode == 0:
                    success = True
            
            # 4. Backlight method
            try:
                backlight_dirs = [f for f in os.listdir('/sys/class/backlight') if os.path.isdir(os.path.join('/sys/class/backlight', f))]
                for bl in backlight_dirs:
                    bl_path = f'/sys/class/backlight/{bl}/bl_power'
                    res = subprocess.run(['sudo', 'sh', '-c', f'echo 0 > {bl_path}'], capture_output=True, text=True)
                    method_results.append(f"Backlight ({bl}): {res.returncode}")
                    if res.returncode == 0:
                        success = True
            except Exception as e:
                method_results.append(f"Backlight check failed: {e}")

            # 5. Framebuffer Blanking (confirmed working for user)
            if os.path.exists('/sys/class/graphics/fb0/blank'):
                res = subprocess.run(['sudo', 'sh', '-c', 'echo 0 > /sys/class/graphics/fb0/blank'], capture_output=True, text=True)
                method_results.append(f"FB Blanking (ON): {res.returncode}")
                if res.returncode == 0:
                    success = True
                
        else:
            # 1. Wayland method
            if os.environ.get('WAYLAND_DISPLAY'):
                res = subprocess.run(['wlr-randr', '--output', output_name, '--off'], capture_output=True, text=True)
                method_results.append(f"Wayland (wlr-randr): {res.returncode} (err: {res.stderr.strip()})")
                if res.returncode == 0:
                    success = True
            
            # 2. Legacy method
            res = subprocess.run(['vcgencmd', 'display_power', '0'], capture_output=True, text=True)
            method_results.append(f"Legacy (vcgencmd): {res.returncode} (err: {res.stderr.strip()})")
            if res.returncode == 0:
                success = True
            
            # 3. DPMS method
            if os.environ.get('DISPLAY'):
                res = subprocess.run(['xset', 'dpms', 'force', 'off'], capture_output=True, text=True)
                method_results.append(f"X11 (xset): {res.returncode} (err: {res.stderr.strip()})")
                if res.returncode == 0:
                    success = True

            # 4. Backlight method (direct hardware)
            try:
                # Try to turn off all backlights
                backlight_dirs = [f for f in os.listdir('/sys/class/backlight') if os.path.isdir(os.path.join('/sys/class/backlight', f))]
                for bl in backlight_dirs:
                    bl_path = f'/sys/class/backlight/{bl}/bl_power'
                    # We might need sudo, but let's try direct first
                    res = subprocess.run(['sudo', 'sh', '-c', f'echo 1 > {bl_path}'], capture_output=True, text=True)
                    method_results.append(f"Backlight ({bl}): {res.returncode}")
                    if res.returncode == 0:
                        success = True
            except Exception as e:
                method_results.append(f"Backlight check failed: {e}")

            # 5. Framebuffer Blanking (confirmed working for user)
            if os.path.exists('/sys/class/graphics/fb0/blank'):
                res = subprocess.run(['sudo', 'sh', '-c', 'echo 1 > /sys/class/graphics/fb0/blank'], capture_output=True, text=True)
                method_results.append(f"FB Blanking (OFF): {res.returncode}")
                if res.returncode == 0:
                    success = True

        for result in method_results:
            logging.info(f" - {result}")

        if success:
            logging.info(f"Display power successfully set to {target}")
        else:
            logging.error(f"FAILED to set display power to {target}. Tried methods: {', '.join(method_results)}")
    except Exception as e:
        logging.error(f"Fatal error controlling display power: {e}")

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
    os.makedirs(os.path.join(config_dir, 'labwc'), exist_ok=True)
    rc_xml = os.path.join(config_dir, 'labwc', 'rc.xml')
    with open(rc_xml, 'w') as f:
        f.write('<labwc_config><core><cursor><timeout>1</timeout></cursor></core></labwc_config>')
    return config_dir

def start_mode(mode):
    global current_process
    global current_mode
    global mqtt_client
    
    if mode == current_mode:
        return
    
    # 1. First attempt at power off while the current session/compositor is still active
    if mode == 'off':
        logging.info("Switching to OFF mode. Finalizing display states...")
        set_display_power(False)

    # 2. Synchronously stop the current mode.
    # This ensures any DRM/TTY resources are released before starting the next mode.
    stop_current_mode()
    
    # Small delay for kernel/TTY handshake settling
    time.sleep(0.5)

    current_mode = mode
    modes_dir = os.path.join(os.path.dirname(__file__), 'modes')
    
    if mode == 'off':
        logging.info("Confirmed OFF state. Forcing low-level power-off.")
        set_display_power(False)
    else:
        # Pre-emptive power ON (so the next mode doesn't start in the dark)
        set_display_power(True)
        
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

        # 3. Intelligent Session Wrapping:
        # If no Wayland or X11 display is active, wrap the mode in a dedicated labwc session.
        # This takes the guess-work out of mode implementation and ensures 100% display coverage.
        setup_display_env()
        final_cmd = base_cmd
        
        if not os.environ.get('WAYLAND_DISPLAY') and not os.environ.get('DISPLAY'):
            try:
                # Check if labwc is available on the system path
                subprocess.check_call(['which', 'labwc'], stdout=subprocess.DEVNULL)
                config_dir = _get_labwc_config()
                # Use --session-command to run the mode as the primary display consumer
                # This ensures the screen remains blank/off when the mode exits.
                cmd_str = ' '.join(base_cmd)
                final_cmd = ['labwc', '-c', config_dir, '-s', cmd_str]
                logging.info(f"Wrapping mode '{mode}' in a managed Wayland session (labwc).")
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

if __name__ == '__main__':
    # Parse command line arguments
    import argparse
    parser = argparse.ArgumentParser(description='SmartFrame Orchestrator')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode with full logs')
    args = parser.parse_args()

    if args.debug:
        DEBUG_MODE = True
        os.environ['SMARTFRAME_DEBUG'] = '1'
        logging.getLogger().setLevel(logging.DEBUG)
        logging.debug("CLI DEBUG FLAG DETECTED: Full subprocess output enabled.")

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER and MQTT_USER != "[MQTT_USERNAME]":
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

    mqtt_client.will_set(MQTT_STATUS_TOPIC, "offline", retain=True)
    
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    # Ensure i2c-dev is loaded for ddcutil support in the future
    try:
        subprocess.run(['sudo', 'modprobe', 'i2c-dev'], capture_output=True)
    except Exception:
        pass

    logging.info("SmartFrame orchestrator starting...")
    set_display_power(False)
    try:
        # Prevent connection error if default IP has not been replaced yet
        if MQTT_BROKER != "[MQTT_SERVER_IP_ADDRESS]":
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        else:
            logging.warning("MQTT broker IP not configured. Starting offline mode.")
    except Exception as e:
        logging.error(f"Error connecting to MQTT broker: {e}")

    # Start with the default mode
    default_mode = config.get('default_mode')
    if default_mode:
        start_mode(default_mode)
    else:
        start_mode('off') # Fallback to off

    try:
        if MQTT_BROKER != "[MQTT_SERVER_IP_ADDRESS]":
            mqtt_client.loop_forever()
            logging.warning("MQTT loop exited. Falling back to offline mode.")
            
        # Keeps the main thread alive in offline mode or if loop_forever exits
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    finally:
        stop_current_mode()
