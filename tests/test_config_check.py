from smartframe import config_check

MODES = ["audio", "mirror", "off"]


def test_empty_config_is_fine():
    assert config_check.check(None, MODES) == ({}, [])
    assert config_check.check({}, MODES) == ({}, [])


def test_not_a_mapping_is_ignored():
    clean, warnings = config_check.check(["a", "b"], MODES)
    assert clean == {}
    assert len(warnings) == 1


def test_valid_config_is_unchanged():
    config = {
        "debug": True,
        "default_mode": "Audio",
        "screen_size": "1280x800",
        "mqtt": {"port": 8883, "tls": True},
        "audio": {"theme": "eink", "mic_channel": "Right", "capture_bits": 32,
                  "render_scale": 0.75, "noise_floor_dba": None, "device_index": 2,
                  "pixel_shift_minutes": 0},
    }
    clean, warnings = config_check.check(config, MODES)
    assert warnings == []
    assert clean == config


def test_bad_values_are_removed_with_a_warning():
    config = {
        "debug": "maybe",
        "default_mode": "disco",
        "screen_size": "big",
        "mqtt": {"port": "abc", "broker": "10.0.0.1"},
        "audio": {"theme": "pink", "render_scale": 3, "calibration_offset_db": "loud",
                  "device_index": -1, "threshold_db_error": 85},
    }
    clean, warnings = config_check.check(config, MODES)
    assert clean == {"mqtt": {"broker": "10.0.0.1"}, "audio": {"threshold_db_error": 85}}
    assert len(warnings) == 8
    assert any("default_mode" in w and "disco" in w for w in warnings)
    # The input is never modified
    assert config["audio"]["theme"] == "pink"


def test_a_section_that_is_not_a_mapping_is_ignored():
    clean, warnings = config_check.check({"audio": "I2S"}, MODES)
    assert clean == {"audio": {}}
    assert len(warnings) == 1


def test_default_mode_is_not_checked_without_modes():
    assert config_check.check({"default_mode": "disco"}) == ({"default_mode": "disco"}, [])


def test_available_modes(tmp_path):
    for name in ("audio_mode.py", "mirror_mode.sh", "notes.txt", "_mode.py"):
        (tmp_path / name).write_text("")
    assert config_check.available_modes(tmp_path) == ["audio", "mirror", "off"]
    assert config_check.available_modes(tmp_path / "missing") == ["off"]


def test_numbers_and_booleans_written_as_text_are_converted():
    config = {
        "keep_compositor_when_off": "false",
        "mqtt": {"port": "8883", "tls": "yes"},
        "audio": {"calibration_offset_db": "37", "calibration_slope": " 1.1 ",
                  "device_index": "2", "show_debug": "False"},
    }
    clean, warnings = config_check.check(config, MODES)
    assert warnings == []
    assert clean["keep_compositor_when_off"] is False
    assert clean["mqtt"] == {"port": 8883, "tls": True}
    assert clean["audio"] == {"calibration_offset_db": 37, "calibration_slope": 1.1,
                              "device_index": 2, "show_debug": False}


def test_port_must_be_a_whole_number():
    clean, warnings = config_check.check({"mqtt": {"port": 1883.5}}, MODES)
    assert clean == {"mqtt": {}}
    assert len(warnings) == 1
