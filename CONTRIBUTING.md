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