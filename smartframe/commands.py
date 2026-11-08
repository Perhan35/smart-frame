"""MQTT commands: parsing and the queue the command worker reads from."""

import collections
import queue
import threading

# Commands whose older values are useless once a newer one arrives (HA sliders send
# a burst of values). Mode switches, retries and screen-off are never merged.
COALESCED = frozenset({"brightness", "contrast", "color_preset", "input_source"})


def parse_command(cmd, payload, modes):
    """Payload of a command topic -> (cmd, value), or None if the value is invalid.

    `cmd` is the command bound to the topic: "mode", "brightness", "contrast",
    "color_preset" or "input_source". `payload` is the raw MQTT payload (bytes);
    invalid UTF-8 never raises.
    """
    if isinstance(payload, (bytes, bytearray)):
        text = bytes(payload).decode("utf-8", errors="replace").strip()
    else:
        text = str(payload).strip()

    if cmd == "mode":
        mode = text.lower()
        return ("mode", mode) if mode in modes else None
    if cmd in ("brightness", "contrast"):
        try:
            value = int(round(float(text)))  # HA may send "50" or "50.0"
        except (ValueError, OverflowError):  # Not a number, nan, inf
            return None
        return cmd, max(0, min(100, value))
    if cmd in ("color_preset", "input_source"):
        return (cmd, text) if text else None  # Names are matched exactly (case kept)
    return None


class CommandQueue:
    """FIFO of (cmd, payload) for the command worker.

    A command in `coalesce` that is still waiting is updated in place instead of
    queued again: dragging the brightness slider queues one ddcutil call (up to
    3.5 s each), not twenty, and a mode switch never waits behind a backlog.
    """

    def __init__(self, coalesce=COALESCED):
        self._cond = threading.Condition()
        self._items = collections.deque()  # [cmd, payload] lists, oldest first
        self._waiting = {}  # cmd -> its queued entry, for coalesced commands
        self._coalesce = frozenset(coalesce)

    def put(self, item):
        cmd, payload = item
        with self._cond:
            entry = self._waiting.get(cmd)
            if entry is not None:
                entry[1] = payload  # Keeps its place in the queue, newest value
                return
            entry = [cmd, payload]
            self._items.append(entry)
            if cmd in self._coalesce:
                self._waiting[cmd] = entry
            self._cond.notify()

    def get(self, timeout=None):
        """Oldest (cmd, payload). Blocks; raises queue.Empty after `timeout` seconds."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._items, timeout):
                raise queue.Empty
            entry = self._items.popleft()
            if self._waiting.get(entry[0]) is entry:
                del self._waiting[entry[0]]
            return entry[0], entry[1]

    def qsize(self):
        with self._cond:
            return len(self._items)
