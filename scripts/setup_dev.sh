#!/bin/bash
set -e

echo "=== SmartFrame Dev Setup ==="

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR/.."

# Install system dependencies (portaudio for PyAudio, SDL2 for pygame)
for pkg in portaudio pkg-config sdl2 sdl2_image sdl2_mixer sdl2_ttf; do
    if ! brew list "$pkg" &>/dev/null; then
        echo "Installing $pkg via Homebrew..."
        brew install "$pkg"
    else
        echo "$pkg already installed."
    fi
done

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
else
    echo ".venv already exists, skipping."
fi

# Activate and install dependencies
echo "Installing Python dependencies..."
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt

# Install post-merge git hook
echo "Installing git post-merge hook..."
HOOK_PATH="$(git rev-parse --git-dir)/hooks/post-merge"
cp "$DIR/post-merge.hook" "$HOOK_PATH"
chmod +x "$HOOK_PATH"
echo "Git hook installed."

echo ""
echo "=== Setup complete ==="
echo "Run './run_dev.sh' to start in dev mode."
