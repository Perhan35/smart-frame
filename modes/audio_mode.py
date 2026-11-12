import pygame
import numpy as np
import pyaudio
import sys
import os
import json
import yaml
import ctypes
import signal
import time
import queue
import threading
from contextlib import contextmanager

# Shared DSP (smartframe/ package at the repo root): same level math as the orchestrator.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from smartframe import dsp  # noqa: E402
from smartframe import perf  # noqa: E402  # PERF
from smartframe.audio_input import pa_format, resolve_device  # noqa: E402
from smartframe import config_check  # noqa: E402
perf.mark("imports_done")  # PERF

# --- PRELOAD (audio.preload) ---
# Started ahead of time by the orchestrator: the slow imports above are done. Wait
# here, without touching the screen or the mic, for the start signal: one line on
# stdin with the environment to run with (display, mic index...). EOF = not needed.
if os.environ.get("SMARTFRAME_PRELOAD") == "1":
    start_line = sys.stdin.readline()
    if not start_line:
        os._exit(0)
    keep = {k: os.environ[k] for k in ("SMARTFRAME_LEVEL_FD",) if k in os.environ}
    os.environ.clear()
    os.environ.update(json.loads(start_line))
    os.environ.update(keep)  # Our end of the level pipe, opened when we were started
    perf.refresh_from_env()  # PERF
    perf.mark("preload_started")  # PERF

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

# Config with env-var fallbacks (passed from orchestrator for speed)
DEVICE_INDEX = None
THRESHOLD_WARNING = 60
THRESHOLD_ERROR = 85
METER_SETTINGS = dsp.meter_settings({})
CAPTURE_BITS = 16
MIC_CHANNEL = 0
SHOW_DEBUG = True  # CPU stats + "FAST RESPONSE | SLOPE" badge
RENDER_SCALE = 1.0  # < 1: draw at a lower resolution and let SDL scale it up
THEME_NAME = "neon"
FONT_NAME = None  # audio.font: "default" / "barlow"; None = the theme's own font
PIXEL_SHIFT_MINUTES = 10.0  # Move the whole picture a few pixels this often (0 = never)

try:
    if os.path.exists(config_path):
        with open(config_path, "r") as file:
            # Invalid values are dropped (defaults used); main.py logs the warnings.
            config, _ = config_check.check(yaml.safe_load(file))
            audio_config = config.get("audio") or {}
            THRESHOLD_WARNING = audio_config.get("threshold_db_warning", 60)
            THRESHOLD_ERROR = audio_config.get("threshold_db_error", 85)
            METER_SETTINGS = dsp.meter_settings(audio_config)
            CAPTURE_BITS = dsp.capture_bits(audio_config)
            MIC_CHANNEL = dsp.channel_index(audio_config.get("mic_channel", "left"))
            DEVICE_INDEX = audio_config.get("device_index")
            SHOW_DEBUG = bool(audio_config.get("show_debug", True))
            RENDER_SCALE = min(1.0, max(0.25, float(audio_config.get("render_scale") or 1.0)))
            THEME_NAME = str(audio_config.get("theme") or "neon").strip().lower()
            if audio_config.get("font"):
                FONT_NAME = str(audio_config["font"]).strip().lower()
            shift = audio_config.get("pixel_shift_minutes")
            PIXEL_SHIFT_MINUTES = 10.0 if shift is None else float(shift)
except Exception as e:
    print(f"Warning: could not read config.yaml ({e}). Using defaults.")

DEVICE_INDEX_ENV = os.environ.get("SMARTFRAME_AUDIO_DEVICE")
if DEVICE_INDEX_ENV and DEVICE_INDEX_ENV != "None" and DEVICE_INDEX_ENV != "":
    DEVICE_INDEX = int(DEVICE_INDEX_ENV)
# Name of the auto-detected mic: lets us notice an index that now points elsewhere
DEVICE_NAME = os.environ.get("SMARTFRAME_AUDIO_DEVICE_NAME") or None

# Private pipe to the orchestrator (set by main.py): one "<LAeq> <LAFmax>" line per
# second, published to Home Assistant. Absent when run by hand (run_dev.sh).
level_pipe = None
try:
    if os.environ.get("SMARTFRAME_LEVEL_FD"):
        level_pipe = int(os.environ["SMARTFRAME_LEVEL_FD"])
        os.set_blocking(level_pipe, False)  # Never let the pipe stall the rendering
except (ValueError, OSError) as e:
    print(f"Warning: level pipe unavailable ({e}). Home Assistant will not get dBA.")
    level_pipe = None


def send_levels(levels):
    """Send (LAeq, LAFmax) to the orchestrator. Drops the value if the pipe is full."""
    global level_pipe
    if level_pipe is None or levels is None:
        return
    try:
        os.write(level_pipe, f"{levels[0]:.1f} {levels[1]:.1f}\n".encode("ascii"))
    except BlockingIOError:
        pass
    except OSError:
        level_pipe = None  # Orchestrator gone


# --- THEMES (audio.theme) ---
# Every color of the screen. Bars are drawn opaque, already blended with the
# background at "bar_alpha" (same look as transparency, without blending a surface).
_GRAY = (200, 200, 195)  # "Ink" of the monochrome theme: soft warm gray
THEMES = {
    # Neon spectrum on black (the original look)
    "neon": {
        "background": (0, 0, 0),
        "font": "default",  # pygame's own bold font (the original look)
        "bands": None,  # None: the frequency gradient (_COLOR_WAYPOINTS below)
        "ranges": None,  # None: each range's own color (FREQ_RANGES)
        "curve": None,  # None: the bar colors
        "bar_alpha": 140 / 255.0,
        "peak_bar": (234, 234, 234),
        "peak_mark": (255, 255, 255),
        "glow": (40, 40, 40),
        "label": (130, 150, 130),
        "tick": (60, 80, 60),
        "text": (180, 255, 180),
        "warning": (255, 180, 50),
        "error": (255, 80, 80),
        "dbz": (180, 210, 180),
        "stats": (110, 130, 110),
        "hot": (220, 80, 80),  # CPU temperature above 75 °C
        "badge": (110, 135, 110),
        "waiting": (120, 120, 120),
        "icon": (100, 100, 100),
        "icon_slash": (255, 50, 50),
    },
    # Calm monochrome on black, for the e-ink look of a matte screen: soft grays only
    # (no bright surface, nothing glows). Orange / red stay for loud levels.
    "eink": {
        "background": (0, 0, 0),
        "font": "barlow",
        "bands": _GRAY,
        "ranges": (105, 105, 100),
        "curve": (165, 165, 160),
        "bar_alpha": 0.55,
        "peak_bar": (225, 225, 220),
        "peak_mark": (185, 185, 180),
        "glow": (32, 32, 30),
        "label": (125, 125, 120),
        "tick": (60, 60, 58),
        "text": (215, 215, 210),
        "warning": (230, 160, 60),
        "error": (235, 85, 75),
        "dbz": (150, 150, 145),
        "stats": (95, 95, 92),
        "hot": (220, 80, 80),
        "badge": (95, 95, 92),
        "waiting": (120, 120, 118),
        "icon": (100, 100, 98),
        "icon_slash": (220, 70, 60),
    },
}
if THEME_NAME not in THEMES:
    print(f"Unknown theme {THEME_NAME!r}: using 'neon'. Themes: {', '.join(THEMES)}")
    THEME_NAME = "neon"
