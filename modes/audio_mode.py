import pygame
import numpy as np
import pyaudio
import sys
import os
import yaml
import ctypes
import signal
import time
from contextlib import contextmanager

# Shared DSP (smartframe/ package at the repo root): same level math as the orchestrator.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from smartframe import dsp  # noqa: E402
from smartframe import perf  # noqa: E402  # PERF
perf.mark("imports_done")  # PERF

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

# Config with env-var fallbacks (passed from orchestrator for speed)
DEVICE_INDEX = None
THRESHOLD_WARNING = 60
THRESHOLD_ERROR = 85
CALIBRATION_OFFSET = 0
NOISE_FLOOR = None
MIC_CHANNEL = 0

try:
    if os.path.exists(config_path):
        with open(config_path, "r") as file:
            config = yaml.safe_load(file) or {}
            audio_config = config.get("audio") or {}
            THRESHOLD_WARNING = audio_config.get("threshold_db_warning", 60)
            THRESHOLD_ERROR = audio_config.get("threshold_db_error", 85)
            CALIBRATION_OFFSET = audio_config.get("calibration_offset_db", 0) or 0
            NOISE_FLOOR = audio_config.get("noise_floor_dba")
            MIC_CHANNEL = dsp.channel_index(audio_config.get("mic_channel", "left"))
            DEVICE_INDEX = audio_config.get("device_index")
except Exception as e:
    print(f"Warning: could not read config.yaml ({e}). Using defaults.")

DEVICE_INDEX_ENV = os.environ.get("SMARTFRAME_AUDIO_DEVICE")
if DEVICE_INDEX_ENV and DEVICE_INDEX_ENV != "None" and DEVICE_INDEX_ENV != "":
    DEVICE_INDEX = int(DEVICE_INDEX_ENV)

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


# Audio Configuration (Ideal Balancing for Zero 2 WH)
CHUNK_READ = 1536  # Optimized speed (~31Hz updates)
FFT_SIZE = 8192  # High-fidelity 8k FFT for maximum visual resolution
FFT_EVERY = 2  # Big FFT on 1 frame out of 2 (~15 Hz): the bars smooth in between
FORMAT = pyaudio.paInt16
OPEN_RETRY_S = 3.0  # Retry opening the mic this often (it may still be held)
# Set from the device when the stream opens (see open_stream)
CHANNELS = 1
SAMPLE_RATE = 48000


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
with ignore_stderr():
    # Selective init: skip mixer/joystick/cdrom probing (~300-500ms saved on Pi Zero 2)
    pygame.display.init()
    pygame.font.init()

# Display initialization (1080p full screen, intended for a 14" LCD)
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    # Headless/Console mode detected, force KMSDRM
    os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
    # Ensure it uses the primary DRM device
    if not os.environ.get("SDL_DRM_DEVICE"):
        os.environ["SDL_DRM_DEVICE"] = "/dev/dri/card0"

# Robust mouse hiding and GPU acceleration for various SDL/Wayland/X11 backends
os.environ["SDL_VIDEO_WAYLAND_HIDECURSOR"] = "1"
os.environ["SDL_MOUSE_RELATIVE"] = "1"
os.environ["XCURSOR_SIZE"] = "0"
os.environ["COG_PLATFORM_FDO_SHOW_CURSOR"] = "0"

# Detect GPU availability for SDL2
HAS_GPU = os.path.exists("/dev/dri/card0") or os.path.exists("/dev/dri/renderD128")

if HAS_GPU:
    # Force Hardware Acceleration in SDL2 (Very useful for Pi Zero 2 WH)
    os.environ["SDL_RENDER_DRIVER"] = "opengles2"
    os.environ["SDL_HINT_RENDER_DRIVER"] = "opengles2"
    os.environ["SDL_HINT_RENDER_SCALE_QUALITY"] = (
        "1"  # Linear scaling for better visuals on GPU
    )
    os.environ["SDL_VIDEO_GLES2"] = "1"
    print("GPU detected: Enabling SDL2 GLES2 hardware acceleration.")
else:
    print("No GPU detected: Falling back to software/standard rendering.")

