# SmartFrame

Raspberry Pi Zero 2 powered smart display with two modes: a Magic Mirror (Chromium kiosk via MagicMirror²) and a real-time audio spectrum analyzer & decibel meter (pygame + PyAudio). Modes are switched remotely via MQTT, with native Home Assistant integration.
Designed for a 14" matte LCD to mimic an e-ink aesthetic.

## Hardware
- **Raspberry Pi**: Zero 2 WH or newer.
- **Screen**: 13.3" or 14" IPS Matte LCD (1920x1080) with HDMI controller board.
- **Audio**: INMP441 Microphone (I2S) for digital audio capture.

## Linux Prerequisites (Raspberry Pi OS)

1. Enable the I2S microphone modules in `/boot/config.txt` (or `/boot/firmware/config.txt`):
```ini
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard
```
*(A reboot is required)*.

## SmartFrame Installation

```bash
git clone https://github.com/Perhan35/smart-frame.git
cd smart-frame
./scripts/setup_pi.sh
```

On some systems (Raspberry Pi Zero 2) the RAM is too low for pip to install the dependencies. In that case, you create a temporary directory:

```bash
mkdir -p ~/pip_tmp
TMPDIR=~/pip_tmp ./scripts/setup_pi.sh
```

The setup script installs all OS-level dependencies (`portaudio`, SDL2, Chromium, etc.), creates a Python virtual environment (`.venv/`), and installs the Python packages inside it.

## Configuration
The setup script creates `config.yaml` from `config.example.yaml` automatically. Edit `config.yaml` to set your preferences (it is gitignored):
- **MQTT**: Set up your broker IP, port, and credentials.
- **MagicMirror**: Set the URL corresponding to your local MagicMirror instance.
- **Audio Mode**:
  - `device_index`: Specific ALSA microphone index ID (if required).
  - `threshold_db_warning`: Volume (in dB) where the dB text will turn yellow/orange (default: 60).
  - `threshold_db_error`: Volume (in dB) where the dB text will turn red (default: 85).

> **Note:** The setup script will warn you if `config.yaml` still has placeholder values and skip the service installation prompt until you configure it.

## Local Development (macOS)

To run the audio mode locally on macOS for testing (mirror mode requires Chromium kiosk and is Pi-only):

**1. Run setup (first time only):**
```bash
./scripts/setup_dev.sh
```
This installs Homebrew dependencies (`portaudio`, SDL2, `pkg-config`) and creates a `.venv` with the Python packages.

**2. Run:**
```bash
./run_dev.sh           # audio spectrum analyzer (windowed, default)
./run_dev.sh main      # full orchestrator in offline mode (no MQTT required)
```

The audio mode opens an 800×600 window. Press `Esc` to quit.

## Usage

Manual startup:
```bash
source .venv/bin/activate
python3 main.py
```

### Service Installation / Update (Start on Boot)
To install or update the service, run the install script:

```bash
./scripts/install_service.sh
```

This generates the systemd service file from the template (`scripts/smartframe.service`), and installs/starts it.

## Home Assistant Integration

SmartFrame natively supports **Home Assistant MQTT Auto-Discovery**. When connected to your MQTT broker, it automatically creates a `select` entity (`select.smartframe_mode`) in Home Assistant, populated with the currently available modes. 

Modes are automatically detected from the `modes/` directory (e.g., `audio`, `mirror`), along with the built-in `off` mode. You do *not* need to manually configure YAML in Home Assistant.

### Advanced MQTT Topics
If you prefer manual configuration or integrating with other systems, the orchestrator uses the following topics (configurable in `config.yaml`):
- `smartframe/set_mode` (Command topic to change active mode)
- `smartframe/mode_state` (State topic showing current active mode)
- `smartframe/status` (LWT availability topic: `online` or `offline`)
- `smartframe/modes_available` (JSON list of dynamically detected available modes)
