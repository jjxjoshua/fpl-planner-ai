#!/usr/bin/env python
"""Answer "what shape is this data actually in?" BEFORE a brief is written
— session s006, `docs/wiki/dispatch-protocol.md` rule 1.

## Why this exists

The s006 migration pinned a design decision (`allow_live_season=False`
should *raise*) from reading code rather than running it. Running the
default path against the real store for thirty seconds would have shown
that a raise leaves no way to *exclude* the live season, so every fit
after GW1 either dies or re-admits exactly the rows the guard exists to
keep out. That was discovered after seven modules had implemented it.

The general lesson, which `CLAUDE.md` already states in another form
("measure the size of the prize before building the plumbing"): a plausible
belief about data shape is not evidence. This prints the evidence, cheaply,
so the brief can quote it instead of asserting it.

## What it reports

- row count, resolved as-of the given deadline
- every column with its dtype
- season coverage with per-season row counts
- provider split (`source_provider`) when the source carries one, so a
  union's composition is visible rather than assumed
- null-rate for any columns named with `--columns`, which is the question
  that actually decides whether a model can train on a field

## Bitemporal resolution

Uses `effective_at()` (valid time) by default, not `as_of()` (observation
time) — the same choice every model's `build_training_table` already makes,
and for the reason `fplai.gameweek_stats`' module docstring spells out:
these datasets are bulk-ingested, so `observed_at` is ingest time and
`as_of()` would return empty for any real historical deadline. Pass
`--observations` to see ingest cadence instead, which is a different
question and rarely the one a brief needs.

## Usage

    python scripts/probe_store_shape.py --capability player.gameweek_stats
    python scripts/probe_store_shape.py --dataset elements
    python scripts/probe_store_shape.py --dataset vaastav_player_gameweek_stats \\
        --columns saves goals_conceded defensive_contribution
    python scripts/probe_store_shape.py --capability player.gameweek_stats \\
        --as-of 2026-08-21T17:30:00Z

Read-only. Paste its output into the brief verbatim.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.gameweek_stats import read_player_gameweek_stats  # noqa: E402
from fplai.store import BitemporalStore  # noqa: E402

# Capability name -> the sanctioned reader for it. A capability is read
# through its reader, never by naming one of the datasets behind it --
# that conflation is the design flaw the s006 migration existed to fix.
_CAPABILITY_READERS = {
    "player.gameweek_stats": read_player_gameweek_stats,
}


def _parse_as_of(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SystemExit("--as-of must be timezone-aware (blueprint 3.2), e.g. 2026-08-21T17:30:00Z")
    return parsed


def _load(store: BitemporalStore, args: argparse.Namespace, as_of: datetime) -> pl.DataFrame:
    if args.capability:
        reader = _CAPABILITY_READERS.get(args.capability)
        if reader is None:
            known = ", ".join(sorted(_CAPABILITY_READERS)) or "(none registered)"
            raise SystemExit(f"no reader registered for capability {args.capability!r}. Known: {known}")
        return reader(store, as_of=as_of)
    if args.observations:
        return store.observations(args.dataset, until=as_of)
    return store.effective_at(args.dataset, as_of)


def _print_counts(df: pl.DataFrame, column: str, *, label: str) -> None:
    if column not in df.columns:
        return
    counts = df.group_by(column).len().sort(column)
    print(f"\n{label}:")
    for row in counts.iter_rows(named=True):
        print(f"  {str(row[column]):<24s} {row['len']:>8,d}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", help="a physical dataset name, e.g. elements")
    source.add_argument("--capability", help="a capability name, read through its sanctioned reader")
    parser.add_argument("--as-of", dest="as_of", default=None, help="tz-aware ISO instant; default now")
    parser.add_argument("--columns", nargs="*", default=[], help="columns to report null-rates for")
    parser.add_argument(
        "--observations",
        action="store_true",
        help="resolve on observation time instead of valid time (ingest cadence, not state)",
    )
    args = parser.parse_args(argv)

    as_of = _parse_as_of(args.as_of)
    store = BitemporalStore()
    df = _load(store, args, as_of)

    name = args.capability or args.dataset
    mode = "observations (observed_at)" if args.observations else "effective_at (valid time)"
    print(f"source:  {name}")
    print(f"as_of:   {as_of.isoformat()}")
    print(f"resolve: {mode}")
    print(f"rows:    {df.height:,d}")

    if df.is_empty():
        print("\nEMPTY at this as_of -- nothing ingested this far back, or the name is wrong.")
        return 0

    print(f"\ncolumns ({len(df.columns)}):")
    for col, dtype in zip(df.columns, df.dtypes):
        print(f"  {col:<32s} {dtype}")

    _print_counts(df, "season", label="rows by season")
    _print_counts(df, "source_provider", label="rows by provider")

    if "attribution_complete" in df.columns:
        degraded = df.filter(pl.col("attribution_complete") == False).height  # noqa: E712
        print(f"\nattribution_complete == False: {degraded:,d} row(s)")

    if args.columns:
        print("\nnull-rate for requested columns:")
        for col in args.columns:
            if col not in df.columns:
                print(f"  {col:<32s} COLUMN ABSENT")
                continue
            nulls = df[col].null_count()
            pct = 100.0 * nulls / df.height
            print(f"  {col:<32s} {nulls:>8,d} / {df.height:,d}  ({pct:5.2f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
