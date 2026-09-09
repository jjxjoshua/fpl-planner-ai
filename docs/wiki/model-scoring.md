# Scoring function — Phase 3 prerequisite 1 of 2 (XL-Coder, session `s005`)

> `src/fplai/scoring.py` (new), `tests/test_scoring.py` (new, 61 tests).
> Implements blueprint §7.1/§7.2's Phase 3 prerequisite ("nothing in the
> codebase converts outcomes into points"), CLAUDE.md rule 4 (nothing
> hardcoded) and rule 5 (a scalar at a *model* boundary is a design error —
> this is not a model). No schema or store changes; this module writes
> nothing and needs no new dataset.

## 1. What this is

A pure, deterministic function that turns one player-gameweek's *realised*
outcome vector into an integer points total, under FPL's own scoring rules
read live from `game_config`:

```python
from fplai.scoring import load_scoring_config, score_outcome, RealisedOutcome
from fplai.store import BitemporalStore
from datetime import datetime, timezone

store = BitemporalStore()
config = load_scoring_config(store, datetime(2026, 9, 5, tzinfo=timezone.utc))  # an explicit, reproducible as_of

outcome = RealisedOutcome(
    minutes=90, goals_scored=1, assists=0, clean_sheet=True,
    goals_conceded=0, saves=0, bonus=2, defensive_contribution_met=False,
)
points = score_outcome(outcome, "MID", config)  # -> int
```

It is **not** a model. It never touches a probability, an expectation, or a
PMF. The next Phase 3 story (prerequisite 2 of 2 — the player-GW points
PMF) composes this function with the six outcome models' PMFs (minutes x
attacking share x DC x bonus x cards x CS/GC) to produce a full points
distribution per blueprint §4.3; that composition is only clean if this
layer stays scalar-in/scalar-out. Do not extend `score_outcome` to accept
probabilities.

## 2. The two dataclasses

`RealisedOutcome` — every field is an FPL scoring identifier, at the grain
`score_outcome` needs. Two fields are **pre-resolved facts the caller
supplies**, not raw counts this module derives eligibility from:

- `clean_sheet: bool` — did the team not concede while this player was on
  the pitch, for a player who cleared the minutes cliff. FPL's own live
  `stats.clean_sheets` is already 0/1 (verified); a team-strength/defensive
  model composing with this function must resolve it the same way.
- `defensive_contribution_met: bool` — did the player's CBIT/CBIRT count
  cross the group's threshold. **Deliberately out of scope here** — the
  threshold logic already lives in
  `fplai.models.defensive_contribution.build_dc_threshold_set`
  (DEF >= 10, MID/FWD >= 12, pinned by observation, `verified=True`).
  Treating FPL's raw `stats.defensive_contribution` COUNT as truthy instead
  of resolving it against the threshold mis-scored **45 of 610** real GW1
  rows in this session's own verification — not a hypothetical risk.

`goals_conceded: int` is likewise a pre-resolved count **while this player
was on the pitch**, the same "on the pitch" scoping `clean_sheet` carries.

`ScoringConfig` — the live scoring rule set, already parsed and
position-remapped (`"GKP"` -> `"GK"`, matching every other model module's
vocabulary) from `game_config`. Built by `load_scoring_config(store,
as_of)`, never by hand in production code.

## 3. Three point values genuinely absent from `game_config`

`game_config`'s `scoring` block publishes the *point value* per unit of an
identifier, never the *unit size* where the unit is not "one occurrence".
Checked directly against the real payload (`"threshold" not in
payload_raw.lower()`, same check `fplai.models.defensive_contribution`'s
own docstring ran):

| Constant | Value | What it gates | Source |
|---|---|---|---|
| `MINUTE_CLIFF` | 60 | `long_play` (2pt, >=60') vs `short_play` (1pt, 1-59') | Same, independently-declared constant as `fplai.models.minutes.APPEARANCE_POINTS_MINUTE_CLIFF` (verified live 2026-08-22; re-confirmed this session against 610/610 real GW1 rows) |
| `SAVES_POINTS_DIVISOR` | 3 | `saves` (1pt) pays per 3 saves, floor-divided | Named in this task's brief; verified against real GK rows (saves 1/3/4, GW1) |
| `GOALS_CONCEDED_POINTS_DIVISOR` | 2 | `goals_conceded` (-1pt GK/DEF) pays per 2 conceded, floor-divided | **Not named in this task's brief — found this session.** A naive "-1 per goal" reading fails immediately against real data (GK id=29, GW1: 90', `goals_conceded=4`, `saves=3`, real `total_points=1`; only `floor(4/2)*-1 = -2` reconciles, `-4` does not) |

