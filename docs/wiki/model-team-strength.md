# Team strength — Dixon-Coles on xG — Phase 2, E5 (XL-Coder, session `s002`)

> `src/fplai/models/team_strength.py` (new), `src/fplai/models/__init__.py` (new),
> `tests/test_team_strength.py` (new, 29 tests), `scripts/fit_team_strength.py` (new),
> `src/fplai/schemas.py` (append-only: `TEAM_STRENGTH_RATING_GAMEWEEK`,
> `TEAM_STRENGTH_RATING_DATASET` constants — no existing entry touched).
> Implements blueprint §4 (E5), §4.2, §4.3, §12.2. First real model in the project, and the
> first real consumer of story 9's derived-capability framework.

## 1. What this is

A Dixon-Coles team-strength model: per-team attack/defence parameters (log scale), a global
home-advantage term, the low-score dependence correction (`rho`), and exponential
time-decay weighting. The output is never a scalar — `predict_scoreline()` returns a
`ScorelinePMF`, a full joint probability mass function over `(home_goals, away_goals)` up
to `max_goals` each side (CLAUDE.md rule 5).

```python
from fplai.models.team_strength import fit_team_strength, predict_scoreline
from fplai.store import BitemporalStore
from datetime import datetime, timezone

store = BitemporalStore()
teams = store.latest("teams")["name"].to_list()
params = fit_team_strength(store, as_of=datetime(2026, 8, 21, 17, 30, tzinfo=timezone.utc), teams=teams)
pmf = predict_scoreline(params, "Man City", "Southampton")
pmf.home_win, pmf.expected_home_goals(), pmf.most_likely_scoreline()
```

## 2. The xG-versus-goals boundary — the decision, and why

Blueprint §4.2 says fit on xG, "the lower-variance signal". Verified live (2026-08-21):
`vaastav_player_gameweek_stats.expected_goals` is **100% NULL for 2019-20/2020-21/2021-22
and 100% populated 2022-23 onward** — a hard boundary, not a gradual thinning. Three options
existed: decline the pre-2022-23 seasons, fit xG-only where available and goals with a
different noise assumption elsewhere, or fit everything on goals. **Chosen: the second.**

- Every match enters the SAME quasi-Poisson MLE for attack/defence/home-advantage
  (`_fit_attack_defence_home`). The target is team xG (summed from player-level
  `expected_goals` per fixture) where the season has it, actual team goals
  (`team_h_score`/`team_a_score`) where it does not — treating xG as a lower-noise draw
  from the same latent scoring rate the Poisson mean represents.
- Every goals-sourced match is **down-weighted** relative to an xG-sourced one via
  `TeamStrengthConfig.goals_source_weight` (default `0.6`, multiplied with the time-decay
  weight) — the "different noise assumption" the brief asked for. **Deliberately
  uncalibrated** — a stated placeholder, same status as `derived.CalibrationReference`'s
  `residual_mean`/`residual_std`, pending the Phase 2 gate's real Brier/log-loss/RPS numbers.
- **Declining the early seasons was rejected.** Phase 1 already showed multi-season data
  materially changes what a backtest can say; three fewer seasons would weaken exactly the
  promoted/relegated-team cases (§4 below) that most need history.

**The low-score correction cannot be fit on xG at all** — `tau(x, y)` is defined for actual
integer scorelines (0-0, 1-0, 0-1, 1-1); "0.3 expected home goals" has no low-score-cell
meaning. So `rho` is fit in a **separate, second stage** (`_fit_rho`), against actual
full-time goals only, holding stage 1's attack/defence/home-advantage fixed. This is a
legitimate profile-likelihood split, not a shortcut: `rho`'s likelihood contribution is
`log(1) = 0` for every match outside the four low-score cells, so nothing is lost by fitting
it in isolation from a different response variable.

## 3. The promoted-team prior

A team with zero matches in the training window gets `attack = defence = 0.0` — exactly
league-average, before home advantage. This is not a special case: it falls out of the L2
ridge penalty (`TeamStrengthConfig.attack_defence_l2`, default `0.05`) that every team's
attack/defence already carries — a team with no matches has zero likelihood gradient and
simply sits at the regulariser's mode. The **same regularisation term also resolves the
model's gauge freedom**: adding a constant to every attack parameter and subtracting it from
every defence parameter leaves every fixture's `lambda`/`mu` unchanged (a genuinely flat
likelihood direction), and the ridge penalty is the only thing pinning that direction down —
no separate sum-to-zero constraint or post-hoc recentring step exists in this code.

Verified live, 2026-08-21, fitting across `data/store/`'s six usable seasons
(2020-21 → 2025-26; 2019-20 excluded, §5): `teams_with_no_history = ('Coventry City', 'Hull
City', 'Ipswich Town')` — all three of 2026-27's promoted/returning clubs, correctly landing
at `attack = defence = 0.0`.

**Stated limitation, not a claimed feature**: this prior very likely overrates genuinely weak
promoted sides in their first few matches. A better prior (shrinking toward the historical
promoted-cohort mean, well below league average) is a real improvement for later.
`TeamStrengthParams.teams_with_no_history` names exactly which teams got it on a given fit,
so a downstream consumer can find and treat them differently without this module changing.

