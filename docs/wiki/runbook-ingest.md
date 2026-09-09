# Runbook — ingest spine (Phase 0, first vertical slice)

> Author: XL-Coder · session `s001` · 2026-08-19
> Covers: `src/fplai/client.py`, `src/fplai/store.py`, `src/fplai/sampling.py`,
> `scripts/snapshot_bootstrap.py`, `scripts/sample_picks.py`.
>
> **Scope note, added `xl-coder` wiki sweep 2026-08-21.** This runbook is scoped to the two
> scripts above and is still accurate for them — `snapshot_bootstrap.py` runs on the live
> 30-minute Task Scheduler cadence exactly as documented here, on `client.py`'s own request
> loop (a deliberate choice, not an oversight — `provider-framework.md` §3 explains why it
> was never rewired onto the newer `HttpTransport`). It does **not** cover the provider
> framework, the five providers, the backfill orchestrator, or Phase 1/2 model work built
> since 2026-08-19/20 — see `provider-framework.md`, `phase1-baselines.md` and
> `model-team-strength.md` for those. The test-count figure in §0 below is specifically
> stale (see the note there) and should not be trusted as current.

GW1 deadline: **Sat 22 Aug 2026 01:30 local** (17:30 UTC Fri 21 Aug). Ownership/price state not captured before
then is gone forever (blueprint §3.4). `snapshot_bootstrap.py` must be running on a
schedule before that.

---

## 0. Setup (one-time)

```powershell
cd C:\Users\fariz\Desktop\Projects\fpl-ai
uv sync
```

