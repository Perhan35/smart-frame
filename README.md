# SmartFrame

Raspberry Pi Zero 2 powered smart display with two modes: a Magic Mirror (Chromium kiosk via MagicMirror²) and a real-time audio spectrum analyzer & decibel meter (pygame + PyAudio). Modes are switched remotely via MQTT, with native Home Assistant integration.
Designed for a 14" matte LCD to mimic an e-ink aesthetic.

## Hardware

- **Raspberry Pi**: Zero 2 WH or newer.
- **Screen**: 13.3" or 14" IPS Matte LCD (1920x1080) with HDMI controller board.
- **Audio**: INMP441 Microphone (I2S) for digital audio capture.

## Microphone Wiring (Pi Zero 2 WH <-> INMP441)

To connect the INMP441 I2S microphone to your Raspberry Pi, follow the wiring table below or the visual diagram:

| INMP441 Pin | Raspberry Pi Pin (Physical) | GPIO Pin | Function             |
|:------------|:----------------------------|:---------|:---------------------|
| **VDD**     | Pin 1 (3.3V)                | -        | Power                |
| **GND**     | Pin 6 (GND)                 | -        | Ground               |
| **L/R**     | Pin 9 (GND)                 | -        | Channel Selec (Left) |
| **SCK**     | Pin 12                      | GPIO 18  | Serial Clock         |
| **WS**      | Pin 35                      | GPIO 19  | Word Select          |
| **SD**      | Pin 38                      | GPIO 20  | Serial Data          |

![Microphone Wiring Diagram](images/i2s_rpi_INMP441_mono.png)

![Raspberry Pi Zero 2 WH Pin Layout](images/RPiZero2WH_pins.png)

## Screenshots

![SmartFrame Audio Mode](images/smartframe_audio_mode.png)

![SmartFrame Mirror Mode](images/smartframe_mirror_mode.png)

## Linux Prerequisites (Raspberry Pi OS)

1. Enable the I2S microphone modules in `/boot/firmware/config.txt`:

```ini
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard
```

1. **Enable GPU Acceleration**: Ensure the KMS overlay is active to leverage the VideoCore GPU for hardware-accelerated rendering:

```ini
dtoverlay=vc4-kms-v3d
gpu_mem=128
```

3. **Configure Screen**: Adapt to your needs:

```ini
# Forces HDMI and resolution 1080p 60Hz
hdmi_force_hotplug=1
hdmi_group=2
hdmi_mode=82
```

or force the kernel by adding into `/boot/firmware/cmdline.txt`:

```ini
video=HDMI-A-1:1920x1080M@60
```

*(a reboot of the Raspberry Pi is required)*.

## Performance Optimization (Pi Zero 2 WH)

The Pi Zero 2 WH has limited RAM (512MB). To prevent Chromium from crashing and maintain a "snappy" UI, it is highly recommended to increase the swap size to 1.5GB using native Linux tools:

```bash
# 1. Disable a-la-carte swap scripts (if any)
sudo apt remove -y rpi-swap dphys-swapfile systemd-zram-generator

# 2. Create a permanent 1.5GB swap file
sudo swapoff -a
sudo fallocate -l 1.5G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# 3. Make it permanent on boot:
# Open /etc/fstab and add this line at the bottom:
/swapfile none swap sw 0 0
```

SmartFrame automatically detects the GPU state and will enable hardware-accelerated 2D canvas and GLES2 rendering if available.

## SmartFrame Installation

```bash
git clone https://github.com/Perhan35/smart-frame.git
cd smart-frame
./scripts/setup_pi.sh
```

On some systems (Raspberry Pi Zero 2) the RAM is too low for pip to install the dependencies. In that case, you should create a temporary directory:

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

