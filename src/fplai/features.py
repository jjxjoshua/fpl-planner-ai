"""Prediction-time feature assembly — Phase 3, E6 keystone gap (docs/
HANDOFF.md §3.5). Session `s006`.

`fplai.points.simulate_fixture_points_pmfs` requires `players: Sequence[
PlayerFixtureFeatures]` — pre-assembled feature rows — and until this
module existed nothing in the codebase built one for a fixture that had
not already been trained on. Every model's own `build_training_table`
derives its feature columns while sitting on the target row's own outcome
(the row it is about to label); at prediction time that outcome does not
exist yet (a genuinely upcoming fixture) or must not be used (a settled
historical fixture being backtested as if predicted in advance).

**This module covers `minutes_feature_row` only** — one worked path, not
all seven models (that is a separate, larger story per the E6 grooming,
docs/HANDOFF.md §3.6). `assemble_minutes_feature_row` produces exactly the
dict shape `fplai.models.minutes.predict_minutes_pmf` requires: its own
`MinutesFeatureSpec.numeric_columns` (13 columns) plus `position`/`team`.

## No duplicated feature logic (pinned decision 1)

`fplai.models.minutes` is READ-ONLY in this module's owning story.
Reimplementing its 13 features by eye would be exactly the drift risk
CLAUDE.md warns about at a module boundary — training and prediction
computing the same feature two different ways, silently diverging with
nothing to catch it. Instead this module IMPORTS minutes.py's own private
feature-engineering functions unmodified — `_build_round_rollup` (the
per-(season,element,round) trailing-window rollup) and
`_build_team_fixture_gap` (the per-(season,team,fixture) rest-day
calendar) — and feeds them the same shape of input `build_training_table`
does. CLAUDE.md's routing clarification (2026-08-22) is explicit that
`FORBIDDEN`/`READ-ONLY` means do-not-edit, not do-not-import: "Importing a
stable public interface unmodified is always allowed... Only *writes* are
scoped." An underscore-prefixed function is not a public interface in the
usual sense, but `tests/test_minutes.py` already imports these same
private names directly (`_build_feature_spec`, `_design_matrix`,
`_fit_from_table`, ...) — the precedent for "this module's private
functions are the shared implementation, not an internal detail with no
external contract" is already established in this codebase, not invented
here.

## Leakage-safe by construction — the enforcing primitive

`assemble_minutes_feature_row` takes an explicit `kickoff_time` (the
target fixture's own kickoff instant) and reads history via
`fplai.gameweek_stats.read_player_gameweek_stats(store, as_of=kickoff_
time)`. That reader calls `BitemporalStore.effective_at()` per dataset,
whose own contract (`store.py`) is `< effective_ts`, **strictly before**,
never `<=`: "a fixture with `kickoff_time == t` has not been played AT
the deadline instant itself." **This is the one primitive this module
relies on for the entire leakage guarantee** — every row this module ever
reads from the store structurally cannot have a `kickoff_time` at or
after the target fixture's own kickoff, by construction of `effective_
at()`, not by a filter this module applies afterwards and could get
wrong. `tests/test_features.py` attacks this directly (see that file's
own docstring) rather than only testing the happy path — per CLAUDE.md,
"a guarantee is earned by attacking it from outside the intended call
path, not by a test that follows it."

The one deliberate exception, and why it is not a leak: `was_home`,
`team`, `position`, and the target's own `kickoff_time` are supplied by
the CALLER, never read from the store for the target fixture. This
mirrors `fplai.models.minutes`'s own documented distinction (module
docstring, "Features" — `days_since_team_previous_fixture`): "kickoff
times and the fixture list are public well before a deadline... not
leakage the way an outcome would be." A player's team, whether they play
home or away, and this fixture's own kickoff instant are schedule facts
announced well ahead of a deadline — never a match OUTCOME (minutes
played, whether they started, price, ownership), which is exactly the
class of fact this module never reads for the target fixture itself.

## The "no row exists yet for the target fixture" problem — a schedule-only placeholder

`_build_round_rollup`/`_build_team_fixture_gap` are shift(1)/rolling
functions: to read off "the trailing value AS OF this round" for a round
that has no row yet (because reading its own row would defeat the
leakage boundary above), this module appends one PLACEHOLDER row per
helper, carrying ONLY the caller-supplied schedule facts (`season`,
`element`/`team`, `round`/`fixture`, `position`, `team`, and — for the
rollup only — sentinel `starts=0`/`minutes=0`/`value=0`/`selected=0`).
Those sentinels are never read INTO the placeholder's own trailing
feature values: every trailing/rolling expression in `_build_round_
rollup` is `.shift(1)` before the window, so a row's own current-round
aggregate feeds only the NEXT round's trailing feature, never its own —
verified directly by the equivalence gate below, not merely asserted.
The placeholder exists purely so the shared rollup/gap machinery has
somewhere to attach the schedule facts it needs to compute a value FOR
that round/fixture; it contributes no outcome information anywhere in
the computation the target row's own 13 features depend on.

## Equivalence — the whole point (pinned decision, GATE 1)

For a SETTLED historical fixture, this module's assembled row and
`build_training_table`'s own row for the identical `(season, round,
element, fixture)` key must be numerically identical across all 13
numeric columns plus `position`/`team` — not close, equal. `tests/
test_features.py::test_matches_build_training_table_*` proves this for
several elements across more than one fixture. This is expected: both
paths ultimately call the SAME two private helper functions on
equivalent input; the only structural difference is a placeholder row
standing in for the target's own row, which — by the shift(1) property
above — was never the value source for its own trailing features either
way.

## Second instance: `assemble_attacking_feature_row` (session `s006`,
## second dispatch of four)

`fplai.models.attacking.predict_attacking_pmf` needs a feature_row
carrying `NUMERIC_FEATURE_COLUMNS_ATTACKING` (15 columns: player-trailing
goals/assists/xg/xa over 3/5/10-round windows, plus the same
`games_played_this_season`/`cold_start`/`was_home` triple the minutes
path above already established) plus `position` — **never** `minutes` or
`minutes_frac` themselves (`fplai.points.PlayerFixtureFeatures`'s own
docstring: those are supplied by the simulation's own minute-band draw,
never assembled from history). Same reuse discipline as pinned decision
1 above: this module imports `fplai.models.attacking`'s own
`_build_player_round_rollup` and `_normalise_and_filter_positions`
(attacking's OWN copy — `attacking.py`'s module docstring is explicit
that it duplicates rather than imports minutes' version, "no cross-model
import" convention; this module follows that same convention rather than
picking one copy to canonicalise, which would itself be an undocumented
change to a READ-ONLY module's own stated design) unmodified, rather than
re-deriving 15 columns by eye.

## Pinned decision 2, applied: what was actually factored out

Two real instances now exist, and the seam between them is narrower than
it looked from the brief: `_build_round_rollup` (minutes) and
`_build_player_round_rollup` (attacking) are NOT the same function under
different names — minutes' rollup also computes
`days_since_team_previous_fixture`/`team_first_fixture_in_window` via a
SEPARATE per-team helper (`_build_team_fixture_gap`) that attacking has
no equivalent of at all (attacking carries no team-name feature anywhere,
per its own module docstring, "Why this is NOT re-deriving team scoring
rates"). So the per-model rollup/placeholder machinery stays duplicated
per model, unchanged from pinned decision 1 — inventing a shared
"generic rollup" abstraction now, from two instances whose actual input
columns and grouping keys differ, is exactly the speculative
over-generalisation pinned decision 2 warns against ("do NOT
speculatively build for DC/cards/bonus/saves shapes you have not
implemented").

What genuinely IS identical between the two, byte-for-byte, factored
into `_require_tz_aware_kickoff` / `_leakage_safe_read` below and used by
BOTH assemblers:

1. **The naive-`kickoff_time` guard** — identical `if kickoff_time.
   tzinfo is None: raise` in both, now one function.
2. **The leakage-safe read itself** — `read_player_gameweek_stats(store,
   as_of=kickoff_time)`, the `allow_live_season` FPL-API-row drop, the
   `attribution_complete` drop, and the REQUIRED_COLUMNS presence check.
   Every one of these four steps is IDENTICAL CODE across
   `build_training_table` in both `minutes.py` and `attacking.py` (only
   the `REQUIRED_COLUMNS` tuple's contents differ, and that is already a
   parameter here, not a hardcoded name) — this is the part rule "no
   duplicated feature logic" actually bites on, and it was duplicated in
   this module's own first draft before this dispatch, not just in the
   two model modules.

Position normalisation, the model-specific "labeled" filter (minutes:
`starts.is_not_null()`; attacking: `expected_goals`/`expected_assists`
both not null), and the placeholder-row shape all stay in each model's
own assembler function — genuinely different per model, not a seam.

## Pinned decision 3: the fixture-schedule input, decided

`team`/`position`/`was_home`/`kickoff_time` stay **explicit named
keyword arguments**, identically named and ordered across both
assemblers (`season, round, element, fixture, kickoff_time, team,
position, was_home`) — NOT a dataclass or a per-player mapping, despite
two instances now sharing the exact same seven-field shape. Reasoning,
not a default: pinned decision 2 in this same brief says two instances
are "enough to see the seam, not enough to guess the rest", and that
applies to the CALLER's shape exactly as much as to the rollup
machinery — nothing in this codebase yet calls either assembler in bulk
(a "whole-fixture, every player at once" caller is a named future seam,
`docs/HANDOFF.md` §3.6, not built here), so there is no real call site to
prove a dataclass's shape against, only two single-player call sites that
a bare kwarg signature already serves without forcing every future caller
through an extra import. `assemble_minutes_feature_row`'s signature is
UNCHANGED by this dispatch (zero external callers exist yet — checked via
grep before touching it — so this was a free choice, not a compatibility
constraint) specifically so the two assemblers read as the SAME decision
applied twice: **if a third instance's caller ever needs to pass N
players' worth of these seven fields in one call, promote to a dataclass
THEN**, when there is a real call site to design it against, not now.

## Third, fourth, fifth instances: cards / DC / bonus (session `s006`, third
## of four dispatches)

Five distinct `feature_row` shapes exist now — the trap this dispatch's own
brief names directly, verified against each model's own `predict_*`/
`BonusPlayerInput` signature before writing anything (never assumed from
the minutes/attacking precedent above):

- `assemble_cards_feature_row` — 8 numeric (`NUMERIC_FEATURE_COLUMNS_
  CARDS`) + `"position"` (no `"team"`): `predict_cards_pmf`'s own
  `_feature_row_to_vector` explicitly `raise`s if `"position"` is absent.
- `assemble_dc_feature_row` — the 9 numeric columns ONLY
  (`NUMERIC_FEATURE_COLUMNS_DC`), no `"position"`/`"team"` key at all:
  `predict_dc_pmf` takes `position` as a SEPARATE argument, and its own
  `_feature_row_to_vector` never reads a `"position"` key even if one were
  present.
- `assemble_bonus_feature_row` — 8 of `NUMERIC_FEATURE_COLUMNS_BONUS`'s 9;
  `minutes_frac` is DELIBERATELY OMITTED (pinned decision 1 below), and
  `"position"` is likewise omitted: `BonusPlayerInput.__post_init__`
  RAISES if either key is present in `feature_row` — attacked from both
  directions, `tests/test_features.py`'s own bonus-exclusion gate.

**All three carry a per-team rollup neither minutes nor attacking needed**
(the brief's own observation) — `team_trailing_*_mean_5` +
`team_cold_start`, one extra placeholder-and-join pair alongside the
per-player one, the same two-rollup shape `assemble_minutes_feature_row`
already established (player rollup + `_build_team_fixture_gap`) rather than
a new pattern invented for this dispatch.

**Reuse, not reimplementation, a third/fourth/fifth time (pinned decision
1 above, applied again).** Every rollup here is the target model's OWN
private function, imported unmodified: `cards._build_player_round_rollup`
/`_build_team_round_rollup`/`_add_outcome_column`,
`defensive_contribution._build_player_round_rollup`/
`_build_team_round_rollup`/`_prepare_group_table`/`build_dc_threshold_set`,
`bonus._build_player_round_rollup`/`_build_team_round_rollup`. No fourth
by-hand copy of any of these exists anywhere in this module.

**No five-model generic abstraction (pinned decision 5, honoured).** The
per-team rollup PATTERN — group by `(season, team, round)`, `shift(1).
rolling_mean(window_size=5, min_samples=1)`, `team_cold_start` from that
same `shift(1)`'s null-ness — is now visibly identical across cards/DC/
bonus, but the column being summed differs (`yellow_cards+red_cards`,
`defensive_contribution`, `bps`) and every model's own copy is READ-ONLY
in this dispatch's scope. This module therefore imports three separate
functions rather than inventing a fourth, generic
`_build_generic_team_rollup(raw, sum_col, alias)` that no model actually
calls — the same judgment the "Pinned decision 2, applied" section above
already made for the player-rollup machinery, extended here to the
per-team shape and to the placeholder-row builders (three small, separate
functions below, not one generic one).

**DC's position-eligibility gate — handled explicitly, not silently.**
`predict_dc_pmf` itself never reads `feature_row` for a DC-ineligible
position (GK, structurally, or any config where a position carries zero
DC points) — its own early-return branch hands back a degenerate PMF
without touching a single feature. `assemble_dc_feature_row` therefore
REFUSES to manufacture a meaningless row for such a position: it raises
`FeatureAssemblyError` immediately, naming `threshold_set.is_eligible(
position)` as the check a caller should already have made — never a
silent zero-filled row a careless caller could feed straight into
`predict_dc_pmf`. `threshold_set` defaults to `build_dc_threshold_set(
store, as_of=kickoff_time)`, the same bitemporal default `defensive_
contribution.build_training_table` itself uses when a caller passes none.

**Bonus's `minutes_frac` exclusion is structural, not a documentation
promise (pinned decision 1, this dispatch).** `assemble_bonus_feature_row`
never computes or writes a `minutes_frac` key anywhere in its body — there
is no line that could regress into re-adding it by accident — and the gate
constructs the forbidden case directly (`BonusPlayerInput` fed a row WITH
`minutes_frac` smuggled in) and shows it raises, the same "attacked from
both sides" discipline `CardsModelConfig`'s own referee guarantee (module
docstring of `fplai.models.cards`) establishes for a different smuggled
key.

## Sixth, and last, instance: saves (session `s006`, fourth of four
## dispatches) — a SIXTH distinct `feature_row` shape, and not the one the
## training constant would suggest

`assemble_saves_feature_row` is built against `fplai.models.saves.
PREDICT_FEATURE_ROW_COLUMNS` (6 entries), **never**
`NUMERIC_FEATURE_COLUMNS_SAVES` (7) — the difference is exactly one column,
`opponent_goals_this_fixture`, and it is not an oversight: that column is a
FIXTURE OUTCOME (the opponent's goals THIS fixture), and assembling it from
this player's own history would be leakage no equivalence test against
`build_training_table` would obviously catch, because `build_training_table`
itself legitimately reads the row's own real historical value at FIT time
(saves.py's own module docstring, "At FIT time"). At PREDICT time that
value does not exist yet (or must not be read even for a settled fixture
being backtested) — `predict_saves_pmf` receives it instead via
`opponent_goals_marginal: Sequence[tuple[int, float]]`, drawn from the
fixture's own scoreline PMF (`fplai.models.team_strength.ScorelinePMF`),
composed at the CALLER, not assembled here. `predict_saves_pmf` itself
raises `SavesModelError` if `feature_row` carries `opponent_goals_this_
fixture` directly — the same "never smuggle the externally-supplied
exposure dimension into feature_row" discipline `assemble_bonus_feature_
row` already established for `minutes_frac`, attacked from both sides the
same way in the test gate.

**GK-only, structurally, not via a live-config eligibility set.** Unlike
DC's `threshold_set.is_eligible(position)` (a genuine per-season, live-
config-driven gate), saves' population is `position == "GK"` by the
model's own fixed design (saves.py's own module docstring, "Data verified
live") — `assemble_saves_feature_row` refuses a non-GK `position` outright,
the same "refuse rather than manufacture a meaningless row" posture, with
no config to check because there is nothing configurable here to check.

**Single player rollup, no team rollup** — the fourth structurally
distinct rollup shape now (minutes: player rollup + team-fixture gap;
attacking: player rollup only; cards/DC/bonus: player rollup + team
rollup; saves: player rollup only, but the DIFFERENT player rollup —
`saves._build_player_round_rollup` groups on `(season, element, round)`
and aggregates `saves`/`minutes` only, not bps/cards/dc-count). Reused
unmodified from `fplai.models.saves`'s own `_build_player_round_rollup`/
`_normalise_and_filter_positions` (pinned decision 2, applied a sixth
time) — `saves.py` stays READ-ONLY in this dispatch's scope.

## Wiring the saves seam — `make_saves_predict_fn`

`fplai.points.simulate_fixture_points_pmfs` accepts an optional
`saves_predict_fn: SavesPredictFn | None` by dependency injection
(`points.py`'s own module docstring, "The saves seam") and deliberately
never imports `fplai.models.saves` itself — that module did not exist when
`points.py` was written, and the seam was kept generic on purpose.
`points.py`'s own docstring anticipated the fix: "bind `params` via
`functools.partial`, matching this contract's remaining keyword names
exactly." `make_saves_predict_fn` below is exactly that binding, verified
against BOTH signatures directly (not merely hoped): `predict_saves_pmf`'s
real signature is `(params, feature_row, *, element, fixture,
minute_exposure, opponent_goals_marginal, max_count=None)`; `points.py`'s
own call site (`_draw_saves_by_band_and_goals`) invokes its `predict_fn`
as `predict_fn(feature_row, element=..., fixture=..., minute_exposure=...,
opponent_goals_marginal=...)` — precisely what `functools.partial(
predict_saves_pmf, params)` produces, with zero further adaptation. The
two contracts agree exactly; `points.py`'s own "should be a small,
mechanical change" claim is now exercised, not merely asserted (gate 3 in
this dispatch's brief calls `simulate_fixture_points_pmfs` with this
adapter as `saves_predict_fn` against a real settled fixture). `points.py`
itself stays untouched — this module supplies the argument the seam
already accepts, not a change to the socket.

## Frame-based feature assembly (session `s006`, second dispatch of the
## E6 tail) — a second ENTRY per assembler, never a second implementation

`fplai.backtest.replay`'s `Strategy` protocol structurally cannot reach a
store at all (`replay.py`'s own module docstring: "A strategy has no
method, parameter, or attribute through which it could reach `SeasonData`
or the store directly") — that is the leakage guarantee the user's ruling
was to PRESERVE, not weaken by injecting a deadline-bounded store into it.
But every assembler above takes a `store` and calls `_leakage_safe_read`
(`read_player_gameweek_stats(store, as_of=kickoff_time)`) as its very
first move. `GameweekView.history` (`replay.py`) is the way through:
every row with `round < gameweek`, every column, for ONE season — the
same within-season, strictly-before-target-round rows the store-based
path's `effective_at()` would itself have returned for a query confined
to that season.

**PROBE, run before writing anything: is `view.history` sufficient, or
does any rollup reach across a season boundary?** Read directly, not
inferred from the docstrings above: `fplai.backtest.data.load_season`
filters `store.observations(DATASET)` to `frame.filter(pl.col("season")
== season)` before ever handing rows to `SeasonData` — `GameweekView.
history` therefore NEVER contains a row from any season but the one
being replayed. Checked against each of the six rollups' own `group_by`
keys (module docstring, "Pinned decision 2, applied" / "third, fourth,
fifth instances" / "sixth instance"): `_build_round_rollup`,
`_build_player_round_rollup` (attacking/cards/DC/bonus/saves, five
distinct copies), `_build_team_fixture_gap`, `_build_team_round_rollup`
(cards/DC/bonus) all group by a key that INCLUDES `season` first —
`(season, element, round)` or `(season, team, round)` or `(season, team,
fixture)`, never `(element, round)` alone. A `shift(1).rolling_mean(...)`
inside a `group_by("season", ...)` cannot see a previous season's rows by
construction — they are a different group, full stop, not merely
filtered out downstream. `games_played_this_season`/`cold_start` are
named for exactly this: a per-season counter that resets by the same
`season`-keyed grouping, never a career total. **No rollup needs a row
`view.history` does not carry. The probe passes; this story does not
stop.**

**What `view.history` does NOT carry, and why that is fine, not a gap.**
`fplai.backtest.data.SeasonData.frame` is the RAW `vaastav_player_
gameweek_stats` stream (`store.observations(DATASET)`), never routed
through `read_player_gameweek_stats` — so it carries none of that
reader's added columns (`source_provider`, `attribution_complete`) and
never a live-season FPL-API row at all (`backtest/data.py`'s own module
docstring: this harness reads settled historical seasons only). Every
frame-based entry point below therefore takes NO `allow_live_season`
parameter and applies neither the `attribution_complete` filter nor the
`source_provider` drop `_leakage_safe_read` applies for the store-based
path — there is nothing of either kind to filter, and manufacturing a
column-presence check against a frame that structurally cannot carry
those columns would be dead code pretending to be a guarantee.

**Leakage is the caller's frame, not a filter here (pinned decision 4,
this dispatch's brief) — stated once, in full, on `assemble_minutes_
feature_row_from_frame`'s own docstring, and referenced rather than
repeated on the other five.** The store-based path's leakage guarantee is
`effective_at()`'s own `<` contract, enforced on every call, structurally.
The frame-based path enforces NOTHING of the kind: it runs the identical
downstream rollup/placeholder logic on WHATEVER rows the caller's frame
contains. If a caller ever handed it a frame with a row at or after
`kickoff_time`, this function would use it without complaint — the same
class of trust `replay.py`'s own `Strategy` protocol already places in
`SeasonReplay` never constructing `view` with anything but `rows_before_
round`. Re-filtering here was considered and rejected: it would either (a)
re-derive `round < gameweek` from `kickoff_time` comparisons this module
has no independent source of truth for (the caller's `round`/`gameweek`
numbering, not this module's), duplicating a guarantee `replay.py` already
owns, or (b) silently swallow a caller bug instead of surfacing it — and
CLAUDE.md's own standard is "no path is unsafe", not "the sanctioned path
re-proves itself twice". The one guarantee this module still owns and DOES
enforce identically on both paths is `_require_tz_aware_kickoff` — a naive
`kickoff_time` is a caller bug regardless of which entry point catches it.

**Reuse, not reimplementation, extended to the frame path.** Each
assembler's post-read logic (position normalisation, the model-specific
"labeled" filter, placeholder construction, the rollup/join, the missing-
feature check) is now a private `_assemble_*_feature_row_from_raw(raw,
...)` helper the store-based function and the frame-based function BOTH
call — the store-based public function still does exactly what it did
before this dispatch (pinned decision 1: "preserve the existing
store-based entries unchanged"), it just delegates its own tail to the
shared helper instead of inlining it. Verified by the equivalence gate
below on real rows: for the same `(season, round, element, fixture)`,
`assemble_minutes_feature_row(store, ...)` and `assemble_minutes_feature_
row_from_frame(read_player_gameweek_stats(store, as_of=kickoff_time),
...)` produce field-by-field identical output, because they now run the
SAME function on the SAME rows — not two implementations that happen to
agree today.

DC's frame-based entry additionally has NO default for `threshold_set`
(unlike the store-based `assemble_dc_feature_row`'s own `DCThresholdSet |
None = None`, which falls back to `build_dc_threshold_set(store, as_of=
kickoff_time)`): a frame-based caller has no store to fall back to, so
`threshold_set` is a required keyword argument here — the same "build
once, pass it in" discipline `FixtureFeatureAssemblyParams.threshold_set`
already established for the fixture-level composition entry point.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import numpy as np
import polars as pl

from fplai.gameweek_stats import FPL_API_DATASET, read_player_gameweek_stats
from fplai.models.attacking import (
    NUMERIC_FEATURE_COLUMNS_ATTACKING,
    REQUIRED_COLUMNS as ATTACKING_REQUIRED_COLUMNS,
    AttackingModelConfig,
    AttackingModelError,
    _build_player_round_rollup,
)
from fplai.models.attacking import _normalise_and_filter_positions as _normalise_and_filter_positions_attacking
from fplai.models.bonus import (
    NUMERIC_FEATURE_COLUMNS_BONUS,
    REQUIRED_COLUMNS as BONUS_REQUIRED_COLUMNS,
    BonusModelConfig,
)
from fplai.models.bonus import _build_player_round_rollup as _build_player_round_rollup_bonus
from fplai.models.bonus import _build_team_round_rollup as _build_team_round_rollup_bonus
from fplai.models.bonus import _normalise_and_filter_positions as _normalise_and_filter_positions_bonus
from fplai.models.cards import (
    NUMERIC_FEATURE_COLUMNS_CARDS,
    REQUIRED_COLUMNS as CARDS_REQUIRED_COLUMNS,
    CardsModelConfig,
)
from fplai.models.cards import _add_outcome_column as _add_outcome_column_cards
from fplai.models.cards import _build_player_round_rollup as _build_player_round_rollup_cards
from fplai.models.cards import _build_team_round_rollup as _build_team_round_rollup_cards
from fplai.models.cards import _normalise_and_filter_positions as _normalise_and_filter_positions_cards
from fplai.models.defensive_contribution import (
    GROUP_POSITIONS,
    NUMERIC_FEATURE_COLUMNS_DC,
    POSITION_GROUP,
    REQUIRED_COLUMNS as DC_REQUIRED_COLUMNS,
    DCModelConfig,
    DCThresholdSet,
    build_dc_threshold_set,
)
from fplai.models.defensive_contribution import _build_player_round_rollup as _build_player_round_rollup_dc
from fplai.models.defensive_contribution import _build_team_round_rollup as _build_team_round_rollup_dc
from fplai.models.defensive_contribution import _normalise_and_filter_positions as _normalise_and_filter_positions_dc
from fplai.models.defensive_contribution import _prepare_group_table
from fplai.models.minutes import (
    NUMERIC_FEATURE_COLUMNS,
    REQUIRED_COLUMNS,
    MinutesModelConfig,
    MinutesModelError,
    _build_round_rollup,
    _build_team_fixture_gap,
    _normalise_and_filter_positions,
)
from fplai.models.saves import (
    PREDICT_FEATURE_ROW_COLUMNS,
    REQUIRED_COLUMNS as SAVES_REQUIRED_COLUMNS,
    SavesModelConfig,
    SavesModelParams,
    predict_saves_pmf,
)
from fplai.models.saves import _build_player_round_rollup as _build_player_round_rollup_saves
from fplai.models.saves import _normalise_and_filter_positions as _normalise_and_filter_positions_saves
from fplai.points import POSITIONS, PlayerFixtureFeatures, PointsPMF, SavesPredictFn
from fplai.store import BitemporalStore

# Same convention `fplai.models.minutes._build_team_fixture_gap` parses
# `kickoff_time` with, and the same one `fplai.gameweek_stats._DEADLINE_
# TIME_FORMAT` declares for the sibling `deadline_time` field — FPL's one
# ISO-8601-with-literal-Z timestamp convention, verified live identical
# across both fields (gameweek_stats.py module docstring).
_KICKOFF_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_ROLLUP_INPUT_COLUMNS = ("season", "element", "round", "starts", "minutes", "value", "selected", "position", "team")
_GAP_INPUT_COLUMNS = ("season", "team", "fixture", "kickoff_time")


class FeatureAssemblyError(ValueError):
    """Misuse or a genuine data problem this module refuses to guess
    past — a naive `kickoff_time`, an unresolvable position, or a rollup/
    gap join that failed to produce exactly one row for the target key.
    With `precomputed=None` (see `assemble_minutes_feature_row`) that zero
    is structurally impossible — the placeholder row alone guarantees one
    match. With a `precomputed` roster-wide rollup (S3a), the same zero
    instead means the target element/team was never in the roster that
    rollup was built for, which is ordinary caller error, not an
    impossibility — see each raise site's own message for which case
    applies."""


# ---------------------------------------------------------------------------
# Shared skeleton (pinned decision 2 — see module docstring, "What was
# actually factored out"): the naive-kickoff_time guard and the leakage-
# safe read+filter block are IDENTICAL code across `build_training_table`
# in both `fplai.models.minutes` and `fplai.models.attacking` (module-
# specific only in `required_columns`, already a parameter here). Both
# `assemble_minutes_feature_row` and `assemble_attacking_feature_row` use
# these two; nothing else is shared (rollup shape and placeholder rows
# stay per model — see module docstring for why).
# ---------------------------------------------------------------------------


def _require_tz_aware_kickoff(kickoff_time: datetime) -> None:
    if kickoff_time.tzinfo is None:
        raise FeatureAssemblyError(
            f"kickoff_time must be timezone-aware (got a naive datetime: {kickoff_time!r}) — blueprint §3.2."
        )


def _leakage_safe_read(
    store: BitemporalStore,
    *,
    kickoff_time: datetime,
    required_columns: tuple[str, ...],
    allow_live_season: bool,
) -> pl.DataFrame:
    """THE leakage-enforcing read (module docstring): effective_at()'s own
    contract is strictly-before, so nothing returned here can have a
    kickoff_time at or after the target fixture's own. Drops live-season
    FPL-API rows unless `allow_live_season`, drops degraded-DGW
    `attribution_complete=False` rows, and checks `required_columns` are
    present — the four steps that were identical, duplicated code between
    the minutes and attacking assemblers before this dispatch. Position
    normalisation and the model-specific "labeled" filter stay in each
    caller — genuinely different per model."""
    raw = read_player_gameweek_stats(store, as_of=kickoff_time)
    if raw.is_empty():
        return raw
    if not allow_live_season:
        live_rows = raw.filter(pl.col("source_provider") == "fpl_api")
        if not live_rows.is_empty():
            raw = raw.filter(pl.col("source_provider") != "fpl_api")
    raw = raw.filter((pl.col("attribution_complete") == True) | pl.col("attribution_complete").is_null())
    missing = [c for c in required_columns if c not in raw.columns]
    if missing:
        raise FeatureAssemblyError(f"player gameweek stats is missing required column(s) {missing}")
    return raw


def _placeholder_rollup_row(*, season: str, element: int, round: int, position: str, team: str) -> pl.DataFrame:
    """Carries only schedule facts (season/element/round/position/team)
    plus sentinel outcome fields that `_build_round_rollup`'s shift(1)
    windowing never reads into this row's OWN trailing features — see
    module docstring, "The 'no row exists yet' problem"."""
    return pl.DataFrame(
        {
            "season": [season],
            "element": [element],
            "round": [round],
            "starts": [0],
            "minutes": [0],
            "value": [0],
            "selected": [0],
            "position": [position],
            "team": [team],
        }
    )


def _placeholder_gap_row(*, season: str, team: str, fixture: int, kickoff_time: datetime) -> pl.DataFrame:
    """Carries only the target fixture's own schedule facts — never an
    outcome field, because `_build_team_fixture_gap` never reads one."""
    return pl.DataFrame(
        {
            "season": [season],
            "team": [team],
            "fixture": [fixture],
            "kickoff_time": [kickoff_time.strftime(_KICKOFF_TIME_FORMAT)],
        }
    )


# ---------------------------------------------------------------------------
# Live-season coalesced-NULL guard (docs/HANDOFF.md §3.0, story S0). The
# READ-ONLY `_build_round_rollup` (fplai.models.minutes, line ~524) does
# `pl.col(c).fill_null(0.0)` on `prev_gw_value`/`prev_gw_selected_log1p`
# (and every other trailing feature) unconditionally — the right call for
# a genuine cold start (no previous round exists), the wrong call when a
# previous round DOES exist but its `value`/`selected` source column is
# NULL because the FPL-API live-ingestion path does not populate it yet
# (docs/HANDOFF.md §3.6: "value and selected are NULL on all 610 FPL-API
# rows"). This module cannot fix that fill_null call (it lives in a
# READ-ONLY module and is correct for the cold-start case it was written
# for) — it can only refuse to hand the caller a feature row built on top
# of it. The check therefore happens HERE, on the SOURCE columns, before
# they ever reach that rollup, per pinned decision 2: detect NULL-ness at
# the point of coalescing, never by inspecting whether the output happens
# to equal zero (zero is legitimate for a real cold start).
# ---------------------------------------------------------------------------

# Source column -> the feature(s) `_build_round_rollup` derives from it via
# `.shift(1)` (see minutes.py lines 512-527). Scoped to minutes only — this
# is not a shared helper in `_leakage_safe_read` because the offending
# columns and the fill_null callsite are specific to this one model.
_MINUTES_LIVE_NULL_SOURCE_COLUMNS: dict[str, str] = {
    "value": "prev_gw_value",
    "selected": "prev_gw_selected_log1p",
}


def _live_season_rows_for_element(
    store: BitemporalStore, *, kickoff_time: datetime, season: str, element: int
) -> pl.DataFrame:
    """FPL-API-sourced rows for this one (season, element), read
    independently of the caller's own `allow_live_season` choice — the
    coalesced-NULL guard below must see these rows even when the caller
    passed `allow_live_season=False` (which makes `_leakage_safe_read`
    drop them from `raw` entirely), because dropping them silently is
    exactly the failure this guard exists to catch, not a way around it.

    Reads `FPL_API_DATASET` directly via `store.effective_at()` — NOT
    `read_player_gameweek_stats()`'s union reader (story S0-a,
    docs/HANDOFF.md) — because this guard only ever looks at FPL-API
    rows and never at vaastav's. The union reader still materialises
    vaastav's ~180k-row archive on every call to build the row this
    function then discards; reading `FPL_API_DATASET` alone measured
    ~22x cheaper for the same output, once per `assemble_minutes_
    feature_row` call. `effective_at()`'s own contract (`store.py`,
    `< effective_ts`, strictly-before) is the identical leakage-safe
    boundary `read_player_gameweek_stats` itself calls on this same
    dataset internally — this is not a weaker read, just a narrower one.
    No `source_provider` filter is needed here (unlike the union reader's
    output): every row `effective_at(FPL_API_DATASET, ...)` returns IS an
    FPL-API row, by construction of which dataset was read, not by a
    column check on a unioned frame."""
    raw = store.effective_at(FPL_API_DATASET, kickoff_time)
    if raw.is_empty():
        return raw
    return raw.filter((pl.col("season") == season) & (pl.col("element") == element))


def _raise_if_live_season_prev_gw_columns_all_null(
    store: BitemporalStore, *, kickoff_time: datetime, season: str, element: int, round: int
) -> None:
    """Raises `FeatureAssemblyError` if this element has at least one
    live-season (FPL-API) row on record and a source column that
    `_build_round_rollup` coalesces into a `prev_gw_*` feature is NULL in
    EVERY one of those rows (pinned decision 2's exact trigger — "NULL in
    every source row for this element", never "the value happens to equal
    zero"). A historical (vaastav-only) fixture has zero FPL-API rows for
    its element+season and returns immediately, unaffected (pinned
    decision 3)."""
    live_rows = _live_season_rows_for_element(store, kickoff_time=kickoff_time, season=season, element=element)
    if live_rows.is_empty():
        return
    all_null_source_columns = [
        source_col
        for source_col in _MINUTES_LIVE_NULL_SOURCE_COLUMNS
        if source_col in live_rows.columns and live_rows[source_col].null_count() == live_rows.height
    ]
    if all_null_source_columns:
        affected_features = [_MINUTES_LIVE_NULL_SOURCE_COLUMNS[c] for c in all_null_source_columns]
        raise FeatureAssemblyError(
            f"element={element} season={season!r} round={round}: source column(s) "
            f"{all_null_source_columns} are NULL in every live-season (fpl_api) row on record for this "
            f"element, so feature(s) {affected_features} would silently coalesce to 0.0 in "
            "fplai.models.minutes._build_round_rollup's fill_null rather than reflect a real value "
            "(docs/HANDOFF.md §3.0 — the coalesced-NULL guard). Pass allow_coalesced_live_nulls=True to "
            "this function to opt into the old, unsafe coalescing behaviour."
        )


def _raise_if_frame_prior_rounds_all_null_for_source_columns(
    raw: pl.DataFrame, *, season: str, element: int, round: int
) -> None:
    """Provenance-free counterpart to `_raise_if_live_season_prev_gw_columns_
    all_null` above (story S0-b, docs/HANDOFF.md §3.0 — the guard the S0
    coder found bypassable via the private raw tail). The store-side check
    above asks "is this row from the live (fpl_api) provider?" — a question
    a bare frame cannot answer (`assemble_minutes_feature_row_from_frame` has
    no `source_provider` column and no store to read one from,
    `fplai.backtest.data.SeasonData.frame` being the raw vaastav stream).
    This check asks the provenance-INDEPENDENT question that is the actual
    defect: for this `(season, element)`, do rows exist at `round < round`,
    and is the source column NULL in EVERY one of them? A settled
    historical fixture (vaastav) carries real `value`/`selected` in its
    prior rounds and never trips this (verified against the real store
    before writing this check: zero `(season, element)` groups anywhere in
    `data/store`'s vaastav history have every prior-round `value` or
    `selected` NULL). This does NOT replace the store-side check above —
    see that function's own docstring and the module's "Frame-based
    feature assembly" section for the asymmetry `allow_live_season=False`
    creates (live rows dropped from `raw` before this ever runs), which
    only the independent store read can catch. Both are required."""
    if raw.is_empty():
        return
    prior = raw.filter((pl.col("season") == season) & (pl.col("element") == element) & (pl.col("round") < round))
    if prior.is_empty():
        return
    all_null_source_columns = [
        source_col
        for source_col in _MINUTES_LIVE_NULL_SOURCE_COLUMNS
        if source_col in prior.columns and prior[source_col].null_count() == prior.height
    ]
    if all_null_source_columns:
        affected_features = [_MINUTES_LIVE_NULL_SOURCE_COLUMNS[c] for c in all_null_source_columns]
        raise FeatureAssemblyError(
            f"element={element} season={season!r} round={round}: source column(s) "
            f"{all_null_source_columns} are NULL in every prior-round row in this frame for this "
            f"element, so feature(s) {affected_features} would silently coalesce to 0.0 in "
            "fplai.models.minutes._build_round_rollup's fill_null rather than reflect a real value "
            "(docs/HANDOFF.md §3.0 — the frame-side coalesced-NULL guard, story S0-b). Pass "
            "allow_coalesced_live_nulls=True to opt into the old, unsafe coalescing behaviour."
        )


def assemble_minutes_feature_row(
    store: BitemporalStore,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: MinutesModelConfig = MinutesModelConfig(),
    allow_live_season: bool = False,
    allow_coalesced_live_nulls: bool = False,
) -> dict:
    """Assemble the exact dict `fplai.models.minutes.predict_minutes_pmf`
    requires for one player-fixture, from data strictly before `kickoff_
    time` plus the caller-supplied schedule facts for the target fixture
    itself (`team`, `position`, `was_home`, `kickoff_time` — see module
    docstring for why these are not a leak). `allow_live_season` mirrors
    `build_training_table`'s own parameter of the same name and semantics
    (CLAUDE.md rule per pinned decision 2) — this module's own gate is the
    HISTORICAL path only (docs/HANDOFF.md §3.5's E6 grooming registers the
    live-season path, blocked separately on `prev_gw_value`/`prev_gw_
    selected_log1p` being NULL on every FPL-API row), so the default stays
    `False`, matching `build_training_table`'s own default.

    `allow_coalesced_live_nulls` (default `False`, story S0, docs/HANDOFF.md
    §3.0): unless set, this function RAISES `FeatureAssemblyError` rather
    than return a feature row built on a `prev_gw_value`/`prev_gw_selected_
    log1p` that silently coalesced from an all-NULL live-season source
    column — see `_raise_if_live_season_prev_gw_columns_all_null` above.
    This check runs regardless of `allow_live_season`'s own value: passing
    `allow_live_season=False` for a live-season element does not avoid the
    problem, it just drops the live rows from `raw` and returns a cold-start
    row for a player who is not actually a cold start — silently wrong in a
    different way, not a fix. A historical fixture is unaffected either way
    (pinned decision 3): it has no FPL-API rows to find.

    Returns a dict keyed by `MinutesFeatureSpec.numeric_columns` (13
    entries) plus `"position"`/`"team"` — pass straight to
    `predict_minutes_pmf(params, feature_row, element=..., fixture=...)`.
    """
    _require_tz_aware_kickoff(kickoff_time)

    if not allow_coalesced_live_nulls:
        _raise_if_live_season_prev_gw_columns_all_null(
            store, kickoff_time=kickoff_time, season=season, element=element, round=round
        )

    raw = _leakage_safe_read(
        store, kickoff_time=kickoff_time, required_columns=REQUIRED_COLUMNS, allow_live_season=allow_live_season
    )

    return _assemble_minutes_feature_row_from_raw(
        raw,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
        allow_coalesced_live_nulls=allow_coalesced_live_nulls,
    )


def _assemble_minutes_feature_row_from_raw(
    raw: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: MinutesModelConfig,
    allow_coalesced_live_nulls: bool = False,
    schedule: pl.DataFrame | None = None,
    precomputed: "MinutesRosterRollup | None" = None,
) -> dict:
    """Everything `assemble_minutes_feature_row` does AFTER its store read —
    shared verbatim with `assemble_minutes_feature_row_from_frame` below, so
    the two entry points can never silently diverge (module docstring,
    "Frame-based feature assembly", "second ENTRY, never a second
    implementation"). `raw` is trusted as-is: this function applies no
    leakage filter of its own — see `assemble_minutes_feature_row_from_
    frame`'s own docstring for what that means and why.

    `allow_coalesced_live_nulls` (default `False`, story S0-b): unless set,
    this function itself now raises `FeatureAssemblyError` when every
    prior-round `value`/`selected` for this `(season, element)` in `raw` is
    NULL — see `_raise_if_frame_prior_rounds_all_null_for_source_columns`.
    This is what closes the hole `tests/test_features.py::test_guard_is_
    bypassable_via_the_private_raw_tail_not_the_public_entry` documented: a
    caller driving this private tail directly (bypassing the public
    store-side check) no longer reaches a silently coalesced row either.

    `schedule` (S3 pilot, `fplai.optimiser.build_decision_calendar`):
    optional extra `(season, team, fixture, kickoff_time)` rows folded into
    `gap_input` alongside `raw` and the target fixture's own `_placeholder_
    gap_row`, so `_build_team_fixture_gap`'s "team's chronologically
    previous fixture" can see kickoffs beyond whatever `raw`/`history`
    itself carries — the defect this story's brief reproduces: `raw` here
    is frozen at `round < target round`, so a team's own PREVIOUS fixture
    (which may be only 1-2 rounds back) is invisible without it.
    `schedule` rows are the team's OWN public schedule, never an outcome —
    threading them is not a leakage boundary (module docstring, the
    `_build_team_fixture_gap` reasoning it inherits). `None` (the default)
    reproduces today's behaviour exactly: `gap_input` is built from `raw`
    and the placeholder alone, byte-for-byte identical to before this
    parameter existed.

    `precomputed` (S3a pilot, `MinutesRosterRollup`/`build_minutes_roster_
    rollup` below): when given, this function skips building `rollup_input`/
    `gap_input` and calling `_build_round_rollup`/`_build_team_fixture_gap`
    entirely — it just looks its own `(season, element, round)`/`(season,
    team, fixture)` row up out of the CALLER's already-built `precomputed.
    rollup`/`precomputed.gap`. `None` (the default) runs the exact
    per-element construction below, untouched — this parameter changes
    NOTHING about that path, it only offers a second way to arrive at the
    same `rollup`/`gap` frames this function then filters identically
    either way."""
    if not allow_coalesced_live_nulls:
        _raise_if_frame_prior_rounds_all_null_for_source_columns(raw, season=season, element=element, round=round)

    if precomputed is not None:
        rollup = precomputed.rollup
        gap = precomputed.gap
    else:
        if not raw.is_empty():
            raw = _normalise_and_filter_positions(raw)
            # Cheap pre-filter: the rollup only ever needs this element's own
            # rows (grouped by (season, element, round)); the gap only ever
            # needs this team's own rows (grouped by (season, team, fixture)).
            # Every OTHER row is irrelevant to this one player-fixture's 13
            # features, and this store's full history is 113k+ rows.
            raw = raw.filter((pl.col("element") == element) | (pl.col("team") == team))
            labeled = raw.filter(pl.col("starts").is_not_null())
        else:
            labeled = raw

        rollup_placeholder = _placeholder_rollup_row(
            season=season, element=element, round=round, position=position, team=team
        )
        if labeled.is_empty():
            rollup_input = rollup_placeholder
        else:
            rollup_input = pl.concat(
                [labeled.select(list(_ROLLUP_INPUT_COLUMNS)), rollup_placeholder], how="vertical_relaxed"
            )
        rollup = _build_round_rollup(rollup_input, config)

        gap_placeholder = _placeholder_gap_row(season=season, team=team, fixture=fixture, kickoff_time=kickoff_time)
        # `schedule=None` must reproduce today's behaviour byte-for-byte (S3
        # pilot, brief pinned decision): when it is None, `gap_sources` below
        # is built exactly as before this parameter existed — [raw] + [placeholder]
        # or just [placeholder], never touching `schedule` at all.
        gap_sources: list[pl.DataFrame] = []
        if not raw.is_empty():
            gap_sources.append(raw.select(list(_GAP_INPUT_COLUMNS)))
        if schedule is not None and not schedule.is_empty():
            gap_sources.append(schedule.select(list(_GAP_INPUT_COLUMNS)))
        gap_sources.append(gap_placeholder)
        gap_input = gap_sources[0] if len(gap_sources) == 1 else pl.concat(gap_sources, how="vertical_relaxed")
        gap = _build_team_fixture_gap(gap_input, config)

    rollup_row = rollup.filter(
        (pl.col("season") == season) & (pl.col("element") == element) & (pl.col("round") == round)
    )
    if rollup_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one rollup row for (season={season!r}, element={element}, round={round}), "
            f"got {rollup_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` rollup (S3a), zero instead "
            "means this element was not in the roster that rollup was built for, which is caller error, "
            "not an impossibility."
        )

    gap_row = gap.filter((pl.col("season") == season) & (pl.col("team") == team) & (pl.col("fixture") == fixture))
    if gap_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one gap row for (season={season!r}, team={team!r}, fixture={fixture}), "
            f"got {gap_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` gap (S3a), zero instead means "
            "this team was not in the roster that gap was built for, which is caller error, not an "
            "impossibility."
        )

    rollup_cols = [c for c in NUMERIC_FEATURE_COLUMNS if c in rollup_row.columns]
    feature_row: dict = {c: rollup_row[c].item() for c in rollup_cols}
    feature_row["days_since_team_previous_fixture"] = gap_row["days_since_team_previous_fixture"].item()
    feature_row["team_first_fixture_in_window"] = gap_row["team_first_fixture_in_window"].item()
    feature_row["was_home"] = bool(was_home)

    missing_features = [c for c in NUMERIC_FEATURE_COLUMNS if c not in feature_row]
    if missing_features:
        raise FeatureAssemblyError(f"assembly failed to produce feature(s) {missing_features}")

    feature_row["position"] = position
    feature_row["team"] = team
    return feature_row


