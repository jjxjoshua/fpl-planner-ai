#!/usr/bin/env python
"""Detect the class of edit damage that survives every check we already
run — session s006, `docs/wiki/dispatch-protocol.md` rule 6.

## What this answers, and why nothing else answers it

After a multi-file dispatch, the question is: **did the edits change only
what they were supposed to change, or did they also silently destroy
something?** Three real failures in s006 say that is not a paranoid
question:

- `saves.py` lost FIVE top-level definitions, `build_training_table`
  among them, to a bulk edit script. The file still parsed.
- `cards.py` lost five more the same way.
- `defensive_contribution.py` and `team_strength.py` were round-tripped
  through cp1252, turning every em-dash and section sign into a two-
  character pair, 309 and 201 times -- **including inside
  `DCThresholdProvenance.source`, which is
  persisted to the store.** Both files stayed valid Python and imported
  cleanly.

Every tool already in the loop missed all four. `python -c "import ..."`
imports a file that lost an unreferenced function. `ast.parse` accepts it.
`git diff` renders a mis-encoded byte as an ordinary-looking dash, so a
human reading the diff sees nothing. Two of the four ALSO failed to decode
as UTF-8 (a raw `0x97`), which at least raises `SyntaxError` at import —
but the two that mattered most did not, because mojibake is valid UTF-8.

So this checks the three things that are invisible everywhere else:

1. **the file decodes as UTF-8** (Python 3 source is UTF-8 by default; a
   raw cp1252 byte is fatal at import and invisible in a diff)
2. **top-level `def`/`class` names are a SUPERSET of the baseline's** — a
   deletion is reported by name, which is what would have caught
   `saves.py`
3. **mojibake signatures have not INCREASED against the baseline** —
relative, never absolute, so a file that legitimately contains an
   today does not fail tomorrow for containing it still

## Baseline-relative by design

Every check compares against a git baseline (`--baseline`, default
`HEAD`), never against a fixed expectation. A deliberate deletion is
reported and can be waived with `--allow-removed`; it is not silently
tolerated, and it does not require editing this file. This is the same
reasoning `CLAUDE.md` records for tests that assert against literals: a
check whose expectation drifts from reality trains you to stop reading it.

## Exit codes

  0  every checked file passed all three checks
  1  at least one file failed at least one check
  2  the baseline could not be resolved (bad ref, not a git repo)

## Usage

    python scripts/check_edit_integrity.py
    python scripts/check_edit_integrity.py --baseline master
    python scripts/check_edit_integrity.py --paths src/fplai/models/saves.py
    python scripts/check_edit_integrity.py --allow-removed _legacy_helper

Read-only. It never writes, restores, or touches the working tree — the
recovery decision belongs to whoever reads the report.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The classic UTF-8-decoded-as-cp1252 signatures. Multi-character on
# purpose: a bare "â" or "Ã" occurs in legitimate prose (façade, café in a
# citation string), while these pairs effectively cannot. Counted, then
# compared against the baseline's own count, so even a false positive here
# cannot fail a file that already contained it.
# The classic UTF-8-read-as-cp1252 signatures, built from CODEPOINTS rather
# than written as literal characters. Literal patterns would put a dozen
# mojibake sequences in this file by construction, forcing
# tests/test_source_integrity.py to exempt it -- and a detector exempted
# from its own check is one nobody can trust. The first run of that test
# failed on exactly this, which is the point of having it.
#
# Each tuple is what a multi-byte UTF-8 character looks like after being
# decoded one byte at a time as cp1252: 0xE2 0x80 0x94 (an em-dash) comes
# back as two characters, U+00E2 followed by U+20AC.
_MOJIBAKE_SEQUENCES = (
    (0x00E2, 0x20AC),          # E2 80 xx -- em/en dash, curly quotes
    (0x00E2, 0x201A, 0x00AC),  # euro sign
    (0x00C2, 0x00A7),          # section sign
    (0x00C2, 0x00B0),          # degree sign
    (0x00C2, 0x00AB),          # left guillemet
    (0x00C2, 0x00BB),          # right guillemet
    (0x00C2, 0x00A0),          # non-breaking space
    (0x00C3, 0x201A),          # capital A with circumflex
    (0x00C3, 0x00A2),          # capital A with tilde
    (0x00C3, 0x0192),          # capital A with acute
    (0x00C3, 0x00A9),          # e with acute
)
_MOJIBAKE = re.compile(
    "|".join(re.escape("".join(chr(c) for c in seq)) for seq in _MOJIBAKE_SEQUENCES)
)


@dataclass
class FileReport:
    path: str
    utf8_ok: bool = True
    utf8_detail: str = ""
    removed_defs: tuple[str, ...] = ()
    mojibake_now: int = 0
    mojibake_baseline: int = 0
    parse_error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def mojibake_delta(self) -> int:
        return self.mojibake_now - self.mojibake_baseline

    @property
    def ok(self) -> bool:
        return (
            self.utf8_ok
            and not self.removed_defs
            and self.mojibake_delta <= 0
            and not self.parse_error
        )


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], capture_output=True)


def _baseline_bytes(path: str, baseline: str) -> bytes | None:
    """The file's content at `baseline`, or None if it did not exist there
    (a newly added file — nothing to compare, only the UTF-8 check
    applies)."""
    result = _git("show", f"{baseline}:{path}")
    if result.returncode != 0:
        return None
    return result.stdout


def _changed_python_files(baseline: str) -> list[str]:
    """Tracked `.py` files differing from `baseline`, plus untracked ones.

    Untracked files are included deliberately: a brand-new module written
    by an agent through a mis-encoding path is exactly as broken as an
    edited one, and it has no baseline to hide behind.
    """
    tracked = _git("diff", "--name-only", baseline)
    untracked = _git("ls-files", "--others", "--exclude-standard")
    names = tracked.stdout.decode("utf-8", "replace").split()
    names += untracked.stdout.decode("utf-8", "replace").split()
    return sorted({n for n in names if n.endswith(".py")})


def _top_level_names(source: str) -> set[str]:
    """Top-level `def`/`class` names. Nested definitions are deliberately
    ignored — this check is about a whole unit vanishing, and reporting
    every inner helper would bury that signal."""
    tree = ast.parse(source)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def check_file(path: str, *, baseline: str, allow_removed: frozenset[str]) -> FileReport:
    report = FileReport(path=path)
    raw = Path(path).read_bytes()

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        report.utf8_ok = False
        report.utf8_detail = f"byte {exc.object[exc.start]:#04x} at offset {exc.start}"
        return report  # nothing further is meaningful on undecodable bytes

    report.mojibake_now = len(_MOJIBAKE.findall(text))

    baseline_raw = _baseline_bytes(path, baseline)
    if baseline_raw is None:
        report.notes.append("new file -- no baseline to compare")
        return report

    try:
        baseline_text = baseline_raw.decode("utf-8")
    except UnicodeDecodeError:
        report.notes.append("baseline itself is not valid UTF-8 -- comparison skipped")
        return report

    report.mojibake_baseline = len(_MOJIBAKE.findall(baseline_text))

    try:
        now_names = _top_level_names(text)
        baseline_names = _top_level_names(baseline_text)
    except SyntaxError as exc:
        report.parse_error = f"{exc.msg} (line {exc.lineno})"
        return report

    removed = sorted(baseline_names - now_names - allow_removed)
    report.removed_defs = tuple(removed)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--baseline",
        default="HEAD",
        help="git ref to compare against (default: HEAD). Use the pre-dispatch commit.",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="explicit files to check; default is every changed/untracked .py",
    )
    parser.add_argument(
        "--allow-removed",
        nargs="*",
        default=[],
        help="top-level names whose removal is intended, so it is waived rather than tolerated",
    )
    args = parser.parse_args(argv)

    if _git("rev-parse", "--verify", args.baseline).returncode != 0:
        print(f"cannot resolve baseline {args.baseline!r} -- not a git repo, or bad ref", file=sys.stderr)
        return 2

    paths = args.paths if args.paths is not None else _changed_python_files(args.baseline)
    if not paths:
        print(f"no changed .py files against {args.baseline} -- nothing to check")
        return 0

    reports = [
        check_file(p, baseline=args.baseline, allow_removed=frozenset(args.allow_removed))
        for p in paths
        if Path(p).exists()
    ]

    width = max(len(r.path) for r in reports)
    print(f"baseline: {args.baseline}\n")
    # ASCII only in console output: this script's own first run died on a
    # UnicodeEncodeError writing a Greek delta to a cp1252 Windows console —
    # the same encoding hazard it exists to detect, one layer up.
    print(f"{'file':{width}s}  {'utf8':>5s}  {'moji +/-':>8s}  removed top-level defs")
    print("-" * (width + 40))
    for r in sorted(reports, key=lambda r: (r.ok, r.path)):
        utf8 = "ok" if r.utf8_ok else "FAIL"
        delta = f"{r.mojibake_delta:+d}" if r.mojibake_baseline or r.mojibake_now else "0"
        detail = ""
        if not r.utf8_ok:
            detail = f"NOT UTF-8: {r.utf8_detail}"
        elif r.parse_error:
            detail = f"SYNTAX: {r.parse_error}"
        elif r.removed_defs:
            detail = "MISSING: " + ", ".join(r.removed_defs)
        elif r.notes:
            detail = "; ".join(r.notes)
        print(f"{r.path:{width}s}  {utf8:>5s}  {delta:>8s}  {detail}")

    failed = [r for r in reports if not r.ok]
    print()
    if failed:
        print(f"INTEGRITY FAILED -- {len(failed)} of {len(reports)} file(s) damaged")
        return 1
    print(f"integrity ok -- {len(reports)} file(s) checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
