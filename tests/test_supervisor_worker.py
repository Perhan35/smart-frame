import types

import pytest

from smartframe import compositor, display, mqtt_link, supervisor, worker
from smartframe.settings import MQTT_RUN_STATE_TOPIC, MQTT_STATE_TOPIC


class FakeProcess:
    def __init__(self, code=None):
        self.code = code
        self.args = ["python3", "audio_mode.py"]
        self.pid = 1234

    def poll(self):
        return self.code


@pytest.fixture
def frame(monkeypatch):
    """Fresh supervisor state; screen, labwc and MQTT replaced by recorders."""
    rec = types.SimpleNamespace(published=[], power=[], labwc_stopped=0, queued=[], launched=[])
    for name, value in {
        "current_process": None, "current_mode": "off", "run_state": supervisor.RUN_OFF,
        "_restart_attempts": 0, "_pending_restart": None, "_mode_started_at": 0.0,
    }.items():
        monkeypatch.setattr(supervisor, name, value)
    monkeypatch.setattr(mqtt_link, "publish", lambda topic, payload, retain=False: rec.published.append((topic, payload)))
    monkeypatch.setattr(display, "set_display_power", lambda state: rec.power.append(state))

    def stop_labwc():
        rec.labwc_stopped += 1
    monkeypatch.setattr(compositor, "stop_labwc", stop_labwc)
    monkeypatch.setattr(compositor, "kill_group", lambda proc, grace=2.0: None)
    monkeypatch.setattr(supervisor.time, "sleep", lambda s: None)
    monkeypatch.setattr(worker.command_queue, "put", lambda item: rec.queued.append(item))

    def launch(mode, previous):
        rec.launched.append((mode, previous))
        return FakeProcess()
    monkeypatch.setattr(supervisor, "_launch_mode", launch)
    monkeypatch.setattr(supervisor, "get_available_modes", lambda refresh=False: ["audio", "mirror", "off"])
    return rec


def run_states(rec):
    return [payload for topic, payload in rec.published if topic == MQTT_RUN_STATE_TOPIC]


def test_start_mode_publishes_the_choice_then_starting(frame):
    supervisor.start_mode("Audio")
    assert supervisor.current_mode == "audio"
    assert isinstance(supervisor.current_process, FakeProcess)
    assert frame.launched == [("audio", "off")]
    assert (MQTT_STATE_TOPIC, "audio") in frame.published
    assert run_states(frame) == [supervisor.RUN_STARTING]


def test_same_running_mode_is_not_restarted(frame):
    supervisor.start_mode("audio")
    supervisor.start_mode("audio")
    assert len(frame.launched) == 1


def test_unknown_mode_switches_off_as_failed(frame):
    supervisor.start_mode("disco")
    assert supervisor.current_mode == "off"
    assert frame.power == [False]
    assert run_states(frame)[-1] == supervisor.RUN_FAILED


def test_off_keeps_labwc_unless_failed(frame, monkeypatch):
    monkeypatch.setattr(supervisor, "KEEP_COMPOSITOR_WHEN_OFF", True)
    supervisor.start_mode("audio")
    supervisor.start_mode("off")
    assert frame.labwc_stopped == 0
    assert run_states(frame)[-1] == supervisor.RUN_OFF
    monkeypatch.setattr(supervisor, "KEEP_COMPOSITOR_WHEN_OFF", False)
    supervisor.start_mode("audio")
    supervisor.start_mode("off")
    assert frame.labwc_stopped == 1


def test_running_after_a_few_seconds(frame, monkeypatch):
    supervisor.start_mode("audio")
    supervisor.supervise_mode()
    assert supervisor.run_state == supervisor.RUN_STARTING
    monkeypatch.setattr(supervisor, "_mode_started_at", supervisor.time.monotonic() - supervisor.RUNNING_AFTER)
    supervisor.supervise_mode()
    assert supervisor.run_state == supervisor.RUN_RUNNING


def test_crash_is_retried_then_gives_up(frame, monkeypatch):
    supervisor.start_mode("audio")
    for attempt in range(len(supervisor.RESTART_DELAYS)):
        supervisor.current_process.code = 1  # The mode crashes
        supervisor.supervise_mode()
        assert supervisor.run_state == supervisor.RUN_RETRYING
        assert supervisor.current_process is None
        # The retry fires once its delay is over: the worker gets a "restart" command
        mode, _ = supervisor._pending_restart
        monkeypatch.setattr(supervisor, "_pending_restart", (mode, 0.0))
        supervisor.supervise_mode()
        assert frame.queued[-1] == ("restart", "audio")
        supervisor.start_mode("audio", restart=True)
    supervisor.current_process.code = 1
    supervisor.supervise_mode()
    assert supervisor.current_mode == "off"
    assert supervisor.run_state == supervisor.RUN_FAILED


