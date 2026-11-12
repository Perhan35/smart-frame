"""SmartFrame orchestrator: entry point of the service (systemd runs `python3 main.py`).

The work is done in the smartframe/ package:
  settings       config.yaml, logging, MQTT topics, constants
  cache          what was learned about the hardware (.smartframe_cache)
  compositor     Wayland/X11 session, persistent labwc compositor
  display        screen power, brightness, contrast, color, input (DDC/CI...)
  audio_monitor  microphone choice and the dBA sensor in every mode
  supervisor     mode processes, watchdog, run state, audio preload
  mqtt_link      MQTT client, Home Assistant discovery, incoming commands
  worker         the command queue, run one command at a time
This file starts them and runs the main loop (watchdog + diagnostics).
"""

import ctypes
import logging
import os
import signal
import sys
import threading
import time

from smartframe import perf  # PERF
from smartframe import settings  # First: loads config.yaml and sets up logging
from smartframe import audio_monitor, display, mqtt_link, supervisor, worker

perf.mark("imports_done")  # PERF

# Suppress noisy ALSA errors from PyAudio hardware probing (they spam stderr with
# "Expression 'ret' failed in pa_linux_alsa.c" during device enumeration)
try:
    _alsa_lib = ctypes.cdll.LoadLibrary('libasound.so.2')
    _alsa_err_handler = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                          ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)

    @_alsa_err_handler
    def _alsa_noop_handler(filename, line, function, err, fmt):
        pass
    _alsa_lib.snd_lib_error_set_handler(_alsa_noop_handler)
except (OSError, AttributeError):
    pass  # Not on Linux or libasound not available: nothing to silence


def signal_handler(sig, frame):
    logging.info(f"Signal {sig} received. Shutting down orchestrator...")
    mqtt_link.mark_going_offline()
    audio_monitor.stop()
    supervisor.stop_all()
    sys.exit(0)


def main_loop():
    """Watchdog and diagnostics, once a second, until the service stops."""
    last_cpu_read = 0.0
    while True:
        now = time.time()
        # One bad iteration must never stop the whole orchestrator.
        try:
            # 1. Watchdog: crashed mode -> retry / off, starting -> running.
            # Skip this tick if a mode switch is in progress: the worker is
            # stopping/starting processes and the state is in between.
            if supervisor.mode_lock.acquire(blocking=False):
                try:
                    supervisor.supervise_mode()
                    supervisor.maybe_preload_audio()
                finally:
                    supervisor.mode_lock.release()

            # 2. CPU temperature (diagnostic sensor), sent only when it changes.
            # States are retained and published when they change, and discovery is
            # sent again when HA restarts (homeassistant/status): no periodic resend.
            if now - last_cpu_read >= settings.CPU_TEMP_EVERY:
                last_cpu_read = now
                mqtt_link.publish_cpu_temp()
                mqtt_link.publish_started(mqtt_link.client)  # Only if the clock jumped (NTP)
        except Exception:
            logging.exception("Orchestrator loop iteration failed (continuing).")

        time.sleep(1)


def main():
    # Parse command line arguments
    import argparse

    parser = argparse.ArgumentParser(description="SmartFrame Orchestrator")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    if args.debug:
        settings.DEBUG_MODE = True
        os.environ["SMARTFRAME_DEBUG"] = "1"
        logging.getLogger().setLevel(logging.DEBUG)
        logging.debug("CLI DEBUG FLAG DETECTED: Full process logs enabled.")

    # Register handlers for clean exit
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    broker = mqtt_link.create_client()

    # Note: the i2c-dev kernel module (needed by ddcutil) is loaded at boot through
    # /etc/modules-load.d/smartframe.conf, installed by scripts/setup_pi.sh.
    if sys.platform.startswith("linux") and not any(
        f.startswith("i2c-") for f in os.listdir("/dev")
    ):
        logging.warning(
            "No /dev/i2c-* device found: DDC/CI display control will not work. "
            "Re-run scripts/setup_pi.sh (or: sudo modprobe i2c-dev)."
        )

    logging.info("--- SmartFrame Orchestrator starting ---")

    # Pre-discover audio device in background (avoids 1-2s probe on first mode switch)
    threading.Thread(target=audio_monitor.discover_audio_device, daemon=True).start()

    # 2. Start command worker thread (Handle all transitions sequentially)
    threading.Thread(target=worker.command_worker, daemon=True).start()
    logging.debug("Command worker thread started.")

    # 3. Connect to MQTT with automatic retry
    try:
        if broker:
            # We use connect_async + loop_start to ensure startup isn't blocked by the broker
            mqtt_link.client.connect_async(broker, settings.MQTT_PORT, 60)
            mqtt_link.client.loop_start()
            logging.info(f"MQTT network loop started (Broker: {broker}).")
        else:
            logging.warning("MQTT broker IP not configured. Starting in OFFLINE mode.")
    except Exception as e:
        logging.error(f"Error initiating MQTT connection: {e}")

    # 4. Request initial state from configuration via the worker queue
    # This prevents the initial mode from racing with MQTT retained 'set_mode' messages.
    # The screen is only switched off when the first mode is "off": done through the
    # same queue, so it can never land after (and undo) a mode that turns it on.
    default_mode = settings.config.get("default_mode", "off")
    if str(default_mode).lower() == "off":
        worker.command_queue.put(("display_off", None))
    else:
        worker.command_queue.put(("mode", default_mode))
    logging.info(f"Queued initial mode request: {default_mode}")

    # 5. Start background dBA monitor thread
    audio_monitor.start()

    try:
        main_loop()
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    except Exception as e:
        logging.critical(f"FATAL: Orchestrator loop crashed: {e}")
    finally:
        supervisor.stop_all()
        display.set_display_power(False)
        if mqtt_link.client:
            mqtt_link.client.loop_stop()
        logging.info("--- SmartFrame Orchestrator terminated ---")


if __name__ == "__main__":
    main()
