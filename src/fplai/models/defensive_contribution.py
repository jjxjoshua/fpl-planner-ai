"""Defensive-contribution (DC) estimator — blueprint §4 ("Defensive"
row), §7.1 (calibration before points), §11 (DC calibration constraint),
§12.2 (derived-fact labelling), `docs/wiki/defensive-contribution.md`
(`fpl-elite`'s sourcing report). Phase 2, session `s003`. Structural
precedent: `fplai.models.minutes`/`fplai.models.team_strength`.

## What is verified, and what is not — read this before anything else

**Verified live against the real store, 2026-08-22** (not re-derived from
the brief; see this session's punch-out for the exact commands):

- All four DC counters (`recoveries`, `tackles`,
  `clearances_blocks_interceptions`, `defensive_contribution`) are in-store
  for **2025-26 only** — 29,747 rows each after `effective_at()`'s own
  dedup (see `fplai.models.team_strength`'s module docstring, "A real,
  live-verified side effect" — the same 10 exact-duplicate rows this store
  carries in one write batch collapse here too; the brief's cited 29,757
  is the pre-dedup raw count).
- **Composition, checked over every 2025-26 row, not a sample**: `DEF`
  count == `tackles + clearances_blocks_interceptions` (0/9,733
  mismatches); `MID`/`FWD` count == `tackles + clearances_blocks_
  interceptions + recoveries` (0/16,587 mismatches). `recoveries` is
  EXCLUDED for defenders — matches `docs/wiki/defensive-contribution.md`
  §1.1 exactly.
- `defensive_contribution` for **GK is always 0** (3,427/3,427 rows) even
  though a keeper's own `recoveries` reaches as high as 18 in this store —
  FPL's own field is already position-gated; this module never recomputes
  a GK's "would-be" count from raw components, because the rules define no
  CBIT/CBIRT formula for goalkeepers at all (they are not a threshold
  short of eligibility — they are outside the rule).
- `game_config`'s `scoring.defensive_contribution` is
  `{"DEF": 2, "FWD": 2, "GKP": 0, "MID": 2}`, live-read 2026-08-22 — the
  POSITION GATE (which positions are DC-eligible at all) is therefore
  machine-readable and never hardcoded here (see `read_dc_points_by_
  position`/`build_dc_threshold_set`). The string `"threshold"` appears
  **zero times** anywhere in the `game_config` payload (checked directly,
  `"threshold" in payload_raw.lower()` → `False`) — the COUNT thresholds
  (10/12) are genuinely absent from the API, not merely unread.
- Grain is per FIXTURE: 2025-26 has 409 `(season, round, element)` pairs
  with two rows after `effective_at()`'s dedup (brief's pre-dedup figure:
  419) — every function in this module operates at that grain for the
  TARGET; only TRAILING features roll up to (season, element/team, round)
  the same way `fplai.models.minutes._build_round_rollup` does, and for
  the same reason (a double gameweek's two fixtures share one deadline).
- DEF hit rate (count >= 10) over every 2025-26 fixture row, including
  0-minute rows: 8.44%. MID/FWD hit rate (count >= 12): 3.59%. Both are
  genuinely rare events — the reason this module fits a full count
  distribution rather than a bare logistic classifier on the threshold
  event (see "Why Negative Binomial, not logistic regression" below).

**NOT verified, and structurally labelled as such at every output row**
(this is the crux of the task, per this session's brief): the 10/12
COUNT THRESHOLDS themselves rest on `docs/wiki/defensive-contribution.md`
§1.1 — premierleague.com, dated, Tier HIGH-but-press, not machine-readable.
`PRESS_DC_COUNT_THRESHOLDS` below carries `verified=False` and a citation
on every entry; `DCThresholdSet`/`DCGroupParams`/`DCPMF` all thread that
flag through to the final persisted row (`threshold_verified` — a real
column, not a docstring promise) so a query against the derived dataset
six weeks from now can filter on it directly, without reading this
module's source. `scripts/pin_dc_thresholds.py` is the script that
attempts to replace `verified=False` with an observation once a gameweek
settles — see its own docstring; **not yet possible as of this session**,
GW1 carries `finished=False, data_checked=False` in the live `events`
snapshot (checked directly against the real store this session).

## Session s004 — the loop actually closes

`scripts/pin_dc_thresholds.py` pinned both groups exactly against the
real, settled GW1 payload (10/12, matching the press values) but only
PRINTED the result — `PRESS_DC_COUNT_THRESHOLDS` still carried
`verified=False` and every persisted DC row still lied about it. This
session wires the two together: the script now PERSISTS its observation
(`write_dc_threshold_observations`, below) and `build_dc_threshold_set`
RESOLVES it back, bitemporally, `as_of` a deadline
(`resolve_dc_threshold_observations`) — a caller that does not pass an
explicit `count_thresholds` override now gets `verified=True` for any
group with a real pin visible by that `as_of`, and the untouched
press-sourced `verified=False` default otherwise. Nothing changes for a
caller that already passes an explicit `count_thresholds` (every real
production caller before this session did, and still can).

**Observed, not derived — the call this session had to make.** A pinned
threshold is a DETERMINISTIC bounds-meeting computation over real
`(count, points)` pairs FPL's own API returned — `T <= min(count |
points==2)` and `T > max(count | points==0)`, resolved to a single value
only when those bounds meet EXACTLY. There is no fitted parameter, no
prior, no residual, no `calibration_reference` that would mean anything
for it — the same reasoning `fplai.schemas.JOB_HEARTBEAT_RUN`'s own
docstring already gives for staying `is_modelled=False` ("a genuine
observation... never a statistical inference"). It is therefore an
OBSERVATION in this store's vocabulary, not a derived/modelled fact under
blueprint §12.2 — it does not belong under `DERIVED_DATASET_PREFIX`, and
persisting it must never stamp `is_modelled=True`/`derived_from`/a
calibration reference it cannot honestly carry.

**Registered as an OBSERVED capability** —
`fplai.schemas.GAME_DC_THRESHOLD_OBSERVATION_GAMEWEEK`, Architect ruling
2026-08-27, same side of the partition and for the same reason as
`job.heartbeat@run`. A pin is a deterministic bounds-meeting computation
over real `(count, points)` pairs, with no fitted parameter and no
residual, so §12.2's derived-provenance contract would be meaningless for
it and it must stay queryable as an observation of FPL's actual scoring
rule. The schema lives in `fplai.schemas` and is imported here, never
redeclared — one place an entity key is declared, never two that can
drift apart (`fplai.schemas`'s own stated rule for `DATASET_ENTITY_KEYS`).

The first implementation left it unregistered as an interim measure,
because registering it requires updating exact-set assertions in
`tests/test_schemas.py`, which was out of scope for that task. That is
closed; the interim hand-rolled path is gone.

## Threshold parameterisation — never a literal in model code

`DCThresholdProvenance` is the injected unit: a group's count threshold,
its points value, its source, its source date, and a `verified: bool`.
`DCThresholdSet` composes one `DCThresholdProvenance` per group with a
LIVE-read `points_by_position` (from `game_config`, always `verified=True`
by construction — it came off the API, not the press) and the derived
`is_eligible(position)` gate. Every fitting/predicting entry point
(`build_training_table`, `fit_dc_model`, `predict_dc_pmf`) takes a
`DCThresholdSet` as an explicit parameter, defaulting to
`build_dc_threshold_set(store)` (which itself defaults its COUNT
thresholds to `PRESS_DC_COUNT_THRESHOLDS`, not a bare 10/12 anywhere) —
never a module-level `THRESHOLD = 10` a caller cannot override. A caller
that has pinned the real threshold via `scripts/pin_dc_thresholds.py`
constructs its own `DCThresholdSet` with `verified=True` and passes it
straight through; nothing else in this module changes.

**A genuine asymmetry, worth stating plainly**: the POSITION gate
(GKP excluded) is machine-verified, HIGH confidence, and effectively
permanent (it would take a scoring-rule change, not a press cycle, to
move). The COUNT threshold (10/12) is press-only and could in principle
differ from an `overrides` block on a specific event (blueprint §11 already
flags this as unranked-out, not ruled out) — these are different kinds of
uncertainty and this module refuses to blur them into one flag.

## The team-style feature — the load-bearing modelling decision this task
## named directly (Elliot Anderson)

`docs/wiki/defensive-contribution.md` §4.3: *"DC is a team-style statistic
wearing a player's name... player identity is the SECOND input, not the
first."* This module takes that literally in its feature set, not just in
prose:

- `player_trailing_count_{3,5,10}` — the player's OWN trailing per-round
  count (raw, not per-90 normalised — see "Feature design notes" below).
  Second input, per Elite's ranking.
- `team_trailing_dc_mean_5` — the player's TEAM's trailing mean total
  `defensive_contribution` (FPL's own already-position-gated field, summed
  across every one of that team's rostered players, at ROUND grain, never
  gameweek-aggregated across a DGW — see `_build_team_round_rollup`) over
  its last 5 rounds. **First input.** This is a genuine team-*style*
  signal — "how much out-of-possession defensive work did this team's
  players collectively do recently" — built entirely from data already
  in-store (§2 of the wiki notes `team.match_stats@match`, the PL API's own
  possession%, has **zero rows in this store** — checked directly,
  `store.latest("pl_team_match_stats")` returns `(0, 0)` — so this is a
  documented, honest PROXY for possession share, not the real thing; see
  "Known limitation" below).

**Why a continuous team feature, not a team-name dummy** (the choice
`fplai.models.minutes`/`fplai.models.team_strength` both make instead):
a categorical team dummy is *estimable* for a transferred player exactly
the same way (Anderson tagged `team="Man City"` picks up Man City's own
fitted coefficient regardless of his personal history) — so that is not
the deciding factor. The deciding factor is **promoted teams**: DC
training is single-season-only (2025-26, no backfill — see "No pre-2025-26
backfill" below), so three of 2026/27's twenty clubs (Hull, Ipswich,
Coventry) have NO 2025-26 top-flight matches to estimate a dummy from at
all, and would need a `team_strength.py`-style promoted-team-prior
mechanism bolted on. A smooth numeric feature degrades gracefully instead
— `team_cold_start`/the feature's own null-to-zero fill (see
`_build_team_round_rollup`) is the SAME cold-start discipline
`fplai.models.minutes` already uses for a player with no prior rounds,
applied to a team with none, and needs no separate prior estimator.

### What this predicts for Elliot Anderson — the live demonstration

`scripts/fit_defensive_contribution.py --show-team-transfer-effect
"Elliot Anderson" "Nott'm Forest" "Man City"` runs the fitted 2025-26
model with Anderson's OWN `player_trailing_count_*` (real, Forest-era
history — cold-start is not what's being tested here) under TWO values of
`team_trailing_dc_mean_5`: Nottingham Forest's real 2025-26 trailing value
and Manchester City's. See this session's punch-out and
`docs/wiki/model-defensive-contribution.md` for the actual numbers this
produced against the real store — the qualitative claim this module is
built to support is that `P(DC awarded)` for the same player-features
should be materially LOWER under the City team-feature than the Forest
one; the exact magnitude is reported, not asserted here.

## Why Negative Binomial, not logistic regression on the threshold event

`docs/wiki/defensive-contribution.md` §3.2: *"DC is a THRESHOLD statistic
... governed by ... dispersion ... not the mean. A season rate ... carries
almost no information about a player's own match-to-match dispersion."*
A logistic/softmax classifier fit directly on `count >= threshold` (the
approach `fplai.models.minutes` takes for its START/SUB/UNUSED state) has
no way to express "same mean count, different dispersion" AT ALL — it
only ever sees the binary outcome, never the count that produced it. This
module instead fits a **Negative Binomial (NB2) regression** on the RAW
COUNT: `count ~ NegBinom(mu, alpha)`, `log(mu) = X @ beta + offset`,
`offset = log(minutes_this_fixture / 90)` (an EXPOSURE term, standard for
count-rate regression with a variable observation window — see "Minutes
interact multiplicatively" below), with `alpha` (dispersion, `var = mu +
alpha * mu^2`) fit as a SEPARATE, LEARNED parameter, not fixed to the
Poisson case (`alpha = 0`, `var = mu`). This is the direct mechanism that
lets two players/situations share a mean but differ in tail mass — the NB
distribution's shape genuinely varies with `alpha` in a way a Bernoulli
classifier's single probability parameter structurally cannot represent.
`P(count >= threshold)` is then a DERIVED quantity of the fitted count
distribution (`DCPMF.p_dc_awarded()`), never the fitting target itself —
the same "fit the richer quantity, derive the narrower one" relationship
`fplai.models.minutes` has between its 6-band PMF and `p_start()`.

One thing this does NOT solve, stated plainly per this task's brief:
`docs/wiki/defensive-contribution.md` §3.2 also flags that COMPONENTS
(tackles/CBI/recoveries) are positively correlated WITHIN a match, and
that summing independently-modelled components understates the upper
tail. This module sidesteps that specific failure mode by never modelling
components separately at all — it fits the ALREADY-SUMMED count directly
— but it does not attempt to model the correlation between a PLAYER's
match and his TEAMMATES' matches (e.g., two defenders on a team enduring
a siege both post high counts the same game) — that is squad-level
correlated Monte Carlo, blueprint §4.3/Phase 5's job, explicitly out of
scope for a single-player PMF module.

## Minutes interact multiplicatively — composed, not hardcoded

`docs/wiki/defensive-contribution.md` §3.2: *"DC has no 60-minute
qualifier and no pro-rating... only as good as the minutes distribution
feeding it... a genuine strength [that] should be exploited rather than
[DC] being built standalone."* This module is DECOUPLED from
`fplai.models.minutes` (no import — same independence
`fplai.models.team_strength` has from `fplai.models.minutes`), but
`predict_dc_pmf`'s `minute_exposure` parameter is built for exact
compatibility with a `MinutesPMF`: it accepts a full
`Sequence[(minutes_value, probability)]` mixture — NOT a single scalar
"expected minutes" (that would be exactly CLAUDE.md rule 5's "a scalar at
a module boundary" mistake, one layer up from `xPts`) — a caller with a
fitted `MinutesPMF` passes `[(0.0, p_state["UNUSED"] + band_prob_of_"0"),
(15.0, ...), (45.0, ...), (67.0, ...), (82.0, ...), (90.0, ...)]`, i.e.
the same six band midpoints `fplai.models.minutes._BAND_MIDPOINT` already
uses (documented here by value, not imported, to keep the two modules
independent). `predict_dc_pmf` computes ONE NB PMF per exposure level and
mixes them by their probability weight — this is what lets a "hooked at
60 vs plays 90" distinction (blueprint §4.1's own named case) propagate
into DC's tail probability instead of being flattened into one "expected
minutes" number that would understate variance exactly where minutes
model's own PMF discipline exists to prevent that.

## No pre-2025-26 backfill — Elite's recommendation, accepted

`docs/wiki/defensive-contribution.md` §3.3: one season of FPL-native
ground truth (29,747 player-fixture rows after dedup) is not small for a
per-match count model, and a multi-season backfill would buy rows measured
under a DIFFERENT provider's definitions for a rule that pays on FPL's
own counters — the definitional bias is unmeasured except in the one
season that does not need it. `build_training_table` therefore filters to
rows with a non-null `defensive_contribution` (the SAME "let the data say
which seasons qualify" discipline `fplai.models.minutes` uses for
`starts`, rather than hardcoding the season string "2025-26" anywhere) —
verified live: this is 2025-26 and only 2025-26 in this store today.

## Feature design notes

- **Raw trailing counts, not per-90 rates.** `fplai.models.minutes._build_
  round_rollup` sets the precedent: `trailing_minutes_mean_*` is a plain
  rolling mean of round-level minutes, not divided by anything. This
  module follows the same convention for `player_trailing_count_*` and
  `team_trailing_dc_mean_5` — a per-90 normalisation would need to decide
  how to treat a 0-minute round (exclude it from the rolling window
  entirely, versus include it as a real zero), and Polars' `rolling_mean`
  gives no clean, verified way to skip nulls mid-window without a second
  hand-rolled mechanism (checked, not assumed — a wrong null-skipping
  implementation here would be exactly the kind of subtly-wrong-and-green
  bug CLAUDE.md's lesson 5 warns about). The regression's OWN `offset`
  term (`log(minutes_this_fixture / 90)`) is what actually does the
  minutes-scaling job for THIS fixture; the trailing features are left as
  a raw "how much defensive output has this player/team produced lately"
  signal, which is what they are.
- **No team-name one-hot dummy** — see "The team-style feature" above.
- **`is_forward`** — a single 0/1 feature (never 1 for a `DEF_CBIT`-group
  row, since that group never contains an FWD row) distinguishing MID from
  FWD inside the shared `MID_FWD_CBIRT` group, since Elite's report (§4.2)
  flags forwards as "near-zero, occasional outlier" — a materially
  different profile from a defensive midfielder sharing the same 12-CBIRT
  threshold.

## Known limitation, stated rather than hidden

`team_trailing_dc_mean_5` is a proxy for possession share, built from this
project's own DC counters — not a directly observed possession%. The PL
API's `team.match_stats@match` capability (`docs/wiki/defensive-
contribution.md` §2.3's `ballRecovery`/`totalTackle`/etc. per team per
match) is DECLARED in `fplai.schemas` but has **zero rows in this store**
(checked directly: `store.latest("pl_team_match_stats")` → `(0, 0)` shape)
— ingesting it is out of this task's OWNED PATHS (`src/fplai/providers/**`
is READ-ONLY here) and is a real, named seam for a future session: it
would let this feature be built from an independent, directly-observed
possession signal instead of the DC counters' own history, closing a
mild circularity (the feature that predicts DC is itself built from past
DC).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import polars as pl
from scipy.optimize import minimize
from scipy.special import gammaln, digamma

from fplai.derived import CalibrationReference, DerivationInput, write_derived
from fplai.gameweek_stats import read_player_gameweek_stats
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    GAME_CONFIG_CURRENT,
    PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_DATASET,
    PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK,
    CANONICAL_SCHEMAS,
    GAME_DC_THRESHOLD_OBSERVATION_GAMEWEEK,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    CapabilityKey,
    FactTableSchema,
    register_derived_capability,
)
from fplai.store import BitemporalStore, WriteResult

logger = logging.getLogger(__name__)

DATASET = "vaastav_player_gameweek_stats"

REQUIRED_COLUMNS = (
    "season",
    "round",
    "element",
    "fixture",
    "kickoff_time",
    "minutes",
    "position",
    "team",
    "was_home",
    "tackles",
    "recoveries",
    "clearances_blocks_interceptions",
    "defensive_contribution",
)

# Verified composition rule (module docstring, "What is verified") — a
# football/rules fact, not a provenance-uncertain one; kept entirely
# separate from DCThresholdProvenance's COUNT values below.
DC_GROUPS: tuple[str, ...] = ("DEF_CBIT", "MID_FWD_CBIRT")
POSITION_GROUP: dict[str, str] = {"DEF": "DEF_CBIT", "MID": "MID_FWD_CBIRT", "FWD": "MID_FWD_CBIRT"}
GROUP_POSITIONS: dict[str, tuple[str, ...]] = {"DEF_CBIT": ("DEF",), "MID_FWD_CBIRT": ("MID", "FWD")}


class DCModelError(ValueError):
    """Misuse or a genuine data problem this module refuses to guess past —
    an ineligible position, a naive `as_of`, an empty training window, a
    PMF that would not (re)normalise, or a `game_config` payload missing
    the DC scoring block."""


def dc_component_count(position: str, tackles: int, cbi: int, recoveries: int) -> int:
    """The verified composition rule (module docstring) as a pure
    function — the single source of truth `_prepare_group_table`'s
    vectorised Polars expression is cross-checked against
    (`tests/test_defensive_contribution.py`), same relationship
    `fplai.models.minutes.minute_band`/`minutes_state` have to their own
    vectorised mirrors. Raises for a position with no CBIT/CBIRT formula
    at all (GK, or anything outside `POSITION_GROUP`) — the caller is
    expected to have already filtered those out via `DCThresholdSet.
    is_eligible`, exactly as `fplai.models.minutes` filters `position !=
    "AM"` before any state/band logic runs."""
    group = POSITION_GROUP.get(position)
    if group is None:
        raise DCModelError(
            f"position {position!r} has no CBIT/CBIRT formula (only "
            f"{sorted(POSITION_GROUP)} do) — filter ineligible positions "
            "out before calling dc_component_count."
        )
    if group == "DEF_CBIT":
        return int(tackles) + int(cbi)
    return int(tackles) + int(cbi) + int(recoveries)


# ---------------------------------------------------------------------------
# Threshold provenance — module docstring, "Threshold parameterisation"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DCThresholdProvenance:
    """One group's COUNT threshold, with full provenance. Never a bare
    literal at a call site — every consumer takes a `DCThresholdSet`
    (below), which is built FROM instances of this class, explicitly."""

    group: str
    count_threshold: int
    source: str
    source_date: str
    verified: bool
    verified_note: str = ""

    def __post_init__(self) -> None:
        if self.group not in DC_GROUPS:
            raise DCModelError(f"unknown DC group {self.group!r} — expected one of {DC_GROUPS}")
        if self.count_threshold <= 0:
            raise DCModelError(f"count_threshold must be positive, got {self.count_threshold}")
        if not self.source.strip():
            raise DCModelError("DCThresholdProvenance.source must not be empty — unauditable otherwise")


# Press-sourced defaults — docs/wiki/defensive-contribution.md §1.1,
# premierleague.com "What's happening with defensive contribution points
# in 2026/27 Fantasy?", published 20 Jul 2026, Tier HIGH-but-press (NOT
# machine-readable — verified absent from game_config, module docstring).
# `verified=False` on both: this is what makes the "unverified until
# pinned" state a DATA fact rather than a comment a caller could miss.
PRESS_DC_COUNT_THRESHOLDS: dict[str, DCThresholdProvenance] = {
    "DEF_CBIT": DCThresholdProvenance(
        group="DEF_CBIT",
        count_threshold=10,
        source=(
            "premierleague.com, \"What's happening with defensive contribution points "
            "in 2026/27 Fantasy?\", published 20 Jul 2026 — \"Any defender who reaches a "
            "combined total of 10 clearances, blocks, interceptions and tackles (CBIT) in "
            "a single match scores two FPL points.\""
        ),
        source_date="2026-07-20",
        verified=False,
    ),
    "MID_FWD_CBIRT": DCThresholdProvenance(
        group="MID_FWD_CBIRT",
        count_threshold=12,
        source=(
            "premierleague.com, \"What's happening with defensive contribution points "
            "in 2026/27 Fantasy?\", published 20 Jul 2026 — midfielders and forwards "
            '"require 12" including ball recoveries (CBIRT).'
        ),
        source_date="2026-07-20",
        verified=False,
    ),
}


def read_dc_points_by_position(store: BitemporalStore) -> dict[str, int]:
    """LIVE-read `scoring.defensive_contribution` from `game_config`
    (module docstring, "What is verified") — the position POINTS/GATE,
    machine-readable, `verified=True` by construction (it is read off the
    API, never off the press). Keys are FPL's own element-type short names
    as they appear in `game_config` (`"GKP"`, not this module's normalised
    `"GK"` — see `build_dc_threshold_set` for the remap, the same GKP/GK
    drift `fplai.models.minutes._normalise_and_filter_positions` documents
    for the `position` COLUMN, here hitting a different field with the
    same underlying cause)."""
    df = store.latest("game_config")
    if df.is_empty():
        raise DCModelError("game_config is empty in this store — cannot read the DC position gate live.")
    payload = json.loads(df["payload"][0])
    scoring = payload.get("scoring")
    if not isinstance(scoring, dict) or "defensive_contribution" not in scoring:
        raise DCModelError(
            "game_config payload has no scoring.defensive_contribution block — "
            f"payload keys: {sorted(payload.keys())}, scoring keys: "
            f"{sorted(scoring.keys()) if isinstance(scoring, dict) else scoring!r}"
        )
    points = scoring["defensive_contribution"]
    if not isinstance(points, dict) or not points:
        raise DCModelError(f"scoring.defensive_contribution is not a non-empty mapping: {points!r}")
    return {str(k): int(v) for k, v in points.items()}


@dataclass(frozen=True)
class DCThresholdSet:
    """The composed, injected parameter every fitting/predicting entry
    point in this module takes — module docstring, "Threshold
    parameterisation". `count_thresholds` carries the (possibly still
    unverified) COUNT per group; `points_by_position` carries the
    LIVE-read points/gate, keyed on THIS module's normalised position
    labels (`GK`/`DEF`/`MID`/`FWD`)."""

    count_thresholds: dict[str, DCThresholdProvenance]
    points_by_position: dict[str, int]

    def group_for_position(self, position: str) -> str | None:
        return POSITION_GROUP.get(position)

    def is_eligible(self, position: str) -> bool:
        return self.points_by_position.get(position, 0) > 0

    def points(self, position: str) -> int:
        return self.points_by_position.get(position, 0)

    def threshold(self, group: str) -> DCThresholdProvenance:
        if group not in self.count_thresholds:
            raise DCModelError(f"no DCThresholdProvenance registered for group {group!r}")
        return self.count_thresholds[group]


# ---------------------------------------------------------------------------
# Pinned-threshold observations (session s004) — module docstring, "Session
# s004 — the loop actually closes". Written by scripts/pin_dc_thresholds.py,
# resolved back here bitemporally by build_dc_threshold_set.
# ---------------------------------------------------------------------------

DC_THRESHOLD_OBSERVATION_DATASET = "dc_threshold_observations"

# The registered schema, imported rather than redeclared — see the module
# docstring. `.validate()`d on every write exactly as before.
DC_THRESHOLD_OBSERVATION_SCHEMA = CANONICAL_SCHEMAS[GAME_DC_THRESHOLD_OBSERVATION_GAMEWEEK]


def dc_threshold_observation_rows(
    results: dict[str, dict], *, season: str, round_: int
) -> pl.DataFrame:
    """Build the persistable row shape from `scripts/pin_dc_thresholds.
    pin_thresholds()`'s return value (one dict per DC_GROUPS entry, each
    carrying `count_threshold`/`lower_bound`/`upper_bound`/
    `n_observations`/`n_unattributable`/`contradiction`) — the single
    place this shape is defined, shared by the writer
    (`write_dc_threshold_observations`) and, by construction, the reader
    (`resolve_dc_threshold_observations` reads exactly these columns
    back), so the two can never drift apart."""
    missing_groups = [g for g in DC_GROUPS if g not in results]
    if missing_groups:
        raise DCModelError(
            f"results is missing group(s) {missing_groups} — expected exactly {DC_GROUPS}"
        )
    rows = []
    for group in DC_GROUPS:
        r = results[group]
        rows.append(
            {
                "season": season,
                "round": round_,
                "group": group,
                "lower_bound": r["lower_bound"],
                "upper_bound": r["upper_bound"],
                "count_threshold": r["count_threshold"],
                "n_observations": r["n_observations"],
                "n_unattributable": r["n_unattributable"],
                "contradiction": r["contradiction"],
            }
        )
    df = pl.DataFrame(
        rows,
        schema={
            "season": pl.Utf8,
            "round": pl.Int64,
            "group": pl.Utf8,
            "lower_bound": pl.Int64,
            "upper_bound": pl.Int64,
            "count_threshold": pl.Int64,
            "n_observations": pl.Int64,
            "n_unattributable": pl.Int64,
            "contradiction": pl.Boolean,
        },
    )
    DC_THRESHOLD_OBSERVATION_SCHEMA.validate(df)
    return df


def write_dc_threshold_observations(
    store: BitemporalStore,
    results: dict[str, dict],
    *,
    season: str,
    round_: int,
    observed_at: datetime,
    source: str = "scripts/pin_dc_thresholds.py",
    skip_if_unchanged: bool = True,
) -> WriteResult:
    """Persist one pin ATTEMPT (both groups, one row each) to
    `DC_THRESHOLD_OBSERVATION_DATASET`. `valid_at == observed_at`: this is
    a snapshot-style observation ("we observed this at this instant"),
    the same convention `fplai.schemas.JOB_HEARTBEAT_RUN`-backed writes
    use, not a per-row domain valid time."""
    df = dc_threshold_observation_rows(results, season=season, round_=round_)
    return store.write(
        DC_THRESHOLD_OBSERVATION_DATASET,
        df,
        valid_at=observed_at,
        observed_at=observed_at,
        source=source,
        skip_if_unchanged=skip_if_unchanged,
    )


def resolve_dc_threshold_observations(
    store: BitemporalStore, *, as_of: datetime
) -> dict[str, DCThresholdProvenance]:
    """Resolve each DC group's count threshold from real pinned
    observations, AS OF `as_of` — CLAUDE.md rule 2, bitemporal: a pin
    written to the store AFTER `as_of` must never be visible to a query
    resolved at `as_of`, exactly the discipline `build_training_table`'s
    own `as_of` cutoff already applies to every other input this module
    reads. Returns a dict with an entry ONLY for groups that have at
    least one PINNED (non-null `count_threshold`) observation visible by
    `as_of` — a group with none is simply absent; the caller
    (`build_dc_threshold_set`) fills the gap from `PRESS_DC_COUNT_
    THRESHOLDS`, `verified=False`.

    Uses `BitemporalStore.observations()`, never `as_of()`/`effective_at()`
    — a deliberate POLICY choice, not a consequence of registration (the
    dataset IS registered; see the module docstring). `as_of()` collapses
    to the latest row per entity key, and the entity key here is (season,
    group, round) — that would hand back the latest ROUND's row whether or
    not it pinned anything. What this function needs is the latest row
    that actually PINNED a value, so an inconclusive later gameweek cannot
    silently withdraw an earlier real pin. That collapse is a different
    one, so it is done here by hand over the raw stream.

    Resolution policy per group, among rows with a non-null `count_
    threshold`: the CHRONOLOGICALLY LATEST one, ordered on (season,
    round, observed_at) — so a genuine mid-season rule change (a LATER
    round pinning a DIFFERENT value) is picked up, while a LATER round
    that came back inconclusive (`count_threshold IS NULL`) never
    silently withdraws an earlier real pin."""
    if as_of.tzinfo is None:
        raise DCModelError(f"as_of must be timezone-aware (got a naive datetime: {as_of!r})")
    raw = store.observations(DC_THRESHOLD_OBSERVATION_DATASET, until=as_of)
    if raw.is_empty():
        return {}
    pinned = raw.filter(pl.col("count_threshold").is_not_null())
    if pinned.is_empty():
        return {}
    resolved: dict[str, DCThresholdProvenance] = {}
    for group in DC_GROUPS:
        group_rows = pinned.filter(pl.col("group") == group).sort(["season", "round", "observed_at"])
        if group_rows.is_empty():
            continue
        row = group_rows.row(-1, named=True)
        observed_date = str(row["observed_at"])[:10]
        resolved[group] = DCThresholdProvenance(
            group=group,
            count_threshold=int(row["count_threshold"]),
            source=(
                "pinned by direct observation, scripts/pin_dc_thresholds.py "
                f"(event/{{gw}}/live/ bounds-meeting), season={row['season']!r} "
                f"round={row['round']}, n_observations={row['n_observations']}, "
                f"lower_bound={row['lower_bound']}, upper_bound={row['upper_bound']}"
            ),
            source_date=observed_date,
            verified=True,
            verified_note=(
                f"bounds met exactly this gameweek: max(count, DC not awarded)="
                f"{row['upper_bound']}, min(count, awarded)={row['lower_bound']}"
            ),
        )
    return resolved


def build_dc_threshold_set(
    store: BitemporalStore,
    *,
    as_of: datetime | None = None,
    count_thresholds: dict[str, DCThresholdProvenance] | None = None,
) -> DCThresholdSet:
    """Compose a `DCThresholdSet`: LIVE points/gate from `game_config` +
    the count thresholds. Pass a caller-built `count_thresholds` (e.g. a
    hand-fabricated dict in a test) to override EVERYTHING below —
    `as_of` is then ignored entirely, exactly the pre-s004 behaviour.

    Session s004: when `count_thresholds` is NOT passed, this starts from
    `PRESS_DC_COUNT_THRESHOLDS` (`verified=False`, as always) and, if
    `as_of` is given, OVERLAYS any real pinned observation visible by that
    deadline (`resolve_dc_threshold_observations`, `verified=True`) on a
    PER-GROUP basis — a group with no pin yet keeps its press default,
    exactly as before. `as_of=None` (the old, only, default) keeps the
    original press-only behaviour unchanged — this is a pure addition for
    every caller that already worked before this session existed."""
    raw_points = read_dc_points_by_position(store)
    # game_config's own key is "GKP"; every other position label already
    # matches this module's normalised vocabulary ("DEF"/"MID"/"FWD") —
    # verified live, 2026-08-22. Remapped explicitly, not silently assumed
    # to already agree, mirroring fplai.models.minutes' own GKP->GK fix.
    points_by_position = {("GK" if k == "GKP" else k): v for k, v in raw_points.items()}
    if count_thresholds is None:
        count_thresholds = dict(PRESS_DC_COUNT_THRESHOLDS)
        if as_of is not None:
            count_thresholds.update(resolve_dc_threshold_observations(store, as_of=as_of))
    missing_groups = [g for g in DC_GROUPS if g not in count_thresholds]
    if missing_groups:
        raise DCModelError(f"count_thresholds is missing group(s) {missing_groups} — expected all of {DC_GROUPS}")
    for group, positions in GROUP_POSITIONS.items():
        group_points = {p: points_by_position.get(p) for p in positions}
        if any(v is None for v in group_points.values()):
            raise DCModelError(
                f"game_config's scoring.defensive_contribution is missing point value(s) "
                f"for {group!r}'s position(s) {positions} — got {group_points}"
            )
        distinct = set(group_points.values())
        if len(distinct) > 1:
            raise DCModelError(
                f"group {group!r}'s positions {positions} have DIFFERENT DC point values "
                f"({group_points}) — this module assumes one shared points value per group "
                "(true for every position checked live, 2026-08-22: DEF=FWD=MID=2); a season "
                "where this no longer holds needs a design change, not a silent average."
            )
    return DCThresholdSet(count_thresholds=count_thresholds, points_by_position=points_by_position)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DCModelConfig:
    """Every fitting hyperparameter, in one place — same convention
    `fplai.models.minutes.MinutesModelConfig`/`fplai.models.team_strength.
    TeamStrengthConfig` establish. Defaults are stated, reasoned starting
    points, not calibrated optima (same status those two modules' defaults
    carry) — see `docs/wiki/model-defensive-contribution.md` for the
    Phase 2 gate's real numbers against these defaults."""

    player_trailing_windows: tuple[int, ...] = (3, 5, 10)
    team_trailing_window: int = 5
    l2_penalty: float = 1.0
    """Ridge penalty on every NB regression's beta vector — same dual
    purpose `fplai.models.minutes.MinutesModelConfig.l2_penalty` documents
    (standard regularisation; here there is no gauge freedom to resolve
    since there is no categorical dummy encoding, so this is purely
    regularisation).

    **Its meaning changed session `s005`**: `_nb_neg_log_lik_and_grad`'s
    ridge term is now scaled by `1/n`, matching `fplai.models.saves`'s own
    already-corrected copy of this formula — see that function's docstring
    for the bug and the measured before/after on both DC groups. `1.0`
    still clears the §7.1 gate with room at every point of a swept
    0.01-100 grid, but is NOT re-tuned here for calibration slope, which
    the corrected formula pulls in the OPPOSITE direction from where the
    unscaled bug left it (overshoots past 1.0 rather than staying
    compressed toward it) — an open question for a future calibration
    pass, not resolved by this fix. See `docs/wiki/model-defensive-
    contribution.md`."""

    log_r_bounds: tuple[float, float] = (-5.0, 8.0)
    """Bounds on `log(r)` where `r = 1/alpha` is the NB dispersion's
    "size" parameter (`var = mu + mu^2/r`) — `r=exp(-5)≈0.0067` is heavy
    overdispersion, `r=exp(8)≈2981` is Poisson-indistinguishable at this
    dataset's count scale. Bounds exist only to keep L-BFGS-B away from a
    numerically degenerate region during early iterations; the fitted
    value for both groups on the real 2025-26 data (see
    `docs/wiki/model-defensive-contribution.md`) sits well inside them."""

    max_lbfgs_iterations: int = 300
    max_count: int = 25
    """PMF truncation for `predict_dc_pmf`'s output support (`0..max_count`
    inclusive) — same truncate-and-renormalise-and-track convention
    `fplai.models.team_strength.ScorelinePMF` uses for `max_goals`. Real
    observed maxima in this store (module docstring): DEF 23, MID/FWD 29 —
    25 covers the DEF case exactly and leaves a small, tracked
    (`mass_before_truncation`) tail for MID/FWD's rare 26-29 outliers."""


