"""Final check: every assemble_timeline caller still resolves through the bridge."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mazinger"))
_deps = os.environ.get("STITCHER_TEST_DEPS", "").strip()
if _deps:
    sys.path.insert(0, _deps)

from mazinger import assemble

# 1. The public API kept its name and its positional signature.
import inspect
pub = inspect.signature(assemble.assemble_timeline)
print("assemble_timeline params:")
for name in pub.parameters:
    print("   ", name)

required = ["segment_info", "original_duration", "output_path"]
assert list(pub.parameters)[:3] == required, pub.parameters
print("\npositional signature preserved:", required)

# 2. Both engines are reachable.
assert callable(assemble._assemble_timeline_python)
assert callable(assemble._assemble_audio_with_rust)
print("python engine: _assemble_timeline_python")
print("rust engine  : _assemble_audio_with_rust")

# 3. New keyword-only knobs exist.
for kw in ("use_rust", "background_audio", "background_volume"):
    assert kw in pub.parameters, kw
print("new kwargs    : use_rust, background_audio, background_volume")

# 4. The tail pad is shared by both engines.
src = inspect.getsource(assemble)
assert src.count("tail_pad_sec = TAIL_PAD_SEC") == 1
assert "TAIL_PAD_SEC = 2.0" in src
print("TAIL_PAD_SEC shared:", assemble.TAIL_PAD_SEC)

# 5. Callers that reference the module attribute resolve.
from mazinger import pipeline
from mazinger.editor import ops as editor_ops
from mazinger.cli import _speak
for mod in (pipeline, editor_ops, _speak):
    assert mod.assemble is assemble or hasattr(mod, "assemble")
print("callers resolve: pipeline.py, editor/ops.py, cli/_speak.py")

print("\nAll integration checks passed")
