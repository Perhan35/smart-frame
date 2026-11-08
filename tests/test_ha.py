from smartframe import ha

TOPICS = {k: f"smartframe/frame/{k}" for k in (
    "command", "state", "status", "run_state", "dba", "brightness_cmd", "brightness_state",
    "contrast_cmd", "contrast_state", "color_preset_cmd", "color_preset_state",
    "input_source_cmd", "input_source_state",
)}


def test_device_id_from_hostname():
    assert ha.device_id(hostname="SmartFrame-Living.local") == "smartframe_living"
    assert ha.device_id(hostname="---") == "frame"
    assert ha.device_id("Kitchen Frame", hostname="ignored") == "kitchen_frame"
    assert ha.device_id("  ", hostname="pi") == "pi"


def test_expand_topic():
    assert ha.expand_topic("smartframe/{device_id}/set_mode", "pi") == "smartframe/pi/set_mode"
    assert ha.expand_topic("fixed/topic", "pi") == "fixed/topic"
    assert ha.expand_topic("odd/{other}", "pi") == "odd/{other}"


def _messages(dev_id):
    return ha.discovery_messages("homeassistant", dev_id, TOPICS, ["audio", "off"], ["sRGB"], ["HDMI-1"])


def test_two_frames_never_share_an_id():
    a, b = _messages("living"), _messages("kitchen")
    topics_a = {t for t, _ in a}
    topics_b = {t for t, _ in b}
    assert not topics_a & topics_b
    ids_a = {p["unique_id"] for _, p in a}
    ids_b = {p["unique_id"] for _, p in b}
    assert not ids_a & ids_b
    assert len(ids_a) == len(a)  # Unique inside one frame too


def test_new_ids_never_use_the_legacy_topics():
    legacy = set(ha.legacy_discovery_topics("homeassistant"))
    # Even a Pi called "smartframe" gets other topics than the old fixed ones
    assert not legacy & {t for t, _ in _messages("smartframe")}


def test_payloads_point_to_this_frames_topics():
    for _, payload in _messages("living"):
        assert payload["availability"] == [{"topic": TOPICS["status"]}]
        assert payload["device"]["identifiers"] == ["smartframe_living"]
    run_state = next(p for t, p in _messages("living") if "/run_state/" in t)
    assert run_state["options"] == list(ha.RUN_STATES)
    assert run_state["entity_category"] == "diagnostic"
