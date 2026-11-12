## This repository is public

Everything committed here (code, comments, docs, config examples, screenshots, commit messages) is visible to anyone. Keep it generic: no personal setup details such as IP addresses, hostnames, usernames or passwords, network layout, or screenshots showing real calendars, places or names. Use placeholders (`[MQTT_SERVER_IP_ADDRESS]`) and made-up data instead. Your own values belong in `config.yaml`, which is not committed.

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