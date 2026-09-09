# Phase 1 — backtest replay harness and the three baselines

> Author: XL-Coder · session `s001` · 2026-08-21
> Implements blueprint §7.2 (backtest discipline, baseline definitions) and the Phase 1 gate
> in §7 ("numbers on the board"). Code: `src/fplai/backtest/**`. Tests: `tests/test_backtest_
> *.py` (33 new). Entry point: `scripts/run_baselines.py`.
> Consumes S-Coder's graphify where relevant — none existed for this area at pickup time;
> nothing to say it's stale.
>
> **Gate-repair addendum: XL-Coder · session `s003` · 2026-08-22.** A reproducibility bug
> (blueprint §7.2's own gate text, CLAUDE.md rule 7) was found and fixed — see §0.1. The
> totals in §4/§4.1/§5 below are the CORRECTED, reproducible numbers, not the 2026-08-21
> originals.

## 0. Gate status

**PASS — numbers on the board, and now actually reproducible.** Three baselines, six usable
seasons (2020-21 through 2025-26), real per-gameweek totals and distributions. Full suite:
**503 passed** (496 pre-fix + 7 new determinism tests). See §7 for the full run and §0.1 for
the gate-repair addendum below — **the totals in §4 were RESTATED on 2026-08-22** after a
reproducibility bug was found and fixed; the numbers in this document are the corrected ones,
not the 2026-08-21 originals (§0.1 gives the diff).

Two real, verified data problems were found and are reported rather than routed around
(§3). One caused a live crash while building this (`GKP`/`AM` position values, §3.3) and
is now handled explicitly, not silently.

---

## 0.1 Gate-repair addendum — session s003, 2026-08-22

> Author: XL-Coder · session `s003`

**The problem, as reported by the Architect.** `scripts/run_baselines.py --seed 42
--seasons 2025-26`, run three times on unmodified code against identical data, produced
`greedy_form` totals of **1907, 1909, 1887** — a direct violation of blueprint §7.2's gate
text, "real, **reproducible**, leak-free totals," and of CLAUDE.md rule 7. `random` and
`template` were stable across the same three runs.

### Root cause — two independent layers, both real, only one dominant

**1. Storage layer (`fplai.store.BitemporalStore.as_of`/`.observations`) — a real fragility,
not the dominant contributor here.** Neither method carried a canonical outer `ORDER BY`
(unlike `effective_at()`, which already has one — the story that added it is
`docs/wiki/provider-framework.md` §17.4). Attacked directly: two temp stores holding the
identical 30 entities, written as 30 separate single-row batches in **opposite** id order,
returned **completely different** `as_of()`/`observations()` row orders before the fix
(`tests/test_store.py::test_as_of_row_order_is_a_pure_function_of_content_not_write_order`
and the `observations()` sibling, both proven to fail against unpatched `store.py` before
being fixed). On the *current* real store's file layout (256 parquet files for
`vaastav_player_gameweek_stats`), this did **not** independently reproduce the reported
spread live — DuckDB's `preserve_insertion_order` default kept a single glob scan stable
run-to-run in this environment/version — but "stable because the current file layout happens
to keep it stable" is exactly the fragility the brief warned about ("adding one parquet file,
or changing partitioning, would silently reintroduce it"), so it is fixed regardless: `as_of()`
now sorts its output on the declared entity key (mirroring `effective_at()`); `observations()`
now uses DuckDB's `ORDER BY ALL` (sorts on every returned column), which works even for a
dataset with no declared entity key.