THEME = THEMES[THEME_NAME]
BACKGROUND = THEME["background"]

# Audio Configuration (Ideal Balancing for Zero 2 WH)
CHUNK_READ = 1536  # Optimized speed (~31Hz updates)
FFT_SIZE = 8192  # High-fidelity 8k FFT for maximum visual resolution
FFT_EVERY = 2  # Big FFT on 1 frame out of 2 (~15 Hz): the bars smooth in between
FORMAT = pa_format(CAPTURE_BITS)
OPEN_RETRY_S = 3.0  # Retry opening the mic this often (it may still be held)
# Format of the open stream: set from the device by open_stream()
stream_channels = 1
stream_rate = 48000


@contextmanager
def ignore_stderr():
    """Context manager to temporarily suppress stderr."""
    if os.environ.get("SMARTFRAME_DEBUG") == "1":
        yield  # In debug mode, don't suppress anything
        return

    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    sys.stderr.flush()
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)


print("Initializing Audio Mode...")

# SDL reads these when the display is initialised, so they are set first.
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    # Headless/Console mode detected, force KMSDRM
    os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
    # Ensure it uses the primary DRM device
    if not os.environ.get("SDL_DRM_DEVICE"):
        os.environ["SDL_DRM_DEVICE"] = "/dev/dri/card0"

# Robust mouse hiding for various SDL/Wayland/X11 backends
os.environ["SDL_VIDEO_WAYLAND_HIDECURSOR"] = "1"
os.environ["SDL_MOUSE_RELATIVE"] = "1"
os.environ["XCURSOR_SIZE"] = "0"
os.environ["COG_PLATFORM_FDO_SHOW_CURSOR"] = "0"

# render_scale < 1: SDL stretches the small image to the screen. pygame asks for
# "nearest" (hard pixel blocks); "linear" smooths them. The environment variable wins
# over pygame's default, and "nearest" can still be forced from the outside.
os.environ.setdefault("SDL_RENDER_SCALE_QUALITY", "linear")

# Detect GPU availability for SDL2
HAS_GPU = os.path.exists("/dev/dri/card0") or os.path.exists("/dev/dri/renderD128")

if HAS_GPU:
    # Ask SDL2 for its GLES2 renderer (used for the window surface on Wayland, and
    # for the scaling of render_scale). Its real effect is still to be checked on the Pi.
    os.environ["SDL_RENDER_DRIVER"] = "opengles2"
    print("GPU detected: asking SDL2 for the GLES2 renderer.")
else:
    print("No GPU detected: Falling back to software/standard rendering.")


def _use_dummy_display(reason):
    """No usable screen: keep running on SDL's dummy driver (the mode still sends dBA)."""
    print(f"Display unavailable ({reason}). Using the dummy video driver.")
    pygame.display.quit()  # A new driver is only picked up by a fresh init()
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()


with ignore_stderr():
    # Selective init: skip mixer/joystick/cdrom probing (~300-500ms saved on Pi Zero 2)
    try:
        pygame.display.init()
    except pygame.error as e:
        _use_dummy_display(e)
    pygame.font.init()
perf.mark("pygame_init_done")  # PERF


def _desktop_size():
    try:
        return tuple(pygame.display.get_desktop_sizes()[0])
    except (pygame.error, IndexError):
        return (1920, 1080)


def _scaled_size():
    """The drawing size for render_scale (the screen's own size at 1.0)."""
    desktop_w, desktop_h = _desktop_size()
    return (max(1, round(desktop_w * RENDER_SCALE)), max(1, round(desktop_h * RENDER_SCALE)))


def _open_screen():
    """Classic display: one surface, at the screen's own size or scaled by SDL
    (render_scale < 1; then the text is scaled up with the bars)."""
    if RENDER_SCALE < 1.0:
        size = _scaled_size()
        print(f"Rendering at {size[0]}x{size[1]}, scaled up to {_desktop_size()}.")
        return pygame.display.set_mode(size, pygame.FULLSCREEN | pygame.SCALED)
    # DOUBLEBUF and HWSURFACE hint SDL2 to use the GPU/video memory if available
    return pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE)


class GpuLayers:
    """Sharp text over smooth bars: the GPU stacks three layers on every frame.

    1. The canvas (bars, peaks, glow, curve): drawn at render_scale on an opaque
       surface (theme background) and stretched to the screen with smoothing. Only the parts that changed
       are sent to the GPU.
    2. The legend band under the bars: drawn once, at full resolution.
    3. Texts and icons: each one its own full-resolution texture, drawn 1:1 (sharp).
    """

    def __init__(self, full_size, canvas_size):
        from pygame._sdl2.video import Renderer, Texture, Window  # Experimental pygame API

        self._texture_cls = Texture
        self.full_rect = pygame.Rect((0, 0), full_size)
        self.window = Window("SmartFrame - Audio Spectrum Analyzer", size=full_size,
                             fullscreen_desktop=True, opengl=True)
        try:
            # accelerated=1: no software fallback (compositing 1080p on the CPU is slower
            # than the classic display).
            self.renderer = Renderer(self.window, accelerated=1, vsync=False)
            output = tuple(self.renderer.get_viewport().size)
            if output != tuple(full_size):
                print(f"Warning: GPU output is {output}, not {tuple(full_size)}: text is scaled.")
            self.renderer.logical_size = full_size
            self.canvas = pygame.Surface(canvas_size)
            self.canvas.fill(BACKGROUND)
            self.canvas_rect = self.canvas.get_rect()
            self.canvas_texture = Texture(self.renderer, canvas_size, streaming=True)
            self.canvas_texture.update(self.canvas)
        except Exception:
            self.window.destroy()
            raise
        self.legend_texture = None
        self.legend_rect = None
        self._textures = {}  # id(surface) -> (surface, texture)
        self._queue = []  # (texture, rect) to draw over the canvas on this frame

    def set_legend(self, surface, topleft):
        self.legend_texture = self._texture_cls.from_surface(self.renderer, surface)
        self.legend_rect = surface.get_rect(topleft=topleft)

    def show(self, surface, topleft):
        """Draw `surface` (a text or an icon) at `topleft`, full-resolution coordinates."""
        entry = self._textures.get(id(surface))
        if entry is None or entry[0] is not surface:
            if len(self._textures) > 300:
                self._textures.clear()
            entry = (surface, self._texture_cls.from_surface(self.renderer, surface))
            self._textures[id(surface)] = entry
        self._queue.append((entry[1], surface.get_rect(topleft=topleft)))

    def present(self, changed_rects, legend):
        """Send the changed parts of the canvas to the GPU, stack the layers, show them."""
        for rect in changed_rects:
            rect = rect.clip(self.canvas_rect)
            if rect.w > 0 and rect.h > 0:
                self.canvas_texture.update(self.canvas.subsurface(rect), rect)
        self.canvas_texture.draw(dstrect=self.full_rect)  # Covers everything: no clear()
        if legend and self.legend_texture is not None:
            self.legend_texture.draw(dstrect=self.legend_rect)
        for texture, rect in self._queue:
            texture.draw(dstrect=rect)
        self._queue.clear()
        self.renderer.present()


