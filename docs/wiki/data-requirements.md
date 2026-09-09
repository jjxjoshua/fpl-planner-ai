# Data Requirements

What FPL-AI needs, in priority order, independent of any particular vendor. Written so a
provider can be evaluated against it — see `data-sources.md` for what we currently have.

**Join key:** FPL `elements[].opta_code` is an Opta identifier (new in 2026/27). **A provider
whose player IDs are Opta-derived joins to our data for free.** Anything else costs us a
fuzzy name-matching layer that will silently mismatch on transfers, loans and accented
names. Treat Opta lineage as a major selection criterion, not a nice-to-have.

**Depth:** 5+ seasons wherever history is asked for. Fewer than 3 makes the team-strength
priors and minutes model noticeably worse; 2 is not enough to fit anything with confidence.

---

## P1 · Historical lineups and substitutions

**The single biggest gap, and it gates the highest-ROI model in the system (§4.1).**

| Need | Granularity |
|---|---|
| Starting XI per fixture | Per match, 5+ seasons |
| Substitutions with minute on and off | Per match |
| Unused bench / squad list | Per match |
| Reason for absence where available | injured / suspended / rested / not selected |

Without this the minutes model is guessing. `chance_of_playing_next_round` from FPL is a
present-tense flag with no history — it cannot train anything.

**Current state:** API-Football free is locked to **2022–2024** at 100 req/day. That is
~380 fixtures/season through a 100/day straw, and no coverage of 2025 or 2026 at all.

