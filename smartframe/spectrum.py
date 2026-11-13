"""Spectrum bars of the audio mode: band levels and their auto-gain scale.

Only depends on numpy (no pygame), so the band math can be tested on any machine
and the noise floor measured on the frame itself.

Two FFTs share the work. A long one (LONG_FFT) gives the bass and mids their
frequency detail. A short one (SHORT_FFT) gives the treble its speed: cymbals and
hi-hats last a few tens of ms, and a 170 ms window would blur them. Both are scaled
to the same units, so a bar does not jump where they meet.
"""

import numpy as np

from smartframe import dsp

NUM_BANDS = 120
MIN_FREQ = 25.0
# The mic's digital filter stops near 20 kHz (at 48 kHz): above, bars only show noise.
MAX_FREQ = 20000.0

LONG_FFT = 8192  # Bass and mids: 5.9 Hz bins at 48 kHz, 170 ms window
SHORT_FFT = 2048  # Treble: 43 ms window; its 23 Hz bins are still narrow up there
CROSSOVER_HZ = 2500.0  # Bands centred above this use the short FFT
# No band is narrower than one bin of the long FFT at 48 kHz. With a pure log
# spacing, several bass bars would read the same bin and move together.
MIN_BAND_HZ = 48000.0 / LONG_FFT

# Display balance. A band's level is the mean power of its bins, and for pink noise
# (same energy in every octave) that falls 3 dB per octave: +3 dB/octave draws it
# flat. The mic's own roll-off (weaker bass and top treble) is undone as well, so
# real pink noise in the room also draws flat bars.
SLOPE_DB_PER_OCTAVE = 3.0

# Auto gain. The top follows the loudest band, so the bars show the shape of the
# sound at any volume. A bar starts at its band's noise gate (the mic's own noise +
# a margin), so silence draws nothing, even when the top is low.
DISPLAY_RANGE_DB = 45.0  # dB from the top of the scale to its bottom
TOP_HEADROOM_DB = 3.0  # The loudest band stays just under the top
TOP_ATTACK_TAU_S = 0.2  # Time constant of the top going up to a louder peak
TOP_RELEASE_DB_PER_S = 3.0  # How fast the top comes down after a loud part
MIN_SPAN_DB = 20.0  # A bar needs at least this many dB above its gate to fill up

# Background noise of an INMP441 on a Raspberry Pi in a quiet room, as (Hz, dB): mean
# band level (mean of the dB values) before the display balance, in LONG_FFT units at
# 48 kHz (int16 LSB scale). Measured from a 20 s silent capture with this band layout:
# below 200 Hz there is one point per bar, because each bar is one FFT bin there and
# mains hum (50 Hz and its 100 Hz harmonic) sits in single bars. The bump near
# 18.6 kHz is a steady electrical tone from the board (18 750 Hz = 48 kHz x 25/64).
NOISE_FLOOR = (
    (27.8, 74.9), (33.7, 73.5), (39.5, 72.3), (45.4, 73.5), (51.3, 72.7), (57.2, 70.0),
    (63, 68.3), (68.9, 67.3), (74.7, 66.6), (80.6, 66.3), (86.5, 65.4), (92.3, 66.2),
    (98.2, 69.9), (104.1, 66.8), (109.9, 63.1), (115.8, 62.5), (121.7, 62.1),
    (127.9, 61.1), (134.3, 60.6), (141.1, 60.8), (148.2, 61.1), (163.6, 59.5),
    (180.6, 58.5), (199.3, 57.9), (250, 56.2), (315, 54.8), (400, 53.3), (500, 51.6),
    (630, 50.4), (800, 48.9), (1000, 47.3), (1250, 46.3), (1600, 44.9), (2000, 43.9),
    (2500, 42.8), (3150, 41.3), (4000, 40.4), (5000, 40.6), (6300, 39.5), (8000, 38.9),
    (10000, 38.7), (12500, 38.5), (16000, 38.5), (17700, 38.6),
    (18600, 42.4), (19500, 39.6),
)
# Gate margin above the noise floor: 10 dB for a 1-bin band, less for wide bands
# (their mean over many bins moves less). Fitted on the same capture: silence then
# crosses a gate about 0.5 % of the time.
GATE_MARGIN_DB = 10.0
GATE_MARGIN_EXPONENT = 0.33


def band_edges(num=NUM_BANDS, lo=MIN_FREQ, hi=MAX_FREQ, min_width=MIN_BAND_HZ):
    """num + 1 band edges from lo to hi: log spaced, but never narrower than min_width.

    The bass bands, where a log step would be narrower than min_width, get exactly
    min_width; the rest share the remaining range on a log scale.
    """
    def edges_for(ratio):
        edges = [lo]
        for _ in range(num):
            edges.append(edges[-1] + max(min_width, edges[-1] * (ratio - 1.0)))
        return edges

    low, high = 1.0, hi / lo
    for _ in range(60):  # Bisection on the log step: the last edge lands on hi
        mid = 0.5 * (low + high)
        if edges_for(mid)[-1] < hi:
            low = mid
        else:
            high = mid
    edges = np.array(edges_for(high))
    edges[-1] = hi
    return edges