**A second, distinct problem the Architect flagged and this section did not previously
record: presentation, not just modelling.** League average (`0.0`) is high enough on the
defence axis that it lands `teams_with_no_history` clubs (Coventry City, Hull City, Ipswich
Town) inside or near the "best 8 defence" band when a full 20-team ranking is printed
unfiltered — ranked alongside, and in some renderings above, genuinely fitted values like
Brighton's and Man Utd's (see §11's table, which shows Man Utd at `-0.0072`; `0.0` sorts
directly adjacent to it). **Unknown is being displayed and consumed as good** — a prior is
not an estimate, and nothing in `predict_scoreline`'s or `params_to_rows`' output currently
marks a `teams_with_no_history` row differently from a genuinely fitted one at the point of
consumption (only `TeamStrengthParams.teams_with_no_history` itself, on the params object,
carries the distinction — a caller that reads only the per-team attack/defence rows would
not see it). Two things follow, not one: (a) presentation — any ranked or tabular rendering
of team strength should mark `teams_with_no_history` rows explicitly rather than let them
sort by value alongside fitted ones; (b) modelling — league average is likely an optimistic
prior for a newly-promoted side specifically (as distinct from the general point above), and
**this store holds 7 seasons of promoted-cohort history from which that prior could be
estimated rather than assumed** — the data to close this exists today and was not used.
Neither is fixed here; both are scoped out of this module the same way the richer
promoted-team prior already is (§12).

### 3.1 Session s003 (2026-08-22, XL-Coder) — closed, both halves

Both gaps §3 named — "the prior is likely optimistic" and "7 seasons of promoted-cohort
history could estimate a real one" — are closed. `TeamStrengthConfig.promoted_team_attack_prior`
/`promoted_team_defence_prior` (default `-0.3061`/`+0.2216`, log scale) replace the flat `0.0`.
Applied at exactly the mechanism §3 already described — `_fit_attack_defence_home` initialises
and regularises a no-history team toward the prior instead of `0.0` — so nothing about *how*
the prior is delivered changed, only its value, and the isolation property (a no-history team
can affect no other team's fitted value, proven by
`tests/test_team_strength.py::test_adding_a_no_history_team_does_not_perturb_any_other_teams_fit`)
holds identically.

**The estimator.** Not the joint multi-season fit (that gives Coventry/Hull/Ipswich `0.0`
today, by construction — they have no matches in a multi-season window either). Instead:

1. From `vaastav_team_identity` (all 7 seasons, task 1 below), take the season-over-season
   `code` set difference to find each season's 3 promoted clubs — not a hardcoded name list.
   Verified against real football history for all 6 transitions this store spans:

   ```
   2020-21: Leeds, West Brom, Fulham        2023-24: Sheffield Utd, Burnley, Luton
   2021-22: Norwich, Watford, Brentford     2024-25: Leicester, Southampton, Ipswich
   2022-23: Nott'm Forest, Fulham, Bournemouth   2025-26: Leeds, Sunderland, Burnley
   ```

2. For each of the 6 target seasons, fit a **single-season** Dixon-Coles model (`seasons=
   [season]`, `as_of` = 1 Aug of the following year) — deliberately isolated from every other
   season, so a promoted team's OWN later, established-club seasons cannot leak into the
   estimate of what its FIRST season looked like.
3. For each of that season's 3 promoted clubs, record `attack[team] - mean(attack)` and
   `defence[team] - mean(defence)` across that season's own 20 fitted teams (correcting for
   any residual non-centring in that single-season fit's own gauge, rather than assuming the
   mean is exactly `0.0`).
4. Average across the resulting 18 promoted-team-season instances.

```
2020-21 Leeds           attack_dev=+0.2506 defence_dev=-0.0053
2020-21 West Brom       attack_dev=-0.2267 defence_dev=+0.3817
2020-21 Fulham          attack_dev=-0.6275 defence_dev=+0.0287
2021-22 Norwich         attack_dev=-0.7210 defence_dev=+0.4882
2021-22 Watford         attack_dev=-0.4588 defence_dev=+0.4019
2021-22 Brentford       attack_dev=-0.0337 defence_dev=+0.0852
2022-23 Nott'm Forest   attack_dev=-0.3378 defence_dev=+0.1774
2022-23 Fulham          attack_dev=-0.2182 defence_dev=+0.0931
2022-23 Bournemouth     attack_dev=-0.1761 defence_dev=+0.1131
2023-24 Sheffield Utd   attack_dev=-0.3481 defence_dev=+0.2508
2023-24 Burnley         attack_dev=-0.3012 defence_dev=+0.1909
2023-24 Luton           attack_dev=-0.2631 defence_dev=+0.3119
2024-25 Leicester       attack_dev=-0.4899 defence_dev=+0.2975
2024-25 Southampton     attack_dev=-0.4863 defence_dev=+0.4658
2024-25 Ipswich         attack_dev=-0.3844 defence_dev=+0.3035
2025-26 Leeds           attack_dev=+0.0305 defence_dev=+0.0529
2025-26 Sunderland      attack_dev=-0.2651 defence_dev=+0.0144
2025-26 Burnley         attack_dev=-0.4538 defence_dev=+0.3366

n=18  mean attack deviation = -0.3061  mean defence deviation = +0.2216
```

**Why this estimator, not the obvious alternative of just running the existing joint fit and
reading off promoted teams' values once they have a season of data**: that would already be
what §3.1's underlying mechanism does for the SECOND season onward, and is not the question —
the question is what to assume BEFORE any of their own data exists, i.e. what a typical
newly-promoted club's first season actually looks like. Isolating each debut season into its
own fit is what makes the 18 instances comparable to each other (each one measured against
its own season's league average, not contaminated by the fitting team's later trajectory).