This creates `.venv\` and installs `requests`, `polars`, `duckdb` (runtime) and `pytest`
(dev). If `uv` isn't on PATH, it was installed to
`C:\Users\fariz\AppData\Roaming\Python\Python311\Scripts\uv.exe` in this environment —
either add that to PATH or call it by full path.

Run the tests (all network-mocked, no live calls):

```powershell
.venv\Scripts\python.exe -m pytest
```

**Expected: 49 passed — this figure is stale (2026-08-19, this slice only) and is left as
the historical record of what this runbook's own setup step originally verified.** The real
suite has grown substantially since (well past 400, across many more modules) as later
sessions built the provider framework, backfill orchestrator, and Phase 1/2 work — check the
actual count from your own `pytest -q` run rather than comparing against 49.

---

## 1. `snapshot_bootstrap.py` — THE URGENT ONE

### What it does

Fetches `bootstrap-static/` live (never from cache — see §4) and writes six datasets to
the bitemporal store at `data\store\`:

| Dataset | Grain | Contents |
|---|---|---|
| `elements` | 1 row / player | Every field bootstrap-static returns (109+ fields incl. `opta_code`, `now_cost`, `selected_by_percent`, `status`, `chance_of_playing_*`, `news`/`news_added`, `transfers_in_event`/`out_event`, `ep_this`/`ep_next`, `form`, `penalties_order`, `direct_freekicks_order`, `corners_and_indirect_freekicks_order`, `web_name`, `team`, ...) |
| `teams` | 1 row / club | Every field bootstrap-static returns per team |
| `events` | 1 row / gameweek | Every field, incl. `chip_plays`, `overrides` (nested — JSON-stringified) |
| `chips` | 1 row / chip x half (8 total, 2026/27) | Chip windows and overrides |
| `game_config` | 1 row | The whole `game_config` object as a JSON string in a `payload` column |
| `game_settings` | 1 row | The whole `game_settings` object, same shape |

Nested fields (lists/dicts, e.g. `chip_plays`, `overrides`, `price_change_projections`)
are JSON-serialised into a string column rather than exploded — nothing is dropped, it's
just not further normalised in this slice.

### Run it manually

```powershell
cd C:\Users\fariz\Desktop\Projects\fpl-ai
.venv\Scripts\python.exe scripts\snapshot_bootstrap.py -v
```

**Verified working, live, 2026-08-19** (actual output from this session):

```
2026-08-19 22:11:08,018 DEBUG urllib3.connectionpool: Starting new HTTPS connection (1): fantasy.premierleague.com:443
2026-08-19 22:11:08,218 DEBUG urllib3.connectionpool: https://fantasy.premierleague.com:443 "GET /api/bootstrap-static/ HTTP/1.1" 200 127606
2026-08-19 22:11:08,488 INFO fplai.snapshot_bootstrap: elements       WROTE                  rows=595   hash=5a760563d481
2026-08-19 22:11:08,488 INFO fplai.snapshot_bootstrap: teams          WROTE                  rows=20    hash=52be934dcc6a
2026-08-19 22:11:08,488 INFO fplai.snapshot_bootstrap: events         WROTE                  rows=38    hash=5a08aed190d3
2026-08-19 22:11:08,488 INFO fplai.snapshot_bootstrap: chips          WROTE                  rows=8     hash=eaf212d27793
2026-08-19 22:11:08,488 INFO fplai.snapshot_bootstrap: game_config    WROTE                  rows=1     hash=5df289195271
2026-08-19 22:11:08,488 INFO fplai.snapshot_bootstrap: game_settings  WROTE                  rows=1     hash=420cb66383ed
```

A second immediate run correctly reported every dataset `unchanged, skipped` — idempotency
confirmed live, not just in unit tests. A subsequent `as_of()`/`latest()` query round-trip
confirmed `game_config.scoring.goals_scored == {'GKP': 10, 'DEF': 6, 'MID': 5, 'FWD': 4}`
and `game_config.rules.squad_total_spend == 1000`, matching blueprint §11 exactly, and that
`as_of(2020-01-01)` correctly returns zero rows (no backward leakage).

### Schedule it — Windows Task Scheduler

A wrapper batch file is provided: `scripts\run_snapshot_bootstrap.bat`. It `cd`s to the
repo root, runs the script with the venv's Python, and appends output to
`cache\logs\snapshot_bootstrap.log` (inside the already-gitignored `cache\` directory —
nothing new to exclude).

**Verified**: running the `.bat` directly (`cmd /c scripts\run_snapshot_bootstrap.bat`)
exits 0 and produces the expected log lines — this is exactly what Task Scheduler will
invoke.

To schedule:

1. Open **Task Scheduler** → **Create Task...** (not "Create Basic Task" — need the
   repeat-every-N-minutes trigger, which Basic Task doesn't expose in its wizard directly,
   though you can add it after via the trigger's "Repeat task every" field).
2. **General** tab: name it `fpl-ai snapshot_bootstrap`. Under "Security options", select
   "Run whether user is logged on or not" if you want it to survive logout, or leave
   "Run only when user is logged on" if that's acceptable — either works since the script
   has no interactive UI.
3. **Triggers** tab → New: trigger type "Daily", start today, recur every 1 day. Then check
   **"Repeat task every"** → `30 minutes` (or `1 hour`), **for a duration of** `Indefinitely`.
4. **Actions** tab → New → Action "Start a program":
   - Program/script: `C:\Users\fariz\Desktop\Projects\fpl-ai\scripts\run_snapshot_bootstrap.bat`
   - Start in (optional): `C:\Users\fariz\Desktop\Projects\fpl-ai`
5. **Conditions** tab: uncheck "Start the task only if the computer is on AC power" if this
   runs on a laptop that's sometimes on battery.
6. Save. Test with "Run" from the Task Scheduler Library and check
   `cache\logs\snapshot_bootstrap.log` for a fresh line.

**Recommended cadence: every 30 minutes until GW1 deadline (Sat 22 Aug 01:30 local), then
every 60 minutes for the rest of the season.** Pre-deadline, prices/news/status/ownership
can move at any time and every unsnapshotted change before the deadline is permanently
unrecoverable state (not "gone" in the sense of the endpoint disappearing, but gone in the
sense that we never observed the intermediate value — the blueprint's bitemporal store is
only as good as its sampling density). Post-deadline the same fields still matter
(price changes, injury news) but the irrecoverable-loss framing is specific to §3.4's EO
concern, which is `sample_picks.py`'s job, not this script's.

### Command-line options

```
scripts\snapshot_bootstrap.py [--store-path PATH] [-v]
```

`--store-path` overrides the store location (default `<repo>\data\store`) — mainly for
testing; there's no reason to change it in normal operation.

---

## 2. `sample_picks.py` — EO / captaincy sampling

### What it does

Implements blueprint §3.4's forward EO collection:

1. Resolves the target gameweek (explicit `--gw`, or the most recently `finished` one).
2. Checks the gameweek's deadline has passed — **if not, exits cleanly (code 0)**, no error.
   Verified live (see below): `entry_picks/` 404s for every entry pre-deadline.
3. Binary-searches the current max valid entry id (~25 requests, verified live: found
   **6,185,941** on 2026-08-19 — already up from the wiki's 6,051,747 recon figure two hours
   earlier that same day, i.e. the id space is growing fast pre-season).
4. Draws `n` (default 10,000) **distinct entry ids uniformly** from `[1, max_id]` using
   `random.Random(seed)` — NOT league-314 paging. Deterministic: same seed, same draw.
   Default seed is `1_000_000 + gw` if `--seed` isn't given; always logged.
5. Probes a 10-entry warm-up batch first. If **all 10** 404, aborts loudly (exit code 2)
   before spending the full request budget — this is the "picks/ might not actually be
   public yet even though the deadline passed" guard.
6. Fetches picks for all `n` entries (one request each, rate-limited by `FPLClient`, ~83
   min wall-clock at the 2 req/s ceiling for n=10,000).
7. Persists one row per (entry, element) pick to the `picks` dataset: `element`,
   `position`, `multiplier`, `is_captain`, `is_vice_captain`, plus the entry's
   `active_chip`, `bank`, `team_value` (`entry_history.value`), `event_transfers`,
   `event_transfers_cost`, `points_on_bench`, `overall_rank`. `valid_at` = the gameweek's
   deadline; `observed_at` = the moment this script ran. Every sample is written
   unconditionally (`skip_if_unchanged=False`) — a re-sample is a fresh independent draw,
   never a duplicate to dedup away.
8. Runs the **mandatory calibration check**: sampled ownership % per element vs
   bootstrap-static's `selected_by_percent`, for players above 1% ownership (below that,
   sampling noise dominates and the check isn't informative). **A deviation above the
   threshold (default 3.0 percentage points, `--calibration-threshold-pp`) is logged as a
   hard `CALIBRATION FAILURE` at ERROR level and the script exits 4** — per blueprint §3.4
   this must never be silently downgraded to a warning. Note: 3.0pp is a judgement call
   made without real post-deadline data to validate it against (theoretical SE at n=10,000
   is ≤0.5pp at any true proportion) — **revisit this threshold once the first real GW1
   sample exists.**

### Command-line options

```
scripts\sample_picks.py [--gw N] [--n 10000] [--seed N] [--store-path PATH]
                         [--calibration-threshold-pp 3.0] [--dry-run] [-v]