# ---------------------------------------------------------------------------
# Raw-row normalisation — same two verified drift cases
# fplai.models.minutes._normalise_and_filter_positions documents for this
# exact dataset, duplicated here (not imported) to keep this module
# independent, per its own "no cross-model import" convention.
# ---------------------------------------------------------------------------


def _normalise_and_filter_positions(raw: pl.DataFrame) -> pl.DataFrame:
    if "position" not in raw.columns:
        return raw
    raw = raw.filter(pl.col("position") != "AM")
    return raw.with_columns(
        pl.when(pl.col("position") == "GKP")
        .then(pl.lit("GK"))
        .otherwise(pl.col("position"))
        .alias("position")
    )


# ---------------------------------------------------------------------------
# Per-group per-fixture target table
# ---------------------------------------------------------------------------


def _prepare_group_table(raw: pl.DataFrame, threshold_set: DCThresholdSet, group: str) -> pl.DataFrame:
    """One row per (season, round, element, fixture) for positions in
    `group`, with `count` (the verified composition rule, vectorised) and
    `awarded` (`count >= threshold_set.threshold(group).count_threshold`)."""
    positions = GROUP_POSITIONS[group]
    sub = raw.filter(pl.col("position").is_in(list(positions)))
    if group == "DEF_CBIT":
        count_expr = pl.col("tackles") + pl.col("clearances_blocks_interceptions")
    else:
        count_expr = pl.col("tackles") + pl.col("clearances_blocks_interceptions") + pl.col("recoveries")
    threshold = threshold_set.threshold(group).count_threshold
    return sub.with_columns(
        count_expr.cast(pl.Int64).alias("count"),
        pl.lit(group).alias("group"),
        (pl.col("position") == "FWD").alias("is_forward"),
    ).with_columns((pl.col("count") >= pl.lit(threshold)).alias("awarded"))


