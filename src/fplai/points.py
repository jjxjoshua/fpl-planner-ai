"""Points-assembly layer — the keystone of Phase 3 (blueprint §4.3, §7.1/
§7.2, CLAUDE.md rules 1/2/3/5/7). Session `s005`, task `points-pmf-assembly`.

## What this module is

The ONE place in this codebase that imports all six outcome models
(`minutes`, `team_strength`, `attacking`, `defensive_contribution`,
`bonus`, `cards`) together with `fplai.scoring`, and composes their PMFs
into a single **player-gameweek points PMF** — a full probability mass
function over integer FPL points, never a scalar `xPts` (CLAUDE.md rule
5). Every other model module in `src/fplai/models/` deliberately does
NOT import its siblings ("composition, not import" — see e.g.
`fplai.models.bonus`'s and `fplai.models.attacking`'s own module
docstrings). This module is the documented exception: assembly is its
entire job, so it is the one place cross-model coupling is allowed and
required.

**Computed on demand, never persisted.** Phase 3's brief is explicit that
persisting this PMF is a Phase 4 caching concern — this module never
calls `fplai.derived.write_derived`, never touches `fplai.schemas`, and
has no `store.write()` anywhere. It reads nothing from the store either:
every input (fitted model params, a `ScorelinePMF`, per-model feature
rows, a `ScoringConfig`) is supplied by the caller, exactly like every
`predict_*_pmf` function in `fplai.models.*` already does. Building a
LIVE feature-row pipeline (trailing stats for an upcoming, not-yet-played
gameweek, read from the store as of "now") is a separate, not-yet-built
prerequisite — out of scope here (see "What this module does NOT do"
below).

## The composition design, and why

Blueprint §4.3 and this task's brief both point at the same fact: bonus
is a rank-within-fixture phenomenon (`fplai.models.bonus`'s own module
docstring), so a player's points distribution is **not** independent of
their fixture's other 21+ participants. `predict_bonus_pmfs_for_fixture`
already reflects this at the marginal level (it takes a whole fixture,
not one player), and the honest composition this module builds on top of
it is a **seeded Monte Carlo over the whole fixture**:

1. Draw ONE joint scoreline `(home_goals, away_goals)` from the fixture's
   `ScorelinePMF` (`fplai.models.team_strength`), shared by every player
   in the fixture for that simulation draw — this is what lets two
   team-mates' attacking returns, and a defender's clean-sheet chance,
   correlate through the SAME underlying match result rather than being
   drawn independently. It also preserves whatever weak home/away
   dependence the Dixon-Coles `rho` correction encodes (module
   docstring, `fplai.models.team_strength`, "The low-score correction").
2. Draw, per player, a minute BAND (one of `fplai.models.minutes.
   MINUTE_BANDS`) from that player's own `MinutesPMF` (band-level
   marginal, collapsing over START/SUB/UNUSED — see "Minutes: band, not
   state" below). This SAME per-simulation band value is then reused,
   for that player, everywhere minutes matter: goals/assists thinning,
   defensive contribution, cards, saves, appearance points, and the
   clean-sheet minutes cliff — never redrawn per model, so a simulation
   where a player is subbed off in the 40th minute consistently shows a
   subbed-off player's reduced BPS-adjacent risk AND reduced attacking
   opportunity AND reduced card exposure in that SAME draw.
3. Thin that simulation's own team-goals draw into the player's
   goals/assists via `fplai.models.attacking`'s fitted Binomial share,
   evaluated at that draw's own minute band (see "The n=1 trick" below
   for how this is done without touching `attacking.py`'s private
   design-matrix helpers).
4. Draw a defensive-contribution count and a cards outcome from
   `fplai.models.defensive_contribution`/`fplai.models.cards`,
   conditioned on that same minute band.
5. Draw a saves count IF a saves predictor was supplied (see "The saves
   seam" below) — otherwise the hole is carried forward, visibly, never
   silently zeroed.
6. Derive clean-sheet / goals-conceded directly from step 1's shared
   scoreline draw (see "Clean sheets and goals conceded" below).
7. Run the resulting `fplai.scoring.RealisedOutcome` through
   `fplai.scoring.score_outcome` (the live, forward-only, rule-set-
   correct scoring function) to get ONE integer points draw.
8. Repeat `n_simulations` times; the empirical histogram of points draws
   IS the player's points PMF (`PointsPMF`, dense integer support,
   `__post_init__` enforces it sums to 1.0 — same discipline every
   sibling `*PMF` dataclass in this codebase already carries).

## Correlation structure captured, and what is explicitly left to Phase 5

**Captured (within one fixture, one simulation draw):**
- Team-mates' attacking returns correlate through a shared team-goals
  draw (a 4-0 draw gives every attacker on that side a better goals/
  assists draw in the SAME simulation, not independently).
- A team's clean-sheet outcome is shared exactly across every one of its
  players who cleared the minutes cliff in that draw.
- A player's OWN minutes band is shared across every outcome that
  depends on it (goals/assists, DC, cards, saves, appearance points,
  clean-sheet eligibility) — no "started for goals, subbed for cards"
  inconsistency within one draw.
- Bonus's RANK structure across every player in the fixture — the
  reason `predict_bonus_pmfs_for_fixture` takes a whole fixture, not one
  player — is fully present in the `BonusPMF` this module draws from
  (see "Bonus: fixture-joint marginal, drawn i.i.d." below for the one
  place this module trades away a slice of fidelity, and why).

**NOT captured, by design, and explicitly out of scope per this task's
brief (blueprint §7, E8, Phase 5):**
- Cross-FIXTURE correlation (a captain's home fixture and a defender's
  away fixture in the SAME gameweek are simulated independently — no
  shared "which way did the whole gameweek's variance go" draw). Full
  correlated Monte Carlo across a gameweek's fixtures is Phase 5's job
  (rank-aware objective, EO), not this module's.
- The bonus/minutes-and-goals correlation WITHIN one player's own draw
  (see "Bonus: fixture-joint marginal, drawn i.i.d." — a genuinely
  correlated version would need bonus's own internal per-simulation BPS
  draws exposed and re-synchronised against this module's own minute-
  band draws, at a cost this task's brief explicitly scopes out: "you do
  not need a correlated Monte Carlo across the whole gameweek").

## The n=1 trick — extracting attacking's fitted share WITHOUT touching
## its private design-matrix helpers

`fplai.models.attacking.predict_attacking_pmf(team_goals_marginal=[(n,
w)], minute_exposure=[(m, w)], stat=stat, max_count=1)`, called with a
POINT MASS at `team_goals_marginal=[(1, 1.0)]`, returns a `Binomial(1,
p)` PMF — `.probabilities[1]` IS `p` exactly (a 1-trial Binomial's P(1)
equals its own success probability, no approximation, no truncation risk
at `max_count=1`). This module calls it once per player per stat per
minute band (12 calls per player: 2 stats x 6 bands) to get
`p_effective_by_band`, then draws `Binomial(team_goals_draw, p_effective)`
directly via `numpy.random.Generator.binomial` — vectorised across all
`n_simulations` draws at once, using the model's OWN production
`predict_attacking_pmf` code path to compute `p`, never a re-derivation
of its sigmoid math. The same reasoning extends to defensive contribution
and cards, which are drawn from the model's own per-band DISCRETE PMFs
(`predict_dc_pmf`/`predict_cards_pmf`, minute_exposure=a single-band point
mass) via a weighted `rng.choice`, grouped by which simulations landed in
which band (`_draw_by_band_groups`) — again the model's own code path,
never a duplicated distributional formula.

## Bonus: fixture-joint marginal, drawn i.i.d. — the one deliberate
## fidelity trade-off, argued rather than assumed

A fully joint composition would need bonus's own internal Monte Carlo
(`predict_bonus_pmfs_for_fixture`'s `n_simulations` BPS draws, bootstrapped
per player) re-run ONCE PER OUTER SIMULATION, conditioned on that outer
draw's own per-player minute realisation, so that bonus/minutes/goals
correlate within one draw for one player. This is computationally
prohibitive at realistic scale: bonus's own pairwise rank computation is
`O(n_players^2)` per inner draw (`fplai.models.bonus`'s own module
docstring), and nesting it inside an outer `n_simulations`-deep loop
multiplies the two draw counts together (e.g. an outer 2,000 draws x an
inner 4,000 draws x a ~40-player fixture would be several orders of
magnitude more expensive than either loop alone, for a marginal gain
this module's other four outcomes do not need).

**What this module does instead**: call `predict_bonus_pmfs_for_fixture`
ONCE per fixture, with every player's REAL minute-exposure mixture (not a
point mass) — this is exactly bonus's own sanctioned interface, and its
result already reflects the full cross-player rank competition (the
structurally important correlation bonus's own docstring insists on).
Then, for each player, each outer simulation draws a bonus-points value
INDEPENDENTLY from that ONE fixed marginal PMF (`rng.choice`, using the
module's shared OUTER rng stream, so the overall composition stays a
pure function of one seed — see "Determinism" below). The cross-player
rank correlation bonus needs is fully present in the marginal itself;
what is lost is the WITHIN-player correlation between "this draw's own
minutes/goals realisation" and "this draw's own bonus points" for the
SAME player — a player who, in one specific outer draw, is subbed off
early will still occasionally draw a high bonus value from their overall
marginal, even though a genuinely joint draw would suppress that
specific combination. **This is a stated, argued, cheaper composition,
not a silent assumption of independence** (this task's brief's own
distinction) — it trades away bonus's correlation with THIS module's
OTHER four outcomes, for the SAME player, in the SAME draw, while fully
preserving bonus's own cross-player rank structure, which is the
correlation blueprint §4 and `fplai.models.bonus` actually call out as
load-bearing. A future Phase 5 revision wanting the fuller joint would
need to expose bonus's internal per-draw BPS array (a private
`_assign_bonus_points_batch` output today) as a public seam — not
attempted here, named as a finding instead.

## Minutes: band, not state

Every downstream model in this codebase (`defensive_contribution`,
`attacking`, `cards`, `bonus`) already consumes a `minute_exposure:
Sequence[tuple[float, float]]` mixture over BAND MIDPOINTS, never over
`fplai.models.minutes.STATES` — a "SUB, 30 minutes" and a "START, 30
minutes" (impossible in practice, but illustrative) would be treated
identically by every one of those models, because none of them takes a
state argument. This module follows the same convention: `MinutesPMF.
joint()` is collapsed to a 6-entry BAND marginal (summing across STATES
for each band) before any per-simulation draw happens, via `_band_
marginal`. `fplai.models.minutes.MINUTE_BANDS` supplies the band labels
(imported — it is this module's own public vocabulary, not a private
helper); the band MIDPOINT values (`0.0, 15.0, 45.0, 67.0, 82.0, 90.0`)
are declared here BY VALUE, not imported from `minutes.py`'s private
`_BAND_MIDPOINT` — the same "documented by value, not imported" posture
`fplai.models.bonus`'s own module docstring already takes for the
identical constant, for the identical reason (a private name is not this
module's to depend on).

## Clean sheets and goals conceded — a stated, unmeasured-direction
## simplification

FPL's real `clean_sheet`/`goals_conceded` are defined **while the player
was on the pitch**, i.e. genuinely goal-TIMING-dependent (`fplai.scoring`'s
own module docstring makes the same point about the fields it consumes).
None of the six outcome models in this codebase produce a goal-timing
distribution — only a fixture-level final scoreline (`ScorelinePMF`).
This module's approximation: a player who cleared the minutes cliff
(`>= fplai.scoring.MINUTE_CLIFF`, i.e. drew the `"60-74"`, `"75-89"`, or
`"90+"` band) in a simulation gets `clean_sheet = True` iff that
simulation's own opponent-goals draw is exactly 0; `goals_conceded`
credits the FULL match opponent-goals draw to any player who appeared at
all (minutes band != `"0"`). This overstates goals-conceded exposure for
a player subbed off before a late opponent goal, and understates it for
a late substitute who was not on the pitch for an earlier one — the
NET DIRECTION of this bias is genuinely unmeasured (CLAUDE.md's "measure
the size of the prize" standard applies to the *fix*, not this omission;
no measurement was attempted here, and none is claimed). Stated as a
named limitation for a future session, not silently assumed harmless.
Because `score_outcome` itself enforces `clean_sheet=True` only when
`minutes >= MINUTE_CLIFF` (raises `ScoringError` otherwise), this
module's own minutes-band gating on clean sheets is required for
correctness, not merely for realism — getting it wrong would not
silently mis-score, it would crash every affected draw.

## Own goals, penalties saved, penalties missed — DECLARED zero, not
## silently omitted

No model in this codebase predicts any of these three `fplai.scoring.
RealisedOutcome` fields, and this module does not invent one:
`own_goals`, `penalties_saved`, `penalties_missed` are always `0` in
every simulated `RealisedOutcome` this module constructs. This is a
DECLARED zero, not a silent one, per this task's brief's own standard —
stated here, carried into every `PointsPMF.caveats` tuple this module
produces (see `_OWN_GOAL_PENALTY_CAVEAT`), and sized against the real
store rather than merely asserted rare: own goals are nonzero in 264 of
179,950 real player-fixture rows (0.15%, verified live this session
against `vaastav_player_gameweek_stats`); penalties missed/saved are
nonzero in 64/49 rows respectively over the 2022-23+ window
(`fplai.models.attacking`'s own module docstring, cited rather than
re-measured). All three are rare enough that a declared zero is a
defensible simplification, not a material one — but "rare" is a measured
claim here, not an assumption, per the standing house discipline.

## The saves seam — a marked, tested hole, never a silent zero

`src/fplai/models/saves.py` did not exist for most of this session
(verified: `ls src/fplai/models/` before writing a line of this module)
and this module is FORBIDDEN from creating or editing it — owned by a
parallel XL-Coder this session, and its files landed on disk only near
this task's punch-out, uncommitted, with no `punch_in`/`punch_out` of its
own yet visible in `.punchcard/s005.jsonl` at the time it was observed —
treated as still in flight, not a stable interface to import against.
This module therefore accepts an OPTIONAL, caller-supplied
`saves_predict_fn: SavesPredictFn | None` — dependency injection, never
an import of `fplai.models.saves` itself, regardless of whether that
module happens to exist on disk at call time.

**The call shape was corrected once `saves.py` appeared.** An earlier
version of this seam assumed the single-mixture shape every OTHER sibling
`predict_*_pmf` in this codebase uses (`feature_row`, `element`,
`fixture`, `minute_exposure`) — reasonable, since five of six existing
models share exactly that shape. Once `fplai.models.saves.
predict_saves_pmf`'s real signature became readable (reading a public
signature is not importing or editing the module, and is how this
correction was made), it turned out to need a SECOND mixture,
`opponent_goals_marginal: Sequence[tuple[int, float]]` — the same
two-mixture generalisation `fplai.models.attacking.predict_
attacking_pmf` already establishes for `team_goals_marginal` x
`minute_exposure`, and for the same reason: shots faced (hence saves)
scale with the opponent's attacking output. `SavesPredictFn`'s documented
contract was corrected to match, and `_draw_saves_by_band_and_goals`
conditions each simulation's saves draw on BOTH its own band draw and its
own opponent-goals draw — both already available from step 1's shared
scoreline draw, so no new randomness is introduced. Wiring in the real
`fplai.models.saves.predict_saves_pmf` should still be a small,
mechanical change at the call site (bind `params` via `functools.
partial`, matching this contract's remaining keyword names exactly) —
verified against the real signature, not merely hoped.

If `saves_predict_fn` is `None` (the default) or a GK player's
`PlayerFixtureFeatures.saves_feature_row` is `None`, that player's
`PointsPMF.saves_status` is set to `SAVES_STATUS_NOT_YET_MODELLED` — a
STRUCTURAL field on every GK's output, impossible to mistake for a
complete GK distribution by reading the dataclass alone (mirrors
`fplai.models.minutes.MinutesPMF.calibration_method`'s "structural
provenance, not a docstring promise" precedent) — and `saves` is held at
a declared `0` in every draw, exactly like own_goals/penalties above, but
flagged with its OWN dedicated status field rather than folded into the
generic caveats tuple, because saves are NOT a rare, safely-ignorable
outcome the way own goals are: `fplai.models.saves` (per this task's
brief) will cover 18.9% of GK points, 0.65 pts/appearance, with a
measured, DIFFERENTIAL bias (`docs/HANDOFF.md` §3: keepers on bad
defences lose ~0.21 pts/app more than keepers on good defences from this
omission) — a biased hole, not a noisy one. Every non-GK player gets
`SAVES_STATUS_NOT_APPLICABLE` (saves genuinely do not apply to outfield
positions — FPL's own scoring rule pays `saves` points to no position
other than GK).

## Determinism (CLAUDE.md rule 7)

Exactly ONE `numpy.random.default_rng(config.seed)` instance drives every
draw in `simulate_fixture_points_pmfs` except bonus's own internal
Monte Carlo (which is independently seeded via `predict_bonus_pmfs_for_
fixture`'s own `seed` kwarg, reusing `config.seed` — a SEPARATE generator
object, so it cannot perturb this module's own draw sequence or vice
versa). Every draw happens in a FIXED order: the fixture's shared
scoreline draw first, then players in ASCENDING `element` order (the
input `players` sequence is sorted at the top of `simulate_fixture_
points_pmfs`, never trusted to already be in a stable order — the same
"do not trust upstream ordering" lesson Phase 1's `greedy_form` bug
taught this project the hard way, blueprint HANDOFF §2, "polars'
`.unique(maintain_order=False)`"), and within each player, band, then
goals, then assists, then DC, then cards, then saves (if modelled), then
bonus — always this order, never conditional on data. `tests/
test_points.py::test_simulate_fixture_points_pmfs_is_bit_identical_
across_repeated_calls_with_the_same_seed` proves two full runs against
identical inputs and the same seed produce byte-identical
`PointsPMF.probabilities` tuples, and a THIRD run with a different seed
produces a measurably different result — a determinism test that only
proves the happy path (same seed -> same output) without also proving a
DIFFERENT seed changes something would be exactly the "test that cannot
fail" class blueprint's lesson 5 warns about.

## What this module does NOT do

- Does not build live feature rows from the store. Every `*_feature_row`
  field on `PlayerFixtureFeatures` is caller-supplied, already in the
  exact shape each model's own `predict_*_pmf` requires (see each
  model's own docstring for its feature vocabulary) — a live "read the
  store as of now, build today's trailing features for an unplayed
  gameweek" pipeline is a separate, not-yet-built prerequisite (`docs/
  HANDOFF.md`, "Phase 3 has two unwritten prerequisites" names the
  scoring function and this PMF layer; a THIRD, a generated live decision
  brief, is referenced elsewhere in the same document as a later Phase 3
  deliverable, not this one).
- Does not persist anything. No `store.write()`, no `write_derived`, no
  `fplai.schemas` import.
- Does not touch `src/fplai/models/saves.py` (forbidden this session —
  see "The saves seam").
- Does not attempt cross-fixture or gameweek-wide correlation (Phase 5,
  blueprint §7 E8 — see "Correlation structure captured").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence, runtime_checkable

import numpy as np
import polars as pl

from fplai.models.attacking import AttackingModelParams, predict_attacking_pmf
from fplai.models.bonus import BonusModelParams, BonusPlayerInput, predict_bonus_pmfs_for_fixture
from fplai.models.cards import CardsModelParams, predict_cards_pmf
from fplai.models.defensive_contribution import DCModelParams, predict_dc_pmf
from fplai.models.minutes import MINUTE_BANDS, MinutesModelParams, MinutesPMF, STATES, predict_minutes_pmf
from fplai.models.team_strength import ScorelinePMF
from fplai.scoring import MINUTE_CLIFF, RealisedOutcome, ScoringConfig, score_outcome

# House vocabulary — declared independently rather than imported, same
# convention every fplai.models.* module already follows for this exact
# four-tuple (module docstring, "Composition, not import").
POSITIONS: tuple[str, ...] = ("GK", "DEF", "MID", "FWD")

# Band midpoints, in fplai.models.minutes.MINUTE_BANDS order — declared
# BY VALUE, not imported (module docstring, "Minutes: band, not state").
# Same six values fplai.models.minutes' private _BAND_MIDPOINT carries,
# and the same values fplai.models.bonus's own module docstring cites for
# the identical reason.
_BAND_MIDPOINTS: tuple[float, ...] = (0.0, 15.0, 45.0, 67.0, 82.0, 90.0)
assert len(_BAND_MIDPOINTS) == len(MINUTE_BANDS)

# Module docstring, "Own goals, penalties saved, penalties missed —
# DECLARED zero, not silently omitted".
_OWN_GOAL_PENALTY_CAVEAT = (
    "own_goals, penalties_saved, penalties_missed held at a DECLARED zero in every "
    "simulated draw -- no model in this codebase predicts them (own_goals nonzero in "
    "264/179,950 = 0.15% of real player-fixture rows, verified live this session; "
    "penalties_missed/saved nonzero in 64/49 rows over the 2022-23+ window, "
    "fplai.models.attacking's own citation). See fplai.points module docstring."
)

_CS_GC_CAVEAT = (
    "clean_sheet/goals_conceded are approximated from the fixture's final scoreline "
    "draw with no goal-timing model (none of the six outcome models produce one) -- "
    "a player who cleared the minutes cliff is credited a clean sheet iff the opponent's "
    "drawn goal count is exactly 0, and any appearing player is credited the FULL match "
    "goals-conceded count. Net bias direction unmeasured. See fplai.points module "
    "docstring, 'Clean sheets and goals conceded'."
)

_BONUS_IID_CAVEAT = (
    "bonus points are drawn i.i.d. per simulation from this fixture's own joint-rank "
    "marginal BonusPMF -- the cross-player rank correlation bonus needs is fully "
    "present, but bonus is NOT resynchronised against this same draw's own minutes/"
    "goals realisation for the same player. See fplai.points module docstring, "
    "'Bonus: fixture-joint marginal, drawn i.i.d.'."
)

SAVES_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
SAVES_STATUS_MODELLED = "MODELLED"
SAVES_STATUS_NOT_YET_MODELLED = "NOT_YET_MODELLED"
SAVES_STATUSES: tuple[str, ...] = (SAVES_STATUS_NOT_APPLICABLE, SAVES_STATUS_MODELLED, SAVES_STATUS_NOT_YET_MODELLED)


class PointsError(ValueError):
    """Misuse or a genuine data problem this module refuses to guess past
    -- a player whose declared team is neither side of the supplied
    fixture, an unknown position, a probability vector that will not
    (re)normalise, or a PMF that would not sum to 1."""


# ---------------------------------------------------------------------------
# The saves seam (module docstring, "The saves seam").
# ---------------------------------------------------------------------------


@runtime_checkable
class SavesPMFLike(Protocol):
    """The minimal duck-typed shape this module needs from
    `fplai.models.saves.SavesPMF` -- `counts`/`probabilities`, the same
    two fields every `*PMF` dataclass in `fplai.models.*` already carries
    (module docstring). This module still never IMPORTS `fplai.models.
    saves` (forbidden this session -- owned by a parallel XL-Coder) even
    though that module's files landed on disk mid-session (observed at
    punch-out, uncommitted, no punch-in/punch-out of its own visible in
    `.punchcard/s005.jsonl` at the time of observation -- treated as
    still in flight, not a stable interface to depend on directly); a
    caller supplies a `saves_predict_fn` shaped like this instead. The
    CONTRACT below (`SavesPredictFn`) was corrected against the real
    `fplai.models.saves.predict_saves_pmf` signature once it appeared on
    disk -- reading a sibling module's public signature to get an
    interoperability contract right is not the same as importing or
    editing it."""

    counts: tuple[int, ...]
    probabilities: tuple[float, ...]


# `feature_row` positional, `element`/`fixture`/`minute_exposure`/
# `opponent_goals_marginal` keyword-only -- mirrors `fplai.models.saves.
# predict_saves_pmf`'s REAL signature (corrected, session s005, once that
# module appeared on disk -- see `SavesPMFLike`'s docstring). Saves is a
# TWO-mixture composition, the same generalisation `fplai.models.
# attacking.predict_attacking_pmf` establishes for `team_goals_marginal`
# x `minute_exposure` (module docstring, "The saves seam") -- an earlier
# version of this contract, written before `saves.py` existed, assumed
# the simpler single-mixture shape `predict_cards_pmf` uses; that
# assumption was WRONG and is corrected here rather than left as a
# plausible-looking but inaccurate seam. Typed loosely (Callable[...,
# SavesPMFLike]) because a Protocol cannot express keyword-only
# parameters precisely; the real contract is documented here, in prose,
# and exercised by `tests/test_points.py`'s stub predictor.
SavesPredictFn = Callable[..., SavesPMFLike]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlayerFixtureFeatures:
    """One player's inputs to a fixture-joint points simulation. Every
    `*_feature_row` is caller-built, already in the EXACT shape that
    model's own `predict_*_pmf` requires (see each model's own docstring
    for its required keys) -- this module does not re-derive or validate
    those shapes itself; each `predict_*_pmf` call already raises a
    precise, named error if a key is missing (module docstring, "What
    this module does NOT do").

    - `minutes_feature_row` -- `fplai.models.minutes.predict_minutes_pmf`'s
      required shape (its own `numeric_columns` plus `position`/`team`).
    - `attacking_feature_row` -- `fplai.models.attacking.
      predict_attacking_pmf`'s shape (its own `numeric_columns` plus
      `position`; never `minutes`/`minutes_frac` -- supplied via the band
      draw instead).
    - `dc_feature_row` -- `fplai.models.defensive_contribution.
      predict_dc_pmf`'s shape (its own `numeric_columns`; no `position`
      key -- that model takes `position` as a separate argument).
    - `cards_feature_row` -- `fplai.models.cards.predict_cards_pmf`'s
      shape (its own `numeric_columns` plus `position`).
    - `bonus_feature_row` -- `fplai.models.bonus.BonusPlayerInput`'s
      shape (its own `numeric_columns`; explicitly WITHOUT `position` or
      `minutes_frac` -- `BonusPlayerInput.__post_init__` raises if either
      is present).
    - `saves_feature_row` -- `None` unless `saves_predict_fn` is also
      supplied to `simulate_fixture_points_pmfs` AND this player is a GK
      (module docstring, "The saves seam"); shape is whatever the
      eventual `fplai.models.saves` module requires -- undefined here.
    """

    element: int
    position: str
    team: str
    is_home: bool
    minutes_feature_row: dict
    attacking_feature_row: dict
    dc_feature_row: dict
    cards_feature_row: dict
    bonus_feature_row: dict
    saves_feature_row: dict | None = None

    def __post_init__(self) -> None:
        if self.position not in POSITIONS:
            raise PointsError(f"unknown position {self.position!r} for element={self.element} -- expected one of {POSITIONS}")


@dataclass(frozen=True)
class PointsSimulationConfig:
    """Every simulation hyperparameter, in one place -- same convention
    every `fplai.models.*` fitting config already establishes (CLAUDE.md
    rule 4's spirit, applied to a simulation hyperparameter rather than a
    scoring/threshold value)."""

    seed: int = 0
    """The single seed driving this module's own rng stream (module
    docstring, "Determinism"). Also reused, unmodified, as the `seed`
    passed to `predict_bonus_pmfs_for_fixture`'s OWN independent
    generator -- see that section for why sharing the integer value does
    not couple the two streams."""

    n_simulations: int = 2000
    """Outer draw count. Every player in a fixture gets exactly this many
    draws. Bounded above by cost: `n_simulations` calls to
    `fplai.scoring.score_outcome` (a pure Python function, no numpy) PER
    PLAYER -- 2,000 is a stated, reasoned starting point balancing
    resolution against a real gameweek's ~10 fixtures x ~30-40 players,
    not a calibrated optimum."""

    bonus_n_simulations: int | None = None
    """Overrides `BonusModelParams.config.n_simulations` for the ONE
    per-fixture call to `predict_bonus_pmfs_for_fixture` this module
    makes (module docstring, "Bonus: fixture-joint marginal"). `None`
    (the default) defers to that model's own configured default."""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PointsPMF:
    """The model's actual output for one player-fixture: a full,
    DENSE-support PMF over integer FPL points -- never a scalar (CLAUDE.md
    rule 5). `expected_points()`/`variance()`/`std()` are convenience
    properties computed FROM this PMF, the same relationship every
    sibling `*PMF` dataclass's own convenience methods have to their own
    distribution (e.g. `fplai.models.minutes.MinutesPMF.expected_minutes
    ()`, `fplai.models.defensive_contribution.DCPMF.expected_dc_points
    ()`) -- never a substitute for reading the PMF itself."""

    element: int
    fixture: int
    position: str
    points: tuple[int, ...]
    probabilities: tuple[float, ...]
    n_simulations: int
    seed: int
    saves_status: str
    caveats: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.position not in POSITIONS:
            raise PointsError(f"unknown position {self.position!r}")
        if self.saves_status not in SAVES_STATUSES:
            raise PointsError(f"unknown saves_status {self.saves_status!r} -- expected one of {SAVES_STATUSES}")
        if len(self.points) != len(self.probabilities):
            raise PointsError("points and probabilities must be the same length")
        if list(self.points) != list(range(self.points[0], self.points[0] + len(self.points))):
            raise PointsError(f"points support must be a DENSE consecutive integer range, got {self.points}")
        total = sum(self.probabilities)
        if abs(total - 1.0) > 1e-6:
            raise PointsError(f"PointsPMF probabilities do not sum to 1.0 (got {total}) for element={self.element}")
        if self.n_simulations <= 0:
            raise PointsError(f"n_simulations must be positive, got {self.n_simulations}")

    def expected_points(self) -> float:
        return sum(p * prob for p, prob in zip(self.points, self.probabilities))

    def variance(self) -> float:
        mean = self.expected_points()
        return sum(prob * (p - mean) ** 2 for p, prob in zip(self.points, self.probabilities))

    def std(self) -> float:
        return self.variance() ** 0.5

    def p_at_least(self, threshold: int) -> float:
        return sum(prob for p, prob in zip(self.points, self.probabilities) if p >= threshold)

    def to_polars(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "element": [self.element] * len(self.points),
                "fixture": [self.fixture] * len(self.points),
                "position": [self.position] * len(self.points),
                "points": list(self.points),
                "probability": list(self.probabilities),
                "n_simulations": [self.n_simulations] * len(self.points),
                "seed": [self.seed] * len(self.points),
                "saves_status": [self.saves_status] * len(self.points),
            }
        )