def assemble_minutes_feature_row_from_frame(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: MinutesModelConfig = MinutesModelConfig(),
    allow_coalesced_live_nulls: bool = False,
    schedule: pl.DataFrame | None = None,
    precomputed: "MinutesRosterRollup | None" = None,
) -> dict:
    """Frame-based entry point (module docstring, "Frame-based feature
    assembly") — the same output as `assemble_minutes_feature_row`, built
    from a CALLER-SUPPLIED history frame (`fplai.backtest.replay.
    GameweekView.history`, or an equivalent within-season, strictly-
    `round < target`-round frame) instead of a store read. Exists because
    `fplai.backtest.replay`'s `Strategy` protocol structurally has no store
    access at all (`replay.py`'s own module docstring).

    **Leakage is the CALLER's `history` frame, not a filter this function
    applies (pinned decision 4).** The store-based path's guarantee is
    `effective_at()`'s own `<` contract, enforced on every call; this
    function enforces NOTHING of the kind — it runs the identical
    downstream rollup/placeholder logic on WHATEVER rows `history`
    contains, whatever their own `kickoff_time`. If `history` ever carried
    a row at or after this call's own `kickoff_time`, this function would
    use it without complaint. The guarantee this relies on belongs to the
    CALLER (`replay.py`'s `_build_view` constructing `history` as `rows_
    before_round`, enforced once, structurally, never re-derived here) —
    see the module docstring's "Frame-based feature assembly" section for
    why re-filtering here was considered and rejected, not merely omitted.

    No `store`, no `allow_live_season` parameter: `GameweekView.history`
    carries none of `read_player_gameweek_stats`'s added columns
    (`source_provider`, `attribution_complete`) and never a live-season
    FPL-API row — `fplai.backtest.data.SeasonData.frame` is the raw
    `vaastav_player_gameweek_stats` stream. There is nothing of either
    kind here to filter.

    `allow_coalesced_live_nulls` (default `False`, story S0-b, docs/
    HANDOFF.md §3.0): this entry point HAS no `source_provider` to check
    and no store to independently re-read (the reason the store-side guard
    could not simply be threaded here — see the probe in this story's
    brief), but it is not exempt from the underlying failure mode: if
    `history` carries prior rounds for this `(season, element)` whose
    `value`/`selected` are NULL in every one, `_assemble_minutes_feature_
    row_from_raw`'s own frame-side check (`_raise_if_frame_prior_rounds_
    all_null_for_source_columns`) raises `FeatureAssemblyError` here too,
    unless this flag is set — the same explicit opt-in the store-based
    entry point requires, applied to the only check this path can run.

    `schedule` (S3 pilot): forwarded to `_assemble_minutes_feature_row_
    from_raw` unchanged — see that function's own docstring. `None` (the
    default) reproduces today's behaviour byte-for-byte; typically
    `fplai.optimiser.build_decision_calendar(view)`, built ONCE per
    decision by the caller (never per player, never per fixture — see that
    function's own docstring for the cost reasoning).

    `precomputed` (S3a pilot, `build_minutes_roster_rollup` above): an
    optional pre-built `MinutesRosterRollup` covering every element/team in
    the CALLER's roster, forwarded to `_assemble_minutes_feature_row_from_
    raw` unchanged. `None` (the default) reproduces today's per-element
    path byte-for-byte — see that function's own docstring for the branch
    this selects."""
    _require_tz_aware_kickoff(kickoff_time)
    return _assemble_minutes_feature_row_from_raw(
        history,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
        allow_coalesced_live_nulls=allow_coalesced_live_nulls,
        schedule=schedule,
        precomputed=precomputed,
    )


