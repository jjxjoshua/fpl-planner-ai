# FPL-AI — Engineering Blueprint

> Status: **v0 planning**, ratified 2026-08-19.
> This document is the single source of truth for scope, architecture, and phase gates.
> Changes require an Architect-authored amendment entry at the bottom.

---

## 1. Thesis

Most FPL tools maximise expected points for the next gameweek. That is the wrong
objective and the wrong horizon. FPL is:

- a **tournament** — your score matters only relative to the field, so the target is a
  rank distribution, not a point total;
- a **multi-period problem under uncertainty** — transfers roll, prices drift, chips are
  once-per-season options, so greedy weekly decisions leak value;
- **lumpy and correlated** — player returns are multi-modal and teammates' outcomes are
  strongly dependent, so scalar xPts discards the information that actually drives
  captaincy, chips, and risk.

FPL-AI is built around those three corrections. Everything else is implementation.

### Non-goals

- We are not building a chatbot that picks teams. LLMs never touch the numbers (§5).
- We are not shipping a UI until a solver provably beats the template in backtest (§7).
- We are not chasing feature breadth. Every component must pass a numeric gate or it
  does not ship.

---

## 2. The objective function

**Decision: rank-aware optimisation.**

Let `S` be a candidate squad decision (15 players, XI, bench order, captain, transfers,
chips) and `F` the field of other managers.

We do not maximise `E[points(S)]`. We maximise a utility over the *distribution* of

```
delta(S) = points(S) - points(F)
```

where the field is approximated by **effective ownership** (EO):

```
EO(player) = ownership% + captaincy%   (+ vice-captain and chip adjustments)
```

Practical consequences that must survive into the optimizer:

| Situation | Naive xPts says | Rank-aware says |
|---|---|---|
| 65%-EO premium captain | Always captain | Captaining is *risk-neutral*; not captaining is a large active bet |
| +1.5 xPts at 5% EO vs +3.0 xPts at 60% EO | Take the +3.0 | Depends on aggression setting and current rank |
| Three players from one club | Fine, under the cap | Correlated clean sheets — real variance is far above the naive estimate |

An **aggression parameter** (`rank_target`: `overall_top_1k` / `top_100k` / `mini_league`
/ `protect`) sets the utility's risk curvature. Template-hugging and differential-seeking
both fall out of the same objective at different settings.

**This requires distributions and covariances, not means.** See §4.

---

## 3. Data layer

### 3.1 Committed sources

| Source | Provides | Cost / limits | Risk |
|---|---|---|---|
| **FPL Official API** | Prices, ownership, minutes, points, fixtures, `chance_of_playing_next_round`, live bonus | Free, unauthenticated | Undocumented; shape changes between seasons |
| **olbauday/FPL-Core-Insights** | xG, shot-level data, lineups, per-GW ownership — **pre-aligned to FPL element ids** | Free archive | Coverage starts 2024-25 |
| ~~Understat / FBref~~ | — | — | **DROPPED.** FBref is Cloudflare-403 on all paths. Understat's `robots.txt` is `Disallow: /`, it has **no REST API at all** (every client is unofficial page-scraping, and the 2026 restructure broke all of them), and olbauday covers the same ground. Understat is retained as a **documented last resort for historical gaps only** — see the 2026-08-21 amendment for the conditions. |
| **The Odds API** (free) | h2h, totals, BTTS, **anytime goalscorer**, shots on target | 500 credits/mo; **no historical** | Props carried by only ~5 books; live-only, so unbackfestable |
| **News / press** | Injury status, press conferences, lineup hints, tier-1 journalists | Free; scrape + LLM extraction | Highest maintenance; most fragile |
| **Historical archives** | Multi-season gameweek-level history for backtesting | Free (public datasets) | Schema drift across seasons |

### 3.2 The bitemporal rule (non-negotiable)

Every fact is stored with **two timestamps**: when it was *true* and when we *learned it*.
All model training and all backtesting query the store **as of a deadline**, never as of
today.

> Backtesting with today's injury table produces spectacular fake results. This rule is
> the difference between an engineering project and a demo.

Store: append-only Parquet, queried through DuckDB. Nothing is ever updated in place.

#### Timestamps stay timezone-aware; `tzdata` is a required dependency

**Decision 2026-08-21.** Python's `zoneinfo` has no bundled tz database on Windows, so
`ZoneInfo("UTC")` raises unless the `tzdata` package is installed. Polars resolves the zone
when a tz-aware column is *materialised*, and on failure **panics from Rust** —
`PanicException`, which derives from `BaseException` and is therefore not caught by ordinary
`except Exception`. Every read of `observed_at` was one `.to_list()` away from an
uncatchable crash.

Rejected alternative: storing naive datetimes. This system's entire premise is *when a fact
was true* versus *when we learned it*; making those two columns naive trades a 500KB
pure-data dependency for a permanent class of silent timezone bug. Bad trade here.

**`tzdata` is therefore a runtime dependency, not a dev convenience.**

**Portability note:** Linux and macOS carry a system tz database, so this fails *only* on
Windows. CI on Linux would never catch it. A regression test must materialise a timestamp
column from a real store read, on the target platform.

#### `as_of` returns STATE, not the observation stream

**Decision 2026-08-20, after the primitive was found returning the wrong thing.**

An append-only store accumulates many observations of the same entity. On 2026-08-20,
`as_of('elements', now)` returned **7,735 rows: 595 players x 13 snapshots** — every
observation up to T, rather than the state at T.

That is a silent-corruption bug of exactly the kind §3.2 exists to prevent. It does not
raise. A consumer that forgets to deduplicate trains on 13x-duplicated rows, weighted by
how often each entity happened to be snapshotted. By season end the same query would return
~1.1M rows where it should return 595.

**Required semantics:**

| Method | Returns |
|---|---|
| `as_of(dataset, t)` | **State at t** — one row per entity key, the latest observation with `observed_at <= t`. What training and backtest must use. |
| `observations(dataset, until=t)` | The raw append-only stream, all rows. Explicit opt-in, for audit and for modelling observation cadence. |

#### An entity key must match the data's actual grain

**Learned 2026-08-21, the third key-omission bug.** `vaastav_player_gameweek_stats` was
keyed `(season, round, element)`. A player plays **twice in a double gameweek**, so that key
is not unique — `as_of()` silently dropped **7,141 rows, 4% of the dataset**, concentrated
entirely on double and triple gameweeks.