**2. Baseline layer — the dominant, live-verified cause.**
`fplai.backtest.replay._build_view()` deduplicated `current_attributes` with polars'
`.unique(subset=["element"], keep="first")`, and polars' `maintain_order` parameter
**defaults to `False`**. Verified live against the real store: five repeated `.unique()`
calls on the identical real 692-row 2025-26-gameweek-1 slice returned **five completely
different row orders, every single call** — not an intermittent edge case. That random
candidate order fed straight into `fplai.backtest.squad.build_squad`'s value-maximising
hill-climb, whose swap search picked "the first candidate that strictly beat the running
best" — for `greedy_form`, where many players routinely tie on a trailing-window total of
zero (or an identical non-zero total), ties are routine, and the winner among tied candidates
was silently whichever one the (randomly-ordered) input list happened to list first.
`random` and `template` were genuinely, not just apparently, unaffected: `random`'s value
is a per-candidate float draw from `random.Random(seed).random()` (a coincidental tie is
astronomically unlikely) and `template`'s is a large, mostly-distinct raw ownership count —
neither produces the routine exact ties `greedy_form`'s summed-points value does. Confirmed
by re-running the *unpatched* code end-to-end, live, 5 times over 2025-26: `greedy_form`
varied (**1916, 1920, 1918, 1913, 1944**), `random` and `template` were bit-identical across
all 5 (`tests/test_backtest.py`, proven fail-first against unpatched `replay.py`/`squad.py`
before being fixed, then green after).

### The fix, at both layers per the brief ("fix both, don't stack until the symptom disappears")

- `fplai.store.BitemporalStore.as_of()` / `.observations()` — canonical `ORDER BY`, as above.
- `fplai.backtest.replay._build_view()` — `current_attributes` is now
  `.unique(subset=["element"], keep="first").sort("element")`: its row order is a pure
  function of content, independent of upstream scan order.
- `fplai.backtest.squad.build_squad()`'s hill-climb — rewritten to select the single best
  swap by an explicit deterministic key, `(gain, -in_candidate.id, -out_candidate.id)`, over
  the **entire** set of legal swaps each iteration, rather than "first found, strictly
  better." This makes the swap winner a pure function of the swap's own content, structurally
  independent of both `selected`'s dict-iteration order and `by_position`'s list order — the
  brief's explicit ask: "I want the baseline reproducible **regardless** of row order."
  Fixing only the replay-layer feed (item above) would have left this fragility resting on
  every future caller of `build_squad` remembering to hand it a pre-sorted list; it no longer
  matters whether they do.

### Proof discipline

Every one of the three fixes above has a test that was run against the **unpatched** code
first (copied aside, never `git checkout`/`stash`), confirmed to fail with the exact
mechanism described, then re-run green after the fix:

- `tests/test_store.py::test_as_of_row_order_is_a_pure_function_of_content_not_write_order`
- `tests/test_store.py::test_observations_row_order_is_a_pure_function_of_content_not_write_order`
- `tests/test_store.py::test_as_of_and_observations_are_stable_across_repeated_queries_same_store`
- `tests/test_backtest_squad.py::test_build_squad_hill_climb_tie_break_is_independent_of_candidate_list_order`
- `tests/test_backtest.py` (new file) — the actual end-to-end regression, live against
  `data/store/`, all 3 strategies, N=5 repeated runs each. This is the one that stands in
  for the gate itself: it must fail on unpatched code and pass on the real store, not just on
  a fixture built to be friendly to the fix.

### Did the published headline change?

**Materially the same shape, imprecise by single digits to ~30 points per season — not
qualitatively wrong.** `random` and `template` are **bit-identical** to the 2026-08-21
publication, seed-for-seed, season-for-season — confirmed, not merely observed stable across
a small sample. `greedy_form` moved in every season, by amounts far smaller than the 22-point
spread the Architect's own 3-run sample showed for 2025-26 alone:

| Season | Greedy-form (2026-08-21, non-reproducible) | Greedy-form (2026-08-22, reproducible, N=3 confirmed identical) | Δ |
|---|---:|---:|---:|
| 2020-21 | 1,961 | 1,945 | −16 |
| 2021-22 | 2,127 | 2,122 | −5 |
| 2022-23 | 2,019 | 2,011 | −8 |
| 2023-24 | 2,017 | 2,043 | +26 |
| 2024-25 | 1,939 | 1,954 | +15 |
| 2025-26 | 1,891 | 1,889 | −2 |

