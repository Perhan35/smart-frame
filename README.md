# SmartFrame

Raspberry Pi Zero 2 powered smart display with two modes: a Magic Mirror (Chromium kiosk via MagicMirror²) and a real-time audio spectrum analyzer & decibel meter (pygame + PyAudio). Modes are switched remotely via MQTT, with native Home Assistant integration.
Designed for a 14" matte LCD to mimic an e-ink aesthetic.

## Hardware
- **Raspberry Pi**: Zero 2 WH or newer.
- **Screen**: 13.3" or 14" IPS Matte LCD (1920x1080) with HDMI controller board.
- **Audio**: INMP441 Microphone (I2S) for digital audio capture.

## Linux Prerequisites (Raspberry Pi OS)
To run Pygame (for the audio spectrogram) and Chromium (for MagicMirror):

1. Install OS dependencies:
```bash
sudo apt update
sudo apt install python3-pyaudio chromium-browser python3-pip x11-xserver-utils
```

2. Enable the I2S microphone modules in `/boot/config.txt` (or `/boot/firmware/config.txt`):
```ini
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard
```
*(A reboot is required)*.

## SmartFrame Installation

```bash
git clone https://github.com/YOUR_PROFILE/smart-frame.git
cd smart-frame
pip3 install -r requirements.txt
```

*(If you get a "externally-managed-environment" error, use `pip3 install -r requirements.txt --break-system-packages`)*.

## Configuration
Edit the `config.yaml` file to set your preferences:
- **MQTT**: Set up your broker IP, port, and credentials.
- **MagicMirror**: Set the URL corresponding to your local MagicMirror instance.
- **Audio Mode**: 
  - `device_index`: Specific ALSA microphone index ID (if required).
  - `threshold_db_warning`: Volume (in dB) where the dB text will turn yellow/orange (default: 60).
  - `threshold_db_error`: Volume (in dB) where the dB text will turn red (default: 85).

## Local Development (macOS)

To run the audio mode locally on macOS for testing (mirror mode requires Chromium kiosk and is Pi-only):

**1. Run setup (first time only):**
```bash
./scripts/setup_dev.sh
```
This installs Homebrew dependencies (`portaudio`, SDL2, `pkg-config`) and creates a `.venv` with dev-compatible packages.

**2. Run:**
```bash
./run_dev.sh           # audio spectrum analyzer (windowed, default)
./run_dev.sh main      # full orchestrator in offline mode (no MQTT required)
```

The audio mode opens an 800×600 window. Press `Esc` to quit.

> **Note:** `requirements.dev.txt` uses `pygame==2.6.1` instead of `2.5.2` since 2.5.2 has no pre-built wheel for Python 3.13. The Pi deployment still uses `requirements.txt`.

## Usage

Manual startup:
```bash
python3 main.py
```

### Service Installation (Start on Boot)
First, edit the systemd service file `scripts/smartframe.service` if your home directory or X11/Wayland session is not standard (Raspberry Pi defaults to the `pi` user).

```bash
sudo cp scripts/smartframe.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable smartframe.service
sudo systemctl start smartframe.service
```

## Home Assistant Integration
Your Home Assistant instance can switch between the modes (or turn off the screen) via MQTT. The orchestrator listens for commands on the `smartframe/set_mode` topic and publishes its current mode to the `smartframe/mode_state` topic.

Supported payloads:
- `audio`
- `mirror`
- `off` (Turns off the LCD screen via `vcgencmd`)

### Home Assistant `configuration.yaml` Example
The best way to integrate this is by creating an MQTT Select entity. Add this to your Home Assistant `configuration.yaml`:

```yaml
mqtt:
  select:
    - name: "SmartFrame Mode"
      unique_id: "smartframe_mode_select"
      command_topic: "smartframe/set_mode"
      state_topic: "smartframe/mode_state"
      options:
        - "audio"
        - "mirror"
        - "off"
      icon: mdi:monitor-dashboard
```

Once added and Home Assistant is restarted, you will have a dropdown entity (`select.smartframe_mode`) to effortlessly switch between modes or turn the screen off!
