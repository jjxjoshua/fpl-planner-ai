# Data Sources — Availability Report

> **Reading order.** `docs/BLUEPRINT.md` is authoritative wherever this file disagrees with
> it. Several findings here were later disproved by direct measurement — each is marked
> SUPERSEDED in place rather than deleted, because knowing what we believed and why we were
> wrong is part of the record.

> **Status note, added `xl-coder` session s004 (2026-08-28).** This report predates the PL
> API discovery (session s001, same date, but written before it landed) and does not mention
> `footballapi.pulselive.com`/`sdp-prem-prod...` at all. **Match officials (referee) — the
> question the cards model was blocked on — are now a live-verified, shipped capability**,
> `match.officials@match` in `src/fplai/providers/pl.py`. See
> `docs/wiki/provider-framework.md` §18 for what shipped, what was verified live, and what is
> still blocked (a real, live-discovered identity gap for the 2020-21…2023-24 archive
> seasons — not a source-availability question, so it does not belong in this file).


> Author: **Data Scout** · Session `s001` · Recon date **2026-08-19**
> Status: reconnaissance only. No production code written. Every claim below marked
> **[VERIFIED]** was established by calling the endpoint on this date; claims marked
> **[UNVERIFIED]** were not, and the reason is given in §9.
>
> Context: 2026/27 has **not started**. GW1 deadline is 01:30 local Sat 22 Aug
> (`2026-08-21T17:30:00Z`) **[VERIFIED]**. There is zero current-season match data, and all
> league standings are empty. Several checks below are consequently blocked until after GW1
> is scored.

---

## 0. Headline

| Question | Answer |
|---|---|
| Can we backfill historical **effective ownership**? | **Partial.** Ownership yes (community archives). **Captaincy — no, from any source.** |
| Can we get EO going forward? | **Yes**, by sampling `picks/` weekly. It must start at GW1 or the data is gone forever. |
| Is API-Football usable this season? | **No.** Free tier is hard-locked to seasons 2022–2024. |
| Is Understat usable? | **Yes**, via a newly-discovered JSON API — but `robots.txt` disallows all crawling. |
| Is FBref usable? | **No** without a headless browser. Cloudflare 403s every plain HTTP client. |
| Are free anytime-goalscorer odds available? | **Not from The Odds API at any tier.** One unverified candidate remains. |

---

## 1. FPL Official API

**Base:** `https://fantasy.premierleague.com/api/`
**Auth:** none for everything listed below. **[VERIFIED]**
**Edge:** Fastly (`via: varnish`, `X-Cache`, `edge-control: max-age=1209600`). Responses can
be served with a large `Age` — one probe returned `Age: 48849` (13.6 h). Treat any single
read as potentially stale by hours. **[VERIFIED]**
**Rate-limit headers:** none present. No `Retry-After`, no `X-RateLimit-*`. **[VERIFIED]**

### 1.1 Endpoint catalogue

