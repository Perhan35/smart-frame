import os
import subprocess
import sys
import time
import json
import yaml
import paho.mqtt.client as mqtt
import logging

logging.basicConfig(level=logging.INFO)

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
if not os.path.exists(config_path):
    logging.error("config.yaml not found. Copy config.example.yaml to config.yaml and fill in your settings.")
    sys.exit(1)
with open(config_path, 'r') as file:
    config = yaml.safe_load(file)

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

def set_display_power(state: bool):
    try:
        power_val = "1" if state else "0"
        subprocess.run(['vcgencmd', 'display_power', power_val], check=True)
        logging.info(f"Display power set to {state}")
    except Exception as e:
        logging.error(f"Failed to control display power (not on Raspberry Pi?): {e}")

def stop_current_mode():
    global current_process
    if current_process:
        logging.info(f"Stopping current mode: {current_mode}")
        current_process.terminate()
        try:
            current_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            current_process.kill()
        current_process = None

def get_available_modes():
    modes = ['off']
    modes_dir = os.path.join(os.path.dirname(__file__), 'modes')
    if os.path.isdir(modes_dir):
        for f in os.path.listdir(modes_dir):
            if f.endswith('_mode.py') or f.endswith('_mode.sh'):
                mode_name = f.replace('_mode.py', '').replace('_mode.sh', '')
                if mode_name not in modes:
                    modes.append(mode_name)
    return sorted(modes)

def start_mode(mode):
    global current_process
    global current_mode
    global mqtt_client
    
    if mode == current_mode:
        return
        
    stop_current_mode()
    current_mode = mode
    
    modes_dir = os.path.join(os.path.dirname(__file__), 'modes')
    
    if mode == 'off':
        logging.info("Switching off screen...")
        set_display_power(False)
    else:
        py_script = os.path.join(modes_dir, f'{mode}_mode.py')
        sh_script = os.path.join(modes_dir, f'{mode}_mode.sh')
        
        if os.path.exists(py_script):
            set_display_power(True)
            logging.info(f"Starting {mode.capitalize()} Mode (Python)...")
            current_process = subprocess.Popen([sys.executable, py_script])
        elif os.path.exists(sh_script):
            set_display_power(True)
            logging.info(f"Starting {mode.capitalize()} Mode (Bash)...")
            current_process = subprocess.Popen(['bash', sh_script])
        else:
            available = get_available_modes()
            logging.warning(f"Unknown mode: {mode}. Available modes: {available}")
            current_mode = None
            return

    # Publish state update
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
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER and MQTT_USER != "[MQTT_USERNAME]":
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

    mqtt_client.will_set(MQTT_STATUS_TOPIC, "offline", retain=True)
    
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    logging.info("SmartFrame orchestrator starting...")
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
        start_mode('mirror') # Fallback to mirror

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
