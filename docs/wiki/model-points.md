# Points-assembly layer — Phase 3 prerequisite 2 of 2 (XL-Coder, session `s005`)

> `src/fplai/points.py` (new), `tests/test_points.py` (new, 19 tests).
> Implements blueprint §4.3's "compose the six outcome models into a
> player-gameweek points PMF" and CLAUDE.md rule 5 (a scalar `xPts` at a
> module boundary is a design error). Computed on demand; no schema or
> store changes, nothing persisted.

## 1. What this is

The one module in this codebase that imports all six outcome models
(`minutes`, `team_strength`, `attacking`, `defensive_contribution`,
`bonus`, `cards`) together with `fplai.scoring`, and turns their PMFs into
a single player-gameweek **points PMF**:

```python
from fplai.points import PlayerFixtureFeatures, PointsSimulationConfig, simulate_fixture_points_pmfs

results = simulate_fixture_points_pmfs(
    fixture=12345,
    scoreline=scoreline_pmf,          # fplai.models.team_strength.ScorelinePMF
    minutes_params=minutes_params,
    attacking_params=attacking_params,
    dc_params=dc_params,
    cards_params=cards_params,
    bonus_params=bonus_params,
    scoring_config=scoring_config,     # fplai.scoring.ScoringConfig
    players=[...],                     # Sequence[PlayerFixtureFeatures], whole fixture
    config=PointsSimulationConfig(seed=0, n_simulations=2000),
)
# -> list[PointsPMF], one per player, each a full PMF over integer points
```

Every other model module in `src/fplai/models/` deliberately does **not**
import its siblings ("composition, not import" — every one of their own
module docstrings says this explicitly). This module is the documented
exception: assembly is its entire job.

**Computed on demand, never persisted.** No `store.write()`, no
`write_derived`, no `fplai.schemas` import, no store reads at all — every
input is caller-supplied, exactly like every `predict_*_pmf` function in
`fplai.models.*` already works. A live feature-row pipeline (read the
store as of "now", build today's trailing features for an unplayed
gameweek) is a separate, not-yet-built prerequisite — `docs/HANDOFF.md`
names it as a later Phase 3 deliverable, not this one.

## 2. Why a fixture-joint Monte Carlo, not six independent PMFs multiplied together

Bonus is a rank-within-fixture phenomenon: `fplai.models.bonus`'s own
module docstring is explicit that `predict_bonus_pmfs_for_fixture` takes a
whole fixture, not one player, because a player's bonus chance depends on
every other player in that match's own BPS draw. That single fact rules
out the cheap alternative (predict each of the six outcomes independently
per player, multiply/convolve them together): bonus cannot be produced
that way at all, and doing so for the other five while bolting bonus on
separately would silently assume independence the models themselves do
not have (team-mates' attacking returns share the same match result;
clean sheets are shared exactly across a team's own players).

The composition this module builds instead is a **seeded Monte Carlo over
the whole fixture**, `n_simulations` draws deep:

1. **One shared scoreline draw** `(home_goals, away_goals)` from the
   fixture's `ScorelinePMF`, reused by every player in that simulation —
   the source of every cross-player, same-team correlation this module
   captures.
2. **One minute-band draw per player**, from their own `MinutesPMF`
   (collapsed to a 6-entry band marginal — see §4), reused everywhere
   minutes matter for that player in that draw: goals/assists thinning,
   DC, cards, saves, appearance points, and the clean-sheet cliff.
3. **Goals/assists** thinned from that draw's own team-goals via
   `attacking`'s fitted Binomial share, evaluated at that draw's minute
   band (§3, "the n=1 trick").
4. **DC and cards** drawn from `defensive_contribution`/`cards`'s own
   per-band discrete PMFs.
5. **Saves**, if a predictor was supplied (§5) — otherwise the hole is
   carried forward on the output PMF, visibly, never a silent zero.
6. **Clean sheet / goals conceded** derived from step 1's shared scoreline
   draw (§6).
7. **Bonus** drawn i.i.d. per player from ONE fixture-joint marginal
   `BonusPMF` (§7 — the one deliberate fidelity trade-off in this design).
8. The resulting `RealisedOutcome` is run through `fplai.scoring.
   score_outcome` (the live, forward-only, rule-set-correct scorer) to get
   one integer points draw. Repeated `n_simulations` times; the empirical
   histogram **is** the player's `PointsPMF` — dense integer support,
   validated to sum to 1.0, same discipline every sibling `*PMF` dataclass
   in this codebase already carries.

## 3. The n=1 trick — reusing each model's own math, never re-deriving it

`fplai.models.attacking.predict_attacking_pmf`, called with a POINT MASS
`team_goals_marginal=[(1, 1.0)]`, returns a `Binomial(1, p)` PMF —
`.probabilities[1]` **is** `p` exactly, no approximation. This module
calls it once per player per stat per minute band (12 calls per player:
2 stats x 6 bands) to get `p_effective_by_band`, then draws
`Binomial(team_goals_draw, p_effective)` directly via
`numpy.random.Generator.binomial`, fully vectorised across every
simulation at once. DC and cards are drawn the same spirit of way — from
the model's own per-band discrete PMFs (`predict_dc_pmf`/
`predict_cards_pmf`, `minute_exposure` a single-band point mass), grouped
by which simulations landed in which band and sampled via a weighted
`rng.choice` (`_draw_by_band_groups`). Every one of these calls runs the
model's own production `predict_*_pmf` code path — this module never
touches a private design-matrix helper (`_feature_row_to_vector`,
`_design_matrix`, etc.) in any sibling module.

