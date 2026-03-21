#!/bin/bash
set -e

if [ ! -d ".venv" ]; then
    echo "No .venv found. Run './scripts/setup_dev.sh' first."
    exit 1
fi

source .venv/bin/activate

MODE=${1:-audio}

case "$MODE" in
    audio)
        echo "Starting audio mode (windowed)..."
        python3 modes/audio_mode.py
        ;;
    main)
        echo "Starting main orchestrator (offline/no MQTT)..."
        python3 main.py
        ;;
    *)
        echo "Usage: $0 [audio|main]"
        echo "  audio  - Run the audio spectrum analyzer directly (default)"
        echo "  main   - Run the full orchestrator (offline mode, no MQTT needed)"
        exit 1
        ;;
esac
