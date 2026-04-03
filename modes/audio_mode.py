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

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

# Config with env-var fallbacks (passed from orchestrator for speed)
DEVICE_INDEX = None
THRESHOLD_WARNING = 60
THRESHOLD_ERROR = 85
CALIBRATION_OFFSET = 0

try:
    if os.path.exists(config_path):
        with open(config_path, "r") as file:
            config = yaml.safe_load(file)
            audio_config = config.get("audio", {})
            THRESHOLD_WARNING = audio_config.get("threshold_db_warning", 60)
            THRESHOLD_ERROR = audio_config.get("threshold_db_error", 85)
            CALIBRATION_OFFSET = audio_config.get("calibration_offset_db", 0)
            DEVICE_INDEX = audio_config.get("device_index")
except Exception:
    pass

DEVICE_INDEX_ENV = os.environ.get('SMARTFRAME_AUDIO_DEVICE')
if DEVICE_INDEX_ENV and DEVICE_INDEX_ENV != 'None' and DEVICE_INDEX_ENV != '':
    DEVICE_INDEX = int(DEVICE_INDEX_ENV)

# Audio Configuration
CHUNK = 8192             # High-fidelity FFT resolution for extreme low-end precision
FORMAT = pyaudio.paInt16
CHANNELS = 1


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
    pygame.init()

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
    os.environ["SDL_HINT_RENDER_SCALE_QUALITY"] = "1"  # Linear scaling for better visuals on GPU
    os.environ["SDL_VIDEO_GLES2"] = "1"
    print("GPU detected: Enabling SDL2 GLES2 hardware acceleration.")
else:
    print("No GPU detected: Falling back to software/standard rendering.")

try:
    # Use DOUBLEBUF and HWSURFACE (hints to SDL2 to use the GPU/video memory if available)
    screen = pygame.display.set_mode((1920, 1080), pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE)
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

# PyAudio initialization and stream configuration
with ignore_stderr():
    p = pyaudio.PyAudio()

try:
    # 1. Robust Device Selection
    device_info = None
    selected_index = None

    if DEVICE_INDEX is not None and isinstance(DEVICE_INDEX, int):
        try:
            device_info = p.get_device_info_by_index(DEVICE_INDEX)
            selected_index = DEVICE_INDEX
            print(
                f"Using configured audio device: {device_info.get('name')} (index {selected_index})"
            )
        except Exception:
            print(
                f"Warning: Configured device index {DEVICE_INDEX} not found, searching for alternatives..."
            )

    if not device_info:
        try:
            device_info = p.get_default_input_device_info()
            selected_index = device_info.get("index")
            print(
                f"Using default audio device: {device_info.get('name')} (index {selected_index})"
            )
        except Exception:
            print(
                "No default input device found, searching for any available capture device..."
            )
            for i in range(p.get_device_count()):
                try:
                    info = p.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) > 0:
                        device_info = info
                        selected_index = i
                        print(
                            f"Found alternative capture device: {device_info.get('name')} (index {selected_index})"
                        )
                        break
                except Exception:
                    continue

    if not device_info or device_info.get("maxInputChannels", 0) == 0:
        raise RuntimeError(
            "No suitable audio input devices found with capture capabilities."
        )

    # 2. Dynamic Parameter Configuration
    # Some devices (like some I2S mappings) only support 2 channels even if the mic is mono.
    # We favor 1 (mono) but use 2 if required.
    max_chans = device_info.get("maxInputChannels", 1)
    CHANNELS = 1 if max_chans == 1 else 2
    SAMPLE_RATE = int(device_info.get("defaultSampleRate", 48000))

    print(
        f"Opening stream: {CHANNELS} channel(s), {SAMPLE_RATE}Hz, index {selected_index}"
    )

    stream_params = {
        "format": FORMAT,
        "channels": CHANNELS,
        "rate": SAMPLE_RATE,
        "input": True,
        "input_device_index": selected_index,
        "frames_per_buffer": CHUNK,
    }

    # A-weighting gain pre-calculation
    def get_a_weighting_gains(rate, chunk):
        freqs = np.fft.rfftfreq(chunk, 1.0 / rate)
        valid_freqs = np.where(freqs == 0, 1e-10, freqs)
        f_sq = valid_freqs**2
        const = (12194**2) * (f_sq**2)
        den = (
            (f_sq + 20.6**2)
            * np.sqrt((f_sq + 107.7**2) * (f_sq + 737.9**2))
            * (f_sq + 12194**2)
        )
        w = const / den
        w *= 1.2589  # Normalize to 0dB at 1kHz
        w[0] = 0.0
        return w

    a_gains = get_a_weighting_gains(SAMPLE_RATE, CHUNK)
    dt = CHUNK / SAMPLE_RATE
    fast_alpha = 1.0 - np.exp(-dt / 0.125)  # 125ms FAST integration time

    stream = p.open(**stream_params)
