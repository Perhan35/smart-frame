"""Mode lifecycle: start / stop the mode processes, watchdog and crash retries, run
state for Home Assistant, and the optional preloaded audio process.

This module owns current_mode, current_process and run_state. They change only
while mode_lock is held (by the command worker or the main loop's watchdog), so a
mode switch and a crash recovery can never run at the same time. Other modules
read them as supervisor.current_mode etc.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time

from smartframe import (
    audio_monitor,
    cache,
    compositor,
    config_check,
    display,
    ha,
    mqtt_link,
    perf,
    settings,
    worker,
)
from smartframe.settings import (
    AUDIO_CONFIG,
    KEEP_COMPOSITOR_WHEN_OFF,
    MODES_DIR,
    MQTT_RUN_STATE_TOPIC,
    MQTT_STATE_TOPIC,
    SCREEN_SIZE,
    config,
)

current_process = None
current_mode = "off"
mode_lock = threading.Lock()

# Crash recovery (watchdog): relaunch a crashed mode after these delays (seconds),
# then give up and switch to "off". The counter resets once a mode runs this long.
RESTART_DELAYS = (3, 10, 30)
RESTART_STABLE_AFTER = 60.0
_restart_attempts = 0
_pending_restart = None  # (mode, monotonic time) of the next relaunch, or None
_mode_started_at = 0.0

# Mode lifecycle, shown in Home Assistant as a diagnostic sensor (run_state_topic):
#   off -> starting -> running -> (process exits) retrying -> starting -> ...
#   ... -> failed (after the last retry; the mode is then "off" until a new choice)
# The mode selector still shows the user's choice right away; this tells whether it
# actually runs. Changed only by whoever holds mode_lock.
RUN_OFF, RUN_STARTING, RUN_RUNNING, RUN_RETRYING, RUN_FAILED = ha.RUN_STATES
RUNNING_AFTER = 3.0  # A mode counts as running once its process has lived this long
run_state = RUN_OFF

# audio.preload: keep an audio mode process ready in the background, with its slow
# imports (pygame, numpy) already done. Switching to "audio" then only has to open
# the screen and the mic. Costs the RAM of that waiting process. Off by default.
AUDIO_PRELOAD = bool(AUDIO_CONFIG.get("preload", False))
PRELOAD_AFTER = 20.0  # Seconds after a mode switch before preloading (leave the CPU to it)
PRELOAD_RETRY_MAX = 600.0  # A preload that keeps failing is retried at most this rarely
_preloaded_audio = None  # Popen of the waiting process, or None
_preload_failures = 0  # Preloads in a row that died before being used
_preload_next_try = 0.0  # monotonic time before which no new preload is started
_last_switch_at = time.monotonic()  # No preload right at boot: the first mode starts first

_available_modes_cache = []


def get_available_modes(refresh=False):
    """Mode names from the modes directory, cached. refresh=True scans it again (a
    mode added after startup is then accepted without restarting the service)."""
    global _available_modes_cache
    if refresh or not _available_modes_cache:
        _available_modes_cache = config_check.available_modes(MODES_DIR)
    return _available_modes_cache


def stop_current_mode():
    """Stop the current mode's client process. The labwc compositor is kept alive for fast switching."""
    global current_process
    if current_process:
        logging.info("Stopping current mode process...")
        # Only the shell modes (mirror) start a browser: no browser cleanup after a
        # Python mode (audio), which saves a pkill run on every switch.
        started_browser = any(str(arg).endswith(".sh") for arg in current_process.args)
        # The mode runs in its own session (start_new_session=True), so killing its
        # group also stops the browser it started. A kiosk browser has nothing to
        # save and needs more than 2 s to shut down cleanly on a Pi Zero 2: it only
        # gets 0.5 s (Chromium runs with --hide-crash-restore-bubble).
        compositor.kill_group(current_process, grace=0.5 if started_browser else 2.0)
        current_process = None

        # Browsers that escaped the group (NOT labwc: it is reused). "chromium-browse"
        # is "chromium-browser" cut to the 15-character process name limit.
        if started_browser:
            compositor.kill_own(["chromium", "chromium-browse", "cog"])
        perf.mark("stop: browser cleanup done")  # PERF

        # Brief settle for the client process to release Wayland resources
        time.sleep(0.1)


