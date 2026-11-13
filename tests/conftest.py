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
# they always use this stub, even where PyAudio is installed, so that they behave
# the same on a dev machine as in CI. Add here any pyaudio name the code uses.
_PYAUDIO_STUB = types.SimpleNamespace(
    PyAudio=None,
    paInt16=8,
    paInt32=2,
    paSampleFormatNotSupported=-9994,
    paDeviceUnavailable=-9985,
)
try:
    import pyaudio as _real_pyaudio
except ImportError:
    _real_pyaudio = None
if _real_pyaudio is not None:  # The stub must keep PyAudio's real values
    for _name, _value in vars(_PYAUDIO_STUB).items():
        _real = getattr(_real_pyaudio, _name)
        assert _value is None or _value == _real, f"pyaudio stub: {_name}={_value}, real {_real}"
sys.modules["pyaudio"] = _PYAUDIO_STUB
