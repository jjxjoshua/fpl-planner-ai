# Provider framework — E2b stories 1-5

> Author: XL-Coder · session `s001` · 2026-08-20
> Implements blueprint §12 (provider abstraction), stories 1-5 of PROGRESS.md's E2b epic.
> Stories 6-11 (PL API adapter, identity resolution, odds adapter, derived-capability
> framework, archive adapters, backfill orchestrator) are **not** built here — see §5.

## 0. Why this exists

Blueprint §12's diagram:

```
domain query        find_player_match_defensive_actions(player, match)
      |
capability registry which providers serve THIS capability at THIS granularity,
      |             for THIS season/competition?  -> ordered candidates
provider adapter    fetch + normalise to the canonical schema
      |
transport           per-provider rate policy, cache, backoff
      |
bitemporal store    rows + provenance + observed_at
```

This slice builds every layer except the domain-query layer itself (no model or backtest
code exists yet to issue one), against exactly one provider — the FPL API — because that
provider is already live, tested, and running on a schedule. The point of story 5 is
proving the abstraction against code that already works, not against something new and
unverified.

## 1. The five pieces, and where they live

| Story | What | File |
|---|---|---|
| 1 | Canonical schemas | `src/fplai/schemas.py` |
| 2 | Capability registry | `src/fplai/registry.py` |
| 3 | Provider interface | `src/fplai/providers/base.py` |
| 4 | Transport | `src/fplai/transport.py` |
| 5 | FPL provider adapter | `src/fplai/providers/fpl.py` |

### 1.1 Canonical schemas (`schemas.py`)

`CapabilityKey(entity, measure, grain)` is a frozen dataclass — structurally hashable, so
it's usable as a dict key everywhere below. Grain is part of the key, not an attribute of
it: `CapabilityKey("player", "defensive_actions", "match")` and
`CapabilityKey("player", "defensive_actions", "season")` are different keys with different
hashes. There is no code path that could accidentally treat one as the other — that's the
specific bug blueprint §12.1 names.

`FactTableSchema` is the canonical shape of one capability's rows: which column(s) identify
an entity (`entity_key`, empty tuple for a singleton), and the *minimum* fields every
provider serving this capability must supply (`required_fields` — deliberately not the
full field list; FPL's `elements` payload carries 109+ fields and every ingest script keeps
all of them). `is_modelled` exists on the dataclass, unused in this slice, for story 9's
derived-capability framework.

`CANONICAL_SCHEMAS: dict[CapabilityKey, FactTableSchema]` holds exactly the seven
capabilities this slice's provider serves — see §2.

### 1.2 Capability registry (`registry.py`)

`CapabilityRegistry.register(capability, provider, coverage)` is the entire write API.
`CoverageSpec(seasons, competitions, priority)` is **declared data** — a provider states
what it covers; the registry never branches on a provider's name or type to guess. Reading:

- `candidates(capability, season=..., competition=...)` — every matching provider, ordered
  by priority, never raises (empty list if nothing matches).
- `resolve(capability, ...)` — the loud-failure entry point. Raises `RegistryError` if
  nothing matches. **Logs every selection**, and specifically logs when there was more than
  one candidate and one was chosen over the others (blueprint §12.3 — a silent fallback to
  a worse source is the failure this module exists to prevent). Only one provider exists in
  this slice, so no real fallback is exercised yet — `tests/test_registry.py` proves the
  logging path with a synthetic two-provider case ahead of story 6 needing it for real.

### 1.3 Provider interface (`providers/base.py`)

`Provider` is a `typing.Protocol`, not an ABC. A provider satisfies it by having
`provider_id: str`, `policy: TransportPolicy`, `supports(capability, season=, competition=)
-> bool`, and `fetch(capability, **params) -> FetchResult` — nothing about a base URL or a
session. That matters specifically for story 10 (archive adapters): a provider that reads a
bulk CSV has no request-per-second model at all, and must not be forced to fake one just to
satisfy an inheritance hierarchy built for HTTP.

`FetchResult` is what every `fetch()` returns: `rows` (already in canonical shape — no
provider-specific field name should ever reach a caller; blueprint §12.6), plus the five
mandatory provenance fields from blueprint §12.3: `provider_id`, `endpoint`, `observed_at`,
`content_hash`, `capability`. `is_modelled` and a free-form `meta` dict round it out.

### 1.4 Transport (`transport.py`)

Four policy shapes, matching blueprint §12.4's table exactly:

```python
RatePolicy(requests_per_second, jitter_fraction)       # FPL API, PL API
DailyQuotaPolicy(requests_per_day, requests_per_second) # API-Football
CreditPolicy(monthly_credits, cost_per_call_description) # The Odds API
BulkFilePolicy(description)                              # vaastav, olbauday
```

`RateLimiter`, `ResponseCache`, `CacheEntry` were moved here **verbatim** from `client.py` —
same code, same behaviour, now defined once. `client.py` imports them back
(`from fplai.transport import RateLimiter, ResponseCache, ...`), so every existing import
site (including `tests/test_client.py`, which imports `RateLimiter`/`ResponseCache` directly
from `fplai.client`) keeps working unchanged.

`QuotaTracker` (daily, resets at UTC midnight) and `CreditTracker` (monthly, resets at UTC
month start) are new. Both raise `TransportError` rather than silently over-spending — a
provider that quietly exceeds its quota risks a ban, which is worse than a loud stop.

`HttpTransport` is a new, generic, policy-dispatching HTTP engine: cache-first, rate-limits
or quota/credit-checks before a live request, retries with exponential backoff on
configurable trigger statuses. **It is not wired into `FPLClient` in this slice** — see §3
for why, and for what the next HTTP provider should actually build on.

### 1.5 FPL provider adapter (`providers/fpl.py`) — the worked example

```python
provider = FPLProvider(client=FPLClient())
provider.provider_id   # "fpl_api"
provider.policy         # RatePolicy(requests_per_second=2.0, jitter_fraction=0.20)

result = provider.fetch(PLAYER_ATTRIBUTES_CURRENT, force_refresh=True)
result.rows             # polars DataFrame, canonical shape, schema-validated
result.provider_id      # "fpl_api"
result.endpoint         # "bootstrap-static/"
result.content_hash     # sha256 of the payload, order-independent
```

Registration:

```python
from fplai.registry import CapabilityRegistry
from fplai.providers.fpl import register

registry = CapabilityRegistry()
register(registry)  # adapter + registry entry — the whole story

provider = registry.resolve(PLAYER_ATTRIBUTES_CURRENT)
```

## 2. Capabilities served in this slice

Exactly the seven writes the two existing ingest scripts already make — no speculative
surface with no adapter or test behind it:

| Capability | Source | Dataset | Grain |
|---|---|---|---|
| `player.attributes@current` | bootstrap-static `elements` | `elements` | current, no history |
| `team.attributes@current` | bootstrap-static `teams` | `teams` | current |
| `gameweek.attributes@season` | bootstrap-static `events` | `events` | whole season, one call |
| `chip.window@season` | bootstrap-static `chips` | `chips` | whole season |
| `game.config@current` | bootstrap-static `game_config` | `game_config` | singleton |
| `game.settings@current` | bootstrap-static `game_settings` | `game_settings` | singleton |
| `manager_picks.selection@gameweek` | `entry/{id}/event/{gw}/picks/` | `picks` | per (entry, gameweek) |

`fixtures`, `entry` (manager profile), `event_live`, `event_status` remain reachable on
`FPLClient` directly, unwrapped — nothing in this slice persists them to the store, so
wrapping them as capabilities now would be surface with no adapter test behind it. Adding
them later is exactly the "adapter method + registry entry" pattern below, nothing more.

## 3. The `fetch_batch` regression, and why `FPLClient` wasn't rewired onto `HttpTransport`

Two design decisions worth recording so a future reviewer doesn't "fix" either one:

