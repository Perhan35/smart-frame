"""Microphone selection shared by the orchestrator and the setup scripts."""

import logging
import re

# Name fragments of the I2S microphone drivers this project is used with.
I2S_KEYWORDS = ("i2s", "googlevoicehat", "mono", "inmp")
_ALSA_CARD_SUFFIX = re.compile(r"\s*\(hw:\d+,\s*\d+\)\s*$")


def pa_format(bits):
    """PyAudio sample format for audio.capture_bits (16 or 32)."""
    import pyaudio  # Here so the pure helpers below can be tested without PortAudio

    return pyaudio.paInt32 if bits == 32 else pyaudio.paInt16


def device_key(name):
    """Stable name of a device: ALSA adds "(hw:1,0)", whose card number can change
    between boots (e.g. when a USB device is plugged in)."""
    return _ALSA_CARD_SUFFIX.sub("", str(name or "")).strip()


def resolve_device(p, index, name=None):
    """Return (index, info) of the input device `index` if it is still the device
    called `name`; otherwise the input device called `name`; otherwise (None, None).

    Indexes follow the ALSA card order, which can change between boots: the name
    tells us whether a cached index still points to the same microphone.
    """
    key = device_key(name) if name else None
    if index is not None:
        try:
            info = p.get_device_info_by_index(int(index))
            if info.get("maxInputChannels", 0) > 0 and (
                key is None or device_key(info.get("name")) == key
            ):
                return int(index), info
        except Exception as e:
            logging.debug(f"Audio device {index} is not usable: {e}")
    if key:
        for i in range(p.get_device_count()):
            try:
                info = p.get_device_info_by_index(i)
            except Exception as e:
                logging.debug(f"  - Error probing device {i}: {e}")
                continue
            if info.get("maxInputChannels", 0) > 0 and device_key(info.get("name")) == key:
                return i, info
    return None, None


def find_input_device(p, configured_index=None):
    """Return (index, device_info) of the capture device to use, or (None, None).

    Order: audio.device_index from config.yaml (if it has inputs), then a device
    whose name looks like the I2S mic, then the system default input.
    `p` is a pyaudio.PyAudio instance.
    """
    if configured_index is not None:
        try:
            info = p.get_device_info_by_index(int(configured_index))
            if info.get("maxInputChannels", 0) > 0:
                return int(configured_index), info
            logging.warning(
                f"audio.device_index {configured_index} has no input channels. "
                "Falling back to auto-detection."
            )
        except Exception as e:
            logging.warning(
                f"audio.device_index {configured_index} is not usable ({e}). "
                "Falling back to auto-detection."
            )

    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
        except Exception as e:
            logging.debug(f"  - Error probing device {i}: {e}")
            continue
        name = info.get("name", "").lower()
        if info.get("maxInputChannels", 0) > 0 and any(k in name for k in I2S_KEYWORDS):
            logging.info(f"Matched I2S microphone: {info.get('name')} (index {i}).")
            return i, info

    try:
        info = p.get_default_input_device_info()
        logging.info(
            f"Using the system default audio input: {info.get('name')} "
            f"(index {info.get('index')})."
        )
        return int(info.get("index")), info
    except Exception as e:
        logging.debug(f"No default audio input: {e}")
    return None, None
