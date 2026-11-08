"""Home Assistant MQTT discovery: one device per frame, named after its hostname.

Every ID (discovery topic, unique_id, device identifier) contains the device ID,
so two frames on the same broker never overwrite each other. Topics in
config.yaml may contain "{device_id}", replaced by the same ID.
"""

import re
import socket

DEVICE_PLACEHOLDER = "{device_id}"

RUN_STATES = ("off", "starting", "running", "retrying", "failed")

# Discovery topics used before IDs were per device (node "smartframe"). They are
# cleared on connect, so Home Assistant removes the old entities and creates the
# new ones under the same names.
_LEGACY_ENTITIES = (
    ("select", "mode"),
    ("number", "brightness"),
    ("number", "contrast"),
    ("select", "color_preset"),
    ("select", "input_source"),
    ("sensor", "dba"),
)


def device_id(override=None, hostname=None):
    """mqtt.device_id from config.yaml if set, otherwise the hostname, cleaned up
    to [a-z0-9_] (the characters HA and MQTT accept everywhere)."""
    raw = str(override or "").strip() or hostname or socket.gethostname()
    raw = raw.split(".")[0]  # "frame.local" -> "frame"
    cleaned = re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")
    return cleaned or "frame"


def expand_topic(topic, dev_id):
    """Replace "{device_id}" in a topic (plain replace: other braces stay as they are)."""
    return str(topic).replace(DEVICE_PLACEHOLDER, dev_id)


def node_id(dev_id):
    return f"smartframe_{dev_id}"


def legacy_discovery_topics(prefix):
    return [f"{prefix}/{component}/smartframe/{obj}/config" for component, obj in _LEGACY_ENTITIES]


def discovery_messages(prefix, dev_id, topics, modes, color_presets, input_sources):
    """[(topic, payload dict)] for every entity of this frame.

    `topics` maps: command, state, status, run_state, dba, brightness_cmd,
    brightness_state, contrast_cmd, contrast_state, color_preset_cmd,
    color_preset_state, input_source_cmd, input_source_state.
    Entity names are unchanged from older versions, so HA gives the new entities
    the same entity_id once the old ones are removed.
    """
    node = node_id(dev_id)
    device_info = {
        "identifiers": [node],
        "name": "Smart Frame",
        "manufacturer": "Custom",
        "model": "Smart Frame V1",
    }
    availability = [{"topic": topics["status"]}]
    messages = []

    def add(component, obj, payload):
        payload = dict(payload)
        payload["unique_id"] = f"{node}_{obj}"
        payload["availability"] = availability
        payload["device"] = device_info
        messages.append((f"{prefix}/{component}/{node}/{obj}/config", payload))

    add("select", "mode", {
        "name": "SmartFrame Mode",
        "command_topic": topics["command"],
        "state_topic": topics["state"],
        "options": list(modes),
    })
    add("number", "brightness", {
        "name": "SmartFrame Brightness",
        "command_topic": topics["brightness_cmd"],
        "state_topic": topics["brightness_state"],
        "min": 0, "max": 100, "step": 5,
        "unit_of_measurement": "%",
        "icon": "mdi:brightness-6",
    })
    add("number", "contrast", {
        "name": "SmartFrame Contrast",
        "command_topic": topics["contrast_cmd"],
        "state_topic": topics["contrast_state"],
        "min": 0, "max": 100, "step": 5,
        "unit_of_measurement": "%",
        "icon": "mdi:contrast",
    })
    add("select", "color_preset", {
        "name": "SmartFrame Color Profile",
        "command_topic": topics["color_preset_cmd"],
        "state_topic": topics["color_preset_state"],
        "options": list(color_presets),
        "icon": "mdi:palette",
    })
    add("select", "input_source", {
        "name": "SmartFrame Input",
        "command_topic": topics["input_source_cmd"],
        "state_topic": topics["input_source_state"],
        "options": list(input_sources),
        "icon": "mdi:video-input-hdmi",
    })
    # Sound level (LAeq of each second). device_class + state_class give long-term
    # statistics in HA.
    add("sensor", "dba", {
        "name": "SmartFrame Ambient Sound",
        "state_topic": topics["dba"],
        "unit_of_measurement": "dBA",
        "device_class": "sound_pressure",
        "state_class": "measurement",
        "suggested_display_precision": 1,
        "icon": "mdi:ear-hearing",
    })
    # What the mode process is really doing (the mode selector shows the choice
    # right away, this shows whether it actually runs).
    add("sensor", "run_state", {
        "name": "SmartFrame Run State",
        "state_topic": topics["run_state"],
        "device_class": "enum",
        "options": list(RUN_STATES),
        "entity_category": "diagnostic",
        "icon": "mdi:state-machine",
    })
    return messages
