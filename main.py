import os
import subprocess
import sys
import time
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

def start_mode(mode):
    global current_process
    global current_mode
    global mqtt_client
    
    if mode == current_mode:
        return
        
    stop_current_mode()
    current_mode = mode
    
    modes_dir = os.path.join(os.path.dirname(__file__), 'modes')
    
    if mode == 'audio':
        set_display_power(True)
        logging.info("Starting Audio Mode...")
        script_path = os.path.join(modes_dir, 'audio_mode.py')
        current_process = subprocess.Popen([sys.executable, script_path])
    elif mode == 'mirror':
        set_display_power(True)
        logging.info("Starting Magic Mirror Mode...")
        script_path = os.path.join(modes_dir, 'mirror_mode.sh')
        current_process = subprocess.Popen(['bash', script_path])
    elif mode == 'off':
        logging.info("Switching off screen...")
        set_display_power(False)
    else:
        logging.warning(f"Unknown mode: {mode}. Send 'audio', 'mirror', or 'off'.")
        current_mode = None
        return

    # Publish state update
    if mqtt_client and current_mode:
        mqtt_client.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
        logging.info(f"Published state: {current_mode} to {MQTT_STATE_TOPIC}")

# MQTT Callbacks
def on_connect(client, userdata, _connect_flags, reason_code, _properties):
    global current_mode
    if not reason_code.is_failure:
        logging.info("Connected to MQTT broker.")
        client.subscribe(MQTT_COMMAND_TOPIC)
        logging.info(f"Subscribed to command topic: {MQTT_COMMAND_TOPIC}")
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
    if payload in ['audio', 'mirror', 'off']:
        start_mode(payload)
    else:
        logging.warning("Invalid payload. Please send 'audio', 'mirror', or 'off'.")

if __name__ == '__main__':
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER and MQTT_USER != "[MQTT_USERNAME]":
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

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