# ---------------------------------------------------------------------------
# Trailing feature rollups — round grain, leakage-safe (module docstring,
# "Feature design notes"; mirrors fplai.models.minutes._build_round_rollup
# exactly in its double-gameweek discipline).
# ---------------------------------------------------------------------------


def _build_team_round_rollup(all_eligible: pl.DataFrame, config: DCModelConfig) -> pl.DataFrame:
    """Team-style feature, built ONCE from every DC-eligible position's
    rows (not restricted to one group — a defender's team-style signal
    must include the team's midfielders'/forwards' contributions too, and
    vice versa). See module docstring, "The team-style feature"."""
    team_round = (
        all_eligible.group_by(["season", "team", "round"])
        .agg(pl.col("defensive_contribution").sum().alias("team_round_dc_total"))
        .sort(["season", "team", "round"])
    )
    w = config.team_trailing_window
    team_round = team_round.with_columns(
        pl.col("team_round_dc_total")
        .cast(pl.Float64)
        .shift(1)
        .rolling_mean(window_size=w, min_samples=1)
        .over(["season", "team"])
        .alias(f"team_trailing_dc_mean_{w}"),
        pl.col("team_round_dc_total").shift(1).over(["season", "team"]).is_null().alias("team_cold_start"),
    )
    return team_round.with_columns(pl.col(f"team_trailing_dc_mean_{w}").fill_null(0.0)).select(
        ["season", "team", "round", f"team_trailing_dc_mean_{w}", "team_cold_start"]
    )


