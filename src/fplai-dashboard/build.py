#!/usr/bin/env python
"""Compile the dashboard sources into a single self-contained page.

    uv run python src/fplai-dashboard/build.py

Reads `index.html` in this directory, inlines the stylesheet and both scripts,
escapes every non-ASCII character, and writes `public/index.html`.

Three things the compiled output must satisfy, and why:

1. **Self-contained.** The published-artifact CSP blocks every external host
   except Google Fonts, so CSS and JS cannot stay as separate files. Fonts are
   left as a `<link>` on purpose -- that host is the one exception.

2. **No `<!doctype>`, `<html>`, `<head>` or `<body>`.** The artifact host wraps
   the page in its own skeleton at publish time, so the compiled file is a
   fragment: title, font link, style, markup, scripts. Browsers construct the
   missing elements themselves, so the same file still opens directly from
   disk.

3. **ASCII only.** Point 2 means the compiled file cannot carry its own
   `<meta charset>`, so it must not depend on one. Every non-ASCII character is
   escaped in the form its own zone understands -- CSS escapes inside the
   stylesheet, `\\uXXXX` inside the scripts, `&#xNN;` in the markup. The source
   files stay readable UTF-8; only the compiled output is escaped.

No dependencies, no build tooling, no network. Standard library only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "public" / "index.html"

STYLESHEET = '<link rel="stylesheet" href="styles.css">'
SCRIPTS = ("data.js", "app.js")


def css_escape(text: str) -> str:
    """CSS identifier/string escape: a backslash, four hex digits, one space."""
    return "".join(c if ord(c) < 128 else "\\%04X " % ord(c) for c in text)


def js_escape(text: str) -> str:
    return "".join(c if ord(c) < 128 else "\\u%04X" % ord(c) for c in text)


def html_escape(text: str) -> str:
    return "".join(c if ord(c) < 128 else "&#x%X;" % ord(c) for c in text)


def build() -> int:
    page = (HERE / "index.html").read_text(encoding="utf-8")

    # The charset declaration exists only so the sources render when opened
    # straight from src/. The compiled fragment has no head to carry it.
    page = page.replace('<meta charset="utf-8">\n', "", 1)

    if STYLESHEET not in page:
        print(f"error: {STYLESHEET} not found in index.html", file=sys.stderr)
        return 1
    css = (HERE / "styles.css").read_text(encoding="utf-8")
    page = page.replace(STYLESHEET, "<style>\n" + css_escape(css).rstrip() + "\n</style>", 1)

    for name in SCRIPTS:
        tag = f'<script src="{name}"></script>'
        if tag not in page:
            print(f"error: {tag} not found in index.html", file=sys.stderr)
            return 1
        source = (HERE / name).read_text(encoding="utf-8")
        page = page.replace(tag, "<script>\n" + js_escape(source).rstrip() + "\n</script>", 1)

    # Anything left outside the inlined blocks is markup.
    page = "".join(
        chunk if chunk.startswith(("<style>", "<script>")) else html_escape(chunk)
        for chunk in re.split(r"(<style>.*?</style>|<script>.*?</script>)", page, flags=re.S)
    )

    stray = sorted({c for c in page if ord(c) > 127})
    if stray:
        print(f"error: non-ASCII survived escaping: {stray}", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}  ({len(page):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