| Endpoint | Status | Notes |
|---|---|---|
| `bootstrap-static/` | 200, 1.56 MB | Everything static + live ownership. See §1.2 |
| `fixtures/?event={gw}` | 200 | Full 2026/27 fixture list already published |
| `event/{gw}/live/` | 200, `{"elements":[]}` | Empty pre-season; per-element live stats + `explain` once played |
| `element-summary/{id}/` | 200 | `fixtures`, `history` (current season, empty now), `history_past` (per-season aggregates incl. `expected_goals`, `defensive_contribution`, `start_cost`/`end_cost`) |
| `entry/{id}/` | 200 | Manager metadata, `years_active`, league memberships |
| `entry/{id}/history/` | 200 | `current: []` (this season), `past: [...]` season totals + rank |
| `entry/{id}/transfers/` | 200, `[]` | Current season only |
| `entry/{id}/event/{gw}/picks/` | **404 everywhere right now** | See §2 — this is the EO endpoint |
| `leagues-classic/{id}/standings/?page_standings=N` | 200, empty results | See §2.3 |
| `event-status/` | 200, `{"status":[],"leagues":""}` | Bonus/points-confirmation state during a GW |
| `team/set-piece-notes/` | 200 | `last_updated` + per-team free-text set-piece notes. Useful alongside `penalties_order` |
| `stats/most-valuable-teams/` | 200, `[]` | |
| `dream-team/{gw}/`, `entry/{id}/cup/` | 404 | Not yet created for 2026/27 |
| `me/` | 200, `{"player":null,...}` | **Requires cookie auth** to be useful |
| `my-team/{id}/` | — | **Requires cookie auth** (not tested; would need the user's session) |
| `entries/search/?q=...` | **404 — does not exist** | See §2.5 |

### 1.2 `bootstrap-static/` contents **[VERIFIED]**

Top-level keys: `chips`(8) · `events`(38) · `game_settings`(35 keys) · `game_config`(3) ·
`phases`(11) · `teams`(20) · `total_players`(**6,048,863**) · `element_stats`(26) ·
`element_types`(4) · `elements`(**592**).

**`elements` now carries 109 fields per player.** Fields relevant to the blueprint:

- Ownership: `selected_by_percent` (string, e.g. `"34.8"`), `selected_rank`, `selected_rank_type`.
  **This is ownership only. There is no captaincy field anywhere in the API.**
- Availability: `status`, `chance_of_playing_this_round`, `chance_of_playing_next_round`,
  `news`, `news_added`. 111 of 592 players currently carry a `news` string.
- Set pieces: `penalties_order`, `penalties_text`, `direct_freekicks_order`,
  `direct_freekicks_text`, `corners_and_indirect_freekicks_order`,
  `corners_and_indirect_freekicks_text`.
- Expected stats: `expected_goals`, `expected_assists`, `expected_goal_involvements`,
  `expected_goals_conceded`, and a `_per_90` variant of each. Strings, 2 dp.
- Defensive: `defensive_contribution`, `defensive_contribution_per_90`, `tackles`,
  `clearances_blocks_interceptions`, `recoveries`.
- **New / not present in older seasons**: `price_change_projections` (list of
  `{offset, projected_percent, likelihood}` for offsets 0/1/2), `price_change_hourly_rate`,
  `price_change_percent`, `price_change_locked_until`, `price_change_calibrating`,
  `scout_risks` (list, currently empty for all 592), `scout_news_link`, `opta_code`
  (`"p154561"` — a direct Opta join key), `known_name`, `birth_date`, `region`,
  `squad_number`, `team_join_date`, `can_select`, `can_transact`, `has_temporary_code`.

> **`opta_code` is significant** — it gives an official Opta player identifier straight from
> the FPL API, which materially reduces the ID-mapping problem in §4.4.

> **Pre-season gotcha:** all counting stats on `elements` right now (`minutes: 3330`,
> `total_points: 162`, `clean_sheets: 19`) are **last season's carried-over totals**, not
> zeros. Any ingest that starts before GW1 must not mistake these for 2026/27 values.
> `selected_by_percent`, `now_cost` and `news` *are* live.

`teams[]`: `strength_overall_home/away` are populated (4/5 for Arsenal) but
`strength_attack_*` and `strength_defence_*` are all **0** pre-season, and `form`/`strength`
are `null`. The blueprint discards FDR anyway (§4.2) — this confirms it would be unusable
at GW1 regardless.

`events[]` fields include `chip_plays` (aggregate counts per chip), `most_captained`,
`most_vice_captained`, `top_element_info`, `average_entry_score`, `ranked_count`,
`transfers_made`, `overrides`. **`most_captained` is a single element id, not a
distribution** — it does not give captaincy%.

`element_stats` (26) confirms the tracked stat vocabulary, including
`defensive_contribution`, `tackles`, `recoveries`, `clearances_blocks_interceptions`,
`starts`, and all four expected-goal metrics.

### 1.3 2026/27 rules, read from the API **[VERIFIED]**

Read from `game_config.rules` / `game_config.scoring` / `chips`. **Do not hardcode.**

**Squad & transfers**

| Setting | Value |
|---|---|
| `squad_total_spend` | `1000` (= £100.0m, `ui_currency_multiplier` 10) |
| `squad_squadsize` / `squad_squadplay` | 15 / 11 |
| `squad_team_limit` | 3 per club |
| Position quotas (`element_types`) | GKP 2, DEF 5, MID 5, FWD 3; min/max play GKP 1/1, DEF 3/5, MID 2/5, FWD 1/3 |
| `transfers_cap` | 20 |
| `max_extra_free_transfers` | 4 → **max 5 banked free transfers** |
| `transfers_sell_on_fee` | 0.5 |
| `element_sell_at_purchase_price` | `false` |
| `sys_vice_captain_enabled` | `true` |

**Scoring (`game_config.scoring`)**

| Rule | Value |
|---|---|
| `long_play` / `short_play` | 2 / 1 |
| `goals_scored` | **GKP 10**, DEF 6, MID 5, FWD 4 |
| `assists` | 3 |
| `clean_sheets` | GKP 4, DEF 4, MID 1, FWD 0 |
| `goals_conceded` | GKP −1, DEF −1, MID 0, FWD 0 |
| `saves` | 1 |
| `penalties_saved` / `penalties_missed` | 5 / −2 |
| `yellow_cards` / `red_cards` / `own_goals` | −1 / −3 / −2 |
| `bonus` | 1 |
| **`defensive_contribution`** | **DEF 2, MID 2, FWD 2, GKP 0** |
| `mng_*` (Assistant Manager) | **all 0** |
| `bps`, `influence`, `creativity`, `threat`, `ict_index`, `starts`, `expected_*` | 0 (informational only) |

**Chips (`chips[]`) — 8 entries, i.e. two of each, one per half:**

| Chip | 1st half | 2nd half | type |
|---|---|---|---|
| `wildcard` | GW2–19 | GW20–38 | transfer |
| `freehit` | GW2–19 | GW20–38 | transfer |
| `bboost` | GW1–19 | GW20–38 | team |
| `3xc` | GW1–19 | GW20–38 | team |

Every chip carries an `overrides: {rules, scoring, element_types, pick_multiplier}` block
(all empty now). `events[].overrides` has the same shape. **The optimiser must read these**
— they are the mechanism by which FPL can alter rules for a single GW or chip.

**Deltas a 2025/26-era assumption would get wrong:**

1. **No Assistant Manager chip.** It is absent from `chips[]` and all `mng_*` scoring is 0.
2. **Goalkeeper goals are worth 10.** Not 6.
3. **Defensive contribution pays forwards too** (FWD 2), not just DEF/MID.
4. **DC point thresholds are NOT in the API.** `game_config.scoring.defensive_contribution`
   gives the *points* (2), but the CBIT/DC count required to earn them is not exposed
   anywhere in `bootstrap-static`. **[UNVERIFIED]** — must be inferred from `event/{gw}/live/`
   `explain` blocks once GW1 is played, and stored as config, not hardcoded.
5. `cup_*` settings are all `null` — the FPL Cup is not configured for 2026/27 as of today.

`phases[]` gives monthly rank phases (Aug = GW1–2, … May = GW34–38) — relevant only if a
monthly mini-league objective is ever added.

---

## 2. Effective ownership (P1) — the critical section

### 2.1 The `picks/` endpoint

`GET /api/entry/{entry_id}/event/{gw}/picks/` — public, no auth.

**Current behaviour: 404 for every entry and every gameweek.** Verified against entry IDs
1, 500000, 3000000 for GW1 and GW38. **[VERIFIED]** This is expected — FPL blocks pick
visibility before the deadline, and no GW has been played.

Documented response shape: `active_chip`, `automatic_subs[]`, `entry_history{}`, and
`picks[]` where each pick carries `element`, `position`, `multiplier`, `is_captain`,
`is_vice_captain`. **[VERIFIED live, 22 Aug 2026, 01:57 local]** — probed against 11 real
entries (including the tracked entry `3434577`) immediately after the GW1 deadline. Every
field held. `entry_history` additionally carries `event`, `points`, `total_points`, `rank`,
`rank_sort`, `overall_rank`, `percentile_rank`, `overall_rank_percentage`, `bank`, `value`,
`event_transfers`, `event_transfers_cost`, `points_on_bench` — the rank/percentile fields
are **NULL until scores settle** (verified: all 11 captures, pre-scoring, had every one
null). `picks[]` also carries `element_type`. `automatic_subs` was `[]` in all 11 captures
— GW1 had not finished scoring at capture time, so a POPULATED example remains
**[UNVERIFIED]**; see §2.8 for how the ingest layer guards against that gap at runtime
instead of only at dev time.

**A THIRD response state exists, undocumented until found live: HTTP 503, body `"The game
is being updated."`** — FPL's post-deadline maintenance window. Observed continuously
17:31-17:41 UTC (8 consecutive probes) and, measured end-to-end on the real GW1 deadline,
lasting **~27 minutes** (503 from ~17:30Z, first 200 at 17:57Z). **Endpoint-scoped, not
global** — `bootstrap-static/` stayed 200 the entire time `picks/` was 503ing (verified: the
snapshotter task ran successfully at 01:45 local, inside the window). See §2.8.

### 2.2 Historical backfill: **NOT POSSIBLE** from the official API

Three independent lines of evidence, all **[VERIFIED]**:

1. **Entry IDs are re-issued from 1 every season.** `entry/1/` returns
   `joined_time: 2026-07-23T12:08:08Z` with `years_active: 12` and a `past` array running
   back to 2014/15. The ID is a *this-season* handle attached to a persistent account.
2. **The 2025/26 ID range no longer resolves.** A binary search over `entry/{id}/` found the
   maximum valid ID to be **6,051,747** at the moment this was measured; 6,051,748 and above
   404 at that same moment. The 2025/26 game had `ranked_count` 10,752,422, so every ID above
   ~6.05M — the majority of last season's entries — is simply unaddressable now, and this
   part of the conclusion is stable regardless of the exact boundary. **The number itself is
   NOT a constant — see §2.4 for a second measurement taken the same day that came in 134k
   higher, hours later.** Do not cache 6,051,747 as a fact; the *unaddressability* is the
   fact, the specific ID is a snapshot.
3. `entry/{id}/history/` returns `current: []`. Only per-season **totals** survive
   (`season_name`, `total_points`, `rank`, `rank_percentage`). No per-GW rows, no picks.
   A `?season=2025` query parameter is ignored (still 404).

**Conclusion: past-season picks, captaincy and per-entry chip usage are permanently gone.**
Nothing in §3 recovers captaincy either. See §2.6 for what this means for the blueprint.

### 2.3 League 314 sampling

`GET /api/leagues-classic/314/standings/?page_standings=N` → **200**, but
`standings.results: []` and `last_updated_data: null`. League 314 ("Overall", created
2026-07-23) is empty pre-season. **[VERIFIED]**

- Page size per page: **[UNVERIFIED]** — cannot count an empty list. Historically 50.
- Deep page behaviour: `page_standings=50000` returns **200**, not an error, but with an
  empty result set and the response echoing `page: 1` on `new_entries`. Whether deep pages
  are genuinely served or silently clamped is **[UNVERIFIED]** until standings populate.

> Even if deep paging works, it is the *worse* sampling method: it costs one request per
> ~50 entries but the response is rank-ordered, so a page is a rank-stratified cluster, not
> an independent sample, and you still need one `picks/` call per entry afterwards. Its
> only real advantage is a cheap, exact enumeration of the ID space.

### 2.4 Recommended sampling method: **uniform over the entry-ID space**

The ID space is **dense**, which makes this viable and cheap. **[VERIFIED]**

```
max valid entry id (2026-08-19)  = 6,051,747   <-- A SNAPSHOT, NOT A CONSTANT.
                                              Measured 6,185,941 the SAME DAY (+134k in
                                              hours, pre-season signups). Never cache this;
                                              re-run the binary search every run.
bootstrap total_players          = 6,048,863
implied hit rate                 = 99.95%
```

Binary search for the current max costs ~25 requests and should be re-run each week — the
range grows as managers join through the season.

**Bias notes.** ID order is join order, so ID correlates with keenness (low IDs = day-one
registrants, high IDs = late/casual joiners). A *uniform* draw over the full live range is
therefore an unbiased sample of the **registered** population, which is the same
denominator FPL uses for `selected_by_percent` (`total_players`). That gives a free
calibration check: **sampled ownership must reproduce `selected_by_percent` to within
sampling error.** If it does not, the sample is broken. Run that check every week.

If the objective is rank-aware against the *active* field rather than the registered field,
filter to entries with a non-null `summary_overall_rank` and re-weight — but do the
comparison against `selected_by_percent` on the unfiltered sample first.

**Sample size** (binomial SE = √(p(1−p)/n)):

| n entries | SE at p=0.50 | SE at p=0.10 | 95% CI half-width at p=0.50 |
|---|---|---|---|
| 2,000 | 1.12 pp | 0.67 pp | ±2.2 pp |
| 5,000 | 0.71 pp | 0.42 pp | ±1.4 pp |
| **10,000** | **0.50 pp** | **0.30 pp** | **±1.0 pp** |
| 20,000 | 0.35 pp | 0.21 pp | ±0.7 pp |

**Recommendation: n = 10,000 entries per gameweek.** ±1.0 pp on a 50%-captaincy premium is
comfortably inside the decision resolution the rank-aware objective needs, and doubling to
20,000 buys only 0.15 pp of SE for twice the traffic.

### 2.5 Finding "Kokdiang FC"

**There is no public manager-search endpoint.** `entries/search/?q=...` returns 404, and no
search route exists in the Postman collection. **[VERIFIED]** Options, in order of
preference:

1. **Ask the user.** The ID is in the URL when they view their own team:
   `fantasy.premierleague.com/entry/{ENTRY_ID}/event/{GW}`. This is the only reliable route.
2. If the user supplies a league code they are in, page that league's
   `leagues-classic/{id}/standings/` and match on `entry_name == "Kokdiang FC"` — but this
   only works after GW1 scoring, and only for leagues they belong to.
3. Brute-force scanning 6M entries for a team name is not acceptable traffic. Rejected.

### 2.6 Consequences for the blueprint

The blueprint (§2) defines `EO = ownership% + captaincy%`. Given §2.2:

- **Ownership** is backfillable to 2016/17 (§3) and available live.
- **Captaincy is not backfillable at all.** Phase 5 ("Distributions … EO, rank-aware
  objective") cannot be validated on historical seasons using true captaincy.

Three options for the Architect:

- **(a) Start collecting now.** Sample `picks/` from GW1 2026/27 onward. Every week not
  collected is permanently lost. **This is the only time-critical action in this report.**
- **(b) Model captaincy from ownership for backtests.** Fit `captaincy% = f(ownership%,
  price, form, fixture, xPts)` on the 2026/27 data collected under (a), then apply that
  model to archived historical ownership to synthesise historical EO. Defensible, but it
  must be labelled as modelled — not observed — in the bitemporal store.
- **(c) Backtest Phases 1–4 on points only, and gate Phase 5 on 2026/27 forward data.**
  Consistent with "no phase ships without passing its gate", at the cost of Phase 5 having
  no multi-season backtest.

(a) is required regardless of which of (b)/(c) is chosen.

### 2.7 Recommended request pattern for weekly EO

| Parameter | Recommendation |
|---|---|
| **Safe sustained rate** | **2 req/s**, single connection, ±20% jitter |
| Observed tolerance | 20 sequential requests at a 10 req/s target — actual ~3.6 req/s after latency — returned all 200s, no throttling. Probing was stopped there deliberately; this is *not* the breaking point |
| Back-off | On any 429 or 403: stop immediately, exponential back-off from 60 s, no more than 3 retries |
| Concurrency | 1. Do not parallelise. The gain is small and the downside is a block |
| User-Agent | A normal browser UA string. Do not send an empty or obviously scripted UA |
| Weekly budget | 10,000 `picks/` + ~25 ID-range binary search + 1 `bootstrap-static/` ≈ **10,030 requests** |
| Wall-clock | ~83 min at 2 req/s |
| **When to run** | After the GW's final match is scored and `event-status/` reports bonus added. Picks are immutable at that point and CDN-cached, so the sample is consistent and cheap for FPL to serve |
| Also capture | `active_chip` and `entry_history.event_transfers` / `points_on_bench` from the same response — chip-usage rates and the field's bench/hit distribution come free with the sample |
| Storage | Bitemporal: `valid_at` = GW deadline, `observed_at` = fetch time. Store the **raw picks**, not just the aggregate — the aggregate can always be recomputed, the picks can never be re-fetched |

**Reliability risk: LOW probability, CATASTROPHIC impact.** The endpoint is undocumented and
unversioned. FPL has historically not rate-limited it, but has no obligation not to start.
Because a missed week is a permanent hole, build retry-next-day logic and alert loudly on
failure.

### 2.8 Pre-deadline gate-repair (session s003) — three blockers found live, all fixed

Found 21-22 Aug by actually calling the API around the real GW1 deadline, not by reading the
code. Each would have corrupted or destroyed the Tue 25 Aug first real sample. Full account:
`docs/HANDOFF.md` §1, code in `src/fplai/client.py`, `src/fplai/transport.py`,
`src/fplai/sampling.py`, `scripts/sample_picks.py`.

**1. The 503 maintenance state (§2.1) was unmodelled and destroyed the whole run.**
`BACKOFF_TRIGGER_STATUSES` was `(429, 403)` only, so `FPLClient._get` did not back off on
503 — `entry_picks` (which modelled only 404/200) raised immediately, `scripts/
sample_picks.py`'s main loop caught nothing, and `run()` accumulated every row in memory
across the whole ~83-minute run before writing ONCE at the end. One 503, from an endpoint
that demonstrably enters maintenance right when this script runs, discarded everything.
**Fixed**, four parts:
  - `client.py`'s `FPL_BACKOFF_TRIGGER_STATUSES` adds 503 to this client's own default
    (not the shared `transport.py` constant other providers use — no live evidence this
    session that 503 means the same thing for PL API/Odds API).
  - Per-entry `FPLApiError` handling in the main loop — one bad entry is logged and skipped,
    never ends the run. A bounded retry count (`MAIN_LOOP_MAX_RETRIES = 1`) keeps one
    flaky entry from costing the full ~7-minute backoff ladder.
  - **Incremental persistence**: `sample_picks.py` now writes the `picks`/`automatic_subs`
    datasets in `CHUNK_SIZE`-entry batches (default 500, ~4 min of requests), each stamped
    with ITS OWN real completion time as `observed_at` — never one timestamp smeared across
    rows actually collected over 83 minutes. A crash costs the current chunk, not the run.
  - **Resumability**: `fplai.sampling.already_sampled_entry_ids` lets a re-invocation (same
    GW, same seed → same deterministic draw) skip entries already safely persisted from a
    prior/crashed run — a restart costs only what wasn't yet on disk.
  - **The readiness gate itself was a false green.** `check_ready()` gated only on
    `bootstrap_static()` succeeding — and the maintenance window is endpoint-scoped (see
    §2.1), so that check passes, the run proceeds, and dies on the first `picks/` request.
    `probe_picks_endpoint_ready()` now issues one fast (`max_retries=0`), live
    (`force_refresh=True`) probe against `picks/` itself before the run commits to spending
    its budget.

**2. Cached 404s had no TTL and would have silently biased the sample frame.**
`entry/{id}/` 404 means "not issued yet" and `picks/` 404 means "not public yet" — both
TEMPORAL states, not permanent properties, yet `ResponseCache` cached every 404 forever.
Verified live: **9 real `entry/{id}/` 404s** cached 19-21 Aug sat in `cache/fpl_api/`
exactly on `find_max_valid_entry_id`'s binary-search path — a Tue 25 Aug run would have
replayed that search from cache at zero live requests, returning the max entry ID as of
19-21 Aug and silently excluding everyone who registered since (fastest-growth period).
**Fixed at the policy layer, not with `force_refresh` at call sites**: `ResponseCache`
(transport.py) now takes `max_age_for_404_seconds`; a cached 404 older than the TTL (or —
this is what neutralises the 9 pre-existing entries — carrying NO stored timestamp at all,
since every file written before this fix has none) is treated as a MISS, transparently, for
every caller. `FPLClient`'s own default cache enables a 1-hour TTL
(`client.ENTRY_404_TTL_SECONDS`); other providers are unaffected (opted out by default).
**Verified live, read-only, against the real (unmodified) poisoned cache entries**: all 9
now report as cache misses; a real cached 200 (a genuine picks payload) is unaffected.

**3. `automatic_subs[]` gets its OWN capability and dataset, not a column on `picks`.**
It is a different GRAIN — one row per substitution event per entry-gameweek, not per pick —
verified `[]` in all 11 real GW1 captures (pre-scoring), so the design (entity key
`(entry_id, event, element_out)`) rests on the FPL API's publicly documented field names
(`entry`, `element_in`, `element_out`, `event`), NOT a live-verified populated example.
`scripts/sample_picks.py` therefore enforces the declared key's uniqueness AT RUNTIME on
every real batch it ever collects (`_assert_automatic_subs_key_uniqueness`) rather than
relying on a one-off dev-time check that was structurally impossible to run this session —
on a violation it logs loudly and drops that chunk's automatic_subs (never the picks
alongside it). Also widened at zero marginal cost: `picks[].element_type`, and
`entry_history`'s `event`/`points`/`total_points`/`rank`/`rank_sort`/`percentile_rank`/
`overall_rank_percentage` (the last being the rank-aware objective's exact target quantity,
blueprint §2/§10).

---

## 3. Historical archives (P3)

### 3.1 `vaastav/Fantasy-Premier-League` — **primary archive** **[VERIFIED]**

- GitHub, 1,775 stars, last push **2026-08-04** (actively maintained).
- Licence: **`NOASSERTION`** via the GitHub API — a `LICENSE` file exists but is not an
  SPDX-recognised licence. **Treat licensing as unresolved**; read the file before any
  redistribution. Internal analytical use is the low-risk path.
- Seasons: **2016-17 … 2026-27** (2026-27 present, no `gws/` yet — expected, no GW played).
- Per season: `players_raw.csv`, `cleaned_players.csv`, `player_idlist.csv`, `teams.csv`,
  `fixtures.csv`, `players/` (per-player dirs), `gws/gw{N}.csv` + `merged_gw.csv`.

**`gws/gw{N}.csv` columns (verified on 2025-26 GW10, 748 player rows):**

```
name, position, team, xP, assists, bonus, bps, clean_sheets,
clearances_blocks_interceptions, creativity, defensive_contribution, element,
expected_assists, expected_goal_involvements, expected_goals, expected_goals_conceded,
fixture, goals_conceded, goals_scored, ict_index, influence, kickoff_time, minutes,
modified, opponent_team, own_goals, penalties_missed, penalties_saved, recoveries,
red_cards, round, saves, selected, starts, tackles, team_a_score, team_h_score, threat,
total_points, transfers_balance, transfers_in, transfers_out, value, was_home, yellow_cards
```

> **`selected` = raw count of managers owning the player at that gameweek**
> (e.g. 550,561). **`value` = point-in-time price.** `transfers_in`/`out`/`balance` are
> also point-in-time. This is a genuine historical ownership and price series.
> Convert to a percentage using that season's `total_players` / `ranked_count`.

**Fetch:** `https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{season}/gws/gw{N}.csv`,
or clone once (repo is ~183 MB). Cloning once is the polite option; do not hit
raw.githubusercontent in a loop.

### 3.2 `olbauday/FPL-Core-Insights` — **richest recent archive** **[VERIFIED]**

- 191 stars, last push **2026-08-19T08:03Z** (today). Licence: **none declared** — higher
  legal risk than vaastav. Internal use only.
- Seasons: **2024-2025, 2025-2026, 2026-2027**.
- Per season: `players.csv`, `playerstats.csv` (9.6 MB, ~30k rows), `teams.csv`,
  `team_history.csv`, `gameweek_summaries.csv`, `By Gameweek/GW{N}/`, `By Tournament/`,
  `supplemental/`.

**`playerstats.csv` — 87 columns, one row per player per GW.** A per-gameweek snapshot of
the `bootstrap-static` element object, including `gw`, `selected_by_percent`, `now_cost`,
`cost_change_event`, `chance_of_playing_next_round`, `status`, `news`, `news_added`,
`penalties_order`, `direct_freekicks_order`, `ep_next`, `ep_this`, `form`, all four
`expected_*` metrics, `defensive_contribution`, `tackles`, `recoveries`, `set_piece_threat`.

> **This is the single most useful artefact found for bitemporal backtesting.** It gives
> point-in-time price, ownership %, injury flag and set-piece order together — exactly what
> the blueprint's §3.2 rule requires and what a naive backtest would otherwise leak.

**`By Gameweek/GW{N}/` contains** `shots.csv`, `xg_by_minute.csv`, `lineups.csv`,
`average_positions.csv`, `player_match_enrichment.csv`, `playermatchstats.csv`,
`incidents.csv`, `momentum.csv`, `match_enrichment.csv`, `matches.csv`, `fixtures.csv`.

> Shot-level and xG data aligned to **FPL element ids**. This is a credible substitute for
> Understat/FBref for 2024-25 onward *and* it dissolves the ID-mapping problem for those
> seasons.

**`gameweek_summaries.csv`** archives the `events[]` object per GW, including `chip_plays`
(counts per chip: e.g. 2025/26 GW2 — wildcard 433,085, 3xc 267,066, bboost 206,501,
freehit 159,592), `most_captained`, `most_vice_captained`, `ranked_count` (10,752,422),
`average_entry_score`, and a **`snapshot_time`** column.

> **Superseded 2026-08-21 (E2b story 10b, `docs/wiki/provider-framework.md` §13.5) — do
> NOT use `snapshot_time` as `observed_at`.** This section's original read (from a single
> sampled row, 2025/26 GW2) was "snapshot timing is inconsistent, five days before that
> GW's deadline." A live check across every gameweek in both seasons that have this file
> found something stronger: `snapshot_time` is **CONSTANT across an entire season's file**
> (2025-2026: every one of 38 rows stamps `2025-08-17T04:46:20Z`, including GW38 rows that
> already carry `finished=True` and a real score; 2026-2027: every row stamps
> `2026-07-23T15:00:09Z`, all `finished=False`). It is a **stale file-generation stamp**,
> not a per-gameweek observation time — using it as `observed_at` let a bitemporal
> `as_of()` query at an EARLY gameweek's deadline return a LATE gameweek's row (real
> leakage; provider-framework.md §13.5 has the full mechanism and the live evidence,
> including the polarity check proving `playerstats.csv` rows are POST-gameweek, not
> pre-deadline). The corrected rule: impute `observed_at` from `gameweek_summaries.csv`'s
> own `deadline_time` column (verified correct per-row) for gameweek N+1, never from
> `snapshot_time`. `snapshot_time` remains a real column in the file — it is simply not a
> timestamp of when any individual gameweek's data became true or known.