**REVISED 2026-08-21 — three of the four rows above are now free, not blocked, per E2b
stories 6/7/7b (`docs/wiki/provider-framework.md` §9, §12).** The PL API's
`match.lineups@match` and `match.substitutions@match` capabilities supply starting XI,
unused bench, and substitutions-with-minute-on/off per match, **free**, for any season back
to 2019-20 (the team-identity archive boundary, §12.2's `vaastav_team_identity.csv` gap) —
subject to the season-appropriate identity-snapshot requirement `provider-framework.md`
documents, not a live-only capability. **What is still genuinely unmet: "reason for
absence."** Neither the PL API's lineup/substitution payloads nor vaastav's archive carry an
injured/suspended/rested/not-selected label for a benched or unused player — API-Football's
`/injuries` endpoint (§5.3, `data-sources.md`) remains the only source found for that
specific field, and only for its 2022-2024 window. This is real progress on P1, not full
closure — do not read the "single biggest gap" framing above as still fully accurate.

## P2 · Per-player defensive actions, per match

**New for 2026/27 and nobody has modelled it yet — genuine edge available.**

Defensive contribution now scores **2 points for DEF, MID and FWD** (§11). The thresholds
aren't published in the API and must be reverse-engineered after GW1. To model DC we need
the underlying counts historically:

> tackles · interceptions · clearances · blocks · recoveries — **per player, per match**

FPL's API exposes `defensive_contribution` only as a current-season aggregate. That trains
nothing. Because this scoring rule is new, the market has no history on it either — which
is exactly why it's worth having.

**REVISED 2026-08-19 — the fallback is stronger than first assessed.** The PL API gives
*every* DC component **per match at team level**, free and historical: `ballRecovery`,
`totalTackle`, `wonTackle`, `interception`, `totalClearance`, `effectiveClearance`,
`outfielderBlock`, `blockedPass`, `blockedScoringAtt`, `blockedCross`. **Recoveries
included** — the field no affordable vendor sells per match. Combined with free per-player
*season* aggregates this supports a real estimator:

> **player season rate x team per-match total**, calibrated on 2025-26 (§11)

**CONFIRMED IN-STORE 2026-08-21.** vaastav's per-gameweek data carries `tackles`,
`recoveries`, `clearances_blocks_interceptions` and `defensive_contribution` **per player,
per gameweek — for 2025-26 and no other season** (29,338 rows; every earlier season is
uniformly zero). This is not a solution to P2; it is exactly the single-season calibration
constraint in §11, now confirmed by measurement rather than inference.

What it does buy, free: the **calibration set** is in hand. Any multi-season DC estimator
built on team-level totals (§11) can be fitted against real per-player 2025-26 ground truth
without purchasing anything.

Per-player-per-match across *multiple* seasons remains the only thing worth paying for, and
it is a refinement rather than a blocker.

## P3 · Match and player xG

| Need | Granularity |
|---|---|
| Match xG for and against | Per fixture, 5+ seasons |
| Player xG, xA, npxG | Per player, per match |
| Shot-level data with location | Per shot — preferred, not required |

Feeds the Dixon-Coles team model (§4). We fit on **xG, not goals** — xG is materially more
predictive of future results.

**Current state — REVISED 2026-08-19: team level is SOLVED and free.** The PL API's
`/v3/matches/{id}/stats` carries 185 keys per side per match including `expectedGoals`,
`expectedGoalsOnTarget`, `expectedAssists` and the conceded variants — verified live,
historical. That is exactly the Dixon-Coles input, so **P3 no longer blocks the team model.**

**Player level — REVISED 2026-08-21, better than recorded.** vaastav's per-gameweek data
carries `expected_goals`, `expected_assists` and `starts` from **2022-23 onward** (verified
in-store: 4 seasons, ~108k rows), not 2024-25. `olbauday` remains the shot-level source from
2024-25. FBref is Cloudflare-blocked; Understat is `Disallow: /`.

Still missing: player-level xG/xA before 2022-23.

### Archive coverage boundary — verified 2026-08-21

vaastav's coverage is **not uniform across files**. `players_raw.csv` reaches back to
2016-17, but `data/{season}/teams.csv` **does not exist for 2016-17, 2017-18 or 2018-19**
(confirmed 404). Season-scoped *team* identity therefore starts at **2019-20**, while
season-scoped *player* identity starts at 2016-17.

Consequence: any historical backfill of a **team-dependent** capability is bounded at
2019-20 unless another per-season team list is sourced. Treated as a hard `TransportError`,
never coerced or null-padded.

## P4 · Full fixture calendar including cup and European competitions

Needed for the **congestion feature** — midweek UCL/UEL fixtures are among the strongest
rotation predictors in the game, and they start in September. Requires kickoff timestamps
(not just dates), competition tier, and round, so "midweek fixture within N days" is
computable.

## P5 · Historical odds

Match odds, over/under, BTTS, and ideally anytime goalscorer, **at multi-season depth**.

Purpose is narrow but real: without it, odds are a **live-only overlay whose value can
never be measured** (§3.3). We can use them; we cannot prove they help. This is the only
item on this list whose absence costs us *validation* rather than *capability*.

Live odds are already solved by The Odds API free tier.

## P6 · Injury and availability history

Onset date, body part, expected and actual return, and status transitions over time — for
Phase 7's news layer and as a prior for P1. Present-tense injury tables are common;
**historical ones with dated transitions are rare**, so check specifically for this rather
than assuming an injuries endpoint implies it.

---

## Evaluation checklist

For any candidate provider:

1. **Are player IDs Opta-derived?** If not, budget for an ID-mapping layer.
2. **How many seasons of history**, and is history on the same tier as live data? (Several
   providers gate history separately or purge it — API-Football purges historical odds.)
3. **Rate limits and quota** — expressed per day and per minute. A generous monthly total
   behind a tight per-minute cap is still slow to backfill.
4. **Is a bulk or dump export available?** Backfilling 5 seasons through a per-fixture
   endpoint at 100 req/day takes months. This one question often decides viability.
5. **Licence** — does it permit local storage and derived modelling? We store everything
   bitemporally and never re-publish raw data.
   > **A paid source must buy a *cleaner* licence than the free alternative, or paying for
   > it is pointless.** footballdata.io's ToS §12 reserves the right to require an
   > enterprise agreement for "long-term storage, bulk replication, data warehousing" and
   > says "cached data should be refreshed regularly" — our store is permanent by design and
   > never updates in place. That is *murkier* than the PL position accepted in §3.6, which
   > is free. Money that buys more legal ambiguity is money spent backwards.
6. **Stability** — undocumented endpoints that shift between seasons cost more than they save.
7. **Maturity signals** — changelog cadence, SDK activity, independent reviews, issue
   trackers. A two-month-old vendor with no user base has no track record to inspect, which
   is not neutral: it means *nobody has found the bugs yet*.
8. **Is the data actually theirs?** Check field vocabulary against known upstreams. A
   reseller inherits its source's ceiling — if the upstream is season-aggregate only, no
   tier of the reseller will ever produce per-match rows.

## Priority if only some can be bought

**P1 and P2 first.** Minutes gate every other prediction in the system, and DC is new
scoring that nothing in the public FPL ecosystem has modelled yet. P3 is valuable but
partially substitutable — a team-strength model can be fit on goals with more shrinkage if
xG history is unavailable. P5 is the most deferrable: it buys proof, not capability.