def test_a_user_choice_cancels_the_pending_retry(frame):
    supervisor.start_mode("audio")
    supervisor.current_process.code = 1
    supervisor.supervise_mode()
    assert supervisor._pending_restart is not None
    supervisor.start_mode("mirror")
    assert supervisor._pending_restart is None
    assert supervisor._restart_attempts == 0


def test_launch_failure_counts_as_a_crash(frame, monkeypatch):
    def broken(mode, previous):
        raise RuntimeError("no script")
    monkeypatch.setattr(supervisor, "_launch_mode", broken)
    supervisor.start_mode("audio")
    assert supervisor.current_process is None
    assert supervisor.run_state == supervisor.RUN_RETRYING


def test_preload_backoff_grows_and_is_capped(monkeypatch):
    monkeypatch.setattr(supervisor, "_preload_failures", 0)
    monkeypatch.setattr(supervisor, "_preload_next_try", 0.0)
    now = supervisor.time.monotonic()
    supervisor._preload_failed("test")
    assert supervisor._preload_next_try - now == pytest.approx(supervisor.PRELOAD_AFTER * 2, abs=1)
    for _ in range(20):
        supervisor._preload_failed("test")
    assert supervisor._preload_next_try - now <= supervisor.PRELOAD_RETRY_MAX + 1


def test_worker_retry_only_if_still_wanted(frame, monkeypatch):
    started = []
    monkeypatch.setattr(supervisor, "start_mode", lambda mode, restart=False: started.append((mode, restart)))
    monkeypatch.setattr(supervisor, "current_mode", "audio")
    worker.run_command("restart", "audio")
    assert started == [("audio", True)]
    monkeypatch.setattr(supervisor, "current_mode", "mirror")  # User chose another mode
    worker.run_command("restart", "audio")
    assert len(started) == 1


def test_worker_dispatches_display_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(display, "set_display_brightness", lambda v: calls.append(("brightness", v)))
    monkeypatch.setattr(display, "set_display_input_source", lambda v: calls.append(("input", v)))
    worker.run_command("brightness", 40)
    worker.run_command("input_source", "HDMI-2")
    assert calls == [("brightness", 40), ("input", "HDMI-2")]


def test_launch_mode_environment_and_level_pipe(monkeypatch):
    """The real _launch_mode with a fake Popen: env vars, labwc display, private pipe."""
    import os

    from smartframe import audio_monitor, cache
    popens, readers = [], []

    class FakePopen(FakeProcess):
        def __init__(self, cmd, env, pass_fds=(), **kw):
            super().__init__()
            self.cmd, self.env, self.pass_fds = cmd, env, pass_fds
            popens.append(self)

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(compositor, "setup_display_env", lambda force=False: None)
    monkeypatch.setattr(compositor, "ensure_labwc", lambda: "wayland-1")
    monkeypatch.setattr(compositor, "labwc_process", object())
    monkeypatch.setattr(compositor, "labwc_wayland_display", "wayland-1")
    monkeypatch.setattr(compositor, "labwc_config_dir", "/tmp/labwc-test")
    monkeypatch.setattr(audio_monitor, "discover_audio_device", lambda: 2)
    monkeypatch.setattr(audio_monitor, "start_level_reader", lambda fd: readers.append(fd))
    monkeypatch.setattr(cache, "working_methods", dict(cache.working_methods, audio_device_name="mic"))
    monkeypatch.setattr(supervisor, "_preloaded_audio", None)

    proc = supervisor._launch_mode("audio", "audio")  # Screen already on: no power thread
    assert proc is popens[0]
    assert proc.cmd[-1].endswith("audio_mode.py")
    env = proc.env
    assert env["SMARTFRAME_AUDIO_DEVICE"] == "2"
    assert env["SMARTFRAME_AUDIO_DEVICE_NAME"] == "mic"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["XCURSOR_PATH"] == "/tmp/labwc-test/icons"
    level_w = int(env["SMARTFRAME_LEVEL_FD"])
    assert proc.pass_fds == (level_w,)
    assert len(readers) == 1  # Our read end goes to the level reader...
    os.close(readers[0])
    with pytest.raises(OSError):
        os.fstat(level_w)  # ...and our copy of the write end is closed (EOF on exit)

    proc = supervisor._launch_mode("mirror", "audio")
    assert proc.cmd[0] == "bash"
    assert "MIRROR_URL" in proc.env
    assert "SMARTFRAME_LEVEL_FD" not in proc.env
    assert proc.pass_fds == ()
