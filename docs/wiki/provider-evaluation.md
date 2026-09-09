# Data Provider Evaluation

> **Status note, added `xl-coder` wiki sweep 2026-08-21.** This is a purchase/build-vs-buy
> *evaluation*, not a build record — its recommendations (below) still stand and have not
> been falsified by later work. But several sources it evaluates only as candidates have
> since actually been built as live adapters: the PL API (§2, this doc's headline finding)
> is `src/fplai/providers/pl.py`, and the vaastav/olbauday archives (§6.2) are
> `src/fplai/providers/vaastav.py` / `olbauday.py` — see `docs/wiki/provider-framework.md`
> for what shipped, was tested, and was live-verified. Read this file for the *why*
> (cost/coverage/licence reasoning); read `provider-framework.md` for the *what's actually
> running*.

> Author: **Data Scout** · Session `s001` · Evaluation date **2026-08-20**
> Evaluated against [`data-requirements.md`](data-requirements.md) P1–P6.
> Every price is **as observed on 2026-08-20** from the cited page. Pricing changes; re-check
> before purchase.
> **[VERIFIED]** = I called the endpoint or read the vendor's own page on this date.
> **[UNVERIFIED]** = could not confirm without an account, a purchase, or a trial. §9 lists all of them.

---

## 0. Headline

**The single most plan-changing finding: P1 does not need to be bought at all.**

The Premier League's own backend — `footballapi.pulselive.com` — is live, unauthenticated,
serves no `robots.txt`, and returns **starting XI + named bench + formation + substitutions
with exact minute + referee** in **one request per fixture**, verified back to **2010/11**,
with season IDs listed back to **1992/93**. It exposes `altIds.opta` in the exact
`pNNNNNN` format of FPL's `elements[].opta_code`. That is P1, P4 (PL only) and the join key,
for £0.

What money must actually buy is **P2 at per-match granularity**, and no affordable provider
covers it completely. The gap is specific and worth stating precisely:

| DC component | Free from FPL | PL API (per match) | PL API (per season) | API-Football | TheStatsAPI |
|---|---|---|---|---|---|
| Tackles | 2025-26 only | team only | **yes, 2006/07→** | **yes** | **yes** |
| Interceptions | in CBI bucket | team only | **yes** | **yes** | **yes** |
| Clearances | in CBI bucket | team only | **yes** | **no** | **yes** |
| Blocks | in CBI bucket | team only | **yes** | **yes** | **no** |
| Recoveries | 2025-26 only | team only | **yes** | **no** | **no** |

No single affordable provider has all five per player per match. The PL API has all five
per player **per season** back to 2006/07 for free. That asymmetry drives the recommendation.

---

## 1. Comparison table

Scoring: **●** full · **◐** partial · **○** none · **?** unverifiable without an account.

| Provider | P1 lineups/subs | P2 DC per match | P3 xG | P4 fixtures | P5 hist. odds | P6 injury history | Seasons | Opta IDs | Bulk export | Price observed |
|---|---|---|---|---|---|---|---|---|---|---|
| **PL Pulselive API** | ● | ○ (◐ per-season) | ○ | ◐ PL only | ○ | ○ | **16+ verified, 35 listed** | **● native** | ○ (1 req/fixture) | **£0** |
| **football-data.co.uk** | ○ | ○ | ○ | ○ | **●** | ○ | 1993/94→ | ○ | **● CSV/season** | **£0** |
| **vaastav archive** | ○ | ◐ 2025-26 only, FPL-native defs | ◐ | ◐ | ○ | ◐ flags only | 2016-17→ | ○ (FPL ids) | **● git clone** | **£0** |
| **API-Football** | ● | ◐ T/I/B, **no C, no R** | ○ | ● all comps | ○ purged | ◐ 2021→ | 2016→ for player stats | ○ | ○ | **$19/mo Pro** ¹ |
| **TheStatsAPI** | ● | ◐ T/I/C, **no B, no R** | ● incl. shotmap | ● | ◐ ? depth | ○ | 10 yrs claimed | ○ | ○ | **$50/mo Starter** |
| **Sportmonks** | ● | **?** | ◐ add-on €24/mo | ● | ◐ add-on €15/mo | ? | 3 incl., older = one-time add-on | ○ (no Opta mention in 893 KB of docs) | ○ | **€29/mo + €29 one-time** |
| **football-data.org** | ● | ○ (scorers/bookings only) | ○ | ● | ◐ €15 add-on | ○ | 10 on ML Pack | ○ | ○ | €29–199/mo |
| **Sportsdata.io** | ◐ | ◐ T/I/Blocks, **no C, no R** | ○ | ● | ? | ? | ? | ○ | ? | not published |
| **StatsBomb open data** | ● | ● event-level | ● | ○ | ○ | ○ | **PL: 2003/04 + 2015/16 only** | ○ | **● git clone** | **£0** |
| **Sportradar** | ● | ● | ● | ● | ● | ● | deep | ○ | ● | quote only ² |
| **Stats Perform / Opta** | ● | ● | ● | ● | ● | ● | deep | **● source of truth** | ● | quote only ² |
| **Hudl StatsBomb / Wyscout** | ● | ● | ● | ● | ○ | ◐ | deep | ○ | ● | quote only ² |
| **FotMob** | — | — | — | — | — | — | — | — | — | **`robots.txt: Disallow: /api/*` — off-limits** |
| **SofaScore** | — | — | — | — | — | — | — | — | — | **403s `/robots.txt` itself — hostile, do not use** |

¹ Price is third-party reported, not read from the vendor page (Cloudflare-blocked). See §9.
² No public pricing exists. Third-party figures range from $1,250/mo to $10,000+/mo and are unverified.

---

## 2. Premier League Pulselive API — **the finding that changes the plan**

**Base:** `https://footballapi.pulselive.com/football/` · **Auth:** none **[VERIFIED]**
**`robots.txt`:** HTTP 404 — none published for this host **[VERIFIED]**
**Edge:** AWS CloudFront, `Cache-Control: max-age=30`. **No rate-limit headers of any kind**
across ~25 probes, all HTTP 200 **[VERIFIED]**

### 2.1 What it provides

| Need | Endpoint | Verified content |
|---|---|---|
| **P1** | `GET /football/fixtures/{id}` | `teamLists[]` → `lineup[11]`, `substitutes[7-9]`, `formation.label` + grid; `events[]` with `type:"S"` / `description:"ON"\|"OFF"` and `clock.label` ("60'00"); `matchOfficials[]` (referee, role `MAIN`); `halfTimeScore`, `attendance`. **One request per fixture.** |
| **P4** | `GET /football/fixtures?comps=1&compSeasons={id}` | Full season list, `kickoff.millis` + `gmtOffset`, `gameweek.gameweek`, `competitionPhase`. PL only — no UCL/UEL/cups |
| **P2 (per season)** | `GET /football/stats/player/{id}?comps=1&compSeasons={id}` | **132 raw Opta stat names in one call**, including `total_tackle`, `won_tackle`, `interception`, `interception_won`, `total_clearance`, `effective_clearance`, `outfielder_block`, `blocked_scoring_att`, `six_yard_block`, `ball_recovery`, `poss_won_def_3rd`, `mins_played`, `appearances` |
| **P2 (leaderboard)** | `GET /football/stats/ranked/players/{stat}?compSeasons={id}&pageSize=100` | ~478–482 qualifying players per season. Verified for `effective_clearance` on **2006/07** and **2015/16** |
| Join key | `GET /football/players?compSeasons={id}&altIds=true&pageSize=100` | `altIds: {"opta":"p427637"}` per player, 1218 entries for 2025/26 |
| Team stats | `GET /football/stats/match/{id}` | 134 Opta stat names, **team-level per match** |

### 2.2 Seasons

`GET /football/competitions/1/compseasons?pageSize=50` returns **35 seasons, 1992/93 → 2026/27**
**[VERIFIED]**. Lineups verified working on **2010/11** (compSeason `19`, fixture `7092`:
11 starters, 7 subs, substitution events, referee) and **2015/16** (compSeason `42`).
`formation.label` is `null` on the 2010/11 fixture — formation appears to start later, XI and
bench do not. **[VERIFIED]**

History is **not** gated behind anything. There is no tier.

### 2.3 Opta lineage — **native**

`&altIds=true` returns Opta identifiers throughout:

- players `{"opta":"p427637"}` — identical format to FPL `elements[].opta_code` (Haaland `p223094`) **[VERIFIED]**
- teams `{"opta":"t14"}` · matches `{"opta":"g2561895"}` **[VERIFIED]**

**This dissolves the ID-mapping problem for the whole historical spine.** No fuzzy matching,
no override list, no silent mismatches on loans and accented names. Note the separate
`playerId` field (e.g. `258394`) is *not* the Opta id — use `altIds.opta`.

### 2.4 Rate limits and quota

No published limits, no headers, no auth, no quota. ~25 requests over this session, all
clean. **Do not probe further.** Recommendation: **2 req/s, single connection, ±20% jitter**,
identical discipline to the FPL API. CloudFront fronts it, so a backfill is cheap for them
to serve.

### 2.5 Bulk export

**None.** Per-fixture and per-player only. Concrete cost of a 10-season backfill at 2 req/s:

| Job | Requests | Wall clock |
|---|---|---|
| Fixture lists, 10 seasons | ~40 (paged) | <1 min |
| **P1: all fixtures 2016/17–2025/26** | **3,800** | **~32 min** |
| Opta id map, 10 seasons @ 100/page | ~130 | ~1 min |
| P2 per-season player stats (~500 players × 10) | ~5,000 | ~42 min |

**~9,000 requests, under 90 minutes total, one time.** Bulk export is not needed at this
volume — this is the one case where per-entity endpoints are fine.

### 2.6 Reliability risk

**Medium-high.** This is an undocumented internal backend, not a product. There is a newer
host (`sdp-prem-prod.premier-league-prod.pulselive.com/api/v1`) already serving the redesigned
site, which means the legacy host is on a deprecation path even though it works today. Cache
every raw response permanently at ingest; a re-fetch may not be possible later. The bitemporal
store already requires this.

### 2.7 Licence — **escalated, user decision**

`footballapi.pulselive.com` publishes no terms. The site it backs does:

> "The Website and App must not be used in any other way, including for commercial purposes"
> "you may not otherwise reproduce, re-utilise or redistribute it (including, by way of example,
> **creating a database (electronic or otherwise) that includes material downloaded or otherwise
> obtained from the Website or App**)"
> "You may download and print material from the Website or App as is reasonable for your own
> **private and personal use**"

