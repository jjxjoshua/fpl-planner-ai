# Progress

**Sprint = Phase 4 (E7).** A sprint runs until its phase gate passes, not until a date.
**Phase 3 (E6) gate: beat the template over 2+ backtested seasons (~2,100; 2,146 ideal) —
PASSED `09-03`, scrum-master audit PHASE DONE `09-03`.** Mean 2,280.3 vs template 1,983.0 across
three backtested seasons (2022-23 excluded, unfittable — `starts` label begins in it, not
season-picking). Full result table and caveats in the E6 section below; audit at
`docs/retro/2026-09-03.md`, sprint-close report at `docs/retro/2026-09-04.md`.

Phase 0 done · Phase 1 gate PASSED · E2b COMPLETE · Phase 2 (E5) COMPLETE, gate PASSED `08-29`,
blocking condition CLOSED `08-29` · **Phase 3 (E6) COMPLETE, gate PASSED `09-03`** ·
**Phase 4 (E7) OPEN — groomed `09-04`, 11 stories, gate decided. S9 DONE `09-06`; S10 and S11 remain.** Sprint total re-estimated 46 -> 51: S9 was registered a 3 and re-estimated an 8 before dispatch, on probes showing its registered gate was unreachable by the wiring alone (see its entry in the E7 section).

**This file is the Architect's.** Registered before dispatch, checkpointed at pilot review, closed
on pasted evidence (`docs/wiki/dispatch-protocol.md` rules 9 and 11). The Scrum Master is summoned
only when the phase gate is claimed passed, and may withhold the phase-done mark. **Collapsed at
Phase 4's open `09-04`** — closed epics reduced to one line per story per the anti-bloat mandate;
the evidence itself still lives in `docs/wiki/**`, `docs/retro/**` and `.punchcard/s006.jsonl`, not
here. 94 story lines before the collapse, 16 open (`[ ]`/`[~]`/`[!]`) and 78 closed/not-started;
the collapse touched only the 71 `[x]` lines — see `docs/retro/2026-09-04.md` for the before/after
count.

**Start here (next Architect):** E6 is closed, and `docs/wiki/optimiser.md` §0/§4 were already
corrected `09-03` (both the headline gate status and the "one call" mechanism claim) — that finding
from the 09-03 audit is resolved, not still open. `docs/HANDOFF.md` is being rewritten for Phase 4
in parallel with this collapse; do not treat its old banner as current until that lands. Read
`docs/wiki/dispatch-protocol.md` before writing any brief for Phase 4. **E7 was groomed `09-04` —
11 stories, 46 points, and the gate is DECIDED: H=6 beats H=1 under identical transfer rules, NOT
"beat 2,280.3" (see the E7 section).** `docs/retro/2026-09-04.md` has the sprint's first estimate-vs-actual
calibration data now that Fibonacci estimates exist — read it before re-grooming anything at a
similar anchor.

**Standing, not this session's to close:** GW3 decision — deadline **01:30 Sat 5 Sep local
(04 Sep 17:30 UTC)**, so the call lands Friday evening. **All three user-blocked items are now CLOSED `08-31`:**
`THE_ODDS_API_KEY` rotated (user-confirmed), `snapshot_odds` task registered and heartbeating,
and `WakeToRun` set **`True`** on both tasks — read live from `Get-ScheduledTask`, not inferred.
The "still `False`, unresolved for a third session" claim carried here was simply wrong; nobody
had queried the live task. The snapshot gaps are a **user-initiated power-off** (System event
1074), which `WakeToRun` cannot help with — it wakes a sleeping machine, never one that is off.