def _build_player_round_rollup(group_table: pl.DataFrame, threshold: int, config: DCModelConfig) -> pl.DataFrame:
    rollup = (
        group_table.group_by(["season", "element", "round"])
        .agg(
            pl.col("count").sum().alias("round_count"),
            pl.col("minutes").sum().alias("round_minutes"),
        )
        .sort(["season", "element", "round"])
    )
    appeared = (pl.col("round_minutes") > 0).cast(pl.Float64)
    round_awarded = (pl.col("round_count") >= pl.lit(threshold)).cast(pl.Float64)

    trailing_exprs = []
    for w in config.player_trailing_windows:
        trailing_exprs.append(
            pl.col("round_count")
            .cast(pl.Float64)
            .shift(1)
            .rolling_mean(window_size=w, min_samples=1)
            .over(["season", "element"])
            .alias(f"player_trailing_count_{w}")
        )
    trailing_exprs.append(
        appeared.shift(1).fill_null(0.0).cum_sum().over(["season", "element"]).alias("games_played_this_season")
    )
    # Baseline-only feature (never fed to the NB regression itself — see
    # walk_forward_validate) mirroring fplai.models.minutes' own
    # prev_round_any_start: this player's own trailing AWARDED rate,
    # kept nullable so a genuinely cold-start row is visibly absent.
    trailing_exprs.append(
        round_awarded.shift(1)
        .rolling_mean(window_size=5, min_samples=1)
        .over(["season", "element"])
        .alias("player_trailing_awarded_rate_5")
    )

    rollup = rollup.with_columns(trailing_exprs)
    fill_zero = [f"player_trailing_count_{w}" for w in config.player_trailing_windows]
    rollup = rollup.with_columns([pl.col(c).fill_null(0.0) for c in fill_zero])
    rollup = rollup.with_columns((pl.col("games_played_this_season") == 0).alias("cold_start"))
    return rollup


