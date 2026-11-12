"""The graphical session the modes draw in: Wayland/X11 detection and the persistent
labwc compositor (kept alive across mode switches), plus process cleanup helpers.

This module owns labwc_process, labwc_wayland_display, labwc_config_dir and
display_env_detected: other modules only read them.
"""

import logging
import os
import shutil
import signal
import socket
import struct
import subprocess
import time

from smartframe import cache, perf
from smartframe.settings import CHROMIUM_PROFILE_DIR

labwc_config_dir = None
labwc_process = None  # Persistent labwc compositor (reused across mode switches)
labwc_wayland_display = None  # WAYLAND_DISPLAY name for the persistent labwc session
display_env_detected = False


def _is_wayland_reachable(display_name):
    """Check if a Wayland socket is actually listening."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    socket_path = os.path.join(runtime_dir, display_name)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            s.connect(socket_path)
        return True
    except Exception:
        return False


def setup_display_env(force=False):
    """Detect and validate the current display environment (Wayland/X11)."""
    global display_env_detected
    if display_env_detected and not force:
        return

    cache.load()
    working_methods = cache.working_methods

    # Ensure a persistent chromium profile exists for speed
    if not os.path.exists(CHROMIUM_PROFILE_DIR):
        os.makedirs(CHROMIUM_PROFILE_DIR, exist_ok=True)
        logging.info(f"Created persistent Chromium profile at: {CHROMIUM_PROFILE_DIR}")

    uid = os.getuid()
    needs_save = False

    def is_x11_active():
        """Check if X server is running by looking for the process."""
        for pattern in ["Xorg", "X"]:
            if (
                subprocess.run(
                    ["pgrep", "-u", str(uid), "-x", pattern], capture_output=True
                ).returncode
                == 0
            ):
                return True
        return False

    # 1. Validate environment
    if "WAYLAND_DISPLAY" in os.environ:
        if not _is_wayland_reachable(os.environ["WAYLAND_DISPLAY"]):
            del os.environ["WAYLAND_DISPLAY"]
            working_methods["session_type"] = None
            needs_save = True

    if "DISPLAY" in os.environ:
        if not is_x11_active():
            del os.environ["DISPLAY"]
            working_methods["session_type"] = None
            needs_save = True

    # 2. Force default XDG_RUNTIME_DIR if missing
    if "XDG_RUNTIME_DIR" not in os.environ:
        os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    # 3. Auto-detection using cached session_type hint
    if "WAYLAND_DISPLAY" not in os.environ and "DISPLAY" not in os.environ:
        if working_methods["session_type"] != "X11":
            for i in range(2):
                name = f"wayland-{i}"
                if _is_wayland_reachable(name):
                    os.environ["WAYLAND_DISPLAY"] = name
                    if working_methods["session_type"] != "Wayland":
                        working_methods["session_type"] = "Wayland"
                        needs_save = True
                    display_env_detected = True
                    if needs_save:
                        cache.save()
                    return

        if is_x11_active() and os.path.exists("/tmp/.X11-unix/X0"):
            os.environ["DISPLAY"] = ":0"
            if working_methods["session_type"] != "X11":
                working_methods["session_type"] = "X11"
                needs_save = True

    if needs_save:
        cache.save()
    display_env_detected = True


def kill_own(names):
    """Fallback cleanup: kill this user's processes whose name is exactly one of
    `names`. (pkill -f would also hit any command line that merely contains the
    word, e.g. "cog" in "recognizer.py".) Normal cleanup kills the process group.
    One pkill for all names: with -x, "a|b" must match a whole name."""
    try:
        subprocess.run(
            ["pkill", "-x", "-u", str(os.getuid()), "|".join(names)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except Exception as e:
        logging.debug(f"pkill {names} failed: {e}")


def kill_group(proc, grace=2.0):
    """SIGTERM the process group started for `proc`, then SIGKILL if it lingers
    more than `grace` seconds."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=grace)
        perf.mark("stop: exited after SIGTERM")  # PERF
    except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
        perf.mark("stop: not gone after SIGTERM, sending SIGKILL")  # PERF
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass  # Already gone
        except Exception as e:
            logging.debug(f"SIGKILL of process group {proc.pid} failed: {e}")