### 3.3 Does any archive contain historical ownership? **Yes. Captaincy? No.**

| Field | vaastav | FPL-Core-Insights | Verdict |
|---|---|---|---|
| Ownership per GW | `selected` (count) 2016-17→ | `selected_by_percent` 2024-25→ | **Available** |
| Point-in-time price | `value` | `now_cost`, `cost_change_event` | **Available** |
| Injury flag at the time | — | `chance_of_playing_next_round`, `status`, `news` | Available 2024-25→ |
| Aggregate chip usage per GW | — | `chip_plays` | Available 2024-25→ |
| Most-captained player id | — | `most_captained` (1 id per GW) | Single id only |
| **Per-player captaincy%** | — | — | **Does not exist anywhere** |

The public live EO tables (Fantasy Football Pundit, LiveFPL) compute captaincy by the same
`picks/` sampling described in §2.4 and publish only a live snapshot — neither is known to
publish a historical series or offer an API. **[UNVERIFIED — not probed; treat as a lead,
not a source.]**

### 3.4 Other

`Fournierp/FPL` (13 stars, Apache-2.0, last push 2026-03-11) mirrors vaastav plus
`data/betting/`, `data/fbref/`, `data/understat/`, `data/fivethirtyeight/` for
2020-21…2024-25. Small and stale, but the `betting/` and `fbref/` directories are worth a
look given §4.2 and §6 are otherwise blocked. Contents **[UNVERIFIED]** — directory listing
only.