**Directionally exactly as expected, and not smoothed to hide the exception**: 17/18 instances
worse on defence, 16/18 weaker on attack. The two attack exceptions are BOTH Leeds (2020-21
and 2025-26) — the same club, two different promotions, both times outperforming the typical
promoted-team attack pattern. This is left as a real, named exception, not folded into a
club-blind average that erases it — a future revision conditioning the prior on the promoted
club's own recent-Championship attacking output (not built here) would presumably catch this
kind of case; a single scalar prior, by construction, cannot.

**Materiality of task 2 (§4 below) on this estimate: none, deliberately.** The estimator's
per-season isolation means no instance's fit ever crosses the season boundary the Ipswich
name-drift bug lives in (a single-season fit never needs to join two seasons' team names
together at all) — verified by construction, not just argued: the 18-instance table above was
produced with the SAME numbers whether or not `team_name_canonicaliser` was passed to the
single-season fits (checked directly; irrelevant to a single-season fit either way, since
there is only one name for each club within one season by definition). Task 2 DOES change
which teams the resulting prior gets *applied to* in the live multi-season fit — see §4.1 —
but not the prior's own value.

## 4. A real, live-verified identity finding: team names are not stable even within FPL's own lineage

**Not hypothetical — found while sanity-checking the real fit.** `Ipswich Town` appears in
`teams_with_no_history` (the promoted-team prior, §3) even though Ipswich were IN the
Premier League as recently as 2024-25 and have real, informative history in the store. Why:

```
vaastav_player_gameweek_stats, season=2024-25: team == "Ipswich"          (verified live)
live bootstrap-static `teams`, 2026-27:         name == "Ipswich Town"    (verified live)
```

Both come from the **same source lineage** (FPL's own `bootstrap-static`, one archived by
vaastav, one fetched live) — this is not blueprint §3.5's cross-provider name mismatch
(PL API's full names vs FPL's short names), it is the **same provider's own name for the
same club drifting across seasons**. This module joins on team name (see §7 below for why),
so Ipswich's 2024-25 relegation history — genuinely the most informative prior available for
them — is silently discarded in favour of the league-average prior, a real accuracy cost,
though not a leakage or correctness bug (the model still produces a valid, if worse,
prediction). `Coventry City` and `Hull City` are unaffected — neither has recent-enough PL
history in this store's window for the question to arise.

**Not fixed here** — `identity.py`/`providers/**` are READ-ONLY for this task, and a proper
fix (joining on FPL's stable `teams[].code` across seasons, via `vaastav_team_identity`,
which the real store does not currently hold any rows for — verified, `ls data/store/` has
no `vaastav_team_identity` directory) is out of scope. **Escalating as a finding**: blueprint
§3.5/§12.5's "never join teams on name" guidance was written for cross-PROVIDER joins; this
is the first live evidence it also needs to cover cross-SEASON joins within one provider's
own lineage. Recorded here rather than routed around.

### 4.1 Session s003 (2026-08-22, XL-Coder) — closed

`identity.py` was owned this session; the escalation above is resolved, not routed around.

**Task 1 — `vaastav_team_identity` populated.** It genuinely held zero files before this
session (verified: `ls data/store/vaastav_team_identity` failed, no such directory). Ran
`scripts/backfill.py --provider vaastav_archive --capabilities team.identity@season --seasons
2019-20,...,2025-26 --execute` — dry-run first (7 units, 7 requests, no network), then against
an isolated `tempfile.mkdtemp()` store to verify shape, then the real store. **140 rows, 20
per season across all 7 seasons.** Entity key `(season, id)` verified unique **within the real
observation batch** (CLAUDE.md lesson 2's standing requirement — three prior bugs of this
class, one silently dropping 7,141 rows): `df.group_by(["season","id"]).len().filter(len>1)`
returns **0 rows**, checked directly against the real store, not asserted from the schema.