# ---------------------------------------------------------------------------
# Small numeric helpers -- pure, no rng, no store, no randomness.
# ---------------------------------------------------------------------------


def _normalize(p: np.ndarray) -> np.ndarray:
    total = float(p.sum())
    if total <= 0.0:
        raise PointsError(f"cannot normalise a probability vector that sums to {total} (<= 0)")
    return p / total


def _band_marginal(pmf: MinutesPMF) -> np.ndarray:
    """Collapse a `MinutesPMF`'s (state, band) joint to a 6-entry BAND
    marginal, in `fplai.models.minutes.MINUTE_BANDS` order (module
    docstring, "Minutes: band, not state")."""
    out = np.zeros(len(MINUTE_BANDS), dtype=np.float64)
    for state in STATES:
        bands = pmf.band_given_state[state]
        p_state = pmf.p_state[state]
        for i, p_band in enumerate(bands):
            out[i] += p_state * p_band
    return _normalize(out)


def _histogram_to_pmf(draws: np.ndarray) -> tuple[tuple[int, ...], tuple[float, ...]]:
    """Dense integer-support histogram, `min(draws)..max(draws)` inclusive
    -- same "dense support, zero-filled gaps" convention every sibling
    `*PMF`'s `counts`/`probabilities` pair already uses (e.g.
    `fplai.models.defensive_contribution.DCPMF`, `0..max_count`)."""
    lo = int(draws.min())
    hi = int(draws.max())
    offsets = (draws - lo).astype(np.int64)
    counts = np.bincount(offsets, minlength=hi - lo + 1)
    probs = counts.astype(np.float64) / draws.shape[0]
    support = tuple(range(lo, hi + 1))
    return support, tuple(float(v) for v in probs)