That is the worst possible bias: double gameweeks are when chips are played and when the
largest hauls occur (§6.2). A model trained on that view would systematically undervalue
exactly the thing chip strategy depends on. It never raised.

**The test that catches this: a declared key must be unique *within a single observation
batch*.** If one batch contains duplicate keys, the key does not match the data's grain.

This also exposes a real limitation of the earlier invariant "`as_of` rows == distinct entity
keys": that is **tautological** — `as_of` collapses *to* the declared key, so it holds
whether or not the key is correct. It verifies internal consistency, not correctness.

Prior instances of the same class: `picks` omitted `event`; this omitted `fixture`. The
pattern is keys declared **by inspection of a schema** rather than derived from the data's
grain. Declare the key by asking "what combination is unique in one batch of source rows",
then verify it against real data.

Every dataset **declares its entity key** (`elements` -> `id`, `teams` -> `id`,
`game_config` -> singleton). **A dataset without a declared key must make `as_of` raise, not
guess.** Guessing the key is how the same class of silent wrongness returns by another route.

This follows the precedent already set by naming `latest()` separately from `as_of(now)`:
where two operations differ subtly and one is dangerous, the API must make misuse *read* as
a bug rather than rely on the caller's diligence.

#### Valid time is per ROW, not per batch — and `as_of` resolves OBSERVED time only

**Decision 2026-08-22, after the third consumer independently routed around the same hole.**

The opening line of this section — "every fact is stored with two timestamps" — was not true
at row granularity. `BitemporalStore.write()` takes `valid_at` as a **single scalar** and
stamps it on every row of the batch (`pl.lit(valid_at).alias("valid_at")`).

That is exactly right for a **snapshot**, where every fact genuinely *is* true at one instant
— an ownership capture, a price table. It is exactly wrong for a **bulk-ingested archive**,
where one batch carries seven seasons and each row's valid time is its own kickoff. For those
datasets `valid_at` is a meaningless constant and `observed_at` is ingest time, so
`as_of(dataset, historical_deadline)` correctly returns **empty** — correct by contract, and
useless.

Callers were therefore left with no sanctioned option and reached for the archive's own domain
column by hand. **Corrected 2026-08-22:** this was **two** consumers, not three —
`fplai.models.team_strength` and `fplai.models.minutes`, which independently arrived at
`observations()` plus a manual `kickoff_time` filter and reproduced the same justifying comment
nearly verbatim. `fplai.backtest.data` was wrongly counted as a third by this amendment's first
draft; it has no `kickoff_time` filter at all, enforcing a round-number boundary
(`round < gameweek`) structurally in `SeasonReplay` instead, which is at least as conservative.
Two consumers converging on an unsanctioned workaround is still a missing primitive. *(The
three-consumer claim originated in one s002 finding and was propagated through a wiki page into
this amendment without re-verification — a single unverified observation can look corroborated
purely by being restated in three places.)*

What makes this urgent rather than untidy: that hand-written predicate **is a leakage
boundary**. Four more models (attacking, defensive, bonus, cards) are still unwritten. Seven
hand-rolled copies of a `<=`-versus-`<` decision is seven independent chances to get §3.2
silently wrong, and silent is the whole problem.

**Ruling:**

1. A dataset whose rows carry individual valid times **declares its valid-time column** in the
   canonical schema, alongside its entity key. Declaration, not inference.
2. `as_of()` keeps today's semantics **unchanged** — *what did we know at time T*, resolved on
   `observed_at`. It is not overloaded.
3. A **separately named** primitive resolves *what was true at time T* on the declared
   valid-time column, collapsing to state on the entity key exactly as `as_of()` does. Two
   operations that differ subtly get two names, per the `latest()`/`as_of(now)` precedent above.
4. A dataset that declares no valid-time column keeps today's behaviour. A dataset that
   declares one **must not** be queryable through the wrong primitive by accident.
5. **No data migration in this story.** Populating `valid_at` per row at write time would mean
   rewriting every archive dataset, a blast radius larger than the bug warrants. The new
   primitive reads the declared column. Per-row `valid_at` at write time is a follow-on story,
   and until it lands, `valid_at` on a bulk-ingested dataset remains a known-meaningless
   constant that nothing may depend on.

### 3.3 Odds — SOURCED AND VERIFIED

**Status corrected twice. Current state is verified by direct probe with the live key
(`THE_ODDS_API_KEY`), 2026-08-19.** The interim recon finding that soccer props were
unavailable was wrong — it did not test with an authenticated key.

**The Odds API free plan, verified working:**

| Market | Endpoint | Books | Verified |
|---|---|---|---|
| `h2h`, `totals` | `/sports/soccer_epl/odds` | **42** incl. **Pinnacle** and **Betfair Exchange** | Yes |
| `player_goal_scorer_anytime` | `/sports/soccer_epl/events/{id}/odds` | 5 | Yes — 212 outcomes/fixture |
| `player_shots_on_target` | per-event | 3 | Yes |
| `player_assists` | per-event | 1 | Thin |
| `btts` | per-event | 15 | Yes |
| `alternate_totals`, `team_totals` | per-event | 11 / 1 | Yes |

**Quota: 500 credits/month.** Cost = regions x markets per call. Per-gameweek budget:

| Pull | Credits |
|---|---|
| h2h + totals, all 10 fixtures, 1 region | 2 |
| anytime goalscorer, 10 fixtures, 1 region | 10 |
| shots on target, 10 fixtures, 1 region | 10 |
| **Total per GW** | **~22** |

At ~4.3 GW/month that is ~95 credits — comfortable headroom inside 500. Use `regions=uk`
alone; adding `eu` doubles cost for marginal extra books.

**On the "most bookmakers" concern:** a non-issue for match odds — Pinnacle and Betfair
Exchange are the two sharpest markets in existence and both are present. For goalscorer
props, 5 books is thin but sufficient to de-vig against.

**The real constraint: `/historical/` returns 401 on the free plan.**
`HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN`. This has a hard design consequence:

> **Odds are a live-only overlay.** They cannot enter the backtest, so the team model must
> stand alone and be validated without them. Odds then act as a live-time shrinkage
> adjustment whose incremental value **cannot be backtested** — only forward-tested.
> Any claim that odds improve results must be earned prospectively, not assumed.

Backtest-era odds, if wanted later, need a free historical CSV archive (football-data.co.uk
was unreachable during recon and remains unverified) or a paid plan.