# ---------------------------------------------------------------------------
# S3a: per-decision rollup hoist (pilot, minutes only). THE PROBLEM,
# measured (this story's brief): `assemble_fixture_player_features_from_
# frame` calls `assemble_minutes_feature_row_from_frame` once per roster
# element, and each call rebuilds `_build_round_rollup`/`_build_team_
# fixture_gap` from scratch — 85% of wall clock is polars' `collect()`,
# ~97 query executions per player across all six assemblers. Both rollups
# are ALREADY vectorised across elements/teams internally (`_build_round_
# rollup` groups by `["season","element","round"]` with every trailing
# expression `.over(["season","element"])`; `_build_team_fixture_gap`
# groups by `["season","team"]`) — the per-element call site is simply
# throwing that vectorisation away by invoking it once per element instead
# of once per roster.
#
# **Why extra rows cannot perturb a given element's/team's own output**
# (the brief's key finding, verified against the two functions' own
# `group_by`/`.over()` calls above, not merely asserted): `_build_round_
# rollup`'s `group_by(["season","element","round"])` and every `.over(...)`
# expression partition strictly by element — a group_by aggregation and a
# windowed `.over()` expression only ever read rows within their own
# partition, never across partitions, so adding MORE elements' rows only
# adds MORE (discarded) groups, never changes an existing group's result.
# The unhoisted code already relies on exactly this: its own rollup_input
# is `(element==e)|(team==t)` — i.e. it ALREADY hands the rollup other
# players' rows (team mates) that get discarded by the `rollup.filter(...)`
# below. The hoist generalises that from "one team's rows" to "every
# roster element's rows, in one pass" — same discard-after-filter shape,
# more rows discarded per pass, one pass instead of `len(roster)` passes.
# The identical argument holds for `_build_team_fixture_gap`'s `["season",
# "team"]` partitioning, generalised from one team's placeholder to every
# team present in `roster`.
#
# **`schedule=None`/no `precomputed` must reproduce today's behaviour
# byte-for-byte** (this story's own pinned decision, inherited from S3):
# `_assemble_minutes_feature_row_from_raw` takes `precomputed` as an
# ADDITIVE optional parameter defaulting to `None` — when omitted, the
# function runs the exact per-element code path that existed before this
# story, untouched. `build_minutes_roster_rollup` below is called ONLY by
# `assemble_fixture_player_features_from_frame`, never implicitly, and
# never memoised at module level (CLAUDE.md rule 5's per-decision-shape
# requirement, and this brief's explicit ban on frame-identity-keyed
# module caches) — it is a plain function call, rebuilt fresh every time
# the caller (one fixture's worth of roster) asks for it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MinutesRosterRollup:
    """One `_build_round_rollup` result and one `_build_team_fixture_gap`
    result, each covering EVERY element/team in one `assemble_fixture_
    player_features_from_frame` call's `roster` — built once by `build_
    minutes_roster_rollup` and threaded through `assemble_minutes_feature_
    row_from_frame` -> `_assemble_minutes_feature_row_from_raw` for every
    roster element to look its own row up from, instead of each element
    rebuilding both from scratch. Per-decision-scoped (built fresh per
    `assemble_fixture_player_features_from_frame` call) — never a module-
    level cache keyed on frame identity (CLAUDE.md rule 5, this story's own
    constraint)."""

    rollup: pl.DataFrame
    gap: pl.DataFrame