def _draw_by_band_groups(rng: np.random.Generator, band_idx: np.ndarray, pmfs_by_band: Sequence) -> np.ndarray:
    """Given a per-simulation band index array and one discrete PMF per
    band (each carrying `.counts`/`.probabilities`), draw one value per
    simulation from the PMF matching that simulation's own band -- the
    grouped-`rng.choice` trick module docstring's "The n=1 trick" section
    describes for defensive contribution / cards / saves. Iterates bands
    in a FIXED order (0..len-1) regardless of which bands are actually
    populated, so the rng consumption order is a pure function of
    `band_idx`'s own values, never of dict/set iteration order (CLAUDE.md
    rule 7)."""
    n = band_idx.shape[0]
    out = np.zeros(n, dtype=np.int64)
    for b, pmf in enumerate(pmfs_by_band):
        mask = band_idx == b
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        probs = _normalize(np.asarray(pmf.probabilities, dtype=np.float64))
        counts = np.asarray(pmf.counts, dtype=np.int64)
        out[mask] = rng.choice(counts, size=cnt, p=probs)
    return out


def _conditional_attacking_p_by_band(
    params: AttackingModelParams, feature_row: dict, *, stat: str, element: int, fixture: int
) -> np.ndarray:
    """`p_effective_by_band[b]` -- module docstring, "The n=1 trick"."""
    out = np.zeros(len(_BAND_MIDPOINTS), dtype=np.float64)
    for i, m in enumerate(_BAND_MIDPOINTS):
        pmf = predict_attacking_pmf(
            params,
            feature_row,
            element=element,
            fixture=fixture,
            stat=stat,
            team_goals_marginal=[(1, 1.0)],
            minute_exposure=[(m, 1.0)],
            max_count=1,
        )
        out[i] = pmf.probabilities[1]
    return out


