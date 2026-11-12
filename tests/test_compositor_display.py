import os
import struct
import subprocess
import types

import pytest

from smartframe import cache, compositor, display, mqtt_link
from smartframe.settings import MQTT_BRIGHTNESS_STATE_TOPIC


def test_invisible_cursor_theme_files(tmp_path):
    compositor._write_invisible_cursor_theme(str(tmp_path))
    cursors = tmp_path / compositor.INVISIBLE_CURSOR_THEME / "cursors"
    names = sorted(p.name for p in cursors.iterdir())
    assert names == sorted(compositor._CURSOR_NAMES)
    data = (cursors / "left_ptr").read_bytes()
    assert data[:4] == b"Xcur"
    assert len(data) == 4 + 12 + 12 + 36 + 4  # Header, table entry, image header, 1 pixel
    width, height = struct.unpack("<II", data[44:52])  # Image header starts at byte 28
    assert (width, height) == (1, 1)
    assert data[-4:] == b"\0\0\0\0"  # Fully transparent pixel


def test_invisible_cursor_env(monkeypatch):
    monkeypatch.setattr(compositor, "labwc_config_dir", "/tmp/labwc-x")
    env = compositor.invisible_cursor_env()
    assert env["XCURSOR_PATH"] == "/tmp/labwc-x/icons"
    assert env["XCURSOR_THEME"] == compositor.INVISIBLE_CURSOR_THEME


def test_kill_own_matches_exact_names_only(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    compositor.kill_own(["chromium", "cog"])
    assert calls == [["pkill", "-x", "-u", str(os.getuid()), "chromium|cog"]]


def test_stop_labwc_resets_the_session(monkeypatch, tmp_path):
    killed = []
    monkeypatch.setattr(compositor, "kill_group", lambda proc, grace=2.0: killed.append(proc))
    monkeypatch.setattr(compositor, "kill_own", lambda names: None)
    monkeypatch.setattr(compositor.time, "sleep", lambda s: None)
    config_dir = tmp_path / "labwc"
    config_dir.mkdir()
    proc = object()
    monkeypatch.setattr(compositor, "labwc_process", proc)
    monkeypatch.setattr(compositor, "labwc_wayland_display", "wayland-0")
    monkeypatch.setattr(compositor, "labwc_config_dir", str(config_dir))
    monkeypatch.setattr(compositor, "display_env_detected", True)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    compositor.stop_labwc()

    assert killed == [proc]
    assert compositor.labwc_process is None
    assert compositor.labwc_wayland_display is None
    assert compositor.labwc_config_dir is None
    assert compositor.display_env_detected is False
    assert not config_dir.exists()
    assert "WAYLAND_DISPLAY" not in os.environ


@pytest.fixture
def screen(monkeypatch):
    """Fake DDC/CI: records the VCP commands; nothing is saved or published for real."""
    sent, published = [], []
    monkeypatch.setattr(cache, "working_methods", dict(cache.working_methods, hardware=[]))
    monkeypatch.setattr(cache, "save", lambda: None)
    monkeypatch.setattr(mqtt_link, "publish", lambda *a, **kw: published.append(a))
    monkeypatch.setattr(display, "_run_vcp_command", lambda code, value: sent.append((code, value)) or True)
    return types.SimpleNamespace(sent=sent, published=published)


def test_brightness_is_clamped_saved_and_published(screen):
    cache.working_methods["brightness"] = 50
    assert display.set_display_brightness(150)
    assert screen.sent == [("10", "100")]
    assert cache.working_methods["brightness"] == 100
    assert screen.published == [(MQTT_BRIGHTNESS_STATE_TOPIC, "100")]


def test_same_brightness_is_not_sent_again(screen):
    cache.working_methods["brightness"] = 60
    assert display.set_display_brightness(60)
    assert screen.sent == []
    display.set_display_brightness(60, force=True)
    assert screen.sent == [("10", "60")]


def test_unknown_color_preset_and_input_are_refused(screen):
    assert display.set_display_color_preset("Rainbow") is False
    assert display.set_display_input_source("SCART") is False
    assert screen.sent == []
    assert display.set_display_color_preset("sRGB")
    assert screen.sent == [("14", "0x01")]


def test_power_on_finds_ddc_and_restores_the_settings(screen, monkeypatch):
    monkeypatch.setattr(compositor, "setup_display_env", lambda force=False: None)
    monkeypatch.setattr(display, "_get_hdmi_output_name", lambda: "HDMI-A-1")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    cache.working_methods["session_type"] = None
    commands = []

    def fake_run(cmd, **kw):
        commands.append(cmd)
        return types.SimpleNamespace(returncode=0 if cmd[0] == "ddcutil" else 1, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(display, "RESTORE_ON_POWER_ON", True)

    display.set_display_power(True)

    assert ["ddcutil", "setvcp", "D6", "0x01"] in commands
    assert cache.working_methods["hardware"] == ["DDC/CI (Fast Off)"]
    assert [code for code, _ in screen.sent] == ["10", "12", "14"]  # Brightness, contrast, color


def test_power_on_without_restore(screen, monkeypatch):
    monkeypatch.setattr(compositor, "setup_display_env", lambda force=False: None)
    monkeypatch.setattr(display, "_get_hdmi_output_name", lambda: "HDMI-A-1")
    cache.working_methods["hardware"] = ["DDC/CI (Fast Off)"]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(display, "RESTORE_ON_POWER_ON", False)
    display.set_display_power(True)
    assert screen.sent == []


def test_failing_cached_method_is_dropped(screen, monkeypatch):
    monkeypatch.setattr(compositor, "setup_display_env", lambda force=False: None)
    monkeypatch.setattr(display, "_get_hdmi_output_name", lambda: "HDMI-A-1")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    cache.working_methods["session_type"] = None
    cache.working_methods["hardware"] = ["HDMI-CEC"]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=1, stderr=""))
    monkeypatch.setattr(display, "FB_BLANK_PATH", "/nonexistent/blank")
    display.set_display_power(False)
    assert cache.working_methods["hardware"] == []