except Exception as e:
    print(f"Fatal error opening audio stream: {e}")
    print(
        "Ensure the INMP441 I2S microphone is properly connected and configured (ALSA/dtoverlay)."
    )
    stream = None

running = True
clock = pygame.time.Clock()

font_extra_large = pygame.font.Font(None, 220)
font_large = pygame.font.Font(None, 160)
font_medium = pygame.font.Font(None, 80)
font_small = pygame.font.Font(None, 40)
font_tiny = pygame.font.Font(None, 25)

last_ui_update_time = time.time()
last_displayed_dba = 0
last_displayed_dbz = 0
ema_rms_a = 0.0
ema_rms_z = 0.0

# --- SPECTRUM ANALYZER CONFIGURATION ---
NUM_BANDS = 120          # Ultra-high density for 1080p professional display
MIN_FREQ = 1
MAX_FREQ = 24000
MIN_DB = 30
MAX_DB = 95
SMOOTHING_FACTOR = 0.78  # Fast, reactive response
PEAK_GRAVITY = 0.015     # Physical-like peak fall
PEAK_MAX_SPEED = 0.05

# Professional Slope Setting: 0dB (Raw), 3dB (Pink Noise), 4.5dB (Modern Standard)
# A slope of 4.5dB/octave is standard in pro plugins like FabFilter Pro-Q to make a 
# balanced mix look "flat" visually.
SLOPE_DB_PER_OCTAVE = 4.5 

# Frequency Range Definitions for visual labels (Updated for 1Hz - 24kHz)
FREQ_RANGES = [
    {"name": "BASS", "min": 1, "max": 250, "level": 1, "color": (100, 150, 255)},
    {"name": "MIDS", "min": 250, "max": 4000, "level": 1, "color": (150, 255, 150)},
    {"name": "TREBLE", "min": 4000, "max": 24000, "level": 1, "color": (255, 150, 100)},
    
    {"name": "Infra", "min": 1, "max": 20, "level": 0, "color": (60, 100, 200)},
    {"name": "Sub", "min": 20, "max": 60, "level": 0, "color": (80, 120, 220)},
    {"name": "Low", "min": 60, "max": 250, "level": 0, "color": (100, 140, 240)},
    {"name": "Low-Mid", "min": 250, "max": 500, "level": 0, "color": (120, 220, 120)},
    {"name": "Mid", "min": 500, "max": 2000, "level": 0, "color": (140, 240, 140)},
    {"name": "Hi-Mid", "min": 2000, "max": 4000, "level": 0, "color": (160, 255, 160)},
    {"name": "Presence", "min": 4000, "max": 6000, "level": 0, "color": (240, 200, 120)},
    {"name": "Brilliance", "min": 6000, "max": 24000, "level": 0, "color": (240, 150, 80)}
]

def get_log_bands(sample_rate, fft_size, num_bands, min_f, max_f):
    freqs = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
    # Ensure min_f is positive for logspace
    safe_min_f = max(1.0, min_f)
    band_edges = np.logspace(np.log10(safe_min_f), np.log10(max_f), num_bands + 1)
    bands = []
    # Pre-calculate central frequencies for each band for slope calculation
    band_centers = []
    for i in range(num_bands):
        indices = np.where((freqs >= band_edges[i]) & (freqs < band_edges[i+1]))[0]
        bands.append(indices)
        band_centers.append(np.sqrt(band_edges[i] * band_edges[i+1])) # Geometric mean
    return bands, band_edges, band_centers

band_indices, band_edges, band_centers = get_log_bands(SAMPLE_RATE, CHUNK, NUM_BANDS, MIN_FREQ, MAX_FREQ)
bar_heights = np.zeros(NUM_BANDS)
peak_heights = np.zeros(NUM_BANDS)