The published headline range **Greedy 1,891-2,127** becomes **1,889-2,122** — the same two
seasons (2025-26 low, 2021-22 high) remain the extremes, and the range's width is
essentially unchanged (236 -> 233 points). **The Phase 1 conclusion — "no baseline beats the
human reference in any of the three comparable seasons" — still holds** under the restated
numbers (§5, updated). The only case where the best-baseline label's *margin* moved by a
non-trivial amount is 2024-25 (human's lead over the best baseline narrows from 312 to 297
points, greedy-form remaining the best baseline there both times) — still a clear gap, not a
reversal. **This is a finding for the Architect, not something absorbed silently**: the
headline range itself is superseded and should be corrected wherever it was cited outside
this document.

---

## 1. Harness design — how leakage is prevented structurally

### 1.1 The type that makes it hard, not just discouraged

`fplai.backtest.replay.GameweekView` is the **only** object a `Strategy` ever receives:

```python
@dataclass(frozen=True)
class GameweekView:
    season: str
    gameweek: int
    history: pl.DataFrame            # round < gameweek, EVERY column
    current_attributes: pl.DataFrame # round == gameweek, ATTRIBUTE_COLUMNS ONLY
```

`ATTRIBUTE_COLUMNS = ("element", "name", "position", "team", "value")` — facts fixed
**before kickoff** (identity, position, club, price), the same information a real manager
sees when picking before a deadline. Outcome columns for the gameweek being decided
(`total_points`, `minutes`, `selected`, `bps`, ...) are never *filtered out* of this object —
they are never *selected into* it in the first place. There is no attribute, method, or
back door on `GameweekView` through which a `Strategy` could reach them, and `Strategy`
objects are never handed `SeasonData` or the `BitemporalStore` — only a `GameweekView`, once
per decision.

`SeasonReplay.run()` enforces the ordering at the call-site level too:

```python
decision = strategy.decide(view)                       # decided first
outcome_rows = rows_for_round(self.season_data, gameweek)  # only exists after
result = score_gameweek(decision, outcome_rows, ...)
```

The full outcome frame for gameweek *t* is not constructed in any variable until
`strategy.decide()` for *t* has already returned. `tests/test_backtest_replay.py::
test_decide_is_called_before_scoring` proves this mechanically (patches `score_gameweek`
to assert a "decided" flag is set before it is ever called), not just by inspection.
`test_current_attributes_never_carries_outcome_columns` and
`test_history_never_includes_the_gameweek_being_decided` run every baseline through a spy
strategy that records every view it was handed and assert, on the *actual objects*, that
`current_attributes`'s column set is exactly `ATTRIBUTE_COLUMNS` and that `history`'s
maximum round is always `< gameweek`.

### 1.2 `observations()`, never `as_of()`, for this dataset — a load-bearing finding

`vaastav_player_gameweek_stats`'s declared entity key is `(season, round, element)`
(`schemas.py`). Real double/triple gameweeks put **2-3 rows under that exact key** —
verified live: 2020-21 GW35 (a Covid-rearrangement pileup) gives Bruno Fernandes three
rows, one per fixture, same `batch_id`, different opponent/kickoff/points.
`BitemporalStore.as_of()`'s `QUALIFY ROW_NUMBER() OVER (PARTITION BY entity_key ...) = 1`
would silently keep exactly one of those and drop the rest — **the mirror image of the bug
blueprint §3.2 already fixed once** (`as_of()` returning duplicated rows instead of state;
this is `as_of()` dropping legitimately distinct rows that happen to share a key).

`fplai.backtest.data` therefore reads this dataset exclusively through
`store.observations(dataset, until=...)` and reasons about "one row per (season, round,
element, fixture)" explicitly — grouping by `element` and *summing* across fixtures within
a round for scoring (`score_gameweek`), never assuming one row per player per round.
`tests/test_backtest_data.py::test_double_gameweek_rows_both_present` and
`tests/test_backtest_replay.py::test_double_gameweek_points_are_summed_not_overwritten`
cover this directly.

**Recommendation for the Architect:** call this out in `schemas.py`'s
`PLAYER_GAMEWEEK_STATS_GAMEWEEK` docstring so a future caller does not reach for `as_of()`
on this dataset out of habit — nothing today stops it, and the failure is silent (a smaller
season total, not an error).

