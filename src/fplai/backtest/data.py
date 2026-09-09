"""Season data loading for the Phase 1 backtest — blueprint §7.2.

**Uses `store.observations()`, never `store.as_of()`, for
`vaastav_player_gameweek_stats` — deliberately.** `as_of()` collapses to one
row per declared entity key, and that dataset's entity key is `(season,
round, element)`. Real double/triple gameweeks put 2-3 rows under that exact
key (verified live: 2020-21 GW35 — a Covid-rearrangement pileup — gives
Bruno Fernandes three rows, one per fixture, same `batch_id`, different
opponent/kickoff/points). `as_of()`'s `QUALIFY ROW_NUMBER() OVER (PARTITION
BY entity_key ...) = 1` would silently keep exactly one of those and drop
the rest — the mirror image of the bug blueprint §3.2 already fixed once
(as_of returning duplicated rows instead of state). This module never
routes through `as_of()` for this dataset; it reads the raw stream via
`observations()` and reasons about "one row per (season, round, element,
fixture)" explicitly instead of trusting the store's declared entity key to
mean "one row."

This is a load-bearing finding, not a style choice — see the `finding`
punch-card entries from this session and the docstring note this session
also proposed for `schemas.py`'s `PLAYER_GAMEWEEK_STATS_GAMEWEEK` schema.

**Coverage gaps are detected and reported explicitly, never silently
degraded** (task brief, "expect the data to fight you"). Two real gaps were
found and verified live against the upstream archive (not assumed):
2019-20 has only gameweeks 1-29 of 38 in the store; 2022-23 is missing
gameweek 7 entirely. Both are ingestion gaps (the archive itself has the
missing files — confirmed with a direct fetch), not genuine blank
gameweeks. `SeasonCoverage` surfaces this so callers (the replay harness,
the report) can decide how to handle it rather than discovering it by a
wrong total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import polars as pl

from fplai.backtest.rules import EXPECTED_GAMEWEEKS
from fplai.store import BitemporalStore

DATASET = "vaastav_player_gameweek_stats"

# The columns this module actually uses. Present for every season
# 2020-21 onward (verified live — see this session's punch-card); 2019-20
# has the `position`/`team` columns but they are wholly NULL for every row
# (pre-dates that FPL schema), which is exactly why 2019-20 cannot be
# scored as a normal season by this harness regardless of its round-gap.
REQUIRED_COLUMNS = (
    "season",
    "round",
    "element",
    "name",
    "position",
    "team",
    "value",
    "selected",
    "total_points",
    "minutes",
    "fixture",
)


class SeasonDataError(ValueError):
    """A season's data does not satisfy what this module needs to build a
    replay — raised rather than silently returning something misleading."""


@dataclass(frozen=True)
class SeasonCoverage:
    season: str
    rounds_present: tuple[int, ...]
    expected_gameweeks: int
    missing_rounds: tuple[int, ...]

    @property
    def is_complete(self) -> bool:
        return len(self.missing_rounds) == 0

    @property
    def coverage_fraction(self) -> float:
        return len(self.rounds_present) / self.expected_gameweeks

    def describe(self) -> str:
        if self.is_complete:
            return f"{self.season}: complete, {len(self.rounds_present)}/{self.expected_gameweeks} gameweeks"
        return (
            f"{self.season}: INCOMPLETE, {len(self.rounds_present)}/{self.expected_gameweeks} "
            f"gameweeks present, missing rounds {list(self.missing_rounds)}"
        )


@dataclass(frozen=True)
class SeasonData:
    season: str
    frame: pl.DataFrame  # every row for this season, as stored (observations() stream)
    coverage: SeasonCoverage

    def rounds(self) -> list[int]:
        """The rounds this replay can actually iterate over — only rounds
        present in the store. A wholesale-missing round (see module
        docstring) is skipped entirely, never fabricated as a scoreless
        round for every player, which would be inventing information the
        store does not have."""
        return sorted(self.coverage.rounds_present)


def _normalise_position(frame: pl.DataFrame, season: str) -> pl.DataFrame:
    """Two more real, verified drift cases in `position`, found live while
    building this harness (not in provider-framework.md's original recon —
    a Phase 1 finding, not a Phase 0 one):

    1. **`GKP` vs `GK`.** 2021-22 carries `GK` for every round except round
       37, which is labelled `GKP` — a one-round vaastav inconsistency, not
       a genuine mid-season schema change (every other season uses `GK`
       throughout, or `GKP` not at all). Normalised to `GK` unconditionally.
    2. **`AM` rows.** 2024-25 (and only 2024-25, of the seasons checked)
       carries 322 rows with `position == 'AM'` — real managers (Mikel
       Arteta, Pep Guardiola, ...) priced and pointed like players. This is
       the Assistant Manager chip (blueprint §11 notes it existed before
       2026/27 and was removed for 2026/27) — a special 16th pick, not a
       member of the 15-man squad this harness builds. Dropped entirely
       from the candidate pool; a squad-selection baseline has no concept
       of the AM chip to build (that is Phase 6 scope, blueprint §7's chip
       phase), and leaving these rows in would let a manager's name be
       greedily "selected" as if it were a footballer.
    """
    if "position" not in frame.columns:
        return frame
    n_am = frame.filter(pl.col("position") == "AM").height
    if n_am:
        frame = frame.filter(pl.col("position") != "AM")
    return frame.with_columns(
        pl.when(pl.col("position") == "GKP")
        .then(pl.lit("GK"))
        .otherwise(pl.col("position"))
        .alias("position")
    )


def load_season(
    store: BitemporalStore,
    season: str,
    *,
    expected_gameweeks: int = EXPECTED_GAMEWEEKS,
    _now: datetime | None = None,
) -> SeasonData:
    """Load one season's gameweek stats from the store, validated and with
    coverage gaps surfaced (never silently patched over).

    `_now` is a test seam only (the archive is static historical data — the
    observed_at cutoff does not gate anything meaningful here, since every
    row was written in one recent bulk ingestion long after the season it
    describes; see this module's docstring for why `observations()` rather
    than `as_of()` is the correct read regardless).
    """
    until = _now if _now is not None else datetime.now(timezone.utc)
    raw = store.observations(DATASET, until=until)
    if raw.is_empty():
        raise SeasonDataError(f"dataset {DATASET!r} is empty in the store — nothing to load")

    frame = raw.filter(pl.col("season") == season)
    if frame.is_empty():
        available = sorted(raw["season"].unique().to_list())
        raise SeasonDataError(
            f"season {season!r} has no rows in {DATASET!r}. Seasons present: {available}"
        )

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise SeasonDataError(f"season {season!r} is missing required columns {missing}")

    if season == "2019-20":
        # Verified live: 2019-20's rows have `position`/`team` columns
        # present but wholly NULL (the FPL schema didn't carry them yet).
        # A season that can't tell a GK from a FWD cannot have a valid
        # squad built against it — fail loudly rather than let every
        # position-based constraint silently pass on garbage.
        for col in ("position", "team"):
            if frame[col].null_count() == frame.height:
                raise SeasonDataError(
                    f"season {season!r}: column {col!r} exists but is wholly NULL for every "
                    "row (a real, verified schema gap for this season — see "
                    "providers/vaastav.py's module docstring). This harness cannot build a "
                    "position/club-constrained squad without it; excluded, not degraded."
                )

    frame = _normalise_position(frame, season)

    rounds_present = tuple(sorted(frame["round"].unique().to_list()))
    expected_set = set(range(1, expected_gameweeks + 1))
    missing_rounds = tuple(sorted(expected_set - set(rounds_present)))

    coverage = SeasonCoverage(
        season=season,
        rounds_present=rounds_present,
        expected_gameweeks=expected_gameweeks,
        missing_rounds=missing_rounds,
    )

    return SeasonData(season=season, frame=frame, coverage=coverage)


def rows_for_round(season_data: SeasonData, round_number: int) -> pl.DataFrame:
    """Every row (one per player per fixture — a double/triple gameweek
    player has more than one) for exactly this round. Never includes any
    other round."""
    return season_data.frame.filter(pl.col("round") == round_number)


def rows_before_round(season_data: SeasonData, round_number: int) -> pl.DataFrame:
    """Every row with `round < round_number` — the leakage boundary. This
    is the ONLY view of the season a strategy is ever handed; see
    `fplai.backtest.replay.GameweekView`."""
    return season_data.frame.filter(pl.col("round") < round_number)