**De-vig note:** anytime-goalscorer books quote only the `Yes` side, so per-player de-vig is
impossible directly. Normalise instead: scale a team's player probabilities so their sum
matches the team goal expectation implied by `totals` + `h2h`. This is principled and uses
two markets to correct one.

### 3.3.1 Odds are conditional on selection — a hard rule

**Learned the hard way, 2026-08-19.** A bookmaker's anytime-goalscorer price is
`P(scores | plays)`, near enough. It carries **no information about whether the player
starts** — books quote squad players who will not be selected, and they quote them at
prices that look attractive precisely because the market assumes participation.

During the GW1 review this produced a wrong recommendation: a forward priced at 26.7% to
score had, days earlier, been displaced by a club-record signing. The number was correct
and the conclusion drawn from it was not.

**Rule:** any odds-derived quantity entering the optimiser must be multiplied by the
minutes model's `P(start)` before use. Raw market probability is never a selection signal.

```
P(returns) = P(start) x P(scores | plays)   # never the right-hand term alone
```

This is a specific instance of the general principle in §4.1: **minutes gate everything.**
A pipeline that lets a market price reach the objective function without passing through
the minutes model is a design error, not a tuning problem.

### 3.4 Effective ownership acquisition

The rank-aware objective (§2) depends on EO, which the FPL API does **not** provide directly:

- **Ownership** — `bootstrap-static` gives `selected_by_percent`, but **current value only**.
  No history. Every deadline that passes unsnapshotted is data permanently lost.
- **Captaincy** — not exposed at all. `events[].most_captained` gives a single player ID
  per gameweek; that is nowhere near a field distribution. Captaincy is the larger half of
  EO, so this gap matters more than the first.

**HISTORICAL BACKFILL IS IMPOSSIBLE FOR CAPTAINCY. Verified, permanent.**

FPL **re-issues entry IDs from 1 every season**. `entry/1/` reports a join time of the
current pre-season with `years_active: 12` — the ID is a this-season handle on a persistent
account, not a stable key. Max valid id today ~6.05M vs 2025/26's 10.75M ranked entries, so
most of last season's range 404s. `entry/{id}/history/` returns `current: []`;
`?season=` is ignored. **Past-season picks are unaddressable.**

| Component | Historical availability |
|---|---|
| **Ownership** | **Recoverable.** vaastav (`selected`, `value`, 2016-17→) and olbauday (`selected_by_percent`, `now_cost`, 2024-25→) |
| **Captaincy** | **Gone.** No archive carries per-player captaincy%. Only `events[].most_captained` — one element id per GW |

**Consequence for Phase 5.** The rank-aware gate cannot be validated on historical
captaincy. Revised approach:

1. Fit a **captaincy model** — captaincy% as a function of ownership, price, form, fixture,
   and `most_captained` identity.
2. **Calibrate it forward** on data collected from GW1 2026/27 onward.
3. Apply it backward over archived ownership with explicit uncertainty bands, using
   historical `most_captained` as a weak per-GW anchor.
4. **The store must label modelled captaincy as modelled.** It must never be queryable in a
   way that lets it pass as observation. This is a hard schema requirement.

### Forward collection (the only source of real captaincy data)

Sample `entry/{id}/event/{gw}/picks/` — public post-deadline, returns `is_captain`,
`is_vice_captain`, `multiplier`, `active_chip`.

- **Method: uniform random draw over the entry-id space** (verified dense, ~99.95% hit
  rate). Re-run the ~25-request id binary search weekly as the range grows.
- **Not league-314 paging** — standings are rank-ordered, yielding clustered rather than
  independent samples. (An elite-template sample is a separate, deliberately biased draw —
  useful, never confused with the field.)
- **n = 10,000 entries/GW** → ±1.0pp at 95% on a 50%-captaincy premium. ~10,030 requests,
  ~83 min at 2 req/s.
- **Timing:** after the GW's final match is scored, when picks are immutable and CDN-cached.
- **Free calibration check:** sampled ownership must reproduce `selected_by_percent`. If it
  doesn't, the sample is biased — treat as a hard failure, not a warning.

**Why 2 req/s** (the operative rule lives in `AGENTS.md`): no rate-limit headers exist and
Fastly fronts the API, so there is no signal to steer by. Probing reached ~3.6 req/s cleanly
and was stopped deliberately — that is *not* the breaking point and must never be treated
as a budget.

**Immediate and unrecoverable:** ownership snapshotting must begin before the GW1 deadline
and picks sampling immediately after GW1 completes.

**Unverified:** the `picks/` response shape 404s pre-deadline. The entire EO plan rests on
it. **Re-verify from Sat 22 Aug 01:30 local (17:30 UTC Fri) before building against it.**

### 3.5 Source division of labour

| Need | Source | Why |
|---|---|---|
| Live season core | FPL API (in-house thin client) | Only real-time source. Every third-party client is stale or broken (`fpl` 0.6.35 is 3 years old); the bitemporal `observed_at` rule requires our own control anyway |
| Historical backfill | vaastav (2016-17→) + olbauday (2024-25→) | Free, bulk, and olbauday is pre-aligned to FPL element ids |
| Historical lineups / injuries / match events | API-Football, **2022-24 only** | Free tier is hard-locked to seasons 2022-2024 — verified. Useless for 2025 and 2026. Trickle at 100 req/day for minutes-model training data only |
| Ownership / captaincy | FPL API sampling (§3.4) | Only viable source; captaincy has no alternative at all |
| Live market signal | The Odds API (§3.3) | h2h/totals across 42 books + goalscorer props. **Live only — never in backtest** |
| Cross-source **player** ids | `elements[].code` == PL API player id | Verified 599/599. `opta_code` is the same value string-prefixed (`p223094`). Free, exact |
| Cross-source **team** ids | `elements`/`teams[].code` == PL API team id | **Verified 20/20, 2026-08-20**, including newly-promoted Coventry(9), Hull(88), Ipswich(40) — which rules out a stale-list coincidence. `teams[].opta_code` is `None`; this is a *legacy FPL* code that happens to be shared, not an Opta code. **Never join teams on name** — 4 of 20 differ (`Man Utd`/`Manchester United`, `Spurs`/`Tottenham Hotspur`, `Nott'm Forest`/`Nottingham Forest`, `Man City`/`Manchester City`) |

