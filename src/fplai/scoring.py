"""FPL scoring function — blueprint §7.1/§7.2 prerequisite, CLAUDE.md rule 4
("nothing hardcoded... read from live config") and rule 5 ("a scalar `xPts`
at a module boundary is a design error"). Phase 3 prerequisite 1 of 2,
session `s005`.

## What this module is

A **pure, deterministic function** that turns one player-gameweek's
*realised outcome vector* (minutes played, goals scored, clean sheet
earned, etc.) into an integer points total, under FPL's own scoring rules
read live from `game_config`. `score_outcome(outcome, position, config) ->
int`. That is the whole module.

It is **not** a model. It never touches a probability, an expectation, or
a PMF — see "FORWARD-ONLY, and why" below for the reason that boundary is
enforced, not just documented. The next Phase 3 story composes this
function with the six outcome models' PMFs (minutes x attacking share x
DC x bonus x cards x CS/GC) to produce a player-GW **points distribution**
— that composition is the PMF layer blueprint §4.3 requires, and it is
only clean if this layer stays a scalar-in/scalar-out arithmetic rule. Do
**not** extend `score_outcome` to accept probabilities or emit an
expectation; that is the next story, not this one.

## FORWARD-ONLY, and why this is enforced, not just documented

**Lesson 7 (blueprint §7.2), restated for this module specifically:**
FPL's scoring rules drift every season — defensive contribution did not
exist before 2025-26, GKP goals are 10 points in 2026/27 where they were
6 before, the Assistant Manager chip existed only in 2025-26. `game_config`
is captured live from the FPL API and **only ever holds the rules in
force for the CURRENT season** — there is no historical equivalent, and
the FPL API exposes none. Phase 1's baselines therefore sum stored
`total_points` and never recompute (blueprint §7.2) — that discipline is
correct and this module must never be used to violate it. **This module
exists to score PREDICTED FUTURE outcomes only.** Feeding it a historical
gameweek's realised stats to "recompute" points a second time would
silently apply THIS season's rules to whatever season the caller actually
meant, exactly the class of error lesson 7 already names.

The enforcement is structural, not a comment a future session can miss:
`load_scoring_config(store, as_of)` calls `store.as_of("game_config",
as_of)`, and `game_config`'s first-ever observation in this store is this
project's own ingest start (2026-08-19, this season). **Any `as_of` before
that returns an empty frame — `store.as_of`'s own documented behaviour,
not a check this module invented — and `load_scoring_config` turns that
into a loud, explicitly-worded `ScoringError`** naming the forward-only
rule and pointing the caller at stored `total_points` instead. A caller
cannot silently get a wrong-season answer from this path: for every season
this project's baselines cover (2019-20 through 2025-26, all strictly
before `game_config` existed), the loader refuses outright. It cannot
prevent a caller from misusing a *current-season* `as_of` to "recompute"
a settled 2026/27 gameweek instead of reading its stored `total_points` —
no timestamp can distinguish "predicting GW9" from "re-deriving GW3
after the fact" when both are legally inside the same season's config
validity window — but that residual case is exactly what this docstring
and lesson 7 exist to keep front-of-mind, and it is why `score_outcome`
takes a *realised* outcome vector by name: the caller must already know
whether the outcome is real (recompute — don't) or predicted (score — do).

## Interface shape and why it stays this narrow

`score_outcome(outcome: RealisedOutcome, position: str, config:
ScoringConfig) -> int`. Scalar-in/scalar-out is deliberate, not an
oversight of CLAUDE.md rule 5 — rule 5 targets a scalar `xPts` sitting at
a *model* boundary, standing in for a distribution. This is the scoring
*rule itself*: given one fully-realised outcome, FPL's rules produce
exactly one integer, always. There is nothing probabilistic about it —
the same way `fplai.models.cards.read_card_points` and
`fplai.models.defensive_contribution.build_dc_threshold_set` read fixed
point values out of `game_config` without themselves becoming models.

`position` uses this codebase's house short-form vocabulary — `"GK"`,
never `"GKP"` — matching `fplai.models.minutes.POSITIONS`,
`fplai.models.cards.POSITIONS`, `fplai.models.bonus.POSITIONS`,
`fplai.models.attacking.POSITIONS` (every one of those modules
independently declares the same four-tuple rather than importing a shared
one — house convention, not an oversight, followed here too).
`game_config`'s own key is `"GKP"`; `load_scoring_config` remaps it once,
at the store boundary, the same way
`fplai.models.defensive_contribution.build_dc_threshold_set` already does
for its own per-position point values.

## What the realised outcome vector does and does NOT resolve

`RealisedOutcome`'s fields are FPL scoring *identifiers*, not raw counts
this module derives eligibility from. Two of its boolean fields are
**pre-resolved facts the caller supplies**, not values this module infers:

- `clean_sheet` — whether the team did not concede while this player was
  on the pitch, for a player who cleared the minutes cliff. FPL's live
  `stats.clean_sheets` field (verified against `event/1/live/`, see
  "Verification" below) already carries this pre-resolved as 0/1; a
  defensive/team-strength model composing with this function must do the
  same. This function does enforce ONE consistency check on it (see
  `MINUTE_CLIFF` below) but does not compute it from a raw goals-conceded
  timeline.
- `defensive_contribution_met` — whether the player's CBIT/CBIRT count
  crossed the group's threshold. **The threshold itself is deliberately
  out of this module's scope** — it already lives in
  `fplai.models.defensive_contribution.build_dc_threshold_set` /
  `DCThresholdSet`, pinned by observation (DEF 10, MID/FWD 12, verified
  2026-08-27) — importing that logic here would duplicate a threshold
  this module has no business re-deciding. A caller resolves the boolean
  there, then hands it to `score_outcome` already resolved. Verified
  directly this session: treating the raw CBIRT *count* as truthy instead
  (i.e. skipping this step) mis-scored 45 of 610 real GW1 rows — this is
  not a hypothetical seam, it is exactly the bug an inline shortcut would
  reintroduce.

`goals_conceded` is likewise a pre-resolved **count while this player was
on the pitch**, not the team's final scoreline — the same "on the pitch"
scoping `clean_sheet` carries, sourced from the same upstream layer.

## Two point values genuinely absent from `game_config` — and a third
## found this session that the brief describing this task did not name

`game_config`'s `scoring` block publishes the *point value* paid per unit
of an identifier, never the *unit size itself* where that unit is not "one
occurrence". Checked directly against the real store's `game_config`
payload this session (`"threshold" not in payload_raw.lower()` — the same
check `fplai.models.defensive_contribution`'s module docstring already
ran for its own gap):

1. **`MINUTE_CLIFF = 60`** — `long_play` (2 pts) applies at 60+ minutes,
   `short_play` (1 pt) at 1-59; the boundary itself is unpublished. Same,
   independently-verified constant `fplai.models.minutes.
   APPEARANCE_POINTS_MINUTE_CLIFF` already carries (verified live
   2026-08-22, this session re-confirms it against real GW1 outcomes,
   610/610 exact). Declared again HERE rather than imported from
   `fplai.models.minutes` on purpose: that module is a full statistical
   model with its own fitting/walk-forward machinery, and this module
   must stay a minimal, dependency-light pure-arithmetic layer the
   optimiser can eventually depend on directly. The duplication is a real
   cost — **a finding, not a shrug**: if this constant is ever wrong, it
   must be fixed in both places, and nothing today enforces that they
   agree beyond this comment. A shared `fplai.rules` constants module is
   the natural fix and is out of scope for this task.
2. **`SAVES_POINTS_DIVISOR = 3`** — `saves` (1 pt) pays per 3 saves,
   floor-divided. The "per 3" grouping is unpublished; the point value
   alone is in config.
3. **`GOALS_CONCEDED_POINTS_DIVISOR = 2`** — `goals_conceded` (-1 pt for
   GK/DEF, 0 for MID/FWD, all read live) pays per 2 goals conceded while
   on the pitch, floor-divided. **This one was not named in this task's
   brief** — the brief flagged exactly two missing values (the minute
   cliff and the saves divisor). Verified directly this session (see
   "Verification"): a naive "-1 per goal conceded" reading fails
   immediately against real data (a GK conceding 4 with a `goals_conceded`
   config value of -1 must land at -2, not -4, to match a real
   `total_points` of 1 once `long_play`/`saves` are added back in) —
   floor-dividing by 2 first matches every GK/DEF row in the real GW1
   payload exactly. Reported as a finding: the brief's "two missing
   values" was an undercount, not a ceiling on what to look for.

All three are named constants at module level, each with its citation and
verification note in this docstring — the single-point-of-correction
convention `fplai.backtest.rules.SEASON_RULES` already establishes for
exactly this class of "the live source does not carry this, so it is
declared once, here, not inlined at a call site" problem.

## Verification — real settled GW1, not fixtures

`score_outcome`, composed with `load_scoring_config` reading the real
store's `game_config` and `fplai.models.defensive_contribution.
build_dc_threshold_set` for the DC threshold boolean, was checked against
every one of the 610 real elements in `event/1/live/` (GW1 2026/27,
`finished=True, data_checked=True` in the store's own `events` snapshot at
verification time) fetched live via `fplai.client.FPLClient` — the same,
unmodified client every other live-API script in this project already
uses. **610/610 exact match against FPL's own `stats.total_points`.**
Every scoring identifier `game_config` exposes was exercised by at least
one real row in this gameweek except `penalties_saved` (zero occurrences
in GW1 2026/27) — that one coefficient is applied identically to every
other identifier (`count * config value`, no hidden divisor) and is not
independently at risk the way `saves`/`goals_conceded` were. Full
reconciliation script and its output are recorded in this session's
punch-card (`.punchcard/s005.jsonl`), not committed as a script under this
module's owned paths — this was a one-off verification, not a
reusable tool the codebase needs going forward. `tests/test_scoring.py`
pins a smaller, hand-checked set of the same real rows as regression
fixtures so this proof does not depend on a live API call to re-run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from fplai.store import BitemporalStore

# House vocabulary — see module docstring, "Interface shape". Every model
# module in fplai.models independently declares this same four-tuple
# rather than importing one from another; followed here for the same
# reason even though this module is not itself a model.
POSITIONS: tuple[str, ...] = ("GK", "DEF", "MID", "FWD")

# game_config's own per-position key for goalkeepers is "GKP"; every other
# position label already matches (verified live, this session, same as
# fplai.models.defensive_contribution's identical remap).
_CONFIG_POSITION_KEY: dict[str, str] = {"GK": "GKP", "DEF": "DEF", "MID": "MID", "FWD": "FWD"}

_PER_POSITION_SCORING_KEYS: tuple[str, ...] = (
    "goals_scored",
    "clean_sheets",
    "goals_conceded",
    "defensive_contribution",
)

# Field name here == dataclass field name on ScoringConfig == game_config's
# own scoring key. Kept identical on purpose so load_scoring_config can
# splat this dict straight into ScoringConfig's constructor without a
# rename table to keep in sync by hand.
_SCALAR_SCORING_KEYS: tuple[str, ...] = (
    "long_play",
    "short_play",
    "saves",
    "assists",
    "bonus",
    "yellow_cards",
    "red_cards",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
)

# See module docstring, "Two point values genuinely absent from
# game_config... and a third". Every one of these three is a checked,
# verified-unpublished constant, never a bare literal at the point of use.
MINUTE_CLIFF = 60
SAVES_POINTS_DIVISOR = 3
GOALS_CONCEDED_POINTS_DIVISOR = 2


class ScoringError(ValueError):
    """Raised on a config that cannot be honestly parsed, an unknown
    position, an outcome vector with an impossible value, or — the
    forward-only guard — an `as_of` for which no `game_config` observation
    exists (see module docstring, "FORWARD-ONLY, and why")."""


def _require_utc(name: str, value: datetime) -> datetime:
    """Same posture as `fplai.store`'s own private `_require_utc` — not
    imported from there (that name is private to `fplai.store`, and this
    module's owned-paths brief lists `store.py` as read-only, imported
    freely but not extended) — a naive datetime here is exactly the
    present-day-vs-historical leakage class CLAUDE.md rule 2 exists to
    prevent, so it is rejected at this boundary too, not just at
    `BitemporalStore`'s."""
    if value.tzinfo is None:
        raise ScoringError(f"{name} must be timezone-aware (got a naive datetime) — pass an explicit UTC datetime.")
    return value