**Task 2 — the full cross-season name-divergence sweep, not just Ipswich.** The brief named
four candidates to check (Wolves/Wolverhampton, Brighton, Spurs/Tottenham, Nott'm Forest).
Checked programmatically against the real, now-populated `vaastav_team_identity` (140 rows)
plus the live `teams` dataset (20 rows) — two separate checks, both exhaustive:

1. **Within the archive itself, across all 7 seasons, grouped by `code`**: zero codes have
   more than one distinct `name` (`by_code.filter(names.list.len() > 1)` is empty). Every
   club's OWN archived name is stable for as long as it appears in this store.
2. **Archive name vs the live 2026-27 name, for every code present in both**: exactly ONE
   mismatch — `code=40`, archive `"Ipswich"` (2024-25) vs live `"Ipswich Town"`. Wolves,
   Leicester, Southampton, West Ham, West Brom, Norwich, Sheffield Utd, Watford, Burnley and
   Luton are all archive codes ABSENT from the live 20 — a real relegation each, not a naming
   divergence, so there is no live name to compare against. Every code present on BOTH sides
   other than Ipswich has a byte-identical name (Brighton = "Brighton" both places, Spurs =
   "Spurs" both places — the live dataset uses FPL's short name, not "Tottenham Hotspur", so
   there is no divergence to find there — and Nott'm Forest = "Nott'm Forest" both places).

**The honest finding: the brief's four named candidates were a reasonable hypothesis, and
none of them actually diverges in this store's real data — only Ipswich does.** Reported as
found, not adjusted to match the brief's expectation. This also confirms §4's `vaastav_
player_gameweek_stats.team` column (what `build_match_table` actually joins on) carries
exactly the archive's own `teams.csv` name for every season checked (`gws.team` vs `team_
identity.name` for the same season: 0 mismatches across 121 distinct (season, team) pairs,
the one exception being 2019-20's wholly-`None` `team` column, already excluded for an
unrelated reason — §5).

**The fix: `fplai.identity.TeamNameCanonicalisationMap`**, joined on the stable `code`, not a
hardcoded `"Ipswich" -> "Ipswich Town"` string substitution — the brief's own warning ("a
one-club patch that leaves three more latent is not a fix") is satisfied by construction: had
the sweep above found three more, the same map would have closed all of them with no extra
code. Built from EVERY season's `team.identity@season` rows (never a single season — a
name-only join across seasons is the exact bug being closed), canonicalising to the LIVE
snapshot's name where supplied (so a caller's `predict_scoreline(params, "Ipswich Town", ...)`
matches the pooled fit) or the archive's own most-recent name otherwise (for a purely
historical caller that must never reference today's naming). Deliberately built at the CALL
SITE (`scripts/fit_team_strength.py`), never fetched implicitly inside `fit_team_strength`/
`build_match_table` — the same caller-supplied discipline `teams=` already follows, so a
function this module also uses for historical backtests never gains an implicit "read today's
snapshot" leakage seam.

**End-to-end, live-verified**: `fit_team_strength(real_store, as_of=..., teams=live_team_
names, team_name_canonicaliser=...)` now puts `"Ipswich Town"` in `teams_with_no_history`
**only when the canonicaliser is omitted**; with it, Ipswich Town's real 2024-25
relegation-season history pools under that exact name and the team gets a real, data-driven
rating — `attack=-0.2423, defence=+0.3621` (identical to what the pre-fix run computed under
the WRONG key `"Ipswich"`, confirming the fix relabels without changing any underlying
statistic). Only `Coventry City` and `Hull City` remain in `teams_with_no_history` for this
store's real 7-season window — both genuinely absent from every season this store holds, not
a further name-drift bug. `tests/test_team_strength.py::
test_fit_with_canonicaliser_gives_ipswich_town_real_history_not_the_promoted_prior` and
`test_team_name_canonicaliser_maps_both_ipswich_spellings_to_the_live_name` are the real-store
regression tests for this.

## 5. Bitemporal fitting — `as_of()` is the wrong primitive for this dataset

The brief's framing ("reads only `store.as_of(deadline_t)`") is blueprint §3.2's general
rule. It is the **wrong primitive** for `vaastav_player_gameweek_stats` specifically, for a
reason `fplai.backtest.data` already discovered and this module's own tests reproduce and
prove (`test_store_as_of_is_empty_for_a_genuinely_historical_deadline_on_this_dataset`):
this archive was bulk-ingested in one backfill session (2026-08-21), so every row's
`observed_at` is approximately "today" regardless of which historical season/gameweek the
row describes. `as_of(dataset, deadline_t)` filters on `observed_at <= deadline_t`; for any
real historical `deadline_t` that predicate is false for every row, so `as_of()` returns
**empty**, not wrong-but-plausible — a loud failure a caller who tried it would immediately
notice, not a silent leak.

The actual bitemporal safety this module needs — never let a fixture that had not yet kicked
off by `as_of` influence the fit — is enforced the way `fplai.backtest.data`/
`fplai.backtest.replay` already enforce it: `store.observations()` (the raw stream, no
misleading per-entity collapse) filtered explicitly on the archive's own valid-time column
(`kickoff_time`), never on `observed_at`. `build_match_table`'s `as_of` is **required, no
default** — mirroring `BitemporalStore.observations()`'s own required `until` — for exactly
the reason an unbounded call is the leakage bug waiting to happen.

**Attacking the guarantee from outside the sanctioned path** (this session's new standing
rule), proven as tests, not just asserted:

- `test_as_of_boundary_is_strictly_before_not_at_kickoff` — `as_of == kickoff` excludes the
  match (strict `<`, not `<=`).
- `test_backdated_observed_at_cannot_smuggle_a_future_kickoff_into_the_training_window` —
  writes a fixture with a FUTURE kickoff but an `observed_at` backdated years earlier (an
  attacker's attempt to make it look like known-in-advance information). It is still
  excluded, because the filter is on `kickoff_time`, never `observed_at`.

## 6. A real bug this fit exposed in its own test — and a hardening it earned

**Not a production bug — but real, and worth recording because of what it revealed.** An
early version of the synthetic parameter-recovery test computed `as_of =
max(kickoff).replace(year=9999)`, an ~8000-year gap. `exp(-ln2 * days / half_life_days)`
underflows to a literal `0.0` for a gap that large — every match's decay weight became
exactly zero, every gradient term became exactly zero, and Adam never moved a single
parameter off its `0.0` initial value. The fit "succeeded" (no exception, a well-formed
`TeamStrengthParams`) while having learned nothing — reported by the coordinator as `attack
= {'Mid': 0.0, 'Strong': 0.0, 'Weak': 0.0}`, `in_sample_residual_mean = 0.0`. Exactly the
class of thing CLAUDE.md warns about: "it never crashes, it just makes the results wrong."

Two responses, not one:

1. **The test was fixed** — the synthetic-recovery helper now anchors `as_of` one day after
   the last match, matching how a real caller would use it (`tests/test_team_strength.py`,
   `fit_team_strength_from_matches`).
2. **`fit_team_strength` now guards against it in production**, not just in the test: after
   computing every match's weight, `sum(weights) <= 1e-9` raises `TeamStrengthError` naming
   the degenerate condition, rather than silently returning a no-op fit that looks like a
   real (if boring) league-average model. This is real hardening earned by a test bug, kept
   in the shipped module rather than discarded once the test itself was fixed.

## 7. Team identity — CLOSED, session s003 (was: "the seam this module deliberately does not close")

`identity.py`, `providers/**` were READ-ONLY for the story that wrote this section originally;
they were OWNED for session s003, which is what let this close. `vaastav_player_gameweek_
stats`'s `team` column is a name string (`"Man Utd"`, `"Nott'm Forest"`) — verified live to
match the CURRENT `teams` dataset's `name` column exactly for every one of the 20 active clubs
(both come from FPL's own naming convention, not a cross-provider join blueprint §3.5 warns
against). This module still keys its internal model on the name string — no identity-
resolution machinery replaced it, and that was a deliberate choice this session (see §4.1):
switching the model's own internal representation to a numeric `code` would be a much larger
change (every `attack`/`defence` dict key, `predict_scoreline`'s public signature, every
existing caller) for the same correctness gain a STRING canonicalisation already buys.

**The fix, instead: canonicalise the string, at the join, before it ever reaches this
module's fitting internals.** `fplai.identity.TeamNameCanonicalisationMap` joins on FPL's
stable `teams[].code`, built from `vaastav_team_identity` — populated this session (§4.1's
task 1: was genuinely empty, now 140 rows, 7 seasons x 20 clubs, live-verified) — plus the
live `teams` snapshot. `build_match_table` accepts it as an optional, caller-supplied
`team_name_canonicaliser` parameter (default `None`, i.e. unchanged pre-fix behaviour); the
real fix is entirely opt-in at the call site, never bundled invisibly into the model's own
internals. §4.1 has the full live-verified before/after.

## 8. Determinism and the seed (CLAUDE.md rule 7)

**No stochastic component.** Parameters initialise at `0.0` (the gauge-neutral point, §3);
both optimisation stages (`_fit_attack_defence_home`'s Adam ascent, `_fit_rho`'s ternary
search) are deterministic functions of the training data and `TeamStrengthConfig`. No
`seed:` parameter exists in this module, and none was added as decoration.
`test_fit_is_deterministic_given_the_same_inputs` proves bit-for-bit identical output across
two runs of the same input. **The seam, stated rather than silently omitted**: a future
revision adding randomised initialisation, minibatching, or a Monte Carlo residual-
uncertainty estimate needs a `seed: int` threaded into whatever `random.Random(seed)`
instance it introduces at that point — there is none to thread today because there is no
randomness to seed.

## 9. No numpy or scipy — checked, not assumed

The brief said "check before assuming". Checked directly, 2026-08-21: neither `numpy` nor
`scipy` is installed in this project's `.venv` (`ModuleNotFoundError` on import;
`pyproject.toml`'s only runtime dependencies are `requests`, `polars`, `duckdb`, `tzdata`).
Per the brief's own instruction ("if the optimiser you want needs something not already
installed, stop and report rather than installing it"), this module is **pure Python** —
every vector/gradient in `_fit_attack_defence_home`/`_fit_rho` is a plain `list`/`dict`, and
the fit is a hand-rolled Adam optimiser plus a bounded ternary search, not a library call.
Fitting 2280 real matches over 20+ teams (400 Adam iterations) takes ~2.6 seconds on this
machine — no performance case for a dependency was ever made, so none was added.

## 10. Story 9's derived-capability framework as its first real consumer

**Mostly held exactly as designed** — `write_team_strength` routes through
`fplai.derived.write_derived` (never a bare `store.write()`), which stamps `is_modelled`,
`derived_from`, and the calibration columns itself, and the store's bidirectional naming
guard (`_require_derived_naming_invariant`) makes the observed/derived separation hold
independent of which function is called. Live-verified end to end (§11 below): a real fit's
ratings write, round-trip through `as_of()`, and are structurally absent from
`vaastav_player_gameweek_stats`'s own `as_of()` result.

**One real friction, raised as a finding to the Architect and now resolved (2026-08-22,
session `s003`).** §15.4 of `docs/wiki/provider-framework.md` states a derived capability
should be "registered once at import time, same lifetime as `CANONICAL_SCHEMAS`" — but
`tests/test_schemas.py` asserted an **exact** set of keys on `CANONICAL_SCHEMAS`/
`DATASET_ENTITY_KEYS`. Registering at `team_strength.py`'s import time — the first time
that stated intention was ever actually followed by a second real derived-capability module
— would have corrupted both assertions the instant `fplai.models.team_strength` was imported
in the same pytest session as `test_schemas.py`, which every full-suite run is. The session
that first hit this (`s002`) shipped a lazy-registration workaround instead (register only on
the first real `write_team_strength` call; register/unregister in a `finally`-protected test
fixture) and flagged the tension for the Architect rather than resolving it unilaterally,
since it touched `tests/test_schemas.py`, outside that task's owned paths.

**Architect ruling: neither of the two options originally offered.** Not "every future
derived module repeats the lazy-registration workaround" (nothing enforces the convention,
and it makes `CANONICAL_SCHEMAS`/`DATASET_ENTITY_KEYS`'s contents depend on **call
history** — had `write_team_strength` been invoked yet, in this process? — which conflicts
with CLAUDE.md rule 7's determinism requirement: the same test suite could see a different
registered set purely from incidental test-execution order). Not "loosen the exact-set
assertions to contains-at-least" either — that discards a guard whose actual job is catching
an accidental *observed* dataset; the exact-set check earns its keep specifically by being
strict.

**The actual fix is a partition, not a loosening.** `CANONICAL_SCHEMAS`/`DATASET_ENTITY_KEYS`
always held both observed (E2b, `is_modelled=False`) and derived (§12.2, `is_modelled=True`)
entries in one dict each — there was only ever one partition (E2b's) until team_strength
became the second real consumer. `fplai.schemas.observed_capabilities()` /
`derived_capabilities()` / `observed_dataset_entity_keys()` / `derived_dataset_entity_keys()`
split them by `is_modelled`, and `tests/test_schemas.py` now asserts an exact set over
**each partition separately**:

- The observed partition's assertion is exactly as strict as it always was — verified
  directly: re-running the *old*, unpartitioned assertion style against the current
  `CANONICAL_SCHEMAS` (with `fplai.models.team_strength` imported) fails with
  `extra beyond expected: {'team.strength_rating@gameweek'}` — proof the partition is
  load-bearing, not decorative.
- The derived partition gets a **new** exact-set assertion nothing enforced before — a
  strengthening (catches an accidental derived dataset), not a weakening.

**Determinism is restored by import-time registration**, exactly as §15.4 originally
intended: `_register_team_strength_capability()` now runs unconditionally at
`team_strength.py`'s own module level (defensively idempotent — checks `CANONICAL_SCHEMAS`
first — though Python's own import cache already makes a second `import` a no-op at the
top-level-code layer regardless). `ensure_team_strength_capability_registered()` and the
register/unregister-in-`finally` test fixture are gone; `registered_capability` is now a
plain lookup into the already-registered `CANONICAL_SCHEMAS[TEAM_STRENGTH_RATING_GAMEWEEK]`,
with nothing to tear down. `tests/test_schemas.py`'s two derived-side exact-set tests import
`fplai.models.team_strength` explicitly, so their result is a pure function of **which
modules were imported**, not of test-execution order or which other files pytest happened to
collect alongside them — the property CLAUDE.md rule 7 actually requires.

**`test_importing_the_module_does_not_register_the_capability` asserted the property being
deliberately reversed** — replaced by
`test_importing_the_module_registers_the_capability_at_import_time`, its direct inverse,
proven the same way the original was: in a genuinely fresh subprocess (not merely "no test in
this session happened to trigger it yet"), which also proves the new production behaviour
fails without the module-level registration line (verified: temporarily commenting it out
made the new test fail with `AssertionError: importing team_strength must register the
derived capability at import time`, then restored).

**This resolves the tension for every future derived-capability module, not just this one.**
The DC estimator and the captaincy backcast — §15.4's own two named future consumers — each
register their own capability at their own module's import time, and each gets its own
`import fplai.models.<theirs>` line added to the two derived-side exact-set tests, growing
the expected set rather than reaching for a per-module workaround. Full suite: **426 passed**
(424 baseline + 2 new partition tests), no regressions.

## 11. What the fit actually produced on real data

`python scripts/fit_team_strength.py --as-of 2026-08-21T17:30:00Z --fixture "Man City"
"Arsenal" --demo-write`, against the real `data/store/` (read-only; `--demo-write` targets an
isolated `tempfile.mkdtemp()` directory, never `data/store/` itself) — full output in
`scripts/fit_team_strength.py`'s own run log, reproduced here:

```
matches used: 2280  seasons: 2020-21, 2021-22, 2022-23, 2023-24, 2024-25, 2025-26
home_advantage=0.1888  rho=-0.1212
in-sample residual: mean=0.0085  std=1.2152
promoted-team prior applied to: Coventry City, Hull City, Ipswich Town

Top 8 attack:            Bottom 8 attack:
  Man City      +0.4740     Sheffield Utd  -0.0934
  Liverpool     +0.3885     Watford        -0.1057
  Chelsea       +0.3813     Wolves         -0.1300
  Arsenal       +0.3699     Norwich        -0.1550
  Bournemouth   +0.3520     Ipswich        -0.2423
  Man Utd       +0.3261     Burnley        -0.2575
  Newcastle     +0.3168     Leicester      -0.3155
  Brentford     +0.3118     Southampton    -0.3367

Best 8 defence (lower=better):    Worst 8 defence:
  Arsenal       -0.4669           Leicester      +0.3419
  Man City      -0.1041           Ipswich        +0.3620
  Liverpool     -0.0493           Burnley        +0.3700
  Brighton      -0.0130           Sheffield Utd  +0.3802
  Man Utd       -0.0072           Luton          +0.4345
                                   Southampton    +0.5081

Man City v Arsenal: home_win=0.329  draw=0.299  away_win=0.371
E[home]=1.216  E[away]=1.304  most likely scoreline: (1, 1)
```

**Passes the sanity check the brief asked for.** Top attack is the six clubs any Premier
League follower would name (Man City, Liverpool, Chelsea, Arsenal, Man Utd, Newcastle) plus
Bournemouth (a real, decay-weighted 2025-26 overperformer, not an error). Bottom attack and
worst defence are entirely relegated/struggling sides (Southampton, Sheffield Utd, Watford,
Norwich, Leicester, Burnley, Luton, Ipswich). Best defence is led by Arsenal by a wide margin
(`-0.467`, more than double the next side) — matches Arsenal's genuinely exceptional recent
defensive record, not an artefact. Man City (home) vs Arsenal (away) comes out close to a
toss-up with Arsenal narrowly favoured away (`0.371` vs `0.329`) and a `(1, 1)` most-likely
scoreline — plausible for two of the era's strongest sides, and specifically shows the model
is NOT just "the higher-attack team always wins": Arsenal's superior defence (`-0.467` vs
Man City's `-0.104`) is doing real work in a fixture where Man City has the better attack.
**No bottom club came out as the strongest attack, and no top club came out with the worst
defence** — the two failure modes the brief named explicitly did not occur.

### 11.1 Session s003 (2026-08-22) — before/after all three tasks, and a defensibility judgment

Same command, same `as_of`, run twice against the real store: once with
`--no-team-name-canonicalisation` (task 3 only — the promoted-team prior is estimated, but
Ipswich's identity is still split) and once with the fix fully on (both tasks). `home_
advantage`/`rho`/most fitted values move by <0.001 between the two §11 runs above and s003's —
noise from `data/store/` gaining rows between sessions (odds captures, later snapshots), not
from these three tasks, which touch none of that data.

Both runs' full console output is reproduced verbatim in `scripts/fit_team_strength.py`'s own
history; the tables below are hand-labelled subsets (the `(PRIOR)` annotation is not something
the script prints) for readability, not a reformatted script output.

**Before (task 3 only, Ipswich identity still split):**
```
promoted-team prior applied to: Coventry City, Hull City, Ipswich Town

Bottom 8 attack:                         Worst 8 defence:
  Coventry City   -0.3061 (PRIOR)          Hull City      +0.2216 (PRIOR)
  Hull City       -0.3061 (PRIOR)          Norwich        +0.2299
  Ipswich Town    -0.3061 (PRIOR)          Leicester      +0.3419
  Leicester       -0.3156                  Ipswich        +0.3621  <- real 2024-25 history,
  Southampton     -0.3366                  Burnley        +0.3700     under the WRONG key
```

**After (both tasks — the shipped default):**
```
promoted-team prior applied to: Coventry City, Hull City

Bottom 8 attack:                         Worst 8 defence:
  Ipswich Town    -0.2423 (real fit)       Hull City      +0.2216 (PRIOR)
  Burnley         -0.2574                  Norwich        +0.2299
  Coventry City   -0.3061 (PRIOR)          Leicester      +0.3419
  Hull City       -0.3061 (PRIOR)          Ipswich Town   +0.3621  <- SAME numbers, now
  Leicester       -0.3156                  Burnley        +0.3700     under the RIGHT key
```

**Defensibility judgment, stated plainly, not just "different":**

- **The promoted-team prior (task 3) is strictly more defensible.** `0.0` (league average) was
  never a claim about promoted teams specifically — it was what fell out of not having one. A
  prior estimated from 18 real promoted-team-season instances, all 6 of this store's
  promotion cohorts, isolated season-by-season so no club's later trajectory could leak into
  its own debut estimate, directly closes the symptom the brief opened with (Coventry/Hull/
  Ipswich Town ranking above Brighton/Man Utd on defence — compare `+0.2216`/`+0.3061` against
  Man Utd's real `-0.0072`: the gap is now the right SIGN and a real margin, not adjacent
  noise). It remains a single scalar applied uniformly to every promoted club regardless of
  spend or Championship form (stated in the module docstring, not hidden), so "more
  defensible" is not "solved" — a club-conditioned version is named as the next step, not
  built here.
- **The identity fix (task 2) is unambiguously more defensible, not merely different.** Before
  it, "Ipswich Town" — the exact string every real caller asks for, because it's what the live
  `teams` dataset calls the club — had ZERO matches under that key and got a prior tuned to
  the AVERAGE promoted club, despite this store holding a real, relevant, 2024-25 relegation
  season for literally this club. After it, the same real data (`attack=-0.2423, defence=
  +0.3621` — identical numbers, confirming the fix relabels rather than re-fits) is used
  instead of a population average standing in for it. Ipswich Town's real defence deviation
  (`+0.3621`) is WORSE than the new promoted-team-prior's average (`+0.2216`) — consistent
  with a club that went straight back down — while its real attack (`-0.2423`) is slightly
  BETTER than the prior's average (`-0.3061`). Both directions are informative and neither
  would be visible under the pre-fix key split; this is the concrete case the brief asked to
  watch for ("if the promoted-team fix makes something else look wrong, that is a finding").
- **Man City's attack rating is unchanged to 4 decimal places by BOTH fixes** (`+0.4740`, same
  as the original 2026-08-21 fit) — expected and confirms the isolation property in §3.1/§4.1:
  neither fix touches any team's fitted value except the specific clubs each targets.

## 12. Deliberately not built here

- **Odds shrinkage** — `shrink_to_odds()` exists as a marked, `NotImplementedError`-raising
  seam (never a silent no-op) documenting exactly where a live-time blend against
  `providers/odds.py`'s de-vigged 1X2 probabilities would attach. Blueprint §3.3: odds have
  no historical endpoint on the free plan, so this cannot enter a backtest; the model stands
  alone and is validated without it, per the brief.
- **The calibration report** — Phase 2's gate (Brier/log-loss/RPS over held-out fixtures,
  blueprint §7.1) is a later slice. This module emits the raw material for it:
  `ScorelinePMF.to_polars()` is the long-format per-fixture predicted distribution a future
  report would score against `team_h_score`/`team_a_score`; `write_team_strength`'s
  `CalibrationReference` is explicit that its own residual is IN-SAMPLE, not the gate.
- **A richer promoted-team prior** (§3) and **fixing the name-based team join** (§4/§7) —
  both real, both scoped out, both stated rather than silently absorbed into "average team"
  and "close enough" respectively.

## 13. Verification

- `uv run pytest tests/test_team_strength.py -q` — **29 passed**. Includes: `_dc_tau`
  formula-exactness against the Dixon & Coles (1997) definition; `ScorelinePMF` sums to 1 /
  non-negative / correct marginals on a hand-constructed degenerate grid; synthetic
  parameter recovery (three teams with known ground-truth attack/defence/home-advantage,
  near-noiseless repeated data, recovered ranking and home-advantage within `0.05`);
  determinism (bit-identical output across two runs); the promoted-team prior; three
  bitemporal-leakage tests including the two adversarial ones (§5); the store-as_of-is-empty
  regression proof (§5); the derived-capability write/read round trip and its physical
  absence from the observed dataset, against an isolated temp store; the lazy-registration
  proof in a genuinely fresh subprocess (§10).
- `uv run pytest -q` (full suite) — **424 passed**, 0 regressions outside this module's own
  files.
- `python scripts/fit_team_strength.py --as-of 2026-08-21T17:30:00Z --fixture "Man City"
  "Arsenal" --demo-write` — live, against the real `data/store/` (read-only) — full output
  §11 above; `--demo-write` confirmed `written=True, n_rows=31,
  dataset=derived_team_strength_rating`, `is_modelled` all `True` on readback, against an
  isolated temp directory only.

### 13.1 Session s003 (2026-08-22, XL-Coder) — the identity fix and the promoted-team prior

- `uv run pytest tests/test_identity.py -q` — **34 passed** (was 29; 5 new:
  `TeamNameCanonicalisationMap` build/canonicalise, live-name fallback, graceful degradation
  on an unresolvable pair, and both missing-column error paths). **Fail-first proven**: the
  5 new tests were run against the ORIGINAL (pre-session) `identity.py`, restored via `git show
  HEAD:src/fplai/identity.py` into a scratch copy first (never `git checkout`/`stash` on the
  real working tree) — all 5 failed with `ImportError: cannot import name
  'TeamNameCanonicalisationMap'`, then the real implementation was restored and all 5 passed.
- `uv run pytest tests/test_team_strength.py -q` — **35 passed** (was 29; 6 new: exact-prior-
  value assertion, the explicit-zero-override case, the no-history-team isolation proof, the
  `build_match_table` canonicalisation regression test, and 2 real-store-gated tests for the
  live Ipswich Town fix). **Fail-first proven the same way**: all 6 failed against the restored
  original `team_strength.py` (`TypeError: unexpected keyword argument 'no_history_teams'` /
  `'team_name_canonicaliser'`), then passed once the real implementation was restored.
- `uv run pytest -q` (full suite) — **567 passed**, 0 regressions outside this session's own
  files (baseline: 556 at commit `a78e980`; +11 new tests: 5 identity, 6 team_strength).
- **Task 1 live verification**: `vaastav_team_identity` was genuinely empty before this
  session (no directory under `data/store/`). `scripts/backfill.py --provider vaastav_archive
  --capabilities team.identity@season --seasons 2019-20,2020-21,2021-22,2022-23,2023-24,
  2024-25,2025-26` run `--dry-run` first (7 units, 7 requests), then `--execute` against an
  isolated `tempfile.mkdtemp()` store to verify shape (140 rows, 20/season, entity key
  `(season, id)` verified unique with **0 duplicates** on the real fetched data), then
  `--execute` against the real `data/store/`. Confirmed identical shape and uniqueness against
  the real store post-write.
- **Live verification of the fix's actual effect**: `TeamNameCanonicalisationMap.canonicalise
  ("2024-25", "Ipswich") == "Ipswich Town"` against the real store's `vaastav_team_identity` +
  `teams`; `fit_team_strength(..., team_name_canonicaliser=...)` against the real store puts
  Ipswich Town's real 2024-25 history in its rating and drops it from `teams_with_no_history`,
  leaving exactly `{Coventry City, Hull City}` — §4.1, §11.1.
- **What was NOT attacked from outside the sanctioned path this session** (stated, not
  hidden): `TeamNameCanonicalisationMap` is deliberately non-raising on an unresolved
  `(season, name)` pair (§4.1's design choice, distinct from `TeamIdentityMap`/
  `PlayerIdentityMap`, which DO raise) — this was tested for the INTENDED degrade-gracefully
  behaviour (`test_team_name_canonicalisation_degrades_gracefully_for_an_unresolvable_pair`),
  not adversarially attacked for a way to make it silently corrupt a resolved pair instead of
  merely leaving an unresolved one alone; a future session tightening this guarantee should
  attempt that before trusting it further.