**`fetch_batch()` exists because `bootstrap-static/` answers six capabilities in one call.**
A naive port would have each capability's `fetch()` call `client.bootstrap_static()`
independently — fine for `force_refresh=False` (the client's own URL cache absorbs it), but
`snapshot_bootstrap.py` needs `force_refresh=True` (a schedule that silently re-served a
cached response would defeat the point of running on a schedule at all), and
`force_refresh=True` bypasses that cache. Calling `fetch()` six times with
`force_refresh=True` would have made **six live HTTP requests instead of one**, every 30
minutes, during exactly the window rate discipline matters most (CLAUDE.md, "the one
irreversible deadline"). `fetch_batch(capabilities, force_refresh=True)` makes the ONE
upstream call and slices it six ways — `tests/test_provider_fpl.py::test_fetch_batch_makes_exactly_one_upstream_call`
asserts this directly, and the live verification run below confirms it (`GET
.../bootstrap-static/ HTTP/1.1" 200` appears exactly once per run).

**`FPLClient`'s own request loop (`_get`) was deliberately left as-is, not rewired onto the
new `HttpTransport`.** It already does everything `HttpTransport` does (rate limit, cache,
backoff) — the only difference is that the logic now lives in two places (`client.py`'s
`_get`, and `transport.py`'s `HttpTransport.get`) instead of one. Consolidating them was
judged not worth the risk: `FPLClient` is running in production on a 30-minute Task
Scheduler cadence capturing ownership data ahead of the GW1 deadline, and rewriting its
request loop during that window for a code-cleanliness win, with no user-visible benefit,
is exactly the kind of change the task brief said to stop and report rather than do. This is
recorded as a **finding**, not a silent deviation: `HttpTransport` is the shared engine the
*next* HTTP-based provider (PL API, story 6) should build on directly; `FPLClient` keeps its
own working implementation. Revisit consolidating them once there's no live-capture pressure
riding on `FPLClient` staying exactly as tested.

## 4. Adding a new provider — the steps, worked against a hypothetical PL API adapter

1. **Add any new capability keys and their canonical schema** to `schemas.py`, if the new
   provider serves something not already covered (e.g. `player.lineup@match`). If it serves
   an *existing* capability (e.g. a second source of `team.attributes@current`), skip this —
   the schema already exists and both providers validate against the same one.
2. **Write the adapter** (`providers/pl_api.py`, by analogy with `providers/fpl.py`):
   a class with `provider_id`, `policy` (a `RatePolicy` — PL API has the same 2 req/s, no
   published quota, shape as the FPL API per docs/wiki/provider-evaluation.md §2.4), and a
   `fetch()` that calls the PL API (via `transport.HttpTransport`, not a bespoke loop — see
   §3) and normalises the JSON response into the capability's canonical `pl.DataFrame`
   shape before returning a `FetchResult`.
3. **Register it**: `register(registry, coverage=CoverageSpec(seasons=frozenset({"2010-11",
   ..., "2026-27"}), competitions=frozenset({"PL"}), priority=...))`.
4. **Nothing else changes.** `schemas.py`'s `FactTableSchema`/`CapabilityKey`,
   `registry.py`'s `CapabilityRegistry`, and `providers/base.py`'s `Provider`/`FetchResult`
   are all provider-agnostic already — no core code branches on which provider is being
   added.

That is the E2b gate, stated as steps rather than a claim.

## 5. Deliberately not built here (stories 6-11)

- **Story 6 (PL API adapter)** and **Story 7 (identity resolution)** — **BUILT**, 2026-08-20,
  by XL-Coder. See §9 below for the full record: `src/fplai/providers/pl.py`,
  `src/fplai/identity.py`, `tests/test_provider_pl.py`, `tests/test_identity.py`. The ToU
  question referenced below was resolved by the user (blueprint §3.6) before this work started.
- **Story 8 (Odds adapter)** — not built. `CreditPolicy`/`CreditTracker` exist and are
  tested standalone (see `test_transport.py`), ready for it.
- **Story 9 (derived-capability framework)** — not built. `FactTableSchema.is_modelled`
  exists, defaults `False`, and is exercised by a dedicated test
  (`test_is_modelled_defaults_false_and_is_settable`) but nothing in this slice sets it
  `True` — there is no derived capability yet.
- **Story 10 (archive adapters)** — **BUILT**, 2026-08-20/21, by XL-Coder. See §10 below:
  `src/fplai/decoders.py` (new module), `src/fplai/transport.py`'s new `FileTransport`/
  `FileCache`, `src/fplai/providers/vaastav.py` (new adapter), `fplai.identity.
  build_player_identity_map_for_season` (new, season-scoped resolution). olbauday
  (same pattern) remains a quick follow-up, explicitly not built here.
- **Story 11 (backfill orchestrator)** — **BUILT**, 2026-08-21, by XL-Coder. See §11 below:
  `src/fplai/backfill.py` (new module), `tests/test_backfill.py`, `scripts/backfill.py`.
  Answers §9.3's open question (identity failure vs upstream absence); also surfaces, live,
  that TEAM identity is still NOT season-scoped (§10.4's open item) — see §11.4.

## 6. Live verification (2026-08-20, post-refactor)

`scripts/snapshot_bootstrap.py` run directly against the **real** `data/store/` (not a temp
directory — this store has been accumulating real snapshots since 2026-08-19, on the live
30-minute Task Scheduler cadence, ahead of the GW1 deadline):

```
2026-08-20 21:58:59,310 DEBUG urllib3.connectionpool: Starting new HTTPS connection (1): fantasy.premierleague.com:443
2026-08-20 21:58:59,501 DEBUG urllib3.connectionpool: https://fantasy.premierleague.com:443 "GET /api/bootstrap-static/ HTTP/1.1" 200 127774
2026-08-20 21:58:59,742 INFO fplai.snapshot_bootstrap: elements       WROTE                  rows=595   hash=6bee79c2cd49
2026-08-20 21:58:59,742 INFO fplai.snapshot_bootstrap: teams          unchanged, skipped     rows=20    hash=52be934dcc6a
2026-08-20 21:58:59,742 INFO fplai.snapshot_bootstrap: events         unchanged, skipped     rows=38    hash=5a08aed190d3
2026-08-20 21:58:59,742 INFO fplai.snapshot_bootstrap: chips          unchanged, skipped     rows=8     hash=eaf212d27793
2026-08-20 21:58:59,742 INFO fplai.snapshot_bootstrap: game_config    unchanged, skipped     rows=1     hash=1437bda35a76
2026-08-20 21:58:59,742 INFO fplai.snapshot_bootstrap: game_settings  unchanged, skipped     rows=1     hash=420cb66383ed
```

Exactly ONE `GET .../bootstrap-static/` — confirms `fetch_batch` did not multiply requests.
`elements` WROTE (real price/ownership drift since the last scheduled run); the other five
correctly skipped as unchanged — idempotency intact.

The exact Task Scheduler entry point, invoked the same way the schedule invokes it:

```
$ cmd /c scripts\run_snapshot_bootstrap.bat
EXIT CODE: 0
2026-08-20 21:59:33,355 INFO fplai.snapshot_bootstrap: elements       unchanged, skipped     rows=595   hash=6bee79c2cd49
2026-08-20 21:59:33,355 INFO fplai.snapshot_bootstrap: teams          unchanged, skipped     rows=20    hash=52be934dcc6a
2026-08-20 21:59:33,355 INFO fplai.snapshot_bootstrap: events         unchanged, skipped     rows=38    hash=5a08aed190d3
2026-08-20 21:59:33,355 INFO fplai.snapshot_bootstrap: chips          unchanged, skipped     rows=8     hash=eaf212d27793
2026-08-20 21:59:33,355 INFO fplai.snapshot_bootstrap: game_config    unchanged, skipped     rows=1     hash=1437bda35a76
2026-08-20 21:59:33,356 INFO fplai.snapshot_bootstrap: game_settings  unchanged, skipped     rows=1     hash=420cb66383ed
```

### The schema-evolution risk this refactor introduced, and how it was closed

Adding `provider_id`/`capability`/`endpoint` columns to `store.write()` means new batches
have three columns that batches written before this session (already on disk, from the live
30-minute schedule that has been running since 2026-08-19) do not have at all. Verified
**before** shipping the change that this actually breaks a naive `read_parquet` glob across
old + new files:

```
Invalid Input Error: ... schema mismatch in glob: column "provider_id" was read from ...
"new.parquet", but could not be found in file "...old.parquet".
If you are trying to read files with different schemas, try setting union_by_name=True
```

Both `read_parquet(...)` calls in `store.py` (`as_of()` and `_latest_content_hash()`) now
pass `union_by_name=true`, which fills a missing column with `NULL` for the older files
instead of raising. Verified against the real store after the live run above:

```python
>>> store.latest("elements", entity_key="id")["provider_id"].n_unique()
1  # all 595 rows are from the run that just wrote provider_id="fpl_api"

>>> store.as_of("elements", datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc), latest_only=True, entity_key="id")["provider_id"].unique().to_list()
[None]  # pre-refactor batches, read correctly, no leakage across the cutoff
```

The `as_of` cutoff correctly excludes the new batch and returns only the pre-refactor rows
(`provider_id=None`, as those rows genuinely have no provenance recorded) — both the
schema-evolution fix and the bitemporal cutoff are proven together, live, on the real store.

`scripts/sample_picks.py` was intentionally left unmodified (see PROGRESS.md / session
report) and re-verified still exits cleanly pre-season:

```
$ python scripts/sample_picks.py --dry-run -v
INFO fplai.sample_picks: no gameweek has finished yet (pre-season) — nothing to sample. Exiting cleanly.
EXIT: 0
```

## 7. Test coverage added this session

`tests/test_schemas.py` (10), `tests/test_transport.py` (17), `tests/test_registry.py` (9),
`tests/test_provider_fpl.py` (17), plus 6 new cases appended to `tests/test_store.py` for
provenance columns and the old/new schema-union scenario. **104 tests total, all passing**
(49 pre-existing + 55 new).

## 8. `as_of()` returns STATE, not the stream — E2b story 12, 2026-08-20

**The bug.** `as_of('elements', now)` on the live store returned **7,735 rows: 595 players
x 13 snapshots** — every observation with `observed_at <= t`, not the state at `t`. It never
raised. A caller that forgot to deduplicate would train on rows duplicated by however many
times each entity happened to be snapshotted — silent corruption, exactly the class of bug
blueprint §3.2 exists to prevent. Full decision recorded in blueprint §3.2, subsection
"`as_of` returns STATE, not the observation stream" — this section is the implementation
record, not a second copy of the decision.

### 8.1 The design

**Entity keys are declared data, not a runtime argument.** `fplai.schemas.
DATASET_ENTITY_KEYS: dict[str, tuple[str, ...]]` maps a *store dataset name* (`"elements"`,
the Parquet subdirectory under `data/store/`) to the column(s) that identify one entity —
`()` for a singleton. It is built from `CANONICAL_SCHEMAS`'s existing `entity_key` field
rather than duplicated, so there is exactly one place a key is declared. `store.py` imports
this dict; `as_of()` looks up the caller's dataset name in it and raises `BitemporalError`
(naming the dataset, and how to declare it) if it isn't there — it never accepts a
caller-supplied `entity_key` override any more. That override was the "guessing" surface:
a caller could ask `as_of()` to collapse on the wrong column and nothing would catch it. Now
there is exactly one place a key can be wrong (the declaration itself), and it is reviewable
in one file.

**`as_of(dataset, t)`** now always returns STATE: `SELECT * FROM (... WHERE observed_at <=
t) QUALIFY ROW_NUMBER() OVER (PARTITION BY <entity_key> ORDER BY observed_at DESC, batch_id
DESC) = 1`. For a singleton the `PARTITION BY` clause is omitted entirely, which correctly
collapses the whole result set to one row. **`observations(dataset, until=t)`** is the new
explicit opt-in for the raw stream — literally the query `as_of()` used to run, module-level
`_require_utc`/`_dataset_exists` guards and all. It never needs a declared key, since there
is no state to collapse to and therefore nothing to guess.

**Tie-break.** Two rows for the same entity key can share the same `observed_at` (same-batch
writes, or coarse test timestamps). The tie breaks on `batch_id DESC` — `batch_id` carries no
semantic meaning, but it is generated once at write time and persisted, so the break is
**stable**: the same on-disk data always produces the same winner on repeated queries
(CLAUDE.md rule 7 — deterministic and seeded). This is documented as "arbitrary but stable,"
not "correct" — nothing about which of two simultaneous writes should win is a business rule
worth inventing.

**Disappearing entities persist in state.** An entity observed in an earlier batch that stops
appearing in a later one (e.g. a player purged from a provider's payload) is **not** treated
as deleted. This append-only store has no tombstone event, so `as_of()` keeps returning its
last known row indefinitely rather than silently dropping it. This was a deliberate choice
between two options:

- **Persist (chosen).** Requires no new machinery — it falls out of the window function
  naturally (`ROW_NUMBER()` only ever sees rows that exist; an entity's last row stays the
  latest row for that key regardless of what other batches contain).
- **Drop, inferred from absence.** Would require deciding, per dataset, whether a given batch
  is a *complete* snapshot (so absence means deletion) or a *partial* one (a sample, where
  absence means nothing). `elements`/`teams`/`events`/`chips` are full bootstrap-static dumps
  each time, so this would be defensible there — but `picks` is a sample of ~10k entries out
  of the full registered population, and a sampled entry's absence from one draw means
  nothing about whether it still exists. Building "batch completeness" as a second axis of
  declared metadata, on top of the entity key, was judged more design surface than the E2b
  story asked for, and a wrong guess there (treating a sample as a complete snapshot) is
  exactly the "guessing" failure mode §3.2 exists to close off. If a genuine deletion needs
  modelling later, it should be an explicit tombstone row, not inferred from absence.

**`picks`' entity key gained `event`.** `CANONICAL_SCHEMAS[MANAGER_PICKS_SELECTION_GAMEWEEK].
entity_key` was `("entry_id", "element")` — missing the gameweek. That was always a latent
bug (nothing exercised it, because nothing called `as_of()`/`latest()` with `entity_key=`
on `picks` before this story), and would have caused exactly the scenario the store must
never allow: state-collapsing a manager's GW3 picks and GW9 picks for the same player into
one row, silently discarding the losing gameweek. Fixed to `("entry_id", "event",
"element")`. `required_fields` already listed `event`, so no schema-validation surface
changed — only which column combination identifies "one entity."

**`latest()`** is unchanged in spirit — `as_of(dataset, now)` — but its `entity_key=`
parameter is gone along with `as_of()`'s, for the same reason.

### 8.2 Caller audit

Searched `src/` and `scripts/` for every call site of `as_of`, `latest`, and (new)
`observations`. **Zero production callers existed before this story** — `scripts/
snapshot_bootstrap.py` and `scripts/sample_picks.py` only ever call `store.write(...)`;
`src/fplai/providers/fpl.py` imports `content_hash` from `store.py`, nothing read-related.
The only call sites of the old `as_of(..., latest_only=..., entity_key=...)` signature were
in `tests/test_store.py` itself. This means the story closed the bug **before** any real
training/backtest code existed to be silently corrupted by it — the earliest point it could
have been caught.

### 8.3 Production verification, 2026-08-20 (post-fix)

```
elements       as_of=   599  observations=  8334
teams          as_of=    20  observations=    20
events         as_of=    38  observations=    38
chips          as_of=     8  observations=     8
game_config    as_of=     1  observations=     2
game_settings  as_of=     1  observations=     1
leakage check as_of('elements', 2020-01-01) -> 0 rows
```

`elements` state is 599 (up from the 595 at the time the bug was found — genuine new players
registered since), stream is 8,334 across every 30-minute snapshot batch since 2026-08-19 —
confirms `as_of()` collapses correctly against real accumulated drift, not just synthetic
test data. `teams`/`events`/`chips` `as_of == observations` because only one batch of each
has ever been written (no content change yet this season) — expected, not a bug.
`game_config` shows the singleton collapse working against two real batches. Leakage guard
holds.

`scripts/snapshot_bootstrap.py` (both `python scripts/snapshot_bootstrap.py -v` directly and
the exact `.venv\Scripts\python.exe scripts\snapshot_bootstrap.py` command `run_snapshot_
bootstrap.bat`'s Task Scheduler entry runs) still exits 0 and logs all six datasets after
this change — write path was not touched, per the story's hard constraint.

### 8.4 Tests

118 total (105 pre-existing + 13 new), all passing, `uv run pytest -q`. Three pre-existing
tests were rewritten because they asserted the pre-decision semantics as correct behaviour,
not because they were weakened:

- `test_as_of_latest_only_collapses_to_one_row_per_entity` → `test_as_of_collapses_to_one_
  row_per_declared_entity_key`: dropped the `latest_only=True, entity_key="id"` call-site
  argument now that `as_of()` resolves the key itself; same assertion.
- `test_as_of_latest_only_requires_entity_key` → `test_as_of_raises_for_dataset_with_no_
  declared_entity_key`: the old test asserted that calling `as_of(..., latest_only=True)`
  *without* an `entity_key` argument raised — that whole calling convention is gone. Rewritten
  to assert the actual new failure mode: a dataset absent from `DATASET_ENTITY_KEYS` raises,
  naming the dataset.
- `test_as_of_without_latest_only_returns_every_historical_batch` → `test_observations_
  returns_every_historical_batch_raw_stream`: this test's premise — that plain `as_of()` on
  `picks` returns the raw stream because there's "no single current row per entity to collapse
  to" — is precisely the bug blueprint §3.2 closed. The raw-stream behaviour it protects still
  exists and is still tested, just via `observations()`, which is what a caller wanting the
  stream must now ask for explicitly.

## 9. Premier League API adapter + identity resolution — E2b stories 6-7, 2026-08-20

> Author: XL-Coder · session `s001`
> `src/fplai/providers/pl.py` (adapter), `src/fplai/identity.py` (new module),
> `tests/test_provider_pl.py`, `tests/test_identity.py`, `scripts/verify_pl_provider.py`.
> Five new capabilities in `schemas.py`: `match.lineups@match`, `match.substitutions@match`,
> `team.match_stats@match`, `player.season_stats@season`, `match.fixtures@matchweek`.
> `player.defensive_actions@match` deliberately left unregistered (no source exists —
> `docs/wiki/provider-evaluation.md`'s "Architect verification" section, "P2 verdict:
> NEGATIVE").

### 9.1 Did the framework hold?

**Yes, for the adapter itself** — `providers/pl.py` + a `register()` call, zero changes to
`registry.py` or `providers/base.py`, exactly §4's steps. **One real addition to core**:
`src/fplai/identity.py`, a new module. That is a deliberate scope call, not a workaround —
§4's steps say nothing about identity because story 5's FPL-only slice never needed
cross-provider identity at all (`opta_code` didn't exist as a join key until this story had
two providers to join). Blueprint §12.5 ("a per-provider ID map resolves to canonical
entities... unmatched entities raise") is a *general* requirement, not `providers/pl.py`'s
alone — the next non-FPL provider (odds, story 8; archives, story 10) will need the same
raise-loudly discipline, so building it once, shared, was judged the honest reading of "an
adapter plus a registry entry" rather than a violation of it. Flagging this plainly per the
brief's request for honesty rather than silently expanding scope.

Two bugs were found and fixed only by actually running the adapter live — both are now
tests, not just narrative:

1. **`fastestPlayer` is not a scalar.** `/v3/matches/{id}/stats` returned
   `{"topSpeed": 35.53, "playerId": "592031"}` for one of its 185 keys; a naive
   `float(value)` crashed on it. Fixed with `value`/`value_raw` — `value_raw` is a JSON-encoded
   escape hatch for any of the 185 keys that isn't a plain number, so nothing is silently
   dropped (CLAUDE.md rule 5's spirit — never discard a model input at a module boundary — 
   applied to a value the raw API handed us, not a distribution).
2. **Team-identity bootstrap season != data-fetch season.** Building `TeamIdentityMap` from a
   *different* season's fixture list than the one the `elements`/`teams` snapshot describes
   raises correctly (§12.5) but for the wrong reason if a caller conflates the two — see §9.3.
   Fixed by splitting `PLProvider.season` (default season for data fetches, overridable
   per-call) from `PLProvider.identity_season` (which season's fixture list verifies team
   identity; must match the snapshot's season). Both live findings are recorded in
   `providers/pl.py`'s docstrings so a future reader hits the explanation before the bug.

### 9.2 Identity resolution

**Players — free, verified 599/599, raises on any miss.** `elements[].code` (int) IS the PL
API's numeric player id; `opta_code` is `"p" + code`. Verified live against the full current
bootstrap-static: **599/599 elements**, zero `opta_code`-format mismatches.
`PlayerIdentityMap.build()` re-asserts this at construction and raises `IdentityError` naming
every failing row if it ever doesn't hold — not just "the FPL feed said code and opta_code
agree," but code that would fail the moment they didn't.

**Teams — a real finding, not a re-confirmation of the brief.** The brief correctly stated
`teams[].opta_code` is `None` for all 20 clubs — no Opta join for teams. What it asked
XL-Coder to build and verify is a *different*, non-Opta equivalence, discovered and verified
this session: **the PL API's own numeric team id (as returned on match/lineup/fixture
payloads) equals FPL's legacy `teams[].code` field exactly.** Verified by cross-referencing
two independent live sources for the current (2026/27) season — FPL `bootstrap-static`
`teams[]` (`code`, `name`) against the PL API's own GW1 fixture list
(`homeTeam.id`/`awayTeam.id`, `homeTeam.name`/`awayTeam.name`) — **20/20 clubs matched**,
including three newly-promoted clubs (Coventry City=9, Hull City=88, Ipswich Town=40) that
only exist in the live 2026/27 fixture calendar, ruling out "reused a stale team list" as an
explanation. `TeamIdentityMap` is built from this and raises, naming every unresolved club, if
the equivalence ever fails for one. Team **name** comparison (`Man Utd` vs
`Manchester United`, `Spurs` vs `Tottenham Hotspur`, `Nott'm Forest` vs `Nottingham Forest`)
is logged as a warning, never fatal — the numeric id match is authoritative; naming-convention
differences are expected, not a resolution failure. **This should be promoted into
blueprint §3.5's cross-source id row** — team identity, not just player identity, turns out
to be free.

**Matches — resolved by (kickoff date, home team, away team), after teams resolve.**
`fplai.identity.resolve_match()` does exactly this against `match.fixtures@matchweek`'s
canonical rows (already team-code-resolved), raising on zero or on more than one candidate.

**Unresolved-entity behaviour, demonstrated live, not just asserted:** fetching a completed
2025/26 match's lineups against the CURRENT (2026/27) identity snapshot correctly raised
`IdentityError` naming **4 of 40** players (James Milner, Solly March, Joël Veltman, Tyrell
Malacia — all have left the Premier League since) — see §9.4. `_fetch_lineups`/
`_fetch_substitutions` collect every unresolved player across the whole match before raising
(instead of stopping at the first), so the error names all offenders at once rather than
forcing a fix-one-rerun-repeat loop.

### 9.3 A real design tension this surfaced — flagging for story 11

`match.fixtures@matchweek` resolves **both teams of every fixture in the matchweek batch
eagerly**, in one `FetchResult`. A historical matchweek (verified live: GW38 2025/26)
routinely contains a fixture between two since-relegated clubs (Burnley v Wolves), which
raises for the **whole matchweek** — even though 9 of its 10 fixtures, including the one a
caller actually wanted, would resolve fine on their own. This is §12.5 working exactly as
specified (never silently drop a row), but it means **the current design cannot backfill
even one clean historical matchweek that mixes current and departed clubs** without a
per-fixture partial-success mode that §12.5's "unmatched entities raise" does not obviously
permit. Not resolved here — deliberately left for the Architect / story 11 (backfill
orchestrator) to decide: whether a matchweek fetch should (a) keep raising on any single
unresolvable fixture, (b) skip the unresolvable fixture with a loud, structured warning while
returning the rest, or (c) require a season-appropriate identity snapshot per historical
season (the same shape as the player problem below). Not a hidden problem — encountered,
understood, and reported rather than quietly worked around.

The same shape of tension applies to players: **`PlayerIdentityMap` is built from ONE
current `elements` snapshot, so any historical fetch will name departed players as
unresolved — this is not rare, it is close to guaranteed** once a transfer window has passed.
Confirmed live across every 2025/26 GW38 fixture tried while building the verification script
(3 different matches, each hit at least one unresolved player). Story 11 will need a
season-appropriate `elements` snapshot per backfilled season, not today's, to resolve
historical lineups/substitutions cleanly — FPL's live API has no historical-elements endpoint,
so this snapshot would have to come from an archive (vaastav, blueprint §3.5) rather than a
live call.

### 9.4 Live verification (2026-08-20)

`python scripts/verify_pl_provider.py` (default: 2025/26 GW38 Brighton 0-3 Man Utd,
`match_id=2562265`, identity built against the live current 2026/27 snapshot):

```
FPL snapshot: 599 elements, 20 teams
player identity map built: 599/599 elements resolved (100%)
team identity: id matched but name comparison was inconclusive for
  [(1, 'Man Utd', 'Manchester United'), (17, "Nott'm Forest", 'Nottingham Forest'),
   (6, 'Spurs', 'Tottenham Hotspur')]           <- expected, logged, not fatal
team identity map built and verified: 20/20 FPL teams resolved (100%)
identity resolved: 599 players, 20 teams (both directions)

match.lineups@match: EXPECTED IdentityError for a historical match —
  v3/matches/2562265/lineups: 4 player(s) in this match could not be resolved to a
  current FPL element: PL player ids ['15157', '109345', '111478', '222690']
  (James Milner, Solly March, Joel Veltman, Tyrell Malacia — all left the PL since)

match.substitutions@match: EXPECTED IdentityError for a historical match —
  v1/matches/2562265/events: substitution(s) involving unresolved player id(s)
  ['109345', '514254', '535301', '15157', '222690', '106760']

match.fixtures@matchweek: 10 fixtures in season 2026 GW1 (all 20 current clubs)
  match_id=2645195 kickoff=2026-08-21 20:00:00 home_code=3 away_code=9
    (Arsenal v Coventry City)
  match_id=2645196 kickoff=2026-08-22 17:30:00 home_code=94 away_code=6
    (Brentford v Tottenham Hotspur)
  match_id=2645197 kickoff=2026-08-22 15:00:00 home_code=11 away_code=31
    (Everton v Crystal Palace)

team.match_stats@match: 340 rows (long format), meta={'n_stat_keys_per_side':
  {'Away': 180, 'Home': 160}}                   <- NOT all 185 keys present every side/match
  expectedGoals side=Away team_code=1 value=1.6567
  expectedGoals side=Home team_code=36 value=0.7866

player.season_stats@season: element_id=1 (Raya), 66 stat rows
VERIFICATION RUN COMPLETE
```

Three of five capabilities (`team.match_stats@match`, `player.season_stats@season`,
`match.fixtures@matchweek`) fetched and validated fully live, end to end. The other two
(`match.lineups@match`, `match.substitutions@match`) hit the identity gap described in §9.3 —
included here in full rather than cherry-picking a match that happened to avoid it, because a
"clean" run against a hand-picked match would misrepresent how the adapter behaves against
real historical data. Both are independently proven working against mocked payloads in
`tests/test_provider_pl.py` (`test_fetch_lineups_resolves_identity_and_marks_bench`,
`test_fetch_substitutions_normalises_rows`), including the bench/captain/minute fields.

**Note on `team.match_stats@match`'s key count**: 180 keys for the away side, 160 for home —
the 185 figure from the brief is an observed maximum, not a guaranteed constant per side/match
(some keys are presumably conditional, e.g. goalkeeper-specific stats depending on which side
had a save to record). This reinforces the long-format decision — a fixed-width wide schema
would have to either null-pad to the union of every key ever seen, or churn every time a new
key/season shows up.

### 9.5 Test suite

`uv run pytest -q` (or `.venv/Scripts/python.exe -m pytest -q`): **160 passed** — 118
pre-existing (stories 1-5 and the `as_of()` fix, §7-8 above) + 4 new capability-coverage
assertions added to `tests/test_schemas.py` + this session's new `tests/test_identity.py`
(18 tests) and `tests/test_provider_pl.py` (20 tests) = 160. Two pre-existing assertions were
rewritten, not weakened: `tests/test_schemas.py::test_canonical_schemas_cover_all_seven_fpl_
capabilities` (now `..._all_fpl_and_pl_capabilities`, asserting all 12) and
`tests/test_provider_fpl.py::test_capabilities_lists_exactly_the_seven_served` (now compares
against the explicit FPL-only 7-capability set rather than the whole registry, since
`CANONICAL_SCHEMAS` legitimately has 12 entries now and `FPLProvider` still only serves 7 of
them). All network access mocked (`FakeTransport` in `test_provider_pl.py`, literal
DataFrames/dicts in `test_identity.py`) — zero live calls in the suite; the live calls are
entirely confined to `scripts/verify_pl_provider.py`, run by hand.

### 9.6 What was left for stories 8-11

- **Story 8 (Odds adapter)** — untouched; `CreditPolicy`/`CreditTracker` from story 4 remain
  the ready infrastructure.
- **Story 9 (derived-capability framework)** — untouched. `is_modelled` still defaults `False`
  everywhere; none of the five new capabilities are derived (they're all direct API reads).
- **Story 10 (archive adapters)** — untouched.
- **Story 11 (backfill orchestrator)** — explicitly NOT attempted (brief: "do not run a bulk
  backfill"). §9.3 above is the design brief story 11 will need to read before starting: the
  current adapter raises on any historical fixture/lineup touching a departed player or a
  since-relegated club, by design, and story 11 needs an explicit decision on
  season-appropriate identity snapshots (or a partial-success mode) before a real backfill can
  run cleanly.
- `player.defensive_actions@match` remains deliberately unregistered — confirmed, not
  reopened; see `docs/wiki/provider-evaluation.md`.

## 10. First non-HTTP provider: transport/decoder split + vaastav archive + season-scoped identity — E2b story 10, 2026-08-20/21

> Author: XL-Coder · session `s001`
> `src/fplai/decoders.py` (new module), `src/fplai/transport.py`'s new `FileTransport`/
> `FileCache` classes, `src/fplai/providers/vaastav.py` (new adapter), `fplai.identity.
> build_player_identity_map_for_season` (new function). Two new capabilities in
> `schemas.py`: `player.gameweek_stats@gameweek`, `player.identity@season`.
> `scripts/verify_vaastav_provider.py` — live verification, including the acceptance test.

### 10.1 Did the transport/decoder split hold?

**Yes, for the split itself and for the adapter built on top of it.** The Architect's
diagram (`Provider = Transport + Decoder`, polymorphism below the capability contract) held
exactly as specified:

- `decoders.py` is a brand-new, standalone module — a `dict[str, Callable[[bytes], Any]]`
  registry (`register_decoder`, `decode`) with `csv` (→ `pl.DataFrame`, via `pl.read_csv`,
  no new dependency) and `json` registered at import time. `transport.py` never imports it;
  `decoders.py` never imports `transport.py`. Proven independent, not just claimed: a
  throwaway third decoder is registered and used in `tests/test_decoders.py::
  test_register_decoder_extends_the_registry_without_touching_core` without touching either
  `providers/vaastav.py` or any core module.
- `FileTransport` (+ its own `FileCache`, deliberately NOT `ResponseCache` reused — see its
  docstring for why: JSON-wrapping a multi-MB CSV body buys nothing) is a genuinely new
  transport primitive in `transport.py`, dispatching only on `BulkFilePolicy`. This was
  **explicitly in scope** (story brief item 1), not scope creep — it's the second half of
  "the transport/decoder split that makes [the archive adapter] possible."
- `providers/vaastav.py` (`VaastavProvider`) composes both: `FileTransport.fetch(url)` for
  raw bytes, `decoders.decode("csv", raw)` for the shape. **Zero changes to `registry.py` or
  `providers/base.py`** — `register()` is the same "adapter + registry entry" shape as
  `providers/fpl.py`/`providers/pl.py`'s.

**One honest exception, same class as story 6/7's:** `schemas.py` gained two new
`CapabilityKey`/`FactTableSchema` entries and two `DATASET_ENTITY_KEYS` entries — additive
declarations, exactly what §4's "steps for adding a new provider" already anticipates
("add any new capability keys... if the new provider serves something not already
covered"), not a change to `FactTableSchema`, `CapabilityKey`, or `CANONICAL_SCHEMAS`'s own
logic. **A second, real exception:** `fplai.identity` gained
`build_player_identity_map_for_season()` — a new function, not just a new adapter file. This
is deliberately NOT vaastav-specific (it takes a plain `callable(season) -> pl.DataFrame`,
never imports `providers/vaastav.py`) because season-scoped identity is blueprint §12.5's
GENERAL requirement, the same judgement call story 7 made for `identity.py` itself. Flagging
this plainly rather than claiming a cleaner gate than actually held.

### 10.2 vaastav's real layout, and how schema drift was handled

**Verified live, 2026-08-20/21 — not assumed from `docs/wiki/data-sources.md` §3.1 (which
only sampled 2025-26 GW10).** This session additionally pulled 2016-17's and 2025-26's
`players_raw.csv`, `teams.csv`, and `gws/gw1.csv` headers directly from GitHub to find the
narrowest common shape across the widest span the archive has (2016-17 → 2025-26, 10
seasons):

```
data/{season}/players_raw.csv     one row per player -- THAT season's own
                                   bootstrap-static 'elements' snapshot
data/{season}/teams.csv            one row per club, that season (code, name)
data/{season}/gws/gw{N}.csv        one row per player, one gameweek
```

`{season}` is FPL's own `"YYYY-YY"` string, never the PL API's starting-year convention.

**Drift is real, not theoretical — confirmed by diffing the two eras' actual CSV headers:**
2016-17's `gws/gw{N}.csv` has NO `position`/`team`/`xP`/`expected_*`/
`defensive_contribution` columns that 2025-26's has, and its `players_raw.csv` has NO
`opta_code` column at all (the FPL API had no Opta join key that far back). **Handled
explicitly, not by coercion:** `schemas.py`'s `required_fields` for both new capabilities
are deliberately the columns verified present in BOTH the oldest and newest seasons checked
— `element`, `round`, `selected`, `value`, `minutes`, `total_points`, `name`,
`was_home`, `team_a_score`, `team_h_score`, `kickoff_time` for gameweek stats; `id`, `code`,
`element_type`, `team`, `team_code`, `web_name`, `first_name`, `second_name`, `now_cost` for
identity. A season genuinely missing one of those raises `SchemaError` loudly — nothing here
null-pads an old season to look like a new one. `opta_code` is the deliberate exception:
NOT required by the schema (a pre-opta season's player list is still a legitimate "season's
player list"), so the schema-validation layer passes it — and the SEPARATE, correct failure
happens one layer down, at `PlayerIdentityMap.build()`, when it finds the join column
missing. `tests/test_provider_vaastav.py::
test_fetch_player_identity_2016_17_has_no_opta_code_but_still_validates` proves both halves
of that split in one test: schema validation passes, `PlayerIdentityMap.build()` raises.

### 10.3 Season-scoped identity — the §12.5 fix, live proof

**Before this story** (`scripts/verify_pl_provider.py`, stories 6-7): resolving a 2025/26
match's lineups against the CURRENT (2026/27) FPL `elements` snapshot correctly raised
`IdentityError` on 4 of 40 players who have since left the league — correct behaviour
against the WRONG snapshot. Nothing in the codebase chose a season-appropriate snapshot;
every call site always reached for today's.

**`fplai.identity.build_player_identity_map_for_season(season, *, current_season,
live_elements=None, archive_elements_fetch=None)`** is the missing choice: current season →
`live_elements` (unchanged behaviour); any other season → `archive_elements_fetch(season)`,
a plain callable so `identity.py` still imports nothing from `providers/`. Both branches
still terminate at `PlayerIdentityMap.build()`, which is where the actual per-row
verification/raise lives — this function's only job is picking the right snapshot, never
deciding whether a row resolves.

**Live proof, `python scripts/verify_vaastav_provider.py` (same match as §9.4's default —
Brighton 0-3 Man Utd, GW38 2025/26, `match_id=2562265`):**

```
player.gameweek_stats@gameweek: season=2025-26 gw=1 -> 692 rows, endpoint=2025-26/gws/gw1.csv
  most-selected player GW1 2025-26: Cole Palmer, selected=6063687 (raw count), value=105 (x10)
current (2026-27) FPL snapshot: 599 elements, 20 teams
team identity map built and verified: 20/20 FPL teams resolved (100%)
team identity (unaffected by this story): 20/20 FPL teams resolved
season-scoped (2025-26) player identity: 841 players resolved from the ARCHIVE, not today's snapshot
BEFORE (current-season identity, as verify_pl_provider.py always did): EXPECTED IdentityError —
  v3/matches/2562265/lineups: 4 player(s) ... could not be resolved ... PL player ids
  ['15157', '109345', '111478', '222690']
AFTER (season-scoped 2025-26 identity, from vaastav): SUCCEEDED — 40 rows (22 starters,
  18 bench), 0 unresolved
ACCEPTANCE TEST PASSED: a 2025/26 match now resolves because it resolves against 2025/26 identity.
```

**Team identity was never broken for this specific match** (Brighton and Man Utd are both
current-season clubs — §9.4 already showed 20/20 resolving) — the script deliberately keeps
using the current live snapshot for teams and ONLY swaps player identity to the archive, to
isolate exactly what the fix changes. §9.3's separate finding (a historical MATCHWEEK
mixing current and departed clubs still fails as a whole batch) is untouched by this story
— see §10.4.

**841 archive-resolved players vs. 599 current elements**: expected, not a bug —
2025-26's full-season player universe (everyone who ever had an `elements` row that season,
including mid-season departures/loans) is larger than today's 599-player live squad list.

### 10.4 What story 11 still needs

- **`build_player_identity_map_for_season` is unit-tested and live-proven for PLAYER
  identity only.** Team identity for a historical season still goes through `PLProvider`'s
  existing `identity_season` mechanism (story 6/7, unchanged) — that was already
  season-scoped and was never the gap this story closed. Story 11 should confirm it's
  sufficient rather than assume so; it wasn't re-verified against a matchweek containing a
  since-relegated club (see next point).
- **§9.3's matchweek-batch tension is still open and untouched.** A historical matchweek
  mixing a current club and a since-relegated one still raises for the WHOLE batch — this
  story only fixed the PLAYER snapshot choice, not the "no partial success" design tension
  §9.3 flagged for team/match resolution. Story 11 needs an explicit decision there.
- **`player.identity@season` gives story 11 exactly the per-season snapshot it was missing**
  (§9.6's prior note: "story 11 will need a season-appropriate `elements` snapshot per
  backfilled season... this snapshot would have to come from an archive"). That gap is now
  closed — `VaastavProvider.fetch(PLAYER_IDENTITY_SEASON, season=...)` is exactly that
  snapshot, live-proven above.
- **olbauday adapter (2024-25 →) is the explicitly-flagged quick follow-up** — same
  transport/decoder pattern, a second `FileTransport` base URL, and (per
  `docs/wiki/data-sources.md` §3.2) richer per-GW ownership% and injury-flag fields than
  vaastav's raw `selected` count. Not built here (out of scope, per the story brief).
- **No bulk backfill was run** (story brief: "do not run a bulk multi-season backfill —
  pull only what you need to build and verify"). Live fetches this session were exactly:
  one `bootstrap-static/`, one PL API matchweek-1 fixtures call (both already cached from
  prior sessions), one vaastav `players_raw.csv` (2025-26), one vaastav `gws/gw1.csv`
  (2025-26), one PL API match-lineups call — five live requests total, all now cached.

### 10.5 Test suite

`uv run pytest -q` (or `.venv/Scripts/python.exe -m pytest -q`): **196 passed** — 160
pre-existing (stories 1-9, untouched) + 36 new: `tests/test_decoders.py` (6),
`tests/test_provider_vaastav.py` (16), new `FileTransport`/`FileCache` cases appended to
`tests/test_transport.py` (9), new `build_player_identity_map_for_season` cases appended to
`tests/test_identity.py` (5), plus 3 new/updated assertions in `tests/test_schemas.py`
(the 12→14-capability count, `DATASET_ENTITY_KEYS` gaining two entries, and two new
schema-drift-specific tests). Every pre-existing assertion that hardcoded a capability count
was updated, not weakened — same discipline as story 6/7's §9.5. All network access mocked
(`FakeFileTransport` in `test_provider_vaastav.py`, literal bytes in `test_decoders.py` and
`test_transport.py`) — zero live calls in the suite; live calls are entirely confined to
`scripts/verify_vaastav_provider.py`, run by hand.

## 11. Backfill orchestrator — E2b story 11, 2026-08-21

> Author: XL-Coder · session `s001`
> `src/fplai/backfill.py` (new module — `WorkUnit`, `BackfillSpec`, `GRAIN_PLANS`,
> `CheckpointStore`, `BackfillOrchestrator`, `dry_run`), `tests/test_backfill.py` (40 tests),
> `scripts/backfill.py` (CLI). Zero changes to `schemas.py`, `registry.py`,
> `providers/base.py`, `providers/pl.py`, `providers/vaastav.py`, `identity.py`,
> `transport.py` — this story is purely a new layer ON TOP of the existing provider
> abstraction, consuming it exactly as designed.

### 11.1 The design

**Work enumeration is two-tier** (module docstring §1 has the full reasoning):
`match.fixtures@matchweek`, `player.gameweek_stats@gameweek` (vaastav), and
`player.identity@season` (vaastav) are **static** — every `WorkUnit` is knowable from
`(season, matchweek/gameweek)` alone, zero live calls, exact counts. `match.lineups@match`,
`match.substitutions@match`, `team.match_stats@match` are **dependent** — a match's real id
is only known once its matchweek's fixtures have actually been fetched, so `--dry-run` uses
a documented, overridable ESTIMATE (`BackfillSpec.matches_per_matchweek`, default 10) while a
real `run()` expands from the ACTUAL fixture rows, read back from the store (never carried in
memory — see §11.1's checkpoint-resume point below for why). `player.season_stats@season`
has the same estimate machinery but its execution is deliberately **not wired**
(`GrainPlan.executable=False`) — `run()` refuses outright (`BackfillError`, naming exactly
what's missing) rather than silently no-op'ing it, per the story brief's time-boxing and this
session's decision to spend the live-verification budget on the two capabilities actually
proven end-to-end (§11.3).

**§9.3's open question, answered.** Blueprint §12.5 rejects a partial-success/skip-the-misses
mode; §9.3 asked whether the orchestrator should therefore keep raising on any single
unresolvable fixture, silently skip it, or require a season-appropriate identity snapshot.
**Answer: the third option, already delivered by story 10's `player.identity@season`.**
`BackfillOrchestrator` does not accept a bare `Provider` — it takes a `provider_factory:
Callable[[str], Provider]`, keyed by season, so a caller is structurally required to supply a
season-appropriate identity resolver rather than reach for whatever happens to be lying
around. Given that, an `IdentityError` should not happen — **if it does anyway, that is a
bug** (the caller supplied the wrong snapshot, or a provider's own resolution has a real gap),
and `run()` treats it as exactly that: catches it, checkpoints NOTHING for the failing unit
(not "absent" — unresolved, retried on a corrected re-run), and raises
`BackfillHalted(reason="identity")`. This is deliberately a fail-fast bug report, not a
recoverable per-unit outcome — §11.4 below is the live proof that this distinction is not
academic.

**Three outcomes, not two, written as three separate `except` clauses so a reader sees the
distinction in the code, not just in a docstring:**

| Exception | Meaning | Orchestrator behaviour |
|---|---|---|
| `IdentityError` | our bug (wrong/missing season-scoped identity) | `BackfillHalted(reason="identity")` — nothing checkpointed for the unit, run stops |
| `TransportError` | transport's own retries/backoff already exhausted — a §3.6 politeness signal, not a per-unit fact | `BackfillHalted(reason="transport")` — nothing checkpointed, run stops |
| `ProviderError` | a genuine fact about the world (404, no matches this matchweek, season predates the source) | `UnitOutcome(status="absent", reason=...)`, checkpointed, run **continues** |

**Checkpointing** is an append-only JSONL file (`CheckpointStore`, same convention as
`.punchcard/`) — one line per resolved unit, keyed by a fully deterministic `WorkUnit.key`
(`f"{provider_id}:{capability}:season={season!r},{sorted params}"`). `run()` loads the file,
skips any unit whose key is already resolved (`done` OR `absent` — both terminal), and
appends as it goes, flushing every write. This is what makes resume genuinely resume rather
than re-derive: the same `BackfillSpec` always re-enumerates the same units in the same
order (blueprint §7), and the checkpoint file is the durable record of which of them are
already settled.

**`valid_at` for historical writes is never `datetime.now()`.** `store.write()` takes one
`valid_at` per batch; a batch spanning several real-world instants (ten fixtures' worth of
kickoffs in one matchweek) is anchored at the EARLIEST instant in it — documented as a
convention, the conservative direction (§3.2: understating how early a fact became true is
merely conservative, overstating it is the leakage direction). Four conventions, one per
capability, in `_valid_at_for`: fixtures → `min(kickoff)` in the batch; match-grain (lineups/
subs/team-stats) → the match's own kickoff, threaded through via `WorkUnit.meta` (never
`fetch_kwargs` — a capability's `fetch()` signature doesn't accept it) from the fixtures row
that produced the match id; vaastav gameweek stats → `min(kickoff_time)`, a real column the
archive already carries; vaastav player identity → the season's own start date (1 Aug of the
season's first year) — a season-scoped player list has no one true instant, so this anchors
at the earliest plausible one. `observed_at` is correctly `datetime.now()` everywhere — that
genuinely is when THIS system learned a years-old fact, which is a different question from
`valid_at` entirely.

### 11.2 Dry-run output (real numbers, zero requests made)

One season, two capabilities (`python scripts/backfill.py --provider pl_api --capabilities
match.fixtures@matchweek,match.lineups@match --seasons 2025`):

```
capability                         dataset                      kind            units   requests
----------------------------------------------------------------------------------------------
match.fixtures@matchweek           pl_match_fixtures            exact              38         38
match.lineups@match                pl_match_lineups             estimated         380        380
----------------------------------------------------------------------------------------------
TOTAL                                                                                        418
Estimated wall-clock at policy rate: 3m 29s
```

A full 10-season PL API backfill, all five PL API capabilities (`--seasons
2015,2016,...,2024`):

```
capability                         dataset                      kind            units   requests
----------------------------------------------------------------------------------------------
match.fixtures@matchweek           pl_match_fixtures            exact             380        380
match.lineups@match                pl_match_lineups             estimated       3,800      3,800
match.substitutions@match          pl_match_substitutions       estimated       3,800      3,800
team.match_stats@match             pl_team_match_stats          estimated       3,800      3,800
player.season_stats@season         pl_player_season_stats       estimated       7,000      7,000
----------------------------------------------------------------------------------------------
TOTAL                                                                                     18,780
Estimated wall-clock at policy rate: 2h 36m 30s
```

**This is a real finding, not just a demo number.** The story brief quoted "~3,800 PL API
requests" for a 10-season backfill. Running `match.fixtures@matchweek` + `match.lineups@
match` ALONE across the same 10 seasons gives **exactly 4,180** (380 fixture-batch requests +
3,800 real match requests) — and 3,800 on its own (`test_dry_run_ten_season_lineups_only_
matches_the_briefs_headline_figure`, `tests/test_backfill.py`) is precisely `10 seasons × 38
matchweeks × 10 matches/matchweek`. The brief's figure is a single match-grain capability's
count, not a full five-capability backfill — a genuinely complete backfill (all five PL API
capabilities) is **~4.5x larger** than the headline number, because `player.season_stats@
season` alone (one request PER PLAYER, ~700/season) adds another 7,000 on top. Worth carrying
forward: **anyone greenlighting "a 10-season PL API backfill" needs to specify WHICH
capabilities**, because the cost varies by that factor depending on the answer.

vaastav (bulk files, no rate ceiling — `--provider vaastav_archive --capabilities player.
gameweek_stats@gameweek,player.identity@season --seasons 2025-26`): 39 requests total (38
gameweek files + 1 identity file), ~39s at an assumed 1s/file (explicitly labelled as not a
policy-derived number — `BulkFilePolicy` enforces nothing at all).

### 11.3 Live proof — vaastav (interrupt + resume) and PL API (interrupt + resume)

**vaastav, `player.gameweek_stats@gameweek`, season 2025-26, 5 gameweeks**, dedicated demo
store (`data/backfill_demo_store`, gitignored) so this session's proof run never touched the
real production store that has been accumulating live snapshots since 2026-08-19:

```
$ python scripts/backfill.py --provider vaastav_archive --capabilities player.gameweek_stats@gameweek \
    --seasons 2025-26 --gameweeks-per-season 5 --execute --max-units 2 \
    --checkpoint-path data/backfill/vaastav_demo.jsonl --store-path data/backfill_demo_store
...GET .../gws/gw2.csv HTTP/1.1" 200 34364
RUN COMPLETE stopped_early=True total=5 done=2 absent=0 already_resolved_on_entry=0

$ python scripts/backfill.py --provider vaastav_archive --capabilities player.gameweek_stats@gameweek \
    --seasons 2025-26 --gameweeks-per-season 5 --execute \
    --checkpoint-path data/backfill/vaastav_demo.jsonl --store-path data/backfill_demo_store
...GET .../gws/gw3.csv ... gw4.csv ... gw5.csv HTTP/1.1" 200 (three live requests)
RUN COMPLETE stopped_early=False total=5 done=5 absent=0 already_resolved_on_entry=2
```

`--max-units 2` stands in for a real interruption (a deliberate, clean early stop — see
`BackfillOrchestrator.run`'s `max_units` parameter) rather than an actual process kill, chosen
so the proof is exact and reproducible; the checkpoint file mechanics are identical either
way. The resumed run's log shows ONLY gw3/gw4/gw5 requests — gw1/gw2 are never re-fetched,
confirmed both by the log and by `provider2.calls` in the equivalent unit test
(`test_run_skips_already_checkpointed_units_never_refetches_them`) using a fresh provider
instance that would `AssertionError` if asked for gw1/gw2 again. Final store state, queried
directly:

```
round  valid_at             provider_id       capability
1      2025-08-15 19:00:00  vaastav_archive   player.gameweek_stats@gameweek
2      2025-08-22 19:00:00  vaastav_archive   player.gameweek_stats@gameweek
3      2025-08-30 11:30:00  vaastav_archive   player.gameweek_stats@gameweek
4      2025-09-13 11:30:00  vaastav_archive   player.gameweek_stats@gameweek
5      2025-09-20 11:30:00  vaastav_archive   player.gameweek_stats@gameweek
```

`valid_at` is genuinely anchored in August/September 2025 — not `datetime.now()`
(2026-08-21) — confirming §11.1's `valid_at` convention live, on real written Parquet, not
just in a unit test.

**PL API, `match.fixtures@matchweek`, current season (2026), 2 matchweeks**, same demo store,
same interrupt/resume shape:

```
$ python scripts/backfill.py --provider pl_api --capabilities match.fixtures@matchweek \
    --seasons 2026 --matchweeks-per-season 2 --execute --max-units 1 \
    --checkpoint-path data/backfill/pl_api_demo2.jsonl --store-path data/backfill_demo_store
RUN COMPLETE stopped_early=True total=2 done=1 absent=0 already_resolved_on_entry=0

$ python scripts/backfill.py --provider pl_api --capabilities match.fixtures@matchweek \
    --seasons 2026 --matchweeks-per-season 2 --execute \
    --checkpoint-path data/backfill/pl_api_demo2.jsonl --store-path data/backfill_demo_store
...GET /api/v1/competitions/8/seasons/2026/matchweeks/2/matches HTTP/1.1" 200
RUN COMPLETE stopped_early=False total=2 done=2 absent=0 already_resolved_on_entry=1
```

20 fixture rows (2 matchweeks × 10) landed in the store; matchweek 1 was already cached from
story 6/7's earlier live verification (zero new request), matchweek 2 made exactly one live
request — gentle by construction (`--matchweeks-per-season 2`), not by luck.

### 11.4 A real live finding: team identity is STILL not season-scoped

§10.4 flagged this as unconfirmed: *"team identity for a historical season still goes through
`PLProvider`'s existing `identity_season` mechanism... it wasn't re-verified against a
matchweek containing a since-relegated club."* This story ran exactly that check, live:

```
$ python scripts/backfill.py --provider pl_api --capabilities match.fixtures@matchweek \
    --seasons 2025 --matchweeks-per-season 2 --execute \
    --checkpoint-path data/backfill/pl_api_demo.jsonl --store-path data/backfill_demo_store
RUN HALTED (identity): backfill halted (reason=identity) at unit
  "pl_api:match.fixtures@matchweek:season='2025',matchweek=1,season='2025'"
  after resolving 0/2 units this phase this run: unresolved team: PL API team id 21 has no
  matching FPL team code in this map (blueprint §12.5). This is expected for a club not in
  the current season's 20 (e.g. a relegated club referenced by a historical fixture) — this
  map is built from one season's fixture list, not a full historical registry.
```

**This is the orchestrator working exactly as designed, not a defect in it.** `--seasons
2025` (2025/26, a completed historical season) with team identity still built from the
CURRENT (2026/27) fixture list hit a club (PL numeric id 21 — relegated since) absent from
today's 20. `run()` correctly classified this as `IdentityError` → `BackfillHalted
(reason="identity")` and checkpointed nothing for the failing unit
(`data/backfill/pl_api_demo.jsonl` is empty, confirmed) — exactly §12.5's "unmatched entities
raise, never silently drop", now proven against a REAL historical fixture rather than only in
a mocked test.

**But it means §9.3's original tension is not fully closed for PL API backfills.** Story 10
built `player.identity@season` (season-scoped PLAYER identity, via vaastav's
`players_raw.csv`) — that part is genuinely fixed and this story's `--seasons 2026`
(current-season) proof above shows it working. **TEAM identity has no equivalent yet.**
`vaastav/Fantasy-Premier-League` carries a `teams.csv` per season with the same shape as
`players_raw.csv` (verified present, story 10's §10.2 recon) — the fix would be the same
pattern as `build_player_identity_map_for_season`: a `team.identity@season` capability + a
`build_team_identity_map_for_season` function, symmetrical to the player-identity fix. **Not
built here** — out of this story's scope (backfill orchestration, not identity resolution),
but now confirmed live rather than theoretical, and it is the actual blocker for backfilling
`match.fixtures@matchweek` (and everything downstream of it — lineups, substitutions,
team-stats) for any season whose matchweeks mix a current club with a departed one, which is
most of PL history beyond the last 2-3 seasons. **Recommended next story.**

### 11.5 A second finding, environment-level: polars + Windows + no tzdata

`BitemporalStore._duckdb_type_to_polars` maps EVERY timestamp column read back through
`as_of`/`observations`/`latest` — including a plain naive `TIMESTAMP` — to `pl.Datetime("us",
"UTC")` (a deliberate "naive means UTC by convention" choice). On a Windows machine with no
IANA tzdata installed (this dev machine: confirmed `import tzdata` → `ModuleNotFoundError`,
and Windows ships no system tzdb at all), converting ANY value of such a column to a Python
object (`.to_dicts()`, `.to_list()`, `.item()`, indexing a `Series`) panics inside polars'
Rust layer — `pyo3_runtime.PanicException: ZoneInfoNotFoundError('No time zone found with key
UTC')` — which is **not catchable by `except Exception`**. This is a real, pre-existing
landmine in `store.py`'s read path (not introduced by this story), simply never triggered
before because no prior story's code iterated row-by-row over data read back FROM the store
with a timestamp column — every previous live script only ever touched freshly-fetched
`FetchResult.rows` (naive Python datetimes, no "UTC" label attached, no zoneinfo lookup
needed). This story's dependent-unit expansion (`_expand_all_dependent_units`) is the first
code path to do exactly that (`store.latest("pl_match_fixtures")` → `.to_dicts()` to build
`match.lineups@match` work units), and hit it immediately, before any fix.

**Fix applied, scoped to this module only** (`store.py` is not this story's to change,
especially mid-production-capture before GW1): `fplai.backfill._strip_tz_for_safe_row_
iteration` strips the "UTC" label (`.dt.replace_time_zone(None)`) from every Datetime column
immediately after reading from the store, before any row-iteration — lossless (the
underlying instant is unchanged), and consistent with the codebase's own existing convention
that naive-means-UTC. **This is a workaround, not a fix of the root cause**, and is flagged
here for the Architect: either add `tzdata` as a (tiny, pure-Python) dependency, or revisit
whether `_duckdb_type_to_polars` needs to attach a "UTC" label to plain `TIMESTAMP` columns
at all, given the codebase already treats naive-as-UTC everywhere else and the label buys
nothing except this exact class of crash on Windows without tzdata. Any future code that
calls `as_of`/`observations`/`latest` and then materialises a timestamp value to Python (a
model or backtest reading `observed_at`/`valid_at`, for instance) will hit the same panic on
this machine until one of those is done.

### 11.6 Test suite

`uv run pytest -q` (or `.venv/Scripts/python.exe -m pytest -q`): **236 passed** — 196
pre-existing (stories 1-10, untouched) + 40 new (`tests/test_backfill.py`): work-unit
determinism, spec validation (including the live-only-capability rejection with its
"no history" hint), static enumeration exact counts, dependent-count estimation formulas,
dry-run rendering and wall-clock math across all four `TransportPolicy` shapes, checkpoint
round-trip/append-only/last-write-wins, all four `valid_at` conventions, and — the core of
the story — orchestrator `run()` behaviour: happy path with real store writes, resumability
(a genuine two-orchestrator-instance test with a provider that would `AssertionError` on a
duplicate fetch), all three failure paths (`IdentityError`→halt, `ProviderError`→absent+
continue, `TransportError`→halt) with an accompanying "absence doesn't crash dependent
expansion" case, dependent match-grain expansion from real (not estimated) fixture rows, all
three `BackfillError` preconditions (missing `MATCH_FIXTURES_MATCHWEEK` in spec, missing
`store`, non-executable capability), and provider-factory memoisation per season. All network
access mocked (`FakeProvider`, a literal `{(capability, params): outcome}` map where
`outcome` is either a `FetchResult` or an exception instance to raise) — zero live calls in
the suite; live calls are entirely confined to `scripts/backfill.py`, run by hand, both
providers, both proven interrupt-and-resume (§11.3).

### 11.7 What was left open

- **Team-identity season-scoping** (§11.4) — the actual next story; without it, PL API
  match-grain backfills are blocked for any season mixing current and departed clubs, which
  is most of PL history.
- **`player.season_stats@season` execution** — cost-modelled (`--dry-run` includes it,
  correctly) but not wired to `run()` (`GrainPlan.executable=False`); needs one more
  expansion step (`identity().players.code_to_element_id.values()` → one unit per player),
  symmetrical to the match-grain expansion already built.
- **No multi-season or multi-capability unattended backfill was run** — every live call this
  session was deliberately small (1-2 matchweeks / gameweeks at a time), per the story
  brief's explicit instruction. The dry-run numbers in §11.2 are what a real decision to run
  one should be made against.
- **The tzdata workaround (§11.5)** is scoped to `fplai.backfill` only — `store.py` itself is
  unchanged, and the same class of crash is latent for any future caller of `as_of`/
  `observations`/`latest` that materialises a timestamp to Python on a tzdata-less machine.

## 12. Season-scoped TEAM identity — E2b story 7b, 2026-08-21

> Author: XL-Coder · session `s001`
> `src/fplai/schemas.py` (`TEAM_IDENTITY_SEASON` capability + schema),
> `src/fplai/providers/vaastav.py` (`_fetch_team_identity`, `data/{season}/
> teams.csv`), `src/fplai/identity.py` (`build_team_identity_map_for_season`),
> `scripts/backfill.py` (`_build_pl_factory` now season-scopes BOTH
> identities; `--identity-season-pl` removed — no longer means anything
> real once team identity is season-scoped too), `scripts/
> verify_team_identity_season.py` (new — live acceptance test).
> Zero changes to `providers/pl.py`, `registry.py`, `providers/base.py`,
> `transport.py`, `decoders.py`, `fplai/backfill.py` (the orchestrator
> module itself) — this story closes §11.4/§11.7's flagged gap entirely
> within the pattern story 10 already established for players.

### 12.1 Did the framework hold?

**Yes, completely — this was the cleanest of the identity stories precisely
because story 10 had already done the hard design work.** Mirroring the
player pattern meant:

- `TEAM_IDENTITY_SEASON = CapabilityKey("team", "identity", "season")` +
  one `CANONICAL_SCHEMAS` entry + one `DATASET_ENTITY_KEYS` entry — additive
  declarations only, same as story 10's two.
- `VaastavProvider.capabilities()` grew from 2 to 3; `_fetch_team_identity`
  is structurally identical to `_fetch_player_identity` (fetch bytes,
  decode CSV, inject `season`, validate, wrap in `FetchResult`). Zero
  changes to `register()`, `capabilities()`'s caller, `transport.py`,
  `decoders.py`.
- `identity.py` gained `build_team_identity_map_for_season()`, same
  "current -> live snapshot, else -> caller-supplied archive callable,
  never import `providers/`" shape as `build_player_identity_map_for_season`.
  **The one real asymmetry, flagged in the function's own docstring, not
  hidden:** team identity needs a season-MATCHED counterpart on **both**
  sides of the join (`pl_teams`, the PL API's raw per-season team list),
  not just the FPL-side snapshot the player version needed — so `pl_teams`
  is a required argument this function does not (and structurally cannot,
  making no network call itself) choose for the caller. Getting this wrong
  — passing a `pl_teams` from a different season than `season` — silently
  reintroduces the exact bug this story exists to close, and this function
  has no way to detect that misuse from the inside. Documented as the
  caller's obligation, not solved architecturally, because the correct
  owner of "which pl_teams season" is `scripts/backfill.py`'s
  `provider_factory`, which already knows the season it was asked for.

**`providers/pl.py` needed zero changes**, confirming §9.1/§10.1's repeated
finding that `PLProvider`'s `identity: IdentityResolver | None` constructor
parameter was already the right seam — every identity story since 6/7 has
been able to build a fully-formed `IdentityResolver` externally and hand it
in, rather than teaching `PLProvider` itself about seasons, archives, or
vaastav. The one reach-in this story needed (`scripts/backfill.py`'s
`pl_teams_for_season` calling `PLProvider._fetch_pl_teams_for_identity()`
directly, a "private" method) is a wiring-script convenience — it avoids
duplicating the endpoint-building logic in a second place, at the cost of
reaching past an underscore. Flagged, not hidden: a cleaner alternative
would promote that method to public API on `PLProvider`, but doing so
wasn't necessary to close this story and would have touched `providers/
pl.py` for a rename only.

### 12.2 vaastav's real `teams.csv` layout, and its own drift (distinct from `players_raw.csv`'s)

**Verified live, 2026-08-21** by pulling headers directly from GitHub —
not assumed from story 10's `players_raw.csv` recon, which is a different
file with a different history:

```
data/{season}/teams.csv   one row per club, that season's own FPL
                          bootstrap-static 'teams' snapshot
```

Columns verified stable 2019-20 → 2025-26: `code`, `id`, `name`,
`short_name`, `draw`, `loss`, `win`, `played`, `points`, `position`,
`strength*`, `team_division`, `unavailable`, `pulse_id`. 2025-26 adds one
extra column (`link_url`), not required. **`code` is present, unbroken,
every season the file exists** — this matters because, unlike
`players_raw.csv`'s `opta_code` (legitimately absent pre-2017), `code` is
the ACTUAL join field `TeamIdentityMap` uses (PL API team id == FPL legacy
`code`, not `opta_code`, which is `None` for every team in every season
checked) — so `TEAM_IDENTITY_SEASON`'s schema requires it outright, no
"present but not required, join fails one layer down" split was needed
here the way story 10 built for `opta_code`.

**A genuinely different drift shape than `players_raw.csv`'s, found live:**
`teams.csv` **does not exist at all** for 2016-17, 2017-18, or 2018-19 —
confirmed 404 on all three directly against GitHub — while
`players_raw.csv` goes back to 2016-17 with no gap. The archive only
started keeping a separate teams file from 2019-20 onward. This is a real,
verified limitation, not a bug in this adapter: `FileTransport.fetch`
surfaces the 404 as a `TransportError` (existing, unmodified behaviour),
and a pre-2019-20 season's TEAM identity simply cannot be built from this
archive at all — the archive's coverage boundary for this specific
capability is three seasons narrower than for `player.identity@season`.
`tests/test_provider_vaastav.py::
test_fetch_team_identity_missing_teams_csv_raises_transport_error` proves
the fail-loud behaviour (with a fake transport standing in for the real
404 — `test_transport.py` already covers the real 404-to-`TransportError`
path).

### 12.3 The acceptance test — live, West Ham, season 2025/26

**Season: PL API `"2025"` / vaastav `"2025-26"`. Club: West Ham United, FPL
legacy code 21.** Confirmed live, this session: present in vaastav's
`2025-26/teams.csv` and in that season's own PL API matchweek-1 fixtures,
**absent** from the current (2026/27) FPL `bootstrap-static` 20-club
snapshot (`data/store/teams`, queried directly — the current 20 are
Arsenal, Aston Villa, Bournemouth, Brentford, Brighton, Chelsea, Coventry
City, Crystal Palace, Everton, Fulham, Hull City, Ipswich Town, Leeds,
Liverpool, Man City, Man Utd, Newcastle, Nott'm Forest, Spurs, Sunderland —
no West Ham). This is not an assumption inherited from story 11's write-up
— it was independently re-verified this session against the real,
currently-accumulating production store and the real vaastav archive.

**BEFORE — reproduced live, byte-for-byte, by two independent methods:**

1. `git stash` the pre-fix `scripts/backfill.py` and run the real CLI:

```
$ python scripts/backfill.py --provider pl_api --capabilities match.fixtures@matchweek \
    --seasons 2025 --matchweeks-per-season 2 --execute \
    --checkpoint-path data/backfill/pl_api_before_demo.jsonl --store-path data/backfill_demo_store_7b
RUN HALTED (identity): backfill halted (reason=identity) at unit
  "pl_api:match.fixtures@matchweek:season='2025',matchweek=1,season='2025'"
  after resolving 0/2 units this phase this run: unresolved team: PL API team id 21 has no
  matching FPL team code in this map (blueprint §12.5). ...
```

Exact match for story 11's §11.4 transcript — same message, same team id,
same halt point. Checkpoint file confirmed empty/absent afterward — nothing
checkpointed for the failing unit, per §12.5's "unmatched entities raise,
never partial-success".

2. `scripts/verify_team_identity_season.py` reproduces the same raise from
   first principles, without touching git history, for repeatability:
   builds team identity against ONLY the current (2026) season's own
   matchweek-1 (which succeeds — nothing is missing FROM that season), then
   reuses that map to fetch **2025/26**'s matchweek-1 fixtures, hitting
   West Ham's PL team id with no counterpart:

```
BEFORE (current-season TEAM identity reused for a 2025 fetch, story 11's exact bug):
  EXPECTED IdentityError — unresolved team: PL API team id 21 has no matching FPL team
  code in this map (blueprint §12.5). This is expected for a club not in the current
  season's 20 ...
```

**AFTER — the fixed `scripts/backfill.py`, same command, same season:**

```
$ python scripts/backfill.py --provider pl_api --capabilities match.fixtures@matchweek \
    --seasons 2025 --matchweeks-per-season 2 --execute \
    --checkpoint-path data/backfill/pl_api_after_demo.jsonl --store-path data/backfill_demo_store_7b
...
team identity map built and verified: 20/20 FPL teams resolved (100%)
RUN COMPLETE stopped_early=False total=2 done=2 absent=0 already_resolved_on_entry=0 elapsed=0.8s
```

20 fixture rows landed in the store (2 matchweeks × 10), queried directly
afterward — **West Ham appears in both**, home and away:

```
match_id  season  matchweek  home_team_code  away_team_code
2561899   2025    1          56              21    <- Sunderland vs West Ham
2561914   2025    2          21              8     <- West Ham vs Chelsea
```

**End-to-end, not just fixtures**: `scripts/verify_team_identity_season.py`
additionally fetches `match.lineups@match` for West Ham's own MW1 2025/26
fixture (match id 2561899) using the fully season-scoped resolver (player
identity from story 10 + team identity from this story) — 40 rows (22
starters, 18 bench across both sides), 20 of them West Ham's own 11
starters + 9 bench, all correctly carrying `player_element_id` (story 10)
and `team_code=21` (this story). Zero unresolved.

**`scripts/verify_team_identity_season.py` is fully idempotent** — a second
run makes zero live HTTP requests (confirmed: `grep -c "GET\|HTTP"` on its
output is 0 on rerun), same caching discipline as every other verify
script in this codebase.

### 12.4 Test suite

`uv run pytest -q`: **257 passed** — 244 pre-existing (stories 1-11 plus
the real-store invariant tests added since story 11's report) + 13 new:
6 in `tests/test_identity.py` (`build_team_identity_map_for_season`'s
current/historical/missing-argument/never-touches-live cases, plus one
proving the current-season branch still raises loudly on a genuine
season-mismatched `pl_teams`), 4 in `tests/test_provider_vaastav.py`
(fetch/validate/resolve for `team.identity@season`, plus the missing-file
case), 3 in `tests/test_schemas.py` (entity key, required-fields shape,
dataset-key mapping). Every pre-existing assertion that hardcoded a
capability/dataset count was updated, not weakened (`test_schemas.py`'s
14→15-capability set, `DATASET_ENTITY_KEYS`'s new entry,
`test_capabilities_lists_exactly_the_two_served` renamed to `..._three_
served`, `register()`'s "adds provider for both capabilities" test renamed
and extended to three). `tests/test_store_invariants.py` (7 tests, added
to the suite since story 11's own report) re-run and confirmed passing —
untouched by this story, checked because the brief called it out
specifically. All network access mocked in the suite (`FakeFileTransport`,
literal CSV bytes) — zero live calls; live calls are confined to
`scripts/verify_team_identity_season.py` and the two manual
`scripts/backfill.py` runs recorded in §12.3, all run by hand.

### 12.5 What was left open

- **No bulk backfill was run** (story brief: prove the fix on enough
  matches to be convincing, a full run is a separate decision). Live
  fetches this session: one `bootstrap-static/` (cached from prior
  sessions), one 2025/26 matchweek-1 PL API fixtures call (new — the
  season-matched `pl_teams` fetch this story adds), one vaastav
  `2025-26/teams.csv`, one vaastav `2025-26/players_raw.csv` (cached from
  story 10), one PL API lineups call for match 2561899 (new), plus the
  `git stash`-based before/after CLI runs (one new fixtures request each,
  matchweek 2 only — matchweek 1 was already cached).
- **`--identity-season-pl` was removed from `scripts/backfill.py`**, not
  merely left unused — once team identity is season-scoped by construction,
  a second, independently-settable "which season verifies team identity"
  knob no longer describes anything real; keeping it would have been a
  dead parameter whose docstring lied about what it did. Flagging this as
  a CLI-surface change in case anything outside this session's scope was
  relying on it (nothing in the codebase was — `scripts/backfill.py` has
  no test coverage of its own, by design, per story 11's report).
  `--current-season-pl` remains, now doing double duty as the live/archive
  boundary for BOTH identities rather than just the player one.
  `PLProvider._fetch_pl_teams_for_identity()` being called from outside
  `providers/pl.py` (a private method, reached into by `scripts/
  backfill.py`) is a wiring convenience flagged in §12.1 — promoting it to
  a public method would be a small, safe follow-up if another caller ever
  needs the same "season-matched raw pl_teams" fetch.
- **Blueprint §12.5's player/team asymmetry is now closed on both sides.**
  With this story, every identity this codebase resolves (player, team,
  and — downstream of both — match, via `resolve_match`) is season-scoped.
  Nothing currently flags a THIRD identity gap; the next real blocker for
  a full historical backfill is likely capability coverage/cost (§11.2's
  ~4.5x multi-capability finding), not identity resolution.

## 13. Second archive provider: olbauday + a bundled backfill fix — E2b story 10b, 2026-08-21

> Author: XL-Coder · session `s002`
> `src/fplai/providers/olbauday.py` (new), `scripts/verify_olbauday_provider.py`
> (new), `tests/test_provider_olbauday.py` (new). Two new capabilities in
> `schemas.py`: `player.attributes@gameweek`, `gameweek.field_summary@
> gameweek`. Bundled fix: `TEAM_IDENTITY_SEASON` added to `fplai.backfill.
> GRAIN_PLANS` and `_valid_at_for` (was missing entirely — `PROGRESS.md`
> E2, `docs/HANDOFF.md` §3). Numbered §13, not §12 as the story brief said
> — §12 was already taken by story 7b's record by the time this session
> started; the brief was written slightly behind the wiki's actual state.
>
> **§13.5 below was corrected same-day, after the Architect reviewed the
> cached data this session pulled.** The original `observed_at` ruling
> (join `gameweek_summaries.csv`'s `snapshot_time`) was implemented exactly
> as specified and still leaked — `snapshot_time` turned out to be a stale
> file-generation stamp, not an observation time. §13.5 now records the
> corrected rule (impute from `deadline_time`) and the evidence behind it;
> it does not describe two versions, only the current, correct one.

### 13.1 Did the framework hold?

**Yes, exactly the story 10 pattern, zero core changes.** `providers/
olbauday.py` composes the SAME `transport.FileTransport` + `decoders.
decode("csv")` pair story 10 built, with its own thin `Provider` adapter
and `register()`. Confirmed by `git diff --stat` against `transport.py`,
`decoders.py`, `registry.py`, `providers/base.py`, `providers/vaastav.py`
and `identity.py`: **no output at all** — genuinely zero lines changed in
any of them — plus `tests/test_provider_olbauday.py::
test_gate_new_provider_is_adapter_plus_registry_entry`, exercising
`register()` -> `registry.resolve()` -> `provider.fetch()` end-to-end with
nothing but the new adapter file and schemas.py's additive entries. The
only production-code touches outside the new adapter file were `schemas.py`
(additive — two new `CapabilityKey`/`FactTableSchema` pairs and two new
`DATASET_ENTITY_KEYS` entries, exactly what §4's "adding a new provider"
steps anticipate) and the bundled `backfill.py` fix (§13.6 below), which
was explicitly in scope, not scope creep.

### 13.2 The real repo layout — verified live 2026-08-21, and two things `docs/wiki/data-sources.md` §3.2 got wrong

§3.2 is a survey (GitHub directory listing + one sampled file), not a live
probe of every season — this story found two structural facts it missed:

- **Branch is `main`, not `master`** (vaastav's branch). Confirmed via the
  GitHub contents API before ever constructing a URL.
- **Every file lives under a `data/` prefix**: `data/{season}/playerstats
  .csv`, not `{season}/playerstats.csv` as §3.2's listing implied.
- **Season string is `"YYYY-YYYY"`** (e.g. `"2025-2026"`), NOT vaastav's
  `"YYYY-YY"` (`"2025-26"`) — verified by listing `data/` itself:
  `2024-2025`, `2025-2026`, `2026-2027`.

**2024-2025 uses a structurally different, older generation of this repo's
scraper** — not a minor path tweak. `data/2024-2025/` has NO flat
`playerstats.csv`/`gameweek_summaries.csv` at all; instead: `matches/`,
`playermatchstats/`, `players/`, `playerstats/`, `teams/` subdirectories.
`playerstats.csv` does exist, one level deeper
(`playerstats/playerstats.csv`); `gameweek_summaries.csv` has NO
equivalent anywhere in that season's tree. `providers/olbauday.py` handles
the path difference by trying the flat path first and falling back to the
nested one on a `TransportError` (a real 404), rather than hardcoding a
season->layout lookup table — self-adapting to a repo that has already
restructured once, at the cost of one wasted 404 request per old-layout
season fetch (documented in the adapter's docstring and in `scripts/
verify_olbauday_provider.py`'s own request-count accounting).

### 13.3 Schema drift — real, verified, and severe (not a column or two)

`playerstats.csv`: 2025-2026 and 2026-2027 have **87 columns**, identical
headers (byte-for-byte, verified). **2024-2025 has 58.** The 29 missing
columns include `web_name`/`first_name`/`second_name` (**no player name in
this file for that season, at all**), `news`/`news_added`,
`minutes`/`goals_scored`/`assists`/`clean_sheets`, and the entire
defensive-contribution family (`defensive_contribution`, `tackles`,
`clearances_blocks_interceptions`, `recoveries` — consistent with DC not
existing as an FPL scoring category before 2025/26, blueprint §11, not a
scraping bug). `schemas.py`'s `required_fields` for `player.attributes@
gameweek` is the verified intersection across all three seasons (22
fields, listed in that module's comment) — same drift-handling philosophy
as vaastav's `opta_code`: present-but-optional, never null-padded to fake
a uniform shape. `entity_key = (season, gw, id)`, verified as genuinely
unique within one season's file for all three seasons (29,978 / 27,657 /
599 rows respectively, zero duplicate `(gw, id)` pairs in any of them).

`gameweek_summaries.csv` only exists for 2025-2026 and 2026-2027 (identical
29-column headers, both verified) — 2024-2025 has none at all (confirmed
404, and no equivalent file anywhere in that season's directory tree).
`entity_key = (season, id)` where `id` is the gameweek number.

### 13.4 `player.attributes@gameweek` is NOT `player.gameweek_stats@gameweek`

Per the Architect's ruling: registered as a separate `CapabilityKey`
(`player.attributes@gameweek` vs. vaastav's `player.gameweek_stats@
gameweek`) — different MEASURES at the same grain (blueprint §12.1).
vaastav's is match performance (`total_points`, `minutes`, `fixture`,
`was_home`); olbauday's is an attribute snapshot (`selected_by_percent`,
`status`, `now_cost`, `chance_of_playing_next_round`). `tests/
test_provider_olbauday.py::
test_register_does_not_collide_with_vaastav_player_gameweek_stats`
registers BOTH providers in one registry and confirms each capability
resolves to its own provider. `selected_by_percent` (olbauday) is a
PERCENTAGE; vaastav's `selected` is a raw ownership COUNT — both schemas'
descriptions say so explicitly, and no code in this story ever compares
the two.

### 13.5 `observed_at` — corrected 2026-08-21: `snapshot_time` is a stale file-generation stamp, not an observation time

**The version of this section this story first shipped with was wrong.**
It joined `gameweek_summaries.csv`'s `snapshot_time` straight in as
`observed_at` and characterised the result as merely "the mirror image of
leakage" — imprecise, not dangerous. The Architect reviewed the actual
cached data and found that framing itself understated the defect: it
**was** leakage, of the kind a downstream query cannot detect, because the
leakage primitive (`BitemporalStore.as_of()`) was working exactly as
designed against a bad input. This section now records the corrected
finding, the live evidence for it, and the corrected rule — not two
competing versions.

**Evidence 1 — `snapshot_time` is constant across a whole season's file.**
Verified live for both seasons that have `gameweek_summaries.csv`: 2025-
2026 stamps all 38 rows `2025-08-17T04:46:20.565427Z`; 2026-2027 stamps
all 38 rows `2026-07-23T15:00:09.957449Z`. The 2025-2026 file's GW38 row
(deadline 2026-05-24) already carries `finished=True` and a real
`average_entry_score` at that SAME constant stamp — the file was clearly
regenerated well after the season ended and simply kept its original
stamp. The 2026-2027 file confirms it from the other direction: stamped
2026-07-23 (pre-season), all 38 rows `finished=False`, no scores anywhere.
This is not "inconsistent" (§3.2's characterisation, from a single sampled
row) — it is a **file-generation timestamp**, unrelated to any individual
gameweek's own timeline.

**Evidence 2 — the polarity check the Architect required before any
design work: is a `playerstats.csv` row for gameweek N pre-deadline or
post-gameweek?** `playerstats.csv` carries cumulative `bootstrap-static`
fields (`total_points`, etc.); vaastav's `player.gameweek_stats@gameweek`
(already in `data/store/vaastav_player_gameweek_stats`, no live call
needed) carries the SAME season's PER-gameweek points. Summing vaastav's
points through gw=N-1 vs. through gw=N and comparing against olbauday's
cumulative `total_points` at gw=N, for 8 players x 4 gameweeks of 2025-26
(32 comparisons):

```
id=430 (Haaland)   gw=5:  olbauday=46   vaastav cum-through-gw4=37   cum-through-gw5=46
id=430 (Haaland)   gw=10: olbauday=98   vaastav cum-through-gw9=85   cum-through-gw10=98
id=5   (Gabriel)   gw=5:  olbauday=25   vaastav cum-through-gw4=23   cum-through-gw5=25
id=21  (Rice)      gw=10: olbauday=63   vaastav cum-through-gw9=50   cum-through-gw10=63
... (28 more, same pattern)
```

Every one of the 32 comparisons matched the cumulative sum **THROUGH gw=N**
(never through N-1). `playerstats.csv` is a **POST-gameweek snapshot**,
not a pre-deadline one — the row for GW N reflects GW N having already
been played.

**Why the combination is dangerous, not merely imprecise:**
`BitemporalStore.as_of(dataset, ts)` filters `WHERE observed_at <= ts`,
partitioned by `DATASET_ENTITY_KEYS[dataset]`. `olbauday_player_
attributes`'s entity key is `(season, gw, id)` — every gameweek is its own
entity. With the original ruling, EVERY gameweek in a season shared the
SAME constant `observed_at` (the file-generation stamp). So
`as_of(GW10's deadline)` would have returned the GW38 row for every
player — May-2026 price, ownership% and injury state surfaced in an
October-2025 query — and nothing downstream would flag it, because
`as_of()` was doing exactly what it is designed to do against a
timestamp that was simply wrong.

**The corrected rule: `observed_at` is IMPUTED from `gameweek_summaries
.csv`'s own `deadline_time` (verified correct per-row), never from
`snapshot_time`.** Both capabilities here are post-gameweek content
(evidence 2 above for `player.attributes@gameweek`; `gameweek.field_
summary@gameweek`'s `average_entry_score`/`chip_plays`/`most_captained`
are definitionally unknowable before a gameweek is played), so a row for
gameweek N gets `observed_at` = **gameweek N+1's own `deadline_time`** —
the latest instant by which N's information is certainly public, without
claiming an earlier, unverified one. Deliberately conservative: the true
publication instant is probably somewhat earlier (shortly after GW N's own
matches finish), but blueprint §12.5 requires "no earlier than verified",
not "as early as plausible". For a season's LAST gameweek (no N+1 row),
falls back to GW N's own `deadline_time` plus a documented offset
(`_FINAL_GAMEWEEK_OBSERVED_AT_OFFSET = timedelta(days=7)`, stated in both
the adapter and the schema description, never an implicit magic number).
Rung 1 (a genuine per-row capture timestamp on `playerstats.csv` itself)
is unchanged and checked first; none of the three seasons has one, so
every fetch today reaches the imputation rung.

**The imputation is labelled ON THE ROWS.** `observed_at_source` and
`observed_at_imputed` are now REQUIRED columns on both schemas (CLAUDE.md
rule 3 — `FetchResult.meta` is lost the moment a caller writes to the
store; a column survives). The raw `snapshot_time` value is kept in
`FetchResult.meta` under `file_generation_stamp` for forensics only,
explicitly not named or used as an observation time anywhere in the
adapter. `lag_hours` (this section's original version) has been REMOVED —
distance to a stale stamp measured nothing real.

**2024-2025 still raises `ProviderError`, but the reason changed.** It is
not "no snapshot source" — it is "no in-archive deadline source":
`gameweek_summaries.csv` (the only file in this archive carrying
`deadline_time`) does not exist for that season at all. Other in-repo
sources could supply a deadline for 2024-2025 (vaastav's `kickoff_time`,
the FPL API's own `events`, or PL API fixtures) but wiring one in is a
**deferred decision** (§13.9), not built here.

**Live-proven**, `scripts/verify_olbauday_provider.py` (rerun 2026-08-21
after the correction): gw=1 and gw=2 of both current-layout seasons each
get a DIFFERENT, correctly-later `observed_at` (e.g. 2025-2026 gw=1 ->
`2025-08-22T17:30:00+00:00`, gw2's own deadline — not the constant
`2025-08-17T04:46:20Z` stamp, which is visible in the log only as the
labelled, unused `file_generation_stamp`); 2024-2025 raises with the
corrected message ("NO IN-ARCHIVE DEADLINE SOURCE"); the nested-path
fallback for 2024-2025's `playerstats.csv` is unaffected by any of this
and still succeeds independently.

**Recommendation for Phase 2:** the imputed `observed_at` is a "no later
than" bound, not a tight point-in-time capture — a real gap remains
between when a gameweek's post-match state actually became public
(shortly after GW N's own deadline) and when this adapter is willing to
claim it (GW N+1's deadline, up to ~a week later). That gap is
conservative (safe) rather than leaking, but it does mean a minutes/
pricing model trained on this capability sees slightly staler information
than an at-deadline model theoretically could. Whether that gap matters is
a modelling call, not an ingestion one.

### 13.6 Bundled fix — `team.identity@season` had no `GrainPlan`

Confirmed absent from `GRAIN_PLANS` before this story (`backfill.py:440`,
matching `PROGRESS.md` E2 / `docs/HANDOFF.md` §3): `BackfillSpec(
capabilities=(TEAM_IDENTITY_SEASON,), ...)` raised `BackfillError`
unconditionally via `__post_init__`'s "unknown capability" check, even
though `providers/vaastav.py` has served this capability since story 7b.
`_build_team_identity_units` mirrors `_build_player_identity_units`
exactly (one unit per season, no gameweek/matchweek parameter); a
`_valid_at_for` branch was also required (anchoring at `_season_start_date`,
same convention as `PLAYER_IDENTITY_SEASON`) — without it, a resolved unit
would have raised `BackfillError` the moment `run()` tried to write it to
the store, a second half of the same gap the brief's one-line description
didn't call out explicitly.

**Proven by deliberately breaking it (handoff lesson #5 discipline):**
removed the `GRAIN_PLANS` entry, removed the `_valid_at_for` branch, and
confirmed the SAME `BackfillError` a live caller would have hit is exactly
what the new tests catch:

```
test_team_identity_season_has_a_static_grain_plan             FAILED (BackfillError: no backfill enumeration strategy declared)
test_team_identity_units_one_per_season_no_gameweek_param      FAILED (same)
test_run_resolves_team_identity_season_units_end_to_end        FAILED (same)
test_valid_at_for_team_identity_season_uses_season_start        FAILED (BackfillError: no valid_at convention declared)
test_run_resolves_team_identity_season_units_end_to_end        FAILED (same, second break)
```

Both fixes restored, all five green again. `test_run_resolves_team_
identity_season_units_end_to_end` additionally proves the full path — a
`FakeProvider` returning real-shaped team-identity rows, written through
`BackfillOrchestrator.run()` into a real `BitemporalStore`, and read back
via `store.latest("vaastav_team_identity")`.

### 13.7 Deliberately not built here

- **Shot-level data under `By Gameweek/GW{N}/`** — explicitly deferred per
  the story brief. Not built, not schema'd, not probed beyond confirming
  the directory exists (it does, per `docs/wiki/data-sources.md` §3.2).
- **No backfill `GrainPlan` for the two new olbauday capabilities.** The
  brief's "add GrainPlans" line refers to the bundled `team.identity@
  season` fix only (§13.6) — nothing in "what to build" asks for
  `player.attributes@gameweek`/`gameweek.field_summary@gameweek` to be
  orchestrator-enumerable, and wiring it properly would need a real design
  decision this story didn't have scope for: unlike vaastav's
  one-file-per-gameweek archive, olbauday's `playerstats.csv` is ONE FILE
  PER SEASON covering every gameweek, so a naive one-`WorkUnit`-per-gw
  enumeration (38 units/season) would re-decode the same cached ~9.6 MB
  CSV up to 38 times per season for no new network cost but real CPU cost
  — worth a real GrainPlan variant (e.g. `kind="static-whole-file"`) rather
  than reusing `_build_gameweek_stats_units`'s shape unexamined. Flagging
  for whichever story next wants this in `backfill.py`.
- **No bulk multi-season backfill was run** (story brief). Live requests
  this session: exactly the seven `scripts/verify_olbauday_provider.py`
  accounts for (§13.5's numbers come from that one run) — five 200s now
  cached under `cache/olbauday/`, two 404s that `FileCache` deliberately
  never caches (see that class's own docstring) and so are re-issued live
  on every subsequent run of the verify script; noted explicitly in the
  script's own docstring rather than overclaiming "zero requests on a
  second run."

### 13.8 Test suite

`uv run pytest -q` (or `.venv/Scripts/python.exe -m pytest -q`): **326
passed** — 291 pre-existing (through story 7b, untouched) + 35 new:
`tests/test_provider_olbauday.py` (26, including the two store-integration
leakage-invariant tests below), 6 new `schemas.py`-capability tests
appended to `tests/test_schemas.py` (one of which — the entity-key test —
was itself corrected same-day to check `observed_at_source`/`observed_at_
imputed` in place of the dropped `snapshot_time` requirement), 4 new
team-identity-GrainPlan tests appended to `tests/test_backfill.py`, plus
the two pre-existing hardcoded-count assertions in `test_schemas.py`
(`test_canonical_schemas_cover_all_fpl_pl_and_vaastav_capabilities`,
`test_dataset_entity_keys_covers_every_store_dataset_this_slice_writes`)
updated to include the two new capabilities/datasets, not weakened — same
discipline as every prior story's test-coverage section.

**The two leakage-invariant tests (§13.5's correction, coordinator-directed
Task 3)** are the most load-bearing new tests this session added:
`test_buggy_reproduction_violates_both_invariants_before_the_correction`
and `test_corrected_adapter_satisfies_both_invariants` both build a REAL
`fplai.store.BitemporalStore` (tmp_path-backed, fully offline) and assert
(1) no written row's `observed_at` precedes its own gameweek's
`deadline_time`, and (2) `store.as_of()` at gameweek N's deadline never
returns a row for a gameweek later than N. The first test reproduces the
ORIGINAL bug (constant `snapshot_time` as `observed_at`) via a small,
clearly-labelled helper duplicated in the test file (the real bug was
removed from the adapter, so nothing to import) and confirms it violates
BOTH invariants — 4/4 rows fail invariant 1, and querying `as_of()` at
GW2's deadline leaks GW3 and GW4's rows (2 rows later than N=2), the exact
failure mode the coordinator described. The second test proves the
CORRECTED, real `OlbaudayProvider` satisfies both. **Proven, not assumed**
(handoff lesson #5): `_resolve_player_attributes_observed_at` was
temporarily reverted in place (not just the test-file helper) to
reintroduce the original bug in the actual code path, `test_corrected_
adapter_satisfies_both_invariants` was confirmed to fail against it
(4 real invariant-1 violations), then the fix was restored and the suite
re-confirmed green. Every other new test in `test_backfill.py` was
similarly proven to fail before its fix (§13.6); the `test_provider_
olbauday.py` flat->nested-fallback test was independently proven to fail
by deliberately breaking the corresponding adapter code, then restored.

All network access in the pytest suite is mocked (`FakeFileTransport`,
raising `TransportError` for any unlisted URL, mirroring a real 404) —
zero live calls in the suite; live calls are entirely confined to
`scripts/verify_olbauday_provider.py`, run by hand, seven live requests
total this session (both before and after the correction — the correction
changed no network behaviour, only how the already-fetched bytes are
interpreted).

### 13.9 What was left open

- **Whether 2024-2025 should get a deadline source from elsewhere is a
  deferred decision, not a built feature.** `gameweek_summaries.csv` (this
  archive's only `deadline_time` source) does not exist for 2024-2025, so
  `player.attributes@gameweek`/`gameweek.field_summary@gameweek` are both
  unfetchable for that season. Other in-repo sources DO carry a deadline
  for that season — vaastav's `kickoff_time` (already in the store),
  the FPL API's own `events`, or PL API fixtures — any of which could
  supply a deadline for a future correction. Not built here: picking one
  is a real design choice (which source is authoritative, how it's wired
  into an adapter that otherwise depends on nothing but its own archive)
  that this story didn't have scope for.
- **The imputation gap (§13.5's "Recommendation for Phase 2") is a
  Phase-2 input, not a Phase-2 decision.** The corrected `observed_at` is
  conservative (a genuine "no later than" bound) but not tight — there is
  slack between a gameweek's actual post-match publication and GW N+1's
  deadline. Whether that slack matters for a given model is a modelling
  call, not an ingestion-layer one.
- **`GrainPlan` support for the two new capabilities** — see §13.7,
  unaffected by this correction.
- **Licence remains undeclared** (`data-sources.md` §3.2) — internal use
  only, same discipline as vaastav; `cache/olbauday/` is gitignored,
  nothing from this archive is committed.

### 13.10 A second bundled fix — `BackfillOrchestrator` was discarding every provider's `observed_at`

Found by the coordinator during independent review of §13.5's correction,
same session: `BackfillOrchestrator.run()`'s `handle()` (`backfill.py`,
in the `else` branch after a successful `provider.fetch()`) called
`self.store.write(..., observed_at=datetime.now(timezone.utc), ...)` —
the run's OWN timestamp, never `result.observed_at`, the value the
provider itself computed. This is a general orchestrator bug, not specific
to olbauday, but olbauday's build is what surfaced it: §13.5's whole
correction — imputing a real, historical `observed_at` instead of
`now()` — would have been silently thrown away the moment anyone wired
`player.attributes@gameweek`/`gameweek.field_summary@gameweek` into
`GRAIN_PLANS` and ran a real backfill, because the orchestrator was never
going to persist what the adapter computed.

**Fix:** `handle()` now persists `result.observed_at` unchanged. `_valid_at
_for`'s docstring (which asserted `observed_at` is "correctly `now()`
elsewhere in this module" — true when written, false after this fix) is
rewritten to state the actual rule: `valid_at` is when a fact became true
in the world, `observed_at` is when THIS SYSTEM learned it, and neither is
the orchestrator's own `now()` — a provider's `observed_at` is that
provider's provenance to set, and the orchestrator has no standing to
overwrite it (blueprint §12.3).

**Test, proven red first (handoff lesson #5):** `tests/test_backfill.py::
test_run_persists_the_providers_own_observed_at_not_the_run_time` supplies
a `FetchResult` stamped with a distinct historical instant
(`2019-03-14T09:26:00Z` — nowhere near any plausible "now") through a real
`BackfillOrchestrator.run()` into a `BitemporalStore`, and asserts the
stored row's `observed_at` equals exactly that instant. Confirmed failing
against the pre-fix code (`observed_at=datetime.now(timezone.utc)`
reinstated temporarily) — the assertion compares against
`2019-03-14T09:26:00`, and the stored value was the test's own run
instant (2026-08-21), so it failed exactly as expected — then confirmed
passing again after restoring the fix.

**Existing on-disk data — checked, not assumed.** `data/store/` currently
holds `vaastav_player_gameweek_stats` (179,960 rows, 256 batches) and
`vaastav_player_identity` (5,404 rows, 7 batches) as the only datasets a
real `BackfillOrchestrator.run()` could plausibly have written (no
`pl_*` or `vaastav_team_identity` datasets exist on disk at all — story
11's PL API interrupt/resume proof and this story's own bundled
`team.identity@season` fix both ran against throwaway/test stores, never
the production one, consistent with every session's "no bulk backfill"
discipline). Queried directly: every batch's `observed_at` values cluster
within a ~4-minute window on 2026-08-20 (`vaastav_player_gameweek_stats`:
17:17:04 -> 17:20:56), and — the number that actually answers the
question — `providers/vaastav.py` itself stamps `observed_at=datetime.now
(timezone.utc)` INSIDE `fetch()`, at construction time, for every one of
its three capabilities (confirmed by inspection: `vaastav.py` lines 186,
208, 229; `fpl.py` and `pl.py` do the same for every capability they
serve). So even under the pre-fix orchestrator, the value actually
written was `datetime.now()` called moments after the provider's OWN
`datetime.now()` call, in the same synchronous call chain with no I/O or
sleep between them — a gap of low milliseconds, not the years a
historical-archive adapter's real `observed_at` would differ by. **No
currently on-disk data is materially affected by this fix** — every
existing provider's `FetchResult.observed_at` was already
indistinguishable from write-time `now()`; the bug was real and worth
fixing before it was ever exercised by a provider whose `observed_at`
ISN'T `now()`, but nothing already ingested needs correction or
re-ingestion.

**Confirmed unchanged, per the coordinator's explicit instruction:** no
`GrainPlan` was added for `player.attributes@gameweek` or `gameweek.
field_summary@gameweek` — both remain absent from `GRAIN_PLANS` (§13.7's
open design question about a whole-season-file GrainPlan variant is still
open, deliberately not decided in passing).

## 14. The Odds API adapter — E2b story 8, 2026-08-21

> Author: XL-Coder · session `s002`
> `src/fplai/providers/odds.py` (new — `OddsProvider`, `_TeamNameIndex`,
> `_PlayerNameIndex`, `build_transport`, `suppress_leaky_third_party_debug_
> logging`), `scripts/snapshot_odds.py` (new, capture), `scripts/verify_
> odds_provider.py` (new, live verification), `tests/test_provider_odds.py`
> (new, 18 tests). Two new capabilities in `schemas.py`: `match.odds@
> fixture`, `player.goal_odds@fixture`. `src/fplai/transport.py` changed —
> see §14.2, the only authorised core touch. `docs/wiki/data-sources.md`
> §6.1 made internally consistent (it carried a superseded claim, a
> correction block, AND a trailing line restating the superseded claim).
>
> **This story surfaced three real, live bugs — an identity join on the
> wrong FPL column, a genuine key-material leak in a third-party library's
> own logger, and a store-corrupting timestamp-type bug that is still
> sitting in the live production store as this record is written.** None
> were caught by the mocked test suite; all three were caught only because
> the story's own discipline (live verification before declaring done, a
> real capture run) was followed. §14.5 is the one that needs the
> Architect's decision before any further capture happens.

### 14.1 Did the framework hold?

**Yes, for the adapter itself.** `providers/odds.py` + `register()`,
exactly the established pattern (§4's steps) — `git diff --stat` against
`registry.py`, `providers/base.py`, `providers/fpl.py`, `providers/pl.py`,
`providers/vaastav.py`, `providers/olbauday.py`, and `identity.py`: **zero
lines changed in any of them.** `identity.py` in particular was
deliberately left untouched even though this provider needed genuinely new
identity-resolution logic — the Odds API speaks names, not the numeric ids
every existing `PlayerIdentityMap`/`TeamIdentityMap` join on, so the
name-based resolution (`_TeamNameIndex`, `_PlayerNameIndex`) lives entirely
inside `providers/odds.py`, reusing `fplai.identity.IdentityError` only for
a consistent exception taxonomy. `tests/test_provider_odds.py::
test_gate_register_is_adapter_plus_registry_entry_zero_core_changes`
exercises `register()` → `registry.resolve()` → `provider.fetch()`
end-to-end with nothing but the new adapter file and `schemas.py`'s
additive entries, same as every prior story's gate test.

### 14.2 The one authorised core change — `CreditTracker`/`QuotaTracker` durability, not a redesign

The brief drew a specific line: fix `CreditTracker`'s incomplete
implementation (in-memory `_spent`, story 4) behind an UNCHANGED interface;
stop and report if the fix required reshaping `CreditTracker`'s or
`HttpTransport`'s interface. It held:

- `CreditTracker`/`QuotaTracker` gained an optional `state_path: Path |
  None = None` field (default preserves the exact original in-memory-only
  behaviour — every pre-existing test/caller unaffected, proven by the full
  pre-existing suite staying green throughout) and a `reconcile(remaining=,
  used=, tolerance=)` method (new, not a change to `consume()`'s existing
  signature). `HttpTransport.get()`'s signature and return shape
  (`(status_code, text)`) are **byte-for-byte unchanged**.
- `HttpTransport.__init__` now derives `state_path` automatically from
  `cache_dir` for `CreditPolicy`/`DailyQuotaPolicy` — every HTTP provider
  using either policy gets durable accounting for free, not just Odds.
  This closes `QuotaTracker`'s identical latent gap too ("fix it too if it
  falls out naturally" — it did, same code shape, ~10 lines).
- `HttpTransport.get()` captures the live response's headers into a new
  `self._last_response_headers` diagnostic attribute (empty on a cache
  hit — nothing to reconcile) and, when a `CreditTracker` is active, feeds
  `x-requests-remaining`/`x-requests-used` into `reconcile()` automatically
  — general infrastructure for ANY credit-based provider whose API reports
  usage this way, not Odds-specific code in the transport layer. `get()`'s
  own return value is untouched; a caller that wants the headers reads the
  new attribute right after calling `get()`, the same way existing tests
  already reach into `transport._quota`/`transport._credits` directly.

**Proven, not just designed** (handoff lesson #5): `tests/test_transport.py`
gained 13 new tests, each proven to fail against the pre-fix code before
being trusted (temporarily reverted `_load_persisted`, confirmed the
persistence tests fail with the exact "10 == 3" mismatch a lost-state bug
would produce, then restored). Full suite before this story: 327. After:
354 (13 transport + 14 provider-level, before §14.5's four additional
regression tests below).

### 14.3 Real response shapes versus blueprint §3.3's claims

Verified live, 2026-08-21, against the real GW1 2026/27 fixture list —
budget: 3 credits for initial recon (outside the transport/cache path, a
throwaway script) + 6 more across two `verify_odds_provider.py` runs while
fixing the bugs below + 47 for the one real capture run (§14.6) = ~56
credits this session, all measured from response headers, not estimated.
The **≤20-credit verification budget was respected** — the recon +
verify-script spend was 9 credits; the capture run is separately budgeted
(§14.6) and was pre-authorised by the coordinator mid-session.

- **`match.odds@fixture` matches §3.3 closely.** `markets=h2h,totals,
  regions=uk`, ONE call, cost=2, returns ALL 10 GW1 fixtures. One surprise:
  Betfair Exchange (`betfair_ex_uk`) also returns an **unrequested
  `h2h_lay` market** (the exchange's lay side) alongside the requested
  `h2h` — not documented, not asked for, handled by storing it as its own
  `market_key` value rather than dropping it or coercing it into `h2h`
  (blueprint §3.3.1/rule 5's "nothing silently discarded" applied to a
  market the API volunteered, not a distribution).
- **`player.goal_odds@fixture`'s "5 books, 212 outcomes/fixture" is an
  observed MAXIMUM, not a guarantee — confirmed live, not assumed.** The
  first GW1 fixture fetched (Arsenal v Coventry City, a newly-promoted
  side) returned only **3 books** (`betfair_ex_uk`, `williamhill`,
  `skybet`) and **107 outcomes** (42+42+23). §3.3's 212/5-books figure was
  real for whichever fixture the Architect sampled 2026-08-19, but book
  coverage is per-fixture, not a sport-wide constant — same shape as PL
  API's `team_match_stats@match` finding (180 vs 160 keys per side,
  §9.4/§13's precedent) applied to a THIRD capability now. `outcome.name`
  is `'Yes'` on every one of the outcomes observed live across both
  fixtures checked — confirms §3.3's de-vig note (anytime-goalscorer books
  quote only the Yes side) rather than assuming it.
- **`player_shots_on_target` remains deliberately deferred**, not
  registered — unchanged from the brief's instruction. ~10 more
  credits/gameweek, and §3.3 names de-vigged anytime-goalscorer as the one
  market to keep if only one could be kept.

### 14.4 Identity resolution — three findings, two closed live, one reported as-is

**Teams: 20/20, live-verified twice** (once during the buggy run, once
after the fix — team resolution was correct both times; the bug below was
entirely on the player side). The Odds API's team-name convention is the
PL API's/Opta's full-name style (`'Manchester City'`), not FPL's own short
`teams[].name` (`'Man City'`) — **the SAME four exceptions blueprint §3.5
already documented for the PL-API-vs-FPL name mismatch** (`Man Utd`/
`Manchester United`, `Man City`/`Manchester City`, `Spurs`/`Tottenham
Hotspur`, `Nott'm Forest`/`Nottingham Forest`), now confirmed to be the
identical gap against a SECOND provider, not a new one. `providers/
odds.py::_TEAM_NAME_ALIASES` closes it explicitly (4 verified entries);
the other 16/20 resolve via a documented noise-word normalisation. This
should be promoted alongside blueprint §3.5's existing PL API row — team
identity naming drift is a property of "the Odds/PL side speaks full
official names," not a PL-API-specific quirk.

**Players: 41/42 on the one live fixture fully worked through (Arsenal v
Coventry City), one genuine miss — reported in full per the brief, not
smoothed over.** The Odds API uses full LEGAL names, not FPL's `(first_
name, second_name)` pair, and the two disagree in several distinct, real
ways found live:

| Odds API name | FPL identity | What differs |
|---|---|---|
| `Ben White` | first=`Benjamin`, second=`White` | nickname vs legal first name |
| `Magalhaes Gabriel` | first=`Gabriel`, second=`dos Santos Magalhães` | surname-first order, truncated surname |
| `Kaine Hayden` | first=`Kaine`, second=`Kesler-Hayden` | truncated hyphenated surname |
| `Martin Odegaard` | second=`Ødegaard` | `ø` — NFKD does not fold it (not a combining-mark letter) |
| `Ogochukwu Onyeka Frank` | first=`Frank`, second=`Onyeka` | **UNRESOLVED** — 3-part name, FPL's `first_name` placed LAST, extra given name FPL doesn't carry at all |

Three EXACT rungs (documented in `providers/odds.py`'s module docstring
and `_PlayerNameIndex`'s) close 41/42: normalised full-name match; a
normalised-surname fallback (the last whitespace token of the Odds name
against FPL's `web_name` or one of its hyphen-split parts) restricted to
the fixture's own two squads; anything else raises, collected across the
whole event before raising (same pattern `providers/pl.py`'s
`_fetch_lineups` already uses), never a similarity/edit-distance fuzzy
match. **`Ogochukwu Onyeka Frank` is the one that didn't resolve** — no
exact, deterministic rule closes it without risking a false match
elsewhere in a real capture (permuting every name-token order and testing
pool membership is exactly the guess-until-something-matches heuristic
blueprint §12.5 forbids). **Recommendation, not a decision made here**:
either accept this class of gap as-is (the raw quote is still recoverable
from `HttpTransport`'s own response cache — nothing is destroyed, just not
identity-resolved yet) or start a small, explicitly human-verified
per-player alias table the way `_TEAM_NAME_ALIASES` already does for
teams — never a bulk/automated guess.

**A real bug found only by running the adapter live, not by the mocked
test suite: `elements[].team` is FPL's `id` (season-local 1-20 slot), NOT
`code` (the stable identifier `_TeamNameIndex.resolve()` and `home_team_
code`/`away_team_code` correctly use).** `_PlayerNameIndex`'s first version
filtered `elements` by `code` against the `team` column — comparing two
different numbering systems. The first real run of `scripts/verify_odds_
provider.py` caught it immediately: **0/42 player names resolved** where
41/42 was expected, because the candidate pool it built was wrong (an
empty or coincidentally-wrong set of players, not the fixture's actual two
squads). Fixed by translating `code` → `id` via the `teams` snapshot before
filtering `elements`; `tests/test_provider_odds.py`'s fixtures were
corrected to use DIFFERENT numbers for `id` and `code` specifically so this
exact class of bug can never pass silently again (the original fixtures
happened to reuse the same small integers for both, which is precisely how
this shipped past the mocked suite the first time) — proven by deliberately
reintroducing the bug and confirming the corrected fixtures catch it
(`assert 10 == 3` → the real failure mode, not a tautology).

### 14.4a The §12.5 re-fetchability exception — implemented, and the full 10-fixture live result

**The single-fixture 41/42 result in §14.4 above was what triggered this
amendment, not the end state.** On the FIRST real capture run (before the
exception existed), raising on that one unresolved name discarded 41 GOOD
observations from that fixture AND every other fixture whose goalscorer
odds this adapter never even reached — the run lost **9 of the 10
fixtures' worth of `player.goal_odds@fixture`**, captured 1. Blueprint
§12.5 was amended the same day (see the blueprint's own amendments table
and this document's §14.6-adjacent history) to add the **re-fetchability
exception**: on a live-only source with no historical endpoint, an
unresolved entity is preserved (`player_element_id = null`,
`identity_resolved = false`, `player_name_raw` kept verbatim) rather than
discarding the whole batch. Scoped explicitly to `player.goal_odds@
fixture`'s player-name join only — `match.odds@fixture`'s team-name join
and every re-fetchable source keep raising, unchanged.

**Implementation** (`OddsProvider._fetch_player_goal_odds`,
`schemas.py`'s `PLAYER_GOAL_ODDS_FIXTURE`): the entity key moved from
`player_element_id` (now nullable, cannot key rows) to `player_name_raw`
(always present, what the source actually reports); `identity_resolved`
is a new required boolean column; `player_element_id` is cast to a
genuine nullable `Int64` (never a Null-dtype or sentinel — the exact
class of bug §14.6 already flagged for a different column, avoided here
deliberately). **"Unusable by default" is structural, not a convention a
consumer has to remember**: an ordinary equi-join against `elements` on
`player_element_id == id` excludes unresolved rows automatically (SQL/
Polars NULL never equals anything) — `tests/test_provider_odds.py::
test_fetch_player_goal_odds_unresolved_rows_are_unusable_by_default_via_
plain_join` proves this against a real Polars join, not prose, and was
proven to fail first (temporarily reintroduced a sentinel id instead of
null, confirmed the join then quietly matched the wrong player, restored).
"Loud in three places" (row flagged, name kept, capture reports every
name) is also directly tested, not merely asserted:
`test_fetch_player_goal_odds_preserves_unresolved_rows_never_raises_or_
drops`, `..._reports_unresolved_names_in_meta_loud_place_2_of_3`,
`..._reachable_via_explicit_opt_in`. **No reordering heuristic was added**
for `Ogochukwu Onyeka Frank` — it is stored unresolved with the raw
string, exactly as instructed, a repair-offline candidate rather than a
guess.

**Full 10-fixture live result (2026-08-21 14:26 local, capture #1, §14.7
has the complete numbers)**: **all 10 fixtures captured**, 1,009 total
outcome rows, **933 resolved (92.5%), 76 preserved unresolved (7.5%)
across 36 distinct player names** — every one logged by name at both the
per-event and run-level summary. This is a materially different picture
from the single-fixture 41/42 (97.6%) figure above: book coverage and
name-format messiness both vary per fixture (§14.3's "not a sport-wide
constant" finding applies to identity resolvability too, not just outcome
counts), and several of the newly-seen unresolved names read as fringe/
youth-squad players odds books cover more thinly than FPL's own player
list — plausible, not verified; a repair-offline task for later, not this
story's.

### 14.5 Two security incidents, both found live, both fixed and tested

**1. The known risk, closed as designed.** The Odds API authenticates with
`?apiKey=<key>` in the query string; a naive integration would let that
key reach `HttpTransport.get()`'s `path`/`url` construction and therefore
the cache filename (`ResponseCache._path`'s hash — not itself a leak, but
the FILENAME'S content is), the cache BODY (`ResponseCache.set`'s
`{"url": url, ...}`, a genuine plaintext leak), and any raised error
message. Closed with **zero changes to `transport.py`** — `build_transport()`
attaches the key via `requests.Session.params`, which `requests` merges
into the outgoing request at send time; nothing this codebase constructs
(`path`, `url`, any f-string) ever contains the key at all. Verified
empirically, not just reasoned about: `tests/test_provider_odds.py::
test_build_transport_key_reaches_the_request_but_never_the_cache_or_errors`
mounts a REAL `requests.Session` against an offline custom transport
adapter and asserts (a) the actual outgoing request URL DID carry the key
(auth genuinely works) and (b) no cache file, filename, or raised
`ProviderError`/`TransportError` contains any substring of it — proven to
fail first (temporarily made `build_transport()` embed the key in
`base_url`, confirmed the test catches the exact leaked substring, then
restored). **Also verified against the REAL cache directory from the real
capture run** (§14.6): `grep -l <the real key> cache/odds/*.json` — zero
matches, across every cache file this session's live traffic produced.

**2. Found only by actually running a real capture, not by any test —
urllib3's own DEBUG-level connection-pool logger prints the full request
URL, including the key.** `scripts/snapshot_odds.py --verbose`'s FIRST
real run printed lines like:

```
"GET /v4/sports/soccer_epl/odds?markets=h2h,totals&regions=uk&apiKey=<32 hex chars> HTTP/1.1" 200 3670
```

straight to the terminal — `-v` sets the ROOT logger to `DEBUG`; `urllib3`'s
logger has no level of its own and inherits it via propagation, and its
`HTTPConnectionPool` debug line logs the full request line including query
string. This is a leak in a **third-party library's own logger**, entirely
outside `_redacted_session`'s reach (that function only keeps the key out
of strings THIS codebase constructs; it has no say over what `urllib3`
itself decides to log about the request it sends). **The real key was
printed in this session's terminal output as a direct consequence.** Fixed
by `providers/odds.py::suppress_leaky_third_party_debug_logging()` —
caps `logging.getLogger("urllib3")` to `WARNING` unconditionally, called
both inside `build_transport()` (so every caller gets it automatically,
regardless of whether a script remembers to) and explicitly, early, in
both `scripts/snapshot_odds.py` and `scripts/verify_odds_provider.py`
(defence in depth — the FPL API calls both scripts make before constructing
the odds transport also go through `urllib3`; harmless for THEM since
their URLs carry no secret, but the suppression must not depend on call
order). Proven to fail first (temporarily removed the call, confirmed
`test_build_transport_suppresses_urllib3_debug_logging_as_a_side_effect`
catches it), then restored.

**Recommendation for the user**: the exposed key is a free-tier, personal
`THE_ODDS_API_KEY` — not committed, not pushed, seen only in this local
terminal session's output — so the practical risk is low, but the user
may still want to rotate it given it was genuinely printed in plaintext
once before the fix landed.

### 14.6 A third bug, found on the first real capture run — narrowly a materialisation failure, not corruption (corrected 2026-08-21)

**This section originally said the two written batches were "unreadable"
and raised "for every row" — imprecise, and the Architect corrected it
after independently checking the actual failure mode. Restated here
precisely, because the distinction changed the decision that followed
it.**

**What is actually true**: the data is fully intact. DuckDB counts 811
and 91 rows from the two files without complaint; the dataset glob
resolves; every NAIVE column (`season`, `provider_event_id`, prices, etc.)
materialises through DuckDB's Python path with no error at all; Polars
reads every column, including the tz-aware ones, directly. **The failure
is narrowly confined to one step**: `BitemporalStore.observations()`/
`as_of()`'s DuckDB-to-Python conversion of the two tz-AWARE columns
(`commence_time`, `market_last_update`) specifically — that step raises
`InvalidInputException: Required module 'pytz' failed to import`, and the
exception text names the missing MODULE, not a damaged file. "Corrupt,
must be discarded" and "intact but unreadable by one code path for two
columns" are different failure modes and point to different remedies —
stating this precisely matters, not just as a courtesy.

**Root cause, unchanged from the original finding**:

- `providers/odds.py::_parse_iso` originally returned genuinely tz-aware
  `datetime` objects (`tzinfo=timezone.utc`) for `commence_time`/`market_
  last_update` — the FIRST capability in this store to put a truly
  tz-AWARE datetime into a capability's own DATA column (every other
  provider's in-`rows` timestamp column is naive: `providers/pl.py`'s
  `kickoff` via a naive `datetime.strptime`, `providers/vaastav.py`'s
  `kickoff_time`, etc. — only `FetchResult.observed_at`/`valid_at`,
  BITEMPORAL METADATA rather than a data column, are tz-aware elsewhere,
  and `store.write()` evidently normalises those two specific columns to
  naive UTC internally before persisting, which is why every OTHER
  dataset/column in this store reads fine).
- Polars/pyarrow wrote that column as a genuine Parquet `TIMESTAMP WITH
  TIME ZONE`. DuckDB's Python conversion path for a genuine TIMESTAMPTZ
  column requires `pytz`, which is **not a project dependency** (blueprint
  §3.2 only requires `tzdata`, a different package solving a different
  problem: Python's own `zoneinfo`, not DuckDB's TIMESTAMPTZ-to-Python
  conversion).
- **Fixed in the adapter**: `_parse_iso` now returns naive UTC datetimes,
  matching the established in-`rows` convention (`.replace(tzinfo=None)`
  after parsing). `scripts/snapshot_odds.py::_earliest_valid_at` was
  updated correspondingly — it derives `valid_at` (a bitemporal metadata
  argument, which MUST be tz-aware, same as everywhere else in this
  codebase) from the now-naive `market_last_update` column by re-attaching
  UTC, the exact pattern `fplai.backfill._parse_kickoff_value` already
  uses for PL API's equally-naive `kickoff` column. **Proven, not assumed**:
  `tests/test_provider_odds.py` gained two tests — one asserting
  `commence_time`/`market_last_update` are naive at the Polars-schema
  level, and one that writes through a REAL, `tmp_path`-backed
  `BitemporalStore` and reads it back via the same DuckDB path
  `scripts/snapshot_odds.py` uses (handoff lesson #6: "tests on fresh
  fixtures cannot catch production bugs" — this one deliberately exercises
  Parquet + DuckDB, not just in-memory Polars). Both were confirmed to
  fail with the EXACT live error (`InvalidInputException ... pytz`) before
  the fix, and pass after.

**Why the write path never objected: `FactTableSchema.validate()` checks
column PRESENCE, not dtype.** Worth stating plainly for whoever adds the
next provider, per the Architect's instruction — this is not being closed
now, only recorded. `schemas.py`'s own module docstring already says so
("Checks column PRESENCE only, not dtype — raw dtypes vary by provider and
by row... normalisation is the adapter's job, not this pure check's"), and
that design choice is reasonable in general (dtype does legitimately vary
across providers and eras). But it means NOTHING between the adapter and
the Parquet file objects to a column being tz-AWARE when the store's own
established (implicit, undeclared anywhere) convention is that in-`rows`
timestamp columns are naive — a genuinely new, well-typed, schema-passing
`FetchResult` still reached the real store and only failed three steps
later, at a DIFFERENT layer (DuckDB's read path), for a dependency reason
with no connection to `schemas.py` at all. A future provider could
reintroduce this exact class of bug and `FactTableSchema.validate()` would
not catch it, because dtype was never its job.

**Resolution, decided and executed by the Architect (not by this story)**:
both already-written batches were **quarantined, not deleted** —
`data/quarantine/2026-08-21-tzaware-odds/` — preserving the observations
intact and recoverable rather than treating them as garbage. `pytz` was
deliberately NOT installed: "adding a dependency so one provider can keep
a dtype every other provider avoids would leave the store permanently
inconsistent" — the naive-UTC adapter fix is the one that generalises,
not a new dependency that papers over a one-off. Suite confirmed back to
green (358 at the time of the fix; 362 after this session's subsequent
§12.5 re-fetchability-exception work added four more tests) once the two
files were out of the scan path. `data/**` remains the Architect's, not
touched further by this story.

### 14.7 What was actually captured

**Capture #1 completed** (`scripts/snapshot_odds.py -v`, 2026-08-21
14:26 local, after both the naive-UTC fix §14.6 describes and the §12.5
re-fetchability exception §14.4a describes): **all 10 of 10 GW1 fixtures,
both capabilities, written.**

- `odds_match_odds` — 811 rows, all 10 fixtures, `WriteResult.written=True`.
  One `observed_at` (all 10 fixtures fetched in the single `match.odds@
  fixture` call), one `valid_at` (earliest `market_last_update` across the
  batch).
- `odds_player_goal_odds` — **1,009 rows across all 10 fixtures** (933
  `identity_resolved=true`, **76 `identity_resolved=false`, PRESERVED not
  dropped** — the §12.5 exception working exactly as specified: EVERY
  fixture captured, none skipped). Ten distinct `observed_at` values, one
  per per-event fetch, as expected.
- **36 distinct unresolved player names across the whole run**, every one
  logged by name at both the per-event and run-level summary (blueprint
  §12.5's "loud in three places" — row flagged, name kept, capture
  reports every name): `Abdul Fatawu Issahaku`, `Alfie Cresswell`,
  `Alvaro Daniel Rodriguez Munoz`, `Alysson Edward`, `Ben Broggio`,
  `Diego Alexander Gomez Amarilla`, `Eiran Cashin`, `Elijah Campbell`,
  `Emile Smith-Rowe`, `Eric Moreira`, `Estevao Oliveira Goncalves`,
  `Ferdi Kadioglu`, `Gustavo Nunes Fernandes Gomes`, `Harold William`,
  `Jaden Philogene-Bidace`, `Jamaldeen Jimoh-Aloba`, `Jamie Jermaine
  Bynoe-Gittens`, `Jocelin Ta Bi`, `Leo Shahar`, `Luca Williams-Barnett`,
  `Luke Chambers`, `Malick Junior Yalcouye`, `Marcelino Ignacio Nunez
  Espinoza`, `Matheus Luiz Nunes`, `Modou Cisse`, `Murillo Santiago Costa
  dos Santos`, `Nilson David Angulo Ramirez`, `Ogochukwu Onyeka Frank`
  (the case the exception was written for), `Omari Giraud-Hutchinson`,
  `Pascal Gross`, `Remy Rees-Dottin`, `Ruben Dias`, `Ryan McAidoo`,
  `Stephen Mfuni`, `Triston Rowe`, `Yeremi Pino`. Every one of these is
  sitting in the store right now with `identity_resolved=false` and its
  raw string intact — repairable offline, per the exception's whole
  point. Several are plausibly fringe/youth squad members odds-api covers
  more thinly than FPL (unverified — a repair-offline task, not this
  story's).
- **Real credit spend, from response headers**: 2 (match odds) + 1×10
  (one goalscorer call per fixture, spent regardless of whether identity
  later resolved) = 12 credits. Month total: 47 → 59 spent, 441
  remaining, `CreditTracker.remaining_this_month` reconciling exactly
  against `x-requests-remaining` after every live call.
- **Store read verified clean after this capture** (the §14.6 concern,
  directly checked, not assumed): `store.observations('odds_match_odds',
  ...)` and `store.observations('odds_player_goal_odds', ...)` both
  succeed — 811 and 1,009 rows respectively, no `pytz` error, confirming
  the naive-UTC fix holds against the real store now that the two
  quarantined tz-aware files are out of the scan path.

**Capture #2 (near 00:30–01:00 local next-day, for price-movement comparison across
the run-up to the deadline) was NOT run in this session** — 14:26 local at
capture #1, ~10 hours short of that window, and a single agent turn does
not hold a session open across that gap. Recommended command, unchanged
from capture #1 (idempotent, safe to re-run):

```
python scripts/snapshot_odds.py
```

`skip_if_unchanged` cannot be evaluated until a second capture exists to
compare against — not reported here because it genuinely isn't knowable
yet, not because it was skipped.

### 14.8 Scheduled capture — shipped, session s005 (supersedes the "not created" recommendation above)

**Correction, session `s005` (`final-docs-integrity`): this section used to say no scheduled
task existed and recommend a flat every-2-3-hours cadence. Neither is true any more.** A
Windows Scheduled Task **`fpl-ai snapshot_odds`** is registered and fires **hourly at HH:45**,
via wrapper `scripts/run_snapshot_odds.bat`.

The cadence is not flat, though — the task fires hourly but `scripts/snapshot_odds.py` decides
for itself, on every firing, whether this is actually a capture point. Capture is
**deadline-relative and self-gated** at **T-78h / T-26h / T-6h / T-2h** before the *next*
gameweek deadline, read live from the store's `events` dataset — not a fixed weekly cron,
because deadlines are not weekly-at-a-fixed-time (GW3 Fri 17:30 UTC; GW4 Sat 12:30 UTC would
both be missed by a fixed slot). **Why 78/26 and not the more obvious 72/24**: an offset that
is a whole multiple of 24h inherits the deadline's own *local* time-of-day, and against FPL's
~01:30-local deadline both T-72h and T-24h land at 01:00 local — inside this machine's
overnight off-window. Breaking the 24h symmetry by 6h moves all four captures into waking
hours instead. Outside a capture window the script exits **cleanly (code 0)** and still writes
a heartbeat row, so a no-op firing is distinguishable from a scheduler that never ran at all;
live-verified that a no-op spends **zero credits** (ledger unchanged across 12 firings).

Budget: ~18-20 credits per real capture × 4/gameweek, against a **500-credit allowance per
calendar month** (not per season) that is **account-level, not key-level** — rotating the API
key does not reset it, so the every-2-3-hours cadence this section used to recommend would
also have been the wrong shape even before the deadline-relative design replaced it.

Full detail — window arithmetic, `decide_capture()`, heartbeat/scheduling conventions — lives
in `docs/wiki/runbook-ingest.md` and `scripts/snapshot_odds.py`'s own docstring, not duplicated
here.

### 14.9 Test suite

`uv run pytest -q`: **362 total in the app-level suite, all green** — 340
pre-existing (through story 10b) + 13 new `transport.py` durability/
reconciliation tests + 22 `test_provider_odds.py` tests (14 initial + 4
while fixing §14.4/§14.5/§14.6's live bugs + 4 net-new for §14.4a's §12.5
re-fetchability exception, replacing the single raise-based test the old
rule needed). The 7 `tests/test_store_invariants.py` failures §14.6
originally reported are gone — the Architect quarantined the two
tz-aware batches (`data/quarantine/2026-08-21-tzaware-odds/`) and this
session's subsequent capture (§14.7) wrote clean, naive-UTC data in their
place; `store.observations()` on both new datasets was directly re-checked
against the real store after that capture and reads cleanly (811 and
1,009 rows, no `pytz` error).

Every new test in this story was proven to fail against the pre-fix code
before being trusted (handoff lesson #5) — **seven** separate live-bug-
reproduction cycles this session: the id/code confusion, the cache/
error-message key leak, the urllib3 debug-log key leak, the pytz/
TIMESTAMPTZ store corruption, a sentinel-id regression against the new
"unusable by default" join guarantee, and a reintroduced-raise regression
against all four of the new re-fetchability-exception tests at once.

## 15. Derived-capability framework — E2b story 9, 2026-08-21 (XL-Coder)

The last open E2b story, opening Phase 2. Blueprint §12.2: "a derived
capability ... must never be queryable as though it were observed — schema
requirement, not convention." `FactTableSchema.is_modelled` /
`FetchResult.is_modelled` have existed since story 1, always `False`,
deliberately deferred until a real consumer existed. There are now three:
the DC estimator (§11, next slice — needs football-domain input not yet
gathered, so **not built here**), the olbauday `observed_at_imputed`
columns (§13.5 above), and the captaincy backcast (§3.4, Phase 5, not
built here but the design must not preclude it).

### 15.1 The structural guarantee, and where it actually lives

Today's odds work (§14.4a) set the standard this story matches: an
unresolved row carries a null id, so an ordinary equi-join cannot silently
include it — the guarantee is a property of the data's own shape, not a
convention a caller has to remember. The equivalent found here is
**physical separation by dataset name**, not a nullable column:

- `fplai.schemas.register_derived_capability` (new) is the only way to add
  a derived capability's `FactTableSchema`. It requires the capability's
  dataset name to start with `DERIVED_DATASET_PREFIX = "derived_"`, a
  namespace entirely disjoint from every observed dataset's name, checked
  both at registration time and, for the static table built by stories
  1-8, in a self-check loop that runs at import time (so a future
  hand-edit to `_DATASET_TO_CAPABILITY` bypassing the function would fail
  the moment the module imports, not silently).
- `BitemporalStore.as_of()`/`observations()` glob **only the one dataset
  directory they're asked for**
  (`<base_path>/<dataset>/**/*.parquet`). A derived row that was never
  written under an observed capability's dataset name is therefore not
  merely unlikely to be returned by a query against that name — it is
  physically absent from the files that query reads.
- `BitemporalStore.write()` itself (`store.py`, **updated 2026-08-21** —
  see §15.2, this was NOT true when this section was first drafted a few
  hours earlier the same day) now enforces the same `derived_`-prefix
  naming invariant bidirectionally, independent of whether the caller
  goes through `fplai.derived.write_derived` at all.
- `fplai.derived.write_derived` (new) is the only sanctioned way to
  persist a derived batch. It resolves the dataset from the capability
  (not a caller-supplied string, closing a whole class of mismatch), and
  stamps `is_modelled = True` onto every row **itself, as a literal** —
  the caller's DataFrame is rejected outright if it already carries an
  `is_modelled` column (or any of the other provenance columns), mirroring
  `store.write()`'s own `RESERVED_COLUMNS` collision check one layer up.
  This is the load-bearing part of "not by convention, not by a flag a
  caller might forget": there is no code path in this codebase where a
  caller controls the value of `is_modelled` at all.

`tests/test_derived.py::test_derived_rows_are_structurally_absent_from_the_observed_dataset`
proves it directly: a single store instance holds both an observed dataset
(`vaastav_player_gameweek_stats`, seeded with **real rows read read-only
from `data/store/`**, per this story's brief) and a derived dataset side
by side; a query against the observed dataset's name returns exactly the
observed rows, with no `is_modelled` column and none of the derived
value's columns at all. Broken deliberately and reverted three times to
confirm this: (1) commenting out the registration-time prefix check let
`register_derived_capability` accept a non-"derived_"-prefixed dataset
without complaint; (2) commenting out `write_derived`'s reserved-column
collision check let a forged `is_modelled=False` column through
uncaught; (3) hardcoding `write_derived`'s target dataset to
`"vaastav_player_gameweek_stats"` made the derived row actually leak into
the observed `as_of()` result — this is the one that matters most, and it
failed exactly as it should: `observed_out.height` grew from 3 to 4 and
`dc_estimate_mean`/`is_modelled` appeared in the observed dataset's
columns. All three reverted; full suite green afterwards (379 passed, up
from 362 at story 8's close, before §15.2's store-level closure added 3
more to reach 382).

### 15.2 The guarantee's boundary — found by attack, closed the same day

**This section originally read "no store.py change was needed, or made."
It was wrong within hours — reported here as history, not silently
edited away, because the correction is itself the useful record.**

The first version of this design left a real gap: `BitemporalStore.
write()` had no notion of `is_modelled` at all and would accept any
dataset name with any columns, including a forged `is_modelled=True`
written straight into an observed-looking dataset name via plain
`store.write()`, bypassing `fplai.derived.write_derived` entirely. The
Architect verified this independently (not just reviewed the design):
wrote a legitimate row to `vaastav_player_identity`, then a second row to
the *same* observed dataset carrying `is_modelled=True` and
`derived_from`, through `store.write()`. It was accepted, and
`is_modelled` then appeared in that dataset's `as_of()` schema.

The ruling: **the guarantee read "a derived fact cannot leak *if you use
`write_derived`*"** — materially weaker than the standard the odds
adapter's null-id trick set (§14.4a), where the data itself enforces the
rule and no code path can silently join an unresolved row, whether or not
a caller remembers a helper function. Escalating instead of routing
around it (story 9's original scope explicitly excluded `store.py`) was
the correct call, precisely because closing it *required* touching a
module story 9 did not own — the brief's own "stop and report" instinct,
followed as designed. The Architect then authorised and made the ruling
explicit: close it in `store.py`, bidirectionally, minimal scope, prove
the attack fails before the fix.

**What changed, and how it was verified before trusting it:**

- `fplai.store._require_derived_naming_invariant` (new, `store.py`) is
  called from `BitemporalStore.write()` on every write, alongside the
  existing `RESERVED_COLUMNS` collision check. It enforces, both
  directions: (1) a payload carrying ANY `DERIVED_PROVENANCE_FIELDS`
  column, written to a dataset NOT namespaced under
  `DERIVED_DATASET_PREFIX`, raises; (2) a payload written to a
  `derived_`-namespaced dataset MISSING any `DERIVED_PROVENANCE_FIELDS`
  column also raises — "a derived dataset with unlabelled rows is the
  same failure wearing the opposite hat" (the ruling's own words).
- **Migration risk confirmed independently, not assumed**: queried every
  dataset in `DATASET_ENTITY_KEYS` against the real store
  (`data/store/`) for any `DERIVED_PROVENANCE_FIELDS` column —
  `datasets carrying any DERIVED_PROVENANCE_FIELDS today: []`. Zero risk
  to existing on-disk data, confirmed rather than taken on the
  Architect's word.
- **The Architect's exact attack, reproduced as a test, proven to fail
  first**: `tests/test_store.py::
  test_derived_provenance_columns_rejected_outside_derived_namespace`
  writes a legitimate row to `vaastav_player_identity`, attempts the
  identical smuggle (`is_modelled=True`, `derived_from` into the same
  dataset), asserts it raises, and asserts the observed dataset's
  `as_of()` never gains the column and its row count stays at 1. Run
  against the pre-fix code (guard call temporarily replaced with `pass`,
  reverted after): `DID NOT RAISE BitemporalError` — confirmed failing,
  exactly as the Architect had just watched happen live. Reverted;
  re-run: passes.
- **The reverse direction, same treatment**:
  `test_derived_dataset_missing_provenance_columns_is_rejected` (a
  `derived_`-namespaced write missing the provenance columns) and
  `test_derived_dataset_with_full_provenance_columns_is_accepted` (the
  positive path). The missing-columns test also failed
  (`DID NOT RAISE`) against the pre-fix code and passes after.
- **What the store-level guard does NOT check, by design**: column
  VALUES. A row carrying `is_modelled=False` under a `derived_`-namespaced
  dataset with every required column present still writes successfully —
  the guard confirms the *shape* is honest, not that a particular boolean
  is `True`. `fplai.derived.read_derived` is what catches a wrong VALUE,
  on the way out (`tests/test_derived.py::
  test_read_derived_raises_if_the_write_derived_contract_was_bypassed`,
  docstring corrected to say "checks presence, not value" — its original
  wording, "store.write() has no is_modelled awareness," stopped being
  true the same session).
- Full suite after the fix, doc corrections, and both directions'
  attack-reproduction tests: **382 passed**, 0 regressions.

**Scope discipline held**: the change is one new module-level function in
`store.py` (`_require_derived_naming_invariant`) plus a two-line
docstring addition to `write()` and one call site — no reshaping of
`write()`'s signature or contract, exactly the "keep the scope minimal or
stop and report" instruction. `docs/BLUEPRINT.md` was not touched by this
story (Architect-only per CLAUDE.md); the Architect stated intent to fold
the §12.2 underspecification findings (§15.5) into a blueprint amendment
separately.

### 15.3 Should olbauday's `observed_at_source`/`observed_at_imputed` fold into this framework? No — and here is why

**Confirmed by the Architect, 2026-08-21: this was a deliberate rejection with reasoning, not an oversight.** Recorded explicitly so a future pass does not "fix" it by merging the two columns into `is_modelled` — that would be the regression, not a cleanup.

They are a **different axis**. `is_modelled` says the row's *value* is
computed, not measured. `observed_at_imputed` says the row's *value* is a
genuine observation, but the *timestamp it is dated under* is inferred
rather than captured at the moment of observation — olbauday's
`player.attributes@gameweek` rows genuinely report what the game's UI
showed (a real historical fact); what's uncertain is only *when* we would
have known it, imputed from `gameweek_summaries.csv`'s `deadline_time`
(§13.5). Folding the two together would conflate "we estimated what
happened" with "we estimated when we learned it" — different failure
modes, different consumers (a backtest reads `observed_at` to prevent
leakage regardless of `is_modelled`; a model consuming the row's *value*
needs to know whether that value itself is trustworthy as a measurement).

What *did* generalise: the **column-pair pattern** itself. It was
invented ad hoc for olbauday (E2b story 10b) with no shared vocabulary
documented anywhere reusable. `schemas.py`'s derived-capability section
does not touch these two columns (they stay exactly where story 10b put
them, on `PLAYER_ATTRIBUTES_GAMEWEEK`/`GAMEWEEK_FIELD_SUMMARY_GAMEWEEK`,
both `is_modelled=False`), but this story's module comment on
`DERIVED_PROVENANCE_FIELDS` documents the general principle the DC
estimator and any future derived capability should follow — carry
provenance as required schema columns, never `FetchResult.meta` — so the
next adapter that needs an imputed-timestamp pair (or a derived-value
label) reaches for a documented convention rather than reinventing the
column names a third time.

### 15.4 What the DC estimator will need that could not be built here

Per the brief — "build the framework, not the DC estimator" — no DC
capability key, dataset, or computation was added. What's ready for it:

- `fplai.schemas.register_derived_capability` — the estimator module
  registers its own `CapabilityKey` (likely
  `player.defensive_contribution_estimate@match`, keyed
  `(season, round, element, fixture)` to match
  `PLAYER_GAMEWEEK_STATS_GAMEWEEK`'s grain) at its own import time, gets
  `DERIVED_PROVENANCE_FIELDS` injected automatically, and is refused if it
  tries to reuse an existing key or a non-`derived_` dataset name.
- `fplai.derived.write_derived`/`read_derived` — the write/read gate,
  needing no changes for the estimator's real shape.
- `fplai.derived.DerivationInput`/`CalibrationReference` — genuinely
  exercised in `tests/test_derived.py` against **real identifiers pulled
  from `data/store/vaastav_player_gameweek_stats`** (season/round/
  element/fixture), not fabricated ones, per this story's "green is not
  done, live is done" instruction (adapted here — there is no live
  provider call for a framework story, so this is the closest available
  form: exercised against real, on-disk data rather than only fixtures).

What is genuinely unknown and would have been guessed if built now: the
exact recoveries fallback shape (§11 — "unobtainable per-player-per-match
from every affordable source surveyed"; the per-90 rate prior needs a
concrete source and a concrete aggregation the Architect/data-scout have
not yet supplied), and the actual residual distribution shape once real
2025-26 calibration is run (this story's `CalibrationReference` carries
`residual_mean`/`residual_std` as the minimum required representation —
sufficient for a Gaussian residual, but if the real DC residual turns out
skewed or multimodal, the estimator will want to add its own
capability-specific quantile columns on top, which the schema already
allows as present-but-optional, same pattern as `value_raw`).

### 15.5 §12.2 in practice — what was underspecified until implemented

§12.2 says a derived row must carry `is_modelled`, its inputs, and a
calibration reference with the residual "carried as uncertainty." Three
things §12.2 itself left open, resolved here and worth folding back:

1. **Where does the guarantee actually attach — the row, the dataset, or
   the query path?** §12.2 reads as though a stored boolean column is
   sufficient. It is necessary but not the thing that makes the guarantee
   *structural* — a boolean a caller could still read past (e.g. an
   incautious `as_of("vaastav_player_gameweek_stats")` caller who never
   filters on it). The physical-separation-by-dataset-name design is the
   actual structural half; `is_modelled` is provenance for a caller who
   already has the derived dataset in hand, not the leakage guard itself.
   **Revised by §15.2's closure**: physical separation alone was still
   only as strong as whichever function actually wrote the bytes — it
   held for `as_of()`/`observations()` (query-path enforcement) but not
   originally for `write()` itself (write-path enforcement), which is
   exactly the gap the Architect's attack found. The guarantee's real
   home turned out to be *both* ends of the store boundary, not one:
   `write()` refusing to put a row in the wrong place, and `as_of()`
   physically unable to read it back from there if it somehow arrived.
2. **"Its inputs" — at what grain?** §12.2 doesn't say whether a derived
   row must name every individual source row or may reference an
   aggregate. `DerivationInput.entity_key` deliberately allows a *partial*
   key (e.g. `{"season": ..., "element": ...}` with no `round`/`fixture`)
   for a derivation that aggregates a whole season's rows — a DC
   per-90-rate estimator will do exactly this; requiring every individual
   row named would make `derived_from` enormous for no real re-derivation
   benefit.
3. **"Calibration residual carried as uncertainty" — carried as what,
   concretely?** §12.2/§11 say "not discarded" but not what shape. A
   single scalar correction factor would satisfy the *letter* while
   violating CLAUDE.md rule 5 in spirit (a scalar at a derived-fact
   boundary is the same design error as a scalar `xPts`). This story
   settled on `residual_mean`/`residual_std` as the required minimum —
   defensible for now, but the Architect may want to require a richer
   representation (quantiles, empirical CDF) once the real DC residual
   shape is known; the schema's present-but-optional escape hatch means
   that's an additive change, not a breaking one, when it happens.
   **Flagged explicitly, per the Architect's instruction: this is a
   PLACEHOLDER, not a settled design.** `residual_mean`/`residual_std`
   is a reasonable minimum to unblock the framework, nothing more — it
   assumes (implicitly, by choosing two moments) something close to a
   Gaussian residual, which is an assumption, not a finding. §11's
   "residual carried as uncertainty" gets its REAL shape only from actual
   2025-26 DC calibration data, not from this story's placeholder. Do not
   let two required columns harden into "the" representation by default
   — revisit with numbers once the DC estimator runs for real, and change
   `DERIVED_PROVENANCE_FIELDS`/`CalibrationReference` then if the real
   residual turns out skewed, multimodal, or otherwise non-Gaussian.

## 16. Closing the presence-not-dtype gap — 2026-08-21 (XL-Coder)

§14.6 left one thing explicitly open: `FactTableSchema.validate()` checked
column PRESENCE only, and nothing between an adapter and the Parquet file
objected to a column's DTYPE — which is exactly how a genuinely tz-aware
`commence_time`/`market_last_update` reached the real store and only failed
three layers downstream, at a DuckDB read, for a `pytz` dependency reason
with no connection to `schemas.py` at all. Phase 2 is about to add a large
volume of new numeric columns (model outputs) to the store, which is what
made this the thing to close before that work starts, not after.

### 16.1 Reproduced first, then closed

Per CLAUDE.md's "prove it fails first": the exact incident was reconstructed
directly against the unmodified `FactTableSchema.validate()` — a tz-aware
`commence_time`/`market_last_update` `pl.Datetime` column, validated against
`MATCH_ODDS_FIXTURE`'s real schema. It passed silently, confirmed live before
any code changed. The two real, still-quarantined incident files
(`data/quarantine/2026-08-21-tzaware-odds/`) were also re-validated directly:
`odds_match_odds`'s quarantined batch now raises on exactly the tz-aware
columns; `odds_player_goal_odds`'s quarantined batch separately fails
presence (it predates the §14.4a `identity_resolved` column) — both are the
real incident data, not a fixture, confirming this against genuine evidence
rather than a constructed reproduction alone.

### 16.2 The dtype contract: a CLASS check, not exact-dtype equality

`FactTableSchema.validate()` now does three things, in order: presence
(unchanged), a dtype-FAMILY check on `required_fields` only, and a
timezone-naive check across **every** column of the payload (not only
required ones — the incident's two offending columns were required only by
coincidence; the failure mode doesn't care).

**Why a class check and not exact-dtype equality**, decided against the real
store, not first principles — every dataset on disk was inspected two ways:
the merged DuckDB view (`union_by_name=true`, what a consumer actually
queries) and, separately, every individual Parquet batch file's own schema
directly via Polars (330 files, 10 datasets with data on disk) to rule out
DuckDB silently promoting a per-batch dtype mismatch into agreement. Result:
**zero per-column dtype drift across batches within any one dataset today**
— every batch of `elements`, `vaastav_player_gameweek_stats`, etc. already
uses one consistent dtype per column. But real, LEGITIMATE cross-*capability*
drift for the same logical concept is already on disk and must not be
rejected: `chance_of_playing_next_round` is `BIGINT` in `elements` but
`VARCHAR` in `vaastav_player_identity`; `selected_by_percent`/`ep_next`/
`form` are similarly `VARCHAR` in one capability and `DOUBLE` in another —
each capability's own adapter is internally consistent, but pinning one
exact dtype across the whole store (or even hand-declaring an expected dtype
per required field) would fight documented, deliberate drift this project
already accepted (`opta_code` absent pre-2016-17, `player.attributes@
gameweek`'s 2024-2025 column set, `Int32` vs `Int64` across CSV eras — see
this file's own module comment in `schemas.py` above `PLAYER_GAMEWEEK_
STATS_GAMEWEEK`). The check therefore classifies a required field's dtype
into one of **six** families — integer, float, string, boolean, temporal,
and **null** — and only refuses a dtype that fits none of them: `List`/
`Struct`/`Array` (a nested shape landing where a scalar is expected — the
"genuinely broken/reshaped response" class the module's own docstring
already names as the reason `required_fields` validation exists at all) or
`Categorical`/`Enum`/`Object` (never produced by any current adapter; new
territory that should fail loudly on first appearance).

**`Null` as its own accepted family was not a first-principles guess — it
was found live, mid-implementation, by the first hand-built reproduction
fixture.** An initial test constructed `MATCH_ODDS_FIXTURE.outcome_point:
[None]` (a single row) to probe the "all fields present, one dtype odd"
shape, and Polars inferred that column as dtype `Null` (no observed value to
type it from) — which the first draft of the family check rejected as
unclassifiable. That draft would have been WRONG in production: this
schema's own description already documents `outcome_point` as legitimately
`NULL` for every row of an h2h/h2h_lay outcome, and `providers/odds.py`
builds it from `outcome.get("point")`, which is `None` whenever a
bookmaker's response for a fixture carries no "totals" market at all —
plausible for a lower-profile fixture on a given capture, not a contrived
edge case. Rejecting an all-null required column would have failed a
schema-conformant live capture for a reason unrelated to the presence-not-
dtype gap this exists to close — the exact "too strict, re-creates a
different failure" trap the brief warned against. `Null` was added as its
own accepted family before this closed, not after a live failure forced it.

### 16.3 The timezone-naive check — every column, not just required ones

Any column whose dtype is a timezone-AWARE `pl.Datetime` (`dtype.time_zone
is not None`) is refused, unconditionally, for every column in the payload —
this is the specific, narrow fix for the actual incident, kept deliberately
separate from the broader family check per the brief's constraint #2. Every
DATA column this store has ever held is naive UTC by convention (only
bitemporal METADATA — `valid_at`/`observed_at` — is genuinely tz-aware, and
`BitemporalStore._require_utc` normalises those itself); this makes that
previously-implicit, undeclared convention enforced rather than merely
documented.

### 16.4 Two-layer defense, mirroring §15.2's precedent — not scope creep

`FactTableSchema.validate()` (the schema-level fix) is called by every
current provider's `fetch()` before returning a `FetchResult`, and by
`fplai.derived.write_derived` on the enriched derived row — so no adapter
needed to change to comply; all already do (verified, 2026-08-21: zero
dtype-family or tz violations across all 330 real batch files on disk).
That alone would have closed the gap for every call site that exists today.

A second, narrower guard — `store._require_naive_temporal_columns`, called
unconditionally inside `BitemporalStore.write()` — was added anyway,
deliberately mirroring the shape (not the content) of `_require_derived_
naming_invariant` (§15.2): a structural backstop at the point data actually
reaches disk, independent of whether the caller happened to validate first.
This is NOT a re-run of the full schema — it does not know about
`required_fields` or dtype families, and does not require `dataset` to be a
registered capability at all (many of `store.py`'s own tests write partial-
shape payloads to unregistered dataset names by design, e.g. `"mystery_
dataset"` or an `"elements"` payload with only `id`/`price`; re-running full
schema validation inside `write()` would reject those and reshape `write()`'s
contract — explicitly out of this story's authorised scope, same boundary
§15.2's derived-naming guard respected). It checks exactly the one thing
that IS universal and schema-independent: no column may be a timezone-AWARE
`Datetime`, anywhere, regardless of whether `dataset` is registered.

**Justification for the second layer, not just the first**: §15.2 already
settled this project's answer to "is a single sanctioned/validated path
enough?" — no. The Architect demonstrated live that a plain `store.write()`
call would accept a forged `is_modelled=True` row bypassing `write_derived`
entirely; the guarantee only held "if you use write_derived", which was
ruled insufficient and closed with a redundant, independent check in
`write()` itself. Applying that same standard here: "every current caller
happens to validate first" is the identical class of guarantee-by-caller-
discipline that §15.2 already rejected once this project, for a materially
similar reason. **Both checks were proven to actually matter, not merely
plausible**: the store-level guard was verified to be load-bearing by
temporarily disabling only its one call site inside `write()` and confirming
its two dedicated tests fail without it (`DID NOT RAISE BitemporalError`),
then restoring it and re-confirming the full suite green — the same
discipline `_require_derived_naming_invariant`'s own tests were held to.

### 16.5 Verified against the real store, not just fixtures

Every one of the 14 datasets this session's brief asked about was checked
directly, twice: once via the merged DuckDB `union_by_name=true` view (what
`as_of()`/`observations()` actually return), and once by reading every
individual Parquet batch file's own schema straight from Polars (bypassing
any DuckDB type promotion). **330 real batch files across the 10 datasets
that currently have data on disk (`chips`, `elements`, `events`,
`game_config`, `game_settings`, `odds_match_odds`, `odds_player_goal_odds`,
`teams`, `vaastav_player_gameweek_stats`, `vaastav_player_identity`) — zero
dtype-family violations, zero tz-aware columns, zero rejections.** The four
remaining registered capabilities with no data on disk yet (`picks`, all
five `pl_*` PL-API datasets, `vaastav_team_identity`, both `olbauday_*`
datasets) could not be checked against real data because none exists yet —
not silently assumed safe; simply unverifiable until a real capture exists,
same caveat every other section of this document applies to unfetched
capabilities. A ninth invariant, `test_schema_dtype_contract_holds_for_
every_real_batch_on_disk`, was added to `tests/test_store_invariants.py`
(the module that already runs read-only against the real store) so this
stays true going forward rather than being a one-time check.

### 16.6 What this does not do, deliberately

- **No adapter changed.** Every current provider already produces
  naive-UTC, classifiable-dtype data; this is a gate, not a fixer — it
  raises loudly rather than coercing a violating payload, the same
  "validate, don't repair" posture every other check in this module already
  takes.
- **`store.write()`'s contract was not reshaped.** The guard added there is
  narrowly scoped to tz-awareness only; it does not gain knowledge of
  `required_fields` or dtype families, and does not require `dataset` to be
  registered. A broader re-validation inside `write()` was considered and
  explicitly rejected — it would break several of `store.py`'s own existing
  tests that deliberately write partial/unregistered-dataset payloads to
  exercise store mechanics in isolation from schema concerns, and reshaping
  `write()`'s contract was flagged in the brief as the Architect's decision,
  not this story's.
- **This does not validate VALUES, only shapes.** A `Datetime` column that
  is naive but wrong (e.g. accidentally local time mislabelled as UTC) is
  not, and cannot be, caught here — that class of bug has no dtype signature
  to check against; it needs a value-level test against a known-correct
  source, out of scope for a presence/dtype gate.

## 17. `BitemporalStore.effective_at()` — the sanctioned pattern for a
per-row valid time, 2026-08-22 (XL-Coder, session s003)

**Read this before reaching for `store.observations()` plus a hand-written
filter on any archive's own timestamp column. That workaround is retired.**

### 17.1 The problem this closes

`BitemporalStore.write()` stamps ONE scalar `valid_at` on every row of a
batch (`pl.lit(valid_at).alias("valid_at")`). Right for a snapshot (an
ownership capture, a price table — every row genuinely *is* true at one
instant). Wrong for a bulk-ingested archive, where one batch carries many
seasons and each row's real valid time is its own domain fact (a fixture's
kickoff). For those datasets `valid_at` is a meaningless per-batch constant
and `observed_at` is ~ingest time, so `as_of(dataset, historical_deadline)`
correctly returns **empty** for any real historical deadline — correct by
contract (`as_of()` only ever resolves *observed* time, and always will —
see §15.4/blueprint §3.2, "`as_of` returns STATE, not the observation
stream"), and useless for training/backtesting, which need *this row's own*
domain valid time.

Session s002/s003 converged on the same workaround **three independent
times**, nearly word-for-word: `fplai.models.team_strength`,
`fplai.models.minutes`, and (on inspection, corrected — see §17.5)
NOT `fplai.backtest.data`, which uses an entirely different, round-number-
based leakage boundary and never touches `kickoff_time` at all. Two real
convergences on `store.observations()` + a hand-rolled
`pl.col("kickoff_time") < as_of_naive` filter is what the Architect ruled a
missing primitive, not a coincidence — blueprint §3.2's amendment,
"Valid time is per ROW, not per batch."

### 17.2 The primitive

```python
store.effective_at(dataset: str, effective_ts: datetime) -> pl.DataFrame
```

Returns STATE effective at `effective_ts` — one row per entity key, the
latest row whose **declared valid-time column** is strictly before
`effective_ts`. A dataset declares that column on its `FactTableSchema`:

```python
CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK] = FactTableSchema(
    ...,
    valid_time_column="kickoff_time",
    valid_time_format="%Y-%m-%dT%H:%M:%SZ",  # the column is VARCHAR on disk, not a native Datetime
)
```

`valid_time_format` is `None` when the declared column is already a native
(naive UTC) `Datetime`; otherwise it is the `strptime` format used to parse
it before comparing — declared, not sniffed from the on-disk dtype at query
time (a caller must never have parsing behaviour depend on which era's file
happened to be read).

Only `vaastav_player_gameweek_stats` (`player.gameweek_stats@gameweek`)
declares one today. A dataset that declares none behaves exactly as before
(`effective_at()` raises `BitemporalError` for it — use `as_of()` or
`observations()` instead); nothing about this story required touching any
other dataset's schema, and no data was migrated (blueprint §3.2 ruling
point 5 — `valid_at` on a bulk-ingested archive remains a known-meaningless
constant until a follow-on story populates it per row).

### 17.3 `effective_at()` is NOT `as_of()` with a different column — two
names, two boundary operators, on purpose

| | resolves | boundary | why |
|---|---|---|---|
| `as_of(dataset, t)` | OBSERVED time (`observed_at`) | `<=` (inclusive) | knowing something exactly at `t` counts as knowing it by `t` |
| `effective_at(dataset, t)` | the DECLARED domain valid-time column | `<` (exclusive) | a fixture with `kickoff_time == t` has not been played AT the deadline instant itself |

This follows the precedent `latest()`/`as_of(now)` and `as_of()`/
`observations()` already set: two operations that differ subtly get two
names that read as different at the call site, rather than one name with a
flag or an inferred behaviour a caller could get wrong silently. Do not
rename one to look like the other; do not add a `valid_time: bool` flag to
`as_of()`.

**`effective_at()` never reads this store's own `valid_at` metadata
column.** It reads the schema-declared domain column (e.g. `kickoff_time`).
This is enforced structurally, not just by convention: the store refuses to
resolve a `valid_time_column` that collides with `RESERVED_COLUMNS`
(`_resolve_valid_time_column` in `store.py`) — attacked directly in
`tests/test_store.py::test_effective_at_never_reads_the_stores_own_valid_at_metadata_column`,
which tampers with the registry from outside `FactTableSchema`'s own
validation to try exactly this, and confirms it still raises.

### 17.4 Collapse ordering — three levels, not one

For an entity key with more than one row passing the `< effective_ts`
filter (a genuinely rescheduled fixture, or — verified live, 2026-08-22 — an
**exact duplicate row within one batch**: `vaastav_player_gameweek_stats`,
2025-26, elements 100/391, same `batch_id`, byte-identical content, an
upstream archive artefact, not a bug in this store):

1. **PRIMARY**: the parsed valid-time column, DESCENDING — pick the most
   recent version whose valid time does not exceed the cutoff.
2. **SECONDARY**: `observed_at`, DESCENDING — among rows sharing the same
   valid time (e.g. a later correction to some other column of the same
   fact), prefer the one learned about more recently. This is deliberate:
   an earlier draft used only `valid_time DESC, batch_id DESC`, which
   degrades to an arbitrary tiebreak whenever two rows share a valid time
   (the common case — a correction never changes a fixture's own kickoff).
   Attacked directly:
   `tests/test_store.py::test_effective_at_tie_break_prefers_the_later_observed_at_correction`.
3. **TERTIARY**: `batch_id`, DESCENDING — final deterministic tiebreak,
   same as `as_of()`'s own.

**Row order is also canonicalised**, sorted on the entity key ascending, as
a final outer `ORDER BY`. This was not optional: a `QUALIFY`/window-function
query has no output-order guarantee from DuckDB without an explicit `ORDER
BY`, and this was caught live, not assumed —
`tests/test_minutes.py::test_walk_forward_validate_cannot_see_a_folds_own_or_future_outcomes`
failed with two players' predictions swapping list positions between two
builds that differed only in a later round, before this `ORDER BY` was
added. **`as_of()`/`observations()` have the same latent gap and do not yet
have this fix** — see the session's punch-out finding: it independently
explains a real, reproduced non-determinism in `scripts/run_baselines.py`'s
`greedy_form` baseline (unrelated to this story, not fixed here, flagged
for a follow-on).

### 17.5 The boundary-agreement check, done before migrating anything

Before migrating `team_strength.py`/`minutes.py`, both were checked against
each other for `<` vs `<=` disagreement (the Architect asked explicitly:
"I want to know if we have been leaking, not just that it is fixed"). Both
already used strict `<` — no disagreement, no historical leak from this
specific asymmetry. `fplai.backtest.data` was checked too and found **not**
to be a third instance of the workaround at all, contrary to how a prior
session's punch-card finding characterised it: it has no `kickoff_time`
filter anywhere; its leakage boundary is round-number-based (`round <
gameweek`), enforced structurally in `fplai.backtest.replay.SeasonReplay`,
at least as conservative as a kickoff-time boundary (it excludes an entire
round's outcome data regardless of which of that round's fixtures have
actually kicked off). It was left unmigrated, on purpose.

### 17.6 A real, live-verified consequence of collapsing to state — not a
regression, but not hidden either

Because `effective_at()` collapses to state (§3.2 ruling point 3, "exactly
as `as_of()` does"), the 10 exact-duplicate rows named in §17.4 above — which
`store.observations()` never deduplicated — are now correctly deduplicated
before `team_strength.py`/`minutes.py` see them. Concretely: the minutes
model's training table dropped from 113,270 to 113,260 rows (10 fewer,
2 fewer START / 4 fewer SUB / 4 fewer UNUSED); the walk-forward gate's eval
row count dropped from 57,040 to 57,030; model log-loss moved from 0.3465 to
0.3464 (Brier unchanged at 4 d.p.); the team-strength fit's Man City attack
rating moved from `+0.4737` to `+0.4740` (none of the 10 duplicate rows'
fixtures involve Man City directly — the tiny movement comes from the
shared `home_advantage`/`rho` parameters and the joint MLE's coupled
gradient, not a direct effect). `docs/wiki/model-minutes.md` and
`docs/wiki/model-team-strength.md` carry the pre-migration numbers as of
this write and should be updated to the numbers above — flagged to the
Architect in this session's punch-out rather than edited here (both files
are outside this story's owned paths).

## 18. Match officials + the first real PL ingest — session s004, 2026-08-28 (XL-Coder)

> Two things landed together, deliberately (Architect's ruling — PL ingest before cards):
> `match.officials@match`, a sixth `providers/pl.py` capability, and the FIRST rows this
> adapter has ever written to the real store (`data/store/`). §18.4 below is longer than
> §18.1-18.3 combined, because the ingest surfaced three real, live-discovered gaps and none
> of them were paperable-over inside this story's owned paths.

### 18.1 `match.officials@match` — one row per official, a SEPARATE request

`GET /v1/matches/{id}/officials` (verified live back to 2020/21). One row per official —
Referee, Assistant Referee#1/#2, Fourth official, Video Assistant Referee, Assistant VAR
Official — six, verified distinct per match on two real fixtures spanning 2020/21 and
2025/26. Entity key `(match_id, role)`, `is_referee` a derived boolean so a consumer never
string-matches the raw `role` literal.

**The brief's premise ("it is ONE endpoint") does not hold for this adapter, and that is
worth recording precisely.** `docs/wiki/provider-evaluation.md` §2.1 is correct that
`GET /football/fixtures/{id}` on the LEGACY `footballapi.pulselive.com` host bundles
lineups + substitutions + officials into one response. `providers/pl.py` was built (session
s001, §9 above) against a DIFFERENT, newer host —
`sdp-prem-prod.premier-league-prod.pulselive.com` — which splits the same data across three
routes. Checked live before writing any code: neither `v3/matches/{id}/lineups` (already
fetched for `match.lineups@match`) nor `v1/matches/{id}/events` (already fetched for
`match.substitutions@match`) contains a referee/official field anywhere in the response body.
`match.officials@match` is therefore a genuine fourth request per match, not a free capture
riding an existing fetch.

**No identity resolution at all.** `matchOfficials[]` carries no PL numeric id anywhere —
only `firstName`/`lastName`/`name` strings. `_fetch_officials` makes no `self.identity()`
call and cannot raise `IdentityError` — the only identity question at this grain is the MATCH
itself, which is already solved: `match_id` is the same id every other match-grain capability
here uses, and `fplai.identity.resolve_match` (live-verified since §9, unmodified) is the
existing mechanism a consumer joins this table onto vaastav's FPL `fixture` column with. The
brief's hardest named risk ("the identity join is the hard part") turned out to already be
closed for this specific capability — nothing new needed building. The real, undocumented-
upstream weakness that remains: there is no stable id for an official, only a name, so a
referee-level historical model (a card-rate prior per referee) must key on NAME and accept
the accent/initial/homonym risk that comes with it.

Live-verified end to end via `scripts/verify_pl_provider.py` (now 7 live requests, was 6):
match 2562265 (2025/26) returns 6 rows, referee Samuel Barrott correctly flagged
`is_referee=True`, the other 5 correctly `False`.

### 18.2 A real crash, found only by running the adapter against real 2025/26 data

The live `--execute` backfill (§18.4) crashed on match 2561915 (Crystal Palace v Aston Villa,
31 Aug 2025) with an unhandled `TypeError`, not an `IdentityError` — `_fetch_substitutions`
called `identity.players.resolve(None)` because a real substitution has
`"playerOnId": null` (a player subbed OFF at minute 90 with no one coming ON — subs already
exhausted, or a deliberate 10-men finish). This is a real football fact, not a malformed
payload, and rule 5/§3.2 forbids silently dropping the row. Fixed inside owned paths only
(`providers/pl.py`, not `identity.py`): `playerOffId` — the entity-key disambiguator — still
raises loudly if ever null (never observed); `playerOnId` may now be genuinely null in the
row, kept, not misclassified as an identity-resolution miss. Break-first proven: reverted the
guard, confirmed the exact `TypeError` reproduces in `tests/test_provider_pl.py`, restored,
confirmed green.

Worth flagging structurally: `BackfillOrchestrator.run()`'s `handle()` only catches
`IdentityError`/`TransportError`/`ProviderError` (module docstring §2's "three separate
`except` clauses, deliberately not collapsed"). An uncaught exception from anywhere inside a
provider's `fetch()` — this one included, before the fix — kills the whole process rather
than becoming a checkpointed `UnitOutcome` or a clean `BackfillHalted`. The checkpoint is
still safe (nothing is recorded for the unit that crashed, so a corrected re-run resumes
cleanly, which is exactly what happened here), but the three-exception list is not
exhaustive against a provider bug, only against the three *expected* failure classes. Not
fixed here — `backfill.py` is outside this story's owned paths — named for whoever picks up
§18.4's follow-up.

### 18.3 An identity-resolution gap, found only by attempting six real seasons

The brief's target was 2020-21 through 2025-26. A dry-run for all six (`match.fixtures@
matchweek` + `match.lineups@match` + `match.substitutions@match` + `team.match_stats@match`)
priced at 7,068 requests, ~59 minutes floor at policy rate — reasonable, so `--execute` was
run for real against `scripts/backfill.py` (unedited; this story does not own it). It failed
twice, both times on the FIRST unit of the run, both times inside `PlayerIdentityMap.build()`
(`identity.py`, READ-ONLY to this story):

1. **Seasons 2020-21 through 2023-24: `opta_code` does not exist in vaastav's
   `players_raw.csv` at all.** Verified directly against the real archive:
   `False/False/False/False` for 2020-21/21-22/22-23/23-24, `True/True` for 2024-25/25-26.
   `PlayerIdentityMap.build()` hard-requires the column with no fallback.
2. **2024-25, which HAS the column, halted too — a genuine archive data-quality anomaly.**
   20 of 804 elements carry `opta_code` values like `'man51018'` instead of `'p<code>'`.
   `PlayerIdentityMap.build()`'s strict `opta == f'p{code}'` check correctly refuses to guess
   (§12.5) — but raises for the WHOLE season, not just the 20 offending rows.
3. **Both halts block ALL FOUR capabilities for the affected season, not only the ones that
   need player identity.** `scripts/backfill.py`'s `_build_pl_factory().factory()` builds a
   full `IdentityResolver` (player map AND team map) EAGERLY per season, regardless of which
   capabilities are actually requested. `match.fixtures@matchweek` needs only team identity;
   `team.match_stats@match` needs neither (team codes are passed in as parameters, never
   resolved inside `_fetch_team_match_stats`) — both are blocked anyway.

None of the three is fixable inside this story's owned paths (`identity.py` is READ-ONLY;
`scripts/backfill.py` is not listed at all, i.e. FORBIDDEN to edit under the brief's own
convention). Logged as a `blocked` punch-card event with a proposed three-part split rather
than routed around: (A) ship what works now — season 2025-26 has zero identity anomalies and
was backfilled for real, see §18.4; (B) a follow-up story, scoped to `identity.py` and/or
`scripts/backfill.py`, either extends `PlayerIdentityMap` with a documented fallback join for
the pre-`opta_code` era, or makes identity construction capability-aware/lazy so a
fixtures-only or team-stats-only request for an anomalous season doesn't need player identity
at all, or both; (C) a real, Architect-level decision on the 20 anomalous 2024-25 elements
(quarantine them, or accept 2024-25 as unbackfillable for lineups/substitutions until (B)
lands).

### 18.4 What actually landed in `data/store/`

Six-season target reached for **one** season this session — 2025-26, the only one with zero
identity anomalies — via the unmodified `scripts/backfill.py --execute` CLI, no workarounds:

| Dataset | Capability | Rows written (real store, `data/store/`) |
|---|---|---|
| `pl_match_fixtures` | `match.fixtures@matchweek` | **375** (38 matchweeks) |
| `pl_match_lineups` | `match.lineups@match` | **15,004** |
| `pl_match_substitutions` | `match.substitutions@match` | **3,098** |
| `pl_team_match_stats` | `team.match_stats@match` | **131,080** (long format) |

Orchestrator summary: `total=1163 done=1163 absent=0 already_resolved_on_entry=433
elapsed=365.8s` (the run crashed and resumed once — §18.2 — `already_resolved_on_entry`
reflects the units the crashed attempt had already checkpointed before it hit match 2561915).
Zero `absent` outcomes for the whole run — every unit the source actually had, resolved.

`match.officials@match` was NOT bulk-backfilled — it has no `GrainPlan` in
`fplai.backfill.GRAIN_PLANS`. The brief's owned-paths list names
`src/fplai/registry.py (GrainPlan / capability declaration, if needed)` — `GrainPlan`/
`GRAIN_PLANS` actually live in `src/fplai/backfill.py`, a materially different, unowned file
(confirmed by reading it). Wiring officials into the orchestrator needs a small addition
there (a `GrainPlan` + expand function mirroring `_make_match_expand` almost exactly, ~15
lines) that is out of scope for this story to make unilaterally. The capability is fully
built, tested and live-verified (§18.1) — nothing was written to `data/store/pl_match_
officials/` this session, deliberately, rather than landing a handful of ad hoc rows outside
the sanctioned checkpoint/orchestrator path that would look like a completed ingest without
being one.

2024-25 through 2020-21 remain unbackfilled — blocked on §18.3, not attempted with a
workaround.