def build_minutes_roster_rollup(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    fixture: int,
    kickoff_time: datetime,
    roster: "Roster",
    config: MinutesModelConfig = MinutesModelConfig(),
    schedule: pl.DataFrame | None = None,
) -> MinutesRosterRollup:
    """Build ONE `_build_round_rollup` and ONE `_build_team_fixture_gap`
    covering every element/team in `roster`, mirroring `_assemble_minutes_
    feature_row_from_raw`'s own per-element construction exactly (see that
    function's own comments) but folding every roster element's placeholder
    row into a single rollup pass, and every roster team's placeholder gap
    row into a single gap pass, instead of one pass per element.

    Deliberately does NOT apply the per-element `(element==e)|(team==t)`
    cost-only pre-filter that `_assemble_minutes_feature_row_from_raw`
    applies (that filter exists purely to shrink the group_by's input for
    a SINGLE element/team; here every roster element/team needs a row out
    of the same pass, so the filter would have to admit almost everything
    anyway) — uses the caller's full `history` frame instead. This is safe
    per the module comment above: extra rows only ever add extra, discarded
    groups, they cannot change any element's or team's own output.

    Returns a `MinutesRosterRollup` whose `.rollup`/`.gap` frames contain
    (at least) one row per roster element / roster team respectively —
    callers filter by their own `(season, element, round)` / `(season,
    team, fixture)` exactly as the unhoisted per-element path already does."""
    if history.is_empty():
        normalised = history
        labeled = history
    else:
        normalised = _normalise_and_filter_positions(history)
        labeled = normalised.filter(pl.col("starts").is_not_null())

    rollup_placeholders = pl.concat(
        [
            _placeholder_rollup_row(season=season, element=element, round=round, position=position, team=team)
            for element, (position, team, _was_home) in sorted(roster.items())
        ],
        how="vertical_relaxed",
    )
    if labeled.is_empty():
        rollup_input = rollup_placeholders
    else:
        rollup_input = pl.concat(
            [labeled.select(list(_ROLLUP_INPUT_COLUMNS)), rollup_placeholders], how="vertical_relaxed"
        )
    rollup = _build_round_rollup(rollup_input, config)

    teams = sorted({team for _position, team, _was_home in roster.values()})
    gap_placeholders = pl.concat(
        [_placeholder_gap_row(season=season, team=team, fixture=fixture, kickoff_time=kickoff_time) for team in teams],
        how="vertical_relaxed",
    )
    gap_sources: list[pl.DataFrame] = []
    if not normalised.is_empty():
        gap_sources.append(normalised.select(list(_GAP_INPUT_COLUMNS)))
    if schedule is not None and not schedule.is_empty():
        gap_sources.append(schedule.select(list(_GAP_INPUT_COLUMNS)))
    gap_sources.append(gap_placeholders)
    gap_input = gap_sources[0] if len(gap_sources) == 1 else pl.concat(gap_sources, how="vertical_relaxed")
    gap = _build_team_fixture_gap(gap_input, config)

    return MinutesRosterRollup(rollup=rollup, gap=gap)


# ---------------------------------------------------------------------------
# Second instance: attacking (module docstring, "Second instance"). No
# `_build_team_fixture_gap` analogue exists here — attacking carries no
# team-name feature at all (attacking.py's own module docstring, "Why
# this is NOT re-deriving team scoring rates"), so there is only one
# rollup, not two, and no `_placeholder_gap_row` counterpart.
# ---------------------------------------------------------------------------

_ATTACKING_ROLLUP_INPUT_COLUMNS = (
    "season",
    "element",
    "round",
    "goals_scored",
    "assists",
    "expected_goals",
    "expected_assists",
    "minutes",
)


def _placeholder_attacking_rollup_row(*, season: str, element: int, round: int) -> pl.DataFrame:
    """Carries only schedule facts (season/element/round) plus sentinel
    outcome fields `_build_player_round_rollup`'s shift(1) windowing never
    reads into this row's OWN trailing features — same reasoning
    `_placeholder_rollup_row` above documents for minutes. No `position`/
    `team` here: unlike minutes' `_build_round_rollup`,
    `_build_player_round_rollup` groups only by `(season, element, round)`
    and never reads either column (verified by reading `attacking.py`
    directly — its rollup's `group_by`/`agg` never names them)."""
    return pl.DataFrame(
        {
            "season": [season],
            "element": [element],
            "round": [round],
            "goals_scored": [0],
            "assists": [0],
            "expected_goals": [0.0],
            "expected_assists": [0.0],
            "minutes": [0],
        }
    )


def assemble_attacking_feature_row(
    store: BitemporalStore,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: AttackingModelConfig = AttackingModelConfig(),
    allow_live_season: bool = False,
) -> dict:
    """Assemble the exact dict `fplai.models.attacking.predict_attacking_
    pmf` requires for one player-fixture, from data strictly before
    `kickoff_time` plus the caller-supplied schedule facts for the target
    fixture itself — same leakage-safe construction `assemble_minutes_
    feature_row` above establishes (module docstring, "Leakage-safe by
    construction").

    `team` and `fixture` are accepted (pinned decision 3: the same seven-
    field shape as `assemble_minutes_feature_row`, even though this
    model's own rollup never groups on `team` or reads `fixture` at all —
    see `_placeholder_attacking_rollup_row`) so a caller holding one
    `(season, round, element, fixture, kickoff_time, team, position,
    was_home)` tuple per player can call either assembler identically,
    without knowing ahead of time which fields a given model's rollup
    actually consumes. `fixture` is likewise accepted-but-unused here for
    the same reason, and is NOT part of the rollup join key (attacking's
    own entity key restricted to `(season, element, round)` for the
    rollup itself — `fixture` only matters for the Binomial `team_goals`
    TARGET at fit time, which this prediction-time module never computes,
    per `attacking.py`'s own "Composition, not import").

    Returns a dict keyed by exactly `NUMERIC_FEATURE_COLUMNS_ATTACKING`
    (15 entries) plus `"position"` — never `"team"` (unlike minutes' 13 +
    `position`/`team`): `predict_attacking_pmf` never reads `team` from
    `feature_row` (`attacking.py`'s own design-matrix construction has no
    team dummy), so this assembler does not manufacture a key nothing
    downstream consumes — pass straight to `predict_attacking_pmf(params,
    feature_row, element=..., fixture=..., stat=..., team_goals_marginal=
    ..., minute_exposure=...)`.
    """
    _require_tz_aware_kickoff(kickoff_time)

    raw = _leakage_safe_read(
        store,
        kickoff_time=kickoff_time,
        required_columns=ATTACKING_REQUIRED_COLUMNS,
        allow_live_season=allow_live_season,
    )

    return _assemble_attacking_feature_row_from_raw(
        raw,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
    )


def _assemble_attacking_feature_row_from_raw(
    raw: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: AttackingModelConfig,
    precomputed: "AttackingRosterRollup | None" = None,
) -> dict:
    """Everything `assemble_attacking_feature_row` does AFTER its store
    read — shared with `assemble_attacking_feature_row_from_frame` below.
    See `_assemble_minutes_feature_row_from_raw`'s docstring for why this
    split exists; `raw` is trusted as-is here too.

    `precomputed` (S3a replicate, `AttackingRosterRollup`/`build_attacking_
    roster_rollup` below): same additive-optional-parameter shape as
    minutes' own `precomputed` — see `_assemble_minutes_feature_row_from_
    raw`'s docstring. `None` (the default) runs the exact per-element
    construction below, untouched."""
    if precomputed is not None:
        rollup = precomputed.rollup
    else:
        if not raw.is_empty():
            raw = _normalise_and_filter_positions_attacking(raw)
            # Cheap pre-filter: the rollup only ever needs this element's own
            # rows (grouped by (season, element, round)) -- there is no
            # per-team feature here to also need `team`'s own rows for.
            raw = raw.filter(pl.col("element") == element)
            labeled = raw.filter(pl.col("expected_goals").is_not_null() & pl.col("expected_assists").is_not_null())
        else:
            labeled = raw

        rollup_placeholder = _placeholder_attacking_rollup_row(season=season, element=element, round=round)
        if labeled.is_empty():
            rollup_input = rollup_placeholder
        else:
            rollup_input = pl.concat(
                [labeled.select(list(_ATTACKING_ROLLUP_INPUT_COLUMNS)), rollup_placeholder], how="vertical_relaxed"
            )
        rollup = _build_player_round_rollup(rollup_input, config)
    rollup_row = rollup.filter(
        (pl.col("season") == season) & (pl.col("element") == element) & (pl.col("round") == round)
    )
    if rollup_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one rollup row for (season={season!r}, element={element}, round={round}), "
            f"got {rollup_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` rollup (S3a), zero instead "
            "means this element was not in the roster that rollup was built for, which is caller error, "
            "not an impossibility."
        )

    rollup_cols = [c for c in NUMERIC_FEATURE_COLUMNS_ATTACKING if c in rollup_row.columns]
    feature_row: dict = {c: rollup_row[c].item() for c in rollup_cols}
    feature_row["was_home"] = bool(was_home)

    missing_features = [c for c in NUMERIC_FEATURE_COLUMNS_ATTACKING if c not in feature_row]
    if missing_features:
        raise FeatureAssemblyError(f"assembly failed to produce feature(s) {missing_features}")

    feature_row["position"] = position
    return feature_row


def assemble_attacking_feature_row_from_frame(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: AttackingModelConfig = AttackingModelConfig(),
    precomputed: "AttackingRosterRollup | None" = None,
) -> dict:
    """Frame-based entry point — same shape and same "leakage is the
    caller's frame" contract as `assemble_minutes_feature_row_from_frame`
    (see that function's own docstring for the full reasoning, not repeated
    here). No `store`, no `allow_live_season`.

    `precomputed` (S3a replicate): forwarded to `_assemble_attacking_
    feature_row_from_raw` unchanged. `None` (the default) reproduces
    today's per-element path byte-for-byte."""
    _require_tz_aware_kickoff(kickoff_time)
    return _assemble_attacking_feature_row_from_raw(
        history,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
        precomputed=precomputed,
    )


# ---------------------------------------------------------------------------
# S3a replicate: per-decision rollup hoist for attacking -- mirrors the
# reviewed minutes pilot exactly, minus the gap half (attacking has no
# team-fixture-gap feature at all, module docstring "Second instance").
# `_build_player_round_rollup` (fplai.models.attacking) groups by
# `["season","element","round"]` with every trailing expression
# `.over(["season","element"])` (verified directly, this story's own
# session) -- the identical partition-safety argument the minutes hoist's
# own module comment makes applies here unchanged.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttackingRosterRollup:
    """One `_build_player_round_rollup` (attacking) result covering every
    element in one `assemble_fixture_player_features_from_frame` call's
    `roster` -- see `MinutesRosterRollup`'s own docstring for the shape
    this mirrors. Per-decision-scoped, never module-level cached."""

    rollup: pl.DataFrame


def build_attacking_roster_rollup(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    roster: "Roster",
    config: AttackingModelConfig = AttackingModelConfig(),
) -> AttackingRosterRollup:
    """Build ONE `_build_player_round_rollup` (attacking) covering every
    element in `roster` -- mirrors `_assemble_attacking_feature_row_from_
    raw`'s own per-element construction exactly, folding every roster
    element's placeholder row into a single pass instead of one pass per
    element. No `element==element` pre-filter (that filter is cost-only,
    same reasoning `build_minutes_roster_rollup` documents) -- uses the
    full `history` frame."""
    if history.is_empty():
        labeled = history
    else:
        normalised = _normalise_and_filter_positions_attacking(history)
        labeled = normalised.filter(pl.col("expected_goals").is_not_null() & pl.col("expected_assists").is_not_null())

    placeholders = pl.concat(
        [_placeholder_attacking_rollup_row(season=season, element=element, round=round) for element in sorted(roster)],
        how="vertical_relaxed",
    )
    if labeled.is_empty():
        rollup_input = placeholders
    else:
        rollup_input = pl.concat([labeled.select(list(_ATTACKING_ROLLUP_INPUT_COLUMNS)), placeholders], how="vertical_relaxed")
    rollup = _build_player_round_rollup(rollup_input, config)
    return AttackingRosterRollup(rollup=rollup)


# ---------------------------------------------------------------------------
# Third instance: cards (module docstring, "Third, fourth, fifth
# instances"). First of the three to carry a per-team rollup ALONGSIDE its
# per-player one -- two placeholders, two joins, same shift(1)-never-reads-
# its-own-round property the module docstring's "no row exists yet" section
# already establishes for minutes/attacking.
# ---------------------------------------------------------------------------

_CARDS_PLAYER_ROLLUP_INPUT_COLUMNS = ("season", "element", "round", "yellow_cards", "red_cards", "outcome", "minutes")
_CARDS_TEAM_ROLLUP_INPUT_COLUMNS = ("season", "team", "round", "yellow_cards", "red_cards")


def _placeholder_cards_player_row(*, season: str, element: int, round: int) -> pl.DataFrame:
    """Sentinel outcome/minutes fields `_build_player_round_rollup`'s
    shift(1) windowing never reads into this row's OWN trailing features --
    same reasoning `_placeholder_rollup_row` documents for minutes."""
    return pl.DataFrame(
        {
            "season": [season],
            "element": [element],
            "round": [round],
            "yellow_cards": [0],
            "red_cards": [0],
            "outcome": [0],
            "minutes": [0],
        }
    )


def _placeholder_cards_team_row(*, season: str, team: str, round: int) -> pl.DataFrame:
    return pl.DataFrame({"season": [season], "team": [team], "round": [round], "yellow_cards": [0], "red_cards": [0]})