Our use is private, non-commercial and never republished, which sits inside the personal-use
carve-out; the database clause is nonetheless explicit and names exactly what we would build.
**This is the user's call, not mine.** It is the single legal question in this document.

---

## 3. API-Football (paid tiers)

**Base:** `https://v3.football.api-sports.io` · **Auth:** `x-apisports-key`

### 3.1 Coverage — verified live on the existing free key

`GET /fixtures/players?fixture=1208021` (2024/25, Man Utd v Fulham) returned **all 40 players
in one request** **[VERIFIED]**:

```
games   {minutes, number, position, rating, captain, substitute}
tackles {total, blocks, interceptions}      duels {total, won}
shots · goals{total,conceded,assists,saves} · passes · dribbles · fouls · cards · penalty
```

Populated for outfielders (e.g. Casemiro: `tackles 4, blocks 1, interceptions 1, duels 13/7`).
**Confirmed absent: clearances and ball recoveries.** FPL builds `defensive_contribution` from
CBIT (clearances + blocks + interceptions + tackles) for DEF and CBIRT (+ recoveries) for
MID/FWD. **API-Football alone cannot reconstruct DC.**

### 3.2 Seasons, and where history is gated

`GET /leagues?id=39` returns 17 seasons (2010–2026) with per-season `coverage` flags **[VERIFIED]**:

| Season | lineups | statistics_players | odds | injuries |
|---|---|---|---|---|
| 2010, 2016 | true / true | **false** (2010) / **true** (2016) | false | false |
| 2019, 2021, 2022, 2025 | true | true | **false** | false / true |
| 2026 | false* | false* | true | true |

*2026 flags are false because the season has not kicked off; they flip once data exists.

Two hard conclusions:
- **Per-player match statistics exist from season 2016 onward.** 2010–2015 have lineups but no player stats.
- **`odds` is `false` for every past season and `true` only for 2026.** Historical odds are purged. **P5 is unobtainable from API-Football at any tier** — this re-confirms the earlier finding with the vendor's own coverage metadata rather than an empty result set.

Free tier remains hard-locked to 2022–2024 (unchanged). Whether a paid tier unlocks 2016–2021
and 2026 is **[UNVERIFIED]** — the coverage object implies it, but only a purchase proves it.

### 3.3 Player ID lineage

Proprietary integer IDs (`Casemiro = 747`). **Not Opta.** Budget a mapping layer, or map once
against the PL API's Opta table using name + DOB and store it as a versioned artefact.

### 3.4 Rate limits **[VERIFIED from response headers on the free key]**

`x-ratelimit-limit: 10` (per minute) · `x-ratelimit-requests-limit: 100` (per day).
Paid quotas as reported by third parties: Pro 7,500/day at 300/min, Ultra 75,000/day at
450/min, Mega 150,000/day at 900/min. **[UNVERIFIED]** — see §9.

### 3.5 Bulk export

**None.** But the backfill is small: 10 seasons × 380 fixtures = **3,800 `/fixtures/players`
calls**, which fits inside a single day of a Pro plan.

