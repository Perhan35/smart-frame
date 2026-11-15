"""Mirror mode health: restart a frozen browser, and restart it once a day.

A hung web page keeps its browser process alive, so the crash watchdog sees
nothing wrong, but the picture stops changing (the clock too). Every CHECK_EVERY
seconds a screenshot of the compositor (grim) is hashed. When the hash stays
the same for magic_mirror.freeze_restart_minutes, the mode is restarted. The
daily restart (magic_mirror.daily_restart) frees the memory a browser slowly piles
up after days of running.

Runs in its own thread: a screenshot takes up to a second on a Pi Zero 2 (more
when it swaps), and must neither hold mode_lock nor delay the main loop. It only queues a "refresh"
command; the worker restarts the mode under mode_lock (supervisor.refresh_mode).
"""

import datetime
import hashlib
import logging
import os
import subprocess
import threading
import time

from smartframe import compositor, supervisor, worker
from smartframe.config_check import parse_daily_time
from smartframe.settings import config

WATCHED_MODE = "mirror"
CHECK_EVERY = 60.0  # Seconds between two screenshots
# No verdict this soon after a launch: MagicMirror can take minutes to show its
# modules when the Pi is short of RAM. A page still unchanged after this is stuck.
GRACE_SECONDS = 300.0
# Freeze restarts in a row of a page that never changed at all: the page is
# probably static (no clock), so the check pauses until the next mode switch.
MAX_FRUITLESS_RESTARTS = 2
GRIM_TIMEOUT = 20.0

DEFAULT_FREEZE_MINUTES = 3
DEFAULT_DAILY_RESTART = "04:00"


def _settings():
    """(freeze seconds or 0, (hour, minute) or None) from config.yaml (already checked)."""
    mirror = config.get("magic_mirror") or {}
    minutes = mirror.get("freeze_restart_minutes")
    if minutes is None:
        minutes = DEFAULT_FREEZE_MINUTES
    try:
        daily = parse_daily_time(mirror.get("daily_restart", DEFAULT_DAILY_RESTART))
    except ValueError:  # config_check already warned and removed it
        daily = parse_daily_time(DEFAULT_DAILY_RESTART)
    return float(minutes) * 60.0, daily


class FreezeDetector:
    """Says when the screen has not changed for `freeze_seconds`.

    feed() gets one screenshot hash at a time. No verdict during `grace_seconds`
    after reset() (the page is still loading), and none without a screenshot.
    """

    def __init__(self, freeze_seconds, grace_seconds=GRACE_SECONDS):
        self.freeze_seconds = freeze_seconds
        self.grace_seconds = grace_seconds
        self.reset(0.0)

    def reset(self, now):
        self.started_at = now
        self.last_digest = None
        self.same_since = now
        self.changed = False  # The picture changed at least once since reset()

    def feed(self, digest, now):
        if digest is None:
            return False
        if digest != self.last_digest:
            if self.last_digest is not None:
                self.changed = True
            self.last_digest = digest
            self.same_since = now
            # Not an early verdict: a screen that only changes during the grace
            # period is judged from its last change, after the grace period.
            return False
        if now - self.started_at < self.grace_seconds:
            return False
        return now - self.same_since >= self.freeze_seconds


def daily_restart_due(launched_at, now, daily):
    """True when today's restart time is past and the browser started before it.

    launched_at, now: local datetimes. daily: (hour, minute) or None. After the
    restart, the new launch is later than the restart time: not due again today.
    """
    if daily is None:
        return False
    target = now.replace(hour=daily[0], minute=daily[1], second=0, microsecond=0)
    return launched_at < target <= now


def capture_digest():
    """Hash of a screenshot of labwc's output, or None if there is none.

    Full size on purpose: on a Pi Zero 2, grim's scaling costs ~2.8 s of CPU per
    picture, a raw full-size picture (PPM, ~6 MB) ~0.1 s, plus ~30 ms to hash it.
    """
    display = compositor.labwc_wayland_display
    if not display:
        return None  # No labwc session of ours (X11 or desktop fallback): no check
    env = dict(os.environ, WAYLAND_DISPLAY=display)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        result = subprocess.run(
            ["grim", "-t", "ppm", "-"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=GRIM_TIMEOUT,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        logging.warning("Mirror screen check: screenshot timed out.")
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return hashlib.sha1(result.stdout).hexdigest()


class MirrorWatch:
    """State of the checks for the browser process currently shown."""

    def __init__(self, capture=capture_digest):
        self.capture = capture
        self.freeze_seconds, self.daily = _settings()
        self.detector = FreezeDetector(self.freeze_seconds)
        self.proc = None
        self.launched_mono = None  # time.monotonic() of the launch being watched
        self.fruitless = 0  # Freeze restarts in a row of a page that never changed

    def tick(self, mono=None, wall=None):
        """One check (from the watch thread). Returns the reason of a queued restart."""
        mono = time.monotonic() if mono is None else mono
        wall = datetime.datetime.now() if wall is None else wall
        proc = supervisor.current_process
        if supervisor.current_mode != WATCHED_MODE or proc is None or proc.poll() is not None:
            self.proc = None
            if supervisor.current_mode != WATCHED_MODE:
                self.fruitless = 0  # A new choice of the mirror mode starts afresh
            return None
        if proc is not self.proc:  # A new launch (user switch, retry or our restart)
            self.proc = proc
            self.launched_mono = mono
            self.detector.reset(mono)

        # Launch time from the monotonic clock: right after boot, the wall clock can
        # jump forward (NTP) and must not make a browser started "before 04:00".
        launched_at = wall - datetime.timedelta(seconds=mono - self.launched_mono)
        if daily_restart_due(launched_at, wall, self.daily):
            return self._restart("daily restart")

        if self.freeze_seconds <= 0 or self.fruitless >= MAX_FRUITLESS_RESTARTS:
            return None
        if self.detector.feed(self.capture(), mono):
            minutes = (mono - self.detector.same_since) / 60.0
            if self.detector.changed:
                self.fruitless = 0
            else:
                self.fruitless += 1
                if self.fruitless >= MAX_FRUITLESS_RESTARTS:
                    logging.warning(
                        "Mirror screen check: the page never changes, even after a restart "
                        "(a page without a clock?). Check paused until the next mode switch. "
                        "Set magic_mirror.freeze_restart_minutes: 0 to turn it off."
                    )
            return self._restart(f"screen unchanged for {minutes:.0f} min (frozen page)")
        return None

    def _restart(self, reason):
        self.proc = None  # The next tick sees the new process
        worker.command_queue.put(("refresh", (WATCHED_MODE, reason)))
        return reason


def _watch_loop(watch):
    while True:
        time.sleep(CHECK_EVERY)
        try:
            watch.tick()
        except Exception:
            logging.exception("Mirror screen check failed (continuing).")


def start():
    """Start the watch thread, unless both checks are turned off."""
    watch = MirrorWatch()
    if watch.freeze_seconds <= 0 and watch.daily is None:
        return None
    threading.Thread(target=_watch_loop, args=(watch,), daemon=True, name="mirror-watch").start()
    return watch