**Rejected:** FBref (Cloudflare 403 everywhere) · Understat (`robots.txt: Disallow: /`).

### 3.6 Premier League API — use decision and its conditions

**Decided by the user, 2026-08-19, after the Architect raised the Terms of Use question.**

premierleague.com's ToU bar commercial use and "creating a database... that includes
material obtained from the Website", with a carve-out for "your own private and personal
use". The user's reading: the clause targets redistribution and public re-serving, and a
private store for personal play falls inside the carve-out. Proceeding on that basis.

**The reading is only true while these hold. They are binding constraints, not preferences:**

1. **Private and personal use only.** The store exists to inform one manager's own team.
2. **Never commercialised.** No paid product, no ads, no resale — direct or indirect.
3. **Never redistributed.** No republishing raw or derived PL data. Nothing in a public
   repo, no public API, no shared dataset. `data/` stays gitignored.
4. **Polite access.** 2 req/s, single connection, cached, backoff. Already enforced by the
   client.
5. **Revisit if the project's nature changes.** If FPL-AI ever becomes public-facing or
   commercial, this decision expires and the PL API must be replaced — API-Football covers
   P1 at a comparable price with a clean licence. Design the ingest layer so the P1 source
   is swappable; do not let PL-specific response shapes leak past the ingest boundary.

Point 5 is the engineering consequence: **treat the P1 source as pluggable from day one.**

---

## 4. Model layer

Each model is independently trainable, independently calibratable, and independently gated.

| Model | Output | Notes |
|---|---|---|
| **Team strength** | Attack/defence ratings to scoreline distribution | Dixon-Coles / bivariate Poisson fit on **xG, not goals**; time-decayed; home advantage; shrunk to odds |
| **Minutes** | `P(start)`, `P(sub given no start)`, `E[min given start]` — a *distribution* | Highest-ROI model in the system. See §4.1 |
| **Attacking involvement** | Player share of team goals / assists | Minutes-weighted; set-piece and penalty responsibility as explicit features |
| **Defensive** | `P(clean sheet)`, goals-conceded bands, saves, defensive-contribution points | Position-specific; derived from team model, never from FPL's FDR |
| **Bonus (BPS)** | Expected bonus distribution | Modelled from BPS components, not from historical bonus alone |
| **Cards / discipline** | Yellow/red probabilities | ~~Referee assignment is a real feature~~ — **measured and evidenced FALSE, 2026-08-29**: fitting with vs without referee identity moved log-loss by **+0.0002** and Brier by **−0.0000139** over 219 folds / 64,531 OOS rows. Do not build an announcement-time capability for officials |
| **Price change** | `P(rise)`, `P(fall)` | Low weight by design — see §8 anti-patterns |
| **News extraction** | Structured injury/availability facts | LLM; bounded update only. See §5 |

### 4.1 Minutes — the model that decides the project

A player with elite xG and a 60% start probability is usually worse than a mediocre player
at 95%. Required features, beyond the obvious:

- **European congestion** — midweek UCL/UEL fixture within N days, competition tier,
  whether the tie is already decided, squad depth at that position. One of the strongest
  rotation predictors in the game and routinely omitted by public tools.
- **Manager rotation priors** — rotation cadence is manager-specific and learnable.
- **Regime change flag** — a new manager inflates variance rather than silently breaking
  the model.