def assemble_cards_feature_row(
    store: BitemporalStore,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: CardsModelConfig = CardsModelConfig(),
    allow_live_season: bool = False,
) -> dict:
    """Assemble the exact dict `fplai.models.cards.predict_cards_pmf`
    requires for one player-fixture: `NUMERIC_FEATURE_COLUMNS_CARDS` (8
    entries) plus `"position"` -- no `"team"` (`predict_cards_pmf`'s own
    design matrix carries no team dummy, same reasoning `assemble_
    attacking_feature_row` states for why it omits `"team"` too), never
    `"minutes"` itself (supplied via `minute_exposure` at predict time --
    module docstring of `fplai.models.cards`, "Minutes as an EXPOSURE
    OFFSET"). Same leakage-safe construction every assembler above uses.

    `cards.build_training_table` has no separate "labeled" pre-filter
    before its rollups (unlike minutes'/attacking's `starts`/`expected_
    goals` filters) -- every row that survives `_leakage_safe_read` feeds
    both rollups directly, after `_add_outcome_column` derives `outcome`
    from `yellow_cards`/`red_cards` (module docstring of `fplai.models.
    cards`, "What is verified" -- the mutual-exclusion invariant this
    raises on if violated)."""
    _require_tz_aware_kickoff(kickoff_time)

    raw = _leakage_safe_read(
        store, kickoff_time=kickoff_time, required_columns=CARDS_REQUIRED_COLUMNS, allow_live_season=allow_live_season
    )

    return _assemble_cards_feature_row_from_raw(
        raw,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
    )


def _assemble_cards_feature_row_from_raw(
    raw: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: CardsModelConfig,
    precomputed: "CardsRosterRollup | None" = None,
) -> dict:
    """Everything `assemble_cards_feature_row` does AFTER its store read —
    shared with `assemble_cards_feature_row_from_frame` below. See
    `_assemble_minutes_feature_row_from_raw`'s docstring for why this split
    exists; `raw` is trusted as-is here too.

    `precomputed` (S3a replicate, `CardsRosterRollup`/`build_cards_roster_
    rollup` below): same additive-optional-parameter shape as minutes' own
    `precomputed`. `None` (the default) runs the exact per-element
    construction below, untouched."""
    if precomputed is not None:
        player_rollup = precomputed.player_rollup
        team_rollup = precomputed.team_rollup
    else:
        if not raw.is_empty():
            raw = _normalise_and_filter_positions_cards(raw)
            # Cheap pre-filter -- same reasoning assemble_minutes_feature_row
            # states: the player rollup only needs this element's own rows, the
            # team rollup only needs this team's own rows, every other row is
            # irrelevant to this one player-fixture's 8 features.
            raw = raw.filter((pl.col("element") == element) | (pl.col("team") == team))
            raw = _add_outcome_column_cards(raw)

        player_placeholder = _placeholder_cards_player_row(season=season, element=element, round=round)
        if raw.is_empty():
            player_input = player_placeholder
        else:
            player_input = pl.concat(
                [raw.select(list(_CARDS_PLAYER_ROLLUP_INPUT_COLUMNS)), player_placeholder], how="vertical_relaxed"
            )
        player_rollup = _build_player_round_rollup_cards(player_input, config)

        team_placeholder = _placeholder_cards_team_row(season=season, team=team, round=round)
        if raw.is_empty():
            team_input = team_placeholder
        else:
            team_input = pl.concat(
                [raw.select(list(_CARDS_TEAM_ROLLUP_INPUT_COLUMNS)), team_placeholder], how="vertical_relaxed"
            )
        team_rollup = _build_team_round_rollup_cards(team_input, config)

    player_row = player_rollup.filter(
        (pl.col("season") == season) & (pl.col("element") == element) & (pl.col("round") == round)
    )
    if player_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one player-rollup row for (season={season!r}, element={element}, round={round}), "
            f"got {player_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` player-rollup (S3a), zero "
            "instead means this element was not in the roster that rollup was built for, which is caller "
            "error, not an impossibility."
        )

    team_row = team_rollup.filter((pl.col("season") == season) & (pl.col("team") == team) & (pl.col("round") == round))
    if team_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one team-rollup row for (season={season!r}, team={team!r}, round={round}), "
            f"got {team_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` team-rollup (S3a), zero "
            "instead means this team was not in the roster that rollup was built for, which is caller "
            "error, not an impossibility."
        )

    feature_row: dict = {
        "player_trailing_card_rate_3": player_row["player_trailing_card_rate_3"].item(),
        "player_trailing_card_rate_5": player_row["player_trailing_card_rate_5"].item(),
        "player_trailing_card_rate_10": player_row["player_trailing_card_rate_10"].item(),
        "games_played_this_season": player_row["games_played_this_season"].item(),
        "cold_start": bool(player_row["cold_start"].item()),
        "team_trailing_cards_mean_5": team_row["team_trailing_cards_mean_5"].item(),
        "team_cold_start": bool(team_row["team_cold_start"].item()),
        "was_home": bool(was_home),
    }

    missing_features = [c for c in NUMERIC_FEATURE_COLUMNS_CARDS if c not in feature_row]
    if missing_features:
        raise FeatureAssemblyError(f"assembly failed to produce feature(s) {missing_features}")

    feature_row["position"] = position
    return feature_row


def assemble_cards_feature_row_from_frame(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: CardsModelConfig = CardsModelConfig(),
    precomputed: "CardsRosterRollup | None" = None,
) -> dict:
    """Frame-based entry point — same shape and "leakage is the caller's
    frame" contract as `assemble_minutes_feature_row_from_frame` (see that
    function's own docstring for the full reasoning). No `store`, no
    `allow_live_season`.

    `precomputed` (S3a replicate): forwarded to `_assemble_cards_feature_
    row_from_raw` unchanged. `None` (the default) reproduces today's
    per-element path byte-for-byte."""
    _require_tz_aware_kickoff(kickoff_time)
    return _assemble_cards_feature_row_from_raw(
        history,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
        precomputed=precomputed,
    )


# ---------------------------------------------------------------------------
# S3a replicate: per-decision rollup hoist for cards -- mirrors the
# reviewed minutes pilot, generalised to a per-player AND a per-team
# rollup (module docstring "Third instance": first of three to carry both).
# `_build_player_round_rollup_cards` groups by `["season","element","round"]`
# / `.over(["season","element"])`; `_build_team_round_rollup_cards` groups
# by `["season","team","round"]` / `.over(["season","team"])` (verified
# directly, this story's own session) -- the same partition-safety argument
# applies to both, independently.
#
# `_add_outcome_column_cards` (the yellow/red mutual-exclusion invariant
# check) is run here over the FULL `history` frame, not a roster-scoped
# subset -- deliberately: `fplai.models.cards.build_training_table` itself
# already runs this exact check unrestricted over its own full input table
# (verified directly: `raw = _add_outcome_column(raw)` there has no
# per-element/team filter at all), so widening this hoist's own check to
# match is not a new blast radius, it is the same check the training path
# already performs every time it builds a table for this season.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardsRosterRollup:
    """One player-rollup and one team-rollup (`_build_player_round_rollup_
    cards`/`_build_team_round_rollup_cards`) covering every element/team in
    one `assemble_fixture_player_features_from_frame` call's `roster` — see
    `MinutesRosterRollup`'s own docstring for the shape this mirrors.
    Per-decision-scoped, never module-level cached."""

    player_rollup: pl.DataFrame
    team_rollup: pl.DataFrame


def build_cards_roster_rollup(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    roster: "Roster",
    config: CardsModelConfig = CardsModelConfig(),
) -> CardsRosterRollup:
    """Build ONE player-rollup and ONE team-rollup covering every
    element/team in `roster` -- mirrors `_assemble_cards_feature_row_from_
    raw`'s own per-element construction exactly, folding every roster
    element's/team's placeholder row into a single pass each, instead of
    one pass per element."""
    if history.is_empty():
        raw = history
    else:
        raw = _normalise_and_filter_positions_cards(history)
        raw = _add_outcome_column_cards(raw)

    player_placeholders = pl.concat(
        [_placeholder_cards_player_row(season=season, element=element, round=round) for element in sorted(roster)],
        how="vertical_relaxed",
    )
    if raw.is_empty():
        player_input = player_placeholders
    else:
        player_input = pl.concat(
            [raw.select(list(_CARDS_PLAYER_ROLLUP_INPUT_COLUMNS)), player_placeholders], how="vertical_relaxed"
        )
    player_rollup = _build_player_round_rollup_cards(player_input, config)

    teams = sorted({team for _position, team, _was_home in roster.values()})
    team_placeholders = pl.concat(
        [_placeholder_cards_team_row(season=season, team=team, round=round) for team in teams], how="vertical_relaxed"
    )
    if raw.is_empty():
        team_input = team_placeholders
    else:
        team_input = pl.concat(
            [raw.select(list(_CARDS_TEAM_ROLLUP_INPUT_COLUMNS)), team_placeholders], how="vertical_relaxed"
        )
    team_rollup = _build_team_round_rollup_cards(team_input, config)

    return CardsRosterRollup(player_rollup=player_rollup, team_rollup=team_rollup)


# ---------------------------------------------------------------------------
# Fourth instance: defensive contribution (module docstring, "Third,
# fourth, fifth instances"). The only one of the five whose feature_row
# carries NEITHER "position" NOR "team" -- predict_dc_pmf takes position as
# a separate argument -- and the only one gated by DC-eligibility before
# any rollup is even attempted.
# ---------------------------------------------------------------------------

_DC_GROUP_TABLE_INPUT_COLUMNS = (
    "season",
    "element",
    "round",
    "team",
    "position",
    "minutes",
    "tackles",
    "clearances_blocks_interceptions",
    "recoveries",
)
_DC_TEAM_ROLLUP_INPUT_COLUMNS = ("season", "team", "round", "defensive_contribution")


def _placeholder_dc_group_row(*, season: str, element: int, round: int, team: str, position: str) -> pl.DataFrame:
    """Sentinel component fields `_prepare_group_table`'s `count`
    derivation and `_build_player_round_rollup`'s shift(1) windowing never
    read into this row's OWN trailing features."""
    return pl.DataFrame(
        {
            "season": [season],
            "element": [element],
            "round": [round],
            "team": [team],
            "position": [position],
            "minutes": [0],
            "tackles": [0],
            "clearances_blocks_interceptions": [0],
            "recoveries": [0],
        }
    )


def _placeholder_dc_team_row(*, season: str, team: str, round: int) -> pl.DataFrame:
    return pl.DataFrame({"season": [season], "team": [team], "round": [round], "defensive_contribution": [0]})


def assemble_dc_feature_row(
    store: BitemporalStore,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    threshold_set: DCThresholdSet | None = None,
    config: DCModelConfig = DCModelConfig(),
    allow_live_season: bool = False,
) -> dict:
    """Assemble the exact dict `fplai.models.defensive_contribution.
    predict_dc_pmf` requires for one player-fixture: `NUMERIC_FEATURE_
    COLUMNS_DC` (9 entries) ONLY -- never `"position"`/`"team"` (module
    docstring, "Third, fourth, fifth instances"; `predict_dc_pmf` takes
    `position` as a SEPARATE keyword argument, `feature_row` carries
    nothing else), never `"minutes"` itself (supplied via `minute_
    exposure` -- module docstring of `fplai.models.defensive_contribution`,
    "Minutes interact multiplicatively").

    `threshold_set` defaults to `build_dc_threshold_set(store, as_of=
    kickoff_time)`, the same bitemporal default `build_training_table`
    itself uses when a caller passes none -- resolving any real pinned
    observation visible strictly before this fixture's own kickoff,
    never one written after it (CLAUDE.md rule 2).

    Raises `FeatureAssemblyError` for a position `predict_dc_pmf` would
    itself treat as DC-ineligible (module docstring, "DC's position-
    eligibility gate") -- check `threshold_set.is_eligible(position)`
    before calling this for a squad that may include goalkeepers."""
    _require_tz_aware_kickoff(kickoff_time)

    if threshold_set is None:
        threshold_set = build_dc_threshold_set(store, as_of=kickoff_time)

    raw = _leakage_safe_read(
        store, kickoff_time=kickoff_time, required_columns=DC_REQUIRED_COLUMNS, allow_live_season=allow_live_season
    )
    return _assemble_dc_feature_row_from_raw(
        raw,
        threshold_set=threshold_set,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
    )


def _assemble_dc_feature_row_from_raw(
    raw: pl.DataFrame,
    *,
    threshold_set: DCThresholdSet,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: DCModelConfig,
    precomputed: "DCRosterRollup | None" = None,
) -> dict:
    """Everything `assemble_dc_feature_row` does AFTER resolving
    `threshold_set` and doing its store read -- shared with `assemble_dc_
    feature_row_from_frame` below, INCLUDING the eligibility raise (both
    entry points must refuse a DC-ineligible position identically, and
    `threshold_set` is a required argument on both -- the frame-based path
    has no store to fall back to a default from). See `_assemble_minutes_
    feature_row_from_raw`'s docstring for why this split exists; `raw` is
    trusted as-is here too.

    `precomputed` (S3a replicate, `DCRosterRollup`/`build_dc_roster_rollup`
    below): same additive-optional-parameter shape as minutes' own
    `precomputed`. `None` (the default) runs the exact per-element
    construction below, untouched. The eligibility raise ALWAYS runs first,
    regardless of `precomputed` -- it is a property of this one call's own
    `position`, not something a precomputed roster-wide rollup can answer."""
    group = POSITION_GROUP.get(position)
    if group is None or not threshold_set.is_eligible(position):
        raise FeatureAssemblyError(
            f"position {position!r} is not DC-eligible (POSITION_GROUP keys: {sorted(POSITION_GROUP)}, "
            f"threshold_set.is_eligible({position!r})={threshold_set.is_eligible(position)}) -- "
            "predict_dc_pmf itself never reads feature_row for an ineligible position (its own "
            "early-return branch returns a degenerate PMF), so this assembler refuses to manufacture "
            "one. Check threshold_set.is_eligible(position) before calling."
        )

    if precomputed is not None:
        player_rollup = precomputed.player_rollup
        team_rollup = precomputed.team_rollup
    else:
        if not raw.is_empty():
            raw = _normalise_and_filter_positions_dc(raw)
            # Cheap pre-filter, same reasoning every assembler above states.
            raw = raw.filter((pl.col("element") == element) | (pl.col("team") == team))

        eligible_positions = [p for p in ("DEF", "MID", "FWD") if threshold_set.is_eligible(p)]
        group_positions = GROUP_POSITIONS[group]

        if raw.is_empty():
            group_source = raw
            team_source = raw
        else:
            group_source = raw.filter(pl.col("position").is_in(list(group_positions)))
            # module docstring of fplai.models.defensive_contribution, "The
            # team-style feature": built from EVERY DC-eligible position's
            # rows, not just this player's own group.
            team_source = raw.filter(pl.col("position").is_in(eligible_positions) & (pl.col("team") == team))

        group_placeholder = _placeholder_dc_group_row(season=season, element=element, round=round, team=team, position=position)
        if group_source.is_empty():
            group_input = group_placeholder
        else:
            group_input = pl.concat(
                [group_source.select(list(_DC_GROUP_TABLE_INPUT_COLUMNS)), group_placeholder], how="vertical_relaxed"
            )
        group_table = _prepare_group_table(group_input, threshold_set, group)
        threshold = threshold_set.threshold(group).count_threshold
        player_rollup = _build_player_round_rollup_dc(group_table, threshold, config)

        team_placeholder = _placeholder_dc_team_row(season=season, team=team, round=round)
        if team_source.is_empty():
            team_input = team_placeholder
        else:
            team_input = pl.concat(
                [team_source.select(list(_DC_TEAM_ROLLUP_INPUT_COLUMNS)), team_placeholder], how="vertical_relaxed"
            )
        team_rollup = _build_team_round_rollup_dc(team_input, config)

    player_row = player_rollup.filter(
        (pl.col("season") == season) & (pl.col("element") == element) & (pl.col("round") == round)
    )
    if player_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one player-rollup row for (season={season!r}, element={element}, round={round}), "
            f"got {player_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` player-rollup (S3a), zero "
            "instead means this element was not in the roster that rollup was built for, which is caller "
            "error, not an impossibility."
        )

    team_row = team_rollup.filter((pl.col("season") == season) & (pl.col("team") == team) & (pl.col("round") == round))
    if team_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one team-rollup row for (season={season!r}, team={team!r}, round={round}), "
            f"got {team_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` team-rollup (S3a), zero "
            "instead means this team was not in the roster that rollup was built for, which is caller "
            "error, not an impossibility."
        )

    feature_row: dict = {
        "player_trailing_count_3": player_row["player_trailing_count_3"].item(),
        "player_trailing_count_5": player_row["player_trailing_count_5"].item(),
        "player_trailing_count_10": player_row["player_trailing_count_10"].item(),
        "games_played_this_season": player_row["games_played_this_season"].item(),
        "cold_start": bool(player_row["cold_start"].item()),
        "team_trailing_dc_mean_5": team_row["team_trailing_dc_mean_5"].item(),
        "team_cold_start": bool(team_row["team_cold_start"].item()),
        "was_home": bool(was_home),
        "is_forward": bool(position == "FWD"),
    }

    missing_features = [c for c in NUMERIC_FEATURE_COLUMNS_DC if c not in feature_row]
    if missing_features:
        raise FeatureAssemblyError(f"assembly failed to produce feature(s) {missing_features}")

    return feature_row