### 1.3 Scoring — stored `total_points` only, captain doubling, autosubs from `minutes`

`score_gameweek()` sums the stored `total_points` for the final active XI (after autosubs)
plus one extra copy of the effective captain's points (captain if they played, else the
vice-captain if *they* played, else no bonus — standard FPL fallback). **Never recomputes
points from components** — blueprint §7.2's explicit instruction, because scoring rules
drift every season (defensive contribution didn't exist before 2025-26, goal values differ
by season and position) and re-deriving would silently apply today's rules to old data.

Autosubs (`simulate_autosubs`) are a simplified but faithful model of FPL's real algorithm:
GK-for-GK first if the starting GK has 0 minutes and the bench GK has >0; then outfield
bench players in bench order, each substituted in only if the resulting XI stays within
`xi_bounds` (1 GK / 3-5 DEF / 2-5 MID / 1-3 FWD). Not a byte-for-byte reproduction of FPL's
own edge cases, but the same shape and the same inputs (bench order, `minutes`).

---

## 2. The three baselines, as actually built

All three rank candidates from `view.current_attributes` by a baseline-specific `value`
computed from `view.history` only, then hand the ranked pool to one shared, deterministic
squad builder (`fplai.backtest.squad.build_squad` — cheapest-first feasibility skeleton,
then a value-maximising local search under budget/formation/club-cap constraints; no
solver dependency — the task brief allows only `requests`/`polars`/`duckdb`/`tzdata`, and a
certified-optimal ILP is Phase 3's job, not a baseline's).