Suite: **1,373 tests, green at HEAD** (`1373 passed in 1823.79s`, 30m23s, run `09-03` to close the audit's one substantive gap). **Fast loop: `1284 passed, 66
deselected in 42s`** via `-m "not slow"` — use it while developing; a bare `pytest` still runs
everything, deliberately.

See [docs/HANDOFF.md](docs/HANDOFF.md) for session state and deadlines.

`[x]` done · `[~]` in progress · `[ ]` open · `[!]` blocked / waiting on someone · `[·]` not started, not broken down

**Estimates in parentheses, Fibonacci: `- [ ] (5) story`.** Compound difficulty — scope plus
uncertainty plus blast radius — not hours. **1-8 is dispatchable; 13 and 21 are not tasks** and
must be split before any agent sees them. Scale anchors: `docs/wiki/dispatch-protocol.md` rule 11.

Technical breakdown happens at pickup time, not here. Keep this file short.

**Every story is written here three times** (`docs/wiki/dispatch-protocol.md` rule 9):
**registered** before the dispatch (story, owned paths, gate), **checkpointed** `[~]` at the
pilot review, **closed** `[x]` only with the pasted gate result. A story that was never
registered cannot be closed. This exists because the seven-model migration below ran to
completion while this file still listed it as open *and prescribed an approach that had been
rejected* — the next session would have been actively misdirected by its own tracker.

---

## E1 · Governance

- [x] Blueprint ratified `08-19`
- [x] Agent protocol + punch-card convention `08-19`
- [x] Progress tracker `08-19`
- [x] Scrum Master agent + retro cadence `08-19`
- [x] Rank target confirmed: top 10%, moderate `08-19`
- [x] Data requirements spec for provider selection `08-19`
- [x] Blueprint amendment — §12.5 re-fetchability exception for non-re-fetchable sources `08-21`
- [x] Blueprint amendment — §12.2 derived-capability guarantee, store-enforced separation `08-21`
- [x] Blueprint amendment — §7.1, ECE/slope/intercept required, base-rate-only cannot pass `08-29`

## E2 · Data spine — *Phase 0*

- [x] Source recon: FPL API, archives, API-Football, odds `08-19`
- [x] Odds verified: EPL h2h/totals (42 books) + goalscorer props `08-19`
- [x] Thin FPL client — 2 req/s, jitter, backoff, cache `08-19`
- [x] Bitemporal store + `as_of()` leakage primitive `08-19`
- [x] `snapshot_bootstrap` live and scheduled every 30 min `08-19`
- [x] Verify real `picks/` payload shape `08-22`
- [x] 503 maintenance state + cached-404 frame bias, both closed `08-22`
- [x] `automatic_subs` entity key verified unique on 6,719 real rows `08-25`
- [x] First EO sample `08-25` — 9,284 entries, 139,260 picks rows
- [x] GW2 EO sample `09-02` — 144,795 picks rows, unbackfillable dataset has no hole
- [x] DC thresholds pinned from `event/1/live` `08-27` — DEF_CBIT 10, MID_FWD_CBIRT 12
- [x] Historical backfill — vaastav, 7 seasons, 172,819 GW rows `08-21`
- [ ] Backfill gaps: 2019-20 has 29/38 GWs, 2022-23 missing GW7
- [x] `vaastav_team_identity` backfilled `08-22`
- [ ] olbauday shot-level backfill, 2024-25+ *(deferred as story 10c)*
- [x] Provider evaluation + P2 route check `08-19`
- [x] PL API ToU decision — proceed, private use, conditions in §3.6 `08-19`
- [x] footballdata.io evaluated — rejected, no per-player-per-match `08-19`
- [x] Heartbeat dataset — `job.heartbeat@run`, verified in production `08-22`
- [ ] Heartbeat not yet wired into `snapshot_odds.py` or `sample_picks.py`
- [x] `THE_ODDS_API_KEY` rotated by the user `08-29` — verified live: quota is account-level, not
  key-level; rotation does not reset the monthly budget
- [x] `fpl-ai snapshot_odds` scheduled task registered `08-29`, verified running unattended in
  production `08-30` — hourly, deadline-gated, zero credits on no-op runs, heartbeat healthy
- [x] Store invariants against the real store — 7 invariants `08-21`
- [x] Backtest replay harness `08-21`
- [x] `store.as_of()` wrong primitive for bulk-ingested archives — CLOSED `08-22`
- **Gate:** reconstruct any past deadline's exact state, verified against known outcomes

## E2b · Provider framework — *Phase 0, blueprint §12*

- [x] **1–12**, all stories — E2b COMPLETE `08-21`, zero core-provider changes
- [x] Odds player-name resolution `08-27` — third resolver rung + eszett folding, 17/26 newly
  resolved, zero regressions
- [ ] No GrainPlan for the two olbauday capabilities — needs a design call
- [ ] 2024-25 olbauday degraded — different layout, no in-archive deadline source
- **Gate:** met — E2b COMPLETE, all 12 stories `08-21`

## E3 · Track A — live season *(runs all season, parallel to everything)*

- [x] GW1 squad review + final call, captain Haaland `08-21`
- [~] Weekly cadence: Thu/Fri review → deadline → post-GW EO sample
- [x] GW1 result `08-25` — **43 pts vs a 50 average**, OR 6,382,778, percentile 75
- [x] GW2 transfer `08-27` — Gibbs-White → Rogers, decided on fixture not fitness. Captain
  Haaland, vice B.Fernandes
- [ ] GW3 loop — deadline **01:30 Sat 5 Sep local (04 Sep 17:30 UTC)**. GW4 unusually early:
  **20:30 Sat 12 Sep local** (12 Sep 12:30 UTC), not the usual 01:30 — note for scheduling
- [·] Automated weekly decision brief — required by the 20-min budget. Was slated to "land with
  E6"; E6 closed `09-03` without it shipping. **ANSWERED `09-04`, the retro's open question 2:
  it did not ship and was not absorbed into another story** — `grep -rln "decision brief|
  weekly_brief|decision_brief" scripts/ src/ docs/wiki/` returns nothing. It was missed at grooming
  time, not deliberately dropped. **Not groomed into the E7 sprint** — it depends on the live-season
  guard and substitute (E6, above), and is worth re-estimating once those close

## E4 · Baselines — *Phase 1*

- [x] Random / template / greedy-form over 6 seasons `08-21`
- [x] Backtest replay harness with structural leakage boundary `08-21`
- [x] **GATE RE-OPENED then CLOSED `08-22`** — `greedy_form` non-determinism fixed at both the
  store's ordering and the hill-climb's tie-break. Verified closed: 4 runs bit-identical
- **Gate:** **PASSED (restated `08-22`)** — Random 497-1,008 · Template 1,895-2,092 · Greedy
  1,889-2,122 · human 2,019/2,251/2,169. **No baseline beats the user.**

## E5 · Core models — *Phase 2, COMPLETE*

Seven models built and gated: team strength, minutes, defensive contribution, attacking
involvement, bonus, cards/discipline, GK saves (7th, added post-gate as a measured Phase 3
prerequisite — does not reopen E5). Full detail in `docs/wiki/model-*.md` and
`docs/wiki/calibration-report.md`; kept short here by design.

- [x] Team strength (Dixon-Coles) `08-21` · Minutes + isotonic recal `08-22` · DC estimator
  `08-22`, thresholds pinned + wired `08-27` · Attacking involvement `08-28` · Bonus (BPS) `08-28`
  · Cards/discipline `08-29` · GK saves `08-30` — NB2 chosen on measurement (`docs/wiki/model-*.md`)
- [x] Calibration report + amended §7.1 gate `08-29`, regenerated `08-30` — **E5 GATE PASSED**: 20
  outcomes across 7 models, 3 PASS / 15 PASS-with-accepted-departure / 2 BASE-RATE-ONLY / 0 FAIL
  (`docs/wiki/calibration-report.md`)
- [x] Cards NONE/YELLOW isotonic layer — E5's one blocking condition on Phase 3, CLOSED `08-29`
  (`docs/wiki/calibration-report.md`)
- [x] L2 penalty-scaling bug fixed in all seven models `08-30` — root cause was scale of `n`, not
  feature scales; no default changed, no gate verdict moved
- [x] Isotonic saturation bug fixed (minutes/cards/saves) `08-30` — Jeffreys `Beta(0.5,0.5)`
  correction, minutes slope 0.896→0.963
- [x] Calibrator consolidated into `src/fplai/calibration.py` `08-30` — bit-identical before/after
- [x] Calibration report regenerated `08-30` (06:57Z run) — was stale four ways; E5 still PASSES
- [x] Calibration report regenerated + diffed `09-03` (audit follow-up, not overwritten) — 52-line
  diff benign, every fold/eval-row count identical, no verdict moved (`docs/retro/2026-09-03.md`)
- [x] Seven-model migration onto the capability reader `09-01` — cost 5 dispatch rounds, 4 damaged
  files, ~553k subagent tokens; root-caused in `docs/wiki/dispatch-protocol.md`, retro at
  `docs/retro/2026-09-01.md`
- [ ] (2) **`scripts/calibration_report.py` is UNSEEDED — a rule-7 gap, found by that diff.** Zero
  references to a seed in the script (grepped), so its numbers are not bit-reproducible: bonus's PMF
  comes from a Monte Carlo over the whole fixture, and that is what the fourth-decimal drift is.
  Magnitude negligible, no verdict affected — but rule 7 requires reproducibility from a commit hash
  **plus a seed**, and a gate report that shifts between identical runs cannot be cited as strict
  evidence. **Gate:** two consecutive runs byte-identical apart from the `as_of` line
- [ ] Two related-but-separate calibrators still exist by design (walk-forward gate's 2-class,
  the deployed 3-class model's) — escalated, not unified
- [ ] `walk_forward_validate`'s `calibrate` default still differs across siblings — `True`
  cards, `False` minutes
- [ ] DC trailing-window length serves two purposes (current form vs stable style) and wants
  different lengths for each; DC is team-style not player-skill (Anderson→Man City,
  Senesi→Spurs both invert the naive per-90 ranking)
- [ ] **Third duplication surface, named not acted on** — `_log_loss`/`_brier`/
  `_calibration_slope_intercept` duplicated 10x across `attacking.py`, `bonus.py`,
  `defensive_contribution.py`, `minutes.py`. Two duplicated-formula bugs already landed this
  session from exactly this shape
- [ ] `pl_match_fixtures.matchweek` ≠ FPL's `round` on 4.4% of matches — gates all 832,890
  already-ingested rows of `pl_team_match_stats`
- [~] **`slow` pytest marker** — raised here, **groomed and sequenced as E6's first story `09-01`**
  (3 → 5). Tracked once, in E6; this line is the origin, not a second open item
- [ ] (3) **Four test files run real-store tests with no `requires_real_store` guard** — surfaced
  by the slow-marker pilot `09-01`: `test_optimiser.py`, `test_backtest.py`, `test_scoring.py`,
  `test_store_invariants.py` (0 guards, 4/4/3/1 real-store references). On a clone without
  `data/store/` these **error rather than skip**. `test_store_invariants` may be deliberate —
  CLAUDE.md says it runs against the real store on purpose — so this is *assess then fix*, not a
  blanket sweep. **Gate:** with `data/store/` moved aside, the suite skips rather than errors
- **Gate:** **PASSED `08-29`**, blocking condition **CLOSED `08-29`**

## E6 · Optimiser — *Phase 3, COMPLETE, gate PASSED `09-03`*

**Phase summary (scrum-master, `09-03`):** gate condition — beat the template over 2+
backtested seasons — met on three seasons (2023-24, 2024-25, 2025-26; 2022-23 excluded,
unfittable). model_stack mean **2,280.3** vs template **1,983.0**, lowest season 2,221 clears
both the ~2,100 bar and the 2,146 career average. Seed 0, commit `495a168`, 152 min, results at
`data/gate/e6_gate_results.jsonl` (gitignored — table below is the durable record). Caveats
that travel with the number: re-decided weekly with no transfer costs (not a season forecast);
three seasons not four; 2025-26 GW1 DC forced ineligible (`dc_cold_start_gameweeks=[1]`);
objective is E[points], not the rank-aware objective Phase 5 needs. **FIFTH CAVEAT, added `09-05`
by S3's probe:** the run carried a stale rest gap on every double-gameweek second leg — measured
at **1,756 of 86,765 player-fixture rows (2.02%)**, confined to **13 rounds of 114** (2023-24 983
rows / 3.31%, 2024-25 364 / 1.33%, 2025-26 409 / 1.37%). It perturbs **one of minutes' thirteen
features**, which feeds a PMF, which feeds a MILP, so the effect on a points total is smaller
again than 2%. **Ruled `09-05`: caveat it, do not re-run** — E6 is closed under a
run-once-report-whatever ruling, and 2,280.3 is already a stated ceiling rather than E7's bar.
The direction is one-sided: today's value is always *larger* than the truth, so the minutes model
over-estimates freshness for DGW second legs — and DGW rounds are where hauls live. S3 fixes it
going forward. A real future-informs-past
DC leak in the gate runner's cold-start fallback was found and closed *before* the recorded run
(measured 90→85 behavioural change, unreachability attacked). Full audit, open questions and
stale-knowledge findings: `docs/retro/2026-09-03.md`.

- [x] `src/fplai/scoring.py` — outcomes→points from live `game_config` `08-29`, 610/610 on real
  GW1, 32/32 GW2 (`docs/wiki/scoring-validation.md`)
- [x] `src/fplai/points.py` — fixture-level seeded Monte Carlo, emits a PMF (rule 5) `08-30`
- [x] `highspy>=1.15.1` added `08-30`
- [x] `src/fplai/optimiser.py` — single-period MILP, 20 tests, `scripts/run_optimiser.py` `08-30`
  (`docs/wiki/optimiser.md`)
- [x] Live gameweek ingest `08-30` — FPL as a 2nd provider of `gameweek_stats`, 180,560 rows; DGW
  path degraded/unverified at the time, closed by the DGW story below
- [x] `position`/`team` join at ingest time `08-30` — 600/610, the 10 unresolved is correct
  bitemporal behaviour (elements added post-GW1-deadline)
- [x] `was_home`/`opponent_team` leak found live and fixed `08-30` — 8 elements changed clubs in
  the 9 days after GW1's deadline, 5 rows wrong, store re-ingested
- [x] ~~E6's gate was run but NOT genuinely attempted~~ SUPERSEDED `09-03` — honest re-run on the
  model stack PASSED 3/3, see the gate result below
- [x] (3 → **5**) `slow` pytest marker `09-01` — fast loop 23m01s → `38.38s`; re-estimated on a
  `--durations=40` probe (`docs/wiki/dispatch-protocol.md`)
- [x] (5) Feature-row contract + one worked path `09-01` — `src/fplai/features.py`, first code in
  the project that can feed a trained model at prediction time; leakage attacked, not just tested
  (`.punchcard/s006.jsonl:75-83`)
- [x] (8 → **~16**, across 4 dispatches) Assemble features for all seven models `09-02` — column
  counts: attacking 15, DC 9, cards 8, bonus 9, saves 7; each model's shape probed separately after
  the first two proved not to mirror each other (`.punchcard/s006.jsonl:88-108`)
- [x] ~~(3) Scoreline input from team_strength~~ **NOT NEEDED `09-02`** — `predict_scoreline`
  already existed as a public entry point, found by a 30-second probe rather than built
- [x] (3) Fixture-level composition entry point `09-02` — `assemble_fixture_player_features`;
  the end-to-end test's open-coded loop was *replaced* by a call to it, not paralleled
- [x] (5) Historical `as_of`-correct assembly `09-03` — **ALREADY SATISFIED**, closed on 5 existing
  attack tests rather than built
**MILP swap RE-GROOMED `09-03`, 3 → ~13** — not a story of its own, a re-estimate: two probes (not
a code read) found `MILPStrategy` has no store and `GameweekView` couldn't identify a player's
fixture; user ruling preserved the structural leakage guarantee over a deadline-bounded store.
The four items below are what it actually cost (`docs/retro/2026-09-03.md`).
- [x] (5) Extend the leakage boundary to carry schedule facts `09-03` — `ATTRIBUTE_COLUMNS` gains
  fixture/was_home/kickoff_time/opponent_team; also fixed an unsorted DGW dedup non-determinism
  found in passing (`.punchcard/s006.jsonl:139`)
- [x] (5) Double-gameweek handling `09-03` — `GameweekView.fixtures` +
  `combine_gameweek_points_pmfs` convolution; DGWs measured at 3.01% of player-gameweeks,
  concentrated in 4 rounds/season; design avoids `build_squad`'s duplicate-id trap
  (`docs/wiki/optimiser.md`)
- [x] (2) `build_squad` raises on duplicate element ids `09-03` — probed first: `selected` dict
  silently overwrote while `chosen`/`club_count` still incremented; fixed with a guard at function
  entry, before position bucketing
- [x] (5) Frame-based feature assembly `09-03` — seven `*_from_frame` entries; equivalence by
  construction via a shared private helper, not by a parallel implementation
- [x] (3) Model-stack strategy `09-03` — `ModelStackStrategy`, purely additive to `optimiser.py`
  (zero removed lines); DC neutralised pre-2025-26; measured cost 49.8s/gameweek, over the brief's
  30s ceiling, run as-is per user ruling
- [x] (2) Scoring-config gap MEASURED AWAY then PINNED `09-03` — current live config reproduces
  stored `total_points` 163,671/163,672 exact; standing test + `docs/wiki/scoring-validation.md`

- [x] (2 → **3**) **S0 · Live-season coalesced-NULL GUARD — COMPLETE `09-05`** (user
  ruling). Raise when a required feature resolves to a coalesced NULL for a live season, instead
  of answering £0.0m. This is the half of the gap below that stops a wrong answer reaching a real
  decision (`docs/HANDOFF.md` §3.0: both live paths are wrong and **neither raises**), and it
  lands **before** the `elements` substitute. **Gate:** an attack — assembling a live upcoming
  fixture raises, and the same call on a historical fixture is unchanged
  - **PILOT REVIEWED `09-05`, semantics ACCEPTED.** Architect re-ran both gates independently:
    `54 passed, 13 deselected in 8.09s` and `integrity ok -- 2 file(s) checked` (0 mojibake, 0
    removed defs), diff purely additive (97 insertions, 0 deletions). Attacked with the original
    probe as the before-picture: the live fixture now raises `FeatureAssemblyError` naming
    `['value','selected']` on **both** `allow_live_season` settings; `allow_coalesced_live_nulls=True`
    reproduces the old wrong row exactly; the historical fixture assembles unchanged with a real
    `prev_gw_value=40.0`. **Re-estimated 2 → 3** on what the pilot found.
  - **Two defects found at pilot review, both now closed; replication measured away:**
  - [x] (2) **S0-a · Guard read cost — DONE `09-05`, 28x.** The guard called the *union* reader
    (vaastav + FPL) and then filtered to `fpl_api`; it now reads `FPL_API_DATASET` directly via
    `store.effective_at`, and the `source_provider` filter is dropped as redundant. One import line
    plus one function body. **Measured clean, quiet machine, no strays:** guard read **2.201s →
    0.079s**; 600-player live-gameweek guard overhead **1,465.2s → 66.8s**; end-to-end historical
    assemble 2.238s. **Gates, all Architect-run, each alone with its own exit status:**
    `54 passed, 13 deselected in 8.36s` · `integrity ok -- 1 file(s) checked` ·
    **`13 passed, 54 deselected in 333.03s`** (full `-m slow` partition — the coder had honestly
    reported this NOT RUN, then ran only the `-k real_store` subset; the Architect's is stricter and
    is the citable one). Behaviour bit-identical: raises on both `allow_live_season` settings,
    escape hatch reproduces the old row, historical fixture unchanged at `prev_gw_value=40.0`.
    **No cache added** — pinned decision 2: a memo keyed on `(as_of, season)` goes stale the moment
    an ingest lands, and a guard is the last place that should answer stale. The residual 0.079s
    belongs to S3's per-decision hoist
  - [x] (3) **S0-b · Guard bypass CLOSED `09-05`.** The fix was *not* threading `source_provider`
    into the frame path (the Architect's first instinct, wrong — it would have widened
    `backtest/data.py`, which S2/S6 are about to rewrite). Instead the check was **reformulated to be
    provenance-free**: *"rows exist for this `(season, element)` at `round < target`, and
    `value`/`selected` is NULL in every one of them"* — checkable on any frame, no store and no
    `source_provider` needed. Added to the shared private tail, so both entry points are covered;
    the store-side check is **kept**, because with `allow_live_season=False` the live rows are
    dropped before the tail sees them and only the independent read catches that (§3.0's *both*
    paths). The bypass test was converted into its opposite rather than deleted.
    **Gates, Architect-run:** `56 passed, 14 deselected in 7.09s` · **`14 passed, 56 deselected in
    325.04s`** (full `-m slow`) · integrity flags exactly one removed def, the deliberately renamed
    bypass test, clean under `--allow-removed`.
    - **E6 gate proven safe, stronger than claimed:** across all 179,960 vaastav rows, `value` and
      `selected` have **0 NULLs** — not merely no all-NULL groups. The frame check cannot fire on
      history, so 2,280.3 is not at risk.
    - **Hot-path cost measured** (a question the brief failed to ask): the check runs per player per
      fixture per horizon step, so it could have re-added on the gate path what S0-a removed on the
      live one. Guard alone **1.3ms mean** against a **13.0ms** minutes assemble → **0.00h** over the
      whole E7 gate. (13ms reconciles with S1's ~56ms/player: S1 timed all six models, this is one.)
  - [x] **Replication to the other five assemblers — CANCELLED `09-05`, measured away.** Full null
    audit of the 610 live rows: **exactly two columns are NULL in every row, `value` and `selected`**
    (four more are 10/610 — the known post-GW1-deadline elements, not new). Cross-referenced against
    every model's `REQUIRED_COLUMNS`: **minutes requires both; attacking, cards, DC, bonus and saves
    require neither.** The failure mode cannot arise in the other five, so the guard belongs exactly
    where it is. Same shape as E6's cancelled scoreline story — a 30-second measurement cancelling
    planned work
- [ ] (3) **Live-season price/ownership SUBSTITUTE — remainder of the (5) below, still open**
- [x] (2 → **3**) **S0b · Live gameweek-stats ingest — DONE `09-05`.** Was: not scheduled at all, found by S0's own
  probe.** **GW2 BACKFILLED `09-05`** — `snapshot_gameweek_stats.py --gw 2 --season 2026-27` wrote
  **626 rows**, live dataset now holds rounds **[1, 2]**, 1,236 rows; element 37 now assembles
  `games_played_this_season=2.0` where it read 1.0 before. GW3 is `is_current` and not yet settled,
  so it cannot be ingested until it is — which is the case for a *scheduled* job rather than a
  one-off. **Re-estimated 2 → 3:** it is four parts, not one — backfill (done), a **sweep mode** so a
  no-argument wrapper can drive it (both existing `.bat` wrappers take no arguments; the script
  decides on each firing — `snapshot_gameweek_stats.py` can't be scheduled as-is because `--gw`/
  `--season` are required and deliberately never inferred), the **missing heartbeat** (verified: only
  `snapshot_bootstrap.py` and `snapshot_odds.py` emit one), and the **task registration**.
  **CODE HALF DONE `09-05`:** `--sweep` (one bootstrap call, ingests every settled-and-not-yet-
  ingested gameweek, reusing the existing idempotence and settlement checks unchanged), an
  unconditional per-invocation heartbeat (`job=snapshot_gameweek_stats`), and
  `run_snapshot_gameweek_stats.bat`. **Gates, Architect-run:** `--sweep` → *"processed 2 settled
  gameweek(s) for season=2026-27: 0 written, 2 already ingested, 0 failed"* (the correct result —
  GW3 unsettled and excluded) · `integrity ok -- 1 file(s) checked`.
  **TASK REGISTERED `09-05` and verified THROUGH THE SCHEDULER, not by hand:** `fpl-ai
  snapshot_gameweek_stats`, hourly, `StartWhenAvailable=True`, `WakeToRun=True`,
  `MultipleInstances=IgnoreNew`. `State=Ready`, **`LastTaskResult=0`**, the scheduler's own run is in
  `cache/logs/snapshot_gameweek_stats.log`, and `check_heartbeat --job snapshot_gameweek_stats` →
  **`HEALTHY -- 6 run(s) seen`**. GW3 will be picked up automatically once it settles — that
  confirmation is pending, not part of the gate.
  **DELIBERATE DEVIATION, reported not smoothed:** registered with `LogonType=Interactive`, while the
  other two tasks use `Password`. `Password` needs the user's credentials, which Claude never
  handles; the unattended alternative `S4U` needs elevation and failed `Access is denied` from a
  non-elevated session. **Consequence: this task runs only while the user is logged on.** In practice
  the gap is small — the standing failure on this machine is a user-initiated *power-off*, which no
  `LogonType` survives — but to make it match, re-register from an **elevated** PowerShell with
  `-LogonType S4U`.
- [x] (2) **S0b-a · Season rollover cross-check — DONE `09-05`.** Was: `--sweep`'s season inference failed SILENTLY AND TOTALLY at rollover,
  found at S0b's review, proven by attack.** `_infer_season_from_store` takes the latest season
  already in the dataset; nothing cross-checks it against the bootstrap the candidates came from.
  In Aug 2027 the store holds 2026-27, inference returns `2026-27`, the live 2027-28 GW1 is checked
  against `(2026-27, 1)`, found present, and **skipped — the whole new season never ingests**, while
  the job reports *"already ingested, 0 written"*, exits 0 and heartbeats healthy.
  **Break-first proof** (throwaway store; project store verified unchanged after):
  `--sweep --season 2025-26 --store-path <tmp>` wrote **610 + 626 rows of 2026-27 live data under a
  `2025-26` label**, `ATTACK_EXIT=0`, nothing objected. **Fix is cheap:** the bootstrap already
  carries `events[].deadline_time` and the script already reads it — a season `YYYY-YY` starts in
  year `YYYY`, so compare GW1's deadline year against `int(season[:4])` and refuse on mismatch.
  **CLOSED:** `_check_season_matches_bootstrap` runs on **both** paths immediately after every
  bootstrap fetch and **before any candidate is enumerated** (sweep path: bootstrap line 493, check
  501, candidates 503 — verified by reading, which is what makes the rollover caught rather than
  skipped past). Anchored on GW1's `deadline_time` year vs `int(season[:4])`; raises
  `SeasonMismatchError` and exits **non-zero**, deliberately not a clean exit, because a season
  mismatch never resolves by retrying and a clean exit would let the hourly job hide it forever.
  *The earlier "explicit `--season` is the caller's problem" reasoning was overruled: this script's
  only live source is `event/{gw}/live/`, which always answers for the current season, so no
  legitimate mismatch exists on any path.*
  **Attacks, Architect-run, project store verified unchanged:** the original attack now raises and
  writes **nothing** (`ATTACK1_EXIT=1`, throwaway dir empty) where it previously wrote 1,236
  mislabelled rows at exit 0; explicit `--gw 2 --season 2025-26` → same error, exit 1; invented
  label-ahead-of-live `--season 2027-28` → same error, exit 1, so the guard is symmetric.
  *Residual, accepted not fixed: a non-numeric season prefix (`abcd-27`) raises a plain `ValueError`
  rather than the named error. Still fails safe — traceback, non-zero exit, nothing written — so the
  guarantee holds and only the message is worse. Typo case on a hand-run flag; cannot mislabel data.*
  **NOTE: S0b fixes STALENESS, not the price gap** — `value`/`selected` are 1236/1236 NULL because
  FPL's `event/{gw}/live/` carries no price or ownership at all, so the guard still correctly fires
  and the `elements` substitute stays open. The `fpl_api` rows in the capability reader cover **round 1 only**
  (`sorted(live['round'].unique()) == [1]`), so even `allow_live_season=True` reads a
  three-week-stale season: Lindelof has 180 real minutes across 2 played gameweeks and the
  assembler reports `games_played_this_season=1.0`. `scripts/snapshot_gameweek_stats.py` exists
  but `Get-ScheduledTask` returns exactly two fpl-ai tasks (`snapshot_bootstrap`,
  `snapshot_odds`). **This means S0 + the substitute are still NOT sufficient for live
  prediction** — that inference was wrong before this probe ran. **Gate:** the reader holds every
  settled round of 2026-27, verified by a live read, and the job is registered and heartbeating
- [ ] (5) **Live-season price/ownership gap — BLOCKS live prediction, not the E6 gate** — found by
  the probe above, `09-01`. `prev_gw_value` and `prev_gw_selected_log1p` come from `value` and
  `selected`, which are **`0` NULL across 179,950 vaastav rows but `610/610` NULL on every
  FPL-API row** — the live endpoint carries no historical price or ownership at all. So two of
  minutes' 13 features cannot be assembled for the current season from the capability reader. The
  substitute exists and is already being snapshotted: `elements` (629 rows) carries `now_cost` and
  `selected_by_percent` every 30 min, resolvable as-of a deadline. **Gate:** assembly for a live
  upcoming fixture produces all 13 features with no NULLs, sourced as-of the deadline

- [x] (5) **Honest E6 gate re-run `09-03` — PASSED, 3/3 seasons.** Was: seasons 2022-23…2025-26, bounded
  below by xG availability. **Gate:** beat the template (~2,100; 2,146 ideal). **Run it ONCE and
  report whatever it says** — user ruling `09-03`: no tuning against the test seasons, no re-running
  with adjusted settings to improve the number. A failure is a finding about the model stack, and it
  goes in this file and the handoff as one. Record the seed and commit so the number is reproducible.
  **RESULT** (`data/gate/e6_gate_results.jsonl`, seed 0, commit `495a168`, 152 min):

  | Season | model_stack | MILP proxy | template | margin |
  |---|---|---|---|---|
  | 2023-24 | **2221** | 1948 | 2092 | **+129** |
  | 2024-25 | **2392** | 2033 | 1906 | **+486** |
  | 2025-26 | **2228** | 1945 | 1951 | **+277** |

  Mean **2280.3** vs template 1983.0, **+297/season**. Lowest season 2221 clears both the 2,100 bar
  and the 2,146 career average. **§3.5's diagnosis confirmed:** the model stack beats the *proxy*
  MILP by 273–359 in every season — same optimiser, same constraints, same solver — so the signal
  was the bottleneck, not the algorithm.

  **Window is three seasons, not four:** 2022-23 cannot be fitted at all (the `starts` label begins
  *in* it, so a walk-forward fit at its opening has nothing prior). Data availability, not
  season-picking. **Caveats that must travel with this number:** the backtest re-decides weekly and
  **pays no transfer costs** — the template is measured identically, so the *gate* is sound, but
  2,280 is **not** a real-season forecast; 2025-26 GW1 ran with DC forced ineligible
  (`dc_cold_start_gameweeks=[1]`), recorded in the results file; and the objective is E[points],
  not the rank-aware one blueprint §10 needs, which is Phase 5.
  **UNBLOCKED `09-03`** — the
  scoring-config concern was measured away (above); today's config scores every backtestable season
  exactly. Known tail risk, carried here so it is not rediscovered: a GK goal in a backtested season
  would be paid 10 under the current config where 2020-21 paid 6. One occurrence in seven seasons,
  none inside the xG-era window the attacking model needs.

## E7 · Multi-period — *Phase 4, OPEN. Groomed `09-04`.*

**GATE DECIDED `09-04`, before the runner exists** (user ruling — `docs/HANDOFF.md` §3.6 warned that
a naive comparison would be unreadable): **the H=6 receding horizon must beat a myopic H=1 strategy
carrying the identical signal, both inside the new stateful harness, both paying identical transfer
costs and hits, over the same three seasons (2023-24, 2024-25, 2025-26).** The template re-measured
under the same transfer rules is the floor; **Phase 3's 2,280.3 is retained as a stated ceiling and
is NOT the bar** — it was scored with free weekly re-picks and no transfer costs. This isolates the
one thing E7 builds: lookahead. **Known risk, named before the run:** lookahead's real edge may be
tens of points per season, so a narrow margin needs a determinism check (two bit-identical runs)
before it is called a pass — and, per the E6 precedent, the gate is **run once and reported
whatever it says**.

**Three probes bind these estimates** (`.punchcard/s007.jsonl`, findings 2-5; commands and raw
output there, not paraphrased here):

1. **`SeasonReplay` is stateless.** No squad, bank, purchase price or free-transfer count crosses a
   gameweek boundary; `Decision` has no transfers; `score_gameweek` subtracts nothing. E7 is a
   *harness* change before it is an optimiser change (`src/fplai/backtest/replay.py:126-175,362-387`).
2. **A naive horizon assembly is silently wrong, and does not raise.**
   `days_since_team_previous_fixture` is computed from the team's previous kickoff *in the supplied
   frame*, so a t+3 prediction off history `< t` measured **28.0 days** where the forward schedule
   gives **3.75**. Same silent-wrong-answer class as §3.0. The fix is sanctioned by the function's
   own docstring — kickoff times are public pre-deadline — so the forward schedule's kickoffs must
   feed the gap calendar. **Pinned into S2/S3, not left to be rediscovered.**
3. **Horizon is a measured cost dial.** Single-period `decide` cost 2085/1922/2108 s per season
   (50.6-55.5 s per decision, `data/gate/e6_gate_results.jsonl`). Fitting does *not* scale with the
   horizon; deciding does, ~linearly. Naive H=6 projects to **~11 h for the three-season gate**
   before any multi-period solve time. S1 measures it before S8 commits to an H.

**Path conflict:** S2 and S6 both write `replay.py`. They run **sequentially**, never in parallel
(dispatch-protocol rule 8, one writer per path).

**CONVERGENT FINDING `09-05`, from S1 and S0 independently — treat as a design constraint on S3,
not as two performance bugs.** S1 measured feature assembly at a flat ~56 ms/player because the
history-derived rollups are rebuilt from the whole frame **once per element**; S0 then added a
second per-player full-capability read at **2.5 s** each. Same root cause: **the feature path
recomputes decision-invariant data once per player.** One fix serves both — hoist the invariant
read/rollup out of the per-player loop — and it is worth ~40-50% of the E7 gate's wall clock plus
~25 min per live gameweek.

- [x] (2) **S1 · Horizon cost spike `09-05` — DONE.** 2025-26, decision GW20, params fit 44.0s,
  history frozen at `round < 20`. `decide_candidates` per horizon step: **88.4 / 88.6 / 97.1 /
  100.6 / 99.6 / 96.7 s** (rounds 20-25, 10 fixtures each, candidates 790→817). **Cost is flat per
  step, so horizon cost is linear in H.** Cumulative per decision: H=1 88.4s, H=3 274.2s, H=6
  571.1s. Projected gate over 3 seasons × 38 GW, **both arms**: H=1 7.0h, H=3 12.9h, **H=6 22.3h**.
  *Projection caveat:* GW20 is mid-season and expensive — 88.4s against E6's 54.9s season average,
  since early gameweeks have little history to roll up. **Honest range for H=6 is ~14-22h**, not a
  point estimate.
  - **RULING: H is NOT tuned.** Blueprint §6.1 pins t..t+5. Choosing H by running 3 and 6 and
    keeping the better number is tuning against the test seasons — what the `09-03` ruling forbade.
    **H=6, run once, reported whatever it says.** H=3 is a development setting only.
  - **Hypothesis refuted, and it was the Architect's.** The Monte Carlo was expected to dominate and
    `n_simulations` (default 2000, its own docstring: "not a calibrated optimum") to be the lever.
    Measured on fixture 191, 83 players: **assembly 5.67s vs simulation 1.88s — assembly is ~75% of
    the cost.** Dropping n to 250 saves ~11% of a decide *and* perturbs the top-8 ordering at every
    reduced n (max E[pts] diff 0.373). **Do not touch `n_simulations`.**
  - **The real lever, measured:** assembly is **per-player and flat** — 58.6/69.4/52.4/54.1/56.7/
    55.3/56.1 ms per player at roster sizes 1/2/5/10/20/40/83. The history-derived rollups are
    rebuilt from the whole 14,185-row frame once per element. **Within one decision the history is
    identical across all 6 horizon steps and all 10 fixtures.** Memoising it is worth ~40-50% of the
    gate wall-clock at **zero accuracy cost** — bit-identical by construction. **Pinned into S3.**
  - **Free saving for S10:** the H=1 arm's decision at *t* is bit-identical to the k=0 step of the
    H=6 arm's decision at *t*. Share it and the entire 2.8h myopic arm disappears. S10's brief must
    require this rather than running two independent passes.
- [x] (5) **S2 · Forward schedule on the leakage boundary — DONE `09-05`. First real Phase 4 code.**
  `GameweekView.forward_fixtures: dict[round, fixtures_table]` for `t+1..t+H`, projected through a
  new `FORWARD_FIXTURE_COLUMNS = ("fixture","team","was_home","kickoff_time")` that deliberately
  **excludes `value`** — `ATTRIBUTE_COLUMNS` is never reused for a forward round, because a future
  price is unknowable at deadline `t` while round `t`'s own price is not. Built by the **existing**
  `_build_fixtures_table` (no second builder); `horizon` is a caller parameter defaulting to 5;
  additive, so every existing strategy and hand-built test fixture is untouched.
  **Gates, Architect-run:** `34 passed, 4 deselected in 2.07s` · `integrity ok -- 2 file(s)` ·
  **`61 passed in 213.66s`** (backtest + baselines + replay + optimiser, no marker filter, so the
  slow real-store tests are included).
  **Leakage attack, all 38 rounds:** *"rounds scanned: 38 | leaks found: NONE"*. Truncation
  `t=36 → [37,38]`, `t=38 → []`. Hard cases match the probe exactly: round 33 → 13 fixtures with 6
  teams doubled, round 34 → 7 fixtures.
  **Hot-path cost measured:** `_build_view` 8.0ms → 31.1ms = **+5.3s over the whole E7 gate**.
  Negligible; kept eager.
  **Caveat still carried into E7's number:** the archive holds the *final* round assignment; a
  postponed fixture's original round is unrecoverable here, and `pl_match_fixtures.matchweek`
  already diverges from FPL `round` on 4.4% of matches. This travels with E7's result the way E6's
  four caveats travel with 2,280.
  *Out of scope, recorded: a Strategy could hold a previous view and compare its `forward_fixtures`
  against a later view's `current_attributes`. Not a leak — both are legitimately visible at their
  own decision points — but cross-call state a determinism audit should check once chip timing
  exists (S6/E9).*
- [x] (5) **S3 · Horizon PMFs from the model stack — DONE `09-05`, gate MET on the restated terms.**
  Closed on: H=1 bit-identical on every non-DGW team-round (**790 assemblies, 0 changed**); the DGW
  differences **enumerated and each shown corrected** (6 team-fixtures, round 33); the t+3 rest gap
  reading **7.958 not 25.844**; and `horizon_candidates` verified across a blank round, a double
  round and a bit-identical `t`. Final suite **`166 passed in 924.48s`** (no marker filter) ·
  `integrity ok`. Commits `e0f7693` (pilot) and `c6f54b0` (part 2). Params fitted as-of deadline t, applied to rounds t..t+H via S2's schedule.
  `ModelStackStrategy.horizon_candidates(view) -> dict[round, list[OptimiserCandidate]]`, one
  decision calendar built once per decision from `view.fixtures` + `view.forward_fixtures` and
  threaded into the minutes assembler as an optional `schedule=` keyword (default `None` = today's
  behaviour byte-for-byte). **OWNED:** `src/fplai/optimiser.py`, `src/fplai/features.py`,
  `tests/test_optimiser.py`, `tests/test_features.py`. **READ-ONLY:** `src/fplai/backtest/**` (S6
  writes `replay.py` next — rule 8), `src/fplai/models/**`, `src/fplai/points.py`.
  **`tests/test_backtest_baselines.py` is FORBIDDEN** — it is the GameweekView leakage suite (8
  `forward_fixtures` tests) and S2's guarantees are unchanged.
  - **GATE, restated** — the registered "H=1 bit-identical to today's `decide_candidates`" is
    **unachievable** once the calendar is correct, because of the DGW defect below. The gate is now:
    H=1 is bit-identical on every **non-DGW** team-round; the only differences are DGW second legs,
    **enumerated** and each shown to be the corrected value; and a t+3 rest gap reads the
    forward-schedule value (**25.844 → 7.958** on the probed case), not the stale one.
  - **PROBE, Architect-run `09-05` against the real store, 2025-26** (rule 1; commands and full
    output in `.punchcard/s007.jsonl`, not paraphrased here):
    1. **Rest-gap defect reproduced**: t=20 → t+3 = round 23, fixture 221 Arsenal v Man Utd, element
       1 — naive `days_since_team_previous_fixture` **25.84375**, with the forward schedule supplied
       **7.95833**.
    2. **The `round` argument is NOT load-bearing** — `round=20` vs `round=23` with identical fixture
       facts differ in **no field**. Every history-derived feature is invariant across the horizon.
    3. **Exactly THREE fields in the whole six-model feature set are schedule-sensitive**, swept over
       36 real players across all four positions: minutes differs in
       `['days_since_team_previous_fixture', 'was_home']`; attacking, cards, DC, bonus and saves
       differ in `['was_home']` **alone** (`team_first_fixture_in_window` is the third in principle).
       This is what makes the per-element assembly memo bit-identical **by construction**. Measured
       cost **57.7 ms/player** for all six assemblers over a 14,185-row history frame — reconciling
       with S1's ~56 ms.
  - **A SECOND, PRE-EXISTING DEFECT found by the probe, on the round-t hot path E6 already ran on.**
    A DGW second leg's rest gap is measured from the last *pre-round* fixture, not from the first leg
    — which `_build_team_fixture_gap`'s **own docstring** says is the right answer. 2025-26 round 33
    (13 fixtures, 6 teams doubled): Bournemouth 332 **11.3125 → 4.2083**, Leeds 332 9.0 → 4.2083,
    Brighton 333 10.2083 → 3.1042, Chelsea 333 9.1458 → 3.0, Burnley 334 11.2083 → 3.25, Man City 334
    10.1458 → 3.1458. Exactly **6 of the round's 26** team-fixture pairs change; the 20 single-fixture
    pairs are bit-identical. It is right in training (full frame) and wrong in backtest assembly
    (history + one placeholder). Scoping the calendar to dodge it is indefensible — round t's fixtures
    **must** be in the calendar for t+1's gap to be right, so the same team-fixture would otherwise
    carry two different gap values depending on which round asked.
  - The **cheap per-element memo stays inside S3** (assemble once per element, overlay the three
    schedule fields per fixture): without it a naive H=6 calls assembly ~6x per element and blows past
    S1's 22 h ceiling. The **deep** hoist S1 named is split out as S3a below.
  - **PILOT (the decision calendar) REVIEWED AND ACCEPTED `09-05`.** `build_decision_calendar(view)`
    in `optimiser.py` (not `replay.py` — S6 owns that path next), plus an optional `schedule=`
    keyword threaded through `_assemble_minutes_feature_row_from_raw`,
    `assemble_minutes_feature_row_from_frame` and `assemble_fixture_player_features_from_frame`,
    forwarded to minutes **only**. `schedule=None` is today's behaviour byte-for-byte;
    `src/fplai/models/minutes.py` was not touched — `_build_team_fixture_gap` is simply handed more
    rows. `_build_candidates` builds it **once per decision**, never per fixture or per player.
    **Gates, all Architect-run, each alone with its own exit status:** `84 passed, 17 deselected in
    8.56s` (fast) · **`156 passed in 514.79s`** (optimiser + features + baselines + replay + points,
    **no marker filter**, so the slow real-store tests are included) · `integrity ok -- 4 file(s)
    checked`. **Probes, Architect-run against the real store:** non-DGW round 20 — **790
    player-fixture assemblies, 0 changed**, so H=1 is bit-identical; DGW round 33 — 1,077
    assemblies, 248 changed, **exactly the 6 predicted team-fixtures**, and the only field that ever
    differs anywhere is `days_since_team_previous_fixture`; t=20 target round 23 — **25.844 →
    7.958**. Cost: `build_decision_calendar` **0.0019 s** for 120 rows against a 57.1 s assembly per
    decision.
  - **One defect found at pilot review, by attacking what the coder did not.**
    `build_decision_calendar` was **non-deterministic in row order** — `pl.concat(...).unique()`, and
    polars' `maintain_order` defaults to `False`. Six repeated builds gave six different row orders.
    This is the **exact bug class** that produced `run_baselines.py --seed 42` → 1907/1909/1887 in
    s003. It was *latent*, not live: the gap value was stable at 7.958333 across all six builds
    because `_build_team_fixture_gap` sorts downstream — **which is precisely the accidental
    stability the s003 fix decided not to rely on**, and rule 7 requires reproducibility, not luck.
    Measured whether the dedup earned its place: **0 rows removed across every round of all three
    gate seasons** — it deduplicated nothing in 114 rounds and bought only non-determinism. Dropped,
    replaced with an explicit `.sort(["fixture","team"])`. **Post-fix attack, stronger than the one
    that found it:** row order identical across 8 builds *and* identical after shuffling every input
    fixture table — a pure function of content. The regression test was proven to fail against the
    old form before being trusted (CLAUDE.md lesson 5)
  - **PART 2 (`horizon_candidates`) REVIEWED AND ACCEPTED `09-05`.**
    `ModelStackStrategy.horizon_candidates(view) -> dict[round, list[OptimiserCandidate]]` for
    `t` plus every round present in `view.forward_fixtures`. The fixture loop was **factored out**
    of `_build_candidates` into a shared `_assemble_round_candidates` that both call — a second
    call, never a second implementation; `_build_candidates`' own behaviour is unchanged. One
    calendar and one params bundle for the whole horizon (a `params_by_gameweek[t+k]` lookup would
    leak). `decide()`/`decide_candidates()` stay single-period.
    **Gates, Architect-run:** `34 passed, 4 deselected in 1.29s` · `integrity ok -- 2 file(s)`.
    **Horizon probe, Architect-run on the real store**, 2025-26 **t=30** — chosen so the horizon
    spans both a blank round and a double round — all seven models fitted as-of GW30's first
    kickoff (fit 33.4s):
    - **`horizon[t]` is BIT-IDENTICAL to `decide_candidates`**, including every PMF's full support
      and probability vector. **Same element set in all 6 rounds (n=822); zero price/team/position
      drift.**
    - **Blanks:** round 31 (4 blank teams, 161 players) and round 34 (6 blank teams, 247 players)
      — **every one a degenerate-0 PMF, zero non-degenerate, zero omitted candidates.**
    - **Doubles:** round 33's 6 doubled teams carry PMF support sizes **13–16**, i.e. genuinely
      convolved two-fixture distributions, not doubled single ones.
    - **COST: `decide_candidates` 63.0s vs `horizon_candidates` 364.4s over 6 rounds — ratio 5.78
      where linear would be 6.00.** Sub-linear, confirming S1's flat-per-step finding.
  - **The S10 free saving is now PROVEN, not projected.** S1 flagged that the H=1 arm's decision at
    `t` should equal the k=0 step of the H=6 arm's, and that sharing it deletes the myopic arm from
    the gate entirely. Measured bit-identical above. **S10's brief must require the share.**
    **Revised projection on this measurement: 364.4s × 38 × 3 = 11.5 h for the H=6 arm, the H=1 arm
    free, plus ~0.8 h fitting ≈ 12.3 h** — the good end of S1's honest 14–22 h range. *Caveats,
    stated not buried:* one gameweek on one machine (S1 measured 88–100 s/step at GW20 where GW30
    measured 60.7 s/round), and it **excludes the multi-period MILP solve S8 has not built yet.**
- [x] (5) **S3a · Per-decision rollup hoist — DONE `09-06`.** S1's convergent finding: the
  history-derived rollups are rebuilt from the whole 14,185-row frame **once per element**, worth
  ~40-50% of the gate's wall clock *even at H=1*. Compute them once for all elements per decision.
  Touches all six assemblers in `features.py`, so rule 3 applies — pilot one, replicate the rest.
  Deliberately **after** S3: its gate is bit-identical candidate output, which is only checkable once
  S3's semantics have settled. **Gate:** candidates bit-identical before/after on a real gameweek,
  plus a measured before/after ms/player
  - **PROBED `09-06`, and the probe found the hoist is easier than registered.** 2024-25 GW20,
    read-only: `assemble_fixture_player_features_from_frame` costs **80.2 ms/player** (74-player
    fixture) / **80.6 ms/player** over the whole 711-player gameweek in 57.32s. cProfile says
    **5.057s of 5.934s (85%) is inside polars' `collect`, `ncalls=7204` for 74 players — 97 query
    executions PER PLAYER**, spread evenly across the six models (minutes 1.601s, cards 1.453s,
    dc 1.338s, bonus 1.207s, attacking 0.942s). Estimate **held at 5**.
  - **The rollup is ALREADY vectorised over elements — it is just being called once per element.**
    `models/minutes._build_round_rollup` groups by `['season','element','round']` and every trailing
    expression is `.over(['season','element'])`. One pass over the whole frame therefore yields
    every element's rollup, partition-independent by construction. Two details make it provably
    safe: the current per-element input is **already** filtered to `(element==e OR team==t)`, so it
    already carries other players' rows which are discarded afterwards by `rollup.filter(element==e)`
    — extra input rows demonstrably cannot perturb element `e`'s output; and each per-`(element,
    round)` placeholder only ever joins its own group.
  - **Reference captured BEFORE any edit** so the gate needs no git working-tree state from an agent
    (CLAUDE.md forbids that, and an s002 `stash` silently reverted another story's work): whole
    gameweek, 711 feature rows, floats at full `repr` with no rounding,
    **SHA256 `40b0dc25bb7cb99b8feed7b9a843dd61152f0c11b520dd366894211f456af3de`**, held in an
    `architect_readonly/` scratchpad subdirectory the brief declares off-limits for writes — a
    direct response to S8's collision, where a coder redirected its suite output over the
    Architect's own file and destroyed the independence of the check
  - **GATE MET, Architect-run with the Architect's OWN unmodified reference script.** Regenerated
    whole-gameweek SHA256 = **`40b0dc25...`, exact match** — 711 feature rows byte-identical.
    Assembly **80.6 → 5.6-7.7 ms/player** (a range over runs, idle machine; **not** a single figure
    — see the correction below). Zero top-level definitions lost, 12 added. Dispatched as a rule-3
    **pilot (minutes) then replicate**; both halves gated separately.
  - **THE REAL PRIZE, measured on the model stack rather than extrapolated**, on S3's own season and
    gameweek (2025-26 GW30, 822 players) so the before/after is directly comparable:
    `decide_candidates` **63.0s → 22.1s**, `horizon_candidates` **364.4s → 129.4s**.
    **E7 gate, H=6 arm, 38 GW × 3 seasons: 11.54 h → 4.10 h.** The story estimated itself at
    "~40-50% of the gate's wall clock"; measured, **~64%**.
  - **THE NUANCE THAT JUSTIFIES HAVING MEASURED IT.** Feature assembly sped up **10.5×** but
    candidate assembly only **2.8×** — so feature assembly was NOT the ~94% of `horizon_candidates`
    that an ms/player extrapolation implies. **Whatever now dominates the remaining 129.4s is no
    longer the rollup path; anyone optimising further must re-profile, not assume.**
  - **The coder shipped 12 public definitions with ZERO tests** (`tests/test_features.py` untouched),
    so the Architect attacked the claimed guards by hand — all four held — and then dispatched
    **S3a-b** to land them as repo tests. Most important of them: **a precomputed rollup asked for an
    element it was never built for RAISES rather than silently returning a wrong row** — the
    dangerous failure mode of any hoist, now closed and tested. Tests **73 → 77**, every value
    derived from the store, no literal gameweek pinned.
  - **A real defect found by attacking, not by testing:** that raise carried the explanation *"this
    should be structurally impossible (the placeholder row alone guarantees one match)"*. True
    before the hoist, **false after it** — with a precomputed rollup it is ordinary caller error.
    **Ten** sites carried the stale wording, plus the `FeatureAssemblyError` class docstring the
    coder found and the Architect had missed; all now name both cases.
  - **Sabotage verified reverted by evidence, not by the agent's word.** The coder temporarily
    replaced a guard with `if False:` to prove its test failed for the right reason. Diffing against
    an Architect snapshot taken *before* the follow-up showed **71 changed lines, every one inside a
    message string or that docstring**, and a residue grep returned 0.
  - `1434 passed in 2378.90s`, `SUITE_EXIT=0` · `integrity ok` · valid UTF-8, 0 mojibake.
  - **PROCESS SLIP, Architect's:** the S3a-b follow-up was dispatched onto `features.py` while
    S3a's own 900-line implementation was still **uncommitted** — the RC7 no-rollback-point
    condition, after correctly checkpointing before both earlier dispatches this session. Mitigated
    by snapshotting the verified file immediately (that snapshot is what later proved the revert
    clean, so the mitigation paid for itself twice). **The checkpoint rule binds before EVERY
    dispatch, including a small follow-up onto already-verified work** — "it's already verified" is
    exactly the reasoning that makes skipping it feel safe
- [x] (2) **S4 · Historical free-transfer rules — DONE `09-05`. The suspected change is REAL.**
  Sourced by the Architect inline (not dispatched to `fpl-data-scout` — a usage-budget call; the
  work was web sourcing plus a store read, no code). All Tier 1: premierleague.com and the project's
  own ingested `game_config`. Full evidence, quotes and URLs: **`docs/wiki/transfer-rules.md`**.
  | Season | FT/GW | Max banked | Hit | Exceptions |
  |---|---|---|---|---|
  | 2023-24 | 1 | **2** | −4 | none found |
  | 2024-25 | 1 | **5** | −4 | none found |
  | 2025-26 | 1 | **5** | −4 | **GW16 top-up to 5** (AFCON) |
  - **The FT bank changed inside the gate window — confirmed.** One PL sentence (13 Aug 2024)
    sources both sides: *"Up until now, you've only been able to bank two free transfers... you can
    now accumulate up to FIVE."* **A single hardcoded value is wrong for 2023-24.** It does **not**
    bias E7's gate — H=6 vs H=1 is compared *within* each season under identical rules, so a
    per-season difference is shared across both arms — but it is a reporting caveat on the
    per-season totals.
  - **2025-26 carries a per-GAMEWEEK exception and `SquadRules` is per-season.** The GW16 AFCON
    top-up is not expressible as a season constant. S5 either grows a sparse override map or
    declares and ignores it — **it must not be silently dropped.**
  - **The hit cost is genuinely ABSENT from `game_config`** (whole payload grepped; only
    `scoring.penalties_missed`/`penalties_saved` match). A **fourth** value of the class
    `fplai.scoring` already documents three of — declared constant with a cited source is the
    established pattern, not a rule-4 violation.
  - **`game_config` corroborates `rules.py`'s budget assumption — for 2026-27 only.**
    `squad_total_spend=1000`, `squad_team_limit=3`, `squad_squadsize=15`, `squad_squadplay=11`.
    Corroboration, **not** verification: the assumption covers 2019-20…2025-26, this config is the
    live season. That flag stays open.
  - **For S7:** `transfers_sell_on_fee=0.5` with `element_sell_at_purchase_price=False` — the 50%
    sell-on fee is live, sourced once, here.
- [x] (3) **S5 · Per-season transfer rules — DONE `09-05`.** **The registered shape was overruled by
  the probe:** `SquadRules` is constructed in **five** test files and consumed by every baseline, so
  new required fields would break all of them and optional-with-defaults would smuggle an unsourced
  default into the one type whose entire docstring is about not doing that. Landed instead as a
  **separate `TransferRules` + `SEASON_TRANSFER_RULES` + `transfer_rules_for_season()`** —
  purely additive, `SquadRules` and `rules_for_season` bit-untouched (verified: still exactly its 5
  original fields). Declared only; **S6/S7 consume it**, nothing is wired up.
  **Gate MET, Architect-run attack:**
  ```
  2023-24 ft/gw=1 max_banked=2 hit=-4 overrides={}
  2024-25 ft/gw=1 max_banked=5 hit=-4 overrides={}
  2025-26 ft/gw=1 max_banked=5 hit=-4 overrides={16: 5}
  every unsourced season raises KeyError naming the wiki:
    2019-20, 2020-21, 2021-22, 2022-23, 2026-27, '', '2023-2024', None -> 8/8 raise
  ```
  `84 passed, 8 deselected in 2.30s` (backtest + squad + replay + baselines + optimiser) ·
  `integrity ok -- 2 file(s)`. The coder attacked its own raise test by sabotaging the function to
  fall back to 2025-26's rules, and confirmed the test caught it — not a vacuous pass.
  - **SIGN CONVENTION, carry into S7:** `hit_cost = -4`, **negative**, matching
    `game_config`'s own `scoring.penalties_missed = -2`. S7's registered gate text says
    `score_gameweek` subtracts `4 * max(0, n - ft)` — with a negative constant that becomes an
    **addition**. S7's brief must pin the sign or this silently pays managers for taking hits
- [x] (5) **S6 · Stateful replay, part 1: state types + threading — DONE `09-05`.** `SquadState` (squad ids,
  purchase prices, bank, free transfers) threaded through `SeasonReplay.run` and `GameweekView`,
  with a stateless compatibility mode.
  - **GATE RE-GROOMED `09-05` — the registered one was described as "cheap" and is not.** It read
    "E6's three-season totals reproduce bit-identically in stateless mode". E6's gate run took
    **152 minutes**; re-running it to prove a harness refactor is another 152, and it would be
    proving the harness by way of the seven-model stack, which is the slowest possible witness for
    a change that does not touch a model. **Restated:** bit-identical **`run_baselines.py` totals
    across the same three seasons** — template, greedy and random exercise `SeasonReplay.run`,
    `_build_view` and `GameweekView` on *exactly* the same code path, run in minutes not hours, and
    greedy is the one already known to expose harness non-determinism (it caught the s003
    `.unique(maintain_order=False)` bug). Plus **one** model-stack gameweek for the
    `ModelStackStrategy` path. Same guarantee, a fraction of the wall clock, and a sharper witness.
  - **PATH CONFLICT, standing:** S6 and S7 rewrite `backtest/replay.py` and `backtest/data.py`.
    Nothing touching those runs beside them (rule 8). S3a is safe to run in parallel — it is
    confined to `features.py`
  - **DONE `09-05`.** `SquadState` (squad ids, per-element purchase prices in tenths, bank, free
    transfers) + `GameweekView.incoming_state: SquadState | None = None` +
    `SeasonReplay.run(strategy, initial_state=None)`. Stateful mode carries the ledger forward and
    **deliberately does not spend it** — no transfer count, no hit, no selling-price haircut; those
    are S7's, and the brief forbade deriving a transfer count by diffing squads so the accounting
    lands in the story that has tests for it.
  - **GATE MET, and stronger than the gate asked for.** The Architect captured the nine baseline
    totals *before* the dispatch and re-ran them after, then diffed the **whole output**, not just
    the totals: **`DIFF_EXIT=0`** — every gameweek count, mean, median, stdev, range and percentile
    byte-identical. Witness confirmed correct: the template column **2092 / 1906 / 1951** matches
    E6's own recorded template totals exactly.
  - **D5 attacked separately, because the three-season gate structurally cannot catch it.**
    `run_baselines.py --seasons 2020-21,2021-22,2022-23` — the seasons with **no** sourced
    `TransferRules` — **exit 0, all three complete**, so the stateless path genuinely never calls
    `transfer_rules_for_season`. Their totals also land inside Phase 1's published bands (2021-22
    greedy **2122** is the top of the recorded 1,889–2,122 range; 2020-21 template **1895** the
    bottom) — independent corroboration that nothing moved.
    `90 passed, 8 deselected in 2.33s` · `integrity ok -- 2 file(s)`.
  - **The coder caught its own vacuous test — lesson 5 working, and a reusable shape.** Its first
    purchase-price carry-forward test used a 2-round season and **passed identically against
    correct and deliberately-broken logic**: the price change lands in round 2's data, but round 2's
    *incoming* state is threaded from round 1, which never saw the raise. Three rounds is the
    minimum that threads state *through* the changed gameweek. **General shape: a test of a value
    carried across a state transition needs at least one transition after the change it tests, or
    the assertion lands before the mechanism runs.**
- [x] (5) **S7 · Stateful replay, part 2 — DONE `09-05`.** `Decision.transfers_in`/`transfers_out`
  (both defaulted to `()`), continuity validation, `_sell_price`, incremental bank arithmetic,
  free-transfer spending, and `score_gameweek(..., hit_points=0)` with the charge recorded on
  `GameweekResult.transfer_hit_points` rather than folded silently into `points`.
  - **GATE MET. Stateless bit-identity held against the ORIGINAL PRE-S6 reference**, not merely
    against S6: `diff` of the whole `run_baselines.py --seed 42` output for the three seasons →
    **`DIFF_EXIT=0`**. S6 and S7 *together* move nothing in stateless mode.
    `97 passed, 8 deselected in 2.40s` · `integrity ok -- 2 file(s)`.
  - **The registered gate text was wrong and the trap was pinned before dispatch.** It said
    `score_gameweek` "subtracts `4 * max(0, n - ft)`" — but `hit_cost` is **−4**, so subtracting
    would have *paid* managers for taking hits. Verified end-to-end by the Architect: no hit **36**,
    one paid transfer **32**, two paid **28** — each hit costs exactly 4 and lowers the total.
  - **`_sell_price` verified independently on seven cases**: 50→53 sells **51**, 50→47 sells **47**,
    50→60 sells **55**, 100→113 sells **106**.
  - **EDGE CASE, carry into S8: a 0.1m rise is NOT realisable.** purchase 50, current 51 → profit 1,
    half 0.5, floor 0 → **sells at 50**. Correct FPL behaviour, but it means bank does not move
    monotonically with price. **S8's rolling-budget constraint must use `_sell_price`, never current
    price**, or it will believe it has money it cannot raise.
  - **The coder caught its own attack firing the wrong guard.** Its first unaffordable-transfer case
    tripped `validate_squad`'s budget check rather than the ledger's, **masking** the guard it meant
    to prove; it rebuilt the case to keep squad cost under budget while starving the ledger's cash.
    **A guard proven by an attack that actually fires a different guard is not proven at all.**
- [x] (8) **S8 · Multi-period MILP — DONE `09-05`.**
  `optimise_multi_period`, built to the design already written in `src/fplai/optimiser.py:267-301`:
  gameweek-indexed variables, continuity link, rolling budget, FT penalty with its `max()`
  linearisation, objective summed over (i,t) through the existing `collapse_to_expected_points`
  seam. Execute week t only. **Chips are OUT of scope — they are E9, Phase 6.** **Gate:** H=1 with
  no incoming squad reproduces `optimise_squad` exactly; a constructed scenario where banking a
  transfer is optimal is solved as such. **Estimate held at 8 at dispatch** (registered 8, no
  re-estimate) — the two probes below removed the scale risk but not the design latitude.
  - **PROBED BEFORE THE BRIEF, and one probe changed the story.** Both throwaway, read-only,
    2024-25 GW20, real store; full output in `.punchcard/s007.jsonl`.
    **(a) Reference:** pool = **711** candidates (GK 73 / DEF 240 / MID 320 / FWD 78, 20 clubs),
    `optimise_squad` solves in **0.273s**, `objective_value=106.49206099999985`, repeat-call
    identical. **(b) Tractability, measured rather than feared:** a crude gameweek-indexed MILP
    over that same real pool solves to `kOptimal` in **T=1 0.15s / T=2 0.41s / T=3 0.78s /
    T=6 2.29s** (21,336 binaries) — and **2.27s at T=6 again** when per-round `e_{i,t}` is
    perturbed so transfers genuinely pay (26 taken). **Multi-period solve is ~4% of the 50.6-55.5s
    per-decision cost E6 already pays.** The gate's wall clock is feature assembly (S1/S3a), not
    the solver, so **no candidate pruning is to be built** — that would be optimising the 4%.
  - **DESIGN, derived from the probe: the `_sell_price` nonlinearity collapses to constants.**
    S3's D2/D3 pinned price as round `t`'s for every horizon round, so a player bought inside the
    horizon is sold inside it at exactly what was paid — no profit, no haircut. Only the
    **incoming** squad carries a purchase-vs-current gap, and there `_sell_price(purchase,
    current)` is a known integer at model-build time. Every `out_{i,t}` coefficient is therefore a
    constant: no auxiliary variables, no nonlinearity. Residual edge case **stated, not solved**:
    sell-then-rebuy-then-resell of an incoming player would realise current price on the second
    sale, so using the haircut constant throughout understates cash — the conservative direction.
  - **The H=1 gate is not aspirational — the crude probe already hit it**: `T=1, incoming=None`
    returned `obj=106.4921` against `optimise_squad`'s `106.49206099999985` on the identical pool.
    The probe's FT model was deliberately wrong (`ft0` at `t=0`, `max_ft` after, no running state);
    **the running `free_transfers_t` state with its cap and `max()` linearisation is the part S8
    actually has to get right**, and is where the review checkpoint sits
  - **GATE MET, both parts, Architect-run — and gate 1 is stronger than it asked.** On the real
    store (2024-25 GW20, 711-candidate pool), `optimise_multi_period(H=1, incoming_state=None)`
    matches `optimise_squad` on squad/xi/bench/captain/vice **and on `objective_value` byte for
    byte** — `106.49206099999985` both sides — so the tie-break terms replicate exactly rather
    than merely landing on the same argmax. Repeat-call bit-identical.
  - **Gate 2 attacked from OUTSIDE the story's own tests**, per the "no path is unsafe" standard.
    Sweeping a second upgrade's edge past the hit boundary: edge 2/3/4 -> 1 transfer, 0 hits,
    `obj=147.9997`; edge 5/6/8 -> 2 transfers, 1 hit, `obj=148.9997/149.9997/151.9997`. **The flip
    is exactly at 4->5 and the objective moves +1 per point above it** — a hit costs exactly 4, is
    not doubled, and is not a reward. Separately: incoming `ft=0` pays a hit for the same move,
    `ft=1`/`ft=2` do not, so free transfers are free and are spent before a hit is taken.
  - **The FT chain was hand-recomputed, not asserted.** Case B's six-round plan reproduced in plain
    Python from `hits=max(0,n-ft)` / `leftover=max(0,ft-n)` / `ft_next=min(max_banked, leftover+per_gw)`:
    all six rounds match the model's own `ft_available` and `hits` (1,1,1,2,2,1 against transfers
    1,1,0,1,2,1, capped correctly at 5).
  - **Determinism MET at H=6**, which E7's gate depends on: three repeated solves fingerprinting the
    executed decision *and the whole forward plan* are bit-identical, both with identical `e` per
    round and with `e` varying so transfers/hits/FT are live.
  - `1430 passed in 3698.99s`, `SUITE_EXIT=0` — **Architect-run, independent of the coder's**
    (3604.75s) · `integrity ok`, zero definitions removed · valid UTF-8, **0 mojibake sequences**.
  - **The coder's own attack found a real bug before the gate passed** — an off-by-one in the
    free-transfer round-key lookup that left the model feasible but silently suboptimal, invisible
    to the H=1 gate and to any constraint-shape test. It also caught its first three banking
    scenarios passing for the wrong reason (a bench-hidden edge, and a "prefund now, buy later"
    escape) and closed both.
  - **THE SOLVE-TIME NUMBER WENT WRONG TWICE, both times mine.** 2.3s (pre-dispatch probe — it
    **stubbed the hard constraint**, using one inequality with a constant RHS in place of the real
    chained FT state, and had no `in+out<=1` family or rolling-budget variables) → 17.2s (measured
    **under CPU contention**, with two full test suites running) → **8.6-11.8s, quiet machine, three
    runs each — the figure S10 must budget.** Neither a stubbed model nor a contended run is a
    measurement, and a wall-clock number without the machine's concurrent load stated beside it is
    not evidence. The qualitative conclusion survived all three: against E6's 50.6-55.5s per-decision
    feature-assembly cost the solver is **not** the bottleneck, and no candidate pruning was built.
  - **PROCESS, RC8 on a scratchpad path.** The coder redirected its own full suite into the exact
    file the Architect's run was writing, clobbering it. Caught only because the reported duration
    was identical to the file's *to the centisecond*, which two independent hour-long runs never
    share; the process table then showed the Architect's run still live. The result was never in
    doubt (the suite is read-only against the store), but **the independence of the check was — the
    Architect's verification had silently become a re-read of the agent's own claim, which is
    exactly the RC4 failure the run-it-yourself rule exists to prevent.** Agent and Architect
    scratchpad paths must not share a namespace.
  - **CARRY INTO S9:** the settle nudge on the FT diagnostic is `tie_break_eps`-scale (up to 5e-6,
    larger than one element-id tie-break step of 1e-6). Determinism is unaffected — measured — but
    the module's stated "lowest element id wins" rule needs a footnote for the multi-round case
    where two squads tie on real points and differ in banked free transfers. Docs note, not a bug.
- [x] (8) **S9 · `HorizonStrategy` — DONE `09-06`. Registered and RE-ESTIMATED 3 -> 8 before dispatch.**
  Wires S3 + S7 + S8 into a `Strategy` the replay runs. **Gate:** a full season without raising;
  two runs bit-identical. Owned paths `src/fplai/optimiser.py`,
  `src/fplai/backtest/replay.py`, `src/fplai/backtest/squad.py`, `tests/test_optimiser.py`,
  `tests/test_backtest_replay.py`. Routed to `fpl-xl-coder` — the brief carries ten pinned
  decisions, which by dispatch-protocol rule 10 is definitionally not mechanical.
  - **THE RE-ESTIMATE IS THE STORY.** The wiring genuinely is a 3. What four pre-dispatch probes
    found is that the registered gate — *a full season without raising* — is **unreachable** with
    the wiring alone, for a reason no code reading had surfaced in three prior stories.
  - **PROBE 1 — the candidate pool's element set is not stable across rounds.** Real store,
    read-only. Players present at round `r` and ABSENT at `r+1`: 2023-24 **6 of 37** round-pairs,
    worst GW28->29 with **494 elements vanishing** (pool 838 -> 355); 2024-25 3 of 37, worst 151;
    2025-26 2 of 37, worst 248. Elements present in EVERY round: **131 of 865**, 417/784, 375/841.
    Real blank gameweeks — all three seasons carry all 38 rounds, `missing_rounds=()`. Horizon
    length is 6 through GW33 and truncates 5/4/3/2/1 over GW34-38, identically in all three.
  - **PROBE 2 — the consequence, demonstrated rather than inferred.** `optimise_multi_period`
    builds continuity only over `ids` = round `t0`'s pool, so a held element outside that pool has
    no `in`/`out` variable at all. 2024-25's GW33 proxy-optimal squad holds **7 ids absent from
    GW34's pool**; the GW34 solve raised `multi-period MILP did not solve to optimality:
    status=kInfeasible, n_candidates=629, horizon_rounds=[34, 35]`. **Not a silent wrong answer —
    INFEASIBLE**, because 7 unsellable holdings cannot fund 7 replacements from a 14.1m bank.
  - **PROBE 3 — a second, independent full-season blocker.** `validate_squad`'s flat budget check
    (`total_price() > budget_tenths` -> `SquadError`), which `score_gameweek` calls
    unconditionally, is a **stateless-mode** invariant: in stateful mode a squad legitimately
    appreciates past 100.0m while the operative constraint is the bank ledger. Measured, not
    assumed: a static GW1 proxy squad held all season peaks at 68.0m / 79.0m / **96.0m** across the
    three seasons — **4.0m of margin with zero transfers**. Hazard measured; failure not yet
    observed.
  - **PROBE 4 — end-to-end rehearsal BEFORE the brief was written** (dispatch-protocol rule 1).
    A hand-rolled replay of 2023-24 **GW25-33**, containing the 494-element blank, running the
    full proposed design — free-build bootstrap, held-player backfill from `view.history`,
    last-known-price sell fallback — **completed all nine rounds without raising, 464 points**.
    The backfill fires for real: **4 held players at GW26, 11 of 15 at GW29**. The sell fallback
    fired twice, elements 294 and 303.
  - **SOLVE COST, corrected upward from S8's figure.** Nine H=6 solves, one run, otherwise-quiet
    machine: **3.91-26.92 s** (mean ~15.6 s; GW29's 355-candidate pool is the fast end) against
    S8's 8.6-11.8 s on one 711-candidate pool. **Budget ~10 min per season per arm at H=6**, not
    6. S10's wall-clock plan takes this number, not S8's.
  - **The E6 numbers cannot move.** Only `horizon_candidates` changes, and only when
    `view.incoming_state` is not `None`. `ModelStackStrategy.decide` and `decide_candidates` are
    untouched, and every E6 run was stateless.
  - **PHASE 1 (pilot) GREEN `09-06`, Architect-run, diff reviewed.** `HorizonStrategy`,
    `HorizonCandidateSource`, `TrailingProxyHorizonSource` in `optimiser.py`; `free_build_state`
    plus the `_advance_state` footnote in `replay.py`. D1-D4 and D8-D10 built; D5/D6/D7 (the
    blank-gameweek work) deliberately deferred to phase 2 and named as such in the module comment.
    `79 passed, 7 deselected in 1.97s` / `G1_EXIT=0` · real-store rehearsal 2024-25 GW1-3 stateful
    H=6 `2 passed in 162.74s` / `G2_EXIT=0`, points `[25, 25, 66]` · `integrity ok -- 3 file(s)`
    / `G3_EXIT=0`.
  - **The free build's no-hit claim was ATTACKED from outside the sanctioned path.** An empty
    `SquadState` passed *directly* to `optimise_multi_period`, bypassing `HorizonStrategy`'s
    `None` mapping, returns 15 `transfers_in` and **`hits == 15`** — a real -60. So the mapping is
    what earns the claim, not an accident of the MILP's continuity constraints.
  - **PHASE 2 GREEN `09-06`, Architect-run, diff reviewed.** Blank-gameweek continuity, three
    changes and none of them inside the MILP: held elements missing from round `t` are backfilled
    from `view.history` via `last_known_attributes`; `SeasonReplay.run`'s sell-price lookup falls
    back to that **same** helper; `validate_squad`'s flat budget check becomes stateless-mode-only.
    Plus a robustness gap found in the phase 1 review — an empty source raised `IndexError`, now
    `OptimiserError`. `93 passed, 8 deselected in 2.06s` / `G1_EXIT=0` · real-store 2023-24
    GW26-30 backfill counts by round `{26: 0, 27: 0, 28: 0, 29: 8, 30: 0}`, `2 passed in 30.21s` /
    `G2_EXIT=0` · `integrity ok -- 5 file(s)` / `G3_EXIT=0`.
  - **ONE helper, deliberately, for two callers.** The model's `sell_proceeds` coefficient and the
    ledger's sale price must be the same number or the two disagree about what a blanked player is
    worth. `last_known_attributes` is that number, and `view.history` **is**
    `rows_before_round(t)` — so this reads an existing leakage boundary rather than opening a new
    one.
  - **D7 was ATTACKED, and the attack is better than the obvious one.** The constructed overspend
    uses a squad costing **660 of 1000** — so the removed flat check would never have caught it
    either — yet it is unaffordable against the bank (50 + 40 proceeds < 100 cost). It raises from
    `_advance_state`'s negative-bank check. Removing a budget check did not open an overspend hole
    because that check was never the one holding the line in stateful mode.
  - **PROBE 5, run during the gate, closed a suspected limitation by measurement.** The backfill
    attaches a vanished element's last-known **team**, which is right for a club blank and wrong
    for a departure (a departed player's club may be playing, so they would join that roster and
    be valued for a game they cannot play). Measured across all three seasons: **1,704 vanished
    elements, 100.0% club blanks, zero departures.** The "absent from `current_attributes` == club
    has no fixture" equivalence holds exactly in this data.
  - **GATE MET `09-06`, BOTH HALVES, Architect-run** — `architect_readonly/s9_gate.py`, a full
    stateful `SeasonReplay` + `HorizonStrategy` + `TrailingProxyHorizonSource` +
    `free_build_state`, fingerprinting **every gameweek's whole decision and result** (squad, XI,
    bench, captain, vice, both transfer lists, autosubs, points, hits), not just the season total.
  - **A full season without raising — six of them.** `RUNA_EXIT=0`,
    `ALL JOBS COMPLETED WITHOUT RAISING`, 38 rounds each:

    | Season | H=6 total | H=6 hits | H=6 wall | H=1 total | H=1 hits | H=1 wall |
    |---|---|---|---|---|---|---|
    | 2023-24 | 1762 | -324 | 634.9s | 1935 | -152 | 19.5s |
    | 2024-25 | 1878 | -276 | 1784.9s | 1866 | -144 | 18.5s |
    | 2025-26 | 1669 | -268 | 556.6s | 1815 | -144 | 21.3s |

  - **Two runs bit-identical — five jobs, every fingerprint equal.** Run B repeated 2023-24 H=6,
    2025-26 H=6 and all three H=1 arms: `5 job(s) compared -- ALL BIT-IDENTICAL`, matching on
    rounds, total, hit points and the SHA256 of the whole per-gameweek decision stream
    (`9371c7d861c6d13a`, `c15f58fa309c89f2`, `c55fe7f76930e45e`, `f90d3e007c823163`,
    `f6df58377504f374`).
  - **DO NOT READ THESE TOTALS AS AN E7 PREVIEW — H=1 beating H=6 here is GUARANTEED by the
    source, not discovered.** `TrailingProxyHorizonSource` is history-only and fixture-blind: it
    carries round `t`'s distribution **unchanged** into every forward round, so a multi-period
    solve has literally zero forward signal to exploit and spends real hits chasing an unvarying
    distribution (-324 against -152 in 2023-24). The E7 gate is `ModelStackStrategy`-fed, where
    forward rounds carry genuinely different distributions. This proxy exists to prove the WIRING
    cheaply and for nothing else. **S10 must not reuse these numbers.**
  - **FULL SUITE GREEN, Architect-run on a quiet machine, independent of the coder's:**
    `1460 passed in 2268.97s (0:37:48)` / `SUITE_EXIT=0` — 30 tests up from S8's 1430, none
    deselected, `slow` real-store tests included. `integrity ok -- 5 file(s)`, zero definitions
    removed, zero mojibake.
  - **Wall clock, ONE run each, machine not fully quiet for the first job** (a data-only probe
    overlapped 2023-24 H=6 — which was nonetheless the *fastest* of the three, so contention does
    not explain the spread). H=6 varies **3.2x across seasons**, 556.6s to 1784.9s. H=1 is
    **18.5-21.3 s for an entire season** — so S10's myopic arm is very nearly free once the `k=0`
    candidate step is shared.
- [x] (5) **S10 · E7 gate runner — DONE `09-06`. Its own gate MET; the E7 PHASE GATE it ran FAILED.**
  `scripts/run_e7_gate.py`, to the gate decided above. Owned paths `scripts/run_e7_gate.py`,
  `src/fplai/optimiser.py`, `tests/test_optimiser.py`, `tests/test_run_e7_gate.py`. Routed to
  `fpl-xl-coder` — the brief carries pinned decisions.
  **Gate:** the comparison table, run once, reported whatever it says.
  - **THE "MUST SHARE THE k=0 STEP" REQUIREMENT IS RE-DERIVED, NOT INHERITED — S3a moved the
    ground under it.** The requirement was written when the myopic arm was 2.8h of a ~14h gate.
    S3a's hoist cut everything by ~3.5x, so the saving had to be measured again rather than
    quoted. **Numbers from generators run this session, never from this file's own prose:**

    | Component | Measured | Source |
    |---|---|---|
    | H=6 assembly, per decision | **104.05 s** | Architect-run slow test, 2025-26 GW20 |
    | H=1 assembly, per decision | **17.26 s** | same run — `ratio=6.03x`, `ratio/n_rounds=1.00`, i.e. **linear in rounds** |
    | Fitting, three seasons | **2,971.6 s = 0.83 h** | `data/gate/e6_gate_results.jsonl` (640.4 + 949.5 + 1381.7) |
    | MILP solve, H=6, three seasons | **2,976.4 s = 0.83 h** | S9's own full-season runs |
    | MILP solve, H=1, three seasons | **~60 s** | S9's own full-season runs |

  - **The waste is real and it is NOT where the requirement said.** `ModelStackStrategy.
    horizon_candidates` builds **every** round in `view.forward_fixtures` and `HorizonStrategy`
    truncates **after** — so today's myopic arm assembles six rounds and discards five.
    A `max_rounds` parameter takes that arm from **3.29 h to 0.55 h: ~2.7 h for about ten lines.**
  - **RULING: build `max_rounds`; do NOT build the lockstep-plus-memo share.** The remaining
    0.55 h is **8% of a ~7.5 h gate**, and buying it costs a generator refactor of the
    leakage-critical replay harness plus a cross-arm cache. The requirement is honoured in the
    form that survives S3a — the myopic arm no longer re-derives the five rounds it throws away —
    and the residual is recorded here as **measured and declined, with its number**, rather than
    quietly dropped. If a future session wants it, the design is: `SeasonReplay.run` as a
    generator, both arms driven in lockstep, one memo keyed `(gameweek, round,
    frozenset(elements))` — the element set is the only input that differs between arms, so that
    key is correct by construction.
  - **`scripts/run_e6_gate.py` is IMPORTED, never reimplemented.** It has a proper
    `if __name__ == "__main__"` guard, so a sibling import is safe, and its
    `_fit_params_by_gameweek` carries leakage reasoning that took an Architect review to settle —
    a cold-start gameweek falls back to `_degenerate_dc_bundle(as_of)` and **not** to the shared
    bundle, because the shared bundle is fitted at `as_of=now()` and has seen 2025-26 and 2026-27
    entire. A second copy of that is a leakage bug waiting to happen. E6's recorded behaviour to
    mirror: `dc_neutralised` **true** for 2023-24 and 2024-25, **false** for 2025-26.
  - **Projected gate wall clock ≈ 7.5 h** (3.29 H=6 assembly + 0.55 H=1 assembly + 0.83 fitting +
    0.83 solve, plus overhead). Per-season resume, E6's own precedent, so a crash costs at most
    ~2.5 h. **Actual: 5.36 h** (5683.7 + 6387.8 + 7236.7 s), `GATE_EXIT=0`.
  - **`max_rounds` MEASURED AT THE GATE'S OWN CONFIGURATION, not projected.** The coder's
    bit-identity test ran at 2025-26 GW37 where the full horizon is only **2** rounds; the gate
    runs **6**, so the Architect re-checked it there. 2025-26 GW20, one fit shared by both calls:
    full horizon `[20..25]` **104.84 s**, `max_rounds=1` `[20]` **17.58 s** — **5.96x, 87.3 s per
    decision** — and round `t`'s **790 candidates bit-identical**, name/position/team/price plus
    **full PMF support and probability vector**. Realised across the gate: the H=1 arm's decide
    wall was **660.7 / 630.6 / 699.9 s = 0.55 h**, exactly the projection, against the ~3.87 h it
    would have cost assembling six rounds. **~3.3 h saved, measured.**
  - **STORY GATE MET:** the comparison table, run once, reported whatever it says. That is S10's
    own deliverable and it is done. **The E7 phase gate is a different question, and it FAILED —
    see directly below.**

- **[!] E7 PHASE GATE — FAILED `09-06`, 0 of 3 seasons. Run once, reported as it came.**
  `scripts/run_e7_gate.py`, seed 0, commit `9713c9c`. **Raw evidence committed at
  `docs/wiki/e7-gate-results.jsonl`** — the run itself wrote `data/gate/`, which is
  **gitignored**, so the gate's own evidence would not have survived a clone. That was a
  flaw in S10's brief (the Architect pinned that path); `run_e6_gate.py`'s own default
  already pointed at `docs/wiki/`. **A future edit should change `run_e7_gate.py`'s
  `--out` default to match.**
  **H=6 did not beat a myopic H=1 arm carrying the identical signal.**

  | Season | H6 net | H1 net | diff | H6 gross | H1 gross | diff | H6 hits | H1 hits | H6 paid hits | H1 paid hits |
  |---|---|---|---|---|---|---|---|---|---|---|
  | 2023-24 | 2047 | 2071 | **-24** | 2151 | 2095 | **+56** | -104 | -24 | 26 | 6 |
  | 2024-25 | 1993 | 2000 | **-7** | 2061 | 2036 | **+25** | -68 | -36 | 17 | 9 |
  | 2025-26 | 2024 | 2024 | **0** | 2076 | 2068 | **+8** | -52 | -44 | 13 | 11 |
  | **Mean** | **2021.3** | **2031.7** | **-10.3** | **2096.0** | **2066.3** | **+29.7** | | | | |

  - **THE RESULT IS NOT "THE HORIZON DOESN'T HELP". It splits cleanly, and the same way in all
    three seasons: H=6 wins on GROSS points every season (+56/+25/+8) and loses on NET every
    season, because it takes far more paid hits (26 v 6, 17 v 9, 13 v 11).** The longer horizon
    picks better squads and trades them worse.
  - **HYPOTHESIS, labelled as such and NOT yet measured:** this is the classic receding-horizon
    over-trading pathology. The objective books a transfer's benefit across up to six rounds but
    the harness commits only round `t`, then re-solves — so the modelled benefit is earned only if
    the player is held, while the **-4 is paid in full immediately**. The measurement that would
    settle it is transfer churn: how often an element bought at `t` is sold again within the
    horizon it was bought to serve. The runner records season totals only, so this needs a
    decision-level log and a targeted re-run (~1.5 h for one season's H=6 arm).
  - **NOT PATCHED AROUND, AND DELIBERATELY NOT TUNED.** No H sweep, no hit-cost adjustment, no
    objective change was tried after seeing this. The `09-03` ruling forbids choosing a setting by
    running several and keeping the best against these same three seasons, and that ruling binds
    hardest exactly here, at the moment the gate says no. **Escalated to the user as a phase-gate
    decision** (CLAUDE.md rule 6).
  - **Context, not a comparison:** E6's stateless model_stack mean of 2,280.3 is a **ceiling** —
    free weekly re-picks, no transfer costs. The ~250-point drop to ~2,021-2,032 is the price of
    transfer realism and was expected; it is not a regression against E6.

- [x] (3) **S10a · Transfer-churn diagnostic — DONE `09-06`, user-chosen after the failed
  gate.** The one measurement that separates "the horizon's benefit is booked but not realised"
  from "the horizon genuinely does not pay". Owned paths `scripts/run_e7_gate.py`,
  `scripts/analyse_transfer_churn.py` (NEW), `tests/test_run_e7_gate.py`,
  `tests/test_analyse_transfer_churn.py` (NEW).
  **This is EVIDENCE, NOT TUNING** — it changes no objective, no horizon, no cost. It is
  explicitly not a step toward making the gate pass; if it confirms the hypothesis, what to do
  about it is a separate decision taken on its own merits.
  **Gate:** the churn table for 2023-24 (the worst season: H6 -24 net, 26 paid hits v 6), both
  arms, run once and reported whatever it says. **MET `09-06` — and it PARTLY REFUTES the
  Architect's own hypothesis.** Evidence committed: `docs/wiki/e7-churn-report-2023-24.txt` and
  the full decision log `docs/wiki/e7-churn-decisions-2023-24.jsonl`.
  - **FREE DETERMINISM CHECK, unplanned.** The re-run (a different commit, `--decision-log` on)
    reproduced the recorded gate **exactly** — `h6 2047 / -104 / 63`, `h1 2071 / -24 / 43`. The
    logging flag perturbs nothing, and E7's 2023-24 numbers are reproducible across a code change.
  - **The free build is excluded structurally**, `transfers_out` empty against a full-squad
    `transfers_in` — not by testing `gameweek == 1`. In a 3-gameweek smoke it had been **71% of
    all "purchases"** and dominated the censored count. Found by the coder, unprompted.

  | Metric, 2023-24 | H=6 | H=1 |
  |---|---|---|
  | Purchases (excl. free build) | 63 | 43 |
  | Mean / median completed hold (rounds) | 7.04 / **6.00** | 8.68 / 7 |
  | Sold within <1 / <2 / <3 / <6 rounds | 0.00 / 0.02 / 0.11 / **0.35** | 0.00 / 0.09 / 0.16 / **0.23** |
  | Round trips (sold, later bought back) | **13** | 7 |
  | Gameweeks taking a hit | **14** | 4 |
  | Mean transfers per gameweek | 1.66 | 1.13 |

  - **THE CRUDE VERSION OF THE HYPOTHESIS IS REFUTED.** "The model books six rounds of benefit and
    holds the player one" does not happen: **0.00** of H=6's purchases are sold within 1 round and
    **0.02** within 2, and its **median completed hold is 6.00 rounds — exactly the horizon whose
    benefit the objective booked.** H=6 largely *does* hold what it plans to hold.
  - **What IS measured:** H=6 churns more on every axis — 35% v 23% sold inside the horizon,
    **13 round trips v 7**, hits in **14 gameweeks v 4**, 1.66 transfers/gw v 1.13. The direction
    of the hypothesis holds; its proposed mechanism largely does not.
  - **SO THE SHARPER READING, and it is a different problem from the one registered:** H=6's
    marginal transfer is not worth its -4. This looks less like a plan-versus-commitment artefact
    and more like **the expected-points model overestimating the gain from a transfer** — a
    calibration question, which is E8's territory, not a horizon question.
  - **Where the cost sits:** 31% of H=6's season hit cost lands in **GW2-3 alone** (-32 of -104),
    correcting a cold-start opening squad once GW1's data arrives. **The arms diverge at GW1** —
    both free-build, but over different horizons — and never hold an identical squad in any of the
    38 gameweeks.
  - **A counterfactual that is NOT a result and must not be quoted as one:** H=6 without its GW2-3
    hits would be 2079 v 2071. Deleting a decision is not an available move; the number says only
    that the season is close and the cost is somewhat concentrated.
- [x] (3) **S11 · What-if engine — DONE `09-10`.** Evaluate a forced scenario ("what if I take a
  -4 for X?") against the optimum. `ForcedTransfer` + `WhatIfScenario` pin exact **round-t**
  transfer `in`/`out` binaries inside the existing `optimise_multi_period` MILP; free-transfer,
  bank and hit accounting are therefore reused unchanged, and later-round `plan` entries remain
  diagnostic rather than commitments. `evaluate_what_if` solves the same model unconstrained and
  constrained and reports horizon gross / hit / net expected points without leaking HiGHS's
  tie-break/FT-settle nudges into the user-facing comparison. Invalid ownership/identity forces
  raise before solving; a valid-but-impossible forced swap surfaces infeasibility rather than
  relaxing the scenario. **Gate:** fail-first import proved the API absent; 5 focused what-if tests
  pass, 69/69 non-slow optimiser tests pass, edit-integrity passes for both changed Python files,
  and the full non-slow suite is **1454 passed, 4 skipped, 81 deselected**.

**Sprint total 51** (46 as groomed `09-04`, plus S9's re-estimate 3 -> 8), plus the (2) live-season guard pulled into E6 above. **Grooming is incremental**
— S1 and S2's pilots are expected to re-shape S3 and S8, and that is the point
(dispatch-protocol rule 11). Per `docs/retro/2026-09-04.md` §2's process finding, **each estimate
pair is punch-carded at registration and at every re-estimate**, so calibration work stops needing
`git log -S` over this file.

- **Gate: FAILED 0/3.** See the ruling above: under identical transfer rules H=6 loses to H=1 net
  in 2023-24 and 2024-25 and ties in 2025-26. S11 completes the named Phase 4 deliverables but does
  **not** change or waive this failed phase gate.

## E8 · Distributions + rank-aware — *Phase 5*

- [·] Player-GW PMFs, correlated Monte Carlo
- [·] EO + captaincy model (forward-calibrated; backcast labelled as modelled)
- **Gate:** better rank distribution at equal or better points

## E9 · Chips — *Phase 6*

- [·] Chips as MILP variables; wildcard timing by option value
- **Gate:** positive chip EV in backtest

## E10 · News LLM — *Phase 7*

- [·] Structured extraction, source tiers, bounded log-odds updates
- **Gate:** measurable lift in minutes calibration vs FPL's own flag

## E11 · UI — *Phase 8*

- [·] FastAPI + Next.js, narration layer