def stop_all():
    """Stop the mode, the preloaded audio process and labwc (service shutdown)."""
    stop_current_mode()
    stop_audio_preload()
    compositor.stop_labwc()


def _set_run_state(state):
    """Change the run state and publish it (call with mode_lock held)."""
    global run_state
    if state != run_state:
        logging.info(f"Run state: {run_state} -> {state}")
    run_state = state
    mqtt_link.publish(MQTT_RUN_STATE_TOPIC, state, retain=True)


def _go_off(new_run_state):
    """Screen off, mode "off" (call with mode_lock held). labwc is kept only for a
    normal "off" with keep_compositor_when_off: after a failure it restarts fresh."""
    global current_mode
    stop_current_mode()
    if new_run_state != RUN_OFF or not KEEP_COMPOSITOR_WHEN_OFF:
        compositor.stop_labwc()
    display.set_display_power(False)
    current_mode = "off"
    mqtt_link.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
    _set_run_state(new_run_state)


def _mode_failed(reason):
    """The mode process could not start or has exited (call with mode_lock held).

    Relaunch the same mode after a growing delay (RESTART_DELAYS), keeping the
    screen and labwc as they are; after the last try, give up and switch off.
    """
    global _restart_attempts, _pending_restart
    mono = time.monotonic()
    if _mode_started_at and mono - _mode_started_at > RESTART_STABLE_AFTER:
        _restart_attempts = 0  # It had been running fine: start counting again
    if _restart_attempts < len(RESTART_DELAYS):
        delay = RESTART_DELAYS[_restart_attempts]
        _restart_attempts += 1
        logging.error(
            f"FAIL-SAFE: Mode '{current_mode}' {reason}. Relaunching in {delay}s "
            f"(try {_restart_attempts}/{len(RESTART_DELAYS)})."
        )
        _pending_restart = (current_mode, mono + delay)
        _set_run_state(RUN_RETRYING)
    else:
        logging.error(
            f"FAIL-SAFE: Mode '{current_mode}' {reason} after {len(RESTART_DELAYS)} "
            "relaunches. Resetting to 'off'."
        )
        _restart_attempts = 0
        _pending_restart = None
        _go_off(RUN_FAILED)


def supervise_mode():
    """Watchdog tick, from the main loop (call with mode_lock held): notice a crashed
    mode process, mark a mode as running once it has lived a moment, fire retries."""
    global current_process, _pending_restart
    proc = current_process  # Snapshot: the global can change
    if current_mode != "off" and proc is not None:
        code = proc.poll()
        if code is not None:
            current_process = None
            _mode_failed(f"exited (code {code})")
        elif run_state == RUN_STARTING and time.monotonic() - _mode_started_at >= RUNNING_AFTER:
            _set_run_state(RUN_RUNNING)

    if _pending_restart and time.monotonic() >= _pending_restart[1]:
        retry_mode = _pending_restart[0]
        _pending_restart = None
        worker.command_queue.put(("restart", retry_mode))


def start_mode(mode, restart=False):
    """Switch to `mode` (call with mode_lock held). restart=True is the watchdog
    relaunching a crashed mode."""
    global current_process, current_mode
    global _restart_attempts, _pending_restart, _mode_started_at, _last_switch_at

    mode = str(mode).lower()
    # Same mode and still running: nothing to do. (Same mode with no process means
    # it crashed and waits for a retry: choosing it again relaunches it now.)
    if mode == current_mode and (mode == "off" or current_process is not None):
        mqtt_link.publish(MQTT_STATE_TOPIC, current_mode, retain=True)
        return
    _last_switch_at = time.monotonic()

    if not restart:
        # A choice made by the user (or at startup) cancels any pending crash retry.
        _restart_attempts = 0
        _pending_restart = None

    if mode != "off" and mode not in get_available_modes() and mode not in get_available_modes(refresh=True):
        logging.warning(f"Unknown mode: {mode}. Available modes: {get_available_modes()}")
        _go_off(RUN_FAILED)
        return

    previous_mode = current_mode
    logging.info(f"Transitioning mode: {previous_mode} -> {mode}")
    perf.begin_switch(mode)  # PERF

    # 1. Publish the choice IMMEDIATELY so the HA selector reacts at once. Whether the
    #    mode really runs is published separately (run state: starting -> running).
    current_mode = mode
    mqtt_link.publish(MQTT_STATE_TOPIC, current_mode, retain=True)

    if mode == "off":
        logging.info("Ensuring display power is OFF.")
        _go_off(RUN_OFF)
        perf.mark("old_mode_stopped")  # PERF
        return

    _set_run_state(RUN_STARTING)
    _mode_started_at = time.monotonic()

    # 2. Stop the current mode's client process (labwc compositor stays alive).
    stop_current_mode()
    perf.mark("old_mode_stopped")  # PERF

    try:
        current_process = _launch_mode(mode, previous_mode)
    except Exception as e:
        current_process = None
        _mode_failed(f"could not start ({e})")
        return
    _mode_started_at = time.monotonic()
    perf.mark("process_launched")  # PERF