def assemble_dc_feature_row_from_frame(
    history: pl.DataFrame,
    *,
    threshold_set: DCThresholdSet,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: DCModelConfig = DCModelConfig(),
    precomputed: "DCRosterRollup | None" = None,
) -> dict:
    """Frame-based entry point — same shape and "leakage is the caller's
    frame" contract as `assemble_minutes_feature_row_from_frame` (see that
    function's own docstring for the full reasoning). No `store`, no
    `allow_live_season`.

    `threshold_set` has NO default here (unlike `assemble_dc_feature_row`'s
    own `DCThresholdSet | None = None`, which falls back to `build_dc_
    threshold_set(store, as_of=kickoff_time)`): a frame-based caller has no
    store to build one from, so a caller must build it once and pass it in
    — the same discipline `FixtureFeatureAssemblyParams.threshold_set`
    already establishes for the fixture-level composition entry point.
    Raises `FeatureAssemblyError` for a DC-ineligible position, identically
    to the store-based path — check `threshold_set.is_eligible(position)`
    first.

    `precomputed` (S3a replicate): forwarded to `_assemble_dc_feature_row_
    from_raw` unchanged. `None` (the default) reproduces today's
    per-element path byte-for-byte."""
    _require_tz_aware_kickoff(kickoff_time)
    return _assemble_dc_feature_row_from_raw(
        history,
        threshold_set=threshold_set,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
        precomputed=precomputed,
    )


# ---------------------------------------------------------------------------
# S3a replicate: per-decision rollup hoist for DC -- the one hoist of the
# six that needs a genuinely different shape, not a copy-paste of the
# minutes pilot, because DC's player-side rollup depends on the TARGET
# player's own DC group (module docstring "The two DC groups": DEF_CBIT vs
# MID_FWD_CBIRT, `POSITION_GROUP`/`GROUP_POSITIONS`), and different roster
# elements can belong to different groups within the same fixture.
#
# **Why one `_prepare_group_table` call per DISTINCT GROUP (not per
# element) is still correct and sufficient**, verified by reading that
# function directly: `_prepare_group_table(raw, threshold_set, group)`
# filters its OWN input to `GROUP_POSITIONS[group]` internally (`sub =
# raw.filter(pl.col("position").is_in(list(positions)))`) BEFORE computing
# `count`/`awarded` -- so the outer `group_source = raw.filter(position.
# is_in(group_positions))` in the per-element path is cost-only, exactly
# the same "redundant with an internal filter" shape the module docstring
# already establishes elsewhere, not a correctness requirement. Feeding
# the SAME roster-wide `group_input` (every DC-eligible roster element's
# placeholder, regardless of which group it belongs to) to every needed
# `_prepare_group_table(..., group)` call is therefore safe: each call
# discards every row outside its own `group`, and `_build_player_round_
# rollup_dc` groups by `["season","element","round"]` / `.over(["season",
# "element"])` (verified directly) on top of that, so cross-group rows
# already cannot perturb a target element's own output even before that
# self-filter is accounted for. At most two `_prepare_group_table` calls
# per decision, never one per element.
#
# **The team-side `position.is_in(eligible_positions)` filter is NOT
# cost-only and MUST be preserved exactly** -- unlike every other "cheap
# pre-filter" in this module, `_build_team_round_rollup_dc` (parameter
# literally named `all_eligible`) has NO internal position filter of its
# own: it sums `defensive_contribution` over whatever rows it is given,
# per `(season, team, round)`. Admitting a DC-ineligible position's row
# here would silently inflate that team's own sum WITHIN the same
# partition the group_by keys on -- this is not the "extra rows land in a
# different, discarded partition" case the rest of this module's hoists
# rely on. `eligible_positions` is a season-global condition (derived from
# `threshold_set` alone, never from any one target player), so it hoists
# to a single filter covering the whole roster, computed once -- it does
# not need to be (and must not be) dropped.
#
# `roster` here is expected to be the CALLER's DC-ELIGIBLE subset already
# (mirroring `assemble_fixture_player_features_from_frame`'s own `dc_
# eligible` gate) -- this function raises if it is handed anything else,
# rather than silently building a rollup an ineligible element could never
# actually look up.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DCRosterRollup:
    """One unified player-rollup (concatenated across every DC group
    actually present in the roster) and one team-rollup, covering every
    DC-eligible element/team in one `assemble_fixture_player_features_
    from_frame` call's roster -- see `MinutesRosterRollup`'s own docstring
    for the shape this mirrors. Per-decision-scoped, never module-level
    cached."""

    player_rollup: pl.DataFrame
    team_rollup: pl.DataFrame


def build_dc_roster_rollup(
    history: pl.DataFrame,
    *,
    threshold_set: DCThresholdSet,
    season: str,
    round: int,
    roster: "Roster",
    config: DCModelConfig = DCModelConfig(),
) -> DCRosterRollup:
    """Build ONE unified player-rollup (across at most two DC groups) and
    ONE team-rollup covering every element/team in `roster` -- mirrors
    `_assemble_dc_feature_row_from_raw`'s own per-element construction
    exactly (see the module comment above for the two shape differences
    from the minutes pilot). `roster` MUST already be DC-eligible only —
    raises `FeatureAssemblyError` for any entry it is not, exactly the
    per-call eligibility raise `_assemble_dc_feature_row_from_raw` performs
    for a single element, applied to the whole roster up front instead."""
    for element, (position, _team, _was_home) in roster.items():
        if POSITION_GROUP.get(position) is None or not threshold_set.is_eligible(position):
            raise FeatureAssemblyError(
                f"build_dc_roster_rollup received element={element} with position={position!r}, which is not "
                "DC-eligible -- pass only the DC-eligible subset of the caller's roster (mirroring "
                "assemble_fixture_player_features_from_frame's own dc_eligible gate)."
            )

    if history.is_empty():
        raw = history
    else:
        raw = _normalise_and_filter_positions_dc(history)

    group_placeholders = pl.concat(
        [
            _placeholder_dc_group_row(season=season, element=element, round=round, team=team, position=position)
            for element, (position, team, _was_home) in sorted(roster.items())
        ],
        how="vertical_relaxed",
    )
    if raw.is_empty():
        group_input = group_placeholders
    else:
        group_input = pl.concat(
            [raw.select(list(_DC_GROUP_TABLE_INPUT_COLUMNS)), group_placeholders], how="vertical_relaxed"
        )

    groups_needed = sorted({POSITION_GROUP[position] for position, _team, _was_home in roster.values()})
    player_rollups = []
    for group in groups_needed:
        group_table = _prepare_group_table(group_input, threshold_set, group)
        threshold = threshold_set.threshold(group).count_threshold
        player_rollups.append(_build_player_round_rollup_dc(group_table, threshold, config))
    player_rollup = player_rollups[0] if len(player_rollups) == 1 else pl.concat(player_rollups, how="vertical_relaxed")

    eligible_positions = [p for p in ("DEF", "MID", "FWD") if threshold_set.is_eligible(p)]
    if raw.is_empty():
        team_source = raw
    else:
        # NOT cost-only -- see module comment above. Must stay.
        team_source = raw.filter(pl.col("position").is_in(eligible_positions))
    teams = sorted({team for _position, team, _was_home in roster.values()})
    team_placeholders = pl.concat(
        [_placeholder_dc_team_row(season=season, team=team, round=round) for team in teams], how="vertical_relaxed"
    )
    if team_source.is_empty():
        team_input = team_placeholders
    else:
        team_input = pl.concat(
            [team_source.select(list(_DC_TEAM_ROLLUP_INPUT_COLUMNS)), team_placeholders], how="vertical_relaxed"
        )
    team_rollup = _build_team_round_rollup_dc(team_input, config)

    return DCRosterRollup(player_rollup=player_rollup, team_rollup=team_rollup)


# ---------------------------------------------------------------------------
# Fifth instance: bonus (module docstring, "Third, fourth, fifth
# instances"). `minutes_frac` is a REQUIRED NUMERIC_FEATURE_COLUMNS_BONUS
# entry that must NOT be in the returned dict (pinned decision 1) --
# BonusPlayerInput.__post_init__ raises if it is present, since it is
# supplied per minute-band draw at simulation time, not from history.
# `position` is likewise accepted here but never returned in feature_row --
# BonusPlayerInput carries it on its OWN field, not inside feature_row.
# ---------------------------------------------------------------------------

_BONUS_PLAYER_ROLLUP_INPUT_COLUMNS = ("season", "element", "round", "bps", "bonus", "minutes")
_BONUS_TEAM_ROLLUP_INPUT_COLUMNS = ("season", "team", "round", "bps")


def _placeholder_bonus_player_row(*, season: str, element: int, round: int) -> pl.DataFrame:
    return pl.DataFrame({"season": [season], "element": [element], "round": [round], "bps": [0], "bonus": [0], "minutes": [0]})


def _placeholder_bonus_team_row(*, season: str, team: str, round: int) -> pl.DataFrame:
    return pl.DataFrame({"season": [season], "team": [team], "round": [round], "bps": [0]})


def assemble_bonus_feature_row(
    store: BitemporalStore,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: BonusModelConfig = BonusModelConfig(),
    allow_live_season: bool = False,
) -> dict:
    """Assemble the dict a caller wraps into `fplai.models.bonus.
    BonusPlayerInput.feature_row` for one player-fixture: 8 of
    `NUMERIC_FEATURE_COLUMNS_BONUS`'s 9 entries -- every one EXCEPT
    `minutes_frac` (pinned decision 1: supplied by the simulation's own
    minute-band draw, `fplai.models.bonus`'s module docstring, "Composition
    with MinutesPMF"; `BonusPlayerInput.__post_init__` raises if it is
    present). `"position"` is likewise never a key of the returned dict --
    it belongs on `BonusPlayerInput.position`, a separate field, and that
    same `__post_init__` raises if `feature_row` carries it too.

    `position`/`team`/`fixture` are accepted as keyword arguments purely
    for the same seven-field caller shape every assembler above uses
    (pinned decision 3) -- `position`/`team` are read straight off the
    same tuple by the caller when it constructs `BonusPlayerInput` itself,
    not from this function's return value.

    `bonus.build_training_table` includes zero-minute rows (module
    docstring of `fplai.models.bonus`, "Zero-minute rows are INCLUDED") --
    unlike cards/DC, there is no `minutes > 0` restriction and no
    `labeled` pre-filter of any kind; every row surviving `_leakage_safe_
    read` feeds both rollups directly."""
    _require_tz_aware_kickoff(kickoff_time)

    raw = _leakage_safe_read(
        store, kickoff_time=kickoff_time, required_columns=BONUS_REQUIRED_COLUMNS, allow_live_season=allow_live_season
    )
    return _assemble_bonus_feature_row_from_raw(
        raw,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
    )


def _assemble_bonus_feature_row_from_raw(
    raw: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: BonusModelConfig,
    precomputed: "BonusRosterRollup | None" = None,
) -> dict:
    """Everything `assemble_bonus_feature_row` does AFTER its store read —
    shared with `assemble_bonus_feature_row_from_frame` below. See
    `_assemble_minutes_feature_row_from_raw`'s docstring for why this split
    exists; `raw` is trusted as-is here too.

    `precomputed` (S3a replicate, `BonusRosterRollup`/`build_bonus_roster_
    rollup` below): same additive-optional-parameter shape as minutes' own
    `precomputed`. `None` (the default) runs the exact per-element
    construction below, untouched."""
    if precomputed is not None:
        player_rollup = precomputed.player_rollup
        team_rollup = precomputed.team_rollup
    else:
        if not raw.is_empty():
            raw = _normalise_and_filter_positions_bonus(raw)
            raw = raw.filter((pl.col("element") == element) | (pl.col("team") == team))

        player_placeholder = _placeholder_bonus_player_row(season=season, element=element, round=round)
        if raw.is_empty():
            player_input = player_placeholder
        else:
            player_input = pl.concat(
                [raw.select(list(_BONUS_PLAYER_ROLLUP_INPUT_COLUMNS)), player_placeholder], how="vertical_relaxed"
            )
        player_rollup = _build_player_round_rollup_bonus(player_input, config)

        team_placeholder = _placeholder_bonus_team_row(season=season, team=team, round=round)
        if raw.is_empty():
            team_input = team_placeholder
        else:
            team_input = pl.concat(
                [raw.select(list(_BONUS_TEAM_ROLLUP_INPUT_COLUMNS)), team_placeholder], how="vertical_relaxed"
            )
        team_rollup = _build_team_round_rollup_bonus(team_input, config)

    player_row = player_rollup.filter(
        (pl.col("season") == season) & (pl.col("element") == element) & (pl.col("round") == round)
    )
    if player_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one player-rollup row for (season={season!r}, element={element}, round={round}), "
            f"got {player_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` player-rollup (S3a), zero "
            "instead means this element was not in the roster that rollup was built for, which is caller "
            "error, not an impossibility."
        )

    team_row = team_rollup.filter((pl.col("season") == season) & (pl.col("team") == team) & (pl.col("round") == round))
    if team_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one team-rollup row for (season={season!r}, team={team!r}, round={round}), "
            f"got {team_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` team-rollup (S3a), zero "
            "instead means this team was not in the roster that rollup was built for, which is caller "
            "error, not an impossibility."
        )

    feature_row: dict = {
        "player_trailing_bps_3": player_row["player_trailing_bps_3"].item(),
        "player_trailing_bps_5": player_row["player_trailing_bps_5"].item(),
        "player_trailing_bps_10": player_row["player_trailing_bps_10"].item(),
        "games_played_this_season": player_row["games_played_this_season"].item(),
        "cold_start": bool(player_row["cold_start"].item()),
        "team_trailing_bps_mean_5": team_row["team_trailing_bps_mean_5"].item(),
        "team_cold_start": bool(team_row["team_cold_start"].item()),
        "was_home": bool(was_home),
    }

    expected_columns = tuple(c for c in NUMERIC_FEATURE_COLUMNS_BONUS if c != "minutes_frac")
    missing_features = [c for c in expected_columns if c not in feature_row]
    if missing_features:
        raise FeatureAssemblyError(f"assembly failed to produce feature(s) {missing_features}")

    return feature_row


def assemble_bonus_feature_row_from_frame(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: BonusModelConfig = BonusModelConfig(),
    precomputed: "BonusRosterRollup | None" = None,
) -> dict:
    """Frame-based entry point — same shape and "leakage is the caller's
    frame" contract as `assemble_minutes_feature_row_from_frame` (see that
    function's own docstring for the full reasoning), and the same
    `minutes_frac`/`position` exclusions as `assemble_bonus_feature_row`
    (pinned decision 1, module docstring "Bonus's `minutes_frac`
    exclusion"). No `store`, no `allow_live_season`.

    `precomputed` (S3a replicate): forwarded to `_assemble_bonus_feature_
    row_from_raw` unchanged. `None` (the default) reproduces today's
    per-element path byte-for-byte."""
    _require_tz_aware_kickoff(kickoff_time)
    return _assemble_bonus_feature_row_from_raw(
        history,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
        precomputed=precomputed,
    )


# ---------------------------------------------------------------------------
# S3a replicate: per-decision rollup hoist for bonus -- mirrors cards'
# hoist exactly (same player+team rollup shape), minus the outcome-column
# derivation step cards needs (bonus has no analogous invariant check).
# `_build_player_round_rollup_bonus` groups by `["season","element",
# "round"]` / `.over(["season","element"])`; `_build_team_round_rollup_
# bonus` groups by `["season","team","round"]` / `.over(["season","team"])`
# (verified directly, this story's own session).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BonusRosterRollup:
    """One player-rollup and one team-rollup (`_build_player_round_rollup_
    bonus`/`_build_team_round_rollup_bonus`) covering every element/team in
    one `assemble_fixture_player_features_from_frame` call's `roster` — see
    `MinutesRosterRollup`'s own docstring for the shape this mirrors.
    Per-decision-scoped, never module-level cached."""

    player_rollup: pl.DataFrame
    team_rollup: pl.DataFrame


def build_bonus_roster_rollup(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    roster: "Roster",
    config: BonusModelConfig = BonusModelConfig(),
) -> BonusRosterRollup:
    """Build ONE player-rollup and ONE team-rollup covering every
    element/team in `roster` -- mirrors `_assemble_bonus_feature_row_from_
    raw`'s own per-element construction exactly, folding every roster
    element's/team's placeholder row into a single pass each, instead of
    one pass per element."""
    if history.is_empty():
        raw = history
    else:
        raw = _normalise_and_filter_positions_bonus(history)

    player_placeholders = pl.concat(
        [_placeholder_bonus_player_row(season=season, element=element, round=round) for element in sorted(roster)],
        how="vertical_relaxed",
    )
    if raw.is_empty():
        player_input = player_placeholders
    else:
        player_input = pl.concat(
            [raw.select(list(_BONUS_PLAYER_ROLLUP_INPUT_COLUMNS)), player_placeholders], how="vertical_relaxed"
        )
    player_rollup = _build_player_round_rollup_bonus(player_input, config)

    teams = sorted({team for _position, team, _was_home in roster.values()})
    team_placeholders = pl.concat(
        [_placeholder_bonus_team_row(season=season, team=team, round=round) for team in teams], how="vertical_relaxed"
    )
    if raw.is_empty():
        team_input = team_placeholders
    else:
        team_input = pl.concat(
            [raw.select(list(_BONUS_TEAM_ROLLUP_INPUT_COLUMNS)), team_placeholders], how="vertical_relaxed"
        )
    team_rollup = _build_team_round_rollup_bonus(team_input, config)

    return BonusRosterRollup(player_rollup=player_rollup, team_rollup=team_rollup)