@dataclass(frozen=True)
class RealisedOutcome:
    """One player-gameweek's realised outcome vector — every field is an
    FPL scoring identifier, already resolved to the grain `score_outcome`
    needs (see module docstring, "What the realised outcome vector does
    and does NOT resolve"). All counts are non-negative; validated in
    `__post_init__`, not left to fail silently deep inside `score_outcome`.

    This is deliberately NOT a model output — see module docstring's
    "Interface shape" section. It is fully realised (predicted-as-certain
    for a forward call, or actually-observed if the caller is scoring a
    single simulated draw from the six models' PMFs), never a probability.
    """

    minutes: int = 0
    goals_scored: int = 0
    assists: int = 0
    clean_sheet: bool = False
    goals_conceded: int = 0
    own_goals: int = 0
    penalties_saved: int = 0
    penalties_missed: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    saves: int = 0
    bonus: int = 0
    defensive_contribution_met: bool = False

    def __post_init__(self) -> None:
        counts = {
            "minutes": self.minutes,
            "goals_scored": self.goals_scored,
            "assists": self.assists,
            "goals_conceded": self.goals_conceded,
            "own_goals": self.own_goals,
            "penalties_saved": self.penalties_saved,
            "penalties_missed": self.penalties_missed,
            "yellow_cards": self.yellow_cards,
            "red_cards": self.red_cards,
            "saves": self.saves,
            "bonus": self.bonus,
        }
        negative = {k: v for k, v in counts.items() if v < 0}
        if negative:
            raise ScoringError(f"RealisedOutcome fields must be non-negative counts, got {negative}")
        # bonus is capped at 3 by FPL's own BPS-rank award rule (top 3
        # scorers in a fixture get 3/2/1) — fplai.models.bonus's own gate
        # already relies on {0,1,2,3} as the outcome space. A value outside
        # it is not a valid realised outcome, never silently clamped.
        if self.bonus > 3:
            raise ScoringError(f"bonus must be in 0..3 (FPL's own BPS-rank award rule), got {self.bonus}")