gpu = None  # GpuLayers, or None: classic display (everything drawn on `screen`)
if HAS_GPU and os.environ.get("SDL_VIDEODRIVER") != "dummy":
    try:
        gpu = GpuLayers(_desktop_size(), _scaled_size())
        screen = gpu.canvas
        print(f"GPU layers: bars at {screen.get_width()}x{screen.get_height()}, "
              f"text at {gpu.full_rect.w}x{gpu.full_rect.h}.")
    except Exception as e:
        print(f"GPU layers unavailable ({e}). Using the classic display.")
        gpu = None

if gpu is None:
    try:
        screen = _open_screen()
    except pygame.error as e:
        try:
            _use_dummy_display(e)
            screen = pygame.display.set_mode((1, 1))
        except pygame.error:
            print("Fatal error: Could not initialize display.")
            sys.exit(1)
perf.mark("set_mode_done")  # PERF
pygame.display.set_caption("SmartFrame - Audio Spectrum Analyzer")

# Hide the mouse cursor (Standard Pygame method)
try:
    pygame.mouse.set_visible(False)
    # Fallback: Create a purely invisible 1x1 cursor
    pygame.mouse.set_cursor((8, 8), (0, 0), (0,) * 8, (0,) * 8)
except pygame.error:
    pass

# ALSA error suppression
ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(
    None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p
)


def py_error_handler(filename, line, function, err, fmt):
    pass


c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)

try:
    asound = ctypes.cdll.LoadLibrary("libasound.so.2")
    asound.snd_lib_error_set_handler(c_error_handler)
except OSError:
    try:
        asound = ctypes.cdll.LoadLibrary("libasound.so")
        asound.snd_lib_error_set_handler(c_error_handler)
    except OSError:
        pass

# PyAudio initialization
with ignore_stderr():
    p = pyaudio.PyAudio()
perf.mark("pyaudio_init_done")  # PERF


def select_input_device(log):
    """Return (index, info) of the capture device, or (None, None).

    Order: index from the orchestrator / config, system default input, then the
    first device that has inputs.
    """
    if DEVICE_INDEX is not None and isinstance(DEVICE_INDEX, int):
        # With a name (auto-detected mic), an index that now points to another device
        # is not used: the mic is looked up by name instead (ALSA can renumber cards).
        index, info = resolve_device(p, DEVICE_INDEX, DEVICE_NAME)
        if info is not None:
            log(f"Using audio device {index}: '{info.get('name')}'")
            return index, info
        log(f"Warning: audio device {DEVICE_INDEX}"
            + (f" ('{DEVICE_NAME}')" if DEVICE_NAME else "")
            + " is not usable. Probing alternatives...")

    log("Probing system default audio input...")
    try:
        info = p.get_default_input_device_info()
        if info.get("maxInputChannels", 0) > 0:
            log(f"  - Default is '{info.get('name')}' (Index {info.get('index')}). Success.")
            return info.get("index"), info
        log("  - Default input has no channels. Searching all devices...")
    except Exception as e:
        log(f"  - System default input not available: {e}. Searching all devices...")

    log(f"Broadly scanning all {p.get_device_count()} audio devices...")
    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
            log(f"  - Testing Index {i}: '{info.get('name', '')}' ({info.get('maxInputChannels', 0)} inputs)")
            if info.get("maxInputChannels", 0) > 0:
                log(f"  - Found viable device: '{info.get('name', '')}' | Success.")
                return i, info
        except Exception as e:
            log(f"  - Skipping {i}: {e}")
    return None, None


# --- SPECTRUM ANALYZER CONFIGURATION ---
NUM_BANDS = 120  # high density (Mastering Grade)
MIN_FREQ = 25
MAX_FREQ = 22500  # Capped at 22.5kHz to eliminate Nyquist noise artifacts
MIN_DB = 65
MAX_DB = 145  # Balanced range for high-energy music

# Smoothing adjusted for optimized (~31Hz) updates
SMOOTHING_FACTOR = 0.85  # Natural decay for 31Hz baseline
RISE_SMOOTHING = 0.95  # Ultra-Reactive: Sudden peaks are instant (95% delta)
CURVE_SMOOTHING = 0.75  # Responsive curve with smooth 'Liquid' feel
PEAK_GRAVITY = 0.008  # Gravity adjusted for ~31Hz
PEAK_MAX_SPEED = 0.08  # Max speed adjusted for ~31Hz
NOISE_GATE_LOW = 0.12  # Stricter gate to remove baseline mic noise
NOISE_GATE_MID = 0.08  # Gate for mid-ranges
NOISE_GATE_HIGH = 0.12  # Stricter floor for brilliance octaves to combat tilt rise
NOISE_GATE_EXTREME = 0.18 # Aggressive gating for near-Nyquist range

# Professional Slope Setting: Increased to 4.0dB for better visual high-end energy
SLOPE_DB_PER_OCTAVE = 4.0
HIGH_FREQ_BOOST = 4.5  # Reduced slightly to 4.5dB for ultra-high frequencies (>10kHz)

# Frequency Range Definitions (Synchronized with Legend Color Boundaries)
FREQ_RANGES = [
    {"name": "BASS", "min": 20, "max": 225, "level": 1, "color": (100, 150, 255)},
    {"name": "MIDS", "min": 225, "max": 2500, "level": 1, "color": (150, 255, 150)},
    {"name": "TREBLE", "min": 2500, "max": 22500, "level": 1, "color": (255, 150, 100)},
    {"name": "Sub", "min": 20, "max": 60, "level": 0, "color": (80, 120, 220)},
    {"name": "Low", "min": 60, "max": 250, "level": 0, "color": (100, 140, 240)},
    {"name": "Low-Mid", "min": 250, "max": 500, "level": 0, "color": (120, 220, 120)},
    {"name": "Mid", "min": 500, "max": 2000, "level": 0, "color": (140, 240, 140)},
    {"name": "Hi-Mid", "min": 2000, "max": 5000, "level": 0, "color": (160, 255, 160)},
    {
        "name": "Highs",
        "min": 5000,
        "max": 10000,
        "level": 0,
        "color": (240, 200, 120),
    },
    {
        "name": "Brilliance",
        "min": 10000,
        "max": 22500,
        "level": 0,
        "color": (240, 150, 80),
    },
]