def _launch_mode(mode, previous_mode):
    """Turn the screen on, make sure labwc runs, start the mode process. Returns it."""
    # 1. Start display power ON and labwc compositor IN PARALLEL.
    #    On Pi Zero 2 cold boot, labwc takes 6-8s to create its Wayland socket.
    #    Overlapping with display power-on (~3s) hides most of that latency.
    display_thread = None
    if previous_mode == "off":
        display_thread = threading.Thread(target=display.set_display_power, args=(True,), daemon=True)
        display_thread.start()

    compositor.setup_display_env()
    labwc_thread = None
    _labwc_start_error = [None]  # Mutable container for thread result
    if compositor.labwc_process is not None or (
        not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY")
    ):
        def _start_labwc_parallel():
            try:
                compositor.ensure_labwc()
            except Exception as e:
                _labwc_start_error[0] = e
        labwc_thread = threading.Thread(target=_start_labwc_parallel, daemon=True)
        labwc_thread.start()

    # 2. Resolve mode script (runs while display + labwc warm up).
    py_script = os.path.join(MODES_DIR, f"{mode}_mode.py")
    sh_script = os.path.join(MODES_DIR, f"{mode}_mode.sh")
    if os.path.exists(sh_script):
        base_cmd = ["bash", sh_script]
    elif os.path.exists(py_script):
        base_cmd = [sys.executable, py_script]
    else:
        raise RuntimeError(f"no script for mode '{mode}' in {MODES_DIR}")

    # 3. Wait for display power ON and labwc to be ready.
    if display_thread:
        display_thread.join(timeout=4.0)
    if labwc_thread:
        labwc_thread.join(timeout=12.0)
    perf.mark("display_and_labwc_ready")  # PERF

    # 4. Prepare mode-specific environment variables.
    env = os.environ.copy()
    audio_idx = audio_monitor.discover_audio_device()
    env["SMARTFRAME_AUDIO_DEVICE"] = str(audio_idx if audio_idx is not None else "")
    # Lets the audio mode check that the index still points to the same mic
    if AUDIO_CONFIG.get("device_index") is None and cache.working_methods.get("audio_device_name"):
        env["SMARTFRAME_AUDIO_DEVICE_NAME"] = cache.working_methods["audio_device_name"]
    else:
        env.pop("SMARTFRAME_AUDIO_DEVICE_NAME", None)
    env["SMARTFRAME_DEBUG"] = "1" if settings.DEBUG_MODE else "0"
    # Python buffers print() when not writing to a terminal: without this, the mode's
    # messages only reach journald when it stops.
    env["PYTHONUNBUFFERED"] = "1"
    env["SMARTFRAME_SCREEN_SIZE"] = SCREEN_SIZE
    if mode == "mirror":
        env["MIRROR_URL"] = (config.get("magic_mirror") or {}).get(
            "url", "http://localhost:8080"
        )

    # Apply labwc result (sets WAYLAND_DISPLAY for the mode process).
    if labwc_thread:
        if _labwc_start_error[0]:
            logging.warning(
                f"Persistent labwc session failed ({_labwc_start_error[0]}). Attempting direct launch."
            )
        elif compositor.labwc_wayland_display:
            env["WAYLAND_DISPLAY"] = compositor.labwc_wayland_display
            if compositor.labwc_config_dir:
                env.update(compositor.invisible_cursor_env())

    # 5. Audio with audio.preload: wake up the process that is already waiting.
    if mode == "audio":
        proc = _start_preloaded_audio(env)
        if proc is not None:
            logging.info(f"Launching 'audio' mode (preloaded process, PID {proc.pid}).")
            return proc

    # 6. Launch the mode process. The audio mode gets the write end of a private
    #    pipe for its sound levels (only that process: pass_fds, not inherited by
    #    anything else). We close our copy so EOF arrives when it exits.
    level_r = level_w = None
    if mode == "audio":
        level_r, level_w = os.pipe()
        env["SMARTFRAME_LEVEL_FD"] = str(level_w)
    logging.info(f"Launching '{mode}' mode (cmd: {' '.join(base_cmd)}).")
    try:
        proc = subprocess.Popen(
            base_cmd, env=env, start_new_session=True, stdout=None, stderr=None,
            pass_fds=(level_w,) if level_w is not None else (),
        )
    except Exception:
        if level_r is not None:
            os.close(level_r)
        raise
    finally:
        if level_w is not None:
            os.close(level_w)
    if level_r is not None:
        audio_monitor.start_level_reader(level_r)
    return proc