### 3.6 Price

**$19/month Pro** as reported by search results and a RapidAPI listing. `www.api-football.com/pricing`
is Cloudflare-protected and returned HTTP 403 to both a plain client and WebFetch, so the
number is **[UNVERIFIED]** against the vendor's own page.

### 3.7 Licence

Not read (page behind Cloudflare). **[UNVERIFIED]**

---

## 4. TheStatsAPI

**Base:** `https://api.thestatsapi.com/api/football/` · Bearer token.

### 4.1 Coverage — read from the vendor's own published response contract **[VERIFIED from docs, not from a live call]**

`GET /football/matches/{match_id}/player-stats` returns per player:

```
rating · minutes_played · started · played
passing{total, accurate, key_passes, assists, crosses, long_balls}
shooting{goals, total_shots, on/off target, blocked_shots, big_chances_created,
         expected_goals, expected_assists, np_expected_goals}
duels{duel_won, duel_lost, aerial_won, challenge_lost, won_contest, dispossessed}
defending{tackles, interceptions, clearances}
general{touches, fouls, possession_lost, cards, player_subbed_on, player_subbed_off}
```

- **P2:** tackles + interceptions + **clearances** — the component API-Football lacks. **No blocks
  made** (`blocked_shots` sits under `shooting` and is shots *of* that player that were blocked,
  not blocks *by* the player). **No recoveries.**
- **P3:** `expected_goals`, `np_expected_goals`, `expected_assists` **per player per match**, plus
  a separate `/shotmap` endpoint. This is the cleanest P3 offer found at any price point.
- **P1:** `/lineups`, `/timeline`, `started`, `player_subbed_on/off`, `/referee`.
- **P5:** `/matches/{id}/odds` returns Pinnacle, Bet365, Paddy Power, Betfair Sportsbook, Kambi
  across 1X2, BTTS, totals, corners and Asian handicap with **`opening` and `last_seen`** values;
  `/player-odds` also exists. The doc says "available for finished matches **where odds were
  captured**" — depth unknown. **[UNVERIFIED]**

### 4.2 Seasons

"10 years of historical match data", "10 years for major leagues. The depth varies by competition."
Whether that 10 years includes *player* stats and odds, or only match results, is **[UNVERIFIED]**.
History is **not** gated to a higher tier — all data is on every plan.

### 4.3 Player ID lineage

Proprietary stable strings: `pl_29627593`, `tm_0406`, `mt_838955483`, `ref_7264609`. **No Opta
mapping documented.** Full fuzzy-matching layer required.

### 4.4 Rate limits

Starter **100,000 req/month, 120 req/min**. Growth 500,000/mo, 300/min. Scale 5,000,000/mo,
1,000/min. **[VERIFIED from the pricing page]**

### 4.5 Bulk export

None documented. 10 seasons of player-stats + shotmaps = ~7,600 calls, ~3% of one month's
Starter quota. Bulk is not needed.

### 4.6 Price

**Starter $50/month** · Growth $129 · Scale $379. USD, GBP and EUR toggles offered.
7-day free trial on all plans. Observed on `thestatsapi.com` 2026-08-20.

### 4.7 Reliability risk — **high**

Unknown vendor with no track record I can verify, marketing that reads as AI-assisted, and a
comparison blog on its own domain that ranks itself first. The data contract is specific and
credible; the company is not yet. **Do not build a dependency on it without running the 7-day
trial against real Premier League fixtures first.**

### 4.8 Licence

Not located. **[UNVERIFIED]**

---

## 5. Sportmonks

### 5.1 Coverage

Per-player per-fixture statistics **do** exist — the docs show `include=lineups.details.type`
with a `lineupdetailTypes` filter, i.e. a LineupDetail entity carrying `type_id` + `value` per
player per fixture. **Which statistic types are available is not published.** The type list
lives behind `GET /v3/my/filters/entity?api_token=...`, which needs a key. I searched their
full 893 KB documentation corpus (`docs.sportmonks.com/v3/llms-full.txt`): "Interceptions" 0
hits, "Clearances" 0, "Duels" 0, "Recover" 0, "Tackles" 1.

**I cannot confirm P2 coverage without an account. Per my brief, I stop here.** A 14-day free
trial would settle it in ten minutes.

Separately, the `Statistic` entity docs state player statistics are **season-level aggregates**
and `FixtureStatistic` is **team-level** — so lineup details are the only per-player-per-match
route, which makes the unverified type list the whole question.

### 5.2 Seasons — **this is the trap the spec warned about, and it is cheap**

> "Historical data older than three seasons is available as a one-time add-on for Starter,
> Growth, and Pro plans. Enterprise plans include full historical access."

Add-on **from €29 one-time**. Three seasons included is below our 3-season floor for confident
fitting, so the add-on is mandatory, not optional. How many seasons €29 actually buys is
**[UNVERIFIED]**.

### 5.3 Player ID lineage

**Zero occurrences of "Opta" in 893 KB of official documentation.** Sportmonks operates its own
collection. Treat as not Opta-keyed.

### 5.4 Rate limits

**Per-entity, not per-endpoint**, which is unusually generous: Starter 2,000 calls/**entity**/hour,
Growth 2,500, Pro 3,000, Enterprise 5,000. The hour window starts at your first request to that
entity. **No daily or monthly cap documented.** A 3,800-fixture backfill against the Fixture
entity is ~2 hours of clock. **[VERIFIED from docs]**

### 5.5 Bulk export

None documented.

### 5.6 Price (observed 2026-08-20, `sportmonks.com/football-api/plans-pricing/`)

| Plan | Monthly | Annual | Leagues |
|---|---|---|---|
| Starter | €29 | €24/mo | any 5 |
| Growth | €99 | €79/mo | any 30 |
| Pro | €249 | €199/mo | any 120 |

Add-ons: **xG & Pressure Index €24/mo** · Odds & Predictions €15/mo · Premium Odds €129/mo ·
historical add-on from €29 one-time.

**Realistic configuration for us: Starter €29/mo + historical €29 one-time + xG €24/mo ≈ €53/mo
plus €29 up front** — and P2 still unconfirmed.

### 5.7 Licence

Not read. **[UNVERIFIED]**

---

## 6. Free sources that close whole requirements

### 6.1 football-data.co.uk — **P5 solved, for free** **[VERIFIED]**

Earlier recon recorded this host as unreachable. **That was transient or local.** It responds
now: `https://www.football-data.co.uk/mmz4281/{YY}{YY}/E0.csv`, HTTP 200, ~176 KB per season,
verified for **2025/26**, **2020/21** and **1993/94**.

The 2025/26 header carries, per match:

- **Opening and closing odds** — `B365H/D/A` … `B365CH/CD/CA`, plus Pinnacle (`PS*`/`PSC*`),
  Betfair Exchange (`BFE*`), BetVictor, Bet365, BoyleSports, Marathon, and market
  `Max*` / `Avg*` aggregates
- **Over/under 2.5** opening and closing, market max and average
- **Asian handicap** with the line (`AHh`, `AHCh`) and per-book prices
- Plus, as a bonus: `Referee`, shots, shots on target, corners, yellows and reds per match

Bulk CSV, no auth, no key, no quota, back to 1993/94. Closing odds are exactly what a market
baseline needs. **No anytime-goalscorer market** — that remains live-only via The Odds API.

**P5 costs zero. Remove it from the shopping list.**

### 6.2 vaastav archive — the only FPL-native P2, and only one season **[VERIFIED]**

`data/2025-26/gws/gw1.csv` header includes **`tackles`, `recoveries`,
`clearances_blocks_interceptions`, `defensive_contribution`** per player per gameweek.
`data/2024-25/gws/gw1.csv` does **not** — FPL had not yet added the fields.

This matters more than its size suggests. These are FPL's **own counters under FPL's own
definitions**, which is what the scoring rule actually pays on. Every third-party tackle or
clearance count is a *proxy* with a different definition. You get exactly **one** historical
season of ground truth (2025-26) plus the live season.

**Use it as the calibration set.** 2025-26 is the one season where FPL's counters and any
third-party per-match counts both exist. Fit the proxy-to-FPL mapping there before trusting a
10-season third-party backfill. Without that step the backfill is uncalibrated and the DC model
inherits an unmeasured definitional bias.

### 6.3 StatsBomb open data — **not viable for us** **[VERIFIED]**

`competitions.json` lists Premier League seasons **2003/04 and 2015/16 only**. Event-level and
therefore perfect in quality, but two disconnected seasons with almost no current-player overlap.
Everything else (La Liga 2004–2020, UCL, World Cups, WSL) is irrelevant to this project.
Licence requires attribution and logo use on published analysis. Not applicable — we publish
nothing.

---

## 7. Rejected and dead

| Source | Status | Evidence, this date |
|---|---|---|
| **SofaScore** | **Dead — hostile** | `GET /robots.txt` with a normal browser UA returns `{"error":{"code":403}}`. It 403s the file that would tell us what is permitted. Treat as refusal. |
| **FotMob** | **Dead — robots.txt** | `Disallow: /api/*` for `User-agent: *`, allowed only for Googlebot, Bingbot, Qwantbot, AmazonAdBot. Its JSON API is off-limits under our standing rule. |
| **football-data.org** | **Not worth buying** | Its "player stats" are goal scorers and bookings only — nowhere near P2. Lineups start at €29/mo, and the PL API gives better lineups for free. `Statistics` add-on €15/mo, contents unverified. |
| **Sportsdata.io** | **Partial, unpriced** | Data dictionary confirms `Tackles`, `TacklesWon`, `Interceptions`, `BlockedShots`, `Minutes`, `Started`. **Confirms absence of Clearances, recoveries and any xG.** Strictly worse than API-Football on P2 and worse than TheStatsAPI on P3, with no published price. |
| **Sportradar · Stats Perform/Opta · Hudl StatsBomb · Wyscout** | **Out of reach** | No published pricing on any of them; all route to a sales call and normally to a commercial entity and an annual contract. Third-party figures for Sportradar range $1,250/mo to $10,000+/mo — **[UNVERIFIED]**, and either number is disproportionate here. Stats Perform is the true source of the Opta data we already get free from the PL API, which is the point. |
| **FBref · Understat** | Unchanged | Cloudflare 403 · `robots.txt: Disallow: /`. Not re-litigated. |

---

## 8. The three questions, answered

### 8.1 Does any single provider cover both P1 and P2 with 5+ seasons of history?

**No — not at any price a private individual would pay.**

- **P1 alone:** yes, several, and the free PL API is the best of them (16+ seasons, Opta-keyed, one request per fixture).
- **P2 complete** (tackles + interceptions + clearances + blocks + recoveries, per player per match, 5+ seasons): **only the enterprise Opta/Sportradar/StatsBomb feeds**, all quote-only, all four to five figures, all effectively requiring a company.
- **P2 partial:** API-Football has T/I/B and is missing C and R. TheStatsAPI has T/I/C and is missing B and R. Sportmonks is unknown.
- **Recoveries per player per match are unobtainable from every affordable source.** That is the single hardest field in this document, and it is a component of DC for MID and FWD.

### 8.2 Is there a free or cheap route to P2 historically?

**Cheap and partial, yes. Free and complete, no. And that gap is the moat.**

Three routes, in order of value:

1. **PL API per-player-per-season stats — free, 2006/07→, Opta-keyed, has all five components
   including recoveries.** Season granularity, not match. Enough to build a per-90
   DC-component rate prior per player-season, which combined with the per-match minutes from
   P1 supports a hierarchical DC model. **This is the best free option and nobody in the public
   FPL ecosystem is using it.**
2. **vaastav 2025-26** — per-gameweek, FPL's own definitions, one season. The calibration set.
3. **API-Football or TheStatsAPI at ~$19–50 for one month** — per-match, 10 seasons, but each
   missing two of the five components.

The fact that per-match defensive actions with FPL-compatible definitions are genuinely hard
to obtain is precisely why DC is worth modelling. The barrier is real, not imagined, and it
applies to everyone else too.

### 8.3 What is the cheapest configuration that unblocks P1 + P2?

**Two cheap ones, and most of it is free.**

| Component | Source | Cost |
|---|---|---|
| P1 lineups, subs, formations, referees, 10 seasons | PL Pulselive API | **£0** |
| Opta ↔ FPL join key | PL API `altIds` + `elements[].opta_code` | **£0** |
| P2 per-player-per-season, all 5 components, 2006/07→ | PL API `stats/player` | **£0** |
| P2 ground truth, FPL definitions, 2025-26 | vaastav | **£0** |
| P5 historical odds, 1993/94→, opening + closing | football-data.co.uk | **£0** |
| **P2 per-match, 10 seasons (T/I/B)** | **API-Football Pro, one month, then cancel** | **~$19 once** |

**Total: about $19, one time.** 3,800 `/fixtures/players` calls fit inside a single day of
Pro quota; back-fill, verify, cancel. Nothing recurring.

Add TheStatsAPI Starter for one month (**+$50**) if you want clearances per match *and*
per-player-per-match xG. That second month's-worth is bought mainly for **P3**, not P2 — it is
the cheapest per-player xG with history I found anywhere, and P3 is otherwise a hole before
2024-25.

---

## 9. Recommendation

### Primary: **spend nothing on a subscription. Buy one month of API-Football Pro (~$19) and stop.**