# Log-spaced band edges (independent of the sample rate)
band_edges = np.logspace(np.log10(max(1.0, MIN_FREQ)), np.log10(MAX_FREQ), NUM_BANDS + 1)
band_centers = np.sqrt(band_edges[:-1] * band_edges[1:])
# Each band is a run of FFT bins [start, stop). Bins are rate/FFT_SIZE (~5.9 Hz) apart:
# below ~100 Hz several narrow bands fall back to the same single bin, hence the
# "staircase" in the bass. That is the FFT resolution, not a bug.
band_starts = band_stops = band_counts = None  # Filled by build_rate_tables()
band_db_adjust = None
_vlf_mask = _nyquist_mask = None


def build_rate_tables(sample_rate):
    """FFT bin tables that depend on the sample rate (rebuilt when the mic opens)."""
    global band_starts, band_stops, band_counts, band_db_adjust, _vlf_mask, _nyquist_mask
    freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / sample_rate)
    starts = np.searchsorted(freqs, band_edges[:-1], side="left")
    stops = np.searchsorted(freqs, band_edges[1:], side="left")
    for i in np.where(stops <= starts)[0]:
        # Range too narrow for this FFT resolution: take the nearest bin
        k = int(np.argmin(np.abs(freqs - band_centers[i])))
        starts[i], stops[i] = k, k + 1
    band_starts, band_stops = starts, stops
    band_counts = (stops - starts).astype(np.float64)
    # A band's level is the RMS of its bins (power mean). For noise that reads about
    # 1.05 dB above the old amplitude mean when a band has many bins (0 dB for one
    # bin): remove that, so the bars and their noise gates stay where they were tuned.
    band_db_adjust = 10 * np.log10(np.pi / 4) * (1.0 - 1.0 / band_counts)
    _vlf_mask = freqs < 1  # DC/VLF removal mask
    _nyquist_mask = freqs > 22500  # Nyquist cleanup mask


build_rate_tables(stream_rate)

bar_heights = np.zeros(NUM_BANDS)
peak_heights = np.zeros(NUM_BANDS)
current_bar_targets = np.zeros(NUM_BANDS)
audio_buffer = np.zeros(FFT_SIZE, dtype=np.float64)

# Blackman-Harris window for ultra-low spectral leakage (Mastering grade)
fft_window = np.blackman(FFT_SIZE)

# Calculate professional visual tilt based on the requested slope
# We use 1kHz as the zero-crossing reference point
octaves_from_reference = np.log2(np.array(band_centers) / 1000.0)
visual_tilt = octaves_from_reference * SLOPE_DB_PER_OCTAVE
# Tilt + the HF boost for air frequencies (>10kHz), per band
band_db_shaping = visual_tilt + np.where(band_centers > 10000, HIGH_FREQ_BOOST, 0.0)

# Store last peak positions and velocities for gravity effect
peak_pos = np.zeros(NUM_BANDS)
peak_vel = np.zeros(NUM_BANDS)
curve_heights = np.zeros(NUM_BANDS)  # Smooth curve buffer

# --- PRE-COMPUTED CONSTANTS (avoid per-frame recalculation on Pi Zero 2) ---

# Per-band gradient colors (synchronized with frequency legend)
_COLOR_WAYPOINTS = [
    (20, 80, 120, 220),     # Sub
    (60, 100, 140, 240),    # Low
    (250, 120, 220, 120),   # Low-Mid
    (500, 140, 240, 140),   # Mid
    (2000, 160, 255, 160),  # Hi-Mid
    (5000, 240, 200, 120),  # Highs
    (10000, 240, 150, 80),  # Brilliance
    (22500, 220, 100, 60),  # Extended Brilliance
]
band_colors = []
for _fc in band_centers:
    _c = _COLOR_WAYPOINTS[-1][1:]
    for _j in range(len(_COLOR_WAYPOINTS) - 1):
        if _COLOR_WAYPOINTS[_j][0] <= _fc < _COLOR_WAYPOINTS[_j + 1][0]:
            _m = (np.log10(_fc) - np.log10(_COLOR_WAYPOINTS[_j][0])) / (
                np.log10(_COLOR_WAYPOINTS[_j + 1][0]) - np.log10(_COLOR_WAYPOINTS[_j][0])
            )
            _c = (
                int(_COLOR_WAYPOINTS[_j][1] + (_COLOR_WAYPOINTS[_j + 1][1] - _COLOR_WAYPOINTS[_j][1]) * _m),
                int(_COLOR_WAYPOINTS[_j][2] + (_COLOR_WAYPOINTS[_j + 1][2] - _COLOR_WAYPOINTS[_j][2]) * _m),
                int(_COLOR_WAYPOINTS[_j][3] + (_COLOR_WAYPOINTS[_j + 1][3] - _COLOR_WAYPOINTS[_j][3]) * _m),
            )
            break
    band_colors.append(_c)
if THEME["bands"] is not None:
    band_colors = [THEME["bands"]] * NUM_BANDS
# One color for the whole curve (eink), or None: each segment takes its band's color
CURVE_COLOR = THEME["curve"]

# Per-band noise gate thresholds (compensates for visual tilt + HF boost)
# Without this, the tilt/boost inflates the high-frequency noise floor above
# the gate, causing >10kHz bands to appear elevated even in silence.
noise_gate_thresholds = np.zeros(NUM_BANDS)
_db_range = MAX_DB - MIN_DB
for _i, _fc in enumerate(band_centers):
    if _fc < 500:
        _base = NOISE_GATE_LOW
    elif _fc < 4000:
        _base = NOISE_GATE_MID
    elif _fc < 12000:
        _base = NOISE_GATE_HIGH
    else:
        _base = NOISE_GATE_EXTREME
    # Add normalized tilt + boost contribution so gate rises with the boost
    _tilt_norm = max(0, visual_tilt[_i]) / _db_range
    _boost_norm = (HIGH_FREQ_BOOST / _db_range) if _fc > 10000 else 0
    noise_gate_thresholds[_i] = _base + _tilt_norm + _boost_norm

# --- LAYOUT (fixed for the session: the screen size never changes) ---
# Designed for 1920x1080: every size scales with the screen height, so the layout
# keeps its proportions on another screen (and with render_scale).
UI_SCALE = screen.get_height() / 1080.0


def px(value):
    """A 1080p size in pixels, scaled to this screen (never below 1)."""
    return max(1, int(round(value * UI_SCALE)))


