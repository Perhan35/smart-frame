import queue

import pytest

from smartframe.commands import CommandQueue, parse_command

MODES = ["audio", "mirror", "off"]


def test_parse_mode():
    assert parse_command("mode", b" Audio ", MODES) == ("mode", "audio")
    assert parse_command("mode", b"disco", MODES) is None


def test_parse_service_restart():
    assert parse_command("service_restart", b"PRESS", MODES) == ("service_restart", None)
    assert parse_command("service_restart", b"press ", MODES) == ("service_restart", None)
    assert parse_command("service_restart", b"", MODES) is None
    assert parse_command("service_restart", b"\xff", MODES) is None


def test_parse_invalid_utf8_never_raises():
    assert parse_command("mode", b"\xff\xfe", MODES) is None
    assert parse_command("color_preset", b"\xff", MODES) == ("color_preset", "\ufffd")


@pytest.mark.parametrize("payload, expected", [
    (b"50", 50), (b"50.0", 50), (b"150", 100), (b"-3", 0),
])
def test_parse_numbers(payload, expected):
    assert parse_command("brightness", payload, MODES) == ("brightness", expected)


@pytest.mark.parametrize("payload", [b"", b"abc", b"nan", b"inf"])
def test_parse_bad_numbers(payload):
    assert parse_command("contrast", payload, MODES) is None


def test_parse_keeps_case_of_names():
    assert parse_command("color_preset", b"Warm (5000 K)", MODES) == ("color_preset", "Warm (5000 K)")
    assert parse_command("input_source", b"  ", MODES) is None


def test_queue_coalesces_display_settings():
    q = CommandQueue()
    for value in range(20):
        q.put(("brightness", value))
    q.put(("mode", "audio"))
    q.put(("brightness", 99))  # Updates the waiting one, keeps its place
    assert q.qsize() == 2
    assert q.get() == ("brightness", 99)
    assert q.get() == ("mode", "audio")


def test_queue_never_merges_modes():
    q = CommandQueue()
    q.put(("mode", "audio"))
    q.put(("mode", "off"))
    assert [q.get(), q.get()] == [("mode", "audio"), ("mode", "off")]


def test_queue_after_get_a_new_value_is_queued_again():
    q = CommandQueue()
    q.put(("contrast", 10))
    assert q.get() == ("contrast", 10)
    q.put(("contrast", 20))
    assert q.get() == ("contrast", 20)


def test_queue_timeout():
    with pytest.raises(queue.Empty):
        CommandQueue().get(timeout=0.01)
