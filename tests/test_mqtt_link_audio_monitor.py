import math
import os
import types

import pytest

from smartframe import audio_monitor, cache, mqtt_link, supervisor, worker
from smartframe.settings import (
    MQTT_COMMAND_TOPIC,
    MQTT_CPU_TEMP_STATE_TOPIC,
    MQTT_HA_STATUS_TOPIC,
    MQTT_RESTART_COMMAND_TOPIC,
    MQTT_STATUS_TOPIC,
)


def message(topic, payload, retain=False):
    return types.SimpleNamespace(topic=topic, payload=payload, retain=retain)


class FakeClient:
    def __init__(self):
        self.published = []

    def is_connected(self):
        return True

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))


@pytest.fixture
def mqtt(monkeypatch):
    rec = types.SimpleNamespace(queued=[], announced=0, client=FakeClient())
    monkeypatch.setattr(worker.command_queue, "put", lambda item: rec.queued.append(item))

    def announce(client):
        rec.announced += 1
    monkeypatch.setattr(mqtt_link, "_announce_later", announce)
    monkeypatch.setattr(mqtt_link, "client", rec.client)
    monkeypatch.setattr(mqtt_link, "going_offline", False)
    monkeypatch.setattr(supervisor, "get_available_modes", lambda refresh=False: ["audio", "mirror", "off"])
    return rec


def test_valid_mode_is_queued(mqtt):
    mqtt_link.on_message(None, None, message(MQTT_COMMAND_TOPIC, b"Mirror"))
    assert mqtt.queued == [("mode", "mirror")]


def test_invalid_bytes_never_reach_paho(mqtt):
    # A non-UTF-8 payload used to kill paho's network thread for good.
    mqtt_link.on_message(None, None, message(MQTT_COMMAND_TOPIC, b"\xff\xfe"))
    assert mqtt.queued == []


def test_exception_in_a_handler_is_swallowed(mqtt, monkeypatch):
    def boom(msg):
        raise RuntimeError("bug")
    monkeypatch.setattr(mqtt_link, "_handle_message", boom)
    mqtt_link.on_message(None, None, message(MQTT_COMMAND_TOPIC, b"audio"))  # No exception


def test_retained_restart_is_ignored(mqtt):
    mqtt_link.on_message(None, None, message(MQTT_RESTART_COMMAND_TOPIC, b"PRESS", retain=True))
    assert mqtt.queued == []
    mqtt_link.on_message(None, None, message(MQTT_RESTART_COMMAND_TOPIC, b"PRESS"))
    assert mqtt.queued == [("service_restart", None)]


def test_home_assistant_restart_sends_discovery_again(mqtt):
    mqtt_link.on_message(None, None, message(MQTT_HA_STATUS_TOPIC, b"online", retain=True))
    assert mqtt.announced == 0  # Delivered on every connection: on_connect announces
    mqtt_link.on_message(None, None, message(MQTT_HA_STATUS_TOPIC, b"online"))
    assert mqtt.announced == 1


def test_stale_offline_is_corrected_unless_stopping(mqtt, monkeypatch):
    mqtt_link.on_message(None, None, message(MQTT_STATUS_TOPIC, b"offline", retain=True))
    assert mqtt.client.published == [(MQTT_STATUS_TOPIC, "online", True)]
    monkeypatch.setattr(mqtt_link, "going_offline", True)
    mqtt_link.on_message(None, None, message(MQTT_STATUS_TOPIC, b"offline"))
    assert len(mqtt.client.published) == 1


def test_cpu_temperature_sent_only_when_it_moves(mqtt, monkeypatch):
    sent = []
    monkeypatch.setattr(mqtt_link, "publish", lambda topic, payload, retain=False: sent.append(payload))
    monkeypatch.setattr(mqtt_link, "_last_cpu_temp", None)
    temps = iter([45.0, 45.3, 45.6])
    monkeypatch.setattr(mqtt_link, "read_cpu_temp", lambda: next(temps))
    for _ in range(3):
        mqtt_link.publish_cpu_temp()
    assert sent == ["45.0", "45.6"]


def test_states_after_connecting(mqtt, monkeypatch):
    monkeypatch.setattr(supervisor, "current_mode", "audio")
    monkeypatch.setattr(mqtt_link, "_last_cpu_temp", 41.25)
    monkeypatch.setattr(cache, "working_methods", dict(cache.working_methods, brightness=70))
    mqtt_link._publish_states(mqtt.client)
    topics = {topic: payload for topic, payload, _ in mqtt.client.published}
    assert topics[MQTT_CPU_TEMP_STATE_TOPIC] == "41.2"
    assert "audio" in topics.values()
    assert "70" in topics.values()


def test_create_client_without_broker():
    assert mqtt_link.create_client() is None  # Placeholder broker in the test config
    assert mqtt_link.client.on_message is mqtt_link.on_message


def test_level_pipe_keeps_only_valid_lines(monkeypatch):
    import queue
    levels = queue.Queue(maxsize=5)
    monkeypatch.setattr(audio_monitor, "_external_levels", levels)
    r, w = os.pipe()
    os.write(w, b"41.5 48.0\nhello\nnan 3\n42 50 extra\n7\n")
    os.close(w)
    audio_monitor.read_level_pipe(r)
    got = []
    while not levels.empty():
        got.append(levels.get_nowait())
    assert got == [(41.5, 48.0), (42.0, 50.0)]
    assert all(math.isfinite(v) for pair in got for v in pair)


def test_configured_mic_index_wins(monkeypatch):
    monkeypatch.setitem(audio_monitor.AUDIO_CONFIG, "device_index", 3)
    assert audio_monitor.discover_audio_device() == 3


def test_cached_mic_index_is_used(monkeypatch):
    monkeypatch.setitem(audio_monitor.AUDIO_CONFIG, "device_index", None)
    monkeypatch.setattr(cache, "working_methods", dict(cache.working_methods, audio_device=1))
    assert audio_monitor.discover_audio_device() == 1


def test_monitor_warns_once_about_a_silent_mic(monkeypatch, caplog):
    client = FakeClient()
    mon = audio_monitor.AudioMonitor(client)
    silent = mon.meter_settings["calibration_offset_db"] - 120
    mon._publish((silent, silent))
    mon._publish((silent, silent))
    assert sum("only zeros" in r.message for r in caplog.records) == 1
    assert len(client.published) == 4  # LAeq + LAFmax, twice