```

`--dry-run` resolves the target GW, does the id-space binary search, and runs the 10-entry
probe — then stops before the full budget or any store write. Useful for a cheap
"is this ready yet" check.

### VERIFIED, live, pre-deadline (2026-08-19)

- `check_ready()` correctly raises for GW1 (deadline in the future) and the CLI exits 0
  with a clear message — confirmed via both a direct function call and the full
  `sample_picks.py --gw 1 --dry-run` CLI invocation.
- `find_max_valid_entry_id()` converges against the live API: **6,185,941**, in ~13s
  (well under the ~25-request / 2 req/s budget the blueprint estimates).
- `draw_uniform_entry_ids()` is deterministic (same seed → same 10 ids) and produces
  distinct, in-range ids — confirmed against the live max id.
- `probe_picks_available()` correctly returns `False` right now (all 10 warm-up entries
  404, as expected pre-deadline) — confirming the abort-loudly guard would actually fire
  if this were reached in the wrong state today.
- Full unit coverage (mocked) for `find_max_valid_entry_id`'s convergence and request
  efficiency, `draw_uniform_entry_ids`'s determinism, and `calibration_check`'s pass/fail/
  edge-case behaviour — 16 tests, `tests\test_sampling.py`.

### NOT YET VERIFIED — cannot be, until Sat 22 Aug 2026 01:30 local (17:30 UTC Fri)

This is the honest gap. The following are built and unit-tested against an *assumed*
response shape, but **the shape itself is unverified** (wiki §2.1: `picks/` 404s for every
entry right now, so the real payload has never been observed):

1. **The actual JSON shape of a successful `picks/` response.** `_picks_to_rows()` in
   `sample_picks.py` assumes `{"picks": [...], "active_chip": ..., "entry_history": {"bank":
   ..., "value": ..., "event_transfers": ..., "points_on_bench": ..., "overall_rank": ...}}`
   based on the pre-2026/27 FPL API's documented shape. If 2026/27 changed field names or
   nesting, this will either silently populate `None`s (missed columns) or KeyError — check
   `cache\logs\sample_picks.log` and a raw sample response after the first real run.
2. **Whether the deadline-passed + probe-succeeds condition is sufficient**, or whether
   there's a further lag (wiki §2.7 recommends waiting for `event-status/` to show bonus
   added — this script logs a warning if `finished` is false but does not block on
   `event-status/`). If the first post-deadline run's probe fails, wait and retry rather
   than assuming something is broken.
3. **The calibration threshold (3.0pp default).** Chosen from the theoretical SE bound
   with headroom, not from an observed distribution. Watch the first real run's
   `max_deviation_pp` and tune `--calibration-threshold-pp` accordingly — or better, tune
   the default in `sampling.py` once there's real data and record the decision in this
   runbook.
4. **Wall-clock and reliability of a real ~10,000-request run.** The rate limiter and
   backoff are unit-tested against a mocked 429/403 sequence, but a real 83-minute,
   10,000-request run against the live API — including whatever failure modes only show up
   at that volume — has not happened and cannot happen before the deadline.

### When to run it

Per blueprint §3.4/wiki §2.7: **after the gameweek's final match is scored**, when
`event-status/` shows bonus points added — picks are then immutable and CDN-cached, which
is both correct (no mid-scoring races) and cheap for FPL to serve. For GW1 (Sat/Sun
matches, per the published fixture list), that's some hours after the last Sunday match,
not immediately at the 01:30 local deadline.

**Recommended first run: manual, not scheduled**, sometime after GW1's final match is
confirmed scored — watch it interactively (`-v`) the first time, given point 1 above. Once
the response shape is confirmed against reality, `scripts\run_sample_picks.bat` can be
scheduled weekly (same Task Scheduler mechanism as §1) for a time window after each
gameweek's matches typically conclude — but re-check `event-status/` timing per gameweek
rather than trusting a fixed clock time, since kickoff times vary (Friday/Saturday/Sunday/
Monday windows all occur across a season).

---

## 3. Output layout

```
data\store\
  elements\date=YYYY-MM-DD\<observed_at>__<batch8>.parquet
  teams\date=YYYY-MM-DD\...
  events\date=YYYY-MM-DD\...
  chips\date=YYYY-MM-DD\...
  game_config\date=YYYY-MM-DD\...
  game_settings\date=YYYY-MM-DD\...
  picks\date=YYYY-MM-DD\...          (after the first sample_picks.py run)
cache\fpl_api\<sha256(url)>.json      (per-URL response cache; gitignored)
cache\logs\snapshot_bootstrap.log     (Task Scheduler wrapper output; gitignored)
cache\logs\sample_picks.log
```

`data\` and `cache\` are both already gitignored (`.gitignore`, pre-existing). Nothing
about this ingest spine needs new gitignore entries.

### Querying the store

> **CORRECTED, `xl-coder` wiki sweep 2026-08-21 — the example below was wrong.** It called
> the pre-2026-08-20 `as_of()`/`latest()` signature, which took a caller-supplied
> `entity_key=`/`latest_only=` override. E2b story 12 (`provider-framework.md` §8) removed
> both: `as_of()` now always collapses to state using the dataset's *declared* entity key
> (`fplai.schemas.DATASET_ENTITY_KEYS`), and the raw stream — what `as_of(..., latest_only=
> False)` used to return — now needs the separate, explicit `store.observations(dataset,
> until=...)` call. The snippet below is the current signature; the one this replaced would
> now raise `TypeError` on the unexpected keyword.

```python
from datetime import datetime, timezone
from fplai.store import BitemporalStore