NUMERIC_FEATURE_COLUMNS_DC: tuple[str, ...] = (
    "player_trailing_count_3",
    "player_trailing_count_5",
    "player_trailing_count_10",
    "team_trailing_dc_mean_5",
    "games_played_this_season",
    "cold_start",
    "team_cold_start",
    "was_home",
    "is_forward",
)


def build_training_table(
    store: BitemporalStore,
    *,
    as_of: datetime,
    threshold_set: DCThresholdSet | None = None,
    seasons: Sequence[str] | None = None,
    config: DCModelConfig = DCModelConfig(),
    allow_live_season: bool = False,
) -> pl.DataFrame:
    """The full leakage-safe feature+target table, BOTH groups combined
    (filter on `group` to fit/evaluate one at a time). One row per
    (season, round, element, fixture) for DC-eligible positions only.

    Reads via the capability reader on `kickoff_time`, never
    `store.as_of()` — the exact same bitemporal-primitive reasoning
    `fplai.models.minutes.build_training_table`'s module docstring already
    proves for this dataset (bulk-ingested archive, `observed_at` ~= ingest
    time regardless of the row's real season/gameweek).
    """
    if threshold_set is None:
        threshold_set = build_dc_threshold_set(store, as_of=as_of)
    if as_of.tzinfo is None:
        raise DCModelError(f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2.")

    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise DCModelError(
            f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()}"
        )

    # Decision 1: live-season contamination — filter with WARNING log by default
    if not allow_live_season:
        live_rows = raw.filter(pl.col("source_provider") == "fpl_api")
        if not live_rows.is_empty():
            n_dropped = live_rows.height
            seasons_dropped = sorted(live_rows["season"].unique().to_list())
            logger.warning(
                f"excluding {n_dropped} FPL-API-sourced row(s) from season(s) {seasons_dropped} "
                f"(allow_live_season=False). Set allow_live_season=True to include them."
            )
            raw = raw.filter(pl.col("source_provider") != "fpl_api")

    # Decision 2: attribution_complete=False rows never train (degraded DGW rows)
    raw = raw.filter((pl.col("attribution_complete") == True) | pl.col("attribution_complete").is_null())
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise DCModelError(f"player gameweek stats is missing required column(s) {missing}")

    raw = _normalise_and_filter_positions(raw)
    if seasons is not None:
        seasons = tuple(seasons)
        raw = raw.filter(pl.col("season").is_in(list(seasons)))

    # No pre-2025-26 backfill (module docstring) — let the data say which
    # seasons carry real DC counters, never hardcode "2025-26" as a string.
    raw = raw.filter(pl.col("defensive_contribution").is_not_null())
    if raw.is_empty():
        raise DCModelError(
            "no rows with a non-null defensive_contribution in the requested window — "
            "this store only carries DC counters for 2025-26 (verified live; module docstring)."
        )

    eligible_positions = [p for p in ("DEF", "MID", "FWD") if threshold_set.is_eligible(p)]
    all_eligible = raw.filter(pl.col("position").is_in(eligible_positions))
    if all_eligible.is_empty():
        raise DCModelError("no DC-eligible position rows remain — check DCThresholdSet.points_by_position")

    team_rollup = _build_team_round_rollup(all_eligible, config)
    team_col = f"team_trailing_dc_mean_{config.team_trailing_window}"

    group_tables: list[pl.DataFrame] = []
    for group in DC_GROUPS:
        threshold = threshold_set.threshold(group).count_threshold
        group_table = _prepare_group_table(all_eligible, threshold_set, group)
        if group_table.is_empty():
            continue
        player_rollup = _build_player_round_rollup(group_table, threshold, DCModelConfig(player_trailing_windows=config.player_trailing_windows))
        trailing_cols = [f"player_trailing_count_{w}" for w in config.player_trailing_windows] + [
            "games_played_this_season",
            "cold_start",
            "player_trailing_awarded_rate_5",
        ]
        merged = group_table.join(
            player_rollup.select(["season", "element", "round", *trailing_cols]),
            on=["season", "element", "round"],
            how="left",
        )
        merged = merged.join(team_rollup, on=["season", "team", "round"], how="left")
        merged = merged.with_columns(
            pl.col(team_col).fill_null(0.0),
            pl.col("team_cold_start").fill_null(True),
        )
        group_tables.append(merged)

    if not group_tables:
        raise DCModelError("no group tables produced — nothing to train on")
    table = pl.concat(group_tables, how="diagonal_relaxed")

    missing_after_join = [c for c in NUMERIC_FEATURE_COLUMNS_DC if c in table.columns and table[c].null_count() > 0]
    if missing_after_join:
        raise DCModelError(
            f"unexpected NULLs after joining trailing features: {missing_after_join} — a row failed "
            "to match its own rollup, which should be structurally impossible."
        )

    season_order = {s: i for i, s in enumerate(sorted(table["season"].unique().to_list()))}
    table = table.with_columns(
        (pl.col("season").replace_strict(season_order, return_dtype=pl.Int64) * 100 + pl.col("round")).alias(
            "_chronological_rank"
        )
    )
    return table