@dataclass(frozen=True)
class ScoringConfig:
    """The live scoring rule set, already parsed and position-remapped
    from `game_config`. Immutable and fully resolved — `score_outcome`
    never re-reads the store; every value it needs is already here. Build
    one with `load_scoring_config`, never by hand (a hand-built config in
    a test is fine — that is what `count_thresholds` overrides look like
    elsewhere in this codebase, e.g.
    `fplai.models.defensive_contribution.DCThresholdSet` — but production
    code always goes through the loader)."""

    valid_as_of: datetime
    """The deadline `load_scoring_config` was called with — NOT
    necessarily when the underlying `game_config` row was captured. Kept
    for provenance/logging on the composed object, never read by
    `score_outcome` itself."""
    observed_at: datetime
    """The resolved `game_config` row's own `observed_at` — when THIS
    payload was actually captured, which may be earlier than `valid_as_of`
    if `game_config` has not changed since."""
    goals_scored: Mapping[str, int]
    clean_sheets: Mapping[str, int]
    goals_conceded: Mapping[str, int]
    defensive_contribution: Mapping[str, int]
    long_play: int
    short_play: int
    saves: int
    assists: int
    bonus: int
    yellow_cards: int
    red_cards: int
    own_goals: int
    penalties_saved: int
    penalties_missed: int