try:
    # Use DOUBLEBUF and HWSURFACE (hints to SDL2 to use the GPU/video memory if available)
    screen = pygame.display.set_mode(
        (1920, 1080), pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE
    )
except pygame.error:
    # Final fallback to dummy
    try:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        pygame.display.init()
        screen = pygame.display.set_mode((1, 1))
    except pygame.error:
        print("Fatal error: Could not initialize display.")
        sys.exit(1)
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


def select_input_device(log):
    """Return (index, info) of the capture device, or (None, None).

    Order: index from the orchestrator / config, system default input, then the
    first device that has inputs.
    """
    if DEVICE_INDEX is not None and isinstance(DEVICE_INDEX, int):
        try:
            info = p.get_device_info_by_index(DEVICE_INDEX)
            if info.get("maxInputChannels", 0) > 0:
                log(f"Using configured audio device ({DEVICE_INDEX}): '{info.get('name')}'")
                return DEVICE_INDEX, info
            log(f"Warning: Configured device {DEVICE_INDEX} has no input channels. Probing alternatives...")
        except Exception as e:
            log(f"Warning: Primary device {DEVICE_INDEX} check failed: {e}. Searching...")

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
band_indices = []  # Filled by build_rate_tables()
_vlf_mask = _nyquist_mask = None


def build_rate_tables(sample_rate):
    """FFT bin tables that depend on the sample rate (rebuilt when the mic opens)."""
    global band_indices, _vlf_mask, _nyquist_mask
    freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / sample_rate)
    band_indices = []
    for i in range(NUM_BANDS):
        indices = np.where((freqs >= band_edges[i]) & (freqs < band_edges[i + 1]))[0]
        # Robustness: If range is too narrow for this FFT resolution, take the nearest bin
        if len(indices) == 0:
            indices = np.array([np.argmin(np.abs(freqs - band_centers[i]))])
        band_indices.append(indices)
    _vlf_mask = freqs < 1  # DC/VLF removal mask
    _nyquist_mask = freqs > 22500  # Nyquist cleanup mask


