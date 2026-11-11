import yaml

import update_config

EXAMPLE = """mqtt:
  broker: "[MQTT_SERVER_IP_ADDRESS]"
  topic: "smartframe/{device_id}/set_mode" # Command topic
  status_topic: "smartframe/{device_id}/status"
  device_id: ""

audio:
  device_index: null
  show_debug: true # CPU stats
"""


def test_deep_merge_adds_missing_keys_only():
    base = {"mqtt": {"broker": "10.0.0.2"}}
    merged, added = update_config.deep_merge(base, {"mqtt": {"broker": "x", "port": 1883}, "debug": False})
    assert merged == {"mqtt": {"broker": "10.0.0.2", "port": 1883}, "debug": False}
    assert sorted(added) == ["debug", "mqtt.port"]


def test_find_unknown_never_removes():
    config = {"mqtt": {"broker": "x", "mine": 1}, "extra": True}
    assert sorted(update_config.find_unknown(config, {"mqtt": {"broker": "y"}})) == ["extra", "mqtt.mine"]
    assert config["mqtt"]["mine"] == 1


def test_insert_keys_as_text_copies_comment():
    config_text = 'mqtt:\n  broker: "10.0.0.2" # mine\n\naudio:\n  device_index: 2\n'
    new = update_config.insert_keys_as_text(config_text, EXAMPLE, ["audio.show_debug"])
    assert "  show_debug: true # CPU stats" in new
    assert "# mine" in new
    assert yaml.safe_load(new)["audio"] == {"device_index": 2, "show_debug": True}


def test_migrate_topics_only_old_defaults():
    config_text = (
        'mqtt:\n'
        '  topic: "smartframe/set_mode" # old default\n'
        '  status_topic: "home/frame/status" # changed by the user\n'
    )
    config = yaml.safe_load(config_text)
    example = yaml.safe_load(EXAMPLE)
    new_text, migrated = update_config.migrate_topics(config_text, config, example)
    loaded = yaml.safe_load(new_text)
    assert loaded["mqtt"]["topic"] == "smartframe/{device_id}/set_mode"
    assert loaded["mqtt"]["status_topic"] == "home/frame/status"
    assert "# old default" in new_text
    assert len(migrated) == 1
    assert config["mqtt"]["topic"] == "smartframe/{device_id}/set_mode"


def test_obsolete_keys_are_not_reported():
    config = {"audio": {"microphone": "I2S", "mine": 1}}
    assert update_config.find_unknown(config, {"audio": {}}) == ["audio.mine"]
