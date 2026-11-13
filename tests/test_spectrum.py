import numpy as np
import pytest

from smartframe import dsp, spectrum

RATE = 48000


def pink_levels(analyzer, draws=60, seed=0):
    """Mean band levels (dB) of pink noise as the INMP441 hears it."""
    rng = np.random.default_rng(seed)
    n = spectrum.LONG_FFT
    freqs = np.fft.rfftfreq(n, 1.0 / RATE)
    mic = 10 ** (-dsp.mic_boost_db(freqs) / 20)  # The mic's own response
    power = np.zeros(spectrum.NUM_BANDS)
    for _ in range(draws):
        x = np.fft.rfft(rng.standard_normal(n))
        x[1:] /= np.sqrt(freqs[1:])  # Pink: power per Hz falls as 1/f
        x[0] = 0
        buffer = np.fft.irfft(x * mic, n)
        analyzer.update_long(buffer)
        analyzer.update_short(buffer)
        power += 10 ** (analyzer.levels / 10)
    return 10 * np.log10(power / draws)


def test_band_edges_span_the_range_and_respect_the_min_width():
    edges = spectrum.band_edges()
    assert len(edges) == spectrum.NUM_BANDS + 1
    assert edges[0] == spectrum.MIN_FREQ and edges[-1] == spectrum.MAX_FREQ
    assert np.all(np.diff(edges) >= spectrum.MIN_BAND_HZ - 1e-9)
    # Above the bass, the spacing is logarithmic (constant ratio)
    ratios = edges[1:] / edges[:-1]
    assert ratios[-1] == pytest.approx(ratios[-30], rel=1e-6)


def test_every_bar_reads_its_own_bins():
    analyzer = spectrum.Spectrum(RATE)
    for table in (analyzer._long, analyzer._short):
        assert np.all(table.counts >= 1)
        assert np.all(table.starts[1:] >= table.stops[:-1])  # No bin shared by two bars


def test_pink_noise_draws_flat_bars():
    db = pink_levels(spectrum.Spectrum(RATE))
    assert db.max() - db.min() < 3.0
    assert np.abs(np.diff(db)).max() < 1.5  # No jump between neighbours (e.g. at 10 kHz)


def test_long_and_short_fft_meet_without_a_jump():
    analyzer = spectrum.Spectrum(RATE)
    db = pink_levels(analyzer)
    k = analyzer.n_long
    assert 0 < k < spectrum.NUM_BANDS
    assert db[k] - db[k - 1] == pytest.approx(0.0, abs=1.0)


def test_close_bass_tones_stay_apart():
    analyzer = spectrum.Spectrum(RATE)
    t = np.arange(spectrum.LONG_FFT) / RATE
    analyzer.update_long(sum(1000 * np.sin(2 * np.pi * f * t) for f in (40, 60, 80)))
    raw = analyzer.raw_levels()
    peaks = [int(np.argmin(np.abs(analyzer.centers - f))) for f in (40, 60, 80)]
    for a, b in zip(peaks, peaks[1:]):
        assert raw[a + 1:b].min() < min(raw[a], raw[b]) - 3.0  # A dip between tones


def test_silence_draws_no_bar():
    analyzer = spectrum.Spectrum(RATE)
    analyzer.update_long(np.zeros(spectrum.LONG_FFT))
    analyzer.update_short(np.zeros(spectrum.LONG_FFT))
    out = analyzer.bar_targets(0.05, np.zeros(spectrum.NUM_BANDS))
    assert not out.any()


def test_loud_tone_fills_its_bar():
    analyzer = spectrum.Spectrum(RATE)
    t = np.arange(spectrum.LONG_FFT) / RATE
    out = np.zeros(spectrum.NUM_BANDS)
    for _ in range(40):  # Let the auto gain settle
        analyzer.update_long(3000 * np.sin(2 * np.pi * 440 * t))
        analyzer.update_short(np.zeros(spectrum.LONG_FFT))
        analyzer.bar_targets(1 / 30, out)
    assert out[int(np.argmin(np.abs(analyzer.centers - 440)))] > 0.9


def test_band_position_follows_the_real_edges():
    analyzer = spectrum.Spectrum(RATE)
    for i in (0, 5, 60, spectrum.NUM_BANDS):
        assert analyzer.band_position(analyzer.edges[i]) == pytest.approx(i)


def test_mic_boost_is_0_db_at_1khz_and_capped():
    assert dsp.mic_boost_db([1000.0])[0] == pytest.approx(0.0, abs=0.01)
    assert dsp.mic_boost_db([5.0])[0] <= dsp.MAX_MIC_BOOST_DB