def _get_labwc_config():
    """Create a temporary labwc config to hide the cursor and optimize for kiosk mode."""
    config_dir = (
        subprocess.check_output(["mktemp", "-d", "/tmp/labwc-orchestrator-XXXXXX"])
        .decode()
        .strip()
    )
    # Using XDG_CONFIG_HOME expects a /labwc subfolder
    os.makedirs(os.path.join(config_dir, "labwc"), exist_ok=True)
    rc_xml = os.path.join(config_dir, "labwc", "rc.xml")
    with open(rc_xml, "w") as f:
        f.write(
            "<labwc_config>\n"
            "  <windowRules>\n"
            '    <windowRule identifier="*">\n'
            '      <action name="Maximize" />\n'
            "    </windowRule>\n"
            "  </windowRules>\n"
            "</labwc_config>"
        )

    _write_invisible_cursor_theme(os.path.join(config_dir, "icons"))
    return config_dir


# Cursor theme with a fully transparent image, given to labwc through XCURSOR_PATH.
# labwc draws its cursor even with no mouse plugged in (top-left of the screen at
# startup), and it has no command to hide it from outside. Named "default": Cog asks
# libwayland-cursor for the default theme (it ignores XCURSOR_THEME), and gets a
# built-in arrow when XCURSOR_PATH has no theme by that name.
INVISIBLE_CURSOR_THEME = "default"
_CURSOR_NAMES = (
    "default", "left_ptr", "pointer", "hand1", "hand2", "text", "xterm",
    "wait", "watch", "progress", "crosshair", "grab", "grabbing", "move",
)


def _write_invisible_cursor_theme(icons_dir):
    """Write an Xcursor theme whose cursors are a single transparent pixel."""
    image_type, nominal_size = 0xFFFD0002, 24
    # File header (magic, header size, version, 1 table entry) + table entry
    # (type, size, offset of the image) + image header + one ARGB pixel (0 = transparent)
    data = (
        b"Xcur" + struct.pack("<III", 16, 0x10000, 1)
        + struct.pack("<III", image_type, nominal_size, 28)
        + struct.pack("<IIIIIIIII", 36, image_type, nominal_size, 1, 1, 1, 0, 0, 0)
        + struct.pack("<I", 0)
    )
    cursors_dir = os.path.join(icons_dir, INVISIBLE_CURSOR_THEME, "cursors")
    os.makedirs(cursors_dir, exist_ok=True)
    for name in _CURSOR_NAMES:
        with open(os.path.join(cursors_dir, name), "wb") as f:
            f.write(data)


def invisible_cursor_env():
    """Environment that selects the invisible cursor theme. Needed by labwc AND by its
    clients: Cog loads its own cursor theme (libwayland-cursor) and falls back to a
    built-in arrow when it finds none."""
    return {
        "XCURSOR_PATH": os.path.join(labwc_config_dir, "icons"),
        "XCURSOR_THEME": INVISIBLE_CURSOR_THEME,
        "XCURSOR_SIZE": "24",
    }