Build the historical spine on the Premier League's own API. It is free, Opta-keyed, deeper than
any paid tier evaluated, and it solves P1 outright — the requirement the spec calls "the single
biggest gap" and "the highest-ROI model in the system". Nothing on the market improves on it
for our use, and the paid alternatives are strictly worse on the join key.

Layer the ~$19 of API-Football per-match defensive actions on top for the per-match DC
component, calibrated against the 2025-26 FPL ground truth.

**Why this and not a subscription:** every paid provider evaluated is *worse* than free on P1
(no Opta IDs, shallower history, more requests per fixture) and *incomplete* on P2. Paying a
recurring fee buys convenience and a support contact, not capability. The one capability money
can buy — complete per-match defensive actions — is only sold at enterprise prices.

### Runner-up: **TheStatsAPI Starter, $50/month, run the 7-day trial first.**

Take this instead if — and only if — **P3 is promoted in priority**. It is the only affordable
source found with `expected_goals` / `np_expected_goals` / `expected_assists` **per player per
match** plus a shotmap endpoint and 10 years of history, and it throws in clearances and
opening/closing odds. That is P2-partial, P3, P4 and P5 from one vendor at 120 req/min.

**Why it is the runner-up and not the pick:** unverified vendor with no track record, no Opta
IDs (full fuzzy-matching layer required), no blocks, no recoveries, and unconfirmed odds depth.
It is a bet on a young company. The 7-day trial costs nothing and would resolve most of §10.

### Explicitly not recommended

- **Sportmonks** — €29/mo + €29 one-time + €24/mo for xG, with P2 unverifiable without signing
  up, no Opta lineage, and history gated behind an add-on. It may well be fine; it is not
  demonstrably better than a $19 one-off, and it is the only candidate where the core question
  cannot be answered from public material.
- **Anything enterprise** — Sportradar, Stats Perform, Hudl, Wyscout. Disproportionate cost, and
  the Opta data they sell is already reaching us free through the Premier League's own API.

### The one thing to decide before any of this

**§2.7.** The Premier League Terms of Use prohibit commercial use and explicitly name
"creating a database … that includes material downloaded or otherwise obtained from the
Website" while permitting download for "private and personal use". The entire primary
recommendation rests on that host. **That is the user's decision.** If the answer is no, the
fallback is API-Football Pro for P1 as well (lineups back to 2010, player stats from 2016),
which costs the same $19 but loses the Opta join key and the free season-level recoveries — a
materially worse position, not a fatal one.

---

## 10. Could NOT verify

| Item | Why | How to resolve |
|---|---|---|
| **Sportmonks per-player-per-match statistic types** | The type list is behind `GET /v3/my/filters/entity?api_token=…`. Their public docs never enumerate it; a full-corpus search found 1 hit for "Tackles" and 0 for interceptions, clearances, duels, recoveries. **The core question about this vendor is unanswerable from public material.** | 14-day free trial. Call the filters endpoint. Ten minutes. **Requires signup — I may not create accounts.** |
| **How many seasons Sportmonks' €29 historical add-on actually buys** | Pricing page says "older than three seasons … one-time add-on … starting at €29". "Starting at" is doing unknown work. | Ask their sales/support, or check during the trial. |
| **API-Football's real pricing** | `www.api-football.com/pricing` returns **HTTP 403 (Cloudflare)** to both a plain client and WebFetch. The RapidAPI mirror returned an empty shell. $19/$29/$39 and the 300/450/900 req/min figures are **third-party reported only**. | Load the page in a real browser. |
| **Whether an API-Football paid tier actually unlocks 2016–2021 and 2026** | The `coverage` object lists them and the free-tier error says "try from 2022 to 2024", which implies gating by plan — but only a purchase proves it. This is the one assumption the $19 recommendation rests on. | Buy one month and immediately call `/fixtures?league=39&season=2019`. If it errors, refund/cancel. |
| **API-Football and TheStatsAPI licence terms** | API-Football's site is Cloudflare-blocked; TheStatsAPI's terms page was not located. Neither confirms local storage and derived modelling are permitted. | Read before purchase. |
| **TheStatsAPI's actual data quality, and whether "10 years" covers player stats and odds or only results** | Everything reported is from the vendor's own published response contract. I made **no live calls** — the API requires a Bearer token. `/odds` says "where odds were captured", which is unbounded. | 7-day free trial. Pull three Premier League fixtures from 2016/17, 2020/21 and 2025/26 and diff against the PL API. |
| **Whether a per-player-per-match route exists anywhere on the PL API** | Searched both hosts. Legacy: `stats/match/{id}` is team-level, `stats/player/{id}` is season-level. New SDP host: `/matches/{opta}/player-stats`, `/players`, `/players/{id}/stats` all return HTTP 400 "No configuration found"; `?type=player` on `/stats` is ignored and returns team stats. **This is a confirmed negative but not an exhaustive one** — the redesigned match centre renders client-side from a widget bundle I did not fully unpack. | Open a PL match centre in a browser with devtools and read the network tab. If a per-player route exists, P2 becomes free and the recommendation drops to $0. **Worth 15 minutes.** |
| **Exact DC thresholds for 2026/27** | Absent from `bootstrap-static` entirely. Unchanged from earlier recon. | Reverse-engineer from `event/{gw}/live/` `explain` blocks after GW1. |
| **PL API rate ceiling** | Deliberately not probed. ~25 requests, all clean, no headers of any kind. The 2 req/s recommendation is conservative by design and is **not** a measured budget. | Never. Do not probe it. |
| **Sportradar / Stats Perform / Hudl / Wyscout pricing** | None publish any figure. Every route is a sales call, and I may not create accounts or enter into contact flows. Third-party figures ($1,250–$10,000+/mo) are hearsay. | User contacts sales, if ever. |
| **SoccersAPI pricing** | Pricing page rendered client-side; WebFetch retrieved only navigation chrome. Not pursued further — nothing about it looked likely to beat the free spine. | Browser. |

---

## Architect verification — 2026-08-19 (browser, live)

Ran the 15-minute devtools check Data Scout flagged. Findings supersede the "could not
unpack the widget bundle" note above.

**A second, newer platform exists:** `https://sdp-prem-prod.premier-league-prod.pulselive.com/api`
— distinct from the `footballapi.pulselive.com` host in the main report. The match centre is
server-rendered, so nothing shows in the network tab; the routes were extracted from
`bundle-es.min.js`. Full route list (55) captured below in abbreviated form.

### Verified live (HTTP 200, unauthenticated)