def load_scoring_config(store: BitemporalStore, as_of: datetime) -> ScoringConfig:
    """LIVE-read every scoring point value from `game_config`, resolved
    bitemporally through the store's own `as_of` — never `store.latest()`,
    so this is safe to call from a reproducible, deterministic decision
    path (CLAUDE.md rule 7) rather than one that silently depends on
    wall-clock "now". `as_of` is required, not defaulted — see module
    docstring, "FORWARD-ONLY, and why", for what an `as_of` predating
    `game_config`'s own history does (raises `ScoringError`, on purpose).

    Raises `ScoringError` if: `as_of` is naive; no `game_config`
    observation exists at or before `as_of`; the payload has no `scoring`
    block; or the `scoring` block is missing a key or a position this
    module needs. Never guesses a default for a missing value — the same
    "raise, don't guess" posture `fplai.models.cards.read_card_points` and
    `fplai.models.defensive_contribution.read_dc_points_by_position`
    already take for the same dataset.
    """
    as_of = _require_utc("as_of", as_of)
    df = store.as_of("game_config", as_of)
    if df.is_empty():
        raise ScoringError(
            f"no game_config observation exists at or before {as_of.isoformat()} — "
            "game_config only ever carries the CURRENT season's rules (there is no "
            "historical equivalent in this store, and the FPL API exposes none). "
            "This is the forward-only guard this module's docstring describes: "
            "score_outcome/load_scoring_config exist to score PREDICTED FUTURE "
            "outcomes, never to re-derive historical points (blueprint §7.2, "
            "CLAUDE.md lesson 7 — 'score from stored total_points, never "
            "recompute'). If you are trying to score a past season or a past "
            "gameweek's realised outcome, stop and sum stored total_points "
            "instead; this loader will not help you do it correctly."
        )
    try:
        payload = json.loads(df["payload"][0])
    except (TypeError, ValueError) as exc:
        raise ScoringError(f"game_config payload at {as_of.isoformat()} is not valid JSON: {exc}") from exc

    scoring = payload.get("scoring")
    if not isinstance(scoring, dict):
        raise ScoringError(f"game_config payload has no 'scoring' block (got {type(scoring).__name__})")

    missing_keys = [k for k in _PER_POSITION_SCORING_KEYS + _SCALAR_SCORING_KEYS if k not in scoring]
    if missing_keys:
        raise ScoringError(f"game_config scoring block is missing key(s) {missing_keys}")

    per_position: dict[str, dict[str, int]] = {}
    for key in _PER_POSITION_SCORING_KEYS:
        raw = scoring[key]
        if not isinstance(raw, dict):
            raise ScoringError(f"game_config scoring.{key} is not a per-position dict, got {raw!r}")
        remapped: dict[str, int] = {}
        for position in POSITIONS:
            config_key = _CONFIG_POSITION_KEY[position]
            if config_key not in raw:
                raise ScoringError(f"game_config scoring.{key} is missing position {config_key!r} (raw: {raw!r})")
            remapped[position] = int(raw[config_key])
        per_position[key] = remapped

    scalars: dict[str, int] = {}
    for key in _SCALAR_SCORING_KEYS:
        raw_value = scoring[key]
        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
            raise ScoringError(f"game_config scoring.{key} is not numeric, got {raw_value!r}")
        scalars[key] = int(raw_value)

    observed_at = df["observed_at"][0]
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)

    return ScoringConfig(
        valid_as_of=as_of,
        observed_at=observed_at,
        goals_scored=per_position["goals_scored"],
        clean_sheets=per_position["clean_sheets"],
        goals_conceded=per_position["goals_conceded"],
        defensive_contribution=per_position["defensive_contribution"],
        **scalars,
    )


