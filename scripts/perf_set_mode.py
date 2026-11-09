#!/usr/bin/env python3
"""Diagnostic test (see smartframe/perf.py): how long pygame takes to open the audio
mode's window, and how fast flip() is, with different SDL settings.

Run it on the Pi with the service in mode "off" and keep_compositor_when_off: true
(labwc stays up). It turns the screen output on, opens a short test window per
setting (each in a fresh process, twice: 1st = cold, 2nd = warm caches), then turns
the output off again. Takes about 3 minutes.

    .venv/bin/python scripts/perf_set_mode.py

--trace looks inside one window opening, with the audio mode's settings
(render_scale from config.yaml): for each step, wall time vs CPU time vs data read
from the SD card, plus the longest silences in the Wayland messages with labwc. It
runs 3 times: the 1st after emptying the file cache (sudo asks for your password;
--no-cold skips that), the 2 others warm. About 1 minute.

    .venv/bin/python scripts/perf_set_mode.py --trace
"""

import json
import os
import re
import subprocess
import sys

# Runs in a fresh process for each setting (SDL is set up once per process).
CHILD = r"""
import json, sys, time
t = time.perf_counter()
import pygame
r = {"import_pygame": time.perf_counter() - t}
scale = float(sys.argv[1])
t = time.perf_counter()
pygame.display.init()
pygame.font.init()
r["display_init"] = time.perf_counter() - t
r["driver"] = pygame.display.get_driver()
w, h = pygame.display.get_desktop_sizes()[0]
t = time.perf_counter()
if scale > 0:
    size = (round(w * scale), round(h * scale))
    screen = pygame.display.set_mode(size, pygame.FULLSCREEN | pygame.SCALED)
else:
    flags = pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE
    screen = pygame.display.set_mode((0, 0), flags)
r["set_mode"] = time.perf_counter() - t
r["size"] = "%dx%d" % screen.get_size()
layer = pygame.Surface(screen.get_size()).convert()
layer.fill((10, 20, 10))
t = time.perf_counter()
pygame.display.flip()
r["first_flip"] = time.perf_counter() - t
clear = flip = 0.0
for i in range(60):
    t0 = time.perf_counter()
    screen.blit(layer, (0, 0))
    pygame.draw.rect(screen, (200, 200, 200), ((i * 15) % screen.get_width(), 100, 50, 300))
    t1 = time.perf_counter()
    pygame.display.flip()
    t2 = time.perf_counter()
    clear += t1 - t0
    flip += t2 - t1
    # Paced like the audio mode (~31 Hz): back-to-back flips would just measure
    # the compositor's 60 Hz refresh.
    time.sleep(max(0.0, 0.032 - (time.perf_counter() - t0)))
r["clear_ms"] = clear / 60 * 1000
r["flip_ms"] = flip / 60 * 1000
t = time.perf_counter()
pygame.quit()
r["quit"] = time.perf_counter() - t
print("RESULT " + json.dumps(r), flush=True)
"""

IMPORTS = r"""
import json, time
r = {}
for name in ("numpy", "pygame", "pyaudio", "yaml"):
    t = time.perf_counter()
    __import__(name)
    r[name] = time.perf_counter() - t
print("RESULT " + json.dumps(r), flush=True)
"""

# (label, render scale (0 = the full-size path of the audio mode), extra environment)
GLES = {"SDL_RENDER_DRIVER": "opengles2"}
LINEAR = {"SDL_RENDER_SCALE_QUALITY": "linear"}
SETTINGS = [
    ("full size, gles2 (scale 1.0 today)", 0, GLES),
    ("full size, no renderer hint", 0, {}),
    ("full size, gles2, wayland forced", 0, {**GLES, "SDL_VIDEODRIVER": "wayland"}),
    ("SCALED 1.0, gles2", 1.0, {**GLES, **LINEAR}),
    ("SCALED 0.75, gles2, linear", 0.75, {**GLES, **LINEAR}),
    ("SCALED 0.67, gles2, linear", 0.67, {**GLES, **LINEAR}),
    ("SCALED 0.5, gles2, linear", 0.5, {**GLES, **LINEAR}),
    ("SCALED 0.75, software renderer", 0.75, {"SDL_RENDER_DRIVER": "software", **LINEAR}),
]


