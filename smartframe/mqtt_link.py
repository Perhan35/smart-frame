"""MQTT and Home Assistant: the client, discovery, incoming commands and the
diagnostic sensors (CPU temperature, start time).

This module owns `client` (created by create_client()) and `going_offline`.
"""

import json
import logging
import socket
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from smartframe import cache, ha, perf, supervisor, worker
from smartframe.commands import parse_command
from smartframe.settings import (
    COLOR_PRESETS,
    CPU_TEMP_PATH,
    DEFAULT_COLOR_PRESET,
    DEVICE_ID,
    INPUT_SOURCES,
    MQTT_AVAILABLE_MODES_TOPIC,
    MQTT_BRIGHTNESS_COMMAND_TOPIC,
    MQTT_BRIGHTNESS_STATE_TOPIC,
    MQTT_BROKER,
    MQTT_CA_CERT,
    MQTT_COLOR_PRESET_COMMAND_TOPIC,
    MQTT_COLOR_PRESET_STATE_TOPIC,
    MQTT_COMMAND_TOPIC,
    MQTT_CONTRAST_COMMAND_TOPIC,
    MQTT_CONTRAST_STATE_TOPIC,
    MQTT_CPU_TEMP_STATE_TOPIC,
    MQTT_DBA_STATE_TOPIC,
    MQTT_DISCOVERY_PREFIX,
    MQTT_HA_STATUS_TOPIC,
    MQTT_INPUT_SOURCE_COMMAND_TOPIC,
    MQTT_INPUT_SOURCE_STATE_TOPIC,
    MQTT_LAFMAX_STATE_TOPIC,
    MQTT_PASS,
    MQTT_PORT,
    MQTT_RESTART_COMMAND_TOPIC,
    MQTT_RUN_STATE_TOPIC,
    MQTT_STARTED_STATE_TOPIC,
    MQTT_STATE_TOPIC,
    MQTT_STATUS_TOPIC,
    MQTT_TLS,
    MQTT_USER,
)

client = None
going_offline = False  # Set while the service stops: "offline" is then expected

# Start time for the "Started" sensor. The Pi has no clock battery: at boot the wall
# clock may still be wrong until NTP syncs, so the start time is computed from the
# monotonic clock when it is published, and sent again if it moves (_publish_started()).
_START_MONOTONIC = time.monotonic()
_started_sent = None  # Wall-clock start time last sent to HA (epoch seconds)
_last_cpu_temp = None  # Last CPU temperature sent to HA (°C), or None

DISCOVERY_DELAY = 3.0  # Seconds between clearing the old entities and announcing the new ones

_last_conn_fail_log = 0.0
_refused_logged_at = 0.0
_refused_logged = set()


def create_client():
    """Create and configure the MQTT client (not connected yet). Returns the broker
    to connect to, or None when it is not configured or TLS could not be set up."""
    global client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=ha.mqtt_client_id(DEVICE_ID))
    # Never let an exception in a callback kill paho's network thread. paho then only
    # reports callback errors through its logger, so attach one.
    client.suppress_exceptions = True
    client.enable_logger(logging.getLogger("paho"))
    # After a lost connection or a refusal (wrong password...): retry after 1 s,
    # then double the wait each time, up to 2 minutes.
    client.reconnect_delay_set(min_delay=1, max_delay=120)

    broker = MQTT_BROKER if MQTT_BROKER != "[MQTT_SERVER_IP_ADDRESS]" else None
    has_credentials = bool(MQTT_USER) and MQTT_USER != "[MQTT_USERNAME]"
    if has_credentials:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    else:
        logging.warning(
            "MQTT: no username/password configured. Anyone on the network who can "
            "reach the broker can control this frame. Set mqtt.username and "
            "mqtt.password in config.yaml (and require auth on the broker)."
        )

    if MQTT_TLS:
        try:
            client.tls_set(ca_certs=MQTT_CA_CERT)
            logging.info("MQTT: TLS enabled.")
        except Exception as e:
            logging.error(
                f"MQTT: TLS setup failed ({e}). Check mqtt.ca_cert in config.yaml. "
                "Starting in OFFLINE mode."
            )
            broker = None  # Skip connecting
    elif has_credentials:
        logging.warning(
            "MQTT: TLS is disabled, so the password and all commands travel in clear "
            "text on the network. Set mqtt.tls: true (usually port 8883) if your "
            "broker supports it."
        )

    client.will_set(MQTT_STATUS_TOPIC, "offline", retain=True)
    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return broker


