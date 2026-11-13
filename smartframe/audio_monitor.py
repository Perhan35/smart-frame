"""Ambient sound level for Home Assistant, in every mode: the microphone choice, the
private pipe the audio mode sends its levels through, and the AudioMonitor thread."""

import logging
import math
import os
import queue
import threading
import time

import pyaudio

from smartframe import cache, dsp, mqtt_link, supervisor
from smartframe.audio_input import device_key, find_input_device, open_input, resolve_device
from smartframe.settings import AUDIO_CONFIG, MQTT_DBA_STATE_TOPIC, MQTT_LAFMAX_STATE_TOPIC

# Levels sent by the audio mode through its private pipe: (LAeq, LAFmax) once per
# second. The pipe only exists between main.py and that one process: no file, so
# nothing else can write fake values, and EOF tells us the process is gone.
_external_levels = queue.Queue(maxsize=5)

monitor = None  # The running AudioMonitor (start() / stop())


def discover_audio_device():
    """Index of the microphone to use: audio.device_index from config.yaml if set,
    otherwise auto-detected once and cached (saves 1-2s of hardware probing)."""
    configured = AUDIO_CONFIG.get("device_index")
    if configured is not None:
        try:
            return int(configured)  # audio_mode / AudioMonitor report it if unusable
        except (TypeError, ValueError):
            logging.warning(f"Ignoring invalid audio.device_index: {configured!r}")

    if cache.working_methods.get("audio_device") is not None:
        return cache.working_methods["audio_device"]

    try:
        p = pyaudio.PyAudio()
        try:
            found_index, info = find_input_device(p)
        finally:
            p.terminate()
        if found_index is not None:
            _remember_audio_device(found_index, info)
            return found_index
    except Exception as e:
        logging.debug(f"Audio discovery failed: {e}")
    return None


def _remember_audio_device(index, info):
    """Cache the auto-detected mic: its index, and its name to check that index later."""
    working_methods = cache.working_methods
    name = device_key(info.get("name")) if info else None
    if (working_methods.get("audio_device"), working_methods.get("audio_device_name")) != (index, name):
        working_methods["audio_device"] = index
        working_methods["audio_device_name"] = name
        cache.save()


def _resolve_audio_device(p):
    """(index, info) of the mic to open with PyAudio instance `p`, or (None, None).

    A cached index is checked against the cached device name: if ALSA renumbered the
    cards (e.g. a USB device was plugged in), the mic is found again by name, or
    detected again. A busy mic is not "wrong": the caller just retries later.
    """
    index = discover_audio_device()
    if index is None:
        return None, None
    configured = AUDIO_CONFIG.get("device_index") is not None
    name = None if configured else cache.working_methods.get("audio_device_name")
    found, info = resolve_device(p, index, name)
    if found is None:
        if not configured:
            logging.warning(
                f"Cached microphone (index {index}, {name!r}) not found any more: detecting it again."
            )
        found, info = find_input_device(p, index if configured else None)
    if found is not None and not configured:
        _remember_audio_device(found, info)
    return found, info


def read_level_pipe(fd):
    """Read '<LAeq> <LAFmax>' lines sent by the audio mode until it exits (EOF)."""
    try:
        with os.fdopen(fd, "r", encoding="ascii", errors="replace") as pipe:
            for line in pipe:
                try:
                    levels = tuple(float(v) for v in line.split()[:2])
                except ValueError:
                    continue
                if len(levels) != 2 or not all(math.isfinite(v) for v in levels):
                    continue
                try:
                    _external_levels.put_nowait(levels)
                except queue.Full:
                    pass  # Nobody is reading (e.g. MQTT offline): drop it
    except Exception as e:
        logging.debug(f"Audio level pipe closed with an error: {e}")


def start_level_reader(fd):
    """Read the audio mode's pipe in a background thread."""
    threading.Thread(target=read_level_pipe, args=(fd,), daemon=True).start()