# ---------------------------------------------------------------------------
# Sixth, and last, instance: saves (module docstring, "Sixth, and last,
# instance"). GK-only, single player rollup -- no team-style rollup, and
# built against PREDICT_FEATURE_ROW_COLUMNS (6), never NUMERIC_FEATURE_
# COLUMNS_SAVES (7) -- opponent_goals_this_fixture is a fixture OUTCOME,
# supplied at predict time via opponent_goals_marginal, never assembled
# from history here.
# ---------------------------------------------------------------------------

_SAVES_ROLLUP_INPUT_COLUMNS = ("season", "element", "round", "saves", "minutes")


def _placeholder_saves_rollup_row(*, season: str, element: int, round: int) -> pl.DataFrame:
    """Sentinel `saves`/`minutes` fields `_build_player_round_rollup`'s
    shift(1) windowing never reads into this row's OWN trailing features --
    same reasoning `_placeholder_rollup_row` documents for minutes."""
    return pl.DataFrame({"season": [season], "element": [element], "round": [round], "saves": [0], "minutes": [0]})


def assemble_saves_feature_row(
    store: BitemporalStore,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: SavesModelConfig = SavesModelConfig(),
    allow_live_season: bool = False,
) -> dict:
    """Assemble the exact dict `fplai.models.saves.predict_saves_pmf`
    requires for one player-fixture: `PREDICT_FEATURE_ROW_COLUMNS` (6
    entries) -- `NUMERIC_FEATURE_COLUMNS_SAVES` MINUS `opponent_goals_
    this_fixture` (module docstring, "Sixth, and last, instance";
    `predict_saves_pmf` itself raises `SavesModelError` if that key is
    present in `feature_row` -- it is supplied at predict time via
    `opponent_goals_marginal`, the fixture's own scoreline draw, never
    from this player's own history).

    GK-only, structurally: `fplai.models.saves` trains on `position ==
    "GK"` alone (that module's own module docstring, "Data verified
    live"). A `position != "GK"` caller here is refused outright, the
    same "refuse rather than manufacture a meaningless row" discipline
    `assemble_dc_feature_row` establishes for a DC-ineligible position --
    unlike DC there is no live-config eligibility set to check, because
    saves' GK-only population is fixed by the model's own design, not
    configurable per season. A caller reaching this for a non-GK player
    should already have routed through `fplai.points.
    SAVES_STATUS_NOT_APPLICABLE` instead.

    Single player rollup only (module docstring, "Sixth, and last,
    instance") -- `fplai.models.saves` carries no team-style rollup at
    all, unlike cards/DC/bonus -- reused unmodified from that module's own
    `_build_player_round_rollup`/`_normalise_and_filter_positions` (pinned
    decision 2 applied a sixth time). Same leakage-safe construction every
    assembler above uses."""
    _require_tz_aware_kickoff(kickoff_time)

    if position != "GK":
        raise FeatureAssemblyError(
            f"assemble_saves_feature_row is GK-only (fplai.models.saves trains on position=='GK' only -- "
            f"that module's own module docstring, 'Data verified live'); got position={position!r} for "
            f"element={element}. A caller reaching this for a non-GK player should have already routed "
            "through fplai.points.SAVES_STATUS_NOT_APPLICABLE instead."
        )

    raw = _leakage_safe_read(
        store, kickoff_time=kickoff_time, required_columns=SAVES_REQUIRED_COLUMNS, allow_live_season=allow_live_season
    )

    return _assemble_saves_feature_row_from_raw(
        raw,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
    )


def _assemble_saves_feature_row_from_raw(
    raw: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: SavesModelConfig,
    precomputed: "SavesRosterRollup | None" = None,
) -> dict:
    """Everything `assemble_saves_feature_row` does AFTER its GK-only
    refusal check and its store read — shared with `assemble_saves_
    feature_row_from_frame` below. See `_assemble_minutes_feature_row_
    from_raw`'s docstring for why this split exists; `raw` is trusted
    as-is here too. The GK-only refusal itself stays in BOTH callers
    (not here) because it must fire before any store/frame read is even
    attempted — see each caller's own body.

    `precomputed` (S3a replicate, `SavesRosterRollup`/`build_saves_roster_
    rollup` below): same additive-optional-parameter shape as minutes' own
    `precomputed`. `None` (the default) runs the exact per-element
    construction below, untouched."""
    if precomputed is not None:
        rollup = precomputed.rollup
    else:
        if not raw.is_empty():
            raw = _normalise_and_filter_positions_saves(raw)
            raw = raw.filter(pl.col("team").is_not_null())  # excludes 2019-20 -- saves.build_training_table's own filter
            raw = raw.filter((pl.col("position") == "GK") & (pl.col("element") == element))

        rollup_placeholder = _placeholder_saves_rollup_row(season=season, element=element, round=round)
        if raw.is_empty():
            rollup_input = rollup_placeholder
        else:
            rollup_input = pl.concat(
                [raw.select(list(_SAVES_ROLLUP_INPUT_COLUMNS)), rollup_placeholder], how="vertical_relaxed"
            )
        rollup = _build_player_round_rollup_saves(rollup_input, config)
    rollup_row = rollup.filter(
        (pl.col("season") == season) & (pl.col("element") == element) & (pl.col("round") == round)
    )
    if rollup_row.height != 1:
        raise FeatureAssemblyError(
            f"expected exactly one rollup row for (season={season!r}, element={element}, round={round}), "
            f"got {rollup_row.height} — with precomputed=None the placeholder row alone guarantees one "
            "match, so zero here is a genuine defect; with a `precomputed` rollup (S3a), zero instead "
            "means this element was not in the roster that rollup was built for, which is caller error, "
            "not an impossibility."
        )

    feature_row: dict = {
        "player_trailing_saves_mean_3": rollup_row["player_trailing_saves_mean_3"].item(),
        "player_trailing_saves_mean_5": rollup_row["player_trailing_saves_mean_5"].item(),
        "player_trailing_saves_mean_10": rollup_row["player_trailing_saves_mean_10"].item(),
        "games_played_this_season": rollup_row["games_played_this_season"].item(),
        "cold_start": bool(rollup_row["cold_start"].item()),
        "was_home": bool(was_home),
    }

    missing_features = [c for c in PREDICT_FEATURE_ROW_COLUMNS if c not in feature_row]
    if missing_features:
        raise FeatureAssemblyError(f"assembly failed to produce feature(s) {missing_features}")

    return feature_row


def assemble_saves_feature_row_from_frame(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    element: int,
    fixture: int,
    kickoff_time: datetime,
    team: str,
    position: str,
    was_home: bool,
    config: SavesModelConfig = SavesModelConfig(),
    precomputed: "SavesRosterRollup | None" = None,
) -> dict:
    """Frame-based entry point — same shape and "leakage is the caller's
    frame" contract as `assemble_minutes_feature_row_from_frame` (see that
    function's own docstring for the full reasoning), and the same
    GK-only refusal as `assemble_saves_feature_row`. No `store`, no
    `allow_live_season`.

    `precomputed` (S3a replicate): forwarded to `_assemble_saves_feature_
    row_from_raw` unchanged. `None` (the default) reproduces today's
    per-element path byte-for-byte."""
    _require_tz_aware_kickoff(kickoff_time)
    if position != "GK":
        raise FeatureAssemblyError(
            f"assemble_saves_feature_row_from_frame is GK-only (fplai.models.saves trains on "
            f"position=='GK' only -- that module's own module docstring, 'Data verified live'); "
            f"got position={position!r} for element={element}. A caller reaching this for a non-GK "
            "player should have already routed through fplai.points.SAVES_STATUS_NOT_APPLICABLE instead."
        )
    return _assemble_saves_feature_row_from_raw(
        history,
        season=season,
        round=round,
        element=element,
        fixture=fixture,
        kickoff_time=kickoff_time,
        team=team,
        position=position,
        was_home=was_home,
        config=config,
        precomputed=precomputed,
    )


# ---------------------------------------------------------------------------
# S3a replicate, last of the six: per-decision rollup hoist for saves --
# GK-only, single rollup, no team-style feature (module docstring "Sixth,
# and last, instance"). `_build_player_round_rollup_saves` groups by
# `["season","element","round"]` / `.over(["season","element"])` (verified
# directly, this story's own session) -- the same partition-safety argument
# the minutes pilot's own module comment makes applies unchanged.
#
# `team.is_not_null()` (excludes 2019-20) is a season-global condition, not
# a per-target one -- hoists to a single filter over the whole roster's
# input, same as every other global (non-cost-only) filter this story's
# replicate phase has kept intact (see DC's team-side filter for the
# contrast this one is NOT: `team.is_not_null()` only ever drops rows, it
# does not change what a SURVIVING row contributes to a sum, so unlike DC's
# `eligible_positions` filter this one degrades gracefully either way --
# kept anyway, for the same "why filter at all if it's free" economy the
# minutes pilot already established, not because omitting it would be
# unsafe here). `roster` MUST already be GK-only, mirroring `assemble_
# fixture_player_features_from_frame`'s own `position == "GK"` gate --
# raises otherwise, same discipline `build_dc_roster_rollup` applies.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SavesRosterRollup:
    """One `_build_player_round_rollup_saves` result covering every
    (GK-only) element in one `assemble_fixture_player_features_from_frame`
    call's `roster` -- see `MinutesRosterRollup`'s own docstring for the
    shape this mirrors. Per-decision-scoped, never module-level cached."""

    rollup: pl.DataFrame


def build_saves_roster_rollup(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    roster: "Roster",
    config: SavesModelConfig = SavesModelConfig(),
) -> SavesRosterRollup:
    """Build ONE `_build_player_round_rollup_saves` covering every element
    in `roster` -- mirrors `_assemble_saves_feature_row_from_raw`'s own
    per-element construction exactly, folding every roster element's
    placeholder row into a single pass instead of one pass per element.
    `roster` MUST already be GK-only — raises `FeatureAssemblyError`
    otherwise, mirroring `assemble_saves_feature_row_from_frame`'s own
    refusal for a single element, applied to the whole roster up front."""
    for element, (position, _team, _was_home) in roster.items():
        if position != "GK":
            raise FeatureAssemblyError(
                f"build_saves_roster_rollup received element={element} with position={position!r}, which is "
                "not GK -- pass only the GK subset of the caller's roster (mirroring assemble_fixture_"
                "player_features_from_frame's own position == 'GK' gate)."
            )

    if history.is_empty():
        raw = history
    else:
        raw = _normalise_and_filter_positions_saves(history)
        raw = raw.filter(pl.col("team").is_not_null())  # excludes 2019-20, same as the per-element path
        raw = raw.filter(pl.col("position") == "GK")

    placeholders = pl.concat(
        [_placeholder_saves_rollup_row(season=season, element=element, round=round) for element in sorted(roster)],
        how="vertical_relaxed",
    )
    if raw.is_empty():
        rollup_input = placeholders
    else:
        rollup_input = pl.concat([raw.select(list(_SAVES_ROLLUP_INPUT_COLUMNS)), placeholders], how="vertical_relaxed")
    rollup = _build_player_round_rollup_saves(rollup_input, config)
    return SavesRosterRollup(rollup=rollup)


# ---------------------------------------------------------------------------
# Wiring the saves seam (module docstring, "Wiring the saves seam") --
# `fplai.points.simulate_fixture_points_pmfs(saves_predict_fn=...)`'s own
# socket, filled here rather than changed. `points.py` stays READ-ONLY.
# ---------------------------------------------------------------------------


def make_saves_predict_fn(params: SavesModelParams) -> SavesPredictFn:
    """The small, mechanical binding `fplai.points`'s own module docstring
    anticipated ("bind `params` via `functools.partial`, matching this
    contract's remaining keyword names exactly") for wiring the real saves
    model into `simulate_fixture_points_pmfs(saves_predict_fn=...)`.
    Verified against both real signatures (module docstring, "Wiring the
    saves seam"), not merely hoped."""
    return functools.partial(predict_saves_pmf, params)


# ---------------------------------------------------------------------------
# Fixture-level composition entry point (session `s006`, follow-up dispatch
# to the six assemblers above) -- promotes `tests/test_features.py::
# test_saves_seam_binds_real_model_into_simulate_fixture_points_pmfs`'s own
# open-coded per-player loop into a callable, so the MILP swap story that
# follows this one does not have to re-derive it. Reads off that test
# directly, not designed from the contracts alone (this brief's own "PROBE
# OUTPUT" section) -- two per-player rules below were found by RUNNING that
# test, not by reading assemble_dc_feature_row's/assemble_saves_feature_
# row's docstrings: a GK's dc_feature_row is `{}` (never call the
# assembler for a DC-ineligible position -- assemble_dc_feature_row itself
# REFUSES to manufacture one), and saves_feature_row is `None` for every
# non-GK (only ever populated for a GK).
# ---------------------------------------------------------------------------

# element -> (position, team, was_home). The roster is caller-supplied
# (pinned decision 2): who is in a squad at a deadline, and which side of
# the fixture they are on, is knowledge the CALLER holds (the same schedule
# facts every assembler above already takes as explicit keyword arguments,
# per pinned decision 3 -- `was_home` is not derived from a `home_team`
# lookup here for the same reason `assemble_minutes_feature_row` never
# derives it: deriving it would mean reading the target fixture's own rows,
# exactly what this module's leakage guarantee forbids doing for anything
# but caller-supplied schedule facts).
Roster = dict[int, tuple[str, str, bool]]


@dataclass(frozen=True)
class FixtureFeatureAssemblyParams:
    """Everything `assemble_fixture_player_features` needs that is NOT a
    per-fixture schedule fact -- one bundle, built ONCE per `as_of` by the
    caller (pinned decision 1: "never fit inside the entry point").

    `threshold_set` has NO default here, unlike `assemble_dc_feature_row`'s
    own `threshold_set: DCThresholdSet | None = None` (which falls back to
    `build_dc_threshold_set(store, as_of=kickoff_time)` when omitted) --
    that fallback reads the real store on every call, and a backtest loop
    calling this entry point once per fixture would pay that cost every
    single fixture for a value that does not change within one `as_of`.
    Build it once (`build_dc_threshold_set(store, as_of=...)`) and pass it
    here; this dataclass makes that the only way to reach this function.

    The six `*ModelConfig` fields are NOT "fitted" in the same sense --
    each is a small dataclass of rollup window sizes / thresholds with zero
    store-read cost to construct (unlike `threshold_set`, they carry no
    state derived by reading history) -- so a bare default instance is a
    safe default here, matching every assembler's own default above.

    `allow_live_season` is applied identically to all six assemblers, the
    same historical-only default (`False`) every one of them already
    carries individually -- this bundle does not let a caller diverge it
    per model, because nothing in this codebase's assemblers has a reason
    to (mirrors `_leakage_safe_read`'s own single `allow_live_season`
    parameter, shared across every assembler that calls it)."""

    threshold_set: DCThresholdSet
    minutes_config: MinutesModelConfig = MinutesModelConfig()
    attacking_config: AttackingModelConfig = AttackingModelConfig()
    dc_config: DCModelConfig = DCModelConfig()
    cards_config: CardsModelConfig = CardsModelConfig()
    bonus_config: BonusModelConfig = BonusModelConfig()
    saves_config: SavesModelConfig = SavesModelConfig()
    allow_live_season: bool = False