---

## 4. xG / advanced stats (P4)

### 4.1 Understat — **access method has changed; all known clients are broken**

**[VERIFIED]** `https://understat.com/league/EPL/2025` returns 200 but the HTML is now an
18 KB shell. The `var teamsData = JSON.parse('\x7B...')` / `playersData` / `datesData`
blobs **are gone** — the only `JSON.parse` left on the page is an adsense promo object.

> Every public Understat client — `understatapi` (0.7.1), `soccerdata`'s Understat scraper,
> `worldfootballR` — parses those blobs out of the HTML. **They will all return empty or
> raise.** Do not build on them.

Reading `js/league.min.js`, `js/match.min.js`, `js/player.min.js`, `js/team.min.js` reveals
the data is now fetched over AJAX from clean JSON endpoints. All four verified live:

| Endpoint | Verified | Returns |
|---|---|---|
| `GET https://understat.com/getLeagueData/{LEAGUE}/{SEASON}` | 200, 530 KB | `{teams{20}, players[537], dates[380]}` |
| `GET https://understat.com/getMatchData/{match_id}` | 200, 40 KB | `{rosters{h,a}, shots, tmpl}` |
| `GET https://understat.com/getPlayerData/{player_id}` | 200, 381 KB | `{player, matches[], groups, positionsList, minMaxPlayerStats, shots, lastMatch}` |
| `GET https://understat.com/getTeamData/{Team}/{SEASON}` | 200, 26 KB | `{dates[], players[], statistics}` |
| `POST https://understat.com/main/getPlayersStats/` | not tested | filtered player table |
| `POST https://understat.com/main/getPlayerMatches/` | not tested | per-player match list |