- Starts/sub history, position changes, recent substitution patterns (hooked at 60' vs
  playing 90').

### 4.2 Fixture difficulty is derived, never assigned

FPL's official 1–5 FDR is discarded. It is hand-set, near-static, and **position-blind** —
it cannot express that a fixture is hard for defenders and easy for attackers. Difficulty
is an *output* of the team model, computed per position, per fixture.

### 4.3 Everything emits a PMF

Each player-gameweek produces a **points probability mass function**, not a scalar. Squad
evaluation is correlated Monte Carlo over simulated matches — teammates' clean sheets
share an outcome, so covariance is simulated rather than assumed away.

This is what makes captaincy, chips, and rank-aware optimisation tractable at all.

---

## 5. Where the LLM is allowed to operate

**Architectural boundary. Absolute.**

| Layer | Owner | Rationale |
|---|---|---|
| Prediction (PMFs, minutes, CS) | Statistical models | Reproducible, backtestable, calibratable |
| Optimisation (squad, transfers, chips) | MILP solver (HiGHS) | Provably optimal under constraints; an LLM cannot reliably satisfy budget + club caps + formation |
| **News to structured facts** | LLM | Genuine strength: unstructured extraction |
| **Explanation of solver output** | LLM | Genuine strength: narration |
| **Scenario framing** | LLM | Genuine strength |

The instant an LLM is asked to "pick a good squad," the output is fluent, plausible
mediocrity that cannot be backtested.

### 5.1 News intelligence — bounded Bayesian update

Extraction produces structured records:

```json
{
  "player": "...",
  "status": "doubt",
  "injury": "hamstring",
  "severity": "minor",
  "expected_return": "GW8",
  "training_status": "partial",
  "start_probability_delta": -0.22,
  "source_tier": "HIGH",
  "source_url": "...",
  "extracted_at": "..."
}
```

Rules:

1. The LLM **adjusts a prior** from the minutes model. It never sets `P(start)` directly.
2. Adjustments are **capped in log-odds by source tier** — indicative: HIGH ±0.35,
   MEDIUM ±0.15, LOW ±0.05. Exact caps are tuned against minutes calibration in Phase 7.
3. Every adjustment stores its citation. The UI must be able to show *why* a number moved.
4. Conflicting sources resolve by tier, then recency; never by averaging.

Source tiers: manager press conference / official club / PL injury report / tier-1
journalist = HIGH. Established outlet = MEDIUM. Random social, forums = LOW.

**Honest scoping note:** the FPL API's own `chance_of_playing_next_round` is free and
reasonably good. The LLM layer's job is only the **24-48h of edge** before the official
flag updates. Phase 7 must demonstrate that lift or the layer does not ship.

---

## 6. Optimiser

### 6.1 Receding-horizon MILP

Solve one integer program over GW *t* … *t+5*, with transfers, hits, bench, captain, and
chips all as decision variables. **Execute only week *t*'s decision.** Re-solve next week
with fresh data.

"Save the transfer for the GW7 fixture swing" becomes *solver output*, not a hand-written
heuristic.

Hard constraints: budget · 15 players (2 GK / 5 DEF / 5 MID / 3 FWD) · max 3 per club ·
valid XI formation · bench order · free-transfer accounting and roll cap · chip
once-per-season and per-half availability.

### 6.2 Chips are options, not rules

Chip heuristics ("triple captain on a double gameweek") are rules of thumb, not decisions.
A held wildcard is an **American option** — its value is the value of *waiting*. Chips are
binary variables in the multi-period MILP; wildcard timing is solved by backward induction
over simulated futures.

> **Rules change annually** (chip counts, per-half resets, defensive-contribution scoring,
> assist definitions). All rules are read from live config. **Nothing is hardcoded** —
> including prices, budget, and squad limits.

### 6.3 Transfer hit threshold

A `-4` hit requires roughly **+6 to +8** expected points over the horizon, not +4. The
margin pays for variance and for the lost option value of a banked transfer. This is a
prior for the optimizer's calibration, not a hard rule.

---

## 7. Phase plan and gates

**No phase ships without passing its gate.** UI is last, deliberately.

| Phase | Delivers | Gate |
|---|---|---|
| **0 — Spine** | Ingest (FPL + Understat + archives), bitemporal store, backtest replay skeleton | Can reconstruct any past deadline's exact state, verified against known outcomes |
| **1 — Baselines** | Random / **template** / greedy-form baselines scored over 2+ past seasons | Numbers on the board |
| **2 — Core models** | Team strength, minutes, simple xPts + calibration report | Beat greedy on calibration (Brier, log-loss, RPS) **and pass the reliability requirement in §7.1** |
| **3 — Optimiser** | Single-period MILP: best 15, XI, bench, captain | **Beat the template over 2+ backtested seasons** — the real bar |
| **4 — Multi-period** | Rolling horizon, transfers, hits, what-if engine | Beat Phase 3 |
| **5 — Distributions** | Full PMFs, correlations, EO, rank-aware objective | Better rank distribution at equal or better points |
| **6 — Chips** | Chips in MILP, option-value wildcard timing | Positive chip EV in backtest |
| **7 — News LLM** | Structured extraction, source tiers, bounded updates | Measurable lift in minutes calibration vs. FPL's own flag |
| **8 — UI** | Front-end + narration layer | — |

**v0 = Phases 0-3.** Deliverable is a CLI/notebook that provably beats the template. No UI.

### 7.2 Backtest discipline and baseline definitions

**Decided 2026-08-21, before Phase 1 implementation.**

#### The leakage rule, restated operationally

Every decision made for gameweek *t* reads **only** `as_of(deadline_t)`. Gameweek *t*'s
outcomes are used **only** to score that decision, never to inform it. Any code path where
outcome data flows backwards into selection invalidates the entire backtest, silently.

Ownership deserves specific care: a gameweek's `selected` figure is not fully knowable at
its own deadline. **Template baselines use the previous gameweek's ownership.**

#### Score from stored `total_points`, never re-derived

FPL's scoring rules change every season — defensive contribution did not exist before
2025-26; the Assistant Manager chip existed only in 2025-26; goal values differ by season
and position. `total_points` in the archive was computed **under the rules in force at the
time**.

> **Sum stored `total_points`. Do not recompute points from components.**

Re-deriving would silently apply today's rules to old seasons, inflating or deflating every
historical result in a way no test would catch. This also sidesteps §11's rule-drift problem
entirely for Phase 1.

Captaincy doubles the captain's stored points. Bench order and autosubs are simulated from
stored `minutes`.

#### The three baselines

| Baseline | Rule |
|---|---|
| **Random** | Seeded valid squad at GW1, buy-and-hold, XI by a fixed pre-deadline rule |
| **Template** | Most-owned valid squad, by *previous* gameweek's ownership |
| **Greedy form** | Highest trailing-N-gameweek points, subject to the same constraints |

All are subject to the real constraints: budget from that season's prices, 15 players,
2/5/5/3, max 3 per club, valid XI.

**Report distributions, not just totals.** A baseline's variance is as informative as its
mean — §2's whole argument is that rank is driven by the shape of the outcome, not its
centre.

#### The human reference point

The user's own recorded scores — **2,019 (2025/26), 2,251 (2024/25), 2,169 (2023/24)** —
are a genuine benchmark alongside the synthetic baselines. If the template baseline beats a
real manager's season, that is itself a finding worth knowing before building anything
cleverer.

**Gate for Phase 1: numbers on the board.** Not "the baselines are good" — just real,
reproducible, leak-free totals that Phase 3 must beat.

### 7.1 Calibration before points

Early on, calibration matters more than score. The optimizer *consumes probabilities* — a
well-calibrated, slightly dull model beats a sharp, overconfident one. Tracked from Phase 2:
Brier (clean sheets), log-loss (`P(start)`), RPS (scorelines), reliability diagrams.

**Proper scoring rules alone do not gate this phase — amended 2026-08-29.** Brier, log-loss
and RPS reward calibration *and sharpness* jointly, so beating a baseline on them establishes
that a model is *better*, never that its probabilities are *trustworthy*. The optimiser
consumes them as though they were. The cards model made the gap concrete: its `RED` outcome
scores log-loss 0.0304, Brier 0.0042 and ECE 0.0060 — excellent on every metric the gate
named — while carrying a calibration slope of **−0.081**, meaning it predicts near the base
rate everywhere and its ranking carries no usable signal. The good scores come from the
rarity of red cards, not from skill. **A gate written in proper scoring rules alone would
have passed it.**

Every model therefore reports, **per model and per outcome**:

1. **log-loss, Brier and RPS** against at least two honest baselines (a group base rate and a
   player-trailing rate), walk-forward, fold-internal calibration only.
2. **Reliability: ECE, calibration slope, and intercept.** ECE alone is not sufficient and
   must not arbitrate — measured across the suite it sits at 0.0002–0.0099 for every model
   while slopes are wrong in *both* directions (cards 0.603/0.643 overconfident, bonus
   1.417/2.218 underconfident, minutes 1.114 before its isotonic layer). Low ECE with a
   departed slope is the normal case here, not an anomaly.
3. **An explicit stated decision on any slope departure** — calibrator built, or departure
   accepted with reasoning. Silence is not an option, and "ECE is fine" is not a reason.
4. **A base-rate-only declaration** for any outcome whose slope shows no usable ranking. Such
   an outcome does **not** count as passing, is labelled in the model's own output, and the
   optimiser must not lean on it. `RED` is the first and is declared as such.

This does not lower the bar on the existing metrics; it stops them being read as evidence of
something they never measured.

---

## 8. Anti-patterns — explicitly rejected

- Optimising a single gameweek greedily.
- Scalar xPts as the interface between models and optimiser.
- FPL's official 1–5 FDR as a difficulty input.
- An LLM selecting players or computing points.
- Backtests that read present-day injury/price/ownership data.
- Sideways transfers chasing 0.1m price rises.
- Hardcoded prices, budgets, chip counts, or scoring rules.
- Assuming teammate independence when computing squad variance.

---

## 9. Stack

Python + `uv` · Polars · DuckDB (bitemporal Parquet store) · HiGHS via `highspy` ·
FastAPI · Next.js + Tailwind/shadcn (Phase 8). Everything deterministic and seeded;
every backtest reproducible from a commit hash plus seed.

---

## 10. Rank target — the reasoning

> Operating rules derived from this section (target, aggression, the two tracks, the
> knowledge boundary) live in `AGENTS.md`. This section records *why* they are set that way.

**Manager baseline.** Career average finish 24th percentile; best 19% (2024/25). Consistent
mini-league top-3, but against small casual fields — that is not evidence of edge, and it
should not be read as one.

**The ladder.** Of ~6.04M entries:

| Target | Percentile | Honest read |
|---|---|---|
| **Top 600k** | 10% | Achievable. This is the v1 goal |
| Top 100k | 1.7% | Stretch. Needs the system working *and* a clean injury year |
| Top 10k | 0.17% | Requires luck the system cannot supply. Not a plan |

**Why moderate aggression.** Chasing top-10k from a 24th-percentile baseline means
high-variance differential play, which lands at the 60th percentile far more often than the
1st. The rank-aware objective (§2) exists to *protect* against that failure mode, not to
encourage it. The aggression parameter is a dial, and it is deliberately not set to
maximum.

GW1 deadline: Sat 22 Aug 2026 ~01:30 local (17:30 UTC Fri 21 Aug).

---

## 11. Current-season rules (2026/27)

The operative constraint values, because the optimiser is built against them. The verified
raw read and the endpoints it came from are in
[wiki §1.3](wiki/data-sources.md) — that page is the evidence, this section is the design
input.

Read from `game_config` on 2026-08-19. **Never hardcode these — re-read each season, and
respect the `overrides{rules,scoring,element_types,pick_multiplier}` blocks carried on
chips and events.**

**Squad:** `squad_total_spend: 1000` · 15 players / 11 starting · max 3 per club ·
`transfers_cap: 20` · `max_extra_free_transfers: 4` (a **5 free-transfer bank**).

**Scoring:** `long_play` 2 / `short_play` 1 · goals **GKP 10** / DEF 6 / MID 5 / FWD 4 ·
assists 3 · clean sheet GKP+DEF 4, MID 1 · **`defensive_contribution` DEF 2, MID 2, FWD 2,
GKP 0** · all `mng_*` scoring = 0.

**Chips:** 8 total — two each of wildcard / free hit / bench boost / triple captain, split
GW1-19 and GW20-38. **No Assistant Manager chip.**

**Differences from a 2025/26-era assumption:** no AM chip · GK goals are 10 not 6.

> ~~*defensive contribution now pays forwards*~~ — **CORRECTED 2026-08-22.** Forwards were
> eligible from DC's 2025/26 launch, at the same 12 CBIRT threshold (premierleague.com,
> "What's new for 2025/26", 10 Aug 2025 — Tier HIGH). This was never a 2026/27 change.
> **Consequence: the 2025-26 calibration season already contains forward DC observations
> under the exact 2026/27 rule.** Any scope premised on FWD DC being unobserved is
> unnecessary.

