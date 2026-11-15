import datetime
import types

import pytest

from smartframe import screen_watch, supervisor, worker

DAY = datetime.datetime(2026, 11, 13)


def at(hour, minute=0, day=0):
    return DAY + datetime.timedelta(days=day, hours=hour, minutes=minute)


class FakeProcess:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


def test_freeze_needs_the_same_picture_for_the_whole_delay():
    d = screen_watch.FreezeDetector(freeze_seconds=180, grace_seconds=300)
    d.reset(0)
    assert not d.feed("a", 0)
    assert not d.feed("b", 60)
    assert not d.feed("b", 299)  # Still in the grace period
    assert d.feed("b", 300)  # Same picture since 60 s, grace period over
    assert d.changed


def test_a_change_restarts_the_count_and_no_screenshot_is_no_verdict():
    d = screen_watch.FreezeDetector(freeze_seconds=180, grace_seconds=0)
    d.reset(0)
    for t in (0, 60, 120):
        assert not d.feed("a", t)
    assert not d.feed("b", 170)
    assert not d.feed(None, 400)
    assert not d.feed("b", 349)
    assert d.feed("b", 350)


def test_a_page_that_never_loads_is_judged_after_the_grace_period():
    d = screen_watch.FreezeDetector(freeze_seconds=180, grace_seconds=300)
    d.reset(1000)
    for t in range(1000, 1300, 60):
        assert not d.feed("black", t)
    assert d.feed("black", 1300)
    assert not d.changed


def test_daily_restart_due():
    four = (4, 0)
    assert screen_watch.daily_restart_due(at(22, day=-1), at(4, 0), four)
    assert screen_watch.daily_restart_due(at(10, day=-3), at(9), four)  # Missed: as soon as possible
    assert not screen_watch.daily_restart_due(at(22, day=-1), at(3, 59), four)
    assert not screen_watch.daily_restart_due(at(4, 1), at(23), four)  # Restarted today already
    assert not screen_watch.daily_restart_due(at(22, day=-1), at(5), None)


@pytest.fixture
def mirror(monkeypatch):
    """The mirror mode running, a fake screenshot and a command recorder."""
    rec = types.SimpleNamespace(queued=[], digest="a")
    monkeypatch.setattr(supervisor, "current_mode", "mirror")
    monkeypatch.setattr(supervisor, "current_process", FakeProcess())
    monkeypatch.setattr(worker.command_queue, "put", lambda item: rec.queued.append(item))
    watch = screen_watch.MirrorWatch(capture=lambda: rec.digest)
    watch.freeze_seconds, watch.daily = 180, (4, 0)
    watch.detector = screen_watch.FreezeDetector(180, grace_seconds=0)
    rec.watch = watch
    return rec


def test_frozen_mirror_is_refreshed_once(mirror):
    w = mirror.watch
    assert w.tick(0, at(12)) is None
    mirror.digest = "b"
    assert w.tick(60, at(12, 1)) is None
    assert w.tick(180, at(12, 3)) is None
    assert "frozen" in w.tick(240, at(12, 4))
    assert mirror.queued[0][0] == "refresh" and mirror.queued[0][1][0] == "mirror"
    # The worker has not restarted it yet: the same process is seen as a new launch
    assert w.tick(300, at(12, 5)) is None
    assert len(mirror.queued) == 1


def test_static_page_pauses_the_check(mirror, monkeypatch):
    w = mirror.watch
    t = 0
    for restart in range(screen_watch.MAX_FRUITLESS_RESTARTS):
        monkeypatch.setattr(supervisor, "current_process", FakeProcess())  # Relaunched
        while w.tick(t, at(12)) is None:
            t += 60
        t += 60
    assert len(mirror.queued) == screen_watch.MAX_FRUITLESS_RESTARTS
    monkeypatch.setattr(supervisor, "current_process", FakeProcess())
    for _ in range(10):
        t += 60
        assert w.tick(t, at(12)) is None
    # Leaving the mirror mode ends the pause
    monkeypatch.setattr(supervisor, "current_mode", "audio")
    w.tick(t, at(12))
    assert w.fruitless == 0


def test_daily_restart_is_queued(mirror):
    w = mirror.watch
    assert w.tick(0, at(23, day=-1)) is None
    assert w.tick(5 * 3600 + 60, at(4, 1)) == "daily restart"


def test_a_clock_jump_after_boot_is_not_a_night(mirror):
    # Booted with yesterday's clock, set right by NTP a minute later
    w = mirror.watch
    assert w.tick(0, at(21, day=-1)) is None
    assert w.tick(60, at(8)) is None
    assert mirror.queued == []


def test_nothing_happens_in_other_modes(mirror, monkeypatch):
    monkeypatch.setattr(supervisor, "current_mode", "audio")
    w = mirror.watch
    for t in range(0, 3600, 60):
        assert w.tick(t, at(4)) is None
    assert mirror.queued == []


def test_turned_off(mirror):
    w = mirror.watch
    w.freeze_seconds, w.daily = 0, None
    for t in range(0, 3600, 60):
        assert w.tick(t, at(4)) is None
    assert mirror.queued == []


def test_capture_without_labwc_or_grim(monkeypatch):
    monkeypatch.setattr(screen_watch.compositor, "labwc_wayland_display", None)
    assert screen_watch.capture_digest() is None
    monkeypatch.setattr(screen_watch.compositor, "labwc_wayland_display", "wayland-0")

    def missing(*args, **kwargs):
        raise FileNotFoundError("grim")
    monkeypatch.setattr(screen_watch.subprocess, "run", missing)
    assert screen_watch.capture_digest() is None