def ensure_labwc():
    """Start a persistent labwc compositor if not already running. Returns env dict for clients."""
    global labwc_process, labwc_wayland_display, labwc_config_dir, display_env_detected

    # Check if existing labwc is still alive and its socket reachable
    if labwc_process and labwc_process.poll() is None:
        if labwc_wayland_display and _is_wayland_reachable(labwc_wayland_display):
            logging.debug("Reusing persistent labwc compositor.")
            return labwc_wayland_display
        # Process alive but socket dead — tear down
        logging.warning("labwc process alive but socket unreachable, restarting.")
        stop_labwc()

    # Looked up on PATH each time (cheap, no subprocess): a path read back from the
    # cache file would be run as-is, whatever that file says.
    labwc_bin = shutil.which("labwc")
    if not labwc_bin:
        raise RuntimeError("labwc not found on PATH (sudo apt install labwc)")

    # Create config directory (reuse if still exists)
    if not labwc_config_dir or not os.path.exists(labwc_config_dir):
        labwc_config_dir = _get_labwc_config()

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = labwc_config_dir
    env.update(invisible_cursor_env())
    env["COG_PLATFORM_FDO_SHOW_CURSOR"] = "0"

    # Clean up stale wayland sockets so labwc can claim wayland-0 cleanly.
    # On Pi Zero 2, stale socket files from previous crashed sessions block labwc from
    # creating its own socket (name collision), causing the socket detection to time out.
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        for f in os.listdir(runtime_dir):
            if f.startswith("wayland-") and not f.endswith(".lock"):
                if not _is_wayland_reachable(f):
                    stale = os.path.join(runtime_dir, f)
                    try:
                        os.remove(stale)
                        logging.debug(f"Removed stale Wayland socket: {stale}")
                    except OSError:
                        pass
                    # Also remove the companion .lock file
                    lock = stale + ".lock"
                    if os.path.exists(lock):
                        try:
                            os.remove(lock)
                        except OSError:
                            pass
    except OSError:
        pass

    # Launch labwc with a long-lived no-op startup command.
    # labwc requires -s to fully initialize its Wayland socket on DRM/KMS backends (Pi Zero 2).
    # "sleep infinity" keeps labwc alive as a bare compositor ready to accept client connections.
    labwc_process = subprocess.Popen(
        [labwc_bin, "-s", "sleep infinity"], env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    logging.info(f"Started persistent labwc compositor (PID {labwc_process.pid}).")

    # Wait for labwc's Wayland socket to become reachable (max ~10s).
    # First cold-start on Pi Zero 2 can take 6-8s (shared libs, DRM init, shader compile).
    # Subsequent starts are ~2s thanks to warm page cache.
    for attempt in range(100):
        if labwc_process.poll() is not None:
            rc = labwc_process.returncode
            labwc_process = None
            raise RuntimeError(f"labwc exited during startup with code {rc}")

        for name in ["wayland-0", "wayland-1", "wayland-2"]:
            if _is_wayland_reachable(name):
                labwc_wayland_display = name
                os.environ["WAYLAND_DISPLAY"] = name
                display_env_detected = True
                logging.info(f"labwc Wayland socket ready: {name} (after {(attempt+1)*0.1:.1f}s)")
                return name

        if attempt % 10 == 9:
            logging.debug(f"labwc socket poll #{attempt+1}: still waiting...")
        time.sleep(0.1)

    raise RuntimeError("labwc started but no Wayland socket detected within 10s")


def stop_labwc():
    """Tear down the persistent labwc compositor (only called for 'off' mode or error recovery)."""
    global labwc_process, labwc_wayland_display, labwc_config_dir, display_env_detected

    if labwc_process:
        logging.info("Stopping persistent labwc compositor.")
        kill_group(labwc_process)
        labwc_process = None

    labwc_wayland_display = None

    if labwc_config_dir and os.path.exists(labwc_config_dir):
        try:
            shutil.rmtree(labwc_config_dir)
        except Exception as e:
            logging.debug(f"Could not remove {labwc_config_dir}: {e}")
        labwc_config_dir = None

    # Orphaned labwc processes of this user (exact name only)
    kill_own(["labwc"])

    if "WAYLAND_DISPLAY" in os.environ:
        del os.environ["WAYLAND_DISPLAY"]
    if "DISPLAY" in os.environ:
        del os.environ["DISPLAY"]
    display_env_detected = False

    time.sleep(0.3)  # Brief settle for DRM/kernel resource release