**Known gap:** DC point *thresholds* are absent from the API entirely. They must be
reverse-engineered from `event/{gw}/live/` `explain` blocks after GW1 completes.

**The DC calibration constraint — load-bearing.** FPL's own defensive counters and
third-party per-match counts (tackles, interceptions, clearances, blocks, recoveries) both
exist for **2025-26 and no other season**. Any multi-season backfill from a third-party
provider is therefore a **proxy measured under a different definition**, not the same
quantity. It must be calibrated against 2025-26 before use, and the calibration residual
carried as uncertainty rather than discarded.

> ~~**Recoveries are the hard part:** unobtainable per-player-per-match from every affordable
> source surveyed~~ — **CORRECTED 2026-08-22, and this had been blocking the DC estimator on a
> false premise.** FPL is an Opta customer and publishes `recoveries`, `tackles`,
> `clearances_blocks_interceptions` and `defensive_contribution` **per player per fixture,
> free, in its own API**. Verified in-store by the Architect: all four columns present for
> 2025-26 at **29,757 rows each**. `docs/wiki/data-requirements.md` §P2 had already recorded
> this on 2026-08-21; the blueprint was never updated to match.
>
> The real constraint is **historical depth before 2025-26**, not observability — a smaller
> and different problem. Estimator scope should be set accordingly.

**Grain warning — DC is per FIXTURE, not per gameweek.** Verified: 2025-26 has **419**
`(season, round, element)` triples carrying two rows, i.e. double gameweeks. DC is awarded
**per match**, so aggregating to gameweek before applying the threshold silently corrupts
exactly the DGWs where chips and hauls live — the same class of error that once dropped
7,141 rows (§3.2).

**DC is a threshold statistic, so a rate proxy is the wrong shape.** What pays is
`P(CBIRT >= threshold)`, governed by per-match *dispersion*, not the mean. A
`season rate x team per-match total` construction carries no information about a player's own
dispersion — two midfielders on identical 8.0/90 season rates, one metronomic and one
swinging 3-to-14 on game state, have very different DC yields and it cannot tell them apart.
Components are also positively correlated within a match, so modelling them separately and
summing understates the upper tail where the points are. A pre-2025-26 backfill is therefore
**not recommended**: it buys rows under a different provider's definitions, for a rule that
pays on FPL's counters, with the bias unmeasured except in the one season that does not need
it. Evidence and sourcing: `docs/wiki/defensive-contribution.md`.

**New fields worth exploiting:** `elements` now carries 109 fields including `opta_code`,
`price_change_projections`, `scout_risks`, `birth_date`.

---

## Amendments

