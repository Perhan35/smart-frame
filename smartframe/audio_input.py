"""Microphone selection shared by the orchestrator and the setup scripts."""

import logging

# Name fragments of the I2S microphone drivers this project is used with.
I2S_KEYWORDS = ("i2s", "googlevoicehat", "mono", "inmp")


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
