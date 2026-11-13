"""Sound level math shared by the orchestrator (AudioMonitor) and the audio mode.

Both paths feed the same Home Assistant sensor, so every number that leaves the
frame is computed here, the same way. Only depends on numpy.

Scale: levels are in dB relative to 1 LSB RMS of int16 samples (32-bit captures are
scaled down to the same scale), plus `calibration_offset_db`. With the optional
`calibration_slope` (2-point calibration) the scale also turns around
CALIBRATION_REF_DBA. It is a relative dBA, accurate near the calibration points,
not a certified SPL meter: the mic's own response, the ALSA chain and a single
phone-app reference all limit the accuracy (a few dB).
"""

import math

import numpy as np

FAST_TAU = 0.125  # IEC 61672 "FAST" time weighting, in seconds (same for rise and fall)
REACTIVE_RELEASE_TAU = 0.08  # Screen only: instant attack, 80 ms release
FRAME_SIZE = 2048  # Samples per analysis frame (43 ms at 48 kHz); hop is half of it
# Z-weighting ("flat") still needs a band: IEC 61672 uses 10 Hz - 20 kHz. Without it,
# DC drift and ultrasonic noise up to Nyquist (24 kHz at 48 kHz) would be counted.
Z_BAND_HZ = (10.0, 20000.0)
# Noise floor subtraction never lowers a reading by more than this (avoids log(0)).
# Readings far below the configured floor are indicative only.
MAX_FLOOR_REDUCTION_DB = 10.0
_MIN_POWER = 1e-12

# INMP441 datasheet: -26 dBFS for 94 dB SPL (1 kHz). A -26 dBFS sine in int16 has an
# RMS of ~1160 LSB = 61.3 dB on this scale, so the theoretical offset is ~94 - 61 = 33.
# Fine-tune it with a sound level meter or a phone app (see README).
DEFAULT_CALIBRATION_OFFSET_DB = 33.0
# The 2-point slope turns the scale around this level: calibrate the offset near it.
CALIBRATION_REF_DBA = 65.0

# INMP441 typical frequency response (datasheet DS-INMP441-00 rev 1.1, Figure 5
# "Typical Frequency Response (Measured)"), as (Hz, dB). Read from the figure's vector
# data. It is a typical unit, not this one: individual mics differ by a dB or two.
INMP441_RESPONSE = (
    (20, -11.0), (25, -8.7), (31.5, -6.6), (40, -4.9), (50, -3.6), (63, -2.5),
    (80, -1.7), (100, -1.15), (125, -0.7), (160, -0.4), (200, -0.25), (250, -0.15),
    (315, -0.05), (500, 0.0), (1000, -0.1), (2700, 0.05), (6000, -0.2), (7700, -0.65),
    (9700, -1.15), (13600, -2.45), (15600, -2.9), (17700, -3.65),
)
# The compensation never boosts more than this (the 20 Hz end needs +11 dB, where
# the A-weighting already removes 50 dB, so the cap only matters for dBZ).
MAX_MIC_BOOST_DB = 12.0


def meter_settings(audio_config):
    """LevelMeter keyword arguments from the `audio:` section of config.yaml."""
    audio_config = audio_config or {}
    offset = audio_config.get("calibration_offset_db")
    slope = audio_config.get("calibration_slope")
    return {
        "calibration_offset_db": DEFAULT_CALIBRATION_OFFSET_DB if offset is None else float(offset),
        "noise_floor_dba": audio_config.get("noise_floor_dba"),
        "calibration_slope": 1.0 if slope is None else float(slope),
        "mic_compensation": audio_config.get("mic_compensation") is True,
    }


def mic_boost_db(freqs, response=INMP441_RESPONSE):
    """dB to add at each frequency to undo the mic's frequency response (0 dB at
    1 kHz, so the calibration offset stays valid), capped at MAX_MIC_BOOST_DB.

    The response is interpolated in log frequency and held flat beyond its first
    and last points (no extrapolation of the roll-offs).
    """
    log_hz = np.log10([p[0] for p in response])
    resp_db = np.array([p[1] for p in response], dtype=np.float64)
    resp_db -= np.interp(3.0, log_hz, resp_db)  # 1 kHz -> 0 dB
    at = np.interp(np.log10(np.maximum(np.asarray(freqs, dtype=np.float64), 1e-10)),
                   log_hz, resp_db)
    return np.minimum(-at, MAX_MIC_BOOST_DB)