| Date | Author | Change |
|---|---|---|
| 2026-08-19 | Architect | Initial ratification. Rank-aware objective, all four data sources, v0 = Phases 0-3, convention-based agent coordination. |
| 2026-08-19 | Architect | Added 3.4 EO acquisition and 3.5 source division of labour. Added 10 operating context. |
| 2026-08-19 | Architect | Post-recon corrections. 3.3 odds downgraded to unsourced (player props unavailable at any tier - original claim was wrong). 3.4 rewritten: historical captaincy backfill proven impossible (entry ids re-issued annually); replaced with forward collection plus a modelled-and-labelled captaincy backcast. 3.5 rewritten: FBref and Understat dropped, API-Football restricted to 2022-24, in-house client mandated. Added 11 current-season rules. |
| 2026-08-21 | Architect | §12.2 specified after story 9 implemented it. "Impossible to query as observed" resolved as physical `derived_` dataset separation with bidirectional store-level enforcement, not a filterable boolean. Input grain = the grain consumed. Residual shape marked provisional pending real DC numbers. Timing provenance separated from value provenance. |
| 2026-08-21 | Architect | §12.5 re-fetchability exception. Unresolved identity on a source that cannot be re-fetched (live-only odds, §3.3) is preserved with `identity_resolved = false` and the raw name, not discarded — the rejected "skip the misses" mode remains rejected for every re-fetchable source. Prompted by story 8 losing 9/10 fixtures of anytime-goalscorer odds to a single unresolvable name. |
| 2026-08-21 | Architect | Understat status refined from flat DROPPED to **dropped-with-a-last-resort-reserve**, on the user's own investigation: Understat exposes no REST API, only unofficial scrapers over a `Disallow: /` site, all broken by the 2026 restructure. It may be reconsidered **only** to close a *historical* gap (2019-20 partial, 2022-23 GW7, 2024-25 olbauday degradation) and **only** after showing vaastav, olbauday and the PL API cannot. Live/current-season use stays rejected outright. Closes retro Q4. |
| 2026-08-22 | Architect | §3.2 corrected: valid time is per ROW, not per batch. `write()` stamps `valid_at` as a scalar across the whole batch, which is right for snapshots and wrong for bulk-ingested archives, leaving `as_of()` correctly-but-uselessly empty at historical deadlines. Three consumers (`backtest.data`, `team_strength`, `minutes`) had each independently hand-rolled `observations()` + a `kickoff_time` filter — a leakage-critical predicate duplicated by hand. Ruling: schemas declare a valid-time column; a separately-named primitive resolves state by valid time; `as_of()` semantics unchanged; no data migration (per-row `valid_at` at write time deferred to a follow-on). |
| 2026-08-22 | Architect | §11 corrected on two load-bearing claims, both escalated by `fpl-elite` and verified in-store before amending. (1) "DC now pays forwards" was never a 2026/27 change — forwards were eligible from the 2025/26 launch, so the calibration season already holds forward DC observations. (2) "Recoveries unobtainable per-player-per-match from every affordable source" was **false and had been blocking the DC estimator on a false premise** — FPL publishes all four DC counters per player per fixture, free; 29,757 rows in-store for 2025-26, already recorded in `data-requirements.md` §P2 but never reflected here. Added the per-fixture grain warning (419 DGW rows in 2025-26) and the threshold-vs-rate argument against a pre-2025-26 backfill. |
| 2026-08-29 | Architect | §7.1 and the Phase 2 gate row amended: proper scoring rules alone no longer gate Phase 2. Brier/log-loss/RPS reward calibration and sharpness jointly, so they establish that a model is better, never that its probabilities are trustworthy — which is precisely what the optimiser consumes. Made concrete by the cards model's `RED` outcome, which scores log-loss 0.0304 / Brier 0.0042 / ECE 0.0060 while carrying a calibration slope of −0.081, i.e. base-rate prediction with no usable ranking; the named gate would have passed it. Every model now reports per-outcome reliability (ECE, slope, intercept) alongside the scoring rules, must state a decision on any slope departure, and must declare base-rate-only any outcome whose slope shows no usable ranking — such an outcome does not count as a pass. ECE explicitly may not arbitrate: it is 0.0002–0.0099 across all six models while slopes are wrong in both directions. Granted authority to amend, 2026-08-29. |
| 2026-08-29 | Architect | §4 model table: the cards row's "Referee assignment is a real feature" struck as **evidenced false for this dataset**. The claim was ratified on 2026-08-19 from football priors, never measured. With `match.officials@match` now ingested for six seasons, it was measured: fitting cards with vs without referee identity moved log-loss by +0.0002 and Brier by −0.0000139 over 219 folds / 64,531 OOS rows. Referee is declared `KNOWN_ABSENT` for deployable use anyway (officials are anchored at kickoff, ~90 min after the FPL deadline, so using it forward would leak), and this measurement removes the case for building an announcement-time capability to make it deployable. The officials ingest still earned its place: it made the measurement possible and flushed out the PL kickoff timezone defect. |
| 2026-08-19 | Architect | Deduplication pass. AGENT_PROTOCOL.md absorbed into AGENTS.md and deleted. Operating rules (rate discipline, two tracks, knowledge boundary) now owned solely by AGENTS.md; 10 keeps only the reasoning. Cross-references added to wiki for evidence. Amendments table moved to end of document. |

---

## 12. Provider abstraction

The ingest layer is **capability-keyed, not provider-keyed**. The core asks for a *fact*;
it never names a vendor. Today's recon produced three lessons that this design exists to
encode.

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

### 12.1 Granularity is part of the capability key

**Lesson: the same logical fact at a different granularity is a different fact.**
Defensive actions exist per-player-per-season (free), per-team-per-match (free), and
per-player-per-match (unobtainable). Flattening these to "defensive actions" invites a
provider that serves one to silently answer for another.

Capabilities are therefore keyed `(entity, measure, grain)` — e.g.
`player.defensive_actions@match` is a *different capability* from
`player.defensive_actions@season`. A provider declares exactly which it serves, and for
which seasons and competitions. **Coverage is declared data, not a code path.**

### 12.2 Derived facts are never observations

The DC estimator — `player season rate x team per-match total` — is a **computed
approximation**, not a measurement. §4 already requires modelled data to be labelled;
the provider layer is where that label is applied or lost.

A **derived capability** is satisfied by computation over other capabilities. Every derived
row carries `is_modelled = true`, the capability keys it consumed, and the calibration
reference used. The store must make it *impossible* to query a derived value as though it
were observed — schema requirement, not convention.