| Route | Returns | Use |
|---|---|---|
| `/v3/matches/{id}/lineups` | Starting XI, bench, substitutions | **P1** |
| `/v3/matches/{id}/stats` | Team-level stats per side — incl. `ballRecovery`, tackles, clearances | Team strength |
| `/v2/competitions/{c}/seasons/{s}/players/{p}/stats` | **Per-player season aggregates** — `expectedAssists`, `blockedShots`, `duelsLost`, shots | Partial **P3** |
| `/v3/competitions/{c}/seasons/{s}/players/stats/leaderboard` | Bulk per-season player stats | Backfill in few requests |
| `/v1/competitions/{c}/seasons/{s}/players/{p}/matches/all` | Player's fixture list — **appearances only, no stats** | Appearance history |
| `/v1/matches/{id}/timeline`, `/events`, `/officials` | Match events, referee | P1 / cards |
| `/v1/players/{id}/career` | Career across competitions | Context |

Competition id `8` = Premier League. Season id is the starting year (`2025` = 2025/26).

### P2 verdict: NEGATIVE — confirmed, not assumed

**Per-player, per-match defensive actions are not exposed on this platform's REST surface.**
Checked directly:

- `/v3/matches/{id}/lineups` — zero hits for tackle / interception / clearance / recovery /
  touches / minutesPlayed. XI and substitutions only.
- `/v1/.../players/{p}/matches/all` — fixture list, same zero hits.
- `/v3/matches/{id}/stats` — defensive counts present but **per side, not per player**.

Defensive actions exist per-player only at **season** granularity. That confirms Data Scout's
recommendation: the paid month for `/fixtures/players` remains the route to per-match P2.

### Not probed — deliberate

`/v3/graphql` is present in the route list and may expose more than REST. **Not probed** —
introspecting an undocumented GraphQL endpoint goes beyond reading a public page, and the
Terms of Use question below is unresolved. Revisit only if that question resolves in favour.

### Consequence for the recommendation

Unchanged in shape, better in detail: P1 is free and richer than thought, partial P3 (season
-level xA and shot data, historical) is also free, and the paid month is needed only for P2
per-match granularity.

---

## Architect — footballdata.io verdict + a material free-data discovery (2026-08-19)

### footballdata.io: NO. Wrong shape, not wrong price.

From their **complete public endpoint reference** (not marketing copy):

| Player data offered | Granularity |
|---|---|
| `/players/{id}/stats` | **"Player season statistics"** — season only |
| `/matches/{id}/stats` | Match statistics — team level |
| `/matches/{id}/events` | Match events |

**There is no per-player-per-match endpoint.** The only player statistics are season
aggregates — exactly what the PL API already gives us free.

> **CORRECTION (Data Scout, same day):** an earlier version of this section said "no lineups
> endpoint at all". Wrong. Lineups exist, embedded in `/matches/{match_id}` rather than on a
> dedicated route, and the `bench` block includes substitution minute and replaced player.
> The P2 verdict is unchanged; the lineups claim was not.

Confirmed from their **published OpenAPI 3.0.3 spec** (`/openapi.json`, unauthenticated,
47 paths, 184KB) — across the vendor's entire surface: `tackle` 0 · `interception` 0 ·
`clearance` 0 · `recover` 0 · `duel` 0 · `expected_assist` 0 · `npxg` 0 · `opta` 0.
A published spec covering every route means no post-signup probing can reveal fields that
have no endpoint. **Hard negative, high confidence.**

**The decisive commercial fact:** the spec's `x-plan` annotations show **all four player
endpoints are on the FREE tier**, and **the Premier League itself is on the Free plan**.
Paid tiers unlock only betting derivatives — predictions, BTTS, corners, FIFA rankings —
which blueprint §1/§5 forbid us from consuming. **$49/mo buys nothing we would use.**

The vendor says so themselves on `/footballdata-io-vs-api-football/`: *"Player data —
Footballdata.io: Limited / check docs. API-FOOTBALL: Available."*

What it *is* good at: `/matches/{id}/odds`, `/probabilities`, `/predictions`, `/btts`,
`/corners`, FIFA rankings. It is a **fixtures + odds + predictions API**, not a granular
stats API. Two reasons that is the wrong purchase for us:

1. Its odds are a reseller feed; we already have The Odds API free with **42 books including
   Pinnacle and Betfair Exchange** — the two sharpest markets available.
2. "Prediction-ready endpoints" is an **anti-feature** here. Blueprint §1 and §5: we build
   the model. Buying someone's predictions is the thing this project exists not to do.

Pro at $49/mo would buy volume and predictions we do not need, and still leave P2 unsolved.

### Material discovery: `/v3/matches/{id}/stats` carries 185 keys per side

Verified live. This **closes the P3 hole** and strengthens the P2 fallback.

**Expected goals, per match, per side — free, historical:**
`expectedGoals` · `expectedGoalsOnTarget` · `expectedAssists` · `expectedGoalsOnTargetConceded`

That is precisely the input the Dixon-Coles team-strength model needs (§4). P3 was recorded
as "nothing free fills it pre-2024-25". **At team level, that is no longer true.**

**Every defensive-contribution component, per match, per side — free:**
`ballRecovery` · `totalTackle` · `wonTackle` · `interception` · `interceptionWon` ·
`totalClearance` · `effectiveClearance` · `headClearance` · `outfielderBlock` ·
`blockedPass` · `blockedScoringAtt` · `blockedCross` · `duelWon` / `duelLost`

Including **recoveries** — the field no affordable vendor sells per-match.

### What this does to the buy recommendation

Still no free source of *per-player* per-match defensive actions. But the fallback is now
much stronger than "season rate prior":

> **player season rate × team per-match total** — both free, with the team total acting as a
> real per-match denominator rather than an assumption. Calibrate on 2025-26, the one season
> where FPL's own DC counts also exist (§11).

**Recommendation: defer the $19 API-Football month.** It buys per-player-per-match
granularity we cannot consume until Phase 2, its granular event quality is questioned in
community reports, and it will cost the same $19 later — by which point we will know exactly
which fields the DC model needs and can test them against a real requirement instead of a
guess. Nothing needs to be bought today.

---

## Addendum — footballdata.io, full verification — Data Scout · 2026-08-20

Follow-up evaluation of **footballdata.io Pro, $49/mo**. **The Architect's verdict above is
correct and this section confirms it from stronger evidence** — the vendor's own
machine-readable OpenAPI 3 spec rather than the HTML endpoint reference. It adds the tier
analysis, licence, reseller and data-quality findings, and corrects one factual detail.

Everything below is **[VERIFIED]** from vendor pages and spec on this date unless marked.
No account created, no payment details entered.

### A.0 Verdict

**footballdata.io does not solve P2 at any tier, including Enterprise.** No
per-player-per-match statistics endpoint exists, and the words *tackle*, *interception*,
*clearance*, *recovery* and *duel* appear **zero times** in its entire published API surface.
**Do not buy Pro. Do not buy Starter.** Confidence: **high** — this is a structural negative,
not a documentation gap.

### A.1 Decisive evidence: the vendor's own OpenAPI 3 spec