class _BandTable:
    """Mean power of a run of bands, read from one FFT size."""

    def __init__(self, rate, n, edges, ref_window_power):
        self.n = n
        self.window = np.blackman(n)
        # Same units for every FFT size: noise power per bin grows with the window's
        # energy, so dividing by it makes both FFTs read the same level.
        self.scale = ref_window_power / float(np.sum(self.window**2))
        freqs = np.fft.rfftfreq(n, 1.0 / rate)
        starts = np.searchsorted(freqs, edges[:-1], side="left")
        stops = np.searchsorted(freqs, edges[1:], side="left")
        centers = np.sqrt(edges[:-1] * edges[1:])
        for i in np.where(stops <= starts)[0]:
            # Band narrower than a bin (only at another sample rate): nearest bin
            k = int(np.argmin(np.abs(freqs - centers[i])))
            starts[i], stops[i] = k, k + 1
        self.starts, self.stops = starts, stops
        self.counts = (stops - starts).astype(np.float64)
        self._top = int(stops.max())

    def levels_db(self, buffer):
        """Band levels in dB from the last n samples of buffer."""
        spectrum = np.fft.rfft(buffer[-self.n:] * self.window)[: self._top]
        power = spectrum.real**2 + spectrum.imag**2
        # Cumulative sum: neighbouring bands may share a bin at other sample rates
        cumulative = np.concatenate(([0.0], np.cumsum(power)))
        band_power = (cumulative[self.stops] - cumulative[self.starts]) / self.counts
        return 10 * np.log10(np.maximum(band_power * self.scale, 1e-18))


class Spectrum:
    """Band levels (dB) and bar heights for the audio mode, at one sample rate.

    Feed it the analysis buffer (the last LONG_FFT samples): update_long() for the
    bass and mids, update_short() for the treble (cheap, can run on every frame),
    then bar_targets() turns the levels into bar heights.
    """

    def __init__(self, rate, noise_floor=NOISE_FLOOR):
        self.rate = int(rate)
        self.edges = band_edges()
        self.centers = np.sqrt(self.edges[:-1] * self.edges[1:])
        self.n_long = int(np.sum(self.centers <= CROSSOVER_HZ))
        ref = float(np.sum(np.blackman(LONG_FFT) ** 2))
        self._long = _BandTable(self.rate, LONG_FFT, self.edges[: self.n_long + 1], ref)
        self._short = _BandTable(self.rate, SHORT_FFT, self.edges[self.n_long:], ref)
        self.counts = np.concatenate((self._long.counts, self._short.counts))

        self.shaping_db = (SLOPE_DB_PER_OCTAVE * np.log2(self.centers / 1000.0)
                           + dsp.mic_boost_db(self.centers))
        floor = np.interp(np.log10(self.centers), np.log10([p[0] for p in noise_floor]),
                          [p[1] for p in noise_floor])
        # Noise gate of each band, in the same units as the levels (balance included).
        # The noise power in one bin grows with the bin width (rate / FFT size).
        self.gate_db = (floor + 10 * np.log10(self.rate / 48000.0) + self.shaping_db
                        + GATE_MARGIN_DB * self.counts ** -GATE_MARGIN_EXPONENT)
        self.levels = np.full(NUM_BANDS, -200.0)  # Nothing heard yet: empty bars
        self.top_db = float(self.gate_db.max())  # Top of the auto-gain scale

    def update_long(self, buffer):
        """Bass and mids from the last LONG_FFT samples."""
        self.levels[: self.n_long] = (self._long.levels_db(buffer)
                                      + self.shaping_db[: self.n_long])

    def update_short(self, buffer):
        """Treble from the last SHORT_FFT samples."""
        self.levels[self.n_long:] = (self._short.levels_db(buffer)
                                     + self.shaping_db[self.n_long:])

    def raw_levels(self):
        """Levels without the display balance: the units of NOISE_FLOOR."""
        return self.levels - self.shaping_db

    def bar_targets(self, dt, out):
        """Bar heights 0..1 in `out`, with the auto-gain scale.

        The top follows the loudest band (fast up, TOP_RELEASE_DB_PER_S down). Each
        bar starts at max(top - DISPLAY_RANGE_DB, its noise gate), so the mic's own
        noise never draws a bar, and spans at least MIN_SPAN_DB.
        """
        peak = float(self.levels.max()) + TOP_HEADROOM_DB
        if peak > self.top_db:
            self.top_db += (1.0 - np.exp(-dt / TOP_ATTACK_TAU_S)) * (peak - self.top_db)
        else:
            self.top_db -= TOP_RELEASE_DB_PER_S * dt
        self.top_db = max(self.top_db, float(self.gate_db.max()))
        bottom = np.maximum(self.top_db - DISPLAY_RANGE_DB, self.gate_db)
        span = np.maximum(self.top_db - bottom, MIN_SPAN_DB)
        np.clip((self.levels - bottom) / span, 0, 1, out=out)
        return out

    def band_position(self, freq):
        """Position of a frequency on the bar axis, in bars from the left edge
        (for the legend: the bars are not evenly spaced in log frequency)."""
        return float(np.interp(np.log10(freq), np.log10(self.edges), np.arange(NUM_BANDS + 1)))