def publish(topic, payload, retain=False):
    """Publish if MQTT is connected (state updates are best effort)."""
    current = client
    if current and current.is_connected():
        try:
            current.publish(topic, payload, retain=retain)
        except Exception as e:
            logging.debug(f"MQTT publish to {topic} failed: {e}")


def say_goodbye():
    """Service stopping on purpose: send our own "offline" (a clean disconnect() does
    not send the last will) and disconnect. Our "offline" is then not corrected."""
    global going_offline
    going_offline = True
    current = client
    if current and current.is_connected():
        try:
            current.publish(MQTT_STATUS_TOPIC, "offline", retain=True).wait_for_publish(2)
            current.disconnect()
        except Exception as e:
            logging.debug(f"MQTT goodbye failed: {e}")


def mark_going_offline():
    """The service is stopping: the "offline" last will is expected from now on."""
    global going_offline
    going_offline = True


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


def clear_legacy_discovery(mq_client):
    """Remove the entities of older versions (fixed IDs, node "smartframe"): an empty
    retained config tells HA to delete them. Harmless when they are already gone."""
    for topic in ha.legacy_discovery_topics(MQTT_DISCOVERY_PREFIX):
        mq_client.publish(topic, "", retain=True)


def publish_discovery_and_status(mq_client):
    if not mq_client.is_connected():
        return
    modes = supervisor.get_available_modes()
    mq_client.publish(MQTT_STATUS_TOPIC, "online", retain=True)
    mq_client.publish(MQTT_AVAILABLE_MODES_TOPIC, json.dumps(modes), retain=True)
    for topic, payload in ha.discovery_messages(
        MQTT_DISCOVERY_PREFIX, DEVICE_ID, _discovery_topics(), modes,
        COLOR_PRESETS, INPUT_SOURCES,
    ):
        mq_client.publish(topic, json.dumps(payload), retain=True)
    logging.info(
        f"Published Home Assistant MQTT discovery payloads (device '{DEVICE_ID}')."
    )


def _publish_states(mq_client):
    """Every state, retained. States are published when they change: this is only
    for a new connection or a Home Assistant restart."""
    if not mq_client.is_connected():
        return
    working_methods = cache.working_methods
    mq_client.publish(MQTT_STATE_TOPIC, supervisor.current_mode, retain=True)
    mq_client.publish(MQTT_RUN_STATE_TOPIC, supervisor.run_state, retain=True)
    mq_client.publish(MQTT_BRIGHTNESS_STATE_TOPIC, str(working_methods.get("brightness", 100)), retain=True)
    mq_client.publish(MQTT_CONTRAST_STATE_TOPIC, str(working_methods.get("contrast", 50)), retain=True)
    mq_client.publish(
        MQTT_COLOR_PRESET_STATE_TOPIC,
        working_methods.get("color_preset", DEFAULT_COLOR_PRESET), retain=True,
    )
    mq_client.publish(MQTT_INPUT_SOURCE_STATE_TOPIC, working_methods.get("input_source", "HDMI-1"), retain=True)
    publish_started(mq_client, force=True)
    if _last_cpu_temp is not None:
        mq_client.publish(MQTT_CPU_TEMP_STATE_TOPIC, f"{_last_cpu_temp:.1f}", retain=True)