def _spawn_audio_preload():
    """Start audio_mode.py ahead of time (audio.preload). It does its slow imports,
    then waits on stdin for the start signal (_start_preloaded_audio), without
    touching the screen or the mic. Call with mode_lock held."""
    global _preloaded_audio
    script = os.path.join(MODES_DIR, "audio_mode.py")
    if not os.path.exists(script):
        return
    level_r, level_w = os.pipe()
    env = os.environ.copy()
    env["SMARTFRAME_PRELOAD"] = "1"
    env["SMARTFRAME_LEVEL_FD"] = str(level_w)
    env["PYTHONUNBUFFERED"] = "1"  # Read when Python starts: must be set now
    try:
        _preloaded_audio = subprocess.Popen(
            [sys.executable, script], env=env, start_new_session=True,
            stdin=subprocess.PIPE, stdout=None, stderr=None, pass_fds=(level_w,),
        )
    except Exception as e:
        os.close(level_r)
        _preload_failed(f"could not start it ({e})")
        return
    finally:
        os.close(level_w)
    audio_monitor.start_level_reader(level_r)
    logging.info(f"Audio mode preloaded in the background (PID {_preloaded_audio.pid}).")


def _start_preloaded_audio(env):
    """Hand the mode's environment to the waiting audio process and let it start.
    Returns the process, or None if there is none ready (normal launch then)."""
    global _preloaded_audio, _preload_failures
    proc, _preloaded_audio = _preloaded_audio, None
    if proc is None:
        return None
    if proc.poll() is None:
        start_env = {k: v for k, v in env.items() if k != "SMARTFRAME_LEVEL_FD"}
        try:
            proc.stdin.write((json.dumps(start_env) + "\n").encode("utf-8"))
            proc.stdin.close()
            _preload_failures = 0
            return proc
        except OSError as e:
            logging.warning(f"Preloaded audio process unusable ({e}): normal launch.")
    else:
        logging.warning(f"Preloaded audio process had exited (code {proc.returncode}): normal launch.")
    compositor.kill_group(proc, grace=0.5)
    return None


def stop_audio_preload():
    """Stop the waiting audio process, if any (call with mode_lock held)."""
    global _preloaded_audio
    proc, _preloaded_audio = _preloaded_audio, None
    if proc is not None and proc.poll() is None:
        compositor.kill_group(proc, grace=0.5)


def _preload_failed(reason):
    """A preload could not start or died while waiting (broken install, out of RAM...):
    wait longer before each new try (40 s, 80 s... up to PRELOAD_RETRY_MAX)."""
    global _preload_failures, _preload_next_try
    _preload_failures += 1
    delay = min(PRELOAD_RETRY_MAX, PRELOAD_AFTER * 2 ** _preload_failures)
    _preload_next_try = time.monotonic() + delay
    logging.warning(f"Audio preload failed: {reason}. Next try in {delay:.0f} s.")


def maybe_preload_audio():
    """Main-loop tick (call with mode_lock held): keep one audio process waiting
    while another mode (or "off") is shown, once that mode has settled."""
    global _preloaded_audio
    if not AUDIO_PRELOAD or current_mode == "audio":
        return
    if _preloaded_audio is not None:
        if _preloaded_audio.poll() is None:
            return
        _preload_failed(f"the waiting process exited (code {_preloaded_audio.returncode})")
        _preloaded_audio = None
        return
    now = time.monotonic()
    if (run_state in (RUN_OFF, RUN_RUNNING) and now - _last_switch_at >= PRELOAD_AFTER
            and now >= _preload_next_try):
        _spawn_audio_preload()
