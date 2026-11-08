import sys
from pathlib import Path

# Tests import the smartframe/ package and the scripts/ from the repo root.
# They never import main.py (it loads config.yaml and needs PortAudio).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