Changelog v1.8.0 (2026-06-26) announces a machine-readable spec. It is public and
unauthenticated:

`GET https://footballdata.io/openapi.json` → **HTTP 200 · 184,205 bytes · OpenAPI 3.0.3 · 47 paths**

Keyword counts across the **whole 184 KB spec**:

| Term | Occurrences |
|---|---|
| `tackle` · `interception` · `clearance` · `recover` · `duel` | **0 · 0 · 0 · 0 · 0** |
| `expected_assist` · `npxg` | **0 · 0** |
| `opta` | **0** |
| `xg` | 27 — every one team-level or pre-match |

Corroborated across the 13 `/documentation/*` pages with identical results. The homepage
contains **zero** occurrences of the word "player".

### A.2 The spec's `x-plan` annotations settle the tier question

Every operation is annotated with the plan required. This is the precise answer the brief
asked for.

| Plan | Endpoints |
|---|---|
| **free** | `/players`, `/players/{id}`, **`/players/{id}/stats`**, `/teams/{id}/players`, `/teams/{id}/stats`, `/matches`, **`/matches/{id}`**, `/matches/{id}/events`, `/matches/{id}/stats`, `/matches/{id}/odds`, `/matches/{id}/probabilities`, `/matches/date/{date}`, all `/leagues`, `/seasons`, `/teams`, `/countries`, `/fixtures/*`, `/search`, `/meta/{coverage,status}`, `/account/usage` |
| **paid** (Starter or Pro) | `/fifa-rankings*`, `/fixtures/today/predictions`, `/leagues/{id}/{stats,btts,corners}`, `/matches/{id}/{predictions,btts,corners}` |
| **enterprise** | `/matches/bulk`, `/webhooks` |

**Every player endpoint is `x-plan: free`.** Everything behind the paywall is a
betting-market derivative or FIFA rankings. **The minimum tier that unlocks per-player-
per-match data is: none. No tier does.**

There is no `/matches/{id}/players`, no `/fixtures/players`, no per-match player-stats route
under any name.

### A.3 Correction to the section above

The Architect's note says footballdata.io has "**no lineups endpoint at all**". That is not
quite right, and the correction is worth recording so the comparison table stays accurate:

`/matches/{match_id}` (**free tier**) returns, per the spec's own field descriptions:

- `lineups` — *"Starting lineups for home and away with player details, shirt numbers, and events."*
- `bench` — *"Bench/substitute players for home and away, including **substitution minute and replaced player**."*
- `events` — *"Chronologically sorted timeline events (goals, cards, substitutions) each with minute, extra_minute, team_side, event_type, player, assist, player_in/out, detail."*

So it **does** carry P1-shaped data, embedded in the match-detail response rather than on a
dedicated route. This does not change the verdict — the PL API gives us better P1 for free,
Opta-keyed — but "no lineups" would be a wrong statement in a comparison table.

Note also the vendor's own `/help/` page claims *"`GET /matches/{match_id}` for match and
**player stats**"*. That is the exact ambiguity the brief flagged, and it is **wrong in the
vendor's own copy**: the response gives player *identity*, not player *statistics*.

### A.4 The remaining questions, answered

**Which fields, for P2?** None. `/players/{player_id}/stats` is described verbatim as
*"season-level statistics"*: appearances, starts, `minutes_played`, goals, assists,
penalties scored/missed, cards, clean sheets, goals conceded, per-90 rates.

⚠️ **Terminology trap:** the spec labels a block `defensive`. It contains **`clean_sheets` +
`goals_conceded`** — not defensive actions. Do not let that phrasing survive into a table.

`include_detailed=1` returns *"a `detailed_data` block of raw source fields"* — an
undocumented upstream passthrough. It is **season-level regardless**, so even in the best
case it cannot produce P2, and the PL API already returns 132 Opta stat names per player per
season, Opta-keyed, back to 2006/07, free. **[UNVERIFIED contents — needs a key]**

**Per-player-per-match xG/xA?** No. xG is team-level (`stats.xg`) plus `xg_prematch`.
`expected_assists` and npxG do not exist. **P3 untouched.**

**History depth?** **Not published anywhere** — pricing page, docs, ToS and SEO pages all
hedge with *"where available"*. No season count, no earliest year. History is *not*
tier-gated in the spec: `/matches`, `/seasons/{id}/matches`, `/leagues/{id}/matches` are all
`x-plan: free`. (The `/help/` page contradicts this too — see A.6.)

**Player ID lineage?** Proprietary sequential integers (`player_id: 5001`, `team_id: 1119`).
Scanning the spec for `opta`, `external`, `source_id`, `reference_id`, `mapping`,
`transfermarkt`, `fifa_id`, `provider` → **0 hits each**. `/search` is name-based only. **No
cross-reference endpoint exists.** A full fuzzy-matching layer would be required.

**Rate limits per minute?** **None published.** `/documentation/rate-limits/` documents
**monthly quotas only**; `/account/usage` returns `limit_type: "monthly"`; the 429 payload is
monthly. ToS §5 *reserves* the right to enforce *"monthly, daily, hourly, per-minute, or
per-second"* limits while stating none. No documented rate-limit headers. **[UNVERIFIED]**

**Does Starter $19 suffice?** The question is moot, but for the record the minimum *useful*
tier here is **Free ($0)**, for three independent reasons:

1. Every player and match endpoint is `x-plan: free`.
2. **England Premier League is on the Free plan** (verified on `/leagues-by-plan/`). The
   50/150-league counts are irrelevant to an FPL-only project.
3. Volume: our entire backfill is ~3,800 fixture requests. Starter's 100,000/mo and Pro's
   300,000/mo are ~26x and ~79x more than we would ever use. **Quota is never the reason to
   pay here.**

### A.5 Licence — permits the modelling, grazes the storage

ToS last updated 2026-05-03 · Texas law · Voroxi Group LLC.

- §11 **permits** using responses to build *"analysis tools … and internal systems"*.
- §11 **forbids** redistributing or exposing raw data *"as a standalone data feed, database,
  downloadable dataset, or competing API"*. Fine — we publish nothing.
- **§12 is the friction:** *"Long-term storage, bulk replication, data warehousing, or
  rebuilding a competing football database **may require written permission or an enterprise
  agreement**"*, plus *"Cached data should be refreshed regularly to avoid showing outdated
  football information."* Our bitemporal store is permanent by design and never updates in
  place. That is not obviously inside §12.
- §10: commercial use requires a paid plan. Ours is private, so Free is licence-appropriate —
  subject to the Free tier's **attribution requirement**.

Worth stating plainly: a paid vendor is supposed to buy a *cleaner* licence than the PL API
position we accepted in blueprint §3.6. **This one is arguably murkier**, because §12 names
data warehousing specifically.

### A.6 Data-quality signals — thin to absent, and that is the finding