Send `Referer: https://understat.com/` and `X-Requested-With: XMLHttpRequest`. Season is the
starting year (`2025` = 2025/26). Match ids for 2025/26 run ~28778–29138.

Sample shapes:

```
players[0] : id, player_name, games, time, goals, xG, assists, xA, shots, key_passes,
             yellow_cards, red_cards, position, team_title, npg, npxG, xGChain, xGBuildup
dates[0]   : id, isResult, h{id,title,short_title}, a{...}, goals{h,a}, xG{h,a},
             datetime, forecast{w,d,l}
rosters[*] : id, player_id, team_id, position, positionOrder, player, h_a, time, goals,
             own_goals, shots, xG, assists, xA, key_passes, xGChain, xGBuildup,
             yellow_card, red_card, roster_in, roster_out
```

**`forecast{w,d,l}` on `dates` is Understat's own xG-derived match-result distribution** —
a free sanity check for the Dixon-Coles model in blueprint §4.

**Coverage:** EPL and 5 other leagues, 2014/15 onward.

> ### Compliance flag — Architect decision required
> `https://understat.com/robots.txt` is:
> ```
> User-agent: *
> Disallow: /
> ```
> **[VERIFIED]** Understat disallows all automated access. The blueprint lists Understat as
> a committed source. The endpoints work, but ingesting them is against the site's stated
> wishes. This is a decision for the Architect, not for me. If it proceeds: one league-level
> call per day is sufficient (530 KB covers the whole season), cache aggressively, and never
> exceed ~1 request per 2 s.