# ---------------------------------------------------------------------------
# Feature spec + design matrix (no categorical dummy — module docstring,
# "No team-name one-hot dummy")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DCFeatureSpec:
    numeric_columns: tuple[str, ...] = NUMERIC_FEATURE_COLUMNS_DC

    @property
    def feature_names(self) -> tuple[str, ...]:
        return ("intercept",) + self.numeric_columns

    @property
    def n_features(self) -> int:
        return len(self.feature_names)


def _design_matrix_dc(table: pl.DataFrame, spec: DCFeatureSpec) -> np.ndarray:
    n = table.height
    cols: list[np.ndarray] = [np.ones(n, dtype=np.float64)]
    for c in spec.numeric_columns:
        series = table[c]
        cols.append(series.cast(pl.Float64).to_numpy())
    return np.column_stack(cols)


def _feature_row_to_vector(feature_row: dict, spec: DCFeatureSpec) -> np.ndarray:
    missing = [c for c in spec.numeric_columns if c not in feature_row]
    if missing:
        raise DCModelError(f"feature_row is missing required feature(s) {missing}")
    return np.array([1.0] + [float(feature_row[c]) for c in spec.numeric_columns], dtype=np.float64)


# ---------------------------------------------------------------------------
# Negative Binomial (NB2) regression — deterministic L-BFGS-B, analytic
# gradient (CLAUDE.md rule 7: no randomness anywhere in this fit).
# ---------------------------------------------------------------------------


def _nb_neg_log_lik_and_grad(
    params: np.ndarray, X: np.ndarray, y: np.ndarray, offset: np.ndarray, n_features: int, l2: float
) -> tuple[float, np.ndarray]:
    """NB2 parametrisation: `mu = exp(X @ beta + offset)`, `r = exp(log_r)`
    (`alpha = 1/r`, `var = mu + mu**2 / r`). `params = [beta..., log_r]`.

    Derivation (also checked numerically,
    `tests/test_defensive_contribution.py::
    test_nb_neg_log_lik_gradient_matches_finite_differences`):
    `dL/d(eta) = r*(y-mu)/(mu+r)` where `eta = log(mu)` — the standard NB2
    GLM score, so `d(beta)`'s gradient is `X.T @ [r*(y-mu)/(mu+r)] / n`.
    `dL/dr = digamma(y+r) - digamma(r) + log(r) - log(r+mu) + 1 - (r+y)/(r+mu)`,
    chained through `dr/d(log_r) = r`.

    **L2 scaling fixed session `s005`** — same defect `fplai.models.saves`'s
    own duplicated copy of this exact formula found and fixed first (that
    module's docstring has the full derivation of why an unscaled `l2`
    against a per-row-averaged likelihood is effectively `l2*n`): the ridge
    term is now scaled by the same `1/n` the log-likelihood already carries,
    in both the loss and the gradient. Measured walk-forward on the real
    store, within 2025-26, same `l2_penalty=1.0` default, before/after:
    `DEF_CBIT` log-loss 0.4523->0.4272 (-5.5%), Brier 0.1528->0.1433 (-6.2%);
    `MID_FWD_CBIRT` log-loss 0.2395->0.2140 (-10.6%), Brier 0.0730->0.0667
    (-8.6%). `beats_both_baselines()` was `True` before AND after at every
    point of a swept `l2` grid (0.01/0.1/1.0/10/100) for both groups — the
    §7.1 gate verdict never changed. **Unlike `fplai.models.minutes`, this
    is not a clean win on every axis**: the calibration slope OVERSHOOTS in
    the opposite direction at the unchanged `l2=1.0` (`DEF_CBIT` 0.677 ->
    1.262; `MID_FWD_CBIRT` 0.909 -> 1.189), and the implied-mean-vs-
    empirical-count diagnostic moves the same way (`DEF_CBIT` +0.134 ->
    -0.208). Both move back toward the pre-fix numbers as `l2` is swept
    upward under the corrected formula, at some cost to log-loss/Brier — no
    single `l2` in the swept grid is simultaneously best on scoring-rule and
    calibration-slope grounds for both groups at once. `docs/wiki/model-
    defensive-contribution.md` has the full table; this is flagged there as
    an open Phase-7 calibration question, not resolved by this fix, which
    corrects the scaling defect only."""
    beta = params[:n_features]
    log_r = params[n_features]
    r = math.exp(log_r)
    eta = np.clip(X @ beta + offset, -30.0, 30.0)
    mu = np.exp(eta)
    n = X.shape[0]

    ll = gammaln(y + r) - gammaln(r) - gammaln(y + 1.0) + r * np.log(r / (r + mu)) + y * np.log(mu / (r + mu))
    nll = -float(np.sum(ll)) / n + (l2 / n) * float(np.sum(beta * beta))

    d_eta = r * (y - mu) / (mu + r)
    grad_beta = -(X.T @ d_eta) / n + 2.0 * (l2 / n) * beta

    dL_dr = digamma(y + r) - digamma(r) + np.log(r) - np.log(r + mu) + 1.0 - (r + y) / (r + mu)
    grad_log_r = -float(np.sum(dL_dr)) / n * r

    grad = np.concatenate([grad_beta, [grad_log_r]])
    return nll, grad


def _fit_negative_binomial(X: np.ndarray, y: np.ndarray, offset: np.ndarray, config: DCModelConfig) -> tuple[np.ndarray, float]:
    n_features = X.shape[1]
    x0 = np.zeros(n_features + 1, dtype=np.float64)
    x0[-1] = 1.0  # log_r=1.0 -> r~2.72, a data-independent, deterministic interior start.
    bounds = [(None, None)] * n_features + [config.log_r_bounds]
    result = minimize(
        _nb_neg_log_lik_and_grad,
        x0,
        args=(X, y, offset, n_features, config.l2_penalty),
        jac=True,
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": config.max_lbfgs_iterations},
    )
    if not result.success and result.status not in (0, 1):
        logger.warning("defensive_contribution model: NB fit did not converge cleanly: %s", result.message)
    beta = result.x[:n_features]
    log_r = float(result.x[n_features])
    return beta, log_r


def _nb_pmf(mu: float, r: float, max_count: int) -> tuple[np.ndarray, float]:
    """`P(Y=k)` for `k=0..max_count`, truncated and renormalised —
    same convention `fplai.models.team_strength.ScorelinePMF` uses for
    `max_goals`. `mu<=0` is a degenerate spike at 0 (an exposure=0 fixture
    — see `predict_dc_pmf`)."""
    if mu <= 0:
        pmf = np.zeros(max_count + 1, dtype=np.float64)
        pmf[0] = 1.0
        return pmf, 1.0
    p = r / (r + mu)
    k = np.arange(0, max_count + 1, dtype=np.float64)
    log_pmf = gammaln(k + r) - gammaln(r) - gammaln(k + 1.0) + r * math.log(p) + k * math.log(1.0 - p)
    pmf = np.exp(log_pmf)
    mass = float(pmf.sum())
    if mass <= 0.0:
        raise DCModelError(f"NB PMF has zero mass within max_count={max_count} for mu={mu}, r={r}")
    return pmf / mass, mass


# ---------------------------------------------------------------------------
# Fitted params
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class DCGroupParams:
    group: str
    feature_spec: DCFeatureSpec
    beta: np.ndarray
    log_r: np.ndarray  # 0-d, kept as array for eq=False consistency with MinutesModelParams' convention
    threshold: DCThresholdProvenance
    points: int
    config: DCModelConfig
    n_rows_used: int

    @property
    def r(self) -> float:
        return float(math.exp(float(self.log_r)))


@dataclass(frozen=True)
class DCModelParams:
    groups: dict[str, DCGroupParams]
    threshold_set: DCThresholdSet
    as_of: datetime
    seasons_used: tuple[str, ...]


def _fit_groups_from_table(
    table: pl.DataFrame, *, threshold_set: DCThresholdSet, config: DCModelConfig
) -> tuple[dict[str, DCGroupParams], tuple[str, ...]]:
    """The actual per-group NB fit, factored out of `fit_dc_model` so a
    test can fit directly against a hand-fabricated table (no store, no
    `as_of`) — mirrors `fplai.models.minutes._fit_from_table`'s role.
    Returns `(groups, seasons_used)`; `fit_dc_model` below is the only
    place `DCModelParams.as_of` is assembled, from the real `as_of` it was
    called with, never fabricated here."""
    spec = DCFeatureSpec()
    groups: dict[str, DCGroupParams] = {}
    for group in DC_GROUPS:
        group_table = table.filter((pl.col("group") == group) & (pl.col("minutes") > 0))
        if group_table.is_empty():
            # Same explicit-fallback philosophy fplai.models.minutes.
            # _fit_band_conditional_or_uniform documents: a group with no
            # rows in THIS table (a small synthetic/test table, or a very
            # early walk-forward fold) is an honest, loggable absence, not
            # a reason to refuse fitting the OTHER group that does have
            # data. Only raise below if BOTH groups end up empty.
            logger.warning(
                "defensive_contribution: zero minutes>0 rows for group %s in this training "
                "table -- skipping this group's fit (not raising) so the other group, if it "
                "has data, still fits.",
                group,
            )
            continue
        X = _design_matrix_dc(group_table, spec)
        y = group_table["count"].cast(pl.Float64).to_numpy()
        offset = np.log(group_table["minutes"].cast(pl.Float64).to_numpy() / 90.0)
        beta, log_r = _fit_negative_binomial(X, y, offset, config)
        threshold_prov = threshold_set.threshold(group)
        groups[group] = DCGroupParams(
            group=group,
            feature_spec=spec,
            beta=beta,
            log_r=np.array(log_r),
            threshold=threshold_prov,
            points=threshold_set.points_by_position[GROUP_POSITIONS[group][0]],
            config=config,
            n_rows_used=group_table.height,
        )
    if not groups:
        raise DCModelError("no minutes>0 rows for ANY DC group in this training table -- nothing to fit")
    seasons_used = tuple(sorted(table["season"].unique().to_list()))
    return groups, seasons_used


