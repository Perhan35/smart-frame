import math

import numpy as np
import pytest

from smartframe import dsp

RATE = 48000


def sine(freq, rms, seconds=2.0, rate=RATE):
    t = np.arange(int(rate * seconds)) / rate
    return rms * math.sqrt(2) * np.sin(2 * np.pi * freq * t)


def laeq(samples, **kwargs):
    meter = dsp.LevelMeter(RATE, **kwargs)
    meter.process(samples)
    return meter.pop_interval()[0]


def test_a_weighting_is_0_db_at_1khz():
    n = 48000  # 1 Hz bins: bin 1000 is exactly 1 kHz
    gains = dsp.a_weighting_gains(RATE, n)
    assert 20 * math.log10(gains[1000]) == pytest.approx(0.0, abs=0.01)


@pytest.mark.parametrize("freq, expected_db", [(100, -19.1), (10000, -2.5)])
def test_a_weighting_known_values(freq, expected_db):
    gains = dsp.a_weighting_gains(RATE, 48000)
    assert 20 * math.log10(gains[freq]) == pytest.approx(expected_db, abs=0.1)


def test_1khz_sine_reads_its_rms_in_db():
    # RMS 1000 LSB = 60 dB on this scale; A-weighting is 0 dB at 1 kHz
    assert laeq(sine(1000, 1000), calibration_offset_db=0) == pytest.approx(60.0, abs=0.2)


def test_offset_is_added():
    assert laeq(sine(1000, 1000), calibration_offset_db=33) == pytest.approx(93.0, abs=0.2)


def test_slope_turns_around_the_reference_level():
    s = sine(1000, 1000)  # 60 dB raw
    # offset 5 -> 65 dB = the reference: the slope does not change it
    assert laeq(s, calibration_offset_db=5, calibration_slope=1.2) == pytest.approx(65.0, abs=0.2)
    # offset 25 -> 85 dB, 20 dB above the reference: slope 1.1 gives 65 + 22
    assert laeq(s, calibration_offset_db=25, calibration_slope=1.1) == pytest.approx(87.0, abs=0.2)


def test_noise_floor_is_removed_in_energy():
    s = sine(1000, 1000)  # 60 dBA
    # Removing a floor 10 dB below takes 10*log10(1 - 0.1) = -0.46 dB
    assert laeq(s, calibration_offset_db=0, noise_floor_dba=50) == pytest.approx(59.54, abs=0.2)


def test_noise_floor_with_slope_round_trips():
    meter = dsp.LevelMeter(RATE, calibration_offset_db=20, calibration_slope=1.3)
    assert meter._calibrate(meter._uncalibrate(42.0)) == pytest.approx(42.0)


def test_laeq_is_an_energy_mean():
    loud = sine(1000, 1000, seconds=1.0)  # 60 dB
    quiet = sine(1000, 10, seconds=1.0)  # 20 dB
    # Half the time at 60 dB, half at 20 dB: about 60 - 3 = 57 dB, not the 40 dB average
    assert laeq(np.concatenate((loud, quiet)), calibration_offset_db=0) == pytest.approx(57.0, abs=0.3)


def test_pick_channel_keeps_one_channel():
    stereo = np.array([1, 100, 2, 200, 3, 300], dtype=np.int16).tobytes()
    assert dsp.pick_channel(stereo, 2, 0).tolist() == [1, 2, 3]
    assert dsp.pick_channel(stereo, 2, 1).tolist() == [100, 200, 300]


def test_pick_channel_32_bit_matches_16_bit_scale():
    raw32 = (np.array([1000, -2000], dtype=np.int32) * 65536).tobytes()
    assert dsp.pick_channel(raw32, 1, 0, bits=32).tolist() == [1000.0, -2000.0]


def test_meter_settings_defaults():
    settings = dsp.meter_settings({})
    assert settings["calibration_offset_db"] == dsp.DEFAULT_CALIBRATION_OFFSET_DB
    assert settings["calibration_slope"] == 1.0
    assert settings["noise_floor_dba"] is None
    # An explicit 0 stays 0
    assert dsp.meter_settings({"calibration_offset_db": 0})["calibration_offset_db"] == 0.0


def test_capture_bits():
    assert dsp.capture_bits({}) == 16
    assert dsp.capture_bits({"capture_bits": 32}) == 32
    assert dsp.capture_bits({"capture_bits": 24}) == 16