# Blackman-Harris window for ultra-low spectral leakage (Mastering grade)
fft_window = np.blackman(CHUNK)

# Calculate professional visual tilt based on the requested slope
# We use 1kHz as the zero-crossing reference point
octaves_from_reference = np.log2(np.array(band_centers) / 1000.0)
visual_tilt = octaves_from_reference * SLOPE_DB_PER_OCTAVE

# Store last peak positions and velocities for gravity effect
peak_pos = np.zeros(NUM_BANDS)
peak_vel = np.zeros(NUM_BANDS)


def signal_handler(sig, frame):
    global running
    print("Interrupt received, shutting down audio mode...")
    running = False


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            running = False

    screen.fill((0, 0, 0))  # Black background

    if stream:
        try:
            # Read data from the microphone
            raw_data = stream.read(CHUNK, exception_on_overflow=False)
            data = np.frombuffer(raw_data, dtype=np.int16)

            # If stereo is used, average the channels to mono for DSP
            if CHANNELS > 1:
                data = data.reshape(-1, CHANNELS).mean(axis=1)
            else:
                data = data.astype(np.float32)

            # Apply Hann window to raw data before FFT (Pro standard)
            windowed_data = data * fft_window

            # --- AUDIO DSP BLOCK ---
            # 1. Forward FFT (on windowed data)
            fft_complex = np.fft.rfft(windowed_data)
            freqs = np.fft.rfftfreq(len(data), 1.0 / SAMPLE_RATE)
            
            # 2. DC/VLF Removal (Low-cut at 1Hz instead of 100Hz for user request)
            fft_complex[freqs < 1] = 0
            
            # 3. Z-weighted (Raw) RMS
            # Time-domain approach (Original)
            data_clean = np.fft.irfft(fft_complex)
            z_rms = np.sqrt(np.mean(data_clean**2))

            # [Future reference: Frequency-domain RMS for Zero 2 optimization]
            # z_rms = np.sqrt(np.sum(np.abs(fft_complex)**2) / (len(data)**2))

            # 4. A-Weighted RMS (Original)
            fft_aw = fft_complex * a_gains
            data_aw = np.fft.irfft(fft_aw)
            a_rms = np.sqrt(np.mean(data_aw**2))

            # [Future reference: Frequency-domain A-RMS for Zero 2 optimization]
            # a_rms = np.sqrt(np.sum(np.abs(fft_aw)**2) / (len(data)**2))

            # Fast Integration (EMA)
            if ema_rms_z == 0:
                ema_rms_z = z_rms
                ema_rms_a = a_rms
            else:
                ema_rms_z = ema_rms_z + fast_alpha * (z_rms - ema_rms_z)
                ema_rms_a = ema_rms_a + fast_alpha * (a_rms - ema_rms_a)

            # Log calculation
            db_z = (20 * np.log10(max(1e-9, ema_rms_z))) + CALIBRATION_OFFSET
            db_a = (20 * np.log10(max(1e-9, ema_rms_a))) + CALIBRATION_OFFSET

            # 5. NOISE FLOOR COMPENSATION
            # Aggressive fix for the "34 vs 40" discrepancy in silence.
            # We fade in an 8dB correction for anything below 45dB.
            if db_a < 45:
                correction = 8.0 * (1.0 - (max(30, db_a) - 30) / 15)
                db_a -= correction
                db_z -= correction

            # Keep track of the display value (Peak hold)
            current_time = time.time()
            if db_a > last_displayed_dba:
                last_displayed_dba = db_a
                last_displayed_dbz = db_z
                last_ui_update_time = current_time
            elif current_time - last_ui_update_time >= 0.5:
                last_displayed_dba = db_a
                last_displayed_dbz = db_z
                last_ui_update_time = current_time

            # --- SPECTRUM VISUALIZATION BLOCK ---
            fft_mag = np.abs(fft_complex)
            current_bar_targets = np.zeros(NUM_BANDS)

            for i, indices in enumerate(band_indices):
                if len(indices) > 0:
                    # Get the average magnitude for this band
                    mag = np.mean(fft_mag[indices])
                    # Visual tilt: +4.5dB per octave approx (18dB over ~4 octaves of interest)
                    db_val = 20 * np.log10(max(1e-9, mag)) + CALIBRATION_OFFSET + visual_tilt[i]
                    
                    # Normalize to 0.0 - 1.0 range (Headroom: 15dB to 95dB)
                    norm_val = (db_val - MIN_DB) / (MAX_DB - MIN_DB)
                    current_bar_targets[i] = np.clip(norm_val, 0, 1)

            # Apply visual smoothing (fast rise, slow decay)
            for i in range(NUM_BANDS):
                if current_bar_targets[i] > bar_heights[i]:
                    bar_heights[i] = bar_heights[i] + 0.8 * (current_bar_targets[i] - bar_heights[i]) # Very fast rise
                else:
                    bar_heights[i] *= SMOOTHING_FACTOR      # Exponential decay

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

            # Draw the spectrum analyzer (Full immersive depth)
            analyzer_height = screen.get_height() // 2 + 100
            analyzer_y_bottom = screen.get_height() - 150
            bar_spacing = 2
            total_bars_width = screen.get_width() - 40 
            bar_width = (total_bars_width // NUM_BANDS) - bar_spacing
            start_x = (screen.get_width() - (NUM_BANDS * (bar_width + bar_spacing))) // 2

            for i in range(NUM_BANDS):
                h = int(bar_heights[i] * analyzer_height)
                ph = int(peak_pos[i] * analyzer_height)
                
                x = start_x + i * (bar_width + bar_spacing)
                
                # Base colors for the gradient
                # Slate -> Teal -> Mint
                r_start, g_start, b_start = 50, 80, 150
                r_end, g_end, b_end = 150, 255, 200
                
                # Dynamic color intensity based on bar height
                mix = bar_heights[i]
                r = int(r_start + (r_end - r_start) * mix)
                g = int(g_start + (g_end - g_start) * mix)
                b = int(b_start + (b_end - b_start) * mix)
                
                if h > 2:
                    # Draw a subtle vertical gradient (stacked rects)
                    num_segments = 5
                    for s in range(num_segments):
                        seg_h = h // num_segments
                        seg_y = analyzer_y_bottom - (s + 1) * seg_h
                        brightness = 0.5 + 0.5 * (s / num_segments)
                        seg_color = (int(r * brightness), int(g * brightness), int(b * brightness))
                        pygame.draw.rect(screen, seg_color, (x, seg_y, bar_width, seg_h))
                
                # Draw peak indicator (floating)
                if ph > 5:
                    peak_alpha = int(255 * peak_pos[i])
                    peak_color = (255, 255, 255)
                    pygame.draw.rect(screen, peak_color, (x, analyzer_y_bottom - ph - 2, bar_width, 2))

            # Draw labels for key frequencies
            key_freqs = [60, 250, 1000, 4000, 16000]
            for f in key_freqs:
                if f >= MIN_FREQ and f <= MAX_FREQ:
                    pos_idx = NUM_BANDS * (np.log10(f) - np.log10(max(1.0, MIN_FREQ))) / (np.log10(MAX_FREQ) - np.log10(max(1.0, MIN_FREQ)))
                    label_x = start_x + pos_idx * (bar_width + bar_spacing)
                    
                    label_text = f"{int(f/1000)}k" if f >= 1000 else f"{f}"
                    label_surface = font_tiny.render(label_text, True, (130, 150, 130))
                    label_rect = label_surface.get_rect(midtop=(label_x, analyzer_y_bottom + 15))
                    screen.blit(label_surface, label_rect)
                    pygame.draw.line(screen, (60, 80, 60), (label_x, analyzer_y_bottom + 2), (label_x, analyzer_y_bottom + 10), 1)

            # --- MULTI-LEVEL RANGE LABELS ---
            for rng in FREQ_RANGES:
                if rng["max"] >= MIN_FREQ and rng["min"] <= MAX_FREQ:
                    # Calculate pixel positions for start/end
                    f_start = max(rng["min"], MIN_FREQ)
                    f_end = min(rng["max"], MAX_FREQ)
                    
                    p_start = NUM_BANDS * (np.log10(f_start) - np.log10(max(1.0, MIN_FREQ))) / (np.log10(MAX_FREQ) - np.log10(max(1.0, MIN_FREQ)))
                    p_end = NUM_BANDS * (np.log10(f_end) - np.log10(max(1.0, MIN_FREQ))) / (np.log10(MAX_FREQ) - np.log10(max(1.0, MIN_FREQ)))
                    
                    x_start = start_x + p_start * (bar_width + bar_spacing)
                    x_end = start_x + p_end * (bar_width + bar_spacing)
                    
                    level_y = analyzer_y_bottom + 50 + (rng["level"] * 40)
                    
                    # Draw a colored line for the range
                    line_color = rng["color"]
                    pygame.draw.line(screen, line_color, (x_start + 2, level_y), (x_end - 2, level_y), 2)
                    pygame.draw.line(screen, line_color, (x_start + 2, level_y - 5), (x_start + 2, level_y + 5), 1)
                    pygame.draw.line(screen, line_color, (x_end - 2, level_y - 5), (x_end - 2, level_y + 5), 1)
                    
                    # Render Range Name
                    range_surface = font_tiny.render(rng["name"], True, line_color)
                    range_rect = range_surface.get_rect(center=((x_start + x_end) // 2, level_y + 15))
                    # Only draw if there is space
                    if x_end - x_start > range_rect.width:
                        screen.blit(range_surface, range_rect)

            # Determine color based on threshold (Evaluate using dBA, professional standard)
            text_color = (180, 255, 180)  # Brighter Green
            if last_displayed_dba >= THRESHOLD_ERROR:
                text_color = (255, 80, 80)   # Vivid Red
            elif last_displayed_dba >= THRESHOLD_WARNING:
                text_color = (255, 180, 50)  # Vivid Orange

            # Render dBA text (Large)
            dba_text = font_extra_large.render(
                f"{last_displayed_dba:.1f} dBA", True, text_color
            )
            dba_rect = dba_text.get_rect()
            dba_rect.topright = (screen.get_width() - 50, 50)
            screen.blit(dba_text, dba_rect)

            # Render dBZ text (Medium)
            dbz_text = font_medium.render(
                f"{last_displayed_dbz:.1f} dBZ", True, (180, 210, 180)
            )  # Green-compensated grey
            dbz_rect = dbz_text.get_rect()
            dbz_rect.topright = (screen.get_width() - 50, dba_rect.bottom + 10)
            screen.blit(dbz_text, dbz_rect)

            # Render "FAST RESPONSE" indicator (Tiny status info)
            slope_info = f"{SLOPE_DB_PER_OCTAVE}dB SL" if SLOPE_DB_PER_OCTAVE != 0 else "RAW"
            badge_text = font_tiny.render(
                f"FAST RESPONSE | SLOPE: {slope_info}", True, (110, 135, 110)
            )  # Compensated subtitle
            badge_rect = badge_text.get_rect()
            badge_rect.topright = (screen.get_width() - 50, dbz_rect.bottom + 10)
            screen.blit(badge_text, badge_rect)

        except Exception as e:
            print(f"Audio read error: {e}")
            break
    else:
        # 1. Draw a stylized "No Mic" icon in the top-left corner
        icon_surface = pygame.Surface((100, 100), pygame.SRCALPHA)
        # Draw Mic Body (Rounded Rect)
        pygame.draw.rect(icon_surface, (100, 100, 100), (35, 20, 30, 45), border_radius=15)
        # Draw Mic Stand
        pygame.draw.line(icon_surface, (100, 100, 100), (50, 65), (50, 80), 3)
        pygame.draw.line(icon_surface, (100, 100, 100), (35, 80), (65, 80), 3)
        # Draw the Warning Slash (Red)
        pygame.draw.line(icon_surface, (255, 50, 50), (20, 20), (80, 80), 6)
        
        screen.blit(icon_surface, (50, 50))
        
        # 2. Render localized error message
        error_text = font_medium.render(
            "Waiting for I2S Microphone Signal...", True, (120, 120, 120)
        )
        text_rect = error_text.get_rect(
            center=(screen.get_width() / 2, screen.get_height() / 2)
        )
        screen.blit(error_text, text_rect)

    pygame.display.flip()
    # Limit framerate: 60 FPS (Original) for smooth animations
    clock.tick(60)
    # [Future reference: Lower to 30 to save 50% CPU cycles on Pi Zero 2 if stuttering occurs]
    # clock.tick(30)

if stream:
    stream.stop_stream()
    stream.close()
p.terminate()
pygame.quit()