def fit_dc_model(
    store: BitemporalStore,
    *,
    as_of: datetime,
    threshold_set: DCThresholdSet | None = None,
    seasons: Sequence[str] | None = None,
    config: DCModelConfig = DCModelConfig(),
) -> DCModelParams:
    if threshold_set is None:
        threshold_set = build_dc_threshold_set(store, as_of=as_of)
    table = build_training_table(store, as_of=as_of, threshold_set=threshold_set, seasons=seasons, config=config)
    groups, seasons_used = _fit_groups_from_table(table, threshold_set=threshold_set, config=config)
    return DCModelParams(groups=groups, threshold_set=threshold_set, as_of=as_of, seasons_used=seasons_used)


# ---------------------------------------------------------------------------
# PMF + prediction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DCPMF:
    """The model's actual output for one player-fixture: a full count PMF
    over `0..max_count` — never a scalar (CLAUDE.md rule 5). `p_dc_awarded
    ()`/`expected_dc_points()` are convenience properties computed FROM
    this PMF, never a substitute for it — same relationship
    `fplai.models.minutes.MinutesPMF.p_start()` has to its own joint PMF."""

    element: int
    fixture: int
    group: str
    eligible: bool
    count_threshold: int | None
    threshold_verified: bool
    threshold_source: str
    points: int
    counts: tuple[int, ...]
    probabilities: tuple[float, ...]
    mass_before_truncation: float

    def __post_init__(self) -> None:
        if len(self.counts) != len(self.probabilities):
            raise DCModelError("counts and probabilities must be the same length")
        total = sum(self.probabilities)
        if abs(total - 1.0) > 1e-6:
            raise DCModelError(f"DCPMF probabilities do not sum to 1.0 (got {total}) for element={self.element}")

    def p_dc_awarded(self) -> float:
        if not self.eligible or self.count_threshold is None:
            return 0.0
        return sum(p for c, p in zip(self.counts, self.probabilities) if c >= self.count_threshold)

    def expected_count(self) -> float:
        return sum(c * p for c, p in zip(self.counts, self.probabilities))

    def expected_dc_points(self) -> float:
        return self.p_dc_awarded() * self.points

    def to_polars(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "element": [self.element] * len(self.counts),
                "fixture": [self.fixture] * len(self.counts),
                "count": list(self.counts),
                "probability": list(self.probabilities),
                "group": [self.group] * len(self.counts),
                "count_threshold": [self.count_threshold] * len(self.counts),
                "threshold_verified": [self.threshold_verified] * len(self.counts),
                "points": [self.points] * len(self.counts),
            }
        )


def predict_dc_pmf(
    params: DCModelParams,
    feature_row: dict,
    *,
    element: int,
    fixture: int,
    position: str,
    minute_exposure: Sequence[tuple[float, float]],
) -> DCPMF:
    """`feature_row` must carry every column in `DCFeatureSpec.
    numeric_columns` (never `minutes` itself — that is supplied via
    `minute_exposure`, module docstring "Minutes interact
    multiplicatively"). `minute_exposure` is a `[(minutes_value,
    probability), ...]` mixture, e.g. the six
    `fplai.models.minutes.MINUTE_BANDS` midpoints and their PMF weight
    from a fitted `MinutesPMF` — must sum to 1.0.

    A position outside `params.threshold_set.points_by_position` (or with
    zero points there — GKP) returns a DEGENERATE PMF (`eligible=False`,
    spike at count=0, `p_dc_awarded()==0` structurally, not by convention)
    rather than raising — a caller iterating a full squad including its
    goalkeepers should not need to special-case them."""
    total_weight = sum(w for _, w in minute_exposure)
    if abs(total_weight - 1.0) > 1e-6:
        raise DCModelError(f"minute_exposure probabilities must sum to 1.0, got {total_weight}")

    threshold_set = params.threshold_set
    if not threshold_set.is_eligible(position):
        return DCPMF(
            element=element,
            fixture=fixture,
            group="INELIGIBLE",
            eligible=False,
            count_threshold=None,
            threshold_verified=True,  # the GATE is machine-verified (game_config), even though counts aren't
            threshold_source="game_config scoring.defensive_contribution (position gate, live-read)",
            points=0,
            counts=(0,),
            probabilities=(1.0,),
            mass_before_truncation=1.0,
        )

    group = threshold_set.group_for_position(position)
    if group is None or group not in params.groups:
        raise DCModelError(f"position {position!r} is eligible but has no fitted DCGroupParams (group={group!r})")
    group_params = params.groups[group]
    spec = group_params.feature_spec
    x = _feature_row_to_vector(feature_row, spec)
    r = group_params.r
    max_count = group_params.config.max_count

    mixture = np.zeros(max_count + 1, dtype=np.float64)
    mass_before = 0.0
    for minutes_value, weight in minute_exposure:
        if minutes_value <= 0:
            mixture[0] += weight
            mass_before += weight
            continue
        eta = float(x @ group_params.beta) + math.log(minutes_value / 90.0)
        mu = math.exp(min(eta, 30.0))
        pmf_k, mass = _nb_pmf(mu, r, max_count)
        mixture += weight * pmf_k
        mass_before += weight * mass

    total = float(mixture.sum())
    if total <= 0.0:
        raise DCModelError(f"predicted DC PMF has zero mass for element={element}, fixture={fixture}")
    mixture = mixture / total

    return DCPMF(
        element=element,
        fixture=fixture,
        group=group,
        eligible=True,
        count_threshold=group_params.threshold.count_threshold,
        threshold_verified=group_params.threshold.verified,
        threshold_source=group_params.threshold.source,
        points=group_params.points,
        counts=tuple(range(max_count + 1)),
        probabilities=tuple(float(p) for p in mixture),
        mass_before_truncation=mass_before,
    )


# ---------------------------------------------------------------------------
# Walk-forward calibration (blueprint §7.1)
# ---------------------------------------------------------------------------


def _log_loss(y_true: Sequence[int], p: Sequence[float], eps: float = 1e-12) -> float:
    n = len(y_true)
    if n == 0:
        raise DCModelError("log_loss over zero rows is undefined")
    total = 0.0
    for y, prob in zip(y_true, p):
        prob = min(max(prob, eps), 1.0 - eps)
        total += -(y * math.log(prob) + (1 - y) * math.log(1 - prob))
    return total / n


def _brier(y_true: Sequence[int], p: Sequence[float]) -> float:
    n = len(y_true)
    if n == 0:
        raise DCModelError("brier score over zero rows is undefined")
    return sum((prob - y) ** 2 for y, prob in zip(y_true, p)) / n


def _skewness(values: Sequence[float]) -> float:
    n = len(values)
    if n < 3:
        raise DCModelError("skewness over fewer than 3 values is undefined")
    mean = sum(values) / n
    m2 = sum((v - mean) ** 2 for v in values) / n
    m3 = sum((v - mean) ** 3 for v in values) / n
    if m2 <= 0:
        return 0.0
    return m3 / (m2 ** 1.5)


@dataclass(frozen=True)
class DCWalkForwardResult:
    """Every fold's out-of-sample predictions for `P(DC awarded)` (model
    vs. both §7.1-required baselines), plus the raw count residuals used
    for the calibration reference and the real residual-shape report —
    module docstring, "Replace the placeholder residual"."""

    group: str
    n_folds: int
    y_true: tuple[int, ...]  # awarded, 0/1
    p_model: tuple[float, ...]
    p_baseline_group_rate: tuple[float, ...]
    p_baseline_player_trailing: tuple[float, ...]
    count_true: tuple[int, ...]
    mu_model: tuple[float, ...]

    def model_log_loss(self) -> float:
        return _log_loss(self.y_true, self.p_model)

    def model_brier(self) -> float:
        return _brier(self.y_true, self.p_model)

    def baseline_group_rate_log_loss(self) -> float:
        return _log_loss(self.y_true, self.p_baseline_group_rate)

    def baseline_group_rate_brier(self) -> float:
        return _brier(self.y_true, self.p_baseline_group_rate)

    def baseline_player_trailing_log_loss(self) -> float:
        return _log_loss(self.y_true, self.p_baseline_player_trailing)

    def baseline_player_trailing_brier(self) -> float:
        return _brier(self.y_true, self.p_baseline_player_trailing)

    def beats_both_baselines(self) -> bool:
        """The gate, §7.1: strictly lower log-loss AND Brier than BOTH
        baselines. Reports the verdict; does not tune to pass it (this
        task's brief, explicit)."""
        return (
            self.model_log_loss() < self.baseline_group_rate_log_loss()
            and self.model_log_loss() < self.baseline_player_trailing_log_loss()
            and self.model_brier() < self.baseline_group_rate_brier()
            and self.model_brier() < self.baseline_player_trailing_brier()
        )

    def residual_mean(self) -> float:
        residuals = [c - mu for c, mu in zip(self.count_true, self.mu_model)]
        return sum(residuals) / len(residuals)

    def residual_std(self) -> float:
        residuals = [c - mu for c, mu in zip(self.count_true, self.mu_model)]
        mean = sum(residuals) / len(residuals)
        var = sum((r - mean) ** 2 for r in residuals) / len(residuals)
        return math.sqrt(var)

    def residual_skewness(self) -> float:
        residuals = [c - mu for c, mu in zip(self.count_true, self.mu_model)]
        return _skewness(residuals)