### 4.2 FBref — **blocked** **[VERIFIED]**

`https://fbref.com/en/comps/9/Premier-League-Stats` and even
`https://fbref.com/robots.txt` return **HTTP 403** with a Cloudflare "Just a moment…"
interstitial. Retried with a full browser header set (`Accept`, `Accept-Language`,
`Sec-Fetch-*`, `Upgrade-Insecure-Requests`) — still 403.

FBref is **not ingestible** from a plain HTTP client. Options: a headless browser
(Playwright), with the resulting maintenance burden and CF-challenge fragility; a paid
CF-solving proxy; or **drop FBref**. Given §3.2 supplies shot-level and lineup data aligned
to FPL ids for 2024-25 onward, **dropping FBref is the recommendation** and needs a
blueprint amendment.

`soccerdata` (2,019 stars, v1.9.1 on PyPI 2026-07-24, actively maintained) remains useful
for Club Elo, ESPN, Football-Data.co.uk, SoFIFA and WhoScored, but its FBref and Understat
scrapers are subject to the two problems above.

### 4.3 Client library status **[VERIFIED via PyPI/GitHub APIs]**

| Package | Version | Last release | Verdict |
|---|---|---|---|
| `soccerdata` | 1.9.1 | 2026-07-24 | Maintained. FBref/Understat scrapers likely broken (§4.1, §4.2) |
| `understatapi` | 0.7.1 | 2026-02-21 | MIT. **Broken** by the Understat restructure |
| `understat` | 0.1.14 | 2025-12-16 | MIT. Same risk |
| `fpl` | 0.6.35 | 2023-08-14 | **Abandoned** — 3 years stale, predates DC scoring and the current chip structure. Do not use |

**Recommendation: write a thin in-house client.** The FPL API is ~12 endpoints of plain JSON
and the Understat JSON API is 4. Every third-party wrapper here is either stale or broken,
and the blueprint's bitemporal rule requires control over the `observed_at` stamp anyway.

### 4.4 The ID-mapping problem

Joining FPL ↔ Understat ↔ Opta requires a name/team/DOB match. What helps:

- **`elements[].opta_code`** (`"p154561"`) — an official Opta player identifier direct from
  the FPL API. This is the strongest join key available and did not exist in older seasons.
- **`elements[].birth_date`** and `team_join_date` — strong disambiguators for name clashes.
- **`olbauday/FPL-Core-Insights`** already aligns shot-level data to FPL element ids for
  2024-25 onward, sidestepping the problem entirely for those seasons.
- `vaastav`'s `player_idlist.csv` maps FPL id ↔ name per season (FPL-internal only).

No public, maintained, authoritative FPL↔Understat mapping table was found.
**[UNVERIFIED]** — the search for one was not exhaustive. Build the map from `opta_code` +
name + DOB and store it as a versioned artefact with a manual override list.

---

## 5. API-Football (P5)

**Base:** `https://v3.football.api-sports.io` · **Auth:** header `x-apisports-key`
**Key status: works.** **[VERIFIED]** Plan **Free**, active to 2027-08-19.

### 5.1 Quota accounting **[VERIFIED]**

| Header | Meaning |
|---|---|
| `x-ratelimit-requests-limit` / `x-ratelimit-requests-remaining` | **Daily: 100** |
| `x-ratelimit-limit` / `x-ratelimit-remaining` | **Per-minute: 10** |

`GET /status` returns `{"requests": {"current": N, "limit_day": 100}}`.
**Plan-blocked calls return HTTP 200 with an `errors.plan` message and do NOT consume
quota** (no rate-limit headers are returned on those responses). Daily counters appear
eventually-consistent — do not rely on the header for exact accounting; use `/status`.

This session consumed 7 of today's 100.

### 5.2 **The blocker: free tier is locked to seasons 2022–2024**

`GET /fixtures?league=39&season={year}` **[VERIFIED]**:

| Season | Result |
|---|---|
| 2020, 2021 | `errors.plan`: *"Free plans do not have access to this season, try from 2022 to 2024."* |
| **2022, 2023, 2024** | **380 fixtures each — works** |
| **2025, 2026** | **`errors.plan` — blocked** |

Confirmed independently on `/odds?league=39&season=2026` and
`/injuries?league=39&season=2026` — both blocked with the same message.

> **API-Football is unusable for the 2026/27 season on the free tier.** It cannot supply
> live injuries, lineups, events, statistics or odds. It is a **historical backfill source
> for 2022/23–2024/25 only.**

### 5.3 Endpoint verification (on season 2024, league 39) **[VERIFIED]**

Premier League `league.id = 39`. Season parameter is the **starting year** (`2026` =
2026/27, running 2026-08-21 → 2027-05-30). `GET /leagues?id=39` lists 2010–2026 with a
per-season `coverage` object.

| Endpoint | 2024 free tier | Response shape (top-level keys of `response[i]`) |
|---|---|---|
| `/injuries?fixture=` | **Works**, 6 results | `player{id,name,photo,type,reason}`, `team`, `fixture`, `league`. `type` ∈ {"Missing Fixture", "Questionable"}; `reason` is free text ("Ankle Injury") |
| `/fixtures/lineups?fixture=` | **Works**, 2 results | `team`, `coach{id,name}`, `formation` ("4-2-3-1"), `startXI[]`, `substitutes[]` |
| `/fixtures/events?fixture=` | **Works**, 16 results | `time{elapsed,extra}`, `team`, `player`, `assist`, `type` ("Card"/"Goal"/"subst"), `detail`, `comments` |
| `/fixtures/statistics?fixture=` | **Works**, 2 results | `team`, `statistics[{type,value}]` — Shots on/off Goal, Total Shots, Blocked, inside/outside box, Fouls, … |
| `/players?league=39&season=2024` | **Works**, 20/page | `player{id,name,birth,height,weight,injured,…}`, `statistics[]` per team |
| **`/odds`** | **0 results** | Odds are purged for past seasons. `coverage.odds` is `false` for 2025 and `true` only for 2026 — which the free tier cannot reach |

> **Net: API-Football odds are unreachable on the free tier in both directions.** The current
> season has odds but is plan-blocked; the accessible seasons have had their odds deleted.

Injury example (trimmed):
```json
{"player": {"id": 153434, "name": "W. Fish", "type": "Missing Fixture",
            "reason": "Ankle Injury"},
 "team": {"id": 33, "name": "Manchester United"},
 "fixture": {"id": 1208021, "date": "2024-08-16T19:00:00+00:00"}}
```

`/odds/bets` (static, works on free tier) lists **338 markets**, including id **92 "Anytime
Goal Scorer"**, 93 First / 94 Last Goal Scorer, 231/218 home/away anytime scorer, 27/28/188
Clean Sheet, 5 Goals Over/Under, 8 Both Teams Score, 1 Match Winner, 80 Cards O/U. The
markets exist in the schema; the data is inaccessible at this tier.

### 5.4 Recommended usage

**Do not build the live pipeline on API-Football.** Use it once, offline, to backfill
2022/23–2024/25 injuries, lineups, events and fixture statistics for model *training* —
especially the minutes model in blueprint §4.1, which needs historical lineup and rotation
data. At 100 req/day and 380 fixtures/season, a three-season `fixtures/lineups` backfill is
~1,140 requests ≈ **12 days of quota** for that endpoint alone. Plan it as a slow background
job, or price the paid tier if this data proves load-bearing.

---

## 6. Bookmaker odds (P6)

### 6.1 The Odds API — **SOURCED AND VERIFIED** (superseded the original recon below)

| Tier | Price | Quota | Coverage |
|---|---|---|---|
| Free | $0 | **25 requests / day** | **NBA + MLB, h2h only** — no soccer at all |
| Professional | $29/mo | 20,000/mo | 25 sports incl. EPL; h2h, spreads, totals |
| Business | $99/mo | 200,000/mo | + 50 international books, historical, **player props for NBA, NHL, MLB, WNBA, AFL, NRL (NFL in season)** |

> **~~Soccer player props — including anytime goalscorer — are not offered on any tier.~~**
>
> **SUPERSEDED 2026-08-19.** The table and the "not offered on any tier" line above were both
> produced by an **unauthenticated** probe against the vendor's public pricing page — it never
> tested with a real key. Re-tested with the live `THE_ODDS_API_KEY`, the **free plan**
> (the one this project is on) returns EPL `h2h` and `totals` across **42 bookmakers including
> Pinnacle and Betfair Exchange**, plus `player_goal_scorer_anytime` (**5 books, 212
> outcomes/fixture, re-verified live by XL-Coder 2026-08-21** — see
> `docs/wiki/provider-framework.md` for the adapter build record, including the caveat that
> 212 is an observed maximum, not a guarantee, for every fixture), `player_shots_on_target`,
> `btts`, `alternate_totals` and `team_totals`. Quota is **500 credits/month**, cost = regions
> x markets per call, ~12-22 credits/gameweek depending which markets are pulled (§3.3 has the
> exact breakdown). The real limitation is `/historical/` → **401, paid plans only**, which is
> why blueprint §3.3 records odds as a **live-only overlay that cannot enter the backtest**.
>
> **Authoritative source: blueprint §3.3, not the pricing-page table above** — that table is
> kept here only as a record of what the vendor's public page claims for tiers this project
> does not use; it does not describe the free plan's real, authenticated behaviour.

Credit accounting: `cost = markets × regions`; historical endpoints cost **×10**.
Endpoints: `/v4/sports/`, `/v4/sports/{sport}/odds/`, `/v4/sports/{sport}/events/{id}/odds`,
`/v4/historical/sports/{sport}/odds?date=`.

**Adapter built E2b story 8, 2026-08-21** — `src/fplai/providers/odds.py`. Both markets are
live and integrated: `match.odds@fixture` (h2h+totals, one call for all fixtures) and
`player.goal_odds@fixture` (anytime-goalscorer, per fixture). `player_shots_on_target` is
deliberately deferred. Full record, including the live identity-resolution match rates (teams
20/20, players 41/42) and the two real bugs the live verification run caught: `docs/wiki/
provider-framework.md`.

### 6.2 `odds-api.io` — the remaining candidate **[UNVERIFIED]**

Vendor claims a permanent free tier of **100 REST requests/hour**, pre-match only, two
bookmakers at a time (swappable), a 3-day WebSocket trial, and explicitly *"individual
player prop markets including anytime goalscorer for 30+ players"* on the free tier.