def _dc_pmfs_by_band(params: DCModelParams, feature_row: dict, *, position: str, element: int, fixture: int):
    return [
        predict_dc_pmf(params, feature_row, element=element, fixture=fixture, position=position, minute_exposure=[(m, 1.0)])
        for m in _BAND_MIDPOINTS
    ]


@dataclass(frozen=True)
class _DiscretePMF:
    """Uniform `.counts`/`.probabilities` shape `_draw_by_band_groups`
    needs -- `fplai.models.cards.CardsPMF` names its support `outcomes`
    (0/1/2 = NONE/YELLOW/RED), not `counts` (its own module docstring's
    house vocabulary, matching `fplai.models.bonus.BonusPMF`'s `counts` of
    bonus POINTS rather than an event count) -- adapted here rather than
    changing `_draw_by_band_groups`'s own contract to special-case one
    caller."""

    counts: tuple[int, ...]
    probabilities: tuple[float, ...]


def _cards_pmfs_by_band(params: CardsModelParams, feature_row: dict, *, element: int, fixture: int):
    out = []
    for m in _BAND_MIDPOINTS:
        pmf = predict_cards_pmf(params, feature_row, element=element, fixture=fixture, minute_exposure=[(m, 1.0)])
        out.append(_DiscretePMF(counts=pmf.outcomes, probabilities=pmf.probabilities))
    return out