analyzer_height = screen.get_height() // 2 + px(80)
BASE_ANALYZER_Y_BOTTOM = screen.get_height() - px(150)
bar_spacing = px(3)  # Tighter spacing for higher density
total_bars_width = screen.get_width() - px(100)
bar_width = max(1, (total_bars_width // NUM_BANDS) - bar_spacing)
BASE_START_X = (screen.get_width() - (NUM_BANDS * (bar_width + bar_spacing))) // 2

BAR_ALPHA = THEME["bar_alpha"]

# Texts, icons and the legend have their own size: the full screen with the GPU
# layers (sharp at any render_scale), the drawing surface otherwise.
TEXT_W, TEXT_H = gpu.full_rect.size if gpu is not None else screen.get_size()
TEXT_SCALE = TEXT_H / 1080.0
TEXT_K = TEXT_SCALE / UI_SCALE  # Drawing-surface pixels -> text pixels


def tpx(value):
    """A 1080p size in text pixels (never below 1)."""
    return max(1, int(round(value * TEXT_SCALE)))


# --- PIXEL SHIFT (audio.pixel_shift_minutes) ---
# The legend and the numbers never move: on an IPS screen left on all day they may
# leave a faint mark. Every few minutes the whole picture moves to the next position
# of this small square, PIXEL_SHIFT_STEP 1080p-pixels apart (not visible from afar).
PIXEL_SHIFT_STEP = 4
PIXEL_SHIFT_PATTERN = [(0, 0), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
shift_x = shift_y = 0  # Text pixels
start_x = BASE_START_X
analyzer_y_bottom = BASE_ANALYZER_Y_BOTTOM
curve_x = []


def set_layout_shift(step):
    """Move everything to position `step` of PIXEL_SHIFT_PATTERN. The caller then
    rebuilds the legend and wipes the whole screen."""
    global shift_x, shift_y, start_x, analyzer_y_bottom, curve_x
    dx, dy = PIXEL_SHIFT_PATTERN[step % len(PIXEL_SHIFT_PATTERN)]
    shift_x = int(round(dx * PIXEL_SHIFT_STEP * TEXT_SCALE))
    shift_y = int(round(dy * PIXEL_SHIFT_STEP * TEXT_SCALE))
    start_x = BASE_START_X + int(round(dx * PIXEL_SHIFT_STEP * UI_SCALE))
    analyzer_y_bottom = BASE_ANALYZER_Y_BOTTOM + int(round(dy * PIXEL_SHIFT_STEP * UI_SCALE))
    curve_x = [start_x + i * (bar_width + bar_spacing) + (bar_width // 2) for i in range(NUM_BANDS)]


set_layout_shift(0)

# Font (audio.font, else the theme's): "default" = pygame's own bold font, "barlow" =
# Barlow, shipped in assets/fonts (SIL Open Font License). pygame's font is also the
# fallback if the Barlow files are missing.
FONT_NAME = FONT_NAME or THEME["font"]
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "fonts")
# Sizes are given in pygame default-font units (it renders at 0.6875 x the asked
# size); FONT_SCALE gives Barlow about the same height, so the layout stays put.
FONT_SCALE = 0.6875 * 1.0


def load_font(size, bold=False):
    """The chosen font at a 1080p size (text pixels)."""
    if FONT_NAME != "barlow":
        return pygame.font.Font(None, tpx(size))
    path = os.path.join(FONT_DIR, "Barlow-SemiBold.ttf" if bold else "Barlow-Regular.ttf")
    try:
        return pygame.font.Font(path, tpx(size * FONT_SCALE))
    except (pygame.error, OSError) as e:
        print(f"Font {path} unavailable ({e}): using pygame's default font.")
        return pygame.font.Font(None, tpx(size))


MARGIN = tpx(50)
font_extra_large = load_font(220, bold=True)
font_medium = load_font(80)
font_tiny = load_font(25)

_text_cache = {}


def render_text(font, text, color):
    """font.render() with a cache: the big dBA text only changes a few times per second."""
    key = (id(font), text, color)
    surface = _text_cache.get(key)
    if surface is None:
        if len(_text_cache) > 300:
            _text_cache.clear()
        surface = font.render(text, True, color)
        _text_cache[key] = surface
    return surface


def _freq_to_x(f):
    pos = NUM_BANDS * (np.log10(f) - np.log10(max(1.0, MIN_FREQ))) / (
        np.log10(MAX_FREQ) - np.log10(max(1.0, MIN_FREQ))
    )
    return start_x + pos * (bar_width + bar_spacing)


def build_static_layer(size, top=0):
    """Draw once everything that never moves: frequency labels and range legend.

    Text coordinates; `top` is the screen row the returned surface starts at (the
    GPU layers only keep the band under the bars)."""
    layer = pygame.Surface(size)
    if gpu is None:
        layer = layer.convert()  # Needs the classic display
    layer.fill(BACKGROUND)
    bottom = analyzer_y_bottom * TEXT_K - top  # Bottom of the bars, in this layer

    # Labels for key frequencies
    for f in [60, 250, 1000, 4000, 16000]:
        if MIN_FREQ <= f <= MAX_FREQ:
            label_x = _freq_to_x(f) * TEXT_K
            label_text = f"{int(f / 1000)}k" if f >= 1000 else f"{f}"
            label_surface = font_tiny.render(label_text, True, THEME["label"])
            layer.blit(label_surface, label_surface.get_rect(midtop=(label_x, bottom + tpx(15))))
            pygame.draw.line(
                layer, THEME["tick"],
                (label_x, bottom + tpx(2)), (label_x, bottom + tpx(10)), 1,
            )

    # Multi-level range labels
    for rng in FREQ_RANGES:
        if rng["max"] >= MIN_FREQ and rng["min"] <= MAX_FREQ:
            x_start = _freq_to_x(max(rng["min"], MIN_FREQ)) * TEXT_K
            x_end = _freq_to_x(min(rng["max"], MAX_FREQ)) * TEXT_K
            level_y = bottom + tpx(50) + (rng["level"] * tpx(40))
            line_color = THEME["ranges"] or rng["color"]
            pygame.draw.line(layer, line_color, (x_start + 2, level_y), (x_end - 2, level_y), tpx(2))
            pygame.draw.line(layer, line_color, (x_start + 2, level_y - tpx(5)), (x_start + 2, level_y + tpx(5)), 1)
            pygame.draw.line(layer, line_color, (x_end - 2, level_y - tpx(5)), (x_end - 2, level_y + tpx(5)), 1)
            range_surface = font_tiny.render(rng["name"], True, line_color)
            range_rect = range_surface.get_rect(center=((x_start + x_end) // 2, level_y + tpx(15)))
            # Only draw if there is space
            if x_end - x_start > range_rect.width:
                layer.blit(range_surface, range_rect)
    return layer


def build_no_mic_icon():
    """Stylized "No Mic" icon (drawn at 100x100, scaled to the screen)."""
    icon = pygame.Surface((100, 100), pygame.SRCALPHA)
    # Mic body (rounded rect) and stand
    pygame.draw.rect(icon, THEME["icon"], (35, 20, 30, 45), border_radius=15)
    pygame.draw.line(icon, THEME["icon"], (50, 65), (50, 80), 3)
    pygame.draw.line(icon, THEME["icon"], (35, 80), (65, 80), 3)
    # The warning slash (red)
    pygame.draw.line(icon, THEME["icon_slash"], (20, 20), (80, 80), 6)
    if tpx(100) != 100:
        icon = pygame.transform.smoothscale(icon, (tpx(100), tpx(100)))
    return icon


def build_legend():
    """(Re)draw the legend at the current position (startup and each pixel shift)."""
    global static_layer
    if gpu is None:
        static_layer = build_static_layer(screen.get_size())
    else:
        # The legend band starts just under the bars (they never reach it), and is
        # drawn over the canvas, opaque, at full resolution.
        legend_top = int((analyzer_y_bottom + px(2)) * TEXT_K)
        gpu.set_legend(build_static_layer((TEXT_W, TEXT_H - legend_top), legend_top), (0, legend_top))


perf.mark("display_ready")  # PERF
static_layer = None
build_legend()
no_mic_icon = build_no_mic_icon()
slope_info = f"{SLOPE_DB_PER_OCTAVE}dB SL" if SLOPE_DB_PER_OCTAVE != 0 else "RAW"
badge_surface = font_tiny.render(f"FAST RESPONSE | SLOPE: {slope_info}", True, THEME["badge"])

# --- MICROPHONE STREAM ---
MIC_QUEUE_CHUNKS = 64  # ~2 s of sound: if the screen stalls longer, the oldest is dropped
mic = None  # MicReader of the open stream
meter = None
last_open_attempt = -OPEN_RETRY_S
last_open_error = None
open_attempts = 0


def open_stream():
    """Open the mic and (re)build everything that depends on its format.

    Returns the stream, or None (the caller retries every OPEN_RETRY_S seconds:
    the orchestrator may still hold the mic for a moment after a mode switch).
    """
    global stream_channels, stream_rate, meter, last_open_error, open_attempts
    open_attempts += 1
    verbose = open_attempts == 1
    try:
        index, info = select_input_device(print if verbose else (lambda *_: None))
        if info is None:
            raise RuntimeError("No suitable audio input devices found with capture capabilities.")
        # Some I2S drivers only accept 2 channels for a mono mic: one is picked later.
        stream_channels, stream_rate = dsp.input_format(info)
        new_stream = p.open(
            format=FORMAT,
            channels=stream_channels,
            rate=stream_rate,
            input=True,
            input_device_index=index,
            frames_per_buffer=CHUNK_READ,
        )
    except Exception as e:
        if str(e) != last_open_error:
            print(f"Could not open the microphone: {e}")
            print(
                "Ensure the INMP441 I2S microphone is properly connected and configured "
                f"(ALSA/dtoverlay). Retrying every {OPEN_RETRY_S:.0f}s..."
            )
            last_open_error = str(e)
        return None

    print(f"Opening stream: {stream_channels} channel(s), {stream_rate}Hz, {CAPTURE_BITS}-bit, "
          f"index {index}, using channel {MIC_CHANNEL if stream_channels > 1 else 0}")
    last_open_error = None
    meter = dsp.LevelMeter(stream_rate, **METER_SETTINGS)
    build_rate_tables(stream_rate)
    audio_buffer[:] = 0
    return new_stream


def close_stream(s):
    try:
        s.stop_stream()
        s.close()
    except Exception as e:
        print(f"Closing the audio stream failed: {e}")


class MicReader:
    """Reads the mic in its own thread, so a slow frame never loses sound.

    The drawing loop takes everything that came in since the previous frame
    (read_all): the level meter sees every sample, whatever the frame rate.
    """

    def __init__(self, stream):
        self.stream = stream
        self.error = None  # Set by the thread when a read fails (it then stops)
        self._chunks = queue.Queue(maxsize=MIC_QUEUE_CHUNKS)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mic-reader", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                data = self.stream.read(CHUNK_READ, exception_on_overflow=False)
            except Exception as e:
                self.error = e
                return
            if self._chunks.full():
                try:
                    self._chunks.get_nowait()  # Screen stalled: drop the oldest chunk
                    perf.frames.add("mic_dropped_chunks", 1)  # PERF
                except queue.Empty:
                    pass
            self._chunks.put(data)

    def read_all(self, timeout):
        """All the bytes received so far, waiting up to `timeout` s for the first chunk."""
        try:
            chunks = [self._chunks.get(timeout=timeout)]
        except queue.Empty:
            return b""
        while True:
            try:
                chunks.append(self._chunks.get_nowait())
            except queue.Empty:
                return b"".join(chunks)

    def close(self):
        """Stop the thread, then the stream (a stream being read must not be closed)."""
        self._stop.set()
        self._thread.join(timeout=0.5)  # One read is ~32 ms
        if self._thread.is_alive():
            print("Mic reader still busy: leaving the stream to the process exit.")
            return
        close_stream(self.stream)


running = True
clock = pygame.time.Clock()
perf.mark("loop_ready")  # PERF

last_ui_update_time = time.time()
last_displayed_dba = 0
last_displayed_dbz = 0
last_level_send = time.time()
frame_count = 0
next_shift_at = 0.0  # 0: the first frame only schedules the first move
shift_index = 0


def signal_handler(sig, frame):
    global running
    print("Interrupt received, shutting down audio mode...")
    running = False


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# --- PARTIAL WIPE ---
# Each frame only wipes what the previous one drew (copied back from the static
# layer), not the whole screen: a full copy costs ~10 ms per frame at render_scale
# 0.75. The analyzer is wiped in vertical strips, each only as high as its tallest
# bar, peak or curve point (the bass is often high while the treble stays low).
WIPE_STRIPS = 12
WIPE_MARGIN = px(12)  # Glow (10 px above its bar), peak marks, curve antialiasing
_strip_edges = np.linspace(0, NUM_BANDS, WIPE_STRIPS + 1).astype(int)
_band_pitch = bar_width + bar_spacing
_screen_rect = screen.get_rect()
dirty_rects = [_screen_rect]  # Drawn on the previous frame: wiped on the next one
dirty_on_legend = None  # Background of the previous frame (True: static layer, False: black)


def analyzer_rects(levels):
    """Screen areas of this frame's bars, peaks, glow and curve (one per strip).
    `levels`: per band, the highest of bar, peak and curve (0..1)."""
    rects = []
    for a, b in zip(_strip_edges[:-1], _strip_edges[1:]):
        # One band more on each side: curve segments cross into the next strip
        level = float(levels[max(a - 1, 0):min(b + 1, NUM_BANDS)].max())
        top = analyzer_y_bottom - int(level * analyzer_height) - WIPE_MARGIN
        x0 = start_x + a * _band_pitch - WIPE_MARGIN
        x1 = start_x + b * _band_pitch + WIPE_MARGIN
        rect = pygame.Rect(x0, top, x1 - x0, analyzer_y_bottom + px(3) - top)
        rects.append(rect.clip(_screen_rect))
    return rects


def show(surface, topleft):
    """Draw a text or an icon (text coordinates): its own sharp texture with the GPU
    layers, else straight onto the screen (then wiped on the next frame)."""
    if gpu is not None:
        gpu.show(surface, topleft)
    else:
        dirty_rects.append(screen.blit(surface, topleft))


def changed_rects(wiped, drawn, everything):
    """Canvas areas to send to the GPU: what was wiped plus what was drawn."""
    if everything:
        return [_screen_rect]
    if len(wiped) == len(drawn):  # Same analyzer strips: one union each
        return [a.union(b) for a, b in zip(wiped, drawn)]
    return wiped + drawn


# --- STATS MONITORING ---
last_stats_update = 0
cpu_usage = 0
cpu_temp = 0
last_cpu_total = 0
last_cpu_idle = 0


def get_cpu_stats():
    """Fetch CPU temperature and raw CPU time data for usage calculation."""
    temp = 0.0
    try:
        # Raspberry Pi temperature path
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                temp = float(f.read()) / 1000.0
    except (IOError, ValueError):
        pass

    total = 0
    idle = 0
    try:
        # Linux CPU stats path
        if os.path.exists("/proc/stat"):
            with open("/proc/stat", "r") as f:
                line = f.readline()
                if line.startswith("cpu "):
                    parts = line.split()
                    # times: user, nice, system, idle, iowait, irq, softirq
                    times = [float(p) for p in parts[1:8]]
                    total = sum(times)
                    idle = times[3]
    except (IOError, ValueError, IndexError):
        pass

    return temp, total, idle


while running:
    perf.frames.start()  # PERF
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            running = False

    now = time.time()

    # --- MICROPHONE: (re)open if needed, then take what the reader thread got ---
    if mic is None and now - last_open_attempt >= OPEN_RETRY_S:
        last_open_attempt = now
        stream = open_stream()
        perf.mark("mic_open_tried")  # PERF
        if stream is not None:
            mic = MicReader(stream)
            perf.frames.add("mic_dropped_chunks", 0)  # PERF
            last_level_send = now

    new_data = None
    if mic is not None:
        # Waits for the first chunk (~32 ms of sound): this paces the loop at ~31 Hz.
        # A slow frame does not wait: it gets all the chunks that came in meanwhile.
        raw_data = mic.read_all(timeout=0.25)
        if mic.error is not None:
            # Don't quit (the watchdog would blank the screen): reopen the mic instead.
            print(f"Audio read error: {mic.error}. Reopening the microphone...")
            mic.close()
            mic = None
            meter = None
            last_open_attempt = now
        elif not raw_data:
            continue  # No sound yet: keep the last image on the screen
        else:
            new_data = dsp.pick_channel(raw_data, stream_channels, MIC_CHANNEL, CAPTURE_BITS)
            perf.frames.add("mic_samples", len(new_data))  # PERF

    perf.frames.lap("mic_wait")  # PERF
    # Anti burn-in: move everything a few pixels now and then (full redraw)
    if PIXEL_SHIFT_MINUTES > 0 and now >= next_shift_at:
        if next_shift_at:
            shift_index += 1
            set_layout_shift(shift_index)
            build_legend()
            dirty_on_legend = None  # Forces a full wipe (and GPU upload) below
        next_shift_at = now + PIXEL_SHIFT_MINUTES * 60

    # Wipe last frame's drawings: static layer (black + legend) with sound, plain
    # black on the "waiting" screen. A change of background wipes everything.
    on_legend = new_data is not None
    # With the GPU layers the canvas is plain background (the legend is its own layer).
    wipe_all = on_legend != dirty_on_legend
    if wipe_all:
        dirty_rects = [_screen_rect]
        dirty_on_legend = on_legend
    for rect in dirty_rects:
        if on_legend and gpu is None:
            screen.blit(static_layer, rect, rect)
        else:
            screen.fill(BACKGROUND, rect)
    wiped_rects, dirty_rects = dirty_rects, []

    perf.frames.lap("clear")  # PERF
    # --- UPDATE CPU STATS (1Hz, only when shown: audio.show_debug) ---
    if SHOW_DEBUG and now - last_stats_update >= 1.0:
        curr_temp, curr_total, curr_idle = get_cpu_stats()
        if last_cpu_total > 0:
            diff_total = curr_total - last_cpu_total
            diff_idle = curr_idle - last_cpu_idle
            if diff_total > 0:
                cpu_usage = (1.0 - (diff_idle / diff_total)) * 100.0

        last_cpu_total = curr_total
        last_cpu_idle = curr_idle
        cpu_temp = curr_temp
        last_stats_update = now

    if SHOW_DEBUG:
        # Render minimalist CPU stats (top-left, discreet)
        stats_color = THEME["hot"] if cpu_temp > 75 else THEME["stats"]
        stats_text = f"CPU {int(cpu_usage)}%  |  {cpu_temp:.1f}°C"
        show(render_text(font_tiny, stats_text, stats_color), (MARGIN + shift_x, MARGIN + shift_y))

    if new_data is not None:
        # --- SOUND LEVEL (shared DSP, same math as the orchestrator) ---
        meter.process(new_data)

        # On-screen number: reactive style (instant attack + 250 ms hold).
        # Display only: Home Assistant gets the 1 s LAeq below.
        db_a, db_z = meter.reactive()
        current_time = time.time()
        if db_a > last_displayed_dba or current_time - last_ui_update_time >= 0.25:
            last_displayed_dba = db_a
            last_displayed_dbz = db_z
            last_ui_update_time = current_time

        # Once per second: LAeq / LAFmax of that second to the orchestrator (-> MQTT)
        if current_time - last_level_send >= 1.0:
            send_levels(meter.pop_interval())
            last_level_send = current_time

        # Slide the analysis buffer (in place, no new allocation)
        n = min(len(new_data), FFT_SIZE)
        audio_buffer[:-n] = audio_buffer[n:]
        audio_buffer[-n:] = new_data[-n:]

        # --- SPECTRUM (big FFT on 1 frame out of FFT_EVERY) ---
        if frame_count % FFT_EVERY == 0:
            fft_complex = np.fft.rfft(audio_buffer * fft_window)
            # DC/VLF Removal + Nyquist cleanup (pre-computed masks)
            fft_complex[_vlf_mask] = 0
            fft_complex[_nyquist_mask] = 0
            power = fft_complex.real**2 + fft_complex.imag**2
            # Mean power of each band (cumulative sum: bands may share a bin)
            cumulative = np.concatenate(([0.0], np.cumsum(power)))
            band_power = (cumulative[band_stops] - cumulative[band_starts]) / band_counts
            # Band level in dB (RMS of its bins). No calibration offset: the bars are
            # a visual on their own MIN_DB..MAX_DB scale, they must not move when
            # the dBA number is calibrated.
            db_vals = (10 * np.log10(np.maximum(band_power, 1e-18))
                       + band_db_adjust + band_db_shaping)
            np.clip((db_vals - MIN_DB) / (MAX_DB - MIN_DB), 0, 1, out=current_bar_targets)
        frame_count += 1

        # Visual smoothing: fast rise, exponential decay
        rising = current_bar_targets > bar_heights
        bar_heights[:] = np.where(
            rising,
            bar_heights + RISE_SMOOTHING * (current_bar_targets - bar_heights),
            bar_heights * SMOOTHING_FACTOR,
        )
        # Dynamic Noise Gate (pre-computed, compensates for tilt + HF boost)
        bar_heights[bar_heights < noise_gate_thresholds] = 0

        # Dominant frequency (peak highlight): the highest bar above 0.15
        candidates = np.where(bar_heights > 0.15, bar_heights, -1.0)
        max_energy_idx = int(np.argmax(candidates))
        if candidates[max_energy_idx] < 0:
            max_energy_idx = -1

        # Professional Unity-style peak handling (Gravity based)
        pushed = bar_heights > peak_pos
        peak_vel[:] = np.where(pushed, 0.0, np.minimum(PEAK_MAX_SPEED, peak_vel + PEAK_GRAVITY))
        peak_pos[:] = np.where(pushed, bar_heights, peak_pos - peak_vel)
        landed = peak_pos < bar_heights
        peak_pos[landed] = bar_heights[landed]
        peak_vel[landed] = 0.0

        # --- CURVE CALCULATION ---
        # A second, even smoother buffer for the "Professional Curve"
        curve_heights[:] = curve_heights * CURVE_SMOOTHING + bar_heights * (1 - CURVE_SMOOTHING)
        curve_y = analyzer_y_bottom - curve_heights * analyzer_height
        curve_points = list(zip(curve_x, curve_y.tolist()))

        # --- DRAWING PASS (all opaque, straight onto the screen) ---

        perf.frames.lap("dsp")  # PERF
        # 1. Subtle glow behind the dominant bar (drawn first, so bars stay on top)
        if max_energy_idx >= 0:
            h = int(bar_heights[max_energy_idx] * analyzer_height)
            x = start_x + max_energy_idx * (bar_width + bar_spacing)
            pygame.draw.rect(
                screen, THEME["glow"],
                (x - px(4), analyzer_y_bottom - h - px(10), bar_width + px(8), h + px(10)),
                border_radius=px(8),
            )

        # 2. Bars, then the sharp peak indicators above them
        for i in range(NUM_BANDS):
            h = int(bar_heights[i] * analyzer_height)
            ph = int(peak_pos[i] * analyzer_height)
            x = start_x + i * (bar_width + bar_spacing)
            is_peak = i == max_energy_idx

            if h > px(4):
                if is_peak:
                    color = THEME["peak_bar"]
                else:
                    # Bar strength based on height, blended over the background
                    k = (0.6 + 0.4 * bar_heights[i]) * BAR_ALPHA
                    c = band_colors[i]
                    color = (int(BACKGROUND[0] + (c[0] - BACKGROUND[0]) * k),
                             int(BACKGROUND[1] + (c[1] - BACKGROUND[1]) * k),
                             int(BACKGROUND[2] + (c[2] - BACKGROUND[2]) * k))
                pygame.draw.rect(
                    screen, color, (x, analyzer_y_bottom - h, bar_width, h), border_radius=px(4)
                )

            # Skip the indicator for the dominant peak bar
            if not is_peak and ph > px(5):
                pygame.draw.rect(
                    screen,
                    THEME["peak_mark"],
                    (x, analyzer_y_bottom - ph - px(3), bar_width, px(2)),
                    border_radius=1,
                )

        # 3. The "High-Fidelity Curve" line, plus a copy 1 px higher for a "thick" look
        #    that keeps the antialiasing.
        if CURVE_COLOR is not None:
            # Single color (eink): the whole curve in 2 calls
            pygame.draw.aalines(screen, CURVE_COLOR, False, curve_points)
            pygame.draw.aalines(screen, CURVE_COLOR, False, [(x, y - 1) for x, y in curve_points])
        else:
            # Each segment in its band's color: a continuous gradient synchronized with
            # the legend (~240 small calls; grouping them would show color steps).
            for i in range(NUM_BANDS - 1):
                p1 = curve_points[i]
                p2 = curve_points[i + 1]
                c = band_colors[i]
                pygame.draw.aaline(screen, c, p1, p2)
                pygame.draw.aaline(screen, c, (p1[0], p1[1] - 1), (p2[0], p2[1] - 1))
        dirty_rects.extend(analyzer_rects(np.maximum(np.maximum(bar_heights, peak_pos), curve_heights)))

        # Determine color based on threshold (Evaluate using dBA, professional standard)
        text_color = THEME["text"]
        if last_displayed_dba >= THRESHOLD_ERROR:
            text_color = THEME["error"]
        elif last_displayed_dba >= THRESHOLD_WARNING:
            text_color = THEME["warning"]

        # Render dBA text (Large, Integer only) — cached until the value changes
        dba_text = render_text(font_extra_large, f"{int(round(last_displayed_dba))} dBA", text_color)
        dba_rect = dba_text.get_rect()
        dba_rect.topright = (TEXT_W - MARGIN + shift_x, MARGIN + shift_y)
        show(dba_text, dba_rect.topleft)

        # Render dBZ text (Medium)
        dbz_text = render_text(font_medium, f"{last_displayed_dbz:.1f} dBZ", THEME["dbz"])
        dbz_rect = dbz_text.get_rect()
        dbz_rect.topright = (dba_rect.right, dba_rect.bottom + tpx(10))
        show(dbz_text, dbz_rect.topleft)

        if SHOW_DEBUG:
            # "FAST RESPONSE" indicator (Tiny status info, pre-rendered)
            badge_rect = badge_surface.get_rect()
            badge_rect.topright = (dba_rect.right, dbz_rect.bottom + tpx(10))
            show(badge_surface, badge_rect.topleft)
    else:
        # 1. The "No Mic" icon in the top-left corner, below the CPU stats if shown
        icon_y = MARGIN + shift_y + (font_tiny.get_linesize() + tpx(10) if SHOW_DEBUG else 0)
        show(no_mic_icon, (MARGIN + shift_x, icon_y))

        # 2. Render localized error message
        error_text = render_text(
            font_medium, "Waiting for I2S Microphone Signal...", THEME["waiting"]
        )
        text_rect = error_text.get_rect(
            center=(TEXT_W / 2 + shift_x, TEXT_H / 2 + shift_y)
        )
        show(error_text, text_rect.topleft)

    perf.frames.lap("draw")  # PERF
    if gpu is not None:
        gpu.present(changed_rects(wiped_rects, dirty_rects, wipe_all), on_legend)
    else:
        pygame.display.flip()
    perf.frames.lap("flip")  # PERF
    # With a mic, waiting for its next chunk already paces the loop (~31 Hz), so this
    # never waits. Without one, it keeps the "waiting" screen from spinning the CPU.
    clock.tick(60 if mic is not None else 10)
    perf.frames.lap("tick")  # PERF
    perf.frames.end()  # PERF

# Fast exit: the kernel frees the mic, the screen and the Wayland connection when the
# process ends. p.terminate() and pygame.quit() are skipped: they may be slow on a
# Pi Zero 2, and the orchestrator force-kills a mode still running after 2 s.
perf.mark("loop_exited")  # PERF
if mic is not None:
    mic.close()
perf.mark("mic_closed")  # PERF
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
