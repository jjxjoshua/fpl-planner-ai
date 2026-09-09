# Session Handoff — 2026-09-06 (end of session `s008`)

**Read this first, then `AGENTS.md`, then `docs/wiki/dispatch-protocol.md`, then
`docs/BLUEPRINT.md`.** This is the state of the world at the end of session `s008` (6 Sep). Phase 3
is closed; **Phase 4 (E7) is open and nine of its eleven stories are done.**

> **NEW ARCHITECT, START HERE. THE E7 PHASE GATE HAS RUN, AND IT FAILED — 0 of 3 seasons.
> That is a decision for the user, not a bug to go and fix.** `09-06`, seed 0, commit `9713c9c`,
> evidence at `docs/wiki/e7-gate-results.jsonl`, wall 5.36 h.
>
> | Season | H6 net | H1 net | H6 gross | H1 gross | H6 paid hits | H1 paid hits |
> |---|---|---|---|---|---|---|
> | 2023-24 | 2047 | **2071** | **2151** | 2095 | 26 | 6 |
> | 2024-25 | 1993 | **2000** | **2061** | 2036 | 17 | 9 |
> | 2025-26 | 2024 | 2024 | **2076** | 2068 | 13 | 11 |
> | Mean | 2021.3 | **2031.7** | **2096.0** | 2066.3 | | |
>
> **Read the gross column before concluding "the horizon doesn't work".** H=6 wins on gross points
> in **all three** seasons (+56/+25/+8) and loses on net in all three, purely on paid hits. **The
> longer horizon picks better squads and trades them worse.**
>
> **Leading hypothesis, NOT yet measured:** receding-horizon over-trading — the objective books a
> transfer's benefit over six rounds, the harness commits round `t` only and re-solves, so the
> benefit is earned only if the player is held while the **-4 is paid in full immediately**. The
> measurement that settles it is **transfer churn**, and the runner logs season totals only, so it
> needs a decision-level log plus a targeted re-run (~1.5 h for one season's H=6 arm).
>
> **NOTHING WAS TUNED AFTER SEEING THIS, AND NOTHING SHOULD BE.** No H sweep, no hit-cost
> adjustment, no objective change. The `09-03` ruling — choosing a setting by running several and
> keeping the best against these same three seasons — binds hardest at exactly this moment, when a
> one-line change would make the gate say yes. **AGENTS.md rule 6: a failing gate stops work and
> goes to the Architect; it is never patched around.**
>
> **What is DONE regardless:** S9 and S10 both shipped and both met their own story gates. S11
> (what-if engine, a 3) is the last named E7 deliverable and is not blocked by any of this.
>
> **BRANCH STATE, read before you commit anything.** s007's and s008's work sits on
> **`chore/s007-e7-grooming`, ahead of `origin/master` and NOT pushed.** That is
> deliberate — pushing is a phase-close act and the E7 gate has not run. **Check the branch
> immediately before every commit and stage paths explicitly**; in s004 a "commit and push" landed
> directly on `master` because the user had merged a PR mid-session and moved the checkout.
>
> **THE E7 GATE IS ALREADY DECIDED — do not redesign it.** H=6 must beat a myopic H=1 arm carrying
> the identical signal, both inside the stateful harness, both paying identical transfer costs and
> hits, over 2023-24/2024-25/2025-26. Phase 3's 2,280.3 is a stated **ceiling, not the bar** — it
> was scored with free weekly re-picks and no transfer costs. Full ruling in `PROGRESS.md`'s E7
> section. Run it **once** and report whatever it says.
>
> **The gate is now affordable.** S3a took the H=6 arm from a projected **11.5 h to 4.10 h**
> (measured, one run, real model stack). S8's MILP adds ~9-12 s per decision on a quiet machine.
>
> **§1 IS STALE — its GW3 deadline (01:30 Sat 5 Sep local) has PASSED.** Do not act on any date
> in §1 without re-deriving it from the API first. The *procedure* in §1.2 is still correct; only
> the dates are dead. Nobody has updated the live-season state since 5 Sep, and this handoff does
> not invent one.
>
> **Track A stays manual, by the user's choice** — `fpl-elite` plus odds captures, decided after
> the pressers and before the deadline. **The engine must NOT be used for a live gameweek yet**, and
> the reason is sharper than "blocked": it returns plausible-looking nonsense without raising.
> §3.0 is **still true** — S0b fixed the staleness half, but `value`/`selected` remain 1236/1236
> NULL because FPL's `event/{gw}/live/` carries no price or ownership at all, so the guard still
> correctly fires and the `elements` substitute is still unbuilt.

**How work is dispatched changed fundamentally this session.** `docs/wiki/dispatch-protocol.md`
is now mandatory reading before writing any brief: probe before the brief, numbers from
generators never prose, pilot-then-replicate, evidence not claims, and a Fibonacci ceiling of 8.
The Architect owns `PROGRESS.md` and grooming; the Scrum Master is post-phase only and can
**withhold** the phase-done mark. `PROGRESS.md` tracks *what*; this explains *where we are and
what is about to happen*.

---

## 3.0 DO NOT USE THE ENGINE FOR A LIVE GAMEWEEK — it answers wrongly without raising

**Measured `09-04` on a real upcoming GW3 fixture** (2026-27 R3, element 1, Arsenal GK):

| Feature | default (`allow_live_season=False`) | `allow_live_season=True` |
|---|---|---|
| `games_played_this_season` | **0.0** | 1.0 |
| `cold_start` | **True** | False |
| `trailing_start_rate_3` | **0.0** | 1.0 |
| `trailing_minutes_mean_3` | **0.0** | 90.0 |
| `prev_gw_value` | 0.0 | **0.0** |
| `prev_gw_selected_log1p` | 0.0 | **0.0** |

**Both paths are wrong and NEITHER RAISES.** The default excludes every 2026-27 row by design, so a
player who has started two gameweeks reads as an unknown cold start. `allow_live_season=True` fixes
the trailing features but tells the model the player **costs £0.0m**, because `value`/`selected` are
NULL on all 610 FPL-API rows and get coalesced to zero instead of refused.

The registered story said the features "cannot be assembled". The truth is they **are assembled,
silently wrong** — strictly more dangerous, and the exact failure class lesson 6 exists for.

**Why it survived every gate:** all assembler tests used *historical* fixtures, where vaastav
supplies price and ownership, so the null path was never exercised. The equivalence gates compared
against `build_training_table` on historical rows — correct for what they tested and structurally
unable to catch this.

**So the fix is two things, not one:** the `elements`-based substitute (`now_cost`,
`selected_by_percent`, resolved as-of the deadline) **and a guard that raises** when a required
feature resolves to a coalesced NULL for a live season. The guard is the half that stops a wrong
answer reaching a real decision, and it should land first.

Until then: **Track A is manual.** `fpl-elite` plus odds captures — the process that produced GW2's
82 against an 81 average.

## 1. Time-critical — check these before anything else

> **STALE AS OF `09-06`. EVERY DATE IN THIS SECTION HAS PASSED.** It was last true on 5 Sep, and
> session `s007` was a build session that touched no live-season state. **The GW3 deadline below is
> gone; whether its decision was made, and whether GW3's EO sample was captured, is NOT RECORDED
> HERE and this handoff does not guess.** Re-derive the current gameweek and deadline from the API,
> and check `data/store/` for the latest `picks` sample, before acting on anything in this section.
> **The GW3 EO sample is the one item here that cannot be backfilled if it was missed** — FPL
> re-issues entry IDs annually, so an unsampled gameweek is a permanent hole in the only dataset
> that can calibrate the captaincy model (blueprint §3.4). Check it first.
>
> **What is still valid in this section: the PROCEDURE (§1.2), the standing shutdown risk, and the
> capture-offset design.** Only the dates are dead.

**Session `s006` ended 3 Sep with Phase 3 complete and the E6 gate PASSED, audited done.**

| When | What | Recoverable? |
|---|---|---|
| ~~Tue 1 Sep~~ **DONE `09-02`** | **GW2 EO sample captured — 144,795 picks rows** (GW1 was 139,260). The unbackfillable captaincy dataset has no hole for GW2. **GW3's sample is the next one**, after GW3 settles. | — |
| **Fri 4 Sep, evening local** | **GW3 decision** — deadline is **01:30 Sat 5 Sep local (04 Sep 17:30 UTC)**, in the user's overnight, so the call must land Friday evening. Odds capture Thu/Fri, `fpl-elite` intel Thu. **GW4 is 12 Sep 12:30 UTC = 20:30 Sat local — an early kickoff, not the usual 01:30.** | Yes |
| ~~Before Phase 3 consumes them~~ | ~~**Cards NONE/YELLOW need an isotonic calibration layer**~~ — **CLOSED 29 Aug.** Nested OOS isotonic layer shipped; slopes 0.592→0.845 and 0.630→0.901, pooled log-loss 0.4017→0.3991 and Brier 0.2247→0.2245 (calibrated strictly beats raw). See §3. | — |
| ~~**Waiting on the user**~~ **ALL THREE CLOSED 31 Aug** | (1) **`THE_ODDS_API_KEY` rotated** — confirmed by the user 31 Aug. The s002 exposure (one transient `urllib3` DEBUG print of the full key) was already closed in code (`snapshot_odds.py:270`, `providers/odds.py:686`); the rotation closes the credential itself. (2) **`WakeToRun` decided — set to `True`** on both `fpl-ai snapshot_bootstrap` and `fpl-ai snapshot_odds`, read live from `Get-ScheduledTask` 31 Aug. (3) **`fpl-ai snapshot_odds` is REGISTERED and running** — 36 heartbeats, `LastTaskResult=0`, `NumberOfMissedRuns=0`. Items 2 and 3 had been carried as open since s004 and were simply never re-read; the same failure mode as the `StartWhenAvailable` claim corrected on 29 Aug. **Read the live task before writing down what it is configured as.** | — |
| **Standing risk — RE-DIAGNOSED 31 Aug: it is SHUTDOWN, not sleep** | The gap is real and recurring — both jobs lost **11 hours on 31 Aug** (03:45 → 14:45 local). But the cause is now measured, not inferred: the System event log shows a **user-initiated power-off at 04:00 local** (event 1074) and boot at 14:35. **`WakeToRun=True` cannot help — it wakes a sleeping machine, never a powered-off one**, so the lever named across s003-s005 was never going to close this. `StartWhenAvailable` fires one catch-up run after boot and cannot backfill an 11-hour window either. **The only fix is behavioural: sleep the machine, do not shut it down.** The user committed to leaving it on through the night of 1 Sep. Deadline-adjacent captures (T-26h = 23:30 Thu local, T-2h = 23:30 Fri local for GW3) fall inside waking hours by design — the 78/26/6/2 offsets exist precisely so no capture needs a 01:00 local machine. | Snapshots, no |

### 1.1 Track A — live season state

**Read live from the FPL API `09-04`, not from recollection.**

| | |
|---|---|
| GW1 | **43** vs a 50 average · OR 6,382,778 |
| GW2 | **82** vs an 81 average — par · transfer Gibbs-White → Rogers, captain Haaland |
| Overall | **125 points**, OR **5,375,251** — improved ~1.0M from GW1 |

GW2 is settled (`finished` and `data_checked` both true) and its **EO sample is captured**:
144,795 picks rows across 9,653 entries.

The GW2 transfer was decided on **fixture, not fitness** — Forest at 16% at Anfield, no goals in two
competitive games, Gibbs-White fourth in their penalty order — specifically so the call did not
depend on a Friday presser landing in the user's overnight. That reasoning is the template:
**a rule that resolves before the user sleeps beats a correct rule that needs a 2am check.**

**GW3 is the live one.** Deadline **01:30 Sat 5 Sep local (04 Sep 17:30 UTC)**, so the decision
lands Friday evening. Note the system is **not** advising this one — Track A remains manual, and
will until the live price/ownership gap closes (§3.6).

### 1.2 Run procedure for every EO sample

> **RUN IT IN THE EVENING, MANUALLY.** Machine-availability constraint, not a preference.

**0. Pre-flight:**
```bash
uv run python scripts/check_heartbeat.py --job snapshot_bootstrap --max-gap-minutes 60
```
Exit 0 healthy · 1 gap exceeded · 2 no data. **Never pipe the command being measured** — in bash `$?` after a pipeline reports the last command's status, which produced a false green in s003.

**1. Confirm the gameweek settled** — `finished` and `data_checked` both true; the script refuses politely (exit 0) if not.

**2. Dry run, then the real run:**
```bash
uv run python scripts/sample_picks.py --gw <N> --dry-run -v
uv run python scripts/sample_picks.py --gw <N> -v
```
Exits **cleanly (code 0)** pre-deadline, pre-scoring, or inside FPL's ~27-minute post-deadline maintenance window — a clean exit means *retry later*, not *failure*. Resumes automatically; persistence is chunked (500).

**3. Post-run:** grep `GRAIN VIOLATION`, confirm `automatic_subs` non-empty, check `hit_rate`.

## 2. Where the project is

**Phase 0 — mostly done.** Ingest spine, bitemporal store, provider framework (**E2b COMPLETE, all 12
stories**, 2026-08-21), five providers (FPL API, PL API, vaastav archive,
olbauday archive, The Odds API), backfill orchestrator.

**Phase 1 — GATE RE-OPENED AND CLOSED 2026-08-22.** It was marked PASSED on 08-21 when it
should not have been: `greedy_form` was **not reproducible** (1907 / 1909 / 1887 on identical
seed, code and data), and §7.2's gate text is "real, **reproducible**, leak-free totals", so
reproducibility *is* the gate.

**The obvious hypothesis was wrong, which is worth remembering.** The Architect attributed it
to `as_of()`/`observations()` lacking a canonical `ORDER BY`. That gap was real but **did not
independently reproduce the spread**. The dominant cause was polars'
`.unique(maintain_order=False)` in `_build_view()` returning a different order on every call
(verified: 5 calls on the identical real 692-row slice, 5 different orders), feeding
`build_squad`'s hill-climb, whose "first found, strictly better" swap took whichever tied
candidate happened to be listed first — routine for `greedy_form`, where players tie on
trailing-window totals constantly. Fixed at both layers anyway: an explicit deterministic swap
key `(gain, -in_id, -out_id)`, plus the canonical ordering, so correctness no longer rests on
storage layout staying accidentally stable.

**Verified closed:** 4 consecutive runs bit-identical. Restated headline **Greedy 1,889-2,122**
(was 1,891-2,127); random and template bit-identical to the original publication. **The Phase 1
conclusion is unchanged — no baseline beats the user in any comparable season**; only 2024-25's
gap narrowed, 312 -> 297.

```
Random      497 - 1,008
Template  1,895 - 2,092
Greedy    1,889 - 2,122     <- RESTATED 08-22 (was 1,891 - 2,127)
Human     2,019 / 2,251 / 2,169     <- the user's actual seasons
```

**No baseline beats the user**, despite baselines re-deciding weekly without paying transfer
costs. **Phase 3 must beat ~2,100, ideally the 2,146 career average.**

**E2b complete (2026-08-21).** Stories 8, 9-deferred, 10b closed the tail. olbauday adapter
(`player.attributes@gameweek`, `gameweek.field_summary@gameweek`) and The Odds API adapter
(`match.odds@fixture`, `player.goal_odds@fixture`), both live-verified. Suite 362.
**Four odds captures landed** across 8.7h (14:26, 14:45, 22:56, 23:06 local): `odds_match_odds` 3,390
rows, `odds_player_goal_odds` 4,216 rows, 10/10 GW1 fixtures every time. The later two also cover
**5 GW2 fixtures' match odds**, starting next week's movement series early. Unresolved names are
preserved with `identity_resolved=false`, never dropped. **Credits are a MONTHLY budget, not a season one** — corrected 29 Aug from `cache/odds/_credit_tracker_state.json`, which keys on `{period: "2026-08", spent: 128}`; it resets to a full 500 on 1 Sep. Observed cost ~18-20 credits per capture run. The earlier "95/500 used, 405 remaining" reads as a season remainder and was being planned against as if scarce;
the durable `CreditTracker` reconciled against provider headers (local 96 vs reported 95, within
tolerance 2) on real use. FPL element count rose **599 → 600** during the session.

**Phase 2 — four of six models done.** Story 9 (derived-capability framework), Dixon-Coles team
strength, minutes, defensive contribution and attacking involvement have all landed.

| Model | State |
|---|---|
| **Team strength** | Dixon-Coles on xG, 2,280 matches / 6 seasons. Man City top attack (+0.4737), Arsenal a clear outlier best defence, relegated sides at the bottom. Promoted-team prior estimated from 18 promoted-team-seasons |
| **Minutes** | Three-state + six-band PMF, walk-forward. Gate PASSED at log-loss 0.3392, Brier 0.1048 after nested out-of-sample isotonic recalibration (pooled ECE 0.0470 → **0.0079**) |
| **Defensive contribution** | Gate PASSED both groups. Thresholds now **pinned by observation** (DEF 10, MID/FWD 12) and resolved as-of, `verified=True` |
| **Attacking involvement** | Gate PASSED both stats both metrics, 143 folds / 43,505 OOS rows. Goals log-loss 0.2347, Brier 0.0673; assists 0.2370 / 0.0676. Binomial thinning of team goals — the output *is* the share, not an approximation of it |
| **Bonus (BPS)** | Gate PASSED, 223 folds / 160,992 OOS rows. Log-loss 0.1881, Brier 0.0807. Bonus PMF derived from a Monte Carlo over the whole fixture — it is a rank-within-fixture phenomenon, so `predict_bonus_pmfs_for_fixture` takes a **fixture**, not a player. Award rule pinned from the archive: **99.92% of 2,569 fixtures** |
| **Cards / discipline** | Gate PASSED, 219 folds / 64,531 OOS rows. Log-loss 0.3973, Brier 0.2215. 3-class `{NONE, YELLOW, RED}` with minutes as an exposure offset. **RED is base-rate only** — slope −0.081, no usable ranking. **Referee measured as worth ~nothing** (Δlog-loss +0.0002): do not build an announcement-time capability |
| **Calibration report** | **DONE — E5 GATE PASSED `08-29`.** **20 outcomes: 3 PASS, 15 PASS-with-accepted-departure, 2 BASE-RATE-ONLY, 0 FAIL** (re-counted 31 Aug from the live report after GK saves was folded in — the earlier "16" predates it) `docs/wiki/calibration-report.md`, regenerable via `scripts/calibration_report.py`. ~~One blocking condition on Phase 3~~ — **CLOSED 29 Aug**: cards NONE/YELLOW got the nested isotonic layer (0.592→0.845, 0.630→0.901, later 0.875/0.931 after the saturation fix). **The report itself is one fix behind** — regenerated 30 Aug 06:57Z, before the isotonic-saturation fix landed that evening, so it still narrates minutes' since-resolved regression (0.3658) and cards at the superseded 0.845/0.901. No verdict is wrong; the narration is. Regenerate before trusting its prose |

**PHASE 2 IS COMPLETE — E5 GATE PASSED 2026-08-29.** All six models built and gated, plus the
calibration report. **20 outcomes: 3 PASS, 15 PASS-with-accepted-departure, 2 BASE-RATE-ONLY, 0 FAIL** (re-counted 31 Aug from the live report after GK saves was folded in — the earlier "16" predates it) `docs/wiki/calibration-report.md`, regenerable via `scripts/calibration_report.py`.

**The gate itself was amended on 08-29 before it was judged** (blueprint §7.1, two
amendments-table entries). Proper scoring rules alone no longer gate this phase: they reward
calibration *and sharpness* jointly, so they establish that a model is better, never that its
probabilities are trustworthy — which is exactly what the optimiser consumes. Cards' `RED`
made it concrete: log-loss 0.0304, Brier 0.0042, ECE 0.0060, all excellent, with a calibration
slope of **−0.081**. The gate as originally written would have passed it.

**Three things the report settled that had been open:**

1. **"Beat greedy on calibration" was literally unbuildable** — `greedy_form` is a
   squad-selection baseline emitting no probabilities. Resolved: the per-model
   **player-trailing-rate baseline IS greedy recast as a probability**, and every outcome is
   already gated against it. Written down so no future session re-opens a closed gate.
2. **Bonus's ECE-vs-slope disagreement** — quantile ECE is materially larger than equal-width
   on every bonus outcome, so the slope was right and the binning was misleading (lesson 10).
3. **Two BASE-RATE-ONLY outcomes**, both correct on the evidence: cards `RED` and — newly —
   **team_strength's `DRAW`** (slope 0.147, CI containing zero). Football-plausible: draws are
   the classically unpredictable outcome, now measured rather than assumed.

**ARCHITECT RULING — one acceptance in the report was REJECTED.** It accepts bonus's
*underconfident* departures partly because underconfidence is the safer direction for an
optimiser to inherit than overconfidence — correct — then accepts cards NONE/YELLOW, which are
**overconfident** (slopes 0.592/0.630, CIs nowhere near 1), on the milder ground that ECE is
low. By its own argument the dangerous direction deserves *less* tolerance. The remedy is
proven here: minutes went 1.114 → 0.972 and ECE 0.0507 → 0.0131 with a nested isotonic layer.
**E5 passes, but cards NONE/YELLOW carry a blocking condition on Phase 3.**

*(Superseded 08-29 — both questions below were settled by the report. Kept for the reasoning.)*
**What had been open before the calibration report:** two calibration questions. **Bonus** measured ECE at 0.0002–0.0091 — an order of magnitude
below the 0.0470 that triggered the minutes calibrator — but its calibration *slope* departs
from 1.0 on outcomes 0 and 3 (1.417, 2.218). ECE and slope disagree, and a gate must not
inherit that ambiguity; the next step is a quantile-binned reliability re-check. And
**attacking** ships with **no reliability measurement at all** — no ECE, no calibration slope or intercept. It passed on
log-loss and Brier, but those are proper scoring rules that reward calibration *and* sharpness
jointly, so beating a baseline on them does not establish that the probabilities are
calibrated. The minutes model is the precedent *and* the warning: it passed its own gate at
log-loss 0.3464 / Brier 0.1062 while carrying ECE 0.0470 and slope 1.114. Measure first, then
decide whether a calibrator is needed — do not assume either precedent transfers.

### Phase 3 — opened 29-30 Aug, session s005

The s005 audit found E6 tracked as a single line, "Single-period MILP", hiding two
prerequisites that did not exist. Both now do, plus the seventh model.

| Piece | State |
|---|---|
| **`src/fplai/scoring.py`** | DONE. `score_outcome(outcome, position, config) -> int` over a `RealisedOutcome`, every point value read from live `game_config` (rule 4). **610/610 exact on real settled GW1** per its author; **32/32 on GW2**, re-verified by the Architect against a gameweek that agent never saw. Forward-only by construction — `load_scoring_config` raises rather than scoring a past season with today's rules (lesson 7). Found a **third** missing config value the brief had undercounted: `GOALS_CONCEDED_POINTS_DIVISOR = 2`, alongside the 60-minute cliff and the saves divisor |
| **Cards calibration** | DONE — **E5 blocking condition CLOSED**. Slopes 0.592->0.845 and 0.630->0.901, calibrated strictly beats raw on both pooled metrics. Closed on a *measured* residual of 0.038 pts/appearance, near common-mode — not on the improvement looking large. See §3 |
| **`src/fplai/models/saves.py`** | DONE — seventh model, gate PASSED. NB2 chosen on measurement (var/mean 1.279, skew +0.807). **Ships a negative result**: the nested isotonic layer was built, attack-tested, and measurably made things *worse* (slope 0.786 -> 0.739), so it ships `calibrate=False` — the opposite of cards' default, on its own evidence |
| **`src/fplai/points.py`** | DONE. `simulate_fixture_points_pmfs` — seeded Monte Carlo over a **whole fixture**, because bonus is rank-within-fixture and cannot be computed per player. One shared scoreline draw per fixture and one shared minute-band draw per player carry the within-fixture correlation. Emits a PMF over integer points, never a scalar (rule 5). Cross-fixture correlation explicitly left to Phase 5 |
| **Live gameweek ingest** | DONE. FPL is now a **second provider of the existing `player.gameweek_stats@gameweek` capability** (§12.6 swappability), writing `fpl_api_player_gameweek_stats`, plus `src/fplai/gameweek_stats.py` — a capability-level reader unioning both sources as-of. Union now returns **180,560 rows: 179,950 vaastav across 7 seasons + 610 FPL for 2026-27 GW1**. Entity key `(season, round, element, fixture)` **measured** unique on real data. Scoring round-trip reproduces FPL's own `total_points` **610/610**. **Double-gameweek path is honestly degraded** — per-fixture rows from `explain` with non-scoring counts NULL, never 0 — and is **unverified**, since 2026/27 has had no DGW |
| **`position`/`team` join** | DONE, at ingest time. **600/610 resolved**; the 10 unresolved are elements 601-610, added to `bootstrap-static` *after* GW1's deadline — **correct bitemporal behaviour, not a shortfall**. Leakage guarantee attacked at two levels and mutation-proven |
| **`was_home`/`opponent_team` leak** | **FIXED — the leak was LIVE, not latent.** 8 elements changed clubs in the 9 days after GW1's deadline; **5 had concretely wrong stored values** — Pinnock's row named *his own club* as the opponent. Store re-ingested, 0 self-opponent rows remain, round-trip holds at 600/600. Residual: fixture *attribution* for an unused player with empty `explain` still uses current-bootstrap team |
| **L2 penalty scaling** | **FIXED IN ALL SEVEN MODELS**, loss and gradient consistent. Root cause of a symptom worked around three times. minutes: log-loss **−12.7%**, ECE **−60%**, slope 1.114→0.945. attacking: 5-75% better. DC: better on log-loss/Brier but slope overshoots (0.677→1.262) — unresolved tradeoff, flagged. bonus: **measured negative**, <0.2% (BPS residual variance ~20-80 dwarfs the penalty at any `n`). cards: ~0.6%, isotonic layer still earns its place. **No default changed; no gate verdict moved** |
| **MILP** | **BUILT** — `src/fplai/optimiser.py`, `highspy` 1.15.1, 20 tests. Constraint-correct, deterministic, captain as a real decision variable. **But see §3.6 — the E6 gate has NOT been genuinely attempted**, because the optimiser was gated on a proxy signal, not on the model stack |

**The E6 gate is unchanged and is the real bar: beat the template over 2+ backtested
seasons (~2,100 points, ideally the 2,146 career average).**

### Data in hand

- 7 seasons of vaastav gameweek history, **179,960 rows**, 2019-20 … 2025-26
- Ownership (`selected`) and price (`value`) for all 7 seasons
- `expected_goals` / `expected_assists` / `starts` from **2022-23**
- Defensive actions (`tackles`, `recoveries`, `clearances_blocks_interceptions`,
  `defensive_contribution`) for **2025-26 only** — the DC calibration set (§11)
- Live FPL ownership snapshots every 30 min since 2026-08-19

---

## 3. Open bugs and gaps — logged, not patched

| | |
|---|---|
| **`pl_match_fixtures.matchweek` is NOT FPL's `round`** | Diverges on **101 of 2,274 matches (4.4%)**, as far apart as round 22 vs matchweek 8 — postponed fixtures keep their original matchweek while FPL re-rounds them. Any match-grain model joining on matchweek silently mis-assigns 4.4%. **Gates all 832,890 already-ingested rows of `pl_team_match_stats`**, which is otherwise the input the DC team-style fix needs. |
| ~~**Cards NONE/YELLOW overconfident**~~ **CLOSED 29 Aug** | Nested OOS isotonic layer shipped; RED left untouched (base-rate-only, calibrating it is meaningless). Slopes **0.592→0.845** [0.794,0.896] and **0.630→0.901** [0.849,0.954]; quantile ECE 0.0124→0.0085 / 0.0124→0.0044. 197 folds, 58,461 OOS rows. **Both CIs still exclude 1.0, so the residual was sized in POINTS before the blocker was closed:** across 74,564 real appearances (yellow base rate 12.5%, card points/app mean −0.1375 vs a total-points std of 2.974), the worst mispricing anywhere in the realistic risk range is **0.038 pts/appearance** — ~1.3% of one standard deviation, peaking near p=0.16 and near common-mode rather than differential. Accepted on the same basis as minutes' own residual (0.972, CI excluding 1). **Contrast the GK saves ruling made the same day: 0.21 pts/app and systematically differential → build.** Same test, opposite answers, on purpose. |
| ~~**NO 2026/27 PERFORMANCE DATA**~~ **CLOSED 30 Aug** | `vaastav_player_gameweek_stats` holds 2019-20…2025-26; `pl_match_fixtures` 2020…2025. The live FPL datasets (`elements`, `events`, `picks`, `teams`) are current-**state** snapshots, not per-gameweek performance. Every model trains and predicts off the vaastav dataset, so the system can produce a points PMF for a **historical** fixture and **not for the upcoming one**. AGENTS.md line 116 already noted “nothing persists `event/{gw}/live/` to the store” as a scoping aside; nobody drew the inference. **The E6 gate is NOT blocked** — the backtest runs on history, which is all present. What is blocked is live weekly use and the Phase 3 decision brief. In progress s005. |
| **Models read a DATASET, not a CAPABILITY — the underlying flaw** | Every model hardcodes `DATASET = "vaastav_player_gameweek_stats"` with its own `build_training_table`; `backtest/data.py` and `backfill.py` do the same. So a second provider for an already-existing capability is invisible to them. Architect decision s005: **sibling dataset for the FPL provider + a capability-level union reader** (`src/fplai/gameweek_stats.py`), purely additive so existing data is never migrated. **Migrating the six models onto it is a deliberate follow-up**, not yet done. |
| ~~**L2 penalty unscaled in five models**~~ **CLOSED 30 Aug — fixed in all seven** | `minutes.py:753`, `attacking.py:715`, `bonus.py:845`, `cards.py:1050`, `defensive_contribution.py:1043` all compute `-sum(ll)/n + l2*sum(beta**2)`, making the penalty **effectively `l2*n`**. Only `saves.py:747` has the corrected `(l2/n)` form. **This is the root cause of a symptom the project worked around three times**: defaults drifted to minutes 1.0, DC 1.0, attacking 0.01, bonus 0.01, cards 0.001, and `attacking.py:307-316` records that `l2=1.0` “fails the §7.1 gate outright” while `bonus.py:242` calls it a cost “the project already paid for”. `PROGRESS.md` blames *feature scales*; the real cause is *scale of n*. **Not established that any gate is invalid** — minutes and DC still run at 1.0 and passed with small ECEs, which a crushed intercept could not produce. Being measured in s005. |
| **`scripts/calibration_report.py` has no `run_cards` path** | It hardcodes `calibrator_built=False` for every model except `minutes`. The regenerated report's cards **numbers** are the honest calibrated series, but its two auto-rendered **decision** bullets still describe the pre-calibration state, and carry a clearly-marked manual addendum saying so. A generated document containing two bullets that contradict the table directly above them is a trap for a future session reading top-down. |
| **`walk_forward_validate` `calibrate` default now differs across sibling models** | `True` in `cards.py`, `False` in `minutes.py`. One of them should move. |
| **No GK saves model — and the omission is BIASED, measured 08-29** | Saves are **18.9% of all GK points** (2,999 of 15,893 across 4,587 GK appearances, 6 seasons), 0.65 pts/appearance. Ex-post they rarely flip a ranking (rank-corr 0.944-0.985 with vs without), **but that is not the relevant test** — the optimiser ranks on *predicted* points. Pooled corr(saves/app, CS/app) = **−0.527** (negative in all six seasons), so dropping saves removes **0.762 pts/app from bad-defence keepers and only 0.552 from good-defence keepers** — a systematic ~0.21 pts/app differential, ~8 pts/season, always favouring premium keepers on strong defences, which then distorts budget allocation across the other 14. **Ruling: build it**, gated to the full E5 standard. Small model — saves is a conditional count on shots faced, and `team_strength` already emits the opponent goal distribution. |
| **Phase 3 has two unwritten prerequisites** | Between six outcome models and a MILP there is **nothing**. Needed first: (1) a **scoring function read from `game_config`/`game_settings`** (rule 4) — Phase 1 legitimately avoided this by scoring from stored `total_points` (lesson 7), so nothing in the codebase converts outcomes into points; (2) a **player-GW points PMF** composing minutes × attacking share × DC × bonus × cards × CS/GC (rule 5 forbids collapsing it to a scalar at the boundary). Verified by grep: every `xPts` reference in `src/` is a comment warning *against* scalar xPts. **`highspy` is also not yet a dependency.** |
| **Bonus underconfident on 3 of 4 classes** | Slopes 1.089–2.219. May ride: underconfidence is the safe direction and the baseline margin is wide. A named seam, not a defect to fix now. |
| **Lineups/subs limited to 2024-25+** | vaastav carries **no `opta_code` at all** for 2019-20…2023-24 (verified: 100% null), so `PlayerIdentityMap.build()` correctly refuses. The PL factory's identity build is now lazy, so capabilities needing no player identity (fixtures, team stats, officials) ingest for all six seasons — but lineups and subs genuinely cannot. |
| **Referee is not a pre-deadline feature** | Officials are anchored at kickoff, ~90 min after the FPL deadline. Measured as worth **+0.0002 log-loss** anyway, so **do not build an announcement-time capability** — blueprint §4 amended. |
| olbauday capabilities have **no GrainPlan** | Deliberate. One `playerstats.csv` per SEASON, unlike vaastav's per-gameweek file, so naive per-GW enumeration re-decodes ~9.6MB up to 38 times. Needs a design decision. |
| ~~**Odds identity: 36 unresolved names**~~ **LARGELY CLOSED 08-27** | The "alias-map gaps" reading was wrong — there is no alias map. The odds feed sends FULL LEGAL names while FPL's `web_name` is often a mononym that is **not** the last token (`Murillo Santiago Costa dos Santos` → `Murillo`; `Ruben Dias` → `Rúben`), and `ß` does not decompose under NFKD so `Groß` could never meet `Gross`. Fixed in `_PlayerNameIndex` (rung 3 + eszett folding): **17 of 26 newly resolved, zero ambiguous, zero regressions.** The 9 left are genuinely not FPL players, or spelling variants (`Yeremi` vs `Yeremy`) the resolver must not guess at. **Rows already written stay `identity_resolved=false`** — the fix applies to future captures; repairing history is a separate backfill. |
| 2024-25 olbauday **degraded** | Different nested layout, no `gameweek_summaries.csv`, `playerstats.csv` 29 columns narrower. Raises for want of an in-archive deadline source. |
| Schema validates **presence, not dtype** | Why a tz-aware write reached the store unchallenged. Documented, not closed. |
| **2019-20 incomplete** | 29/38 gameweeks in store; upstream has no `position`/`team` columns at all. Excluded from Phase 1 headline results. |
| **2022-23 missing GW7** | Upstream file decodes to zero rows. Ran 37/38. |
| olbauday **shot-level** data | `By Gameweek/GW{N}/shots.csv`, 2024-25+, aligned to FPL element ids. Deferred from story 10b; a Phase 2 attacking-model input. |
| ~~**DC estimator residual shape**~~ **RESOLVED 08-22** | The near-Gaussian placeholder was **wrong**, as suspected. Measured out-of-sample on 2025-26: **right-skewed, skewness +0.40 to +0.44** in both `DEF_CBIT` and `MID_FWD_CBIRT`. Replaced with the real shape. |
| ~~**DC thresholds pinned but NOT wired in**~~ **CLOSED 08-27** | New observed capability `game.dc_threshold_observation@gameweek`; `build_dc_threshold_set(store, as_of=...)` resolves the latest *pinned* observation and carries `verified=True` through to the persisted `threshold_verified`. **The press-only dependency in the rule set is closed.** |
| **Reserved SQL words in entity keys** — *fixed, but read this* | The store interpolated entity-key columns into `PARTITION BY`/`ORDER BY` as bare identifiers. `(season, group, round)` broke `as_of()` outright — latent since the beginning, for every dataset, and invisible to fixtures because none had ever used a reserved word. Now quoted via `_quote_ident()`. **Caught by `tests/test_store_invariants.py` against the REAL store** — lesson 6 again. |
| **`explain` lists only scoring identifiers** | Confirmed on the real settled GW1 payload: 31 `defensive_contribution` entries, every one `points=2`, zero `points=0` entries for *any* identifier across 610 elements. Anything inferring a negative from `explain` is inferring from a set that cannot contain it. The unscored count lives in `stats`, which is per-element-per-gameweek and so **cannot be attributed to a fixture on a DGW**. |
| **Odds market omits some FPL-relevant players** | Rogers (CHE, £7.5m, 25.9% owned) had **no anytime-goalscorer price** at either UK book on 27 Aug, while 43 other players in the same fixture did. Absence from the market is not evidence about the player; it is a hole in the feature. |

---

## 3.5 E6 PASSED `09-03` — and the 31 Aug ruling was right to wait

**Gate result**, seed 0, commit `495a168`, 152 minutes, raw data
`data/gate/e6_gate_results.jsonl` (gitignored — the durable record is `PROGRESS.md`'s table):

| Season | model_stack | MILP proxy | Template | Margin |
|---|---|---|---|---|
| 2023-24 | **2,221** | 1,948 | 2,092 | **+129** |
| 2024-25 | **2,392** | 2,033 | 1,906 | **+486** |
| 2025-26 | **2,228** | 1,945 | 1,951 | **+277** |

Mean **2,280.3** against the template's 1,983.0. The *lowest* season clears both the 2,100 bar and
the 2,146 career average.

**FIVE CAVEATS TRAVEL WITH THIS NUMBER. Read them before quoting it.**

1. **The backtest pays NO TRANSFER COSTS** — it re-decides freely every week. The template is
   measured identically, so the *gate* is a sound comparison, but **2,280 is not a real-season
   forecast** and must never be presented as one. E7's receding horizon is what makes it realistic.
2. **Three seasons, not four.** 2022-23 cannot be fitted at all: the `starts` label begins *in* it,
   so a walk-forward fit at its opening has nothing prior. Data availability, not season-picking.
3. **2025-26 GW1 ran with DC forced ineligible** (`dc_cold_start_gameweeks=[1]`), recorded in the
   results file itself.
4. **The objective is E[points]**, not the rank-aware objective blueprint §10's top-10% target
   needs. That is Phase 5; `collapse_to_expected_points` is the single seam where it changes.
5. **A stale rest gap on every double-gameweek second leg** — added `09-05`, found by S3's probe.
   `_build_team_fixture_gap` measured a DGW second leg's rest from the last *pre-round* fixture
   rather than from the first leg, which its own docstring says is the right answer (it is right
   in training, where the full frame is present, and wrong in backtest assembly, which sees
   history plus one placeholder row). 2025-26 round 33: Bournemouth's second leg read **11.31
   days where the truth is 4.21**; six of that round's 26 team-fixture pairs were affected and the
   twenty single-fixture pairs were exact. **Measured across the three gate seasons: 1,756 of
   86,765 player-fixture rows = 2.02%, confined to 13 rounds of 114.** It perturbs **one of
   minutes' thirteen features**, which feeds a PMF, which feeds a MILP — so the effect on a points
   total is smaller again than 2%. **Ruled `09-05`: caveat, not a re-run** (E6 is closed under a
   run-once-report-whatever ruling and 2,280.3 is a stated ceiling, not E7's bar). The direction is
   one-sided — today's value is always *larger* than the truth, so the model over-estimates
   freshness for DGW second legs, and DGW rounds are where hauls live. **S3 fixes it going
   forward**, which is why E7's own number will not carry this caveat.

**The 31 Aug ruling below is preserved because it was vindicated.** It refused to record E6 as
failed when the MILP lost to the template on a trailing-points proxy, arguing the signal was the
bottleneck rather than the algorithm. Same optimiser, same constraints, same solver — only the
distribution source changed — and the model stack beat that proxy by **273-359 points in every
season**. Recording a failure then would have been as wrong as recording a pass.

### The original ruling, 31 Aug (preserved)

**Architect ruling, 31 Aug.** Recording a failure here would be as inaccurate as recording a
pass. The gate has not yet been genuinely attempted.

The MILP backtest ran and produced this:

| Season | MILP | Template | Δ |
|---|---|---|---|
| 2020-21 | 1,989 | 1,895 | **+94** |
| 2021-22 | 2,107 | 2,060 | **+47** |
| 2022-23 | 1,992 | 2,078 | −86 |
| 2023-24 | 1,948 | 2,092 | −144 |
| 2024-25 | 2,033 | 1,906 | **+127** |
| 2025-26 | 1,945 | 1,951 | −6 |

Three wins, three losses. Across the three seasons with a human reference, MILP totals
**5,926 vs the template's 5,949** — behind by 23, never within 130 of the 2,146 career average.

**But the MILP was never fed the models.** It ran on a trailing-4-gameweek empirical
distribution of stored `total_points` — **the same signal `GreedyFormBaseline` uses** — stated
openly as an argued proxy. Fed identical input, MILP and greedy swap the lead season to season
by amounts that look like noise, and a *heuristic* beats a certified-optimal solve in two
seasons. **That is the signature of the signal being the bottleneck, not the algorithm.** The
backtest measured the optimiser using greedy's eyes; it did not measure the system.

### The keystone gap — the single most valuable thing to build next

`points.py::simulate_fixture_points_pmfs` requires `players: Sequence[PlayerFixtureFeatures]`
— **pre-assembled feature rows — and nothing in the codebase builds them.**

Hit three times independently before anyone connected it: the points story could not
sanity-check against 2026/27 and said so; the MILP story routed around it with a stated proxy;
the Architect confirmed it from the signature. **Seven trained, gated, walk-forward-validated
models cannot currently be invoked to produce a prediction for any gameweek, historical or
live.** Training assembles its own tables per model via `build_training_table`; **prediction
has no equivalent.**

**The swap is one call.** `MILPStrategy` moves `_trailing_points_distributions` →
`simulate_fixture_points_pmfs` and nothing else in the optimiser changes, because both satisfy
the same `PointsDistributionLike` Protocol. Build the pipeline, then re-run the gate honestly.
**Do not tune the optimiser against the test seasons** — that is how a backtest becomes a lie.

## 3.6 What comes next — Phase 4 opens here

**Everything in the old E6 critical path is done.** The migration, the `slow` marker, the MILP and
its honest gate all landed `09-01`…`09-03`. This section is now the Phase 4 starting point.

### Phase 4 IS E7 — decided `09-04`

**Blueprint order says E7 — receding horizon, transfers, hits.** That is also what makes the E6
number mean something, since the Phase 3 backtest re-decides weekly and pays **no transfer costs**.

**But the system still cannot advise on a real deadline.** Live prediction is blocked on one
registered story: `value` and `selected` are **NULL on all 610 FPL-API rows**, so `prev_gw_value`
and `prev_gw_selected_log1p` cannot be assembled for the current season and
`assemble_minutes_feature_row` will not run for an upcoming gameweek. The substitute exists and is
already captured every 30 minutes — `elements` carries `now_cost` and `selected_by_percent`,
resolvable as-of a deadline. It is a **(5)**.

**The user chose E7 `09-04`**, and chose to keep Track A manual in the meantime — `fpl-elite` plus
odds captures, decided after the pressers and before the deadline. That is a deliberate split:
E7 makes the backtest number honest; Track A keeps scoring points while it does.

**The live gap therefore stays open and stays dangerous** (§3.0). It is not merely "the engine is
unavailable" — the engine will answer a live gameweek with silent nonsense. Whoever picks it up
should land **the guard before the substitute**: raising on a coalesced-NULL feature is what
prevents a wrong answer reaching a real decision, and it is much smaller than the `elements`
wiring.

### E7 progress — S0…S10 and S3a closed. **S11 remains; the PHASE GATE FAILED.**

Story-by-story evidence lives in `PROGRESS.md`'s E7 section, not here. What a new session needs:

| Story | State | The one thing to know |
|---|---|---|
| S1 | done | Assembly is ~75% of a decide, **not** the Monte Carlo — do not touch `n_simulations` |
| S2 | done | `GameweekView.forward_fixtures` — schedule facts only, `value` deliberately excluded |
| S3 | done | `horizon_candidates(view)` → one candidate list per round `t..t+H` |
| S3a | done | Rollup hoisted out of the per-element loop. **80.6 → 5.6-7.7 ms/player** |
| S4/S5 | done | `TransferRules` per season. The FT bank really did change mid-window (2 → 5, 2024-25) |
| S6/S7 | done | `SquadState` threaded and **spent** — transfers, hits, selling-price haircut |
| S8 | done | `optimise_multi_period` — gameweek-indexed MILP, executes week `t` only |
| S9 | done, **an 8 not a 3** | `HorizonStrategy`. A held player whose club blanks falls out of the pool and the solve goes **`kInfeasible`** — the backfill is the story, not the wiring |
| S10 | done | The gate runner, and `max_rounds`: the myopic arm stopped assembling six rounds to use one. **5.96x, measured at the gate's own configuration** |
| **S11** | **open, a 3** | What-if engine. Small once S8 exists. **Not blocked by the failed phase gate** |

**Four facts that outlive their stories:**

1. **`horizon_candidates(view)[t]` is bit-identical to `decide_candidates(view)`** — element for
   element, including every PMF's full support. **S10 must share it, and the entire myopic arm's
   wall clock disappears.** Proven in S3, not projected.
2. **`hit_cost` is `-4`, NEGATIVE.** In a maximisation objective the penalty is `+ hit_cost * hits`.
   Subtracting it *pays* managers for taking hits — S7's registered gate text had this backwards,
   and it was caught before dispatch, not after.
3. **A 0.1m price rise is not realisable** (purchase 50, current 51 → profit 1, half 0.5, floor 0
   → sells at 50). Bank does not move monotonically with price, so any budget arithmetic must use
   `_sell_price`, never current price.
4. **The rest-gap defect was live on the round-`t` path too**, not only the horizon (§3.5's fifth
   caveat). Fixed going forward, so **E7's own number will not carry it**.

### S9, as built — and the four facts S10 inherits

**S9 shipped as an 8, not the registered 3, and the re-estimate is the useful part of the
record.** The wiring genuinely was a 3. Four read-only probes run *before* the brief was written
found that S9's registered gate — *a full season without raising* — was unreachable by the wiring
alone, for a reason three prior stories had not surfaced because all of them tested against
fixtures or stateless runs.

1. **The candidate pool's element set is not stable across rounds.** 2023-24 GW28->29 drops **494
   of 838** elements; only **131 of that season's 865** appear in every round. These are real
   blank gameweeks — all three seasons carry all 38 rounds.
2. **`optimise_multi_period` builds continuity only over round `t`'s pool**, so a held player
   whose club blanks has no `in`/`out` variable at all. It does not answer wrongly — 2024-25's
   GW34 solve returned **`kInfeasible`**, because seven unsellable holdings cannot fund seven
   replacements from a 14.1m bank.
3. **`validate_squad`'s flat budget check was a second, independent blocker.** It is a
   *stateless-mode* invariant; a squad genuinely held across a season appreciates (2025-26's
   reaches **96.0m of a 100.0m cap with zero transfers**). It is now `check_budget=`-gated and
   `SeasonReplay.run` passes `validate_budget=(state is None)`.
4. **The fix is one helper with two callers, deliberately.** `last_known_attributes` supplies both
   the backfilled candidate's price and the ledger's sale price, because two independent
   "last known price" computations is precisely the bug that would put the model and the ledger in
   disagreement. It reads `view.history`, which **is** `rows_before_round(t)` — an existing
   leakage boundary, not a new one.

**A suspected limitation, closed by measurement rather than carried as a caveat:** the backfill
attaches a vanished element's last-known *team*, which would overstate a player who *departed* the
league rather than blanked. Measured across all three seasons: **1,704 vanished elements, 100.0%
club blanks, zero departures.**

### S10 packet — what the gate runner needs

**Do not redesign the E7 gate** (the ruling is in this file's banner and `PROGRESS.md`'s E7
section). What S9 hands you:

| Piece | Where | Note |
|---|---|---|
| The strategy | `optimiser.HorizonStrategy(source, rules, transfer_rules, horizon, config)` | `horizon` counts rounds **inclusive of `t`** — `H=1` is the myopic arm |
| The real source | `optimiser.ModelStackStrategy` | satisfies `HorizonCandidateSource` unchanged; **this is the gate's source** |
| The cheap source | `optimiser.TrailingProxyHorizonSource` | harness only — fixture-blind. **Never a gate number** |
| The bootstrap | `replay.free_build_state(rules)` | pass as `SeasonReplay.run(strategy, initial_state=...)` |
| Season rules | `backtest.rules.transfer_rules_for_season` | **raises** outside the three sourced seasons — stateful mode is unavailable there, not degraded |

**The `k=0` sharing S1 flagged and S3 proved still holds**, with one clarification S9 added:
`horizon_candidates(view)[t]` is bit-identical to `decide_candidates(view)` **in stateless mode,
where that was proven**. In *stateful* mode round `t`'s list is a strict **superset** — it gains
the held-but-blank elements. Both arms receive the identical `GameweekView` and therefore the
identical backfill, so sharing between arms is unaffected; it is the comparison against the
single-period `decide_candidates` that no longer holds, and S10 does not use that path.

### What E6 left in place, and which E7 built on

Kept because it is still the load-bearing shape, not because it is news:

- `ModelStackStrategy` produces a `dict[element, PointsPMF]` per gameweek. E7 needs the same
  distributions across a **horizon**, not one week.
- `collapse_to_expected_points` is the **single named seam** where the objective changes. E7 does
  not change it (that is Phase 5's rank-aware job) but it will wrap it in a multi-period objective.
- **`build_squad` now raises on duplicate ids** `09-03` — that guard was put in specifically because
  E7's per-fixture candidate lists are where duplicates would first appear.
- The gate: **beat Phase 3.** Phase 3's own number is 2,280.3 mean, and E7 must beat it *while
  paying transfer costs Phase 3 did not* — so a naive comparison will look like a regression. Decide
  how that is measured **before** running it, or the gate will be unreadable the way E6's proxy run
  was.

### Sequenced follow-ups, none blocking

1. **(5) Live price/ownership gap** — as above. Highest value if the user picks usability.
2. **(2) `scripts/calibration_report.py` is unseeded** — rule 7. Bonus drifts in the 4th decimal
   between identical runs. Gate: two consecutive runs byte-identical apart from `as_of`.
3. **(3) Four test files run real-store tests with no `requires_real_store` guard** —
   `test_optimiser`, `test_backtest`, `test_scoring`, `test_store_invariants`. They *error* rather
   than skip on a clone without `data/store/`.
4. **Estimates are only in `PROGRESS.md`'s git history**, not the punch-card — punch-card the
   estimate pair at registration so calibration work stops under-counting its own data
   (`docs/retro/2026-09-04.md`).

## 4. Hard-won lessons — do not relearn these

**Added `09-06`, session `s008`.**

- **A gate that a story cannot reach is a grooming failure, and the probe is where you find it.**
  S9 was registered a 3 whose gate read "a full season without raising". Four read-only probes,
  run before the brief and costing minutes, showed the gate was unreachable — the multi-period
  solve goes `kInfeasible` at the first real blank gameweek. **The estimate moved 3 -> 8 before an
  agent saw the story, rather than after five rounds of a failing dispatch.** The tell was
  available to anyone who ran the data instead of reading the code: three prior stories had tested
  this path only against fixtures and stateless runs, and fixtures have a stable element set by
  construction.
- **Rehearse the design end to end in the scratchpad before writing the brief.** S9's probe 4 was
  a hand-rolled replay of the nine gameweeks around 2023-24's 494-element blank, running the exact
  design the brief was about to pin. It came back clean, with the backfill firing on 11 of 15
  holdings — so the brief could say "this is known to work, build it properly" instead of "try
  this". The pilot then surfaced no design errors, which is the outcome a rehearsal buys.
- **A suspected limitation is worth one probe before it becomes a permanent caveat.** The
  backfill attaches a vanished player's last-known club, which is wrong for a player who *left*
  the league. Rather than document that as a known weakness, it was measured: **1,704 vanished
  elements across three seasons, 100.0% club blanks, zero departures.** A caveat became a
  verified property for the cost of one query.
- **When a gate number will be misread, say so where the number lives.** S9's own gate shows H=1
  beating H=6 in two of three seasons — which is *guaranteed* by its fixture-blind proxy source,
  not discovered about horizons. That warning is written into `PROGRESS.md` beside the table and
  into this file's banner, because the E7 gate reuses the same words (H=1, H=6, three seasons) and
  a future reader will otherwise take the proxy's answer for the real one.

**Added `09-06`, session `s007`. Four of these are the Architect's own errors.**

- **A tractability probe that stubs the hard constraint measures the easy problem.** S8's
  pre-dispatch probe timed a multi-period MILP at 2.3 s by replacing the chained free-transfer
  state with one inequality and omitting the `in+out<=1` family. The built model measured **7.5x**
  that. Label such a number a **lower bound**, never a measurement.
- **A wall-clock number without the machine's concurrent load beside it is not evidence.** The same
  S8 figure was then re-measured at 17.2 s — with two full test suites running. Quiet-machine truth
  was 8.6-11.8 s. **Quote a range over repeated runs, or say explicitly it is one run.** This cost
  three separate corrections in one session.
- **Agent and Architect scratchpad paths must not share a namespace.** In S8 a coder redirected its
  full-suite output over the exact file the Architect's own run was writing. Caught only because the
  reported duration matched to the centisecond — which two independent hour-long runs never do —
  and the process table then showed the Architect's run still live. Nothing was corrupted, but **the
  Architect's verification had silently become a re-read of the agent's own claim**, which is
  precisely the RC4 failure the run-it-yourself rule exists to prevent. Give agents a `coder_`
  prefix and keep an `architect_readonly/` directory.
- **The checkpoint rule binds before EVERY dispatch**, including a small follow-up onto work an
  agent has just finished and you have already verified. S3a's 900-line implementation sat
  uncommitted while a follow-up agent edited the same file. *"It is already verified"* is exactly
  the reasoning that makes skipping the checkpoint feel safe.
- **A story's checkbox and its body are two hand-maintained records of one fact.** S6 carried
  `- [ ]` while its own body read DONE with the gate evidence pasted, and survived two subsequent
  stories that way. **Flip the marker in the same edit that writes the evidence**, and have the
  Scrum Master check marker-vs-body agreement at phase close — it is mechanically checkable.
- **An agent's temporary sabotage must be verified reverted by evidence, not by its word.** A coder
  replaced a guard with `if False:` to prove its test failed for the right reason — correct
  practice. The revert was confirmed by diffing against an Architect snapshot taken beforehand, plus
  a residue grep, not by accepting the report.
- **Do not extrapolate one layer's speedup to the layer above it.** S3a made feature assembly
  **10.5x** faster but candidate assembly only **2.8x**. An ms/player extrapolation would have
  claimed assembly was ~94% of `horizon_candidates`; it was not. Re-profile after any large win.

**Every one of these cost real debugging time. They are in the blueprint; this is the index.**

1. **Odds are conditional on selection (§3.3.1).** A market goal probability is
   `P(scores | plays)`. It carries *no* selection signal. This produced a wrong squad
   recommendation — a forward priced at 26.7% to score had been displaced by a club-record
   signing 16 days earlier. Always multiply by `P(start)`.

2. **An entity key must match the data's actual grain (§3.2).** Verify as *uniqueness within
   one observation batch*. Three bugs of this class: `picks` omitted `event` (would have
   destroyed the EO time series), `vaastav_player_gameweek_stats` omitted `fixture`
   (silently dropped 7,141 rows, **all on double gameweeks** — precisely where chips and
   hauls live).

3. **`as_of` returns state; `observations` returns the stream (§3.2).** Confusing them
   inflated a query 13x with no error.

4. **Identity is bitemporal (§12.5).** Resolve as of the season being processed, never
   today. Player and team identity are *asymmetric* — team codes persist across seasons so
   historical team resolution appears to work; that is a property of the id space, not
   evidence that as-of resolution is optional.

5. **A test that cannot fail is worse than no test.** The invariant written to catch bug #2
   used `pl.lit(1).count()`, which returns 1 per group regardless of size — it reported
   green over ten real violations. **Prove a new test fails before trusting that it passes.**

6. **Tests on fresh fixtures cannot catch production bugs.** Three store bugs all passed a
   full green suite. `tests/test_store_invariants.py` now runs against the real store.

7. **Score from stored `total_points`, never recompute (§7.2).** Scoring rules drift every
   season; re-derivation silently applies today's rules to old data.

8. **A test that asserts against a literal date or gameweek is a scheduled false alarm.**
   Three were found in session s004 alone: one hardcoded `--gw 1` that expired the moment GW1
   settled; one whose "unknown player" fixture name became resolvable when the resolver
   improved; and one whose read-back cutoff was `dt(2026, 8, 28)` — **it passed all day and
   failed at midnight, mid-session, between two full-suite runs, with no code change at all.**
   Derive bounds from the clock or from the store, never from a literal.

9. **A negative result can be the deliverable.** Blueprint §4 asserted "referee assignment is a
   real feature" from football priors for ten days; measured, it moves cards' log-loss by
   **+0.0002**. That killed a planned announcement-time capability. **Measure the size of the
   prize before building the plumbing** — and if a design claim has never been measured, say so
   rather than treating a plausible prior as evidence.

10. **Equal-width ECE under-reports a departure concentrated in a sparse high-probability
    tail.** Bonus's quantile ECE is materially larger than its equal-width ECE on every
    outcome. Report **both** binnings; a single-binning ECE is an incomplete measurement, and
    ECE alone must never arbitrate a calibration question (blueprint §7.1, amended 08-29).

---

11. **A leakage bug in an identity-bearing column gets worse with calendar time.** Most
    defects sit static until touched. This one compounds: `was_home`/`opponent_team` on
    FPL-sourced rows were derived from the *current* club, and in the nine days after GW1's
    deadline **8 elements changed clubs, 5 of them producing concretely wrong stored rows** —
    one naming the player's *own club* as the opponent. The transfer window was still open, so
    every further day added corruption to stored rows *and* to every future re-ingest. Audit
    identity-derived columns **while the window is open**, not after.

12. **"Unresolved name" and "stale identity" are the same phenomenon seen from two
    subsystems.** The odds feed flagged `Ethan Pinnock` as unresolvable and it was logged as a
    resolver gap. It was not: he had transferred to a non-PL club. During an open window,
    before opening a resolver investigation, **check whether the player simply left**.

13. **An offset that is a whole multiple of 24h inherits the deadline's local time-of-day.**
    "Hours before deadline" and "a civilised hour locally" are not independent choices. Capture
    offsets of T-72h/T-24h against FPL's standard 01:30-local deadline land at 01:00 local —
    inside the documented machine-off window, so the two most valuable captures were the two
    most likely to never fire. Breaking the symmetry (T-78h/T-26h) fixes it. **Compute where a
    schedule actually lands in the user's clock before believing it is civilised.**

14. **The severity of a shared-formula bug scales with the ratio of penalty to loss
    magnitude.** The unscaled-L2 bug was material in five models carrying near-unit-scale
    log-likelihoods and **immaterial in `bonus`** (<0.2% on every metric), which fits a ridge
    on BPS residuals whose variance runs ~20-80. Before assuming a shared bug bites everywhere
    it appears, **compare the scales** — and report the negative where it does not.

15. **Write findings to the punch-card as they are produced, not at punch-out.** Two session
    limits killed agents mid-flight this session. The one that had been writing incrementally
    lost compute but **no knowledge** — its complete measurement grid was recovered from the log
    without its context. Use a **quoted heredoc** (`cat >> … <<'EOF'`), never `printf`: a `%` in
    a message body corrupted line 127 of `s005.jsonl`. The log is append-only, so a bad entry is
    **superseded, never edited**.

## 5. Decisions already made — do not re-litigate

- **Rank-aware objective**, target **top 10% overall**, moderate aggression. User-confirmed,
  and **re-confirmed 2026-08-29** when the wording "above average, not the typical 50th
  percentile" came up and could have been read as a downgrade. It is not. **Top 10% is the
  goal; comfortably-above-average is the floor the system must not fall below.** The user's
  own view is that top 10% "seems far from reality" given the nature of the game — that is
  realism about the odds, not a revised target, and the aggression parameter stays set for
  top 10%. Do not quietly re-tune it toward the floor.
- **Architect authority, granted 2026-08-29.** The Architect may **amend the blueprint** when a
  better design is found — always with an amendments-table entry saying what changed and why,
  and flagged to the user in the same message, never landing silently. The Architect may also
  **seek alternative APIs** when a data gap is genuinely blocking, bringing licensing and cost
  to the user before anything is adopted. **The standing "no purchases" decision holds** until
  the user says otherwise. The user also asked explicitly for continued proactive defect
  detection and database health — the PL kickoff timezone bug (§3) is the kind of thing meant.
- **User time budget ~20 min on deadline day** → the weekly loop must be automated, and a
  **generated decision brief ships with Phase 3**, not at Phase 8 with the UI.
- **No purchases.** footballdata.io rejected (no per-player-per-match data at any tier; EPL
  is on its *free* plan). API-Football's $19 month deferred until Phase 2 can consume it.
- **PL API used under §3.6 conditions** — private/personal only, never commercialised, never
  redistributed, 2 req/s. **Keep the P1 source pluggable**; if the project ever goes public,
  this decision expires.
- **Odds are a live-only overlay** — free tier has no history, so their value can never be
  backtested, only forward-tested. Do not make them load-bearing.
- **Squad structure: no standing prior** ("let the optimiser decide"). Interim rule until
  Phase 3: **every XI slot must clear `P(start) >= 0.75`; enablers on the bench only.**

---

## 6. Questions the user has NOT answered

From `docs/retro/2026-08-19.md` §2. The Scrum Master surfaces these **at phase close** — its
scope narrowed to post-phase audit on `09-01`, so between phases this list is the Architect's to
carry. Unanswered questions become silent assumptions either way.

- ~~**Understat**~~ — **ANSWERED 2026-08-21 by the user.** Understat never had a REST API;
  every "API" is an unofficial package wrapping page scraping, on top of the
  `robots.txt: Disallow: /` question and the restructure that broke every known client
  (`docs/wiki/data-sources.md` §4.1). **Verdict: stays dropped, but is retained as a
  documented last resort** — reachable only if no cleaner source closes a *historical* gap.
  The gaps that could ever trigger it are named in §3: 2019-20 (29/38 GWs, no
  `position`/`team` upstream), 2022-23 GW7, and 2024-25 olbauday degradation. Nothing may
  reach for Understat without first showing which of those it closes and that olbauday,
  vaastav and the PL API cannot.
- **Paid data** — user offered to buy "a better API"; the spec is
  `docs/wiki/data-requirements.md`. Nothing bought yet, deliberately.

---

## 7. Live season — Track A

**Entry `3434577`, "Kokdiang FC".** Career average 24th percentile (2,146).

**GW1 result: 43 points against a 50 average.** OR **6,382,778**, percentile rank 75, 1 point
on the bench, no transfers. The captain call was right on the ownership logic — the damage was
elsewhere in the squad.

**GW2 squad** after the one free transfer (**Gibbs-White → Rogers**, applied by the user
27 Aug, £0.5m banked). Note three clubs differ from the GW1 intel file, which is stale on them:
**Van Hecke and Dubravka are Spurs, Tzolis is Arsenal, Kusi-Asare is Fulham.**

> **GK** Lammens (MUN) · Dubravka (TOT) — **DEF** Gabriel (ARS) · Shaw (MUN) · Van Hecke (TOT)
> · Diop (IPS) · van Ewijk (COV) — **MID** Szoboszlai (LIV) · Tzolis (ARS) · **Rogers (CHE)**
> · B.Fernandes (MUN) · Hughes (CRY) — **FWD** João Pedro (CHE) · Haaland (MCI) ·
> Kusi-Asare (FUL)

Captain **Haaland**, vice **B.Fernandes**. Bench order: **van Ewijk first** — the only bench
player who actually starts.

### Why the transfer was decidable without the presser

Gibbs-White was FPL-flagged (`status=d`, "Knee injury — 75% chance of playing") and
`fpl-elite` read P(start) at 0.60-0.70, failing the interim ≥0.75 XI rule. **But the fitness
was the weaker half of the case.** Even fully fit he was away at Anfield with Forest at
**16%** to win, in a side with no goals in two competitive games, and fourth in their penalty
order. Deciding on fixture rather than fitness meant the decision did not depend on Forest's
Friday presser — which lands in the user's overnight, possibly after the deadline. **This is
the shape every deadline decision should take**: a rule that resolves before the user sleeps
beats a correct rule that needs a 2am check.

### Market numbers, GW2 capture 27 Aug 21:39 local

Devigged match odds, median across books:

```
MUN v IPS   MUN 67%    LIV v NFO   LIV 62%    AVL v ARS   ARS 62%
CRY v MCI   MCI 57%    CHE v BHA   CHE 51%    TOT v NEW   TOT 42%
```

Anytime goalscorer — **raw implied, vig NOT removed, only 2 UK books**; ordering is solid,
levels are inflated. Second column applies P(start) per lesson 1:

```
                P(score|plays)   P(start)    unconditional
Haaland             59.2%          0.95         ~56%
João Pedro          47.5%          0.90         ~43%
Tzolis              36.4%          0.78         ~28%
B.Fernandes         34.2%          0.95         ~32%
Gibbs-White         30.8%       0.60-0.70       ~20%      <- transferred out
```

Captaincy stayed with Haaland on a 56% vs 32% unconditional gap, which the fixture edge
(United 67% vs City 57%) does not overturn, and Fernandes' profile is assist-weighted — the
worse captaincy bet at equal xGI. **The lineup lands ~90 minutes after the deadline** (CRY v
MCI kicks off 03:00 Sat local / 19:00 UTC Fri), so the armband is placed blind, and FPL's vice
rule is minutes-based: it only inherits if the captain plays *zero* minutes.

**Watch, do not act:** Tzolis at ~0.78 is borderline on the start rule but has the squad's
second-best goal probability. Van Hecke has a rotation vector that did not exist in GW1 — van
de Ven is fit again and Van Hecke started the midweek cup tie.

Full intel, thirteen could-not-confirms, and two sources discarded outright:
`docs/wiki/gw2-2026-27-intel.md`.

**Track A output is never presented as system output.**

## 8. Conventions worth knowing

- **Punch-card** `.punchcard/s001.jsonl` — 161 events, five agents. Append-only, `>>` only.
  The Architect completes missing entries; six were recovered from a mangled filename this
  session.
- **Agents cannot append if their tool list lacks Bash** — Elite hit this and correctly
  wrote a sibling file rather than rewriting the log. Check the roster's `tools:` before
  requiring an append.
- **Line endings**: Git warns LF→CRLF constantly on this Windows checkout. Cosmetic. A
  `.gitattributes` would silence it.
- **The user is UTC+8 (Asia/Kuala_Lumpur). Always give times in BOTH zones.** A UTC-only
  instruction ("capture at 16:30-17:00 UTC") was 00:30-01:00 local — the middle of the night —
  so it was run immediately instead, 19 minutes after the previous capture, and the movement
  signal was lost. FPL deadlines are UK time; **17:30 UTC = 01:30 next-day local**, which means
  most deadline-adjacent work is overnight for the user and should be **scheduled, not manual**.
- **Scripts run under `uv`, never bare `python`.** `uv run python scripts/<name>.py` or
  `.venv/Scripts/python.exe scripts/<name>.py`. Bare `python` picks up the system interpreter
  and fails on `ModuleNotFoundError: polars`.
- **Never commit `data/` or `cache/`.** Gitignored. `data/store/` holds unrecoverable
  ownership captures.
