"""Standing source-encoding invariant — session s006,
`docs/wiki/dispatch-protocol.md` rule 6.

`scripts/check_edit_integrity.py` is the *dispatch-time* check: it compares
a working tree against a git baseline and is run by whoever ran the
dispatch. This file is the *standing* half — the part that runs in the
suite forever, so a corrupted file cannot sit in the repo unnoticed
between dispatches.

## Why this is deliberately NOT baseline-comparing

A test asserting "the working tree matches HEAD" would fail on every
legitimate edit, and a test asserting a fixed list of definitions would
fail on every legitimate deletion. `CLAUDE.md` already records what that
costs: "a suite which cries wolf on the calendar trains you to stop
reading it." So this asserts only properties that are true of *healthy*
source at all times and false only when something is actually broken:

1. every `.py` under `src/`, `scripts/` and `tests/` decodes as UTF-8
2. none of them contains a mojibake signature

Both were violated in s006 and neither was caught by anything else. Four
files ended up with a raw `0x97` byte (`SyntaxError: Non-UTF-8 code` at
import); two more were round-tripped through cp1252 into 309 and 201
mojibake sequences **while remaining valid Python that imported cleanly**,
corrupting citation strings that are persisted to the store.

The mojibake regex is imported from the script rather than duplicated, so
the two halves can never drift apart.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_edit_integrity.py"

# Same importlib-from-path pattern as tests/test_check_heartbeat.py --
# `scripts/` is not a package. Registered in sys.modules before
# exec_module for the dataclass-field-resolution reason that file records.
_spec = importlib.util.spec_from_file_location("check_edit_integrity", _SCRIPT_PATH)
check_edit_integrity = importlib.util.module_from_spec(_spec)
sys.modules["check_edit_integrity"] = check_edit_integrity
_spec.loader.exec_module(check_edit_integrity)

_SEARCH_ROOTS = ("src", "scripts", "tests")


def _python_files() -> list[Path]:
    """Every `.py` under the searched roots, derived from the filesystem —
    never a hardcoded list, which would silently stop covering new files."""
    files: list[Path] = []
    for root in _SEARCH_ROOTS:
        files.extend(sorted((_REPO_ROOT / root).rglob("*.py")))
    return [f for f in files if "__pycache__" not in f.parts]


def test_search_roots_actually_contain_files():
    """Guards the guard: if `_python_files()` ever returns nothing (a moved
    directory, a renamed root), both tests below would pass vacuously and
    the invariant would be silently switched off."""
    files = _python_files()
    assert len(files) > 50, f"expected the repo's full source tree, found {len(files)} .py files"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_source_file_decodes_as_utf8(path: Path):
    """Python 3 source is UTF-8 by default. A raw cp1252 byte is fatal at
    import and invisible in a diff, which is exactly how four files
    shipped broken in s006."""
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        pytest.fail(
            f"{path.relative_to(_REPO_ROOT)} is not valid UTF-8: "
            f"byte {exc.object[exc.start]:#04x} at offset {exc.start}. "
            "Something wrote this file with a non-UTF-8 encoding -- see "
            "docs/wiki/dispatch-protocol.md rule 5."
        )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_source_file_has_no_mojibake(path: Path):
    """Mojibake is valid UTF-8 and valid Python, so nothing else in the
    suite can see it. `defensive_contribution.py` carried 309 sequences
    through an import check and a `git diff` review in s006."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    hits = check_edit_integrity._MOJIBAKE.findall(text)
    assert not hits, (
        f"{path.relative_to(_REPO_ROOT)} contains {len(hits)} mojibake sequence(s) "
        f"({sorted(set(hits))}) -- a UTF-8 file was read or written as cp1252 "
        "somewhere. See docs/wiki/dispatch-protocol.md rule 5."
    )