def assemble_fixture_player_features(
    store: BitemporalStore,
    *,
    season: str,
    round: int,
    fixture: int,
    kickoff_time: datetime,
    roster: Roster,
    params: FixtureFeatureAssemblyParams,
) -> list[PlayerFixtureFeatures]:
    """Build one `fplai.points.PlayerFixtureFeatures` per roster entry for
    ONE fixture -- the bridge between this module's six single-player
    assemblers and `fplai.points.simulate_fixture_points_pmfs`, which
    already requires exactly this list shape.

    Calls all six assemblers per player EXCEPT where a per-player rule
    says not to (both found by running the composition, not by reading a
    contract -- this brief's own "PROBE OUTPUT"):

    - **DC**: a position `params.threshold_set` does not consider
      DC-eligible (GK, structurally, or any live-config season where a
      position carries zero DC points) never reaches `assemble_dc_feature_
      row` at all -- that function REFUSES to manufacture a row for such a
      position (its own docstring, "DC's position-eligibility gate"), so
      calling it here would just relocate the same raise. Such a player's
      `dc_feature_row` is `{}` -- `predict_dc_pmf` never reads it for a
      DC-ineligible position either (its own early-return branch), so an
      empty dict is exactly as much as that position's `feature_row` is
      ever asked to carry. The eligibility check here is the SAME live-
      config check (`POSITION_GROUP`/`threshold_set.is_eligible`), never
      hardcoded to `"GK"` (CLAUDE.md rule 4) -- unlike saves below, DC
      eligibility genuinely is a per-season config, not a fixed model
      design.
    - **Saves**: only a `position == "GK"` player gets a non-`None`
      `saves_feature_row` (`assemble_saves_feature_row` refuses any other
      position outright) -- this IS hardcoded to `"GK"`, matching that
      assembler's own hardcode, because saves' GK-only population is fixed
      by the model's own design, not a live-config eligibility set (see
      `assemble_saves_feature_row`'s own docstring, "GK-only, structurally,
      not via a live-config eligibility set").

    Every other assembler (minutes, attacking, cards, bonus) is called for
    EVERY roster entry unconditionally -- none of them carries a position
    eligibility gate.

    `roster` values are `(position, team, was_home)` -- schedule facts the
    caller already holds for its own squad (see `Roster`'s own docstring
    for why this is not derived here). Returned in ascending-`element`
    order for the same reason `simulate_fixture_points_pmfs` itself never
    trusts caller list order (that function's own module docstring,
    "Determinism") -- this function's own output is already safe to feed
    it either way, but a stable order here costs nothing and removes one
    more place a caller's dict iteration order could matter.

    Scope: ASSEMBLY only, never simulation (pinned decision 4) -- this
    function does not call `simulate_fixture_points_pmfs`, does not take a
    `ScorelinePMF` or `team_strength` params, and does not accept the six
    models' FITTED `*ModelParams` (`minutes_params`, `attacking_params`,
    ...) at all -- none of the six assemblers below consume a fitted
    params object, only a `*ModelConfig` (rollup window sizes), because
    building a feature ROW does not require the model that will later
    consume it. A second wrapper forwarding this function's output straight
    into `simulate_fixture_points_pmfs` was considered and left out: doing
    so would force `team_strength` params / a `ScorelinePMF` / all five
    remaining fitted `*ModelParams` into THIS function's signature purely
    to forward them, and a fixture's `ScorelinePMF` is naturally computed
    once per fixture by the CALLER already (from `team_strength` params
    that have nothing to do with feature assembly) -- bundling the two
    would not remove a real seam, only rename one.

    `scoring_config` is deliberately not a parameter here either -- it is
    never used during assembly (only inside `simulate_fixture_points_pmfs`
    / `fplai.scoring.score_outcome`), and it is a forward-only, current-
    season-only config with no historical equivalent (pinned decision 3);
    sourcing it for a backtest is an open Architect decision out of scope
    for this function, which only ever reads bitemporal history.
    """
    _require_tz_aware_kickoff(kickoff_time)

    result: list[PlayerFixtureFeatures] = []
    for element in sorted(roster):
        position, team, was_home = roster[element]
        common = dict(
            season=season,
            round=round,
            element=element,
            fixture=fixture,
            kickoff_time=kickoff_time,
            team=team,
            position=position,
            was_home=was_home,
            allow_live_season=params.allow_live_season,
        )

        dc_eligible = POSITION_GROUP.get(position) is not None and params.threshold_set.is_eligible(position)
        dc_feature_row = (
            assemble_dc_feature_row(store, threshold_set=params.threshold_set, config=params.dc_config, **common)
            if dc_eligible
            else {}
        )
        saves_feature_row = (
            assemble_saves_feature_row(store, config=params.saves_config, **common) if position == "GK" else None
        )

        result.append(
            PlayerFixtureFeatures(
                element=element,
                position=position,
                team=team,
                is_home=was_home,
                minutes_feature_row=assemble_minutes_feature_row(store, config=params.minutes_config, **common),
                attacking_feature_row=assemble_attacking_feature_row(store, config=params.attacking_config, **common),
                dc_feature_row=dc_feature_row,
                cards_feature_row=assemble_cards_feature_row(store, config=params.cards_config, **common),
                bonus_feature_row=assemble_bonus_feature_row(store, config=params.bonus_config, **common),
                saves_feature_row=saves_feature_row,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Fixture-level composition entry point, frame-based (session `s006`, this
# dispatch) -- the same promotion `assemble_fixture_player_features` above
# made for the store-based path, extended to the frame-based path this
# dispatch's six `assemble_*_feature_row_from_frame` functions add. This IS
# the shape `fplai.backtest`'s model-stack strategy story needs: ONE call
# per fixture, a caller-supplied `history` frame (`GameweekView.history`)
# instead of a store, and the same two per-player rules `assemble_fixture_
# player_features` already established (DC eligibility -> `{}`, GK-only ->
# non-`None` `saves_feature_row`) -- reused, not re-derived, since both
# rules are properties of `threshold_set`/`position` alone, never of
# whether the underlying read came from a store or a frame.
# ---------------------------------------------------------------------------


def assemble_fixture_player_features_from_frame(
    history: pl.DataFrame,
    *,
    season: str,
    round: int,
    fixture: int,
    kickoff_time: datetime,
    roster: Roster,
    params: FixtureFeatureAssemblyParams,
    schedule: pl.DataFrame | None = None,
) -> list[PlayerFixtureFeatures]:
    """Frame-based counterpart to `assemble_fixture_player_features` — same
    per-player composition rules (see that function's own docstring for the
    full reasoning, not repeated here), built from a caller-supplied
    `history` frame instead of a store. Exists for the same reason every
    `assemble_*_feature_row_from_frame` function above does: `fplai.
    backtest.replay`'s `Strategy` protocol has no store access at all (see
    module docstring, "Frame-based feature assembly").

    Reuses `FixtureFeatureAssemblyParams` as-is rather than a parallel
    dataclass — every field it carries (`threshold_set`, the six
    `*ModelConfig`s) means exactly the same thing for a frame-based call.
    The one field this function does NOT read is `params.allow_live_season`
    — meaningless here, the same reason no individual `*_from_frame`
    assembler takes that parameter (module docstring: a caller-supplied
    frame has no live-season/FPL-API distinction to opt into). Left on the
    shared dataclass rather than split into a second params type so a
    caller building one `FixtureFeatureAssemblyParams` per `as_of` can pass
    it to EITHER composition entry point unchanged.

    Leakage is `history`'s, not this function's — see module docstring,
    "Frame-based feature assembly", and each individual `_from_frame`
    assembler's own docstring for the full reasoning, not repeated here.

    `schedule` (S3 pilot, `fplai.optimiser.build_decision_calendar`):
    forwarded to `assemble_minutes_feature_row_from_frame` ONLY (pinned
    decision 3 — minutes is the only one of the six assemblers with a
    schedule-gap feature at all; see that model's own `_build_team_
    fixture_gap`). `None` (the default) reproduces today's behaviour
    byte-for-byte, on every one of the six assemblers, including minutes.

    **S3a per-decision rollup hoist, all six assemblers (pilot: minutes;
    replicate: attacking, cards, dc, bonus, saves):** this is the
    "per-decision" level each hoist's own module comment (above each
    `build_*_roster_rollup`) names — one `*RosterRollup` per assembler is
    built ONCE per call, covering every element/team in `roster` (DC and
    saves narrowed to their own eligible/GK-only subsets first, mirroring
    the SAME gates this function already applies below), and handed to
    every corresponding `assemble_*_feature_row_from_frame` call instead of
    each one rebuilding its rollup(s) from scratch. Built fresh on every
    call (never cached across calls) — see each builder's own docstring for
    why."""
    _require_tz_aware_kickoff(kickoff_time)

    dc_eligible_roster: Roster = {
        element: (position, team, was_home)
        for element, (position, team, was_home) in roster.items()
        if POSITION_GROUP.get(position) is not None and params.threshold_set.is_eligible(position)
    }
    gk_roster: Roster = {
        element: (position, team, was_home) for element, (position, team, was_home) in roster.items() if position == "GK"
    }

    minutes_precomputed = (
        build_minutes_roster_rollup(
            history,
            season=season,
            round=round,
            fixture=fixture,
            kickoff_time=kickoff_time,
            roster=roster,
            config=params.minutes_config,
            schedule=schedule,
        )
        if roster
        else None
    )
    attacking_precomputed = (
        build_attacking_roster_rollup(history, season=season, round=round, roster=roster, config=params.attacking_config)
        if roster
        else None
    )
    cards_precomputed = (
        build_cards_roster_rollup(history, season=season, round=round, roster=roster, config=params.cards_config)
        if roster
        else None
    )
    dc_precomputed = (
        build_dc_roster_rollup(
            history,
            threshold_set=params.threshold_set,
            season=season,
            round=round,
            roster=dc_eligible_roster,
            config=params.dc_config,
        )
        if dc_eligible_roster
        else None
    )
    bonus_precomputed = (
        build_bonus_roster_rollup(history, season=season, round=round, roster=roster, config=params.bonus_config)
        if roster
        else None
    )
    saves_precomputed = (
        build_saves_roster_rollup(history, season=season, round=round, roster=gk_roster, config=params.saves_config)
        if gk_roster
        else None
    )

    result: list[PlayerFixtureFeatures] = []
    for element in sorted(roster):
        position, team, was_home = roster[element]
        common = dict(
            season=season,
            round=round,
            element=element,
            fixture=fixture,
            kickoff_time=kickoff_time,
            team=team,
            position=position,
            was_home=was_home,
        )

        dc_eligible = POSITION_GROUP.get(position) is not None and params.threshold_set.is_eligible(position)
        dc_feature_row = (
            assemble_dc_feature_row_from_frame(
                history, threshold_set=params.threshold_set, config=params.dc_config, precomputed=dc_precomputed, **common
            )
            if dc_eligible
            else {}
        )
        saves_feature_row = (
            assemble_saves_feature_row_from_frame(
                history, config=params.saves_config, precomputed=saves_precomputed, **common
            )
            if position == "GK"
            else None
        )

        result.append(
            PlayerFixtureFeatures(
                element=element,
                position=position,
                team=team,
                is_home=was_home,
                minutes_feature_row=assemble_minutes_feature_row_from_frame(
                    history, config=params.minutes_config, schedule=schedule, precomputed=minutes_precomputed, **common
                ),
                attacking_feature_row=assemble_attacking_feature_row_from_frame(
                    history, config=params.attacking_config, precomputed=attacking_precomputed, **common
                ),
                dc_feature_row=dc_feature_row,
                cards_feature_row=assemble_cards_feature_row_from_frame(
                    history, config=params.cards_config, precomputed=cards_precomputed, **common
                ),
                bonus_feature_row=assemble_bonus_feature_row_from_frame(
                    history, config=params.bonus_config, precomputed=bonus_precomputed, **common
                ),
                saves_feature_row=saves_feature_row,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Double/blank-gameweek combination (session `s006`, double-gameweek
# handling story). `assemble_fixture_player_features_from_frame` above
# builds ONE `PlayerFixtureFeatures` list per FIXTURE; a DGW player needs
# TWO calls (one per fixture, driven by `fplai.backtest.replay.
# GameweekView.fixtures` — see that module's own docstring) and two
# resulting `fplai.points.PointsPMF` objects, one per fixture. This section
# is what a model-stack `Strategy` calls next: combine however many
# fixture-level PointsPMFs a player has THIS gameweek (0, 1, or 2+) into
# ONE gameweek-level distribution — a blank gameweek's mirror-image case
# (0 fixtures) is handled by the SAME function, not a separate branch a
# caller could forget to call.
#
# **Convolution, not a sum of expectations, not a doubled single fixture**
# (pinned decision 3). Two fixtures are independent draws of a points
# total (the SAME independence assumption `fplai.points`'s own module
# docstring already states it does NOT model across fixtures — "Cross-
# FIXTURE correlation... is Phase 5's job, not this module's" — so treating
# them as independent here is consistent with, not an addition to, the
# existing modelling boundary). The distribution of the SUM of two
# independent random variables is the convolution of their PMFs; nothing
# else preserves both fixtures' full variance, and a doubled single-
# fixture PMF would be wrong even in expectation whenever the two
# fixtures' own expected points differ (e.g. a home game against a
# relegation-threatened side and an away game against a top-four side).
#
# **A blank gameweek is a degenerate PMF at 0, not an omitted candidate**
# (pinned decision 4, CLAUDE.md rule 5 — a scalar 0 at a module boundary
# would still be a design error; a PMF with all its mass on 0 is not).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameweekPointsPMF:
    """One player's total FPL points distribution for ONE gameweek — the
    model-stack strategy's own decision unit, never fixture-scoped the way
    `fplai.points.PointsPMF` itself is (`PointsPMF.fixture: int` has no
    slot for "two"; a DGW player's two fixtures carry two different
    fixture ids). `points`/`probabilities` are the discrete convolution of
    every one of `fixture_pmfs`' own support for a double(+) gameweek, the
    identity of the single entry for a single gameweek, and a point mass
    at 0 for a blank gameweek (module docstring, "Double/blank-gameweek
    combination"). `fixture_pmfs` keeps the ORIGINAL per-fixture
    `PointsPMF` objects, sorted by `fixture`, for traceability — never
    discarded once combined."""

    element: int
    position: str
    points: tuple[int, ...]
    probabilities: tuple[float, ...]
    fixture_pmfs: tuple[PointsPMF, ...]

    def __post_init__(self) -> None:
        if self.position not in POSITIONS:
            raise FeatureAssemblyError(f"unknown position {self.position!r} — expected one of {POSITIONS}")
        if len(self.points) != len(self.probabilities):
            raise FeatureAssemblyError("points and probabilities must be the same length")
        if list(self.points) != list(range(self.points[0], self.points[0] + len(self.points))):
            raise FeatureAssemblyError(f"points support must be a DENSE consecutive integer range, got {self.points}")
        total = sum(self.probabilities)
        if abs(total - 1.0) > 1e-6:
            raise FeatureAssemblyError(
                f"GameweekPointsPMF probabilities do not sum to 1.0 (got {total}) for element={self.element}"
            )

    def expected_points(self) -> float:
        return sum(p * prob for p, prob in zip(self.points, self.probabilities))


def _convolve_dense_pmfs(
    points_a: Sequence[int], probs_a: Sequence[float], points_b: Sequence[int], probs_b: Sequence[float]
) -> tuple[tuple[int, ...], tuple[float, ...]]:
    """Discrete convolution of two independent, dense-integer-support
    PMFs — the distribution of the SUM of two independent draws. index
    `i+j` of `np.convolve(probs_a, probs_b)` accumulates
    `probs_a[i]*probs_b[j]` summed over every `(i, j)` with `i+j` fixed,
    i.e. `P(X=points_a[0]+i) * P(Y=points_b[0]+j)` summed over every pair
    whose totals equal one output point — exactly the definition of the
    sum-of-two-independent-variables PMF, not an approximation of it.
    Renormalises away float accumulation drift only — `np.convolve`'s own
    output already sums to 1.0 to float precision whenever both inputs
    do; this guards against nothing more than that drift."""
    lo = points_a[0] + points_b[0]
    conv = np.convolve(np.asarray(probs_a, dtype=np.float64), np.asarray(probs_b, dtype=np.float64))
    total = float(conv.sum())
    if total <= 0.0:
        raise FeatureAssemblyError(f"convolved probability vector sums to {total} (<= 0)")
    conv = conv / total
    support = tuple(range(lo, lo + len(conv)))
    return support, tuple(float(v) for v in conv)


def combine_gameweek_points_pmfs(
    fixture_pmfs: Sequence[PointsPMF], *, element: int, position: str
) -> GameweekPointsPMF:
    """Combine however many fixture-level `PointsPMF`s one player has THIS
    gameweek into one `GameweekPointsPMF` — 0 fixtures (blank) -> a
    degenerate PMF at 0 points; 1 fixture (the ordinary case) -> that same
    PMF's own support/probabilities, unchanged; 2+ fixtures (a double/
    triple gameweek) -> their discrete convolution (module docstring,
    "Double/blank-gameweek combination"). Every entry in `fixture_pmfs`
    must declare the SAME `element`/`position` this call declares — a
    mismatch is a caller bug (e.g. accidentally mixing two different
    players' PMFs into one combination), refused rather than silently
    combined.

    Input order is never trusted: `fixture_pmfs` is sorted by `.fixture`
    before folding, so the result is a pure function of WHICH fixtures a
    player has, never of the caller's own list order (CLAUDE.md rule 7,
    the same "do not trust upstream ordering" discipline `fplai.points`'s
    own module docstring states for its `players` argument)."""
    for pmf in fixture_pmfs:
        if pmf.element != element:
            raise FeatureAssemblyError(
                f"fixture_pmfs element mismatch: combine_gameweek_points_pmfs called with element={element}, "
                f"but a fixture_pmfs entry declares element={pmf.element}"
            )
        if pmf.position != position:
            raise FeatureAssemblyError(
                f"fixture_pmfs position mismatch: combine_gameweek_points_pmfs called with position={position!r}, "
                f"but a fixture_pmfs entry declares position={pmf.position!r}"
            )

    if not fixture_pmfs:
        return GameweekPointsPMF(element=element, position=position, points=(0,), probabilities=(1.0,), fixture_pmfs=())

    ordered = tuple(sorted(fixture_pmfs, key=lambda pmf: pmf.fixture))
    points, probs = ordered[0].points, ordered[0].probabilities
    for pmf in ordered[1:]:
        points, probs = _convolve_dense_pmfs(points, probs, pmf.points, pmf.probabilities)

    return GameweekPointsPMF(element=element, position=position, points=points, probabilities=probs, fixture_pmfs=ordered)