def mic_compensation_gains(rate, n, response=INMP441_RESPONSE):
    """Linear gain for each rfft bin of an n-point frame that undoes the mic's
    frequency response (see mic_boost_db)."""
    return 10 ** (mic_boost_db(np.fft.rfftfreq(n, 1.0 / rate), response) / 20.0)


def capture_bits(audio_config):
    """audio.capture_bits: 32 (default) keeps the mic's full 24-bit resolution; 16 only
    when asked. A driver that refuses 32 falls back to 16 (audio_input.open_input)."""
    return 16 if str((audio_config or {}).get("capture_bits", 32)).strip() == "16" else 32


def a_weighting_gains(rate, n):
    """Linear A-weighting gain for each rfft bin of an n-point frame (0 dB at 1 kHz)."""
    f = np.fft.rfftfreq(n, 1.0 / rate)
    f_sq = np.maximum(f, 1e-10) ** 2
    num = (12194.0**2) * f_sq**2
    den = (
        (f_sq + 20.6**2)
        * np.sqrt((f_sq + 107.7**2) * (f_sq + 737.9**2))
        * (f_sq + 12194.0**2)
    )
    gains = num / den * 10 ** (2.0 / 20)  # +2.00 dB: IEC normalisation to 0 dB at 1 kHz
    gains[0] = 0.0
    return gains


def power_to_db(power, offset_db=0.0):
    return 10.0 * math.log10(max(float(power), _MIN_POWER)) + offset_db


def input_format(device_info):
    """(channels, rate) to open a capture stream with, from PyAudio device info.

    Some I2S drivers only accept 2 channels even for a mono mic, so a device that
    reports 2+ inputs is opened in stereo and one channel is picked (pick_channel).
    """
    channels = 1 if int(device_info.get("maxInputChannels", 1)) <= 1 else 2
    rate = int(device_info.get("defaultSampleRate", 48000))
    return channels, rate


def channel_index(name):
    """Config value audio.mic_channel ("left" / "right") -> interleaved channel index."""
    return 1 if str(name).strip().lower() in ("right", "r", "1") else 0


def pick_channel(raw, channels, channel=0, bits=16):
    """Interleaved int16 (or int32) bytes -> float64 mono samples of one channel.

    The INMP441 only sends audio in one I2S slot (left when L/R is tied to GND).
    Averaging both slots would mix in a silent channel and lose 6 dB.
    32-bit samples are divided by 65536: same scale as int16 (so the same
    calibration), with the extra low bits kept as decimals.
    """
    data = np.frombuffer(raw, dtype=np.int32 if bits == 32 else np.int16)
    if channels > 1:
        data = data[min(channel, channels - 1) :: channels]
    data = data.astype(np.float64)
    if bits == 32:
        data /= 65536.0
    return data