def _draw_saves_by_band_and_goals(
    rng: np.random.Generator,
    predict_fn: SavesPredictFn,
    feature_row: dict,
    *,
    element: int,
    fixture: int,
    band_idx: np.ndarray,
    opp_goals_arr: np.ndarray,
) -> np.ndarray:
    """Saves' real interface (session s005 correction -- see module
    docstring, "The saves seam") is a TWO-mixture composition, the same
    generalisation `fplai.models.attacking.predict_attacking_pmf`
    establishes for `team_goals_marginal` x `minute_exposure`: saves needs
    `minute_exposure` AND `opponent_goals_marginal` (shots faced scale
    with the opponent's attacking output, which this module already draws
    once per fixture as the shared scoreline -- module docstring §2 step
    1). This function conditions each simulation's saves draw on BOTH its
    own band draw AND its own opponent-goals draw (`opp_goals_arr`,
    already computed from the SAME shared scoreline draw every other
    outcome uses), via point-mass `minute_exposure`/`opponent_goals_
    marginal` pairs, grouped and sampled exactly like `_draw_by_band_
    groups` but over the unique `(band, opponent_goals)` pairs actually
    drawn -- `np.unique(..., axis=0)` returns rows in a fixed, sorted
    order, so iteration order is a pure function of the input arrays, not
    of dict/set ordering (CLAUDE.md rule 7)."""
    n = band_idx.shape[0]
    out = np.zeros(n, dtype=np.int64)
    pairs = np.stack([band_idx, opp_goals_arr], axis=1)
    for b, g in np.unique(pairs, axis=0):
        mask = (band_idx == b) & (opp_goals_arr == g)
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        pmf = predict_fn(
            feature_row,
            element=element,
            fixture=fixture,
            minute_exposure=[(_BAND_MIDPOINTS[int(b)], 1.0)],
            opponent_goals_marginal=[(int(g), 1.0)],
        )
        probs = _normalize(np.asarray(pmf.probabilities, dtype=np.float64))
        counts = np.asarray(pmf.counts, dtype=np.int64)
        out[mask] = rng.choice(counts, size=cnt, p=probs)
    return out


