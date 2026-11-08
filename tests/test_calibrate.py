import pytest

from calibrate import parse_db, solve_calibration
from smartframe import dsp


def calibrated(raw, offset, slope):
    """Same model as dsp.LevelMeter._calibrate."""
    ref = dsp.CALIBRATION_REF_DBA
    return ref + slope * (raw + offset - ref)


def test_one_point_moves_the_offset_only():
    offset, slope = solve_calibration([(60.0, 64.0)], old_offset=33.0)
    assert offset == pytest.approx(37.0)
    assert slope == 1.0


def test_two_points_match_both_references():
    old = 38.0
    true_offset, true_slope = 31.0, 1.15
    raws = (27.0, 52.0)  # Raw levels of the 2 sounds (no offset)
    points = [(raw + old, calibrated(raw, true_offset, true_slope)) for raw in raws]
    offset, slope = solve_calibration(points, old)
    assert slope == pytest.approx(true_slope)
    for raw, (_, phone) in zip(raws, points):
        assert calibrated(raw, offset, slope) == pytest.approx(phone)


def test_two_points_away_from_the_pivot():
    # Neither point at 65 dBA: both still match exactly.
    points = [(45.0, 40.0), (80.0, 85.0)]
    offset, slope = solve_calibration(points, old_offset=30.0)
    for shown, phone in points:
        assert calibrated(shown - 30.0, offset, slope) == pytest.approx(phone)


def test_bad_points_are_rejected():
    with pytest.raises(ValueError):
        solve_calibration([(60.0, 60.0), (60.0, 80.0)], old_offset=30.0)
    with pytest.raises(ValueError):
        solve_calibration([(40.0, 80.0), (80.0, 40.0)], old_offset=30.0)


@pytest.mark.parametrize("text, value", [
    ("64.5", 64.5), ("64,5", 64.5), (" 70 dBA", 70.0), ("abc", None), ("0", None),
])
def test_parse_db(text, value):
    assert parse_db(text) == value
