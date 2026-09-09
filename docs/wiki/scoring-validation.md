# Scoring reproduction — can we score history with today's rules?

Session `s006`. Pins a measurement the Architect made to close an apparent design gap in
`fplai.scoring`: `game_config` only ever holds the CURRENT season's rules (see that module's
docstring, "FORWARD-ONLY"), which looked like it would make `score_outcome` structurally unable
to reproduce a HISTORICAL gameweek's `total_points` — the wrong rules applied to the wrong
season. Measured instead of argued.

## What was measured

Every scoreable row (`position` and `total_points` both non-null) this store has ever ingested,
scored with `score_outcome`, composed with:

- `load_scoring_config(store, now)` — the CURRENT `game_config` (2026/27 rules)
- `build_dc_threshold_set(store, as_of=now)` — the CURRENT DC threshold set
- `defensive_contribution_met` resolved as a **bool** (`count >= threshold`), never the raw count

against the stored `total_points`:

| Season  | Exact match   | Season  | Exact match   |
|---------|---------------|---------|---------------|
| 2020-21 | 24,364/24,365 | 2023-24 | 29,725/29,725 |
| 2021-22 | 25,447/25,447 | 2024-25 | 27,283/27,283 |
| 2022-23 | 26,505/26,505 | 2025-26 | 29,747/29,747 |
|         |               | 2026-27 |    600/600    |

**163,671 / 163,672 exact — one residual mismatch.**

## The one residual, explained

`(season=2020-21, round=36, element=252, position=GK, goals_scored=1)`: predicted 14, actual 10.
This is a real GK goal — **exactly one exists across all seven seasons in this store** (Alisson,
2020-21 round 36) — scored under the CURRENT config, which pays a GKP goal **10** points
(2026/27 rules), where 2020-21's real rule paid **6**. `4` points of drift, matching the gap
exactly. This is not a bug in `score_outcome`; it is the forward-only gap made concrete and
measured: it costs one row in seven full seasons of data, confined to the single scoring
identifier (`goals_scored["GKP"]`) that is known to have changed value.

## Where the standing test lives

`tests/test_scoring.py::test_score_outcome_reproduces_stored_total_points_across_the_whole_scoreable_archive`,
marked `@pytest.mark.slow` (reads the whole archive against the real store; skips politely if the
real store is empty). The season list is derived from the store at test time, never a literal —
this must keep working as new seasons land without editing the test.

The assertion is expressed **by cause, not by count**: every mismatching row must be a GK-goal
row (`position == "GK" and goals_scored > 0`), never "exactly 1 mismatch". A hardcoded count would
break harmlessly (and noisily) the next time any GK scores anywhere in the archive; the cause-based
form stays quiet then and still fails loudly the moment FPL changes a rule that isn't the known
GKP-goals drift — which is the actual event this test exists to catch.

**Break-first proof** (not part of the standing suite — a one-off check that the test can fail):
perturbing the live-loaded `ScoringConfig`'s `assists` value by +1 (`dataclasses.replace(cfg,
assists=cfg.assists + 1)`) and re-running the same reproduction against the real store produces
**5,340 mismatches that are NOT GK-goal rows** (out of 163,672), which the cause-based assertion
would correctly fail on. Restoring the real config reproduces the 1/163,672 result above. Full
commands and output are in this session's punch-card (`.punchcard/s006.jsonl`).

## Two caveats, stated plainly

1. **This validates the REALISED outcome-to-points map only.** A backtest predicts an outcome
   vector first, then applies `score_outcome` to it — this measurement says nothing about
   whether that *prediction* is accurate, only that once an outcome is correctly resolved,
   `score_outcome` turns it into the right number of points. Residual risk is therefore confined
   to outcome combinations that are rare or entirely absent from this store's real history — the
   GK goal is the one known, now-measured instance of that risk; there may be others that simply
   never occurred in seven seasons of data (a keeper being sent off while also saving a penalty,
   for instance).
2. **322 rows in 2024-25 and all of 2019-20 are excluded** — they have no `position` value in
   this store's vaastav-sourced data, and `build_training_table` filters null positions out of
   training anyway (they were never going to influence a model regardless of this measurement).
   Every percentage above is **over scoreable rows only** (non-null `position` and
   `total_points`), not over the raw archive.

## What this does NOT license

`fplai.scoring`'s own module docstring is still correct and this measurement does not relax it:
`score_outcome`/`load_scoring_config` exist to score **predicted future** outcomes, never to
recompute a settled historical gameweek's points instead of reading stored `total_points`
directly (blueprint §7.2, CLAUDE.md lesson 7). This page measures how CLOSE a recompute would
land if someone did it anyway — it is evidence for the design decision, not a green light to
route around it.