# ---------------------------------------------------------------------------
# The main entry point.
# ---------------------------------------------------------------------------


def simulate_fixture_points_pmfs(
    *,
    fixture: int,
    scoreline: ScorelinePMF,
    minutes_params: MinutesModelParams,
    attacking_params: AttackingModelParams,
    dc_params: DCModelParams,
    cards_params: CardsModelParams,
    bonus_params: BonusModelParams,
    scoring_config: ScoringConfig,
    players: Sequence[PlayerFixtureFeatures],
    config: PointsSimulationConfig = PointsSimulationConfig(),
    saves_predict_fn: SavesPredictFn | None = None,
) -> list[PointsPMF]:
    """The composition point (module docstring). One call covers a WHOLE
    fixture -- `players` should carry every player this caller wants a
    `PointsPMF` for, PLUS ideally the rest of both squads for the most
    accurate bonus ranking (a caller who omits fringe squad members
    understates the true competitive field bonus ranks against by a
    small, undramatic amount -- most such players carry minute_exposure
    weighted almost entirely at the "0" band, contributing negligible
    BPS mass either way). Needs at least 2 players (`fplai.models.bonus.
    predict_bonus_pmfs_for_fixture`'s own floor -- bonus cannot be ranked
    against a field of one).

    Raises `PointsError` if any player's declared `team` is neither
    `scoreline.home_team` nor `scoreline.away_team`, or is on the wrong
    side of `is_home` -- a caller bug this module refuses to guess past,
    not a data problem it can recover from.
    """
    if len(players) < 2:
        raise PointsError(f"simulate_fixture_points_pmfs needs at least 2 players, got {len(players)}")

    # Deterministic input order (module docstring, "Determinism") --
    # never trust the caller's own list order, the same lesson Phase 1's
    # polars .unique(maintain_order=False) bug already cost this project.
    ordered_players = sorted(players, key=lambda p: p.element)
    seen_elements = set()
    for p in ordered_players:
        if p.element in seen_elements:
            raise PointsError(f"duplicate element={p.element} in players")
        seen_elements.add(p.element)
        expected_team = scoreline.home_team if p.is_home else scoreline.away_team
        if p.team != expected_team:
            raise PointsError(
                f"element={p.element} declares team={p.team!r}, is_home={p.is_home}, but "
                f"scoreline is {scoreline.home_team!r} (home) v {scoreline.away_team!r} (away) "
                "-- team/is_home mismatch."
            )

    rng = np.random.default_rng(config.seed)
    n_sims = config.n_simulations

    # --- 1. One shared scoreline draw for the whole fixture. -------------
    grid = np.array(scoreline.grid, dtype=np.float64)  # (max_goals+1, max_goals+1), row-major (h, a)
    mg = scoreline.max_goals
    flat = _normalize(grid.reshape(-1))
    flat_idx = rng.choice(flat.shape[0], size=n_sims, p=flat)
    h_arr = flat_idx // (mg + 1)
    a_arr = flat_idx % (mg + 1)

    # --- 2. Per-player minute-band marginals (needed for bonus's real
    # minute_exposure below, and for each player's own band draw). ------
    band_marginals: dict[int, np.ndarray] = {}
    for p in ordered_players:
        pmf = predict_minutes_pmf(minutes_params, p.minutes_feature_row, element=p.element, fixture=fixture)
        band_marginals[p.element] = _band_marginal(pmf)

    # --- 3. Bonus: ONE fixture-joint call, real (non-point-mass) minute
    # exposure per player (module docstring, "Bonus: fixture-joint
    # marginal, drawn i.i.d."). --------------------------------------
    bonus_inputs = [
        BonusPlayerInput(
            element=p.element,
            position=p.position,
            feature_row=p.bonus_feature_row,
            minute_exposure=[(m, float(w)) for m, w in zip(_BAND_MIDPOINTS, band_marginals[p.element])],
        )
        for p in ordered_players
    ]
    bonus_n_sims = config.bonus_n_simulations if config.bonus_n_simulations is not None else bonus_params.config.n_simulations
    bonus_pmfs = predict_bonus_pmfs_for_fixture(
        bonus_params, bonus_inputs, fixture=fixture, n_simulations=bonus_n_sims, seed=config.seed
    )
    bonus_pmf_by_element = {pmf.element: pmf for pmf in bonus_pmfs}

    results: list[PointsPMF] = []

    for p in ordered_players:
        band_probs = band_marginals[p.element]
        band_idx = rng.choice(len(MINUTE_BANDS), size=n_sims, p=band_probs)
        minutes_arr = np.array([_BAND_MIDPOINTS[b] for b in band_idx], dtype=np.int64)

        team_goals_arr = h_arr if p.is_home else a_arr
        opp_goals_arr = a_arr if p.is_home else h_arr

        # --- goals / assists: p_effective(band) x Binomial(team_goals) --
        p_goals_by_band = _conditional_attacking_p_by_band(
            attacking_params, p.attacking_feature_row, stat="goals", element=p.element, fixture=fixture
        )
        p_assists_by_band = _conditional_attacking_p_by_band(
            attacking_params, p.attacking_feature_row, stat="assists", element=p.element, fixture=fixture
        )
        p_goals_sim = p_goals_by_band[band_idx]
        p_assists_sim = p_assists_by_band[band_idx]
        goals_arr = rng.binomial(team_goals_arr, p_goals_sim)
        assists_arr = rng.binomial(team_goals_arr, p_assists_sim)

        # --- defensive contribution ---------------------------------
        dc_pmfs = _dc_pmfs_by_band(dc_params, p.dc_feature_row, position=p.position, element=p.element, fixture=fixture)
        dc_count_arr = _draw_by_band_groups(rng, band_idx, dc_pmfs)
        dc_threshold = dc_pmfs[-1].count_threshold  # position-driven, identical across every band
        if dc_threshold is not None:
            dc_met_arr = dc_count_arr >= dc_threshold
        else:
            dc_met_arr = np.zeros(n_sims, dtype=bool)

        # --- cards -----------------------------------------------------
        cards_pmfs = _cards_pmfs_by_band(cards_params, p.cards_feature_row, element=p.element, fixture=fixture)
        card_outcome_arr = _draw_by_band_groups(rng, band_idx, cards_pmfs)
        yellow_arr = (card_outcome_arr == 1).astype(np.int64)
        red_arr = (card_outcome_arr == 2).astype(np.int64)

        # --- saves (module docstring, "The saves seam") ----------------
        caveats = [_OWN_GOAL_PENALTY_CAVEAT, _CS_GC_CAVEAT, _BONUS_IID_CAVEAT]
        if p.position != "GK":
            saves_status = SAVES_STATUS_NOT_APPLICABLE
            saves_arr = np.zeros(n_sims, dtype=np.int64)
        elif saves_predict_fn is not None and p.saves_feature_row is not None:
            saves_arr = _draw_saves_by_band_and_goals(
                rng, saves_predict_fn, p.saves_feature_row, element=p.element, fixture=fixture,
                band_idx=band_idx, opp_goals_arr=opp_goals_arr,
            )
            saves_status = SAVES_STATUS_MODELLED
        else:
            saves_status = SAVES_STATUS_NOT_YET_MODELLED
            saves_arr = np.zeros(n_sims, dtype=np.int64)
            caveats.append(
                "GK saves NOT YET MODELLED (fplai.models.saves does not exist / no "
                "saves_predict_fn supplied) -- saves points held at a declared zero, NOT "
                "predicted. See fplai.points module docstring, 'The saves seam'."
            )

        # --- bonus: i.i.d. draw from the fixture-joint marginal --------
        bonus_pmf = bonus_pmf_by_element[p.element]
        bonus_arr = rng.choice(np.asarray(bonus_pmf.counts, dtype=np.int64), size=n_sims, p=_normalize(np.asarray(bonus_pmf.probabilities, dtype=np.float64)))

        # --- clean sheet / goals conceded (module docstring) -----------
        appeared = minutes_arr > 0
        cleared_cliff = minutes_arr >= MINUTE_CLIFF
        clean_sheet_arr = cleared_cliff & (opp_goals_arr == 0)
        goals_conceded_arr = np.where(appeared, opp_goals_arr, 0)

        points_draws = np.empty(n_sims, dtype=np.int64)
        for i in range(n_sims):
            outcome = RealisedOutcome(
                minutes=int(minutes_arr[i]),
                goals_scored=int(goals_arr[i]),
                assists=int(assists_arr[i]),
                clean_sheet=bool(clean_sheet_arr[i]),
                goals_conceded=int(goals_conceded_arr[i]),
                own_goals=0,
                penalties_saved=0,
                penalties_missed=0,
                yellow_cards=int(yellow_arr[i]),
                red_cards=int(red_arr[i]),
                saves=int(saves_arr[i]),
                bonus=int(bonus_arr[i]),
                defensive_contribution_met=bool(dc_met_arr[i]),
            )
            points_draws[i] = score_outcome(outcome, p.position, scoring_config)

        support, probs = _histogram_to_pmf(points_draws)
        results.append(
            PointsPMF(
                element=p.element,
                fixture=fixture,
                position=p.position,
                points=support,
                probabilities=probs,
                n_simulations=n_sims,
                seed=config.seed,
                saves_status=saves_status,
                caveats=tuple(caveats),
            )
        )

    return results