def _announce_later(mq_client):
    """Discovery (then the states) after DISCOVERY_DELAY seconds, in a timer thread."""
    def announce():
        publish_discovery_and_status(mq_client)
        _publish_states(mq_client)
    timer = threading.Timer(DISCOVERY_DELAY, announce)
    timer.daemon = True
    timer.start()


def on_connect(mq_client, userdata, _connect_flags, reason_code, _properties):
    if not reason_code.is_failure:
        logging.info("Connected to MQTT broker.")
        perf.mark("mqtt_connected")  # PERF
        for topic in _command_routes():
            mq_client.subscribe(topic)
        mq_client.subscribe(MQTT_HA_STATUS_TOPIC)
        # Our own status: a stale "offline" last will is caught and corrected
        mq_client.subscribe(MQTT_STATUS_TOPIC)
        logging.info(f"Subscribed to the command topics (device '{DEVICE_ID}').")

        mq_client.publish(MQTT_STATUS_TOPIC, "online", retain=True)
        _publish_states(mq_client)
        # Old entities first; the new ones a moment later, so HA has deleted the old
        # ones and gives the new ones the same entity_id (dashboards keep working).
        clear_legacy_discovery(mq_client)
        _announce_later(mq_client)
    else:
        # No disconnect(): it would stop paho's automatic reconnection for good. paho
        # retries with a growing delay (reconnect_delay_set), so a fixed password or
        # ACL on the broker is picked up without restarting the frame.
        _log_connect_failure(f"MQTT connection refused by the broker: {reason_code}.")
        if reason_code in ["Not authorized", "Bad user name or password"]:
            _log_connect_failure("MQTT authentication failed. Check mqtt.username / "
                                 "mqtt.password in config.yaml (and the broker's users).")


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


def on_connect_fail(mq_client, userdata):
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


def on_disconnect(mq_client, userdata, _flags, reason_code, _properties):
    logging.warning(f"MQTT disconnected ({reason_code}). Will retry automatically.")


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


def on_message(mq_client, userdata, msg):
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
            _announce_later(client)
        return
    if msg.topic == MQTT_STATUS_TOPIC:
        # "offline" while we run: the last will of a connection the broker only now
        # found dead (power cut, Wi-Fi drop). HA would show every entity unavailable.
        if ha.should_reassert_online(msg.payload, going_offline):
            if msg.retain:
                # Delivered on (re)subscribe: the last will of the previous run (a normal
                # service restart). Expected; on_connect has already sent "online" too.
                logging.info("Previous run left 'offline' on the broker: sending 'online' again.")
            else:
                logging.warning("Stale 'offline' status found on the broker: sending 'online' again.")
            client.publish(MQTT_STATUS_TOPIC, "online", retain=True)
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
    get_available_modes = supervisor.get_available_modes
    command = parse_command(cmd, msg.payload, get_available_modes())
    if command is None and cmd == "mode":
        # Maybe a mode added since startup: look at the modes directory again.
        known = get_available_modes()
        command = parse_command(cmd, msg.payload, get_available_modes(refresh=True))
        if get_available_modes() != known:
            logging.info(f"Modes changed: {get_available_modes()}.")
            _announce_later(client)
    if command is None:
        logging.warning(
            f"Invalid {cmd} payload on {msg.topic}: {msg.payload[:100]!r}"
            + (f" (available modes: {get_available_modes()})" if cmd == "mode" else "")
        )
        return
    worker.command_queue.put(command)


def publish_started(mq_client, force=False):
    """Send the start time (ISO 8601). Without `force`, only if the wall clock moved
    by more than a minute since the last value (NTP sync after boot)."""
    global _started_sent
    started = time.time() - (time.monotonic() - _START_MONOTONIC)
    if not force and _started_sent is not None and abs(started - _started_sent) < 60:
        return
    if mq_client and mq_client.is_connected():
        stamp = datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds")
        mq_client.publish(MQTT_STARTED_STATE_TOPIC, stamp, retain=True)
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
        publish(MQTT_CPU_TEMP_STATE_TOPIC, f"{temp:.1f}", retain=True)
