import pytest

from smartframe.audio_input import device_key, find_input_device, open_input, resolve_device


class FakePyAudio:
    def __init__(self, devices):
        self.devices = devices

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, i):
        if not 0 <= i < len(self.devices):
            raise OSError("Invalid device index")
        return dict(self.devices[i], index=i)

    def get_default_input_device_info(self):
        raise OSError("no default")


MIC = "snd_rpi_googlevoicehat_soundcar: Google voiceHAT SoundCard HiFi voicehat-hifi-0"


def test_device_key_drops_alsa_card_number():
    assert device_key(f"{MIC} (hw:1,0)") == device_key(f"{MIC} (hw:2,0)") == MIC


def test_resolve_keeps_a_valid_index():
    p = FakePyAudio([{"name": "hdmi", "maxInputChannels": 0}, {"name": f"{MIC} (hw:1,0)", "maxInputChannels": 2}])
    index, info = resolve_device(p, 1, MIC)
    assert index == 1


def test_resolve_finds_the_mic_again_after_renumbering():
    # The cached index 1 now points to a USB mic; ours moved to index 2 (card 2)
    p = FakePyAudio([
        {"name": "hdmi", "maxInputChannels": 0},
        {"name": "USB Audio (hw:1,0)", "maxInputChannels": 1},
        {"name": f"{MIC} (hw:2,0)", "maxInputChannels": 2},
    ])
    index, info = resolve_device(p, 1, MIC)
    assert index == 2


def test_resolve_gives_up_when_the_mic_is_gone():
    p = FakePyAudio([{"name": "USB Audio (hw:1,0)", "maxInputChannels": 1}])
    assert resolve_device(p, 5, MIC) == (None, None)


def test_resolve_without_name_only_checks_the_index():
    p = FakePyAudio([{"name": "USB Audio", "maxInputChannels": 1}])
    assert resolve_device(p, 0)[0] == 0
    assert resolve_device(p, 3) == (None, None)


def test_find_input_device_prefers_the_i2s_mic():
    p = FakePyAudio([
        {"name": "USB Audio", "maxInputChannels": 1},
        {"name": f"{MIC} (hw:1,0)", "maxInputChannels": 2},
    ])
    assert find_input_device(p)[0] == 1


class FakeStreamOpener:
    """p.open() that refuses some formats, like a driver without 32-bit support."""

    def __init__(self, refused_formats, busy=False):
        self.refused = set(refused_formats)
        self.busy = busy
        self.formats = []

    def open(self, format, **kwargs):
        import pyaudio

        self.formats.append(format)
        if self.busy:  # PyAudio raises OSError(text, PortAudio code)
            raise OSError("Device unavailable", pyaudio.paDeviceUnavailable)
        if format in self.refused:
            raise OSError("Invalid sample format", pyaudio.paSampleFormatNotSupported)
        return ("stream", format)


def test_open_input_falls_back_to_16_bits_when_32_is_refused():
    pyaudio = pytest.importorskip("pyaudio")
    p = FakeStreamOpener({pyaudio.paInt32})
    warnings = []
    stream, bits = open_input(p, 32, warn=warnings.append, rate=48000)
    assert bits == 16
    assert stream == ("stream", pyaudio.paInt16)
    assert p.formats == [pyaudio.paInt32, pyaudio.paInt16]
    assert len(warnings) == 1


def test_open_input_keeps_32_bits_when_accepted():
    pyaudio = pytest.importorskip("pyaudio")
    p = FakeStreamOpener(set())
    stream, bits = open_input(p, 32, warn=lambda m: pytest.fail(m), rate=48000)
    assert bits == 32
    assert p.formats == [pyaudio.paInt32]


def test_open_input_does_not_retry_a_16_bit_failure():
    pyaudio = pytest.importorskip("pyaudio")
    p = FakeStreamOpener({pyaudio.paInt16})
    with pytest.raises(OSError):
        open_input(p, 16, rate=48000)
    assert p.formats == [pyaudio.paInt16]


def test_open_input_does_not_fall_back_when_the_mic_is_busy():
    # Busy is not a refused format: the caller retries later, still in 32 bits
    pyaudio = pytest.importorskip("pyaudio")
    p = FakeStreamOpener(set(), busy=True)
    with pytest.raises(OSError):
        open_input(p, 32, warn=lambda m: pytest.fail(m), rate=48000)
    assert p.formats == [pyaudio.paInt32]
