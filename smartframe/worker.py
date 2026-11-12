"""The command worker: runs slow hardware and process commands one at a time, in
order, so the MQTT network thread is never blocked. Also the HA "Restart" button."""

import logging
import os

from smartframe import audio_monitor, compositor, display, mqtt_link, supervisor
from smartframe.commands import CommandQueue
from smartframe.settings import KEEP_COMPOSITOR_WHEN_OFF, RESTART_EXIT_CODE

# Display settings (brightness...) keep only their latest value while they wait.
command_queue = CommandQueue()


def run_command(cmd, payload):
    """Run one command from the queue."""
    if cmd == "mode":
        with supervisor.mode_lock:
            supervisor.start_mode(payload)
    elif cmd == "restart":
        # Watchdog retry: only if nothing else happened in the meantime.
        with supervisor.mode_lock:
            if supervisor.current_mode == payload and supervisor.current_process is None:
                logging.info(f"Relaunching crashed mode '{payload}'.")
                supervisor.start_mode(payload, restart=True)
    elif cmd == "display_off":
        # Startup in "off". With keep_compositor_when_off, labwc is started now
        # (screen still off), as if a mode had run: the first switch then skips
        # its 5-6 s cold start, like every later switch from "off".
        with supervisor.mode_lock:
            if KEEP_COMPOSITOR_WHEN_OFF and supervisor.current_mode == "off":
                compositor.setup_display_env()
            # Same rule as _launch_mode: no labwc inside an existing desktop session
            if (KEEP_COMPOSITOR_WHEN_OFF and supervisor.current_mode == "off"
                    and not os.environ.get("WAYLAND_DISPLAY")
                    and not os.environ.get("DISPLAY")):
                try:
                    compositor.ensure_labwc()
                except Exception as e:
                    logging.warning(f"Could not pre-start labwc ({e}). "
                                    "It will start with the first mode.")
            display.set_display_power(False)
    elif cmd == "brightness":
        display.set_display_brightness(payload)
    elif cmd == "contrast":
        display.set_display_contrast(payload)
    elif cmd == "color_preset":
        display.set_display_color_preset(payload)
    elif cmd == "input_source":
        display.set_display_input_source(payload)
    elif cmd == "service_restart":
        restart_service()


def command_worker():
    """Background worker that executes slow hardware or process commands sequentially
    to avoid blocking the main MQTT loop thread."""
    while True:
        try:
            run_command(*command_queue.get())
        except Exception:
            logging.exception("Command worker error")


def restart_service():
    """HA "Restart" button: stop everything, then exit with an error code so that
    systemd (Restart=on-failure) starts the service again ~10 s later. The service
    has no sudo, so it cannot ask systemd to restart it in any other way."""
    if not os.environ.get("INVOCATION_ID"):
        logging.warning("Restart requested, but this process does not run under systemd: "
                        "it will exit and nothing will start it again.")
    logging.warning(f"Restart requested from Home Assistant: exiting (code {RESTART_EXIT_CODE}), "
                    "systemd starts the service again.")
    with supervisor.mode_lock:
        audio_monitor.stop()
        supervisor.stop_all()
    mqtt_link.say_goodbye()  # Our own "offline" below must not be corrected
    logging.shutdown()
    os._exit(RESTART_EXIT_CODE)