All three are the same class of gap `fplai.models.defensive_contribution`
already names for its own count thresholds and `fplai.models.minutes`
already names for the same minute cliff — the live API simply does not
publish every constant its own scoring rule depends on. Declared as
module-level constants in `scoring.py`, each with citation and
verification note in the module docstring, mirroring
`fplai.backtest.rules.SEASON_RULES`'s single-point-of-correction
convention. **Duplication note:** `MINUTE_CLIFF` is declared independently
in both `fplai.scoring` and `fplai.models.minutes` rather than imported
from one to the other — deliberate (this module stays a minimal,
dependency-light pure-arithmetic layer; `minutes.py` is a full statistical
model with heavy fitting machinery) but a real cost: if the constant is
ever wrong, both must be corrected, and nothing today enforces they agree
beyond a cross-referencing comment in each. A shared constants module is
the natural fix, out of scope for this task.

## 4. FORWARD-ONLY — enforced structurally, not just documented

Lesson 7 (blueprint §7.2): score history from stored `total_points`, never
recompute — scoring rules drift every season and `game_config` only ever
holds the CURRENT season's rules. This module exists to score **predicted
future outcomes**, never to re-derive historical points.

The guard is a consequence of `store.as_of`'s own documented semantics, not
a bolt-on check: `game_config`'s first-ever observation in this store is
this project's own ingest start (2026-08-19, i.e. inside the 2026/27
season). `load_scoring_config(store, as_of)` calls `store.as_of("game_config",
as_of)`; for any `as_of` before that first observation, `store.as_of`
returns empty by its own contract, and `load_scoring_config` turns that
into a `ScoringError` naming the forward-only rule and pointing the caller
at stored `total_points` instead. Every season Phase 1's baselines cover
(2019-20 through 2025-26, all strictly before `game_config` existed in this
store) is therefore **structurally unreachable** through this loader — not
merely discouraged by a docstring. Verified directly: `load_scoring_config`
called with an `as_of` before the real store's earliest `game_config`
observation raises; called with any `as_of` at or after it succeeds.

What the guard does **not** catch: a caller "recomputing" a *settled
current-season* gameweek instead of reading its stored `total_points`. No
timestamp can distinguish "predicting GW9" from "re-deriving GW3 after the
fact" when both fall legally inside the same season's config-validity
window. This residual is named explicitly in the module docstring — the
type signature (`RealisedOutcome`, not a probability) is the remaining
guard against that specific misuse, and it is a convention, not a
structural one.

## 5. Verification — real settled GW1, not fixtures

`score_outcome` composed with `load_scoring_config` (real store) and
`build_dc_threshold_set` (for the DC-met boolean) was checked against every
one of the **610 real elements** in `event/1/live/` (GW1 2026/27,
`finished=True, data_checked=True` in the store's `events` snapshot at
verification time), fetched live via `fplai.client.FPLClient` — same,
unmodified client every other live-API script in this project already
uses (`scripts/pin_dc_thresholds.py`, `scripts/sample_picks.py`).

**Result: 610/610 exact match against FPL's own `stats.total_points`.**
Zero mismatches, zero raised `ScoringError`s. Every scoring identifier
`game_config` exposes was exercised by at least one real row except
`penalties_saved` (zero occurrences in GW1 2026/27) — covered instead by a
synthetic, clearly-labelled unit test, since that coefficient is a direct
`count * config value` term with no hidden divisor and therefore carries
materially less risk than `saves`/`goals_conceded` did before this
session's check.

19 of the 610 rows, chosen for identifier coverage (both minute bands,
clean sheet true/false across GK/DEF/MID, goals conceded 0/2/4, saves
1/3/4, own goal, red card, penalties missed, bonus 0/1/2/3, DC met
true/false across both groups, an unused 0-minute sub), are pinned as
literal regression fixtures in `tests/test_scoring.py` so this proof does
not require a live network call to re-run. The full 610-row reconciliation
script is recorded in `.punchcard/s005.jsonl`, not committed to the repo —
a one-off verification, not a reusable tool.

## 6. What this session deliberately did NOT do

- No expected-points / probability-weighted path. `score_outcome` stays
  scalar-in/scalar-out; the PMF composition is the next story.
- No wiring into `fplai.models.defensive_contribution` or any other model
  — this module has zero dependents yet. The next story is the first
  consumer.
- No shared constants module for `MINUTE_CLIFF` even though the value is
  now declared twice (here and in `fplai.models.minutes`) — flagged above,
  not fixed, since fixing it means touching `minutes.py`, which was
  read-only for this task.
- `highspy` is still not a `pyproject.toml` dependency — out of scope for
  a scoring-only task, but still true, per the Architect's own earlier
  finding this session.
