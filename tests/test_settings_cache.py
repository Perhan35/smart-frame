import json

import pytest

from smartframe import cache, settings


def test_topics_use_the_device_id():
    assert settings.DEVICE_ID == "testframe"
    assert settings.MQTT_COMMAND_TOPIC == "smartframe/testframe/set_mode"
    assert settings.MQTT_DBA_STATE_TOPIC == "smartframe/testframe/audio/dba"
    assert settings.MQTT_HA_STATUS_TOPIC == "homeassistant/status"


def test_paths_are_absolute_and_in_the_repo():
    assert settings.MODES_DIR.endswith("modes")
    assert settings.MODES_DIR.startswith(settings.ROOT)
    assert settings.CHROMIUM_PROFILE_DIR.startswith(settings.ROOT)


def test_defaults_without_the_optional_keys():
    assert settings.KEEP_COMPOSITOR_WHEN_OFF is True
    assert settings.RESTORE_ON_POWER_ON is True
    assert settings.SCREEN_SIZE == "1920x1080"
    assert settings.DEFAULT_COLOR_PRESET in settings.COLOR_PRESETS


@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    monkeypatch.setattr(cache, "CACHE_FILE", str(path))
    monkeypatch.setattr(cache, "working_methods", dict(cache.working_methods, hardware=[]))
    return path


def test_save_then_load_round_trip(cache_file):
    cache.working_methods["brightness"] = 42
    cache.working_methods["hardware"].append("DDC/CI (Fast Off)")
    cache.save()
    assert json.loads(cache_file.read_text())["brightness"] == 42
    assert not cache_file.with_name("cache.json.tmp").exists()  # Atomic: tmp renamed

    cache.working_methods["brightness"] = 0
    cache.load()
    assert cache.working_methods["brightness"] == 42
    assert cache.working_methods["hardware"] == ["DDC/CI (Fast Off)"]


def test_load_fixes_old_values(cache_file):
    cache_file.write_text(json.dumps({"color_preset": "6500 K", "labwc_path": "/tmp/evil"}))
    cache.load()
    assert cache.working_methods["color_preset"] == settings.DEFAULT_COLOR_PRESET
    assert "labwc_path" not in cache.working_methods


def test_broken_cache_file_is_ignored(cache_file):
    cache_file.write_text("{not json")
    cache.working_methods["contrast"] = 50
    cache.load()  # No exception
    assert cache.working_methods["contrast"] == 50
