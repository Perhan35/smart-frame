"""Optional performance measurements (startup, mode switch, frame times, CPU).

Off by default: nothing is measured and every function returns at once unless
SMARTFRAME_PERF=1 (scripts/perf_report.sh on / off sets it for the service).
Output lines start with "[PERF]" and go to stdout (journalctl -u smartframe).

To remove: delete this file, scripts/perf_report.sh and scripts/perf_set_mode.py,
then every line tagged "# PERF" in main.py and modes/audio_mode.py.
"""

import os
import time

ENABLED = os.environ.get("SMARTFRAME_PERF") == "1"
REPORT_EVERY_S = 10.0

_tag = os.path.basename(os.environ.get("SMARTFRAME_PERF_TAG", "") or "main")


def _process_start_wall():
    """Wall-clock time this process started (from /proc), or now if unknown."""
    try:
        with open("/proc/self/stat") as f:
            start_ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        return time.time() - uptime + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return time.time()


_proc_start = _process_start_wall() if ENABLED else 0.0


def _log(msg):
    print(f"[PERF] {_tag}: {msg}", flush=True)


def refresh_from_env():
    """For a process started ahead of time (audio.preload), once it gets its real
    environment: label its lines with the mode switch it now serves."""
    global _tag
    _tag = os.path.basename(os.environ.get("SMARTFRAME_PERF_TAG", "") or "main")


def begin_switch(mode):
    """Start timing a mode switch. Child processes inherit the start time."""
    if not ENABLED:
        return
    os.environ["SMARTFRAME_PERF_T0"] = repr(time.time())
    os.environ["SMARTFRAME_PERF_TAG"] = mode
    _log(f"switch -> {mode}: start")


def mark(name):
    """Log a step: time since process start, and since the mode switch began."""
    if not ENABLED:
        return
    now = time.time()
    parts = [f"{name}", f"proc +{now - _proc_start:.3f}s"]
    t0 = os.environ.get("SMARTFRAME_PERF_T0")
    if t0:
        try:
            parts.append(f"switch +{now - float(t0):.3f}s")
        except ValueError:
            pass
    _log("  ".join(parts))


def _rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return 0.0


def _cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return float(f.read()) / 1000
    except (OSError, ValueError):
        return 0.0


class _Frames:
    """Per-frame section timer. Call start() at the top of the loop, lap(name) after each part."""

    def __init__(self):
        self.sections = {}
        self.frames = 0
        self.first_frame_done = False
        self.t_last = 0.0
        self.t_report = time.monotonic()
        self.cpu_report = sum(os.times()[:2])
        self.counters = {}  # name -> total since the last report (shown per second)

    def start(self):
        if not ENABLED:
            return
        self.t_last = time.perf_counter()

    def add(self, name, value):
        """Add to a counter (any thread). Once seen, it is shown in every report."""
        if not ENABLED:
            return
        self.counters[name] = self.counters.get(name, 0) + value

    def lap(self, name):
        if not ENABLED:
            return
        t = time.perf_counter()
        self.sections.setdefault(name, []).append((t - self.t_last) * 1000)
        self.t_last = t

    def end(self):
        if not ENABLED:
            return
        if not self.first_frame_done:
            self.first_frame_done = True
            mark("first_frame")
        self.frames += 1
        now = time.monotonic()
        elapsed = now - self.t_report
        if elapsed < REPORT_EVERY_S:
            return
        cpu = sum(os.times()[:2])
        cpu_pct = 100 * (cpu - self.cpu_report) / elapsed  # 100% = one full core
        counters, self.counters = self.counters, {}
        self.counters.update(dict.fromkeys(counters, 0))
        stats = []
        for name, values in self.sections.items():
            values.sort()
            avg = sum(values) / len(values)
            p95 = values[int(len(values) * 0.95) - 1] if len(values) > 1 else values[0]
            stats.append(f"{name} {avg:.1f}/{p95:.1f}")
        _log(
            f"fps {self.frames / elapsed:.1f}  cpu {cpu_pct:.0f}%  rss {_rss_mb():.0f}MB  "
            f"temp {_cpu_temp():.1f}C  ms avg/p95: " + "  ".join(stats)
            + "".join(f"  {name}/s {total / elapsed:.0f}" for name, total in counters.items())
        )
        self.sections.clear()
        self.frames = 0
        self.t_report = now
        self.cpu_report = cpu


frames = _Frames()
