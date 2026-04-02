import pygame
import numpy as np
import pyaudio
import sys
import os
import yaml
import ctypes

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
try:
    with open(config_path, 'r') as file:
        config = yaml.safe_load(file)
except Exception:
    config = {}

audio_config = config.get('audio', {})
DEVICE_INDEX = audio_config.get('device_index')
THRESHOLD_WARNING = audio_config.get('threshold_db_warning', 60)
THRESHOLD_ERROR = audio_config.get('threshold_db_error', 85)

# Audio Configuration
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1

print("Initializing Audio Mode...")
pygame.init()

# Display initialization (1080p full screen, intended for a 14" LCD)
if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
    # Headless/Console mode detected, force KMSDRM
    os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
    # Ensure it uses the primary DRM device
    if not os.environ.get("SDL_DRM_DEVICE"):
        os.environ["SDL_DRM_DEVICE"] = "/dev/dri/card0"

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

# Hide the mouse cursor
try:
    pygame.mouse.set_visible(False)
except pygame.error:
    pass

# ALSA error suppression
ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
def py_error_handler(filename, line, function, err, fmt):
    pass
c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)

try:
    asound = ctypes.cdll.LoadLibrary('libasound.so.2')
    asound.snd_lib_error_set_handler(c_error_handler)
except OSError:
    try:
        asound = ctypes.cdll.LoadLibrary('libasound.so')
        asound.snd_lib_error_set_handler(c_error_handler)
    except OSError:
        pass

# PyAudio initialization
p = pyaudio.PyAudio()

try:
    stream_params = {
        'format': FORMAT,
        'channels': CHANNELS,
        'input': True,
        'frames_per_buffer': CHUNK
    }
    if DEVICE_INDEX is not None and isinstance(DEVICE_INDEX, int):
        stream_params['input_device_index'] = DEVICE_INDEX
        device_info = p.get_device_info_by_index(DEVICE_INDEX)
    else:
        try:
            device_info = p.get_default_input_device_info()
        except IOError:
            # Fallback if no default input is found
            device_info = {'defaultSampleRate': 48000}
            
    stream_params['rate'] = int(device_info['defaultSampleRate'])
        
    stream = p.open(**stream_params)
except Exception as e:
    print(f"Error opening audio stream: {e}")
    print("Ensure the INMP441 I2S microphone is properly connected and configured (ALSA/dtoverlay).")
    stream = None

running = True
clock = pygame.time.Clock()

# Use default font, change size
font = pygame.font.SysFont(None, 64)

while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            running = False

    screen.fill((0, 0, 0)) # Black background
    
    if stream:
        try:
            # Read data from the I2S microphone
            data = np.frombuffer(stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16)
            
            # RMS calculation for volume (~ simplified dBA estimation)
            rms = np.sqrt(np.mean(data.astype(np.float32)**2))
            
            # Only calculate target dB if there is sound
            db = 20 * np.log10(rms) if rms > 0 else 0
            
            # Spectrum analyzer (basic FFT)
            fft_data = np.abs(np.fft.rfft(data))
            fft_data = fft_data[:100] # Keep the lower/mid frequencies
            
            # Draw the spectrum analyzer
            num_bars = len(fft_data)
            bar_width = screen.get_width() // num_bars
            
            for i, val in enumerate(fft_data):
                # Scale the value vertically
                h = min(screen.get_height() - 150, int(val / 500)) 
                # Draw a white bar (e-ink aesthetic)
                pygame.draw.rect(screen, (220, 220, 220), 
                                 (i * bar_width, screen.get_height() - h, bar_width - 5, h))
            
            # Determine color based on threshold
            text_color = (255, 255, 255) # White
            if db >= THRESHOLD_ERROR:
                text_color = (255, 0, 0) # Red
            elif db >= THRESHOLD_WARNING:
                text_color = (255, 165, 0) # Orange
                
            # Display the volume level (dB)
            db_text = font.render(f"Volume : {db:.1f} dB", True, text_color)
            screen.blit(db_text, (50, 50))
                
        except Exception as e:
            print(f"Audio read error: {e}")
            break
    else:
        # Error message if microphone is unresponsive, keeping dark background to avoid screen flash
        error_text = font.render("Waiting for I2S Microphone Signal...", True, (150, 150, 150))
        text_rect = error_text.get_rect(center=(screen.get_width()/2, screen.get_height()/2))
        screen.blit(error_text, text_rect)

    pygame.display.flip()
    # Limit framerate: screen can easily handle 60 FPS, but 30 or 60 is perfectly fine
    clock.tick(60) 

if stream:
    stream.stop_stream()
    stream.close()
p.terminate()
pygame.quit()