def score_outcome(outcome: RealisedOutcome, position: str, config: ScoringConfig) -> int:
    """The scoring rule. Pure and deterministic: the same
    `(outcome, position, config)` always returns the same int, no
    randomness, no store access, no clock read. See module docstring for
    the full identifier-by-identifier mapping and the three unpublished
    constants (`MINUTE_CLIFF`, `SAVES_POINTS_DIVISOR`,
    `GOALS_CONCEDED_POINTS_DIVISOR`) this arithmetic depends on.

    Raises `ScoringError` on an unknown `position`, or if `outcome.
    clean_sheet` is True while `outcome.minutes < MINUTE_CLIFF` — a real,
    unconditional FPL rule (clean sheet points require the appearance
    cliff to have been cleared), enforced here rather than trusted of the
    caller, because a caller that gets this wrong would otherwise be paid
    silently rather than failing loudly.
    """
    if position not in POSITIONS:
        raise ScoringError(f"unknown position {position!r}, expected one of {POSITIONS}")
    if outcome.clean_sheet and outcome.minutes < MINUTE_CLIFF:
        raise ScoringError(
            f"inconsistent RealisedOutcome: clean_sheet=True but minutes={outcome.minutes} "
            f"< MINUTE_CLIFF={MINUTE_CLIFF} — FPL never pays clean-sheet points below the "
            "appearance cliff; this is a caller bug, not a real outcome."
        )

    total = 0

    if outcome.minutes >= MINUTE_CLIFF:
        total += config.long_play
    elif outcome.minutes > 0:
        total += config.short_play

    if outcome.clean_sheet:
        total += config.clean_sheets[position]

    total += outcome.goals_scored * config.goals_scored[position]
    total += outcome.assists * config.assists
    total += (outcome.goals_conceded // GOALS_CONCEDED_POINTS_DIVISOR) * config.goals_conceded[position]
    total += outcome.own_goals * config.own_goals
    total += outcome.penalties_saved * config.penalties_saved
    total += outcome.penalties_missed * config.penalties_missed
    total += outcome.yellow_cards * config.yellow_cards
    total += outcome.red_cards * config.red_cards
    total += (outcome.saves // SAVES_POINTS_DIVISOR) * config.saves
    total += outcome.bonus * config.bonus

    if outcome.defensive_contribution_met:
        total += config.defensive_contribution[position]

    return total