class LevelMeter:
    """A- and Z-weighted sound level meter.

    Feed mono samples with process(). The signal is cut into frames of FRAME_SIZE
    samples with 50 % overlap and a sine window: with that pair the squared windows
    add up to exactly 1, so every sample counts once in the energy (no blind spot at
    frame edges). Each frame's mean square is read from the weighted spectrum
    (Parseval), so no inverse FFT is needed.

    Outputs:
      - pop_interval(): LAeq (energy mean) and LAFmax since the previous call.
        The LAeq is what Home Assistant receives.
      - laf(): current FAST level (symmetric 125 ms exponential, on power).
      - reactive(): instant-attack levels for the on-screen number (never published).
    """

    def __init__(self, rate, calibration_offset_db=0.0, noise_floor_dba=None,
                 frame_size=FRAME_SIZE, calibration_slope=1.0, mic_compensation=False):
        self.rate = int(rate)
        self.n = int(frame_size)
        self.hop = self.n // 2
        self.offset = float(calibration_offset_db or 0.0)
        self.slope = float(calibration_slope or 1.0)
        self.window = np.sin(np.pi * (np.arange(self.n) + 0.5) / self.n)

        # Parseval: sum(x^2) = (1/N) * sum over all bins |X|^2. rfft keeps half the
        # bins, so every bin except DC and Nyquist counts twice. Dividing by
        # (N * hop) turns a frame's windowed energy into the mean square of its hop.
        bin_weights = np.full(self.n // 2 + 1, 2.0)
        bin_weights[0] = 1.0
        bin_weights[-1] = 1.0
        norm = 1.0 / (self.n * self.hop)
        if mic_compensation:  # audio.mic_compensation: undo the mic's own response
            bin_weights = bin_weights * mic_compensation_gains(self.rate, self.n) ** 2
        self._w_a = bin_weights * a_weighting_gains(self.rate, self.n) ** 2 * norm
        z = bin_weights.copy()
        freqs = np.fft.rfftfreq(self.n, 1.0 / self.rate)
        z[(freqs < Z_BAND_HZ[0]) | (freqs > Z_BAND_HZ[1])] = 0.0  # Also drops DC (mic offset)
        self._w_z = z * norm

        frame_dt = self.hop / self.rate
        self._fast_alpha = 1.0 - math.exp(-frame_dt / FAST_TAU)
        self._release_alpha = 1.0 - math.exp(-frame_dt / REACTIVE_RELEASE_TAU)

        self._floor_power = None
        if noise_floor_dba is not None:
            self._floor_power = 10 ** (self._uncalibrate(float(noise_floor_dba)) / 10.0)

        self._pending = np.zeros(0)
        self._fast_a = None
        self._react_a = 0.0
        self._react_z = 0.0
        self._sum_a = 0.0
        self._count = 0
        self._max_fast_a = 0.0

    def process(self, samples):
        """Add new mono samples (any length)."""
        buf = np.concatenate((self._pending, np.asarray(samples, dtype=np.float64)))
        if len(buf) < self.n:
            self._pending = buf
            return
        count = (len(buf) - self.n) // self.hop + 1
        frames = np.lib.stride_tricks.sliding_window_view(buf, self.n)[:: self.hop][:count]
        # Remove each frame's mean first: a DC offset would otherwise leak through the
        # window into the lowest bins (inside the Z band).
        frames = frames - frames.mean(axis=1, keepdims=True)
        spectrum = np.fft.rfft(frames * self.window, axis=1)
        power = spectrum.real**2 + spectrum.imag**2
        for ms_a, ms_z in zip(power @ self._w_a, power @ self._w_z):
            self._update(float(ms_a), float(ms_z))
        self._pending = buf[count * self.hop :].copy()

    def _update(self, ms_a, ms_z):
        if self._fast_a is None:
            self._fast_a = ms_a
        else:
            self._fast_a += self._fast_alpha * (ms_a - self._fast_a)
        self._react_a = ms_a if ms_a > self._react_a else (
            self._react_a + self._release_alpha * (ms_a - self._react_a))
        self._react_z = ms_z if ms_z > self._react_z else (
            self._react_z + self._release_alpha * (ms_z - self._react_z))
        self._sum_a += ms_a
        self._count += 1
        self._max_fast_a = max(self._max_fast_a, self._fast_a)

    def _calibrate(self, db):
        """Raw dB -> calibrated dB: offset, then the optional 2-point slope."""
        db += self.offset
        if self.slope != 1.0:
            db = CALIBRATION_REF_DBA + self.slope * (db - CALIBRATION_REF_DBA)
        return db

    def _uncalibrate(self, db):
        """Calibrated dB -> raw dB (inverse of _calibrate)."""
        if self.slope != 1.0:
            db = CALIBRATION_REF_DBA + (db - CALIBRATION_REF_DBA) / self.slope
        return db - self.offset

    def _db_a(self, power):
        if self._floor_power is not None:
            power = max(power - self._floor_power,
                        power * 10 ** (-MAX_FLOOR_REDUCTION_DB / 10.0))
        return self._calibrate(power_to_db(power))

    def laf(self):
        """Current FAST A-weighted level in dBA, or None before the first frame."""
        return None if self._fast_a is None else self._db_a(self._fast_a)

    def reactive(self):
        """(dBA, dBZ) with instant attack and quick release, for the screen only."""
        return self._db_a(self._react_a), self._calibrate(power_to_db(self._react_z))

    def pop_interval(self):
        """(LAeq, LAFmax) in dBA since the previous call, or None if no frame was seen."""
        if self._count == 0:
            return None
        laeq = self._db_a(self._sum_a / self._count)
        lafmax = self._db_a(self._max_fast_a)
        self._sum_a = 0.0
        self._count = 0
        self._max_fast_a = 0.0
        return laeq, lafmax
