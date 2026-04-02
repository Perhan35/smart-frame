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
try:
    with open(config_path, "r") as file:
        config = yaml.safe_load(file)
except Exception:
    config = {}

audio_config = config.get("audio", {})
DEVICE_INDEX = audio_config.get("device_index")
THRESHOLD_WARNING = audio_config.get("threshold_db_warning", 60)
THRESHOLD_ERROR = audio_config.get("threshold_db_error", 85)
CALIBRATION_OFFSET = audio_config.get("calibration_offset_db", 0)

# Audio Configuration
CHUNK = 2048
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

# Robust mouse hiding for various SDL/Wayland/X11 backends
os.environ["SDL_VIDEO_WAYLAND_HIDECURSOR"] = "1"
os.environ["SDL_MOUSE_RELATIVE"] = "1"
os.environ["XCURSOR_SIZE"] = "0"
os.environ["COG_PLATFORM_FDO_SHOW_CURSOR"] = "0"

try:
    screen = pygame.display.set_mode((1920, 1080), pygame.FULLSCREEN)
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

            # --- IMPROVED DSP BLOCK ---
            # 1. DC Offset Removal (Mean Subtraction)
            data -= np.mean(data)

            # 2. Software High-Pass Filter (Simple 100Hz suppression)
            # This helps match the Watch which likely ignores room rumble
            # and it will fix the "51 dBZ in silence" issue.
            fft_complex = np.fft.rfft(data)
            freqs = np.fft.rfftfreq(len(data), 1.0 / SAMPLE_RATE)
            fft_complex[freqs < 100] = 0  # Kill everything below 100Hz

            # 3. Z-weighted (Raw) RMS
            data_clean = np.fft.irfft(fft_complex)
            z_rms = np.sqrt(np.mean(data_clean**2))

            # 4. A-Weighted RMS (using our pre-calculated gains)
            # a_gains is now calculated once at the top of the script
            # because CHUNK is constant again.
            fft_aw = fft_complex * a_gains
            data_aw = np.fft.irfft(fft_aw)
            a_rms = np.sqrt(np.mean(data_aw**2))

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

            # Spectrum analyzer (basic FFT for visuals, unweighted for full bass visibility)
            fft_data_vis = np.abs(fft_complex)[:100]  # Keep the lower/mid frequencies

            # Draw the spectrum analyzer
            num_bars = len(fft_data_vis)
            bar_width = screen.get_width() // num_bars

            for i, val in enumerate(fft_data_vis):
                # Scale the value vertically
                h = min(screen.get_height() - 150, int(val / 500))
                # Draw a white bar (e-ink aesthetic)
                pygame.draw.rect(
                    screen,
                    (220, 220, 220),
                    (i * bar_width, screen.get_height() - h, bar_width - 5, h),
                )

            # Determine color based on threshold (Evaluate using dBA, professional standard)
            text_color = (144, 238, 144)  # Light Green
            if last_displayed_dba >= THRESHOLD_ERROR:
                text_color = (255, 0, 0)  # Red
            elif last_displayed_dba >= THRESHOLD_WARNING:
                text_color = (255, 165, 0)  # Orange

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
            badge_text = font_tiny.render(
                "FAST RESPONSE", True, (110, 135, 110)
            )  # Compensated subtitle
            badge_rect = badge_text.get_rect()
            badge_rect.topright = (screen.get_width() - 50, dbz_rect.bottom + 10)
            screen.blit(badge_text, badge_rect)

        except Exception as e:
            print(f"Audio read error: {e}")
            break
    else:
        # Error message if microphone is unresponsive, keeping dark background to avoid screen flash
        error_text = font_medium.render(
            "Waiting for I2S Microphone Signal...", True, (150, 150, 150)
        )
        text_rect = error_text.get_rect(
            center=(screen.get_width() / 2, screen.get_height() / 2)
        )
        screen.blit(error_text, text_rect)

    pygame.display.flip()
    # Limit framerate: screen can easily handle 60 FPS, but 30 or 60 is perfectly fine
    clock.tick(60)

if stream:
    stream.stop_stream()
    stream.close()
p.terminate()
pygame.quit()