| Baseline | Value signal | Re-decided? |
|---|---|---|
| **Random** | seeded `random.Random(seed).random()` per candidate, drawn once | No — buy-and-hold from the first available gameweek, XI/captain/bench fixed by the same seeded ranking, held all season (matches the brief's explicit "buy-and-hold" wording for Random specifically) |
| **Template** | previous gameweek's raw `selected` (ownership) count | **Yes, every gameweek** |
| **Greedy-form** | trailing `N=4` gameweeks' summed `total_points` | **Yes, every gameweek** |

**A design choice worth flagging plainly.** The brief described Random explicitly as
buy-and-hold but did not say so for Template or Greedy-form; both are phrased as per-
gameweek rules ("using the *previous* gameweek's ownership"; "highest trailing-N-gameweek
points"). Re-deciding both every week avoids building any transfer/budget-continuity
machinery this early (that is Phase 3/4's job), and each week's decision stays a clean,
independent, budget-constrained selection — no different in kind from a fresh draft. If the
Architect wants a strict single-buy-and-hold reading of Template/Greedy-form instead, that
is a small, contained change (drop the per-gameweek loop, keep the same value functions
evaluated once) — flagged here rather than assumed away.

### 2.1 The cold-start gameweek, taken to its logical conclusion

Applied strictly, the leakage rule ("a gameweek's own `selected` is not knowable at its own
deadline") means **Template has no legitimate signal for the season's first available
gameweek** — there is no *previous* gameweek yet. Greedy-form's trailing window is
similarly empty. Rather than fabricate a signal (violates §7.2) or crash the whole
backtest, both fall back to `value = 0.0` for every candidate for that one gameweek —
squad selection degenerates to a deterministic id-order tiebreak, explicitly documented in
`baselines.py`'s module docstring. This is a real, labelled degenerate case, not a modelled
decision — one gameweek out of 37-38 per season.

---

## 3. What the data did to fight back

### 3.1 Blank and double gameweeks

Handled by construction, not as a special case: a player with two rows in a round (a real
double/triple gameweek) has both fixtures' `total_points`/`minutes` **summed**
(`score_gameweek`'s `group_by("element")`); a player with zero rows in a round (blank, or
simply not selected that week) is treated as 0 points / 0 minutes via dictionary lookup
default — correct either way, since a non-featured player genuinely scores 0.

### 3.2 The disrupted seasons — and a second, harder problem underneath

**2019-20 is excluded from every result in this report**, for two independent reasons, both
verified live rather than assumed:

1. **The store only has 29 of 38 gameweeks** — rounds 30-38 are entirely absent.
   Re-fetched `gws/gw30.csv` and `gws/gw38.csv` directly from the upstream archive during
   this session: both exist and return real data (HTTP 200, real rows). **This is an
   ingestion gap in this project's own backfill, not the season's real disruption** — the
   season itself completed all 38 gameweeks by 26 Jul 2020, and PROGRESS.md's "Historical
   backfill — vaastav, 7 seasons, 172,819 GW rows, done" does not flag this. Recommend a
   follow-up backfill run.
2. **Even a complete backfill would not fix it.** 2019-20's raw archive rows genuinely have
   no `position`/`team` columns at all (confirmed by fetching `gws/gw1.csv`'s header
   directly: no `position`, no `team` — the FPL schema didn't carry them that far back).
   `fplai.backtest.data.load_season` detects this (`position`/`team` present as columns but
   wholly `NULL`) and raises rather than building a squad that cannot enforce formation or
   club-cap constraints. A future fix exists — `vaastav_player_identity`'s `element_type`
   carries position for every season, including 2019-20, and could be joined in — but that
   is a real design decision (season-scoped join, price convention differences) left for
   whoever picks this up next, not attempted here.

**2022-23 is included, with gameweek 7 explicitly labelled a skipped round** — the store is
missing round 7 entirely (0 rows, all players), also re-verified live as a real, fetchable
upstream file, so also an ingestion gap, not a genuine schedule quirk (the 2022-23 World
Cup break was after GW16, not GW7). This affects every baseline equally for that one round
(all three simply have no gameweek-7 decision or score, and their season totals are over 37
gameweeks, not 38) — reported as `37/38` in every table below, never silently presented as
a full season.

**Every other season checked (2020-21, 2021-22, 2023-24, 2024-25, 2025-26) has all 38
gameweeks present.**

### 3.3 Uneven column coverage — and two more live drift findings beyond Phase 0's recon

`expected_goals`/`starts` are null before 2022-23; `defensive_contribution` is null before
2025-26 — consistent with `providers/vaastav.py`'s existing documentation, and irrelevant
here since Phase 1 scores from stored `total_points` only, never these components.

**Two drift cases surfaced live while building this harness, not previously documented:**

1. **`GKP` vs `GK`.** 2021-22 uses `GK` for every round except round 37, which is labelled
   `GKP` — a one-round vaastav inconsistency (verified: no other season mixes the two).
   Normalised to `GK` unconditionally (`fplai.backtest.data._normalise_position`).
2. **`AM` rows.** 2024-25 carries 322 rows with `position == 'AM'` — real Premier League
   managers (Mikel Arteta, Pep Guardiola, ...) priced and pointed like players. This is the
   Assistant Manager chip (blueprint §11 notes it existed before 2026/27 and was removed for
   2026/27) — a special 16th pick, not a member of the 15-man squad. Dropped from the
   candidate pool entirely; letting these rows through crashed squad-building outright
   (`not enough GK candidates`, traced to a gameweek where the pool briefly skewed toward
   non-footballers) before this fix.

Both are handled in `load_season`, loudly documented in code, never silently coerced.

---

## 4. The numbers

`python scripts/run_baselines.py --seed 42` against the real store, **re-run 2026-08-22
after the gate-repair fix (§0.1), confirmed identical across 3 full repeated runs (all 6
seasons x all 3 strategies)** — these are the corrected, reproducible totals, superseding
the 2026-08-21 publication:

| Season | Coverage | Random total | Template total | Greedy-form total | Human reference |
|---|---|---:|---:|---:|---:|
| 2020-21 | 38/38 | 688 | 1,895 | 1,945 | — |
| 2021-22 | 38/38 | 908 | 2,060 | 2,122 | — |
| 2022-23 | **37/38** (GW7 missing) | 497 | 2,078 | 2,011 | — |
| 2023-24 | 38/38 | 570 | 2,092 | 2,043 | **2,169** |
| 2024-25 | 38/38 | 1,008 | 1,906 | 1,954 | **2,251** |
| 2025-26 | 38/38 | 942 | 1,951 | 1,889 | **2,019** |

*(2019-20 excluded — §3.2. 2022-23's total is over 37 gameweeks, not directly comparable
to a 38-gameweek season without accounting for the missing round. Random and Template are
bit-identical to the 2026-08-21 publication — confirmed genuinely unaffected by the
reproducibility bug, §0.1, not merely stable-by-luck. Greedy-form's totals changed by
−16..+26 points per season; see §0.1 for the full before/after and why the conclusion below
is unchanged.)*

### 4.1 Distributions (per-gameweek points), not just totals — blueprint §2

| Season / strategy | mean | median | stdev | min | max | p5 | p25 | p75 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2020-21 random | 18.1 | 17.0 | 9.2 | 1 | 49 | 5.7 | 13.0 | 24.0 | 31.6 |
| 2020-21 template | 49.9 | 48.0 | 14.8 | 10 | 84 | 26.7 | 43.0 | 60.8 | 76.0 |
| 2020-21 greedy-form | 51.2 | 50.0 | 16.7 | 10 | 88 | 28.9 | 37.8 | 63.8 | 75.8 |
| 2021-22 random | 23.9 | 24.5 | 11.9 | 2 | 66 | 8.7 | 15.0 | 29.8 | 42.3 |
| 2021-22 template | 54.2 | 56.0 | 17.8 | 4 | 91 | 23.6 | 42.2 | 67.5 | 79.8 |
| 2021-22 greedy-form | 55.8 | 56.0 | 15.2 | 4 | 85 | 37.2 | 47.2 | 67.8 | 77.7 |
| 2022-23 random | 13.4 | 14.0 | 8.1 | 2 | 33 | 4.0 | 6.0 | 18.0 | 26.4 |
| 2022-23 template | 56.2 | 57.0 | 20.6 | 2 | 92 | 19.4 | 46.0 | 69.0 | 89.2 |
| 2022-23 greedy-form | 54.4 | 53.0 | 18.2 | 5 | 96 | 31.8 | 41.0 | 66.0 | 84.0 |
| 2023-24 random | 15.0 | 13.0 | 8.4 | 4 | 35 | 4.8 | 9.0 | 20.8 | 31.6 |
| 2023-24 template | 55.1 | 57.5 | 21.8 | 4 | 97 | 19.8 | 38.2 | 71.2 | 84.9 |
| 2023-24 greedy-form | 53.8 | 48.5 | 21.2 | 4 | 125 | 31.9 | 39.2 | 65.0 | 86.9 |
| 2024-25 random | 26.5 | 25.5 | 10.8 | 2 | 52 | 14.0 | 19.0 | 32.0 | 49.4 |
| 2024-25 template | 50.2 | 51.0 | 18.2 | 0 | 104 | 22.2 | 40.2 | 58.8 | 76.3 |
| 2024-25 greedy-form | 51.4 | 52.0 | 15.4 | 0 | 75 | 30.2 | 42.0 | 62.0 | 73.2 |
| 2025-26 random | 24.8 | 24.0 | 9.6 | 6 | 45 | 11.4 | 19.0 | 30.8 | 43.1 |
| 2025-26 template | 51.3 | 49.0 | 16.3 | 4 | 80 | 27.6 | 42.5 | 63.0 | 76.2 |
| 2025-26 greedy-form | 49.7 | 50.5 | 15.9 | 4 | 80 | 29.9 | 37.2 | 61.0 | 73.6 |

Random's spread (stdev ~8-12) is narrower in absolute terms than Template/Greedy-form's
(~15-22) but far larger *relative to its mean* — consistent with §2's argument that shape,
not just the mean, drives outcomes: Random is not just lower-scoring, it is a much noisier
bet relative to its own level.

---

## 5. Does any baseline beat the human reference? Honest read.

**No baseline beat the human reference in any of the three comparable seasons.**

| Season | Best baseline | Baseline total | Human reference | Gap |
|---|---|---:|---:|---:|
| 2023-24 | template (2,092) | 2,092 | 2,169 | human +77 |
| 2024-25 | greedy-form (1,954) | 1,954 | 2,251 | human +297 |
| 2025-26 | template (1,951) | 1,951 | 2,019 | human +68 |

(Restated 2026-08-22 after the gate-repair fix, §0.1 — only 2024-25's greedy-form total and
gap moved, from 1,939/+312 to 1,954/+297; template's two rows are bit-identical to the
2026-08-21 publication and the best-baseline label did not change in any season.)

The gap is small in two of three seasons (77 and 68 points — roughly two good gameweeks'
worth) and large in one (297 points, 2024-25). **This is not evidence the baselines are
"almost as good as a real manager."** A real manager makes weekly transfers, uses chips
(wildcard, bench boost, triple captain, free hit), and reacts to injury/rotation news —
none of which these baselines do at all; they are static, single-decision (Random) or
mechanically-reactive (Template/Greedy-form) squads with no transfer logic whatsoever.
That a *simple, static, ownership-chasing* squad gets within 70 points of a real season in
two of three cases says more about how much of a real manager's edge comes from just
**not making bad transfers and not missing chip windows** than it says the floor is high.
The honest reading: **these are legitimate, non-flattering floors** — exactly what
blueprint §7.2 asks for — and Phase 3's "beat the template over 2+ backtested seasons" gate
is real, not a foregone conclusion, precisely because Template already gets uncomfortably
close to a real season without doing anything clever.

**No baseline was tuned to produce this result.** Per the task brief's explicit instruction,
nothing here was adjusted after seeing the numbers — the trailing window (N=4), the random
seed (42), and the squad-builder's mechanics were fixed before this section was written.

---

## 6. Test suite

```
$ uv run pytest -q
503 passed in ~130s
```

As of the original s001 publication: 257 pre-existing (unmodified, including the 7
real-store invariant tests) + 33 new across `tests/test_backtest_data.py` (9),
`tests/test_backtest_squad.py` (8), `tests/test_backtest_replay.py` (7 — the
leakage-boundary tests are here), `tests/test_backtest_baselines.py` (6),
`tests/test_backtest_report.py` (3) = 290.

**s003 gate-repair addendum (§0.1) added 7 more, all proven fail-first against unpatched
code before being fixed**: 3 in `tests/test_store.py` (row-order-is-a-pure-function-of-content
for `as_of()`/`observations()`, plus repeated-query stability), 1 in
`tests/test_backtest_squad.py` (`build_squad`'s hill-climb tie-break independent of
candidate-list order), 3 in the new `tests/test_backtest.py` (end-to-end, live against the
real store, N=5 repeated runs per strategy — the test that actually stands in for the gate).
The count that matters for this session is **496 -> 503**: 496 was the full suite's state at
s003 pickup (unrelated model/provider work landed on `master` between s001 and s003, not
itemised here), confirmed green both before this session's changes and after (503, no
regressions).

---

## 7. What Phase 2 needs that does not exist yet

- **The 2019-20 and 2022-23 store gaps should be closed** (§3.2) before this report's
  numbers are relied on for a Phase 3 comparison — a follow-up `scripts/backfill.py` run
  against `vaastav_archive` for 2019-20 rounds 30-38 and 2022-23 round 7. 2019-20 will
  additionally need `vaastav_player_identity`'s `element_type` joined in for
  position/club-cap enforcement (a small design decision, not attempted here).
- **`schemas.py`'s `PLAYER_GAMEWEEK_STATS_GAMEWEEK` docstring should document the
  `as_of()`-is-unsafe finding (§1.2)** so a future caller does not reach for `as_of()` on
  this dataset and silently undercount every double/triple gameweek.
- **A documented historical squad-rules source**, if one is ever found (`rules.py`'s
  budget/formation constants are a stated, flagged assumption — §7.2 of this doc — not
  verified against a per-season primary source, because none exists in the store for
  completed seasons).
- **A real optimiser to compare against** (Phase 3) — this report is the floor, not a
  preview; nothing here uses a solver, multi-period reasoning, transfers, or chips.
- **Calibration inputs (Phase 2 proper)** — team strength, minutes, xPts distributions —
  are unaffected by this work; Phase 1 deliberately never touches predictive modelling.