**Specified 2026-08-21, after story 9 implemented this section for the first time.** Three
things above were underspecified, and each was resolved by building it:

**1. "Impossible" means the query path, not a boolean.** A stored `is_modelled` flag is not
sufficient — it relies on every caller remembering to filter, which is the convention this
section rejects. Derived facts are **physically separated into `derived_`-namespaced
datasets**, so a query against an observed dataset cannot return them: they are not in the
directory it reads. `BitemporalStore.write()` enforces the namespace **bidirectionally** —
provenance columns on a non-derived dataset raise, and a derived dataset missing them raises.
Verified adversarially: before the store guard existed, a modelled row could be smuggled into
an observed dataset through the ordinary write path and its column appeared in that dataset's
`as_of()` schema. "The sanctioned path is safe" is not the standard; **"no path is unsafe"**
is.

**2. Inputs are recorded at the grain the derivation actually consumed.** A derivation that
aggregates over an entity key records the **partial** key it consumed, not a fabricated
row-level one. Recording a grain finer than the computation used would misrepresent
re-derivability — the thing the input record exists to guarantee.

**3. The residual's shape is provisional, deliberately.** §11 requires the calibration
residual to be *carried as uncertainty, not discarded*, but does not say in what form. The
current `residual_mean` / `residual_std` pair **assumes a near-Gaussian residual and is a
placeholder**, flagged as such in code and wiki. It gets its real shape from actual 2025-26 DC
calibration numbers. Do not let it harden by default — a skewed or multi-modal residual
carried as a mean and a standard deviation is a lie with error bars.

**Timing provenance is a different axis from value provenance.** `observed_at_imputed` (was
this row's *observation time* inferred?) must not be folded into `is_modelled` (is this row's
*value* computed?). Collapsing them makes one flag mean two things. This was considered and
**deliberately rejected**, not overlooked.

### 12.3 Provenance is mandatory

Every row records: provider id, endpoint, `observed_at`, content hash, and the resolved
capability. Provider **selection and fallback are always logged** — a silent fallback to a
lower-quality source is the failure mode this whole design exists to prevent.

### 12.4 Per-provider policy, not a global rate limit

Providers constrain differently and the abstraction must express all of them:

| Provider | Constraint |
|---|---|
| FPL API | 2 req/s, single connection, no published quota |
| PL API | polite ceiling; §3.6 conditions apply |
| The Odds API | **credit-based** — 500/month, cost = regions x markets per call |
| Archives (vaastav, olbauday) | bulk files, no rate limit, different shape entirely |

Policy expresses requests/sec, daily quota, monthly credits, **and cost-per-call**. The
archive case matters: a provider need not be an HTTP API at all, and the interface must not
assume one.

### 12.5 Identity resolution fails loudly, and is itself bitemporal

**Extended 2026-08-20, after story 6/7 surfaced the gap.**

Resolving a 2025/26 match against the *current* player list correctly raised on four
players who have since left the league. The all-or-nothing failure is **correct behaviour
and must not be weakened** — but it exposes the real requirement:

> **Identity maps are season-scoped.** `resolve(entity, as_of=T)` resolves against the
> identity snapshot valid at T — never against today's.

Using today's squad list to resolve a 2019 fixture is the same class of error as reading
today's prices into a historical context (§3.2). Identity is a fact with a valid time like
any other, and the bitemporal rule applies to it.

Sources by era: **current season** — FPL `bootstrap-static`; **historical** — the vaastav
archives (`data/{season}/players_raw.csv`), which carry per-season player lists, and the PL
API's own per-season squad route.

**Player and team identity are asymmetric, and only one of them broke.** Team codes are
stable across seasons — the same 20-ish codes persist and promoted clubs simply appear — so
resolving a historical match's teams against today's list happens to work. Player identity
does not: squads churn every window. The 2025/26 archive holds **841 players** against
today's **599**.

Do not generalise from "team identity worked" to "identity is fine". The asymmetry is a
property of how the two id spaces evolve, not evidence that as-of resolution is optional.

**A "partial success / skip the misses" batch mode is explicitly rejected.** It converts a
loud, correct failure into silent data loss, which is the exact inversion of this section's
purpose. Story 11 (backfill orchestrator) must fix identity, not tolerate misses.

**The re-fetchability exception — added 2026-08-21, after story 8.** The rule above assumes
the only alternative to raising is silence. For a **re-fetchable** source that assumption
holds: an archive or backfill that raises on an unresolved name costs nothing, because you
fix the identity map and re-run. Nothing is lost and the rule stands unchanged.

A **non-re-fetchable** source breaks the assumption. Bookmaker odds have no historical
endpoint on the free plan (§3.3): a fixture's pre-match prices cease to exist at kickoff and
cannot be bought back at any price. There, raising on one unresolvable name out of forty-two
discards forty-one good observations permanently — which is itself the silent data loss this
section exists to prevent, merely arriving by a louder route. Story 8 lost nine of ten
fixtures this way on its first live capture.

> **Where a source cannot be re-fetched, an unresolved entity is PRESERVED, never
> discarded.** The row is stored with a null canonical id, an explicit
> `identity_resolved = false`, and the provider's raw name string retained verbatim.

This is not the rejected mode. "Skip the misses" is silent and lossy; this is neither. The
failure stays loud in three places — the row is flagged, the name is kept, and the capture
reports every unresolved entity by name. Identity is then repaired **offline, from the
stored string**, with no refetch, which is precisely what the live source cannot offer.

The marker must make unresolved rows **unusable by default**: any consumer that does not
explicitly opt into them must receive nothing, never a null that joins quietly. A null id
that silently participates in a join is the original §12.5 bug wearing a new hat.

**The test is re-fetchability, not convenience.** An adapter may only take this path where
losing the observation is genuinely permanent. Archives, backfills and anything with a
historical endpoint continue to raise.



`opta_code` is the join key where available. Where it is not, a per-provider ID map
resolves to canonical entities. **Unmatched entities raise, they never silently drop.**
A fuzzy name match that quietly mismatches on a transfer or an accented name is exactly the
class of bug that corrupts a model without failing a test.

### 12.6 Swappability is a §3.6 obligation

The PL API is used under conditions that expire if the project's nature changes. **No
provider-specific response shape may leak past its adapter.** Replacing a provider must be
an adapter change and a registry entry — never a change to the core.
