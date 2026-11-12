import os
import sys
import tempfile
import types
from pathlib import Path

# Tests import the smartframe/ package and the scripts/ from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# The orchestrator modules (smartframe.settings and the ones that import it) read
# config.yaml and .smartframe_cache when imported: give them test files instead of
# the real ones, before any test imports them.
_TMP = Path(tempfile.mkdtemp(prefix="smartframe-tests-"))
(_TMP / "config.yaml").write_text(
    "mqtt:\n"
    '  broker: "[MQTT_SERVER_IP_ADDRESS]"\n'
    '  device_id: "testframe"\n'
    "default_mode: off\n"
    "audio:\n"
    "  device_index: null\n"
)
os.environ["SMARTFRAME_CONFIG"] = str(_TMP / "config.yaml")
os.environ["SMARTFRAME_CACHE"] = str(_TMP / "cache.json")

# PyAudio needs PortAudio (not installed in CI). The tests never open a microphone:
# a stub is enough to import smartframe.audio_monitor.
try:
    import pyaudio  # noqa: F401
except ImportError:
    sys.modules["pyaudio"] = types.SimpleNamespace(PyAudio=None, paInt16=8, paInt32=2)