store = BitemporalStore()  # defaults to <repo>/data/store

# OPERATIONAL use — current state, safe outside training/backtest paths:
elements_now = store.latest("elements")

# TRAINING/BACKTEST use — MUST pass an explicit historical deadline:
deadline = datetime(2026, 8, 21, 17, 30, tzinfo=timezone.utc)
elements_as_of_gw1 = store.as_of("elements", deadline)

# Raw observation stream (every batch, no per-entity collapse) — a different call, not a flag:
elements_stream = store.observations("elements", until=deadline)
```

`as_of()` filters on `observed_at`, never on "today" — see `src/fplai/store.py`'s module
docstring for why this is the load-bearing part of the whole spine.

---

## 4. Design notes / things a reviewer should know

- **`FPLClient` caches by URL, indefinitely, unless `force_refresh=True`.** Both ingest
  scripts that need fresh state (`bootstrap_static()` in `snapshot_bootstrap.py`) pass
  `force_refresh=True` explicitly — the cache exists for idempotent re-runs of
  `entry()`/`entry_picks()` during development and for the id-space binary search, not to
  silently serve stale bootstrap data on a schedule that exists specifically to catch
  change. If you add a new call site that needs live data, check whether it needs
  `force_refresh=True` — the default is cache-first.
- **The store's `skip_if_unchanged` idempotency compares content only, not time.** Running
  `snapshot_bootstrap.py` every 30 minutes for a quiet week writes nothing new — by design.
  This is different from `sample_picks.py`, which always writes (`skip_if_unchanged=False`)
  because a re-sample is never a duplicate of a previous one even if the field-level content
  happens to coincide.
- **Timestamps are stored as naive UTC**, not `TIMESTAMPTZ`. `duckdb`'s TIMESTAMPTZ→python
  conversion pulled in `pytz` as an undeclared runtime dependency, which we didn't want for
  a two-line convenience — see `_require_utc()` in `store.py`. The public API still *rejects
  naive input* at the boundary (the actual safety property); it just normalises to naive
  UTC once validated. Every timestamp in the store is UTC by convention, always.
- **`store.py` deliberately has no "read current state" convenience without a cutoff.**
  `as_of(dataset, timestamp)` is the only way to read historical data; `latest()` exists as
  clearly-named sugar for `as_of(dataset, now())`, for operational (not backtest) use. If a
  future model-training or backtest code path calls `.latest()`, that is very likely a bug —
  it should call `.as_of()` with an explicit historical deadline instead.

## 5. What this slice deliberately does NOT build

Per scope: no backtest harness, no models, no optimiser. `store.py` provides the `as_of`
primitive the backtest harness will need, but does not itself replay a season. Odds
ingestion (`THE_ODDS_API_KEY`) was in scope only optionally and was **not built** — nothing
in blueprint §3.3 is urgent before GW1 the way ownership snapshotting is, and it is
live-only / cannot enter the backtest per that section, so there's no deadline pressure to
build it in this pass.

---

## 6. Heartbeat — "nothing changed" vs "scheduler was down" (session s003)

> Author: XL-Coder · session `s003` · 2026-08-22 · closes PROGRESS.md E2.

### The problem, demonstrated live

At 17:32 local on 22 Aug 2026 the newest `events` row in the real store was **~11 hours
old** against a 30-minute Task Scheduler cadence. From the store alone that is
indistinguishable from an outage — `store.write(..., skip_if_unchanged=True)` (§4's second
bullet above: the correct behaviour for every real dataset) means "no new row" is genuinely
ambiguous between *nothing changed* and *the scheduler never ran*. Establishing the
snapshotter was actually healthy that day required Task Scheduler's own `LastRunTime` /
`NumberOfMissedRuns` plus the `.bat` wrapper's log file — neither is queryable, neither is
bitemporal, and the log is not part of the store at all.

### The fix — a dedicated `heartbeat` dataset

`scripts/snapshot_bootstrap.py` now writes one row per target dataset (six today) to a new
`heartbeat` dataset on **every run**, unconditionally (`skip_if_unchanged=False` — literal,
regression-tested, see `write_heartbeat`'s own docstring), regardless of whether the run
succeeded, partially succeeded, or raised outright. Schema: `fplai.schemas.
JOB_HEARTBEAT_RUN` (`job.heartbeat@run`), entity key `(job, run_ts, target_dataset)` — see
that constant's own module comment in `src/fplai/schemas.py` for the full grain/uniqueness
reasoning (an EVENT STREAM, not entity state — the same posture `MANAGER_PICKS_SELECTION_
GAMEWEEK`'s `event` and `PLAYER_GAMEWEEK_STATS_GAMEWEEK`'s `fixture` already take). Columns:
`outcome` (`written` / `skipped_unchanged` / `failed`), `payload_hash`, `n_rows`, `error` —
enough to reconstruct what the job saw, not merely that it woke up.

**The heartbeat write can never take down the run it monitors.** `write_heartbeat_safely`
wraps the build-and-write step in a deliberately broad `except Exception`, called from
`main()` AFTER `run()` (whether `run()` succeeded or raised) and BEFORE `main()` returns —
attacked directly (fail-first, not merely asserted): the try/except was removed and a
mocked `store.write` failure, and a malformed `dataset_names` list, both confirmed to raise
without it, then confirmed swallowed with it restored
(`tests/test_snapshot_bootstrap.py::test_write_heartbeat_safely_swallows_a_store_write_
failure` / `::test_write_heartbeat_safely_swallows_a_build_failure_from_a_bad_dataset_name_
list`). The `main()` restructuring itself (heartbeat write must happen even when `run()`
raises, not only on the success path) was also fail-first proven, not assumed — see
`::test_main_writes_heartbeat_even_when_run_raises_and_returns_exit_1`.

### `scripts/check_heartbeat.py`

Reports the largest gap between consecutive heartbeats for a job over a window, and exits
non-zero if it exceeds `--max-gap-minutes` (required, never hardcoded — the threshold
depends on the job's own cadence, which this script has no way to infer):

```
python scripts\check_heartbeat.py --max-gap-minutes 45
python scripts\check_heartbeat.py --job snapshot_bootstrap --max-gap-minutes 45 --window-hours 12
```

Exit codes: `0` healthy, `1` at least one job's largest gap exceeds the threshold, `2` no
heartbeat data at all for the requested job(s) (never silently treated as "nothing to
report" — the brief was explicit that this itself is unhealthy).

**Critically, this looks only at `run_ts` — never at `outcome`.** A long run of
`skipped_unchanged` rows for a genuinely quiet API is completely healthy and must never be
reported as a gap; a gap in the *scheduler's own execution timestamps* is the only thing
that means the scheduler stopped. The largest-gap computation always includes the trailing
gap from the last known heartbeat to "now" (`compute_gap_report`'s `augmented = [*ts,
as_of]` sentinel), regardless of the `--window-hours` filter — this is the check that
actually catches "the scheduler stopped and nothing has run since" (a job with a perfect
history that simply stopped has no *large* gap anywhere in its own timestamp history except
this trailing one); fail-first proven by removing the sentinel and confirming both the
stalled-scheduler unit test and the `main()` CLI test fail without it.

**Verified live, 22 Aug 2026, against the real store**, reproducing the exact incident above:

```
python scripts\check_heartbeat.py --job snapshot_bootstrap --max-gap-minutes 45
```

reported **HEALTHY** — the `snapshot_bootstrap` heartbeat's own run-to-run cadence was
consistently ~30 minutes throughout the day, correctly NOT flagged as an outage, even though
`elements`/`events`/etc. themselves had gone many hours without a new row (a genuinely quiet
API). This is the exact false positive the feature exists to eliminate, confirmed against
real data rather than only a synthetic reproduction (`tests/test_check_heartbeat.py::test_
main_a_quiet_but_alive_scheduler_is_never_reported_as_an_outage` is the synthetic version of
the same scenario, run before the live check as a cheaper first pass).

### Recommended use

Run `check_heartbeat.py` before any deadline-adjacent manual step that depends on the
scheduler having been reliably alive (blueprint §3.4's "the one irreversible deadline" —
e.g. before Tuesday's post-GW1 EO sample) rather than trusting a Task Scheduler success icon
alone. `--job`/`--max-gap-minutes`/`--window-hours` are all real arguments, not fixed
defaults, because the right cadence differs pre- vs post-deadline (30 min vs 60 min per §1
above) and a second job adopting the same `heartbeat` dataset has its own cadence too.
**Updated `08-31`: `snapshot_odds.py` is now WIRED and running in production** — it writes a
heartbeat on *every* invocation including no-op runs (so a no-op is distinguishable from a
scheduler that never fired), fires hourly, and is deadline-gated; see §9 and
`docs/wiki/provider-framework.md` §14.8. Check it with
`check_heartbeat.py --job snapshot_odds --max-gap-minutes 90`. **`sample_picks.py` remains
unwired** — the `job` column exists specifically so that adoption needs no schema change, and
it is still the irreplaceable job running without a heartbeat.

---

## 7. `snapshot_gameweek_stats.py` — the current-season performance gap (session s005)

> Author: XL-Coder · session `s005` · 2026-08-30.

### The gap this closes

Every model in `fplai.models` trains off `vaastav_player_gameweek_stats` — real per-
player-per-gameweek PERFORMANCE (minutes, goals, cards, bonus, ...). That archive stops at
2025-26. Nothing in the store before this session persisted the equivalent for 2026/27, so
the system could produce a points PMF for a historical fixture but **not** for the upcoming
gameweek — CLAUDE.md's own line 116 names the cause in passing ("nothing persists
`event/{gw}/live/` to the store") without connecting it to this consequence.

`player.gameweek_stats@gameweek` already existed as a capability (`schemas.py`,
`PLAYER_GAMEWEEK_STATS_GAMEWEEK`) served only by `providers/vaastav.py`. This session adds
`providers/fpl.py::FPLProvider._fetch_gameweek_stats` as a **second provider of the same
capability** (blueprint §12.6 swappability) — not a new capability — writing to its own
dataset, `fpl_api_player_gameweek_stats`, so the two providers' rows are never mixed under
one dataset name. `fplai.gameweek_stats.read_player_gameweek_stats(store, as_of=..., season=
...)` is the capability-level reader that unions both, with a `source_provider` column so a
caller can always tell which one produced a row.

### The fixture-attribution problem, resolved

`event/{gw}/live/` gives two views of one gameweek, and neither alone matches vaastav's own
entity key `(season, round, element, fixture)`: `stats` is per-element-per-GAMEWEEK
(complete, but un-splittable across a double gameweek's two fixtures); `explain` is per-
FIXTURE (exact when present, but lists ONLY identifiers that scored a nonzero point value —
verified live against real GW1, the same finding `scripts/pin_dc_thresholds.py` already
recorded for `defensive_contribution` specifically).

Resolution (full reasoning and identifier classification in `providers/fpl.py`'s module
docstring):

- **One fixture this gameweek (the only case verified live so far — GW1 2026/27 had zero
  multi-fixture elements)**: the gameweek's own `stats` block IS that fixture's complete
  picture, used wholesale. `attribution_complete=True`.
- **Two or more fixtures (a double gameweek)**: one row PER FIXTURE, never dropped or
  aggregated (lesson 2 — vaastav's own entity key once omitted `fixture` and silently
  dropped 7,141 real double-gameweek rows). `total_points` and `minutes` are always exact.
  Linear-scoring identifiers (no divisor, no threshold) are exact when present in that
  fixture's `explain` block and correctly `0` when absent. Divisor/threshold identifiers
  (`saves`, `goals_conceded`, `defensive_contribution`) are exact when present and **NULL**
  (never guessed as `0`) when absent — a nonzero raw count for these can legitimately score
  zero points. `attribution_complete=False` on every row this path produces.
  **This path is UNVERIFIED against real data** — no double gameweek has occurred in
  2026/27 as of this session; it is exercised only by a synthetic payload in
  `tests/test_provider_fpl.py`.
- **Zero fixtures (blank gameweek for that element's team)**: no row, matching vaastav's own
  implicit behaviour.

### What this provider cannot give you

`selected`/`value` (ownership/price) are always `NULL` on this provider's rows — the live
API has no price/ownership HISTORY at all, which is literally the reason vaastav's archive
is a capability in the first place. Neither gap is silently filled — see `providers/fpl.py`
and `gameweek_stats.py`'s own docstrings for the column-alignment table in full.

## 8. `position`/`team` join — closing the load-bearing gap (session s005, continued)

> Author: XL-Coder · session `s005` · 2026-08-30.

### The gap

Section 7 shipped with `position`/`team` NULL on every FPL-API-sourced row —
`event/{gw}/live/` carries neither. **Every one of the six outcome models filters or groups
by position**, so the union reader could not yet feed a model for the current season even
after a mechanical `DATASET -> reader` migration. Flagged as load-bearing by the previous
session, not fixed there; this is the follow-up.

### Where the join lives, and why

Three places were possible: inside the provider (reading the store directly — rejected, no
provider in this codebase touches `fplai.store` for I/O, only `content_hash`; `PLProvider`'s
own precedent is taking already-resolved snapshots as plain DataFrames, followed here too);
inside `src/fplai/backfill.py`'s orchestrator (rejected outright — that file is READ-ONLY for
this story, and in any case `scripts/backfill.py`'s CLI never wires `--provider fpl_api`, so
nothing would actually exercise the fix); or in `scripts/snapshot_gameweek_stats.py` — the
ONE live entry point that produces `fpl_api_player_gameweek_stats` rows. Chosen: resolved
ONCE per gameweek (`fplai.gameweek_stats.resolve_elements_and_teams_as_of_deadline`) and
handed to `FPLProvider._fetch_gameweek_stats` as two already-resolved `elements`/`teams`
snapshots (`elements_as_of`/`teams_as_of`, both optional — omitting them reproduces the
pre-fix NULL behaviour). The resolved values are written into the stored rows — genuinely
"at ingest", not recomputed on every read.

### The bitemporal resolution — why `as_of()`, and the leakage this closes

`elements`/`teams` declare no `valid_time_column` (there is no per-row domain valid-time fact
for "a player's team" — only "what our snapshot cadence had observed by instant T"), so
`store.as_of(dataset, T)` (OBSERVED-time, `<=` inclusive) is the correct primitive, with `T`
the gameweek's own `deadline_time` (from `events`) — **never "today's" state**. Resolving
against today's `elements` would silently carry a transferred player's NEW club into a
historical gameweek's row — CLAUDE.md rule 2, made concrete, and exactly the trap lesson 4
(docs/HANDOFF.md) names: team codes persisting across seasons make historical resolution
*appear* to work, which is a property of the id space, not evidence that as-of resolution is
optional. `position` itself is FPL's live `element_types[].singular_name_short`, read off the
same already-fetched bootstrap-static payload (never hardcoded, CLAUDE.md rule 4) — an
`element_type` code absent from that list (e.g. a future season's Assistant-Manager-only
type) resolves to `None`, never silently mapped onto an outfield position.

### The leakage attack, and its result

No real player in the store has changed team since 2026-08-19 (the earliest snapshot), so
the attack is fabricated in a temp store, as the brief allowed: element 1 at team 1 in every
`elements` batch observed before a deadline, then team 2 in a batch observed after it. A
deadline strictly between the two batches resolves team 1 (`tests/test_gameweek_stats.py::
test_resolve_elements_and_teams_as_of_deadline_leakage_attack_picks_the_pre_deadline_team`);
a deadline after both resolves team 2 — proving genuine as-of resolution, not "always the
earliest row". A second, code-path-level attack in `tests/test_provider_fpl.py::
test_fetch_gameweek_stats_uses_as_of_snapshot_not_the_current_live_bootstrap_team` constructs
a live bootstrap payload where the CURRENT team differs from the AS-OF snapshot's team, and
asserts the row carries the AS-OF value — proving the provider does not take the shortcut of
reading `bs_el`/the live team, which are trivially reachable right next to the new code.
**Both tests were verified to fail against the pre-fix/leaky logic before this session
trusted them** (lesson 5) — the second one by temporarily reverting the resolution to read
`bootstrap["elements"]` directly and confirming the assertion failed with `'Current FC' !=
'Old FC'`, then restoring from a file copy (never `git stash`/`checkout`).

### VERIFIED live, 30 Aug 2026

- Re-ingested GW1 2026/27 with `--force-refetch` against the real store: **600 of 610
  elements (98.36%) resolved position/team**; the 10 unresolved are elements 601-610 —
  verified (cross-checked against the current `elements` snapshot) to be players added to
  bootstrap-static's `elements` list AFTER GW1's deadline (600 elements existed at the
  deadline; 623 exist now), so `event/1/live/` retroactively reports their stats but no
  pre-deadline `elements` snapshot could ever have known their position/team — a genuine,
  honest miss, not a bug. Two of the ten (Yalcouyé, Jebbison) actually played and scored a
  point; both correctly carry `position=None`/`team=None` rather than a guess.
- Manual spot-check against `bootstrap-static`: Raya (element 1) resolved to `GKP`/`Arsenal`,
  Gabriel (element 4) to `DEF`/`Arsenal` — both match `elements.element_type`/`team` and
  `teams.name` directly.
- **Scoring round-trip, strengthened**: every one of the 600 resolved rows fed through
  `src/fplai/scoring.py::score_outcome`, using the newly-resolved `position` to select BOTH
  the per-position scoring bands (goals/clean sheets) AND the correct `DCThresholdSet` group
  (`fplai.models.defensive_contribution.build_dc_threshold_set`) — **600/600 exact** against
  FPL's own `total_points`. This is a strictly stronger check than the previous session's
  610/610 (which only proved `total_points` survives the union, not that `position` itself is
  correct) — an incorrect position would misprice DC/clean-sheet points and this would have
  caught it.
- Full suite (baseline **1022 passed, 0 failed**): re-run after this change — see this
  session's punch-card for the final count.

### Usage

```
python scripts\snapshot_gameweek_stats.py --gw 1 --season 2026-27 [-v] [--store-path PATH] [--force-refetch]
```

`--gw`/`--season` are both required, never inferred (same convention `pin_dc_thresholds.py`
already established for this identical payload — the live API carries no season field
anywhere). Gated on `finished && data_checked`; an unsettled gameweek exits **cleanly (code
0)** — the `sample_picks.py` convention, retry later rather than fail. A settled gameweek is
immutable, so the script also skips (clean exit, before any live call) a `(season, gw)` this
store already has rows for, unless `--force-refetch` is passed.

### VERIFIED live, 30 Aug 2026

- GW1 2026/27 (`finished=True, data_checked=True`): **610/610 elements**, entity key
  `(season, round, element, fixture)` unique across the real batch (measured, not asserted —
  lesson 2's direct test), zero double/blank-gameweek elements.
- **Scoring round-trip**: every row's own extracted fields (`goals_scored`, `assists`,
  `clean_sheets`, `goals_conceded`, `saves`, `defensive_contribution`, cards, bonus, ...),
  fed back through `src/fplai/scoring.py`'s `score_outcome` (independently verified 610/610
  against FPL's own `total_points` this same session) — **610/610 exact**, both on the
  in-memory fetch and after a real write-then-read-back through the store via
  `read_player_gameweek_stats`.
- GW2 (deadline 2026-08-28) had **not** settled as of this verification
  (`finished=False, data_checked=False`) — `snapshot_gameweek_stats.py --gw 2 --season
  2026-27` was run live and confirmed to exit cleanly (code 0), the expected behaviour for
  an in-progress gameweek, not a bug.
- Live-persisted to the real store: `data\store\fpl_api_player_gameweek_stats\` now holds
  GW1's 610 rows, written via `scripts/snapshot_gameweek_stats.py --gw 1 --season 2026-27`.

### What was deliberately NOT done this session

No model was migrated onto `read_player_gameweek_stats` — all six still hardcode `DATASET =
"vaastav_player_gameweek_stats"`. Two of the six (`minutes.py`, `defensive_contribution.py`)
were owned by a parallel session-`s005` story at the time this one ran; migrating any of the
six is a separate, largely mechanical follow-up — see `gameweek_stats.py`'s own docstring,
"What a migration would still need to do", for exactly what that follow-up must decide
(most importantly: the `position`/`team` join above, and what `attribution_complete=False`
should mean for a model's own gate).

## 9. `was_home`/`opponent_team` join — closing the second leak (session `s005`, continued)

> Author: XL-Coder · session `s005` · 2026-08-30.

### The gap

Section 8 named this out of its own scope on the way out: `was_home`/`opponent_team` were
still derived from `bs_el.get("team")` — the element's CURRENT bootstrap-static team,
refetched fresh on every ingest call — never a per-gameweek snapshot. This matters more than
`position`/`team` did: `opponent_team` is a direct model feature (`team_strength` keys on it,
and every fixture-difficulty notion downstream flows from it), where `position` is closer to
a stable descriptive attribute.

### Audit: the leak was LIVE, not latent

Compared the real GW1 2026/27 store's pre-fix `was_home`/`opponent_team` (computed at the
2026-08-30 `--force-refetch` re-ingest, ~9 days after the GW1 deadline) against the
GW1-deadline-resolved team via `resolve_elements_and_teams_as_of_deadline`. **8 real elements**
had already moved clubs in that 9-day window — a genuine, real transfer-window/deadline-day
event, not a data artefact (Ethan Pinnock Brentford→Coventry City, Carlos Baleba
Brighton→Man Utd, Axel Disasi/Nicolas Jackson/Liam Delap all Chelsea→elsewhere, Omar
Marmoush/Sávio/Nico González all Man City→elsewhere).

**Concrete wrong values on the real stored row, before this fix**, for 5 of the 8 (the other 3
happened to land on the same numeric answer by coincidence — neither their current nor
deadline team matched the fixture's home side, so both paths hit `_fixture_home_away`'s
"not home" default):

| Element | Deadline team (correct) | Fixture (GW1) | Pre-fix stored | Correct (post-fix) |
|---|---|---|---|---|
| Pinnock (91) | Brentford | Brentford(H) v Spurs | `was_home=False, opponent=4` (his OWN club) | `was_home=True, opponent=19` (Spurs) |
| Baleba (131) | Brighton | Brighton(H) v Aston Villa | `was_home=False, opponent=5` (own club) | `was_home=True, opponent=2` (Aston Villa) |
| Marmoush (401) | Man City | Man City(H) v Bournemouth | `was_home=False, opponent=15` (own club) | `was_home=True, opponent=3` (Bournemouth) |

The pattern is the bug made visible: whenever the current-live team didn't match the
`opponent_team` default fallback (`fixture.team_h`), the row's own `opponent_team` ended up
naming the player's **own club** as their opponent.

### The fix

Same shape as the `position`/`team` fix: `_resolved_team_id(element_id, elements_as_of_by_id)`
reads the element's team from the SAME already-resolved, AS-OF-THE-DEADLINE `elements_as_of`
snapshot the position/team join already built — never `bs_el`. `_fixture_home_away(fixture,
team)` now takes that resolved id (or `None`) and returns `(None, None)`, never a guess, when
it is unresolved (no snapshot supplied, or this element absent from it — same as the 10
elements added to bootstrap-static after the GW1 deadline, section 8). A new
`n_was_home_opponent_unresolved` meta counter mirrors `n_position_team_unresolved`.

**Deliberately NOT touched**: fixture *attribution* for a genuinely unused player (empty
`explain` block) still falls back to the CURRENT bootstrap team to find which fixture(s) that
team played this gameweek — there is no as-of-deadline fixture history to look up instead
(fixtures come from one live `fixtures/?event=` call, not a bitemporal store read). This is a
narrower residual risk than the one closed here (it can only bite a player who BOTH
transferred AND did not play at all that gameweek), and is still flagged, separately, in the
module docstring for a future session.

### The leakage attack, and its result

Two tests, both **verified to fail against the pre-fix logic first** (copied `providers/
fpl.py` aside, reverted `_fixture_home_away`'s None-guard and the deadline-resolved call
site, ran the tests, confirmed both failed with the exact wrong values, restored from the
copy, `diff -q` confirmed byte-identical restoration, re-ran green):

- `tests/test_provider_fpl.py::test_fetch_gameweek_stats_was_home_opponent_team_uses_deadline_resolved_team_not_current`
  — the direct attack: current/live team (1) and deadline-resolved team (7) are BOTH real
  sides of the one fixture in the test payload, so a current-team read produces a
  **different, wrong** answer rather than coincidentally agreeing. Proves the fix picks the
  deadline-resolved side.
- `tests/test_provider_fpl.py::test_fetch_gameweek_stats_uses_as_of_snapshot_not_the_current_live_bootstrap_team`
  — extended (it already attacked `position`/`team`) to also assert `was_home`/`opponent_team`
  never resolve to the value the CURRENT live team would have produced.

### VERIFIED live, 30 Aug 2026

- Real GW1 store re-ingested with `--force-refetch`: **610 rows, unchanged count** — no row
  was lost by this fix. The 8 known-mismatched elements above now carry the deadline-correct
  values (verified against a live `fixtures/?event=1` call and `bootstrap-static`'s team
  names, table above). The 10 position/team-unresolved elements (section 8) now also
  correctly carry `was_home=None`/`opponent_team=None` instead of an unverifiable guess —
  `gw_rows.filter(pl.col("was_home").is_null()).height == 10`, exactly the same 10 elements
  (601-610).
- **Scoring round-trip re-run after the fix and re-ingest**: all 600 position-resolved rows,
  same method as section 8 (per-position scoring bands + `DCThresholdSet` group) — **600/600
  exact** against FPL's own `total_points`, unchanged (this fix does not touch any scoring
  identifier).
- Full suite: see this session's punch-card for the post-fix count.