def walk_forward_validate(
    table: pl.DataFrame,
    *,
    group: str,
    threshold_set: DCThresholdSet,
    min_train_rows: int = 500,
    config: DCModelConfig = DCModelConfig(),
) -> DCWalkForwardResult:
    """Refits the NB regression for `group` at every `(season, round)`
    fold, using only rows strictly earlier (by `_chronological_rank`) than
    that fold — same walk-forward discipline
    `fplai.models.minutes.walk_forward_validate` documents, applied here
    within a single season (module docstring, "No pre-2025-26 backfill" —
    there is currently only one season to walk forward WITHIN).
    Restricted to `minutes > 0` rows throughout (module docstring,
    "Minutes interact multiplicatively" — DC awarded is trivially 0 for a
    non-appearance; scoring the model on those would dilute the gate with
    rows that carry no real test of the count distribution).

    `threshold_set` is an explicit parameter, not a module-level cache —
    every other entry point in this module (`build_training_table`,
    `fit_dc_model`, `predict_dc_pmf`) takes it the same way (module
    docstring, "Threshold parameterisation"); a hidden global would be
    exactly the "caller discipline instead of a structural guarantee"
    pattern this project's derived-capability framework was built to
    reject (blueprint §12.2)."""
    if "_chronological_rank" not in table.columns:
        raise DCModelError("table must carry _chronological_rank -- build it via build_training_table")

    threshold = threshold_set.threshold(group).count_threshold
    group_table = table.filter((pl.col("group") == group) & (pl.col("minutes") > 0))
    if group_table.is_empty():
        raise DCModelError(f"no minutes>0 rows for group {group!r}")

    fold_keys = group_table.select(["season", "round", "_chronological_rank"]).unique().sort("_chronological_rank")

    spec = DCFeatureSpec()
    y_true: list[int] = []
    p_model: list[float] = []
    p_baseline_group_rate: list[float] = []
    p_baseline_player_trailing: list[float] = []
    count_true: list[int] = []
    mu_model: list[float] = []
    n_folds = 0

    for season, round_, rank in fold_keys.iter_rows():
        train = group_table.filter(pl.col("_chronological_rank") < rank)
        eval_rows = group_table.filter(pl.col("_chronological_rank") == rank)
        if train.height < min_train_rows or eval_rows.is_empty():
            continue

        n_folds += 1
        X_train = _design_matrix_dc(train, spec)
        y_train = train["count"].cast(pl.Float64).to_numpy()
        offset_train = np.log(train["minutes"].cast(pl.Float64).to_numpy() / 90.0)
        beta, log_r = _fit_negative_binomial(X_train, y_train, offset_train, config)
        r = math.exp(log_r)

        group_rate = float(train["awarded"].cast(pl.Float64).mean())

        X_eval = _design_matrix_dc(eval_rows, spec)
        offset_eval = np.log(eval_rows["minutes"].cast(pl.Float64).to_numpy() / 90.0)
        eval_counts = eval_rows["count"].to_list()
        eval_awarded = eval_rows["awarded"].cast(pl.Int8).to_list()
        eval_trailing = eval_rows["player_trailing_awarded_rate_5"].to_list()

        for i in range(eval_rows.height):
            eta = float(X_eval[i] @ beta) + float(offset_eval[i])
            mu = math.exp(min(eta, 30.0))
            count_this = int(eval_counts[i])
            y_true.append(int(eval_awarded[i]))
            count_true.append(count_this)
            mu_model.append(mu)

            pmf_k, _mass = _nb_pmf(mu, r, config.max_count)
            p_award = float(pmf_k[threshold:].sum())
            p_model.append(p_award)

            p_baseline_group_rate.append(group_rate)
            trailing = eval_trailing[i]
            p_baseline_player_trailing.append(float(trailing) if trailing is not None else group_rate)

    if n_folds == 0:
        raise DCModelError(
            f"no usable folds for group={group!r} with min_train_rows={min_train_rows}"
        )

    return DCWalkForwardResult(
        group=group,
        n_folds=n_folds,
        y_true=tuple(y_true),
        p_model=tuple(p_model),
        p_baseline_group_rate=tuple(p_baseline_group_rate),
        p_baseline_player_trailing=tuple(p_baseline_player_trailing),
        count_true=tuple(count_true),
        mu_model=tuple(mu_model),
    )


# ---------------------------------------------------------------------------
# Derived-capability registration + persistence
# ---------------------------------------------------------------------------


def _register_dc_capability() -> FactTableSchema:
    existing = CANONICAL_SCHEMAS.get(PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK)
    if existing is not None:
        return existing
    return register_derived_capability(
        PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK,
        entity_key=("season", "round", "element", "fixture", "count"),
        value_fields=("probability", "group", "count_threshold", "threshold_verified", "points"),
        dataset=PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_DATASET,
        description=(
            "Defensive-contribution estimator (blueprint §4, §11, E5, session "
            "s003) -- LONG format, one row per (player, fixture, count) with "
            "its probability under the fitted Negative Binomial count PMF. "
            "`group` in ('DEF_CBIT','MID_FWD_CBIRT'). `count_threshold`/"
            "`threshold_verified` are STRUCTURAL provenance -- threshold_"
            "verified=False means the count threshold rests on press "
            "material (docs/wiki/defensive-contribution.md), not a live "
            "observation; a consumer must be able to tell this without "
            "reading this module's source. `points` is the LIVE game_config "
            "DC points value for this position. Summing `probability` over "
            "every `count` for one (season, round, element, fixture) must "
            "equal 1.0. Excludes ineligible positions (GKP) entirely -- see "
            "fplai.models.defensive_contribution's module docstring."
        ),
    )


DC_SCHEMA: FactTableSchema = _register_dc_capability()


def pmfs_to_rows(pmfs: Sequence[DCPMF], *, season: str, round_: int) -> pl.DataFrame:
    ineligible = [p for p in pmfs if not p.eligible]
    if ineligible:
        raise DCModelError(
            f"pmfs_to_rows received {len(ineligible)} ineligible (e.g. GKP) DCPMF(s) -- "
            "these are structurally trivial (spike at 0, p_dc_awarded()==0) and this "
            "module refuses to persist them; filter them out before calling write_dc_pmfs "
            "(module docstring, predict_dc_pmf)."
        )
    frames = [pmf.to_polars() for pmf in pmfs]
    combined = (
        pl.concat(frames)
        if frames
        else pl.DataFrame(
            schema={
                "element": pl.Int64,
                "fixture": pl.Int64,
                "count": pl.Int64,
                "probability": pl.Float64,
                "group": pl.String,
                "count_threshold": pl.Int64,
                "threshold_verified": pl.Boolean,
                "points": pl.Int64,
            }
        )
    )
    return combined.with_columns(pl.lit(season).alias("season"), pl.lit(round_).alias("round"))


def write_dc_pmfs(
    store: BitemporalStore,
    pmfs: Sequence[DCPMF],
    *,
    season: str,
    round_: int,
    valid_at: datetime,
    calibration_by_group: dict[str, CalibrationReference],
    source: str = "fplai.models.defensive_contribution",
    skip_if_unchanged: bool = True,
) -> dict[str, WriteResult]:
    """Splits `pmfs` by group and calls `write_derived` once PER GROUP,
    each stamped with THAT group's own out-of-sample `CalibrationReference`
    (module docstring, "Replace the placeholder residual") -- a pooled
    single reference across both groups would blur two genuinely different
    fitted dispersions into one number. Returns one `WriteResult` per group
    actually present in `pmfs`."""
    if not pmfs:
        raise DCModelError("write_dc_pmfs called with zero PMFs -- nothing to write")
    missing_calibration = [g for g in {p.group for p in pmfs} if g not in calibration_by_group]
    if missing_calibration:
        raise DCModelError(f"calibration_by_group is missing group(s) {missing_calibration}")

    results: dict[str, WriteResult] = {}
    for group in DC_GROUPS:
        group_pmfs = [p for p in pmfs if p.group == group]
        if not group_pmfs:
            continue
        rows = pmfs_to_rows(group_pmfs, season=season, round_=round_)
        derived_from = [
            DerivationInput(
                capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
                entity_key={"season": s},
                note=(
                    "trailing per-player and per-team-round defensive-contribution "
                    "component counts (tackles/CBI/recoveries), aggregated per the "
                    "feature engineering in fplai.models.defensive_contribution -- "
                    "whole-season aggregate input, not an individually-named row subset."
                ),
            )
            for s in ({season})
        ] + [
            DerivationInput(
                capability=GAME_CONFIG_CURRENT,
                entity_key={},
                note="DC points-by-position (the position gate) -- singleton, live-read.",
            )
        ]
        results[group] = write_derived(
            store,
            PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK,
            rows,
            valid_at=valid_at,
            observed_at=datetime.now(timezone.utc),
            source=source,
            derived_from=derived_from,
            calibration=calibration_by_group[group],
            skip_if_unchanged=skip_if_unchanged,
        )
    return results