def base_env():
    """The service's display environment: labwc's Wayland socket, no X11."""
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("SDL_RENDER_DRIVER", None)
    env.pop("SDL_RENDER_SCALE_QUALITY", None)
    env["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    runtime = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if "WAYLAND_DISPLAY" not in env:
        for name in ("wayland-0", "wayland-1"):
            if os.path.exists(os.path.join(runtime, name)):
                env["WAYLAND_DISPLAY"] = name
                break
    return env


def run(code, args, env):
    """Run `code` in a fresh interpreter; its RESULT line as a dict, or the error text."""
    try:
        out = subprocess.run(
            [sys.executable, "-c", code, *args], env=env, capture_output=True,
            text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return "timeout (90 s)"
    for line in out.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    return (out.stderr.strip().splitlines() or ["no output"])[-1]


def disabled_outputs(env):
    """Screen outputs that labwc has switched off (wlr-randr), e.g. ["HDMI-A-1"]."""
    try:
        text = subprocess.run(["wlr-randr"], env=env, capture_output=True,
                              text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        print("wlr-randr not available: the screen output is left as it is.")
        return []
    names, current = [], None
    for line in text.splitlines():
        if line and not line[0].isspace():
            current = line.split()[0]
        elif current and line.strip() == "Enabled: no":
            names.append(current)
    return names


def set_outputs(env, names, on):
    for name in names:
        subprocess.run(["wlr-randr", "--output", name, "--on" if on else "--off"],
                       env=env, capture_output=True, timeout=10)


# --trace: one window opening, step by step. Each step reports wall time, CPU time,
# major page faults and bytes read: a long wall time with little CPU is waiting
# (SD card reads if read_mb is high, else the compositor).
TRACE_CHILD = r"""
import json, os, resource, sys, time
def snap():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return time.perf_counter(), r.ru_utime + r.ru_stime, r.ru_majflt, r.ru_inblock
steps = {}
def step(name, start):
    end = snap()
    steps[name] = {"wall": end[0] - start[0], "cpu": end[1] - start[1],
                   "majflt": end[2] - start[2], "read_mb": (end[3] - start[3]) * 512 / 1e6}
    print("### end of " + name, file=sys.stderr, flush=True)  # Marker in the Wayland log
    return end
s = snap()
import numpy, pygame
s = step("import numpy+pygame", s)
pygame.display.init()
pygame.font.init()
s = step("display.init", s)
w, h = pygame.display.get_desktop_sizes()[0]
scale = float(sys.argv[1])
if scale < 1.0:
    screen = pygame.display.set_mode((round(w * scale), round(h * scale)),
                                     pygame.FULLSCREEN | pygame.SCALED)
else:
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE)
s = step("set_mode", s)
pygame.display.flip()
s = step("first flip", s)
print("RESULT " + json.dumps(steps), flush=True)
sys.stdout.flush()
os._exit(0)
"""

# libwayland's WAYLAND_DEBUG lines start with "[<ms>.<us>]" (a 32-bit µs counter)
_WL_TIME = re.compile(r"^\[\s*(\d+)\.(\d{3})\]")


def wayland_gaps(log, top=5):
    """The `top` longest silences between two Wayland messages: (seconds, step, before, after)."""
    gaps, prev_t, prev_line, current = [], None, "", "import numpy+pygame"
    steps = ["import numpy+pygame", "display.init", "set_mode", "first flip"]
    for line in log.splitlines():
        if line.startswith("### end of "):
            name = line[len("### end of "):]
            if name in steps and steps.index(name) + 1 < len(steps):
                current = steps[steps.index(name) + 1]
            continue
        m = _WL_TIME.match(line)
        if not m:
            continue
        t = int(m.group(1)) * 1000 + int(m.group(2))
        if prev_t is not None:
            gaps.append(((t - prev_t) % 2**32 / 1e6, current, prev_line, line))
        prev_t, prev_line = t, line
    return sorted(gaps, reverse=True)[:top]


def render_scale():
    """audio.render_scale from config.yaml, as the audio mode reads it."""
    try:
        import yaml
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
        with open(path) as f:
            audio = (yaml.safe_load(f) or {}).get("audio") or {}
        return min(1.0, max(0.25, float(audio.get("render_scale") or 1.0)))
    except Exception:
        return 1.0


def drop_file_cache():
    """Empty the kernel's file cache (as after the browser pushed everything out)."""
    print("Emptying the file cache (sudo)...")
    done = subprocess.run(["sudo", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"])
    return done.returncode == 0


def trace(env, cold):
    scale = render_scale()
    env = {**env, "WAYLAND_DEBUG": "client", "SDL_RENDER_DRIVER": "opengles2",
           "SDL_RENDER_SCALE_QUALITY": "linear"}
    print(f"render_scale {scale} (from config.yaml), GLES2, linear scaling")
    results = []
    for i in range(3):
        label = "warm"
        if i == 0 and cold:
            label = "cold" if drop_file_cache() else "warm (cache not emptied)"
        try:
            out = subprocess.run([sys.executable, "-c", TRACE_CHILD, str(scale)], env=env,
                                 capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"  run {i + 1}: timeout (120 s)")
            continue
        steps = None
        for line in out.stdout.splitlines():
            if line.startswith("RESULT "):
                steps = json.loads(line[7:])
        if steps is None:
            print(f"  run {i + 1} failed: {(out.stderr.strip().splitlines() or ['no output'])[-1]}")
            continue
        results.append((label, steps, out.stderr))

    print("\n=== Per step: wall s | CPU s | major page faults | MB read from disk ===")
    for i, (label, steps, _) in enumerate(results):
        print(f"  run {i + 1} ({label})")
        for name, v in steps.items():
            print(f"    {name:20} {v['wall']:6.2f} | {v['cpu']:5.2f} | {v['majflt']:6d} | "
                  f"{v['read_mb']:6.1f}")

    if results:
        label, steps, log = max(results, key=lambda r: r[1]["set_mode"]["wall"])
        gaps = wayland_gaps(log)
        print(f"\n=== Longest silences in the Wayland messages (slowest run: {label}) ===")
        if not gaps:
            print("  no timestamped Wayland message found")
        for seconds, step_name, before, after in gaps:
            print(f"  {seconds:5.2f} s during {step_name}")
            print(f"      after:  {before[:110]}")
            print(f"      before: {after[:110]}")
    print("\nLong wall time with low CPU = waiting: on the SD card if MB read is high, "
          "else on labwc (see the silences).")


def main():
    env = base_env()
    if "WAYLAND_DISPLAY" not in env:
        sys.exit("No Wayland socket found: is the service running, in mode \"off\", "
                 "with keep_compositor_when_off: true?")
    print(f"Wayland socket: {env['WAYLAND_DISPLAY']}")

    if "--trace" in sys.argv:
        outputs = disabled_outputs(env)
        set_outputs(env, outputs, True)
        try:
            trace(env, cold="--no-cold" not in sys.argv)
        finally:
            set_outputs(env, outputs, False)  # Back the way they were
        return

    print("\n=== Import times (s), 1st / 2nd run ===")
    runs = [run(IMPORTS, [], env) for _ in range(2)]
    if all(isinstance(r, dict) for r in runs):
        for name in runs[0]:
            print(f"  {name:8} {runs[0][name]:.2f} / {runs[1][name]:.2f}")
    else:
        print(f"  failed: {runs}")

    # A switched-off output gets no frames: flip() would wait or mislead.
    outputs = disabled_outputs(env)
    set_outputs(env, outputs, True)
    try:
        print("\n=== Window setup (s) and per-frame cost (ms), 1st / 2nd run ===")
        print(f"  {'setting':36} {'set_mode':>11} {'1st flip':>9} {'clear':>6} "
              f"{'flip':>6} {'quit':>5}  driver, size")
        for label, scale, extra in SETTINGS:
            runs = [run(CHILD, [str(scale)], {**env, **extra}) for _ in range(2)]
            bad = [r for r in runs if not isinstance(r, dict)]
            if bad:
                print(f"  {label:36} failed: {bad[0]}")
                continue
            a, b = runs
            print(f"  {label:36} {a['set_mode']:5.2f}/{b['set_mode']:<5.2f} "
                  f"{b['first_flip']:9.2f} {b['clear_ms']:6.1f} {b['flip_ms']:6.1f} "
                  f"{b['quit']:5.2f}  {b['driver']}, {b['size']}")
    finally:
        set_outputs(env, outputs, False)  # Back the way they were
    print("\n(clear, flip, quit and 1st flip are from the 2nd run.)")


if __name__ == "__main__":
    main()
