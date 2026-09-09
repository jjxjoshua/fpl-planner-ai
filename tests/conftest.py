import sys
from pathlib import Path

# Allow `import fplai...` and `import <script_module>` without an editable
# install — this is a Phase 0 spine, not a published package (pyproject.toml
# sets tool.uv.package = false).
ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "scripts"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