class AudioMonitor(threading.Thread):
    """Publishes the ambient sound level to MQTT once per second, in every mode.

    Outside audio mode it reads the microphone itself. In audio mode it releases the
    mic and forwards the values the audio mode sends through its pipe. Both sides use
    smartframe.dsp.LevelMeter, so the sensor does not jump when the mode changes.
    """

    CHUNK = 4096  # Frames per read (~85 ms at 48 kHz): few wake-ups, cheap

    def __init__(self, mq_client):
        super().__init__(daemon=True)
        self.mqtt_client = mq_client
        self.running = True
        self.meter_settings = dsp.meter_settings(AUDIO_CONFIG)
        self.bits = dsp.capture_bits(AUDIO_CONFIG)
        self.channel = dsp.channel_index(AUDIO_CONFIG.get("mic_channel", "left"))
        self._warned_silent = False

    def _publish(self, levels):
        laeq, lafmax = levels
        silent_db = self.meter_settings["calibration_offset_db"] - 100
        if lafmax < silent_db and not self._warned_silent:
            # A whole second of digital zeros: no real room is that quiet.
            logging.warning(
                "Microphone sends only zeros. If its L/R pin goes to 3.3V, set "
                "audio.mic_channel: \"right\" in config.yaml (or check the wiring)."
            )
            self._warned_silent = True
        if self.mqtt_client and self.mqtt_client.is_connected():
            try:
                self.mqtt_client.publish(MQTT_DBA_STATE_TOPIC, f"{laeq:.1f}")
                self.mqtt_client.publish(MQTT_LAFMAX_STATE_TOPIC, f"{lafmax:.1f}")
            except Exception as e:
                logging.debug(f"Monitor: publish failed: {e}")

    def _open(self, p):
        """Open the mic with its own channel count and rate. Returns (stream, channels, meter)."""
        idx, info = _resolve_audio_device(p)
        if idx is None:
            return None, 0, None
        channels, rate = dsp.input_format(info)
        stream, self.bits = open_input(
            p,
            self.bits,
            channels=channels,
            rate=rate,
            input_device_index=idx,
            frames_per_buffer=self.CHUNK,
        )
        meter = dsp.LevelMeter(rate, **self.meter_settings)
        logging.info(
            f"Monitor: microphone open (index {idx}, {channels} ch, {rate} Hz, {self.bits}-bit)."
        )
        return stream, channels, meter

    @staticmethod
    def _close(stream):
        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except Exception as e:
                logging.debug(f"Monitor: closing the stream failed: {e}")

    def run(self):
        logging.info("Audio Monitor background thread started.")
        p = pyaudio.PyAudio()
        stream, channels, meter = None, 0, None
        last_publish = time.monotonic()

        while self.running:
            # 1. Audio mode owns the mic (also while it starts or waits for a retry):
            #    release it and forward the values it sends through its pipe.
            if supervisor.current_mode == "audio":
                if stream:
                    self._close(stream)
                    stream, meter = None, None
                try:
                    self._publish(_external_levels.get(timeout=0.5))
                except queue.Empty:
                    pass
                continue

            # Values left over from an audio session are stale now.
            while not _external_levels.empty():
                try:
                    _external_levels.get_nowait()
                except queue.Empty:
                    break

            # 2. Read the microphone directly.
            if not stream:
                try:
                    stream, channels, meter = self._open(p)
                except Exception as e:
                    logging.debug(f"Monitor: failed to open the microphone (busy?): {e}")
                    stream = None
                if not stream:
                    time.sleep(5)
                    continue
                last_publish = time.monotonic()

            try:
                raw = stream.read(self.CHUNK, exception_on_overflow=False)
                meter.process(dsp.pick_channel(raw, channels, self.channel, self.bits))
            except Exception as e:
                logging.debug(f"Monitor: microphone read error: {e}")
                self._close(stream)
                stream, meter = None, None
                time.sleep(2)
                continue

            # 3. Publish LAeq / LAFmax of the last second.
            now = time.monotonic()
            if now - last_publish >= 1.0:
                levels = meter.pop_interval()
                last_publish = now
                if levels:
                    self._publish(levels)

        self._close(stream)
        p.terminate()


def start():
    """Start the background dBA monitor thread."""
    global monitor
    monitor = AudioMonitor(mqtt_link.client)
    monitor.start()


def stop():
    """Ask the monitor thread to stop (it releases the mic at its next read)."""
    if monitor:
        monitor.running = False
