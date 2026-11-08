import yaml

from smartframe import config_edit

TEXT = """mqtt:
  broker: "1.2.3.4" # keep me
  topic: "smartframe/set_mode"

audio:
  device_index: null # Change to an integer
  noise_floor_dba: 30.0
"""


def test_set_value_keeps_comments():
    new = config_edit.set_value(TEXT, "audio", "device_index", 3)
    assert "  device_index: 3 # Change to an integer\n" in new
    assert "# keep me" in new
    assert yaml.safe_load(new)["audio"]["device_index"] == 3


def test_set_value_only_in_its_section():
    text = "a:\n  x: 1\nb:\n  x: 2\n"
    assert yaml.safe_load(config_edit.set_value(text, "b", "x", 5)) == {"a": {"x": 1}, "b": {"x": 5}}


def test_set_value_strings_and_null():
    new = config_edit.set_value(TEXT, "mqtt", "topic", "smartframe/{device_id}/set_mode")
    assert yaml.safe_load(new)["mqtt"]["topic"] == "smartframe/{device_id}/set_mode"
    new = config_edit.set_value(TEXT, "audio", "noise_floor_dba", None)
    assert yaml.safe_load(new)["audio"]["noise_floor_dba"] is None


def test_set_value_missing_key():
    assert config_edit.set_value(TEXT, "audio", "nope", 1) is None
    assert config_edit.set_value(TEXT, "nope", "topic", 1) is None
