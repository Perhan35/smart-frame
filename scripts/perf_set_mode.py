#!/usr/bin/env python3
"""Temporary test (see smartframe/perf.py): how long pygame takes to open the audio
mode's window, and how fast flip() is, with different SDL settings.

Run it on the Pi with the service in mode "off" and keep_compositor_when_off: true
(labwc stays up). It turns the screen output on, opens a short test window per
setting (each in a fresh process, twice: 1st = cold, 2nd = warm caches), then turns
the output off again. Takes about 3 minutes.

    .venv/bin/python scripts/perf_set_mode.py
"""

import json
import os
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


def main():
    env = base_env()
    if "WAYLAND_DISPLAY" not in env:
        sys.exit("No Wayland socket found: is the service running, in mode \"off\", "
                 "with keep_compositor_when_off: true?")
    print(f"Wayland socket: {env['WAYLAND_DISPLAY']}")

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