**Not verified.** Verification requires creating an account, which is outside my permitted
actions. The claim appears on the vendor's own comparison blog post and should be treated
with scepticism until tested.

> **Action for the user:** if anytime-goalscorer odds matter as much as the blueprint says,
> sign up at odds-api.io, obtain a key, and I will verify EPL coverage, actual market
> availability, de-vig quality, and the real rate limit.

### 6.3 Other candidates **[UNVERIFIED]**

| Source | What it offers | Note |
|---|---|---|
| **Betfair Exchange API** | Free with an app key; exchange prices are the sharpest available and it carries correct-score and first/anytime-goalscorer markets | Requires account + app-key approval + certificate auth. Highest quality if the onboarding is acceptable |
| **Football-Data.co.uk** | Free historical CSVs per season with closing 1X2, O/U 2.5 and Asian handicap from multiple books | **Could not reach the host** — `www.football-data.co.uk` connect-timed-out on both 443 and 80 from this machine. Cause unknown (network/geo). Worth retrying from another network. No goalscorer markets |
| `sportsgameodds.com`, `prop-line.com`, BetsAPI, OpticOdds | Commercial player-prop APIs | Not evaluated. Likely paid |
| `Fournierp/FPL` → `data/betting/` | Archived odds 2020-21…2024-25 | Free, in-repo. Worth inspecting for backtest-only odds |

### 6.4 Interim recommendation

1. **Backtesting odds (Phases 1–4):** try `Fournierp/FPL` `data/betting/` and
   Football-Data.co.uk (once reachable). Free, historical, sufficient for the
   scoreline-prior use in blueprint §3.3(1).
2. **Anytime goalscorer (§3.3(2)):** currently **unsourced**. Verify odds-api.io, else
   Betfair Exchange, else budget for a paid provider — or accept that the direct
   market-implied `P(scores)` feature does not ship in v0.
3. §3.3(2) is a Phase-5-era feature, so there is time — but the Architect should not plan
   around a free source that has not been shown to exist.

---

## 7. Reliability risk summary

| Source | Risk | Failure mode | Mitigation |
|---|---|---|---|
| FPL `bootstrap-static/` | **Low** | Schema drift between seasons | Read all rules from `game_config`; assert on unknown fields rather than silently dropping them |
| FPL `picks/` (EO sampling) | **Low probability, catastrophic impact** | Rate-limiting introduced; data unrecoverable if a week is missed | 2 req/s, immediate back-off, retry-next-day, loud alerting |
| Community archives | **Medium** | Maintainer stops; licences undeclared | Clone and vendor a snapshot into our own store immediately. Do not depend on live raw.githubusercontent reads |
| Understat | **High** | Undocumented private endpoints, restructured once already this year; robots disallows all | Wrap in one adapter module with contract tests; cache every response; expect breakage |
| FBref | **Blocked** | Cloudflare 403 | Drop; substitute FPL-Core-Insights shot data |
| API-Football | **Low but useless** | Free tier locked to 2022–2024 | Backfill only. Keep it out of the live path |
| Odds | **Unsourced** | No verified free anytime-goalscorer source exists | Escalated to Architect; see §6.4 |

---

## 8. Consolidated recommended usage pattern

| Cadence | Call | Cost |
|---|---|---|
| Every 30–60 min, deadline week | `bootstrap-static/` | 1 req. Captures `selected_by_percent`, `now_cost`, `news`, `price_change_projections` drift. Stamp `observed_at`, and record the CDN `Age` header — the payload can be hours old |
| Daily | `team/set-piece-notes/` | 1 req. Has its own `last_updated` |
| Per GW, at deadline | `fixtures/`, `bootstrap-static/`, `event-status/` | 3 req. This is the bitemporal "as-of-deadline" snapshot the backtester replays |
| During matches | `event/{gw}/live/` | 1 req / 5 min. Provides the `explain` blocks needed to reverse-engineer the DC thresholds |
| **Per GW, post-scoring** | **`entry/{id}/event/{gw}/picks/` × 10,000** | **~10,030 req @ 2 req/s ≈ 83 min. THE time-critical job** |
| Daily (if approved) | `understat.com/getLeagueData/EPL/2026` | 1 req, 530 KB, covers the whole season |
| One-off | Clone `vaastav` + `olbauday` archives into our store | 2 clones |
| One-off, slow | API-Football backfill, seasons 2022–2024 | 100 req/day budget |

---

## 9. Could NOT verify

| Item | Why |
|---|---|
| ~~`picks/` response shape~~ | **RESOLVED, VERIFIED live 22 Aug 2026 — see §2.1.** Every field held on 11 real captures; `automatic_subs` populated example remains unverified (see §2.8) |
| **League 314 page size and deep-page behaviour** | Standings are empty pre-season. `page_standings=50000` returned 200 with an empty list, which proves nothing. **Verifiable after GW1 scoring** |
| **Whether uniform ID sampling reproduces `selected_by_percent`** | Requires `picks/` to work — now does (§2.1). Still requires the real 10,000-entry Tue 25 Aug sample to actually run; not exercised this session by design (CLAUDE.md: do not probe/run the real sample outside its scheduled window) |
| **Defensive-contribution point thresholds** | Not present anywhere in `bootstrap-static`. Must be reverse-engineered from `event/{gw}/live/` `explain` blocks after GW1 |
| **`my-team/{id}/` and `me/` authenticated shapes** | Require the user's session cookie. Not attempted — credential handling is out of scope for me |
| **"Kokdiang FC" entry ID** | No public search endpoint exists. **Ask the user for the number in their team URL** |
| **The true FPL rate limit** | Deliberately not probed to failure. Tested clean to ~3.6 effective req/s over short bursts and stopped there. The 2 req/s recommendation is conservative by design |
| **Football-Data.co.uk** | Host unreachable from this machine (connect timeout on 443 and 80). Cause unknown |
| **odds-api.io free tier and goalscorer markets** | Requires account creation, which I may not do. See §6.2 |
| **Betfair Exchange API** | Requires account, app-key approval and certificate auth |
| **Understat POST endpoints** (`main/getPlayersStats/`, `main/getPlayerMatches/`) | Found in the JS but not called — the four GET endpoints already cover the need |
| **Archive licences** | vaastav is `NOASSERTION`; olbauday declares none. GitHub API metadata only — the actual `LICENSE` files were not read |
| **`Fournierp/FPL` `data/betting/` and `data/fbref/` contents** | Directory listing only. Worth a follow-up given §6 is otherwise unsourced |
| **LiveFPL / Fantasy Football Pundit EO tables** | Not probed. Neither is known to offer an API or a historical series |