- **No independent reviews, forum posts, Reddit threads or GitHub issues exist anywhere.**
  Searched; found only vendor pages and competitors' comparison posts. Zero community footprint.
- **The product is ~2 months old.** The changelog holds exactly **9 entries, all between
  2026-06-24 and 2026-07-11**, and **nothing has shipped in the 6 weeks since**.
- The "official" PHP SDK `github.com/voroxi/footballdata-php` was **created 2026-07-11 and
  last pushed 60 seconds later**. One commit, 0 stars, 0 issues.
- A `/status/` page with incident history exists — good practice — but rendered as a template
  shell with no inspectable incidents. **[UNVERIFIED]**
- **The `/help/` centre is AI-generated persona content** (*"Samuel visits Footballdata.io
  documentation to clarify…"*) and **contradicts the product in three places**: free plan
  *1,000* req/month (pricing and changelog v1.8.2 say 2,000); *"Starter plan and above include
  historical data"* (pricing lists historical fixtures on Free); and `/matches/{id}` returning
  *"player stats"* (it does not).
- ToS §16/§17 disclaim completeness and accuracy and attribute outages to *"data provider
  problems"*. No accuracy SLA below Enterprise.

The brief asked for the equivalent of API-Football's "occasional inconsistencies in granular
match events". **There is no granular match event data here to be inconsistent about** — and
no user base large enough to have reported anything either way. For P2, where silent errors
corrupt the DC model, an unreviewed 2-month-old aggregator would be the wrong risk even if it
*did* have the fields.

### A.7 Reseller — almost certainly FootyStats, not proven

They name no upstream; ToS §22 admits reliance on *"third-party … data providers"*.
Circumstantial case, strong:

- Field vocabulary is FootyStats' near-verbatim: `date_unix`, `game_week`, `round_id`,
  BTTS/corner/over-under **"potential"** percentages, **PPG**, `xg_prematch`, *"attacks and
  dangerous attacks"*, offsides over-lines, 10-minute `goal_timing_minutes` bands,
  correct-score distributions, and a set-piece block of throw-ins / free kicks / goal kicks.
- Plan ladder mirrors FootyStats': **5 / 50 / 150 / 1,200+** leagues vs **1 / 50 / 150 /
  1,500+**.
- Changelog v1.8.3 reads as a confession: *"Fixtures whose matchweek has not been assigned
  **by the data source** yet … now return `game_week: null` instead of a misleading `0`."*
  A `0` sentinel for unassigned gameweeks is FootyStats behaviour.

This matters practically: **FootyStats' player data is also season-aggregate only**, which
explains the shape of footballdata.io's surface exactly. It is **not** the same upstream as
API-Football (API-SPORTS), so the two are genuinely different feeds — footballdata.io is
simply the weaker one for our purpose.

### A.8 The vendor concedes the point itself

On `footballdata.io/footballdata-io-vs-api-football/`, their own comparison table:

| | Footballdata.io | API-FOOTBALL |
|---|---|---|
| **Player data** | **Limited / check docs** | Available |
| Odds | Limited / check docs | Available |

and in their own words: *"Choose API-FOOTBALL if you need … **player data**."*

### A.9 Pricing — verified exactly as the user reported

Read from the `#pricing` block's `data-monthly` / `data-annual` attributes, 2026-08-20:

| Tier | Monthly | Annual | Quota | Leagues |
|---|---|---|---|---|
| Free | $0 | — | 2,000/mo | 5 (**incl. England Premier League**) · attribution required |
| Starter | **$19** | $190 | 100,000/mo | 50 · commercial use |
| Pro | **$49** | $490 | 300,000/mo | 150 |
| Enterprise | custom | — | custom | 1,200+ · bulk export, webhooks, SLA |

`robots.txt` HTTP 200: `User-agent: *` / `Allow: /`, Cloudflare content signals
(`ai-train=no`, `use=reference`). **No access restriction on the site.** The API is fully
auth-walled — three unauthenticated probes each returned a clean `401 missing_api_key`.
Probing stopped there.

### A.10 Head-to-head with API-Football one-month-$19, for the same job

| | **API-Football, 1 month $19** | **footballdata.io Pro, $49/mo** |
|---|---|---|
| Per-player-per-match stats | **Yes** — `/fixtures/players`, 40 players in one call, verified live | **No — none at any tier** |
| Tackles / interceptions / blocks per match | **Yes** (verified) | No |
| Clearances / recoveries per match | No | No |
| Per-player-per-match xG/xA | No | No |
| Lineups + subs per match | Yes | Yes (inside `/matches/{id}`) |
| Team match stats | Yes | Yes, richer on betting markets |
| Seasons of history | 17 listed; player stats from **2016** | **Unpublished** |
| Opta IDs | No | No |
| Per-minute rate limit | **Published** | **Not published at all** |
| Vendor track record | Years, large user base | **~2 months, zero footprint** |
| Cost for our job | **~$19 once** | $49/mo recurring, delivers nothing for P2 |

**API-Football wins outright.** footballdata.io is not in the race for this job.

### A.11 Could NOT verify without an account

| Item | First-five-minutes check after Free signup ($0, no card) |
|---|---|
| **History depth** — the only genuinely open question | `GET /meta/coverage` → returns `earliest/latest match dates`, entity counts, season-year distribution. **One call settles it.** |
| `detailed_data` raw passthrough contents | `GET /players/{id}/stats?include_detailed=1` on a PL player. Season-level regardless. |
| Per-minute / per-second rate limit | Short controlled burst; read headers for `X-RateLimit-*`. Stop at the first 429. |
| Whether PL payloads actually populate `lineups` / `bench` | `GET /matches/{id}` on a completed PL fixture. |
| Status-page incident history | `/status/incidents` in a browser. |
| Upstream identity | Diff a `detailed_data` block against a FootyStats response. Not otherwise resolvable. |

**None of these can change the recommendation.** The P2 negative is structural — the route
does not exist in a published OpenAPI spec covering the vendor's *entire* surface — so no
post-signup probing can produce per-player-per-match defensive actions.

### A.12 Recommendation

**Do not buy footballdata.io at any tier. Confidence: high.**

The only thing money was still supposed to buy is P2, and this vendor does not have it at any
price including Enterprise. The $49 tier over $19 buys leagues we do not need, quota we would
use ~1.3% of, and prediction endpoints blueprint §1/§5 forbid us from consuming. Even the free
tier adds nothing our existing free stack lacks.

If the user wants to spend around $49 on data, **TheStatsAPI Starter for one month ($50)**
buys strictly more: per-player-per-match clearances *and* `expected_goals` / `np_expected_goals`
/ `expected_assists` plus a shotmap endpoint. Run their 7-day trial first (§4.7).

**Recoveries per player per match remain unobtainable from every affordable source.** This
evaluation adds a twelfth vendor to that list and does not move it.