## 4. Minutes: band, not state

Every downstream model already consumes minutes as a
`[(minute_value, weight), ...]` mixture over BAND MIDPOINTS, never over
`fplai.models.minutes.STATES` (START/SUB/UNUSED) — none of
`defensive_contribution`/`attacking`/`cards`/`bonus` takes a state
argument at all. `MinutesPMF.joint()`'s (state, band) probabilities are
therefore collapsed to a 6-entry band marginal (`_band_marginal`) before
any draw happens. Band midpoints (`0.0, 15.0, 45.0, 67.0, 82.0, 90.0`) are
declared **by value** in `points.py`, not imported from `minutes.py`'s
private `_BAND_MIDPOINT` — the same "documented by value, not imported"
posture `fplai.models.bonus`'s own module docstring already takes for the
identical constant.

## 5. The saves seam — a marked, tested hole, never a silent zero

`src/fplai/models/saves.py` did not exist at any point during this
session (checked before writing a line of this module, and again before
punch-out) — it is being built in parallel by another agent this session
and this module is forbidden from creating or touching it. The seam is
**dependency injection**, not an import: `simulate_fixture_points_pmfs`
takes an optional `saves_predict_fn` matching `predict_cards_pmf`'s own
call shape exactly (`feature_row` positional, `element`/`fixture`/
`minute_exposure` keyword-only, returning anything with
`.counts`/`.probabilities` — `SavesPMFLike`, a `typing.Protocol`). Wiring
in the real `fplai.models.saves.predict_saves_pmf` once it lands should be
a one-line change at the call site.