build_rate_tables(SAMPLE_RATE)

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
analyzer_height = screen.get_height() // 2 + 80
analyzer_y_bottom = screen.get_height() - 150
bar_spacing = 3  # Tighter spacing for higher density
total_bars_width = screen.get_width() - 100
bar_width = max(1, (total_bars_width // NUM_BANDS) - bar_spacing)
start_x = (screen.get_width() - (NUM_BANDS * (bar_width + bar_spacing))) // 2
curve_x = [start_x + i * (bar_width + bar_spacing) + (bar_width // 2) for i in range(NUM_BANDS)]

# Bars are drawn opaque on the black background with their colors already scaled
# by the old alpha (140/255): same look as alpha blending, without blending a full
# screen surface every frame.
BAR_ALPHA = 140 / 255.0
PEAK_BAR_COLOR = (234, 234, 234)  # White at alpha 230 over the glow
GLOW_COLOR = (40, 40, 40)  # White at alpha 40 over black

font_extra_large = pygame.font.Font(None, 220)
font_medium = pygame.font.Font(None, 80)
font_tiny = pygame.font.Font(None, 25)

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


def build_static_layer():
    """Draw once everything that never moves: frequency labels and range legend."""
    layer = pygame.Surface(screen.get_size()).convert()
    layer.fill((0, 0, 0))

    # Labels for key frequencies
    for f in [60, 250, 1000, 4000, 16000]:
        if MIN_FREQ <= f <= MAX_FREQ:
            label_x = _freq_to_x(f)
            label_text = f"{int(f / 1000)}k" if f >= 1000 else f"{f}"
            label_surface = font_tiny.render(label_text, True, (130, 150, 130))
            layer.blit(label_surface, label_surface.get_rect(midtop=(label_x, analyzer_y_bottom + 15)))
            pygame.draw.line(
                layer, (60, 80, 60),
                (label_x, analyzer_y_bottom + 2), (label_x, analyzer_y_bottom + 10), 1,
            )

    # Multi-level range labels
    for rng in FREQ_RANGES:
        if rng["max"] >= MIN_FREQ and rng["min"] <= MAX_FREQ:
            x_start = _freq_to_x(max(rng["min"], MIN_FREQ))
            x_end = _freq_to_x(min(rng["max"], MAX_FREQ))
            level_y = analyzer_y_bottom + 50 + (rng["level"] * 40)
            line_color = rng["color"]
            pygame.draw.line(layer, line_color, (x_start + 2, level_y), (x_end - 2, level_y), 2)
            pygame.draw.line(layer, line_color, (x_start + 2, level_y - 5), (x_start + 2, level_y + 5), 1)
            pygame.draw.line(layer, line_color, (x_end - 2, level_y - 5), (x_end - 2, level_y + 5), 1)
            range_surface = font_tiny.render(rng["name"], True, line_color)
            range_rect = range_surface.get_rect(center=((x_start + x_end) // 2, level_y + 15))
            # Only draw if there is space
            if x_end - x_start > range_rect.width:
                layer.blit(range_surface, range_rect)
    return layer


perf.mark("display_ready")  # PERF
static_layer = build_static_layer()
slope_info = f"{SLOPE_DB_PER_OCTAVE}dB SL" if SLOPE_DB_PER_OCTAVE != 0 else "RAW"
badge_surface = font_tiny.render(f"FAST RESPONSE | SLOPE: {slope_info}", True, (110, 135, 110))

# --- MICROPHONE STREAM ---
stream = None
meter = None
last_open_attempt = -OPEN_RETRY_S
last_open_error = None
open_attempts = 0


def open_stream():
    """Open the mic and (re)build everything that depends on its format.

    Returns the stream, or None (the caller retries every OPEN_RETRY_S seconds:
    the orchestrator may still hold the mic for a moment after a mode switch).
    """
    global CHANNELS, SAMPLE_RATE, meter, last_open_error, open_attempts
    open_attempts += 1
    verbose = open_attempts == 1
    try:
        index, info = select_input_device(print if verbose else (lambda *_: None))
        if info is None:
            raise RuntimeError("No suitable audio input devices found with capture capabilities.")
        # Some I2S drivers only accept 2 channels for a mono mic: one is picked later.
        CHANNELS, SAMPLE_RATE = dsp.input_format(info)
        new_stream = p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
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

    print(f"Opening stream: {CHANNELS} channel(s), {SAMPLE_RATE}Hz, index {index}, "
          f"using channel {MIC_CHANNEL if CHANNELS > 1 else 0}")
    last_open_error = None
    meter = dsp.LevelMeter(SAMPLE_RATE, CALIBRATION_OFFSET, NOISE_FLOOR)
    build_rate_tables(SAMPLE_RATE)
    audio_buffer[:] = 0
    return new_stream


def close_stream(s):
    try:
        s.stop_stream()
        s.close()
    except Exception as e:
        print(f"Closing the audio stream failed: {e}")


running = True
clock = pygame.time.Clock()
perf.mark("loop_ready")  # PERF

last_ui_update_time = time.time()
last_displayed_dba = 0
last_displayed_dbz = 0
last_level_send = time.time()
frame_count = 0


def signal_handler(sig, frame):
    global running
    print("Interrupt received, shutting down audio mode...")
    running = False


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

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

    # --- MICROPHONE: (re)open if needed, then read one chunk ---
    if stream is None and now - last_open_attempt >= OPEN_RETRY_S:
        last_open_attempt = now
        stream = open_stream()
        if stream is not None:
            last_level_send = now

    new_data = None
    if stream is not None:
        try:
            # Blocks until CHUNK_READ frames are there: this paces the loop (~31 Hz)
            raw_data = stream.read(CHUNK_READ, exception_on_overflow=False)
            new_data = dsp.pick_channel(raw_data, CHANNELS, MIC_CHANNEL)
        except Exception as e:
            # Don't quit (the watchdog would blank the screen): reopen the mic instead.
            print(f"Audio read error: {e}. Reopening the microphone...")
            close_stream(stream)
            stream = None
            meter = None
            last_open_attempt = now

    perf.frames.lap("mic_wait")  # PERF
    # Static layer = black background + legend (a plain copy, no blending)
    if new_data is not None:
        screen.blit(static_layer, (0, 0))
    else:
        screen.fill((0, 0, 0))

    perf.frames.lap("clear")  # PERF
    # --- UPDATE CPU STATS (1Hz) ---
    if now - last_stats_update >= 1.0:
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

    # Render minimalist CPU stats (top-left, discreet)
    stats_color = (110, 130, 110)  # Subtle technical green
    if cpu_temp > 75:
        stats_color = (220, 80, 80)  # Warning red for high temp
    stats_text = f"CPU {int(cpu_usage)}%  |  {cpu_temp:.1f}°C"
    screen.blit(render_text(font_tiny, stats_text, stats_color), (50, 50))

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
            fft_mag = np.abs(fft_complex)

            for i, indices in enumerate(band_indices):
                # Get average magnitude for this band
                mag = np.mean(fft_mag[indices])
                # 1. Base dB
                db_val = 20 * np.log10(max(1e-9, mag)) + CALIBRATION_OFFSET
                # 2. Add visual tilt
                db_val += visual_tilt[i]
                # 3. Add HF Boost for air frequencies (>10kHz)
                if band_centers[i] > 10000:
                    db_val += HIGH_FREQ_BOOST
                # Normalize to the MIN_DB..MAX_DB window
                norm_val = (db_val - MIN_DB) / (MAX_DB - MIN_DB)
                current_bar_targets[i] = np.clip(norm_val, 0, 1)
        frame_count += 1

        # Apply visual smoothing (fast rise, slow decay)
        max_energy_idx = -1
        max_energy_val = -1

        for i in range(NUM_BANDS):
            if current_bar_targets[i] > bar_heights[i]:
                # Interpolated smoothing for the rise
                bar_heights[i] = bar_heights[i] + RISE_SMOOTHING * (
                    current_bar_targets[i] - bar_heights[i]
                )
            else:
                bar_heights[i] *= SMOOTHING_FACTOR  # Exponential decay

            # Dynamic Noise Gate (pre-computed, compensates for tilt + HF boost)
            if bar_heights[i] < noise_gate_thresholds[i]:
                bar_heights[i] = 0

            # Identify dominant frequency (Peak highlight)
            if bar_heights[i] > max_energy_val and bar_heights[i] > 0.15:
                max_energy_val = bar_heights[i]
                max_energy_idx = i

            # Professional Unity-style peak handling (Gravity based)
            if bar_heights[i] > peak_pos[i]:
                peak_pos[i] = bar_heights[i]
                peak_vel[i] = 0
            else:
                peak_vel[i] = min(PEAK_MAX_SPEED, peak_vel[i] + PEAK_GRAVITY)
                peak_pos[i] -= peak_vel[i]
                if peak_pos[i] < bar_heights[i]:
                    peak_pos[i] = bar_heights[i]
                    peak_vel[i] = 0

        # --- CURVE CALCULATION ---
        # A second, even smoother buffer for the "Professional Curve"
        curve_points = []
        for i in range(NUM_BANDS):
            curve_heights[i] = curve_heights[i] * CURVE_SMOOTHING + bar_heights[
                i
            ] * (1 - CURVE_SMOOTHING)
            curve_points.append((curve_x[i], analyzer_y_bottom - curve_heights[i] * analyzer_height))

        # --- DRAWING PASS (all opaque, straight onto the screen) ---

        perf.frames.lap("dsp")  # PERF
        # 1. Subtle glow behind the dominant bar (drawn first, so bars stay on top)
        if max_energy_idx >= 0:
            h = int(bar_heights[max_energy_idx] * analyzer_height)
            x = start_x + max_energy_idx * (bar_width + bar_spacing)
            pygame.draw.rect(
                screen, GLOW_COLOR,
                (x - 4, analyzer_y_bottom - h - 10, bar_width + 8, h + 10),
                border_radius=8,
            )

        # 2. Bars, then the sharp peak indicators above them
        for i in range(NUM_BANDS):
            h = int(bar_heights[i] * analyzer_height)
            ph = int(peak_pos[i] * analyzer_height)
            x = start_x + i * (bar_width + bar_spacing)
            is_peak = i == max_energy_idx

            if h > 4:
                if is_peak:
                    color = PEAK_BAR_COLOR
                else:
                    # Bar brightness based on height, pre-multiplied by the bar alpha
                    k = (0.6 + 0.4 * bar_heights[i]) * BAR_ALPHA
                    c = band_colors[i]
                    color = (int(c[0] * k), int(c[1] * k), int(c[2] * k))
                pygame.draw.rect(
                    screen, color, (x, analyzer_y_bottom - h, bar_width, h), border_radius=4
                )

            # Skip the indicator for the dominant peak bar
            if not is_peak and ph > 5:
                pygame.draw.rect(
                    screen,
                    (255, 255, 255),
                    (x, analyzer_y_bottom - ph - 3, bar_width, 2),
                    border_radius=1,
                )

        # 3. The "High-Fidelity Curve" line (colors synchronized with the legend)
        if len(curve_points) > 2:
            for i in range(len(curve_points) - 1):
                p1 = curve_points[i]
                p2 = curve_points[i + 1]
                c = band_colors[i]
                # Draw subtle anti-aliased segment
                pygame.draw.aaline(screen, c, p1, p2)
                # Draw a second slightly offset line for a 'thickness' feel without losing AA
                pygame.draw.aaline(screen, c, (p1[0], p1[1] - 1), (p2[0], p2[1] - 1))

        # Determine color based on threshold (Evaluate using dBA, professional standard)
        text_color = (180, 255, 180)  # Brighter Green
        if last_displayed_dba >= THRESHOLD_ERROR:
            text_color = (255, 80, 80)  # Vivid Red
        elif last_displayed_dba >= THRESHOLD_WARNING:
            text_color = (255, 180, 50)  # Vivid Orange

        # Render dBA text (Large, Integer only) — cached until the value changes
        dba_text = render_text(font_extra_large, f"{int(round(last_displayed_dba))} dBA", text_color)
        dba_rect = dba_text.get_rect()
        dba_rect.topright = (screen.get_width() - 50, 50)
        screen.blit(dba_text, dba_rect)

        # Render dBZ text (Medium)
        dbz_text = render_text(font_medium, f"{last_displayed_dbz:.1f} dBZ", (180, 210, 180))
        dbz_rect = dbz_text.get_rect()
        dbz_rect.topright = (screen.get_width() - 50, dba_rect.bottom + 10)
        screen.blit(dbz_text, dbz_rect)

        # "FAST RESPONSE" indicator (Tiny status info, pre-rendered)
        badge_rect = badge_surface.get_rect()
        badge_rect.topright = (screen.get_width() - 50, dbz_rect.bottom + 10)
        screen.blit(badge_surface, badge_rect)
    else:
        # 1. Draw a stylized "No Mic" icon in the top-left corner
        icon_surface = pygame.Surface((100, 100), pygame.SRCALPHA)
        # Draw Mic Body (Rounded Rect)
        pygame.draw.rect(
            icon_surface, (100, 100, 100), (35, 20, 30, 45), border_radius=15
        )
        # Draw Mic Stand
        pygame.draw.line(icon_surface, (100, 100, 100), (50, 65), (50, 80), 3)
        pygame.draw.line(icon_surface, (100, 100, 100), (35, 80), (65, 80), 3)
        # Draw the Warning Slash (Red)
        pygame.draw.line(icon_surface, (255, 50, 50), (20, 20), (80, 80), 6)

        screen.blit(icon_surface, (50, 50))

        # 2. Render localized error message
        error_text = render_text(
            font_medium, "Waiting for I2S Microphone Signal...", (120, 120, 120)
        )
        text_rect = error_text.get_rect(
            center=(screen.get_width() / 2, screen.get_height() / 2)
        )
        screen.blit(error_text, text_rect)

    perf.frames.lap("draw")  # PERF
    pygame.display.flip()
    perf.frames.lap("flip")  # PERF
    # With a mic, the blocking stream.read() already paces the loop (~31 Hz), so this
    # never waits. Without one, it keeps the "waiting" screen from spinning the CPU.
    clock.tick(60 if stream is not None else 10)
    perf.frames.lap("tick")  # PERF
    perf.frames.end()  # PERF

if stream:
    close_stream(stream)
p.terminate()
pygame.quit()