If `saves_predict_fn` is `None`, or a GK's own `PlayerFixtureFeatures.
saves_feature_row` is `None` (both are required together), that GK's
`PointsPMF.saves_status` is set to `"NOT_YET_MODELLED"` — a **structural**
field on every output, present whether or not the caller reads the
caveats — and a caveat naming the omission explicitly is appended. Every
non-GK player gets `"NOT_APPLICABLE"` (saves genuinely does not apply to
outfield positions in FPL's own rule set). Proven live in
`tests/test_points.py::test_gk_with_saves_predict_fn_is_wired_in` — a stub
predictor exercises the `"MODELLED"` path end to end, without importing
`fplai.models.saves`, proving the seam actually works and is not merely a
documented intention.

**Why this matters more than the other declared-zero outcomes (§6):**
`docs/HANDOFF.md`'s own measurement (session s004/s005) found saves cover
**18.9% of GK points** (0.65 pts/appearance) with a real, **differential**
bias — keepers on bad defences lose ~0.21 pts/app more than keepers on
good defences from omitting saves, because `corr(saves/app, CS/app) =
-0.527`. A biased hole, not a noisy one. This is exactly why the omission
gets its own dedicated `saves_status` field rather than being folded into
the generic `caveats` tuple every other declared-zero outcome shares.

## 6. Clean sheets and goals conceded — approximated, direction unmeasured

None of the six outcome models produce a goal-TIMING distribution, only a
fixture-level final scoreline. This module's approximation: a player who
cleared the minutes cliff (`>= fplai.scoring.MINUTE_CLIFF`) in a draw gets
`clean_sheet=True` iff that draw's own opponent-goals count is exactly 0;
`goals_conceded` credits any appearing player (minutes band != `"0"`) the
FULL match opponent-goals count. This overstates exposure for a player
subbed off before a late opponent goal and understates it for a late sub
who missed an earlier one. **The net direction is genuinely unmeasured**
— stated as a named limitation for whichever future session tackles it,
not silently assumed harmless. Because `score_outcome` itself raises
`ScoringError` if `clean_sheet=True` with `minutes < MINUTE_CLIFF`, this
module's own gating is required for correctness (a crash, not a silent
mis-score, if it were ever wrong).

## 7. Bonus: fixture-joint marginal, drawn i.i.d. — the one deliberate trade-off

A fully joint composition would re-run bonus's own internal Monte Carlo
(`predict_bonus_pmfs_for_fixture`'s bootstrapped BPS draws) once **per
outer simulation**, conditioned on that draw's own per-player minutes —
computationally prohibitive at realistic scale (bonus's own pairwise rank
computation is `O(n_players^2)` per inner draw; nesting an outer
`n_simulations`-deep loop around it multiplies the two draw counts
together).

**What this module does instead**: call `predict_bonus_pmfs_for_fixture`
**once per fixture**, with every player's real (non-point-mass) minute
exposure — bonus's own sanctioned interface — giving each player a
marginal `BonusPMF` that already reflects the full cross-player rank
competition (the structurally important correlation). Each outer
simulation then draws a bonus value **independently** from that one fixed
marginal, via the module's shared outer rng stream. This preserves
bonus's cross-player rank correlation in full; it loses the
WITHIN-player correlation between "this draw's own minutes/goals
realisation" and "this draw's own bonus points" for the same player. This
is a stated, argued, cheaper composition — not a silent assumption of
independence — and a caveat naming it is attached to every `PointsPMF`.

A future Phase 5 revision wanting the fuller joint would need bonus's
internal per-draw BPS array exposed as a public seam (today it lives
inside a private `_assign_bonus_points_batch` call) — not attempted here,
named as a finding for the Architect instead.

## 8. Own goals, penalties saved, penalties missed — declared zero

No model in this codebase predicts any of the three. `points.py` holds
all three at a **declared** zero in every simulated draw, never a silent
one, and sizes the omission against the real store rather than merely
asserting it is rare: own goals are nonzero in **264 of 179,950** real
player-fixture rows (0.15%, verified live this session against
`vaastav_player_gameweek_stats`); penalties missed/saved are nonzero in
64/49 rows over the 2022-23+ window (`fplai.models.attacking`'s own
citation, reused rather than re-measured). Every `PointsPMF.caveats`
tuple names this explicitly.

## 9. Correlation structure — what Phase 3 captures, what Phase 5 inherits

**Captured, within one fixture, one simulation draw:** team-mates'
attacking returns sharing a team-goals draw; a team's clean-sheet outcome
shared exactly across its own on-pitch players; one player's own minute
band shared across every outcome that depends on it; bonus's cross-player
rank structure (via its own fixture-joint marginal, §7).

**Not captured, by design, explicitly scoped to Phase 5 (blueprint §7,
E8):** cross-FIXTURE correlation across a gameweek's ~10 simultaneous
matches (each fixture is simulated independently of every other); the
bonus/minutes-and-goals within-player correlation traded away in §7.
Full correlated Monte Carlo across a gameweek, and the rank-aware
objective/EO that would consume it, are explicitly out of this task's
scope per its own brief.

## 10. Determinism (CLAUDE.md rule 7)

Exactly one `numpy.random.default_rng(config.seed)` instance drives every
draw except bonus's own internal Monte Carlo, which is independently
seeded via `predict_bonus_pmfs_for_fixture`'s own `seed` kwarg (reusing
`config.seed`'s integer value — a genuinely separate generator object, so
it cannot perturb or be perturbed by this module's own stream). Every
draw happens in a fixed order: the fixture's shared scoreline draw first,
then players in ascending `element` order (the input list is sorted at
the top of `simulate_fixture_points_pmfs`, never trusted to already be
stable — Phase 1's `greedy_form` reproducibility bug is the standing
lesson for exactly this class of mistake), and within each player: band,
goals, assists, DC, cards, saves, bonus, always in that order.

Proven directly in `tests/test_points.py`:
`test_simulate_fixture_points_pmfs_is_bit_identical_across_repeated_calls_with_the_same_seed`
(two full runs, identical seed and inputs, exact tuple equality — not
`np.allclose`) and `test_a_different_seed_changes_the_result` (the other
half of the proof blueprint's lesson 5 requires — a test that only checks
"same seed -> same output" cannot distinguish "correctly seeded" from
"ignores its seed entirely").

## 11. Verification

**Unit/composition tests (19, all green, no network):** a small
hand-built multi-model synthetic store (`tests/test_points.py`, six
rounds, two teams, eight players spanning all four positions and all
three minutes states) fitted via the REAL public `fit_*_model`/
`fit_team_strength` entry points every sibling module's own tests already
use — never a private helper, never a hand-fabricated `*ModelParams`.
Covers: every PMF sums to 1 and has dense support; the saves seam's three
states (`NOT_APPLICABLE`/`NOT_YET_MODELLED`/`MODELLED` via a stub
predictor); the own-goal/penalty caveat is always present; fixture-input
validation (team/`is_home` mismatch, duplicate element, fewer than 2
players); both halves of the determinism proof.

**Sanity check against real settled 2026/27 results — honest limitation,
not attempted the way the brief first framed it.** `vaastav_
player_gameweek_stats` (every model's own training dataset) holds
**2019-20 through 2025-26 only** — verified live this session
(`sorted(store.latest("vaastav_player_gameweek_stats")["season"].unique())`)
— the 2026/27 season the brief names is **not** in this store at all.
Composing a points PMF for a real 2026/27 fixture needs feature ROWS
(trailing stats) this module deliberately does not build (§1, "What this
module does NOT do" in the source docstring) — that pipeline is a
separate, not-yet-built prerequisite. A live sanity check was therefore
**not completed this session** — recorded as a finding for the Architect
rather than silently skipped or faked with a same-season proxy that would
have compared apples to oranges (a historical season's `total_points` was
scored under THAT season's own rules, which `load_scoring_config` can
only read for the CURRENT season by design — blueprint §7.2, lesson 7).
What a same-session follow-up COULD do cheaply: GW1 of any season needs
no trailing history at all (every model's own rollups reset per season,
cold-start for every player) — a genuine GW1 2026/27 sanity check needs
only real fixtures/positions (fetchable live via `fplai.client.FPLClient`,
the same client `fplai.scoring`'s own verification already used) and
would not need `vaastav_player_gameweek_stats` to carry 2026/27 at all.
GW2+ would additionally need a live trailing-feature builder from GW1's
own live payload. Neither was built this session — named here so the next
session does not have to rediscover the gap.

## 12. What this session deliberately did not do

- No live feature-row pipeline (§11).
- No `fplai.models.saves` — forbidden this session, seam left marked
  (§5).
- No cross-fixture/gameweek-wide correlation, no rank-aware objective, no
  EO — Phase 5 (§9).
- No persistence — Phase 4 caching concern per this task's own brief.
- No fuller bonus/minutes joint (§7) — named as a finding, not attempted.
