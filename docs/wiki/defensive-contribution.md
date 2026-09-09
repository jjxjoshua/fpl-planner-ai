# Defensive contribution (DC) — football and data reality

**Author:** `fpl-elite` · **Session:** s003 · **Written:** 22 Aug 2026 (local, GMT+8)
**Scope:** football knowledge and source reality only. No schema, no estimator, no code —
those are the coder's lane and are deliberately absent.

> **Read §1 and §2 before anything else. Two load-bearing claims currently in
> `BLUEPRINT.md` §11 are wrong, and one of them is the reason the DC estimator is
> considered blocked. It is less blocked than recorded.**

---

## 0. Headline verdicts

| # | Verdict | Confidence |
|---|---|---|
| 1 | 2026/27 DC rules are **unchanged from 2025/26**: DEF 10 CBIT, MID+FWD 12 CBIRT, 2 pts, capped at 2/match | **HIGH** — premierleague.com, 20 Jul 2026 |
| 2 | §11's *"defensive contribution now pays forwards"* is **factually wrong**. Forwards were eligible from the 2025/26 launch, at the same 12 CBIRT | **HIGH** — premierleague.com, 10 Aug 2025 |
| 3 | §11's *"recoveries unobtainable per-player-per-match from every affordable source"* is **wrong for 2025-26 and for the live season**. FPL's own API publishes `recoveries` per player per fixture, free. The project has already measured this in-store | **HIGH** — project's own measurement + vaastav data dictionary |
| 4 | The real gap is **historical depth (pre-2025-26)**, not observability. Different problem, much smaller | **HIGH** |
| 5 | A recoveries proxy is **not needed for any season the model will actually be scored on**. For pre-2025-26 backfill a proxy is possible but is a proxy for a *rate*, and DC pays on a *threshold* — see §3 | **HIGH** on the reasoning |
| 6 | DC is **not a minor term.** The best specialists banked 50-52 DC points in 2025/26, roughly a quarter to a third of a good defender's season total. It deserves real modelling investment | **MEDIUM-HIGH** |
| 7 | The largest error source for 2026/27 DC is **role and system churn, not data depth.** The league's best DC midfielder just moved to its most possession-dominant club | **HIGH** on the transfer, **MEDIUM-HIGH** on the consequence |

---

## 1. The verified 2026/27 rules

### 1.1 What the rule is

| Position | Counting stats | Threshold, per match | Points | Cap |
|---|---|---|---|---|
| **GKP** | — | — | **0** (not eligible) | — |
| **DEF** | **C**learances + **B**locks + **I**nterceptions + **T**ackles (**CBIT**) | **10** | **2** | 2 per match |
| **MID** | CBIT + **R**ecoveries (**CBIRT**) | **12** | **2** | 2 per match |
| **FWD** | CBIT + Recoveries (**CBIRT**) | **12** | **2** | 2 per match |

Awarded **once** per match — hitting 20 CBIT pays the same 2 points as hitting 10.
Awarded regardless of result, clean sheet, or minutes played (no 60-minute qualifier).

**Tier: HIGH.** premierleague.com, *"What's happening with defensive contribution points in
2026/27 Fantasy?"*, **published 20 Jul 2026** (date read off the page, not assumed). Wording:
*"Any defender who reaches a combined total of 10 clearances, blocks, interceptions and
tackles (CBIT) in a single match scores two FPL points"*; midfielders and forwards
*"require 12"* with *"ball recoveries"* added.

Corroborated independently at **MEDIUM** by fploracle.team (24 Jul 2026), draftfantasy.com and
Operation Sports, all agreeing 10/12 unchanged. No source found anywhere claiming a change.

The point *values* (DEF 2 / MID 2 / FWD 2 / GKP 0) are separately confirmed **live from the
API** in the project's own `game_config` read of 2026-08-19 (`data-sources.md` §1.3) — that is
the strongest tier available and it agrees with the press material.

### 1.2 Discrepancies against `BLUEPRINT.md` §11 — both should be corrected

**DISCREPANCY 1 — "defensive contribution now pays forwards" is wrong.**
§11 lists this under *"Differences from a 2025/26-era assumption"*. It is not a difference.
Forwards were eligible from the moment DC launched.

> premierleague.com, *"What's new for 2025/26"*, **10 Aug 2025**: *"in addition to their
> clearances, blocks, interceptions and tackles, their 'ball recoveries' will also count
> towards their total of defensive contributions (CBIRT). As a result, they [midfielders **and
> forwards**] have to make 12 defensive contributions to earn their two FPL points, instead of
> 10."*

**Tier: HIGH** (official Premier League, dated, and the 2026/27 article explicitly frames the
mechanic as *remaining* rather than being extended).

*Why it matters downstream:* the 2025-26 calibration season **already contains forward DC
observations under the exact 2026/27 rule**. Nothing about FWD DC needs to be modelled fresh
or extrapolated. If anyone has scoped work on the assumption that FWD DC is unobserved, that
scope is unnecessary.

The other two items in that §11 bullet — no Assistant Manager chip, GK goals worth 10 — are
outside my brief and I did not re-verify them.

**DISCREPANCY 2 — the recoveries claim.** §11 states recoveries are *"unobtainable
per-player-per-match from every affordable source surveyed."* That sentence is true only of
**seasons before 2025-26**. As written, without the temporal qualifier, it is false and it is
the sentence that made this estimator look blocked. See §2.

### 1.3 What is genuinely still unknown about the rules

- **The thresholds are not published in the API.** `game_config.scoring.defensive_contribution`
  gives the *points* (2) and nothing about the *count* required. Confirmed by the project's own
  live read (`data-sources.md` §4). The 10/12 values above rest on press material, not on the
  API. **Tier: HIGH but not machine-readable.**
- **`overrides` could in principle alter this per event.** §11 already flags that chips and
  events carry `overrides{rules,scoring,...}`. I found no reporting of any DC override and
  cannot rule one out. **Unknown.**
- **First empirical verification is days away.** GW1 fixtures are being played 22-23 Aug 2026.
  `event/1/live/` `explain` blocks will carry actual DC point awards against actual component
  counts, which pins 10 and 12 by observation for the first time this season. **Flagging as
  time-critical: this is a one-line confirmation available from Monday and it removes the last
  press-only dependency in the whole rule set.**

### 1.4 Adjacent, not DC — a 2026/27 BPS change that touches the same players

Separate mechanic, easy to conflate. For 2026/27: clearances, blocks and interceptions are
reported to earn **1 BPS per 3 actions (was per 2)**, and players are **no longer penalised in
BPS for being tackled**.

**Tier: MEDIUM.** The "no longer penalised for being tackled" element is corroborated by
fploracle.team (24 Jul 2026). The "per 3 rather than per 2" figure I carried forward from the
project's own s002 intel pass (`gw1-2026-27-intel.md`, tiered MEDIUM-HIGH there) and **I did
not independently re-verify it against a Tier-1 source this pass — treat it as unconfirmed.**

Net football effect if true: a mild *downgrade* to the bonus-point yield of exactly the
high-CBIT centre-back profile that DC rewards. DC and bonus are pulling slightly against each
other for that archetype this season.

---

## 2. Per-component observability

### 2.1 The correction that matters

FPL is an **Opta customer**. It does not compute these counts itself — it publishes Opta's, and
it publishes them **per player, per fixture, free, in its own public API**, under exactly the
definitions the scoring rule pays on.

The project has already proven this by measurement:

- `data-requirements.md` §P2, **CONFIRMED IN-STORE 2026-08-21**: vaastav's per-gameweek data
  carries `tackles`, `recoveries`, `clearances_blocks_interceptions` and
  `defensive_contribution` per player per gameweek — **29,338 rows for 2025-26**, uniformly zero
  for every earlier season.
- `data-sources.md` §1.2: `elements` carries the defensive family; `element_stats` (26 entries)
  lists `defensive_contribution`, `tackles`, `recoveries`, `clearances_blocks_interceptions` as
  tracked stat names — i.e. they are part of the per-gameweek live stat vocabulary, not just
  season aggregates.
- vaastav's `DATA_DICTIONARY.md` (fetched 22 Aug 2026) lists all four in **both** `players_raw.csv`
  (season grain) **and** `gw*.csv` (per-match grain): `recoveries` → *"Ball recoveries"*,
  `tackles` → *"Tackles made"*, `clearances_blocks_interceptions` → *"Defensive actions"*.

vaastav is a **mirror of the FPL API**, not an independent measurement. So this is one source,
not two — but it is the *authoritative* source, because it is the scorer's own counter.

### 2.2 The table

Grain throughout is **per player, per match**.

| Component | 2026-27 (live) | 2025-26 | 2019-20 → 2024-25 | Best source, and what it actually is |
|---|---|---|---|---|
| **Tackles** | **Yes, free** | **Yes, free** | **No** | FPL API `element-summary/{id}/history` → `tackles`; mirrored in vaastav `gw*.csv`. Opta `totalTackle` under FPL's badge. Field did not exist in the API before 2025-26 (`gw1.csv` header check, `provider-evaluation.md` §6.2) |
| **Clearances** | **Bundled** | **Bundled** | **No** | Not separable. FPL publishes `clearances_blocks_interceptions` as **one summed field**, never the three parts. For DC this is harmless — the rule sums them anyway |
| **Blocks** | **Bundled** | **Bundled** | **No** | as above |
| **Interceptions** | **Bundled** | **Bundled** | **No** | as above |
| **Recoveries** | **Yes, free** | **Yes, free** | **No** | FPL API → `recoveries`. **This is the field §11 calls unobtainable.** It is obtainable, for the seasons that matter |
| **`defensive_contribution` (the derived total)** | **Yes, free** | **Yes, free** | **No** | FPL's own computed CBIT/CBIRT sum. The label, not a feature |

**Caveat on the live-season cells:** GW1 fixtures are being played *now*. As of writing, no
2026/27 per-match row exists for anyone. The "Yes" for 2026-27 is an inference from the fields
being present in `element_stats` and in last season's `history` payloads — a strong inference,
but **it becomes an observation only after GW1 settles**, and it should be checked then rather
than assumed.

**Caveat on DGWs:** the FPL `history` array is keyed per *fixture*, not per gameweek, so the
grain genuinely is per match. Anything that aggregates to gameweek before applying a
per-match threshold will silently corrupt double gameweeks.

### 2.3 Sources for the pre-2025-26 gap, named

| Source | Recoveries per player per match? | Notes |
|---|---|---|
| **PL API `/football/stats/player/{id}`** | **No — per SEASON only** | But it has `ball_recovery` plus all other components, **free, Opta-native, back to 2006/07** (`provider-evaluation.md` §P2). The best free historical surface that exists |
| **PL API `/v3/matches/{id}/stats`** | **No — per TEAM per match** | Has `ballRecovery`, `totalTackle`, `wonTackle`, `interception`, `totalClearance`, `effectiveClearance`, `outfielderBlock`. Free, historical |
| **API-Football** | **No** | Has tackles / interceptions / blocks per player per match. **No clearances, no recoveries** (verified, `provider-evaluation.md`). 2022-24 window only |
| **TheStatsAPI** | **No** | Tackles + interceptions + clearances. **No blocks, no recoveries** |
| **Sportsdata.io** | **No** | Data dictionary confirms absence of clearances *and* recoveries |
| **Sportmonks** | **Unknown** | Stat-type list is behind an authenticated endpoint. Genuinely unanswerable from public material |
| **FBref** | **No — and newly worse** | See §2.4 |
| **StatsBomb open data** | Event-level, perfect — **but PL 2003/04 and 2015/16 only** | No useful overlap with current players |
| **Opta / Stats Perform / Sportradar direct** | Yes | Quote-only, four-to-five figures, effectively requires a company |

### 2.4 New this year: FBref is gone as a fallback, for a new reason

The project already had FBref marked dead on a Cloudflare 403. The underlying situation is now
worse and it is worth recording so nobody re-litigates it:

**On 20 January 2026, Stats Perform (Opta) terminated its agreement with Sports Reference and
required the immediate removal of all advanced statistics from FBref.** Reported to have come
eight days after Stats Perform was named FIFA's exclusive betting-data and streaming-rights
distributor for the 2026 World Cup. Historical pages reportedly remain but **no longer update**.

**Tier: MEDIUM** — sports-reference.com's own blog post is the primary source and it **403s to
this agent**, so I am relying on secondary reporting (The IX Sports; community aggregation) that
consistently quotes it. I could not read the primary. Treat the date and the "historical data
remains, frozen" detail as reported, not confirmed.

Relevant because FBref's `Recov` field (Miscellaneous Stats) was the one free public surface
that carried a recoveries-like number per player. It is not a live option now, and even the
frozen historical pages would be a **different provider-era measurement** for anything before
2022 (Sports Reference's football data came from StatsBomb before Opta took over in 2022).

### 2.5 Is "recovery" a well-defined stat?

Yes, and unusually cleanly.

**Opta's definition:** a Ball Recovery is collected when possession changes from one team to the
other in open play and **full control is established**; the player with the first controlled
action is credited. It covers taking control of a genuinely loose ball, and receiving an
uncontrolled ball from an opponent.

**Tier: HIGH** for the definition existing and being Opta-owned (Stats Perform's own event
definitions page; Opta Analyst's definitions glossary).

**A definitional event you must know about, and its resolution:**
Opta **reworded** the ball-recovery definition in **February 2026 — mid-2025/26 season**. The
old wording emphasised the ball being *"played directly to him by an opponent"*; the new
emphasises *"full control must be established"*, which excludes e.g. picking up a defensive
clearance where no possession change occurred.

Fantasy Football Scout (23 Feb 2026, **Tier: MEDIUM** — established specialist outlet, dated)
reports the practical impact as: *"Nothing! The rewording from Opta is purely to provide a
better understanding of how they collect ball recoveries."* Clarificatory, **not** a collection
change and **not** retroactive.

**Why this matters and why it is good news:** had it been a real collection change mid-season,
the single calibration season would have been split across two incompatible definitions and
would have needed a mid-season break point. On the reported facts it does not. **I would still
treat this as MEDIUM rather than HIGH** — it rests on one specialist outlet's characterisation
of Opta's intent, not on an Opta statement I read directly. If the DC work ever produces an
unexplained mid-2025-26 level shift in recoveries, this is the first hypothesis to test.

---

## 3. Verdict on proxying the unobtainable components

### 3.1 The short version

**For every season the model is actually scored on, no proxy is required. Do not build one.**

- **2026/27 (live):** observed directly, free, from the scorer's own counter.
- **2025-26 (calibration/training):** observed directly, in-store already, 29,338 player-matches.
- **Pre-2025-26:** not observed. A proxy is *possible*. Whether it is *worth it* is §3.3.

Anyone scoping "estimate recoveries because they are unobtainable" for the current season is
solving a problem that no longer exists. That is the single most useful thing in this document.

### 3.2 If pre-2025-26 backfill is attempted anyway — the honest football critique

§11's fallback shape is `player season rate × team per-match total`. As football reality, here
is what that construction can and cannot capture. This is not a modelling prescription; it is a
statement about what the underlying quantity is like.

**It will get the mean roughly right and the thing that pays wrong.**
DC is a **threshold** statistic. What determines DC points is not a player's mean CBIRT but
`P(CBIRT ≥ 12)` — which is governed by the *dispersion* of his per-match distribution as much as
its centre. A season rate multiplied by a team match total carries almost no information about a
player's own match-to-match dispersion; it inherits the *team's*, which is a different quantity
driven by different things (opponent, game state, red cards).

Concretely: two midfielders with an identical 8.0 CBIRT/90 season rate — one a metronomic
6-to-10 every week, one who posts 3 when his side controls the game and 14 when it is pinned
back — have very different DC yields. The estimator cannot distinguish them. And FPL pays the
second one more.

**Components are positively correlated within a match, and the threshold is on the sum.**
A match where your team is under sustained pressure produces more clearances *and* more blocks
*and* more interceptions *and* more recoveries simultaneously. Estimating components separately
and summing them will understate the upper tail — precisely the tail where DC points live.

**Minutes are first-order and interact multiplicatively.**
DC has no 60-minute qualifier and no pro-rating. A 10-per-90 player who plays 60 minutes is a
coin-flip; the same player at 90 is near-certain. The DC term is therefore only as good as the
minutes distribution feeding it — which the project now has (`model-minutes.md`). This is a
genuine strength and it should be exploited rather than the DC term being built standalone.

**Verdict on "defensible":** a pre-2025-26 proxy is defensible **only** if it is fitted on
2025-26 (where both the proxy inputs and the FPL ground truth exist) and validated against
**threshold-crossing accuracy**, not against rate accuracy. A proxy that reproduces season rates
beautifully and has never been checked against `P(CBIRT ≥ 12)` tells you nothing about the thing
being modelled. If that validation is skipped, then yes — plainly — **anyone claiming the proxy
is good is guessing**, and the guess is structurally biased in the direction that matters.

### 3.3 My recommendation on whether to backfill at all

**Probably not, and this is the contrarian part.**

One season of FPL-native ground truth is ~380 matches and 29,338 player-match rows. That is not
a small dataset for a per-match count model with strong positional and team-style structure.

Against that, a multi-season backfill buys you rows measured under **a different provider's
definitions** for a rule that pays on **FPL's counters specifically**. `provider-evaluation.md`
§6.2 already makes this point and it is correct: every third-party tackle or clearance count is
a proxy with a different definition, and the definitional bias is unmeasured except in 2025-26 —
which is exactly the season you would not need it for.

More decisively: **the dominant error in a 2026/27 DC forecast is not sampling noise from having
one season. It is role and system churn** (§4.3). Five extra seasons of a player's history does
not help you when the thing that changed is his manager, his team's possession share, or his
position in the shape. Investment in conditioning DC on **team style and role** will outperform
investment in historical depth, at lower risk of importing silent definitional bias.

That is a judgement, offered as such — **confidence MEDIUM-HIGH** — not a fact.

---

## 4. Which archetypes DC actually pays out for

All figures below are **2025/26 outcomes**, from premierleague.com's *"Who's best at earning
defensive contribution points in Fantasy?"* (**published 30 Jul 2026**, date read off the page),
which describes 2025/26 as *"the inaugural season for defensive contributions."* **Tier: HIGH**
for the numbers. Club attributions are 2026/27 where the article gave them; I have re-verified
the two that matter.

### 4.1 The paying archetypes, in order

**1. Centre-back at a deep-block, low-possession side — the best DC profile in the game.**
Volume comes from clearances and blocks, and **defenders need no recoveries at all** (10 CBIT).
The lower threshold plus a role that mechanically generates clearances is the whole story.

| Player | 2025/26 hit rate | DC points |
|---|---|---|
| Marcos Senesi (Bournemouth → **Tottenham**) | 67.6% | 50 |
| Mavropanos (West Ham) | 66.7% | 36 |
| Charlie Hughes (Hull) | 63.9% | 46 |
| James Hill (Bournemouth) | 63.6% | 28 |
| Cristian Andersen (Fulham) | 60.6% | 40 |
| James Tarkowski (Everton) | 59.5% | 44 |
| John Egan (Hull) | 57.1% | 48 |
| Maxence Lacroix (Crystal Palace) | 57.1% | 40 |
| Dara O'Shea (Ipswich) | 56.5% | 52 |

Senesi averaged **11.5 DC per 90** — i.e. his central expectation sat *above* the threshold,
which is what a 67.6% hit rate looks like.

**Promoted-side note:** Hull, Ipswich and Coventry came up for 2026/27 (Coventry as champions,
Ipswich second, Hull via the play-off final, 1-0 v Middlesbrough). **Tier: HIGH** —
premierleague.com. Hughes, Egan and O'Shea therefore carry those numbers *into* the Premier
League from Championship football, at £4.0m-ish prices. Two cautions, both real: a Championship
CBIT rate is not a Premier League CBIT rate (different opponent quality, though the direction
here favours *more* defending, not less), and promoted-side defenders carry high minutes risk
from squad churn.

**2. Single-pivot / ball-winning midfielder at a low-possession side.**
Needs 12 including recoveries. Recoveries are what make this viable at all — central areas are
where the ball changes hands most.

| Player | 2025/26 hit rate | DC points |
|---|---|---|
| Elliot Anderson (Nottingham Forest → **Man City**) | **70.3%** | 52 |
| Azor Matusiwa (Ipswich) | 56.8% | 50 |
| James Garner (Everton) | 52.6% | 40 |

Anderson's 70.3% was the highest of any player in any position and *"almost 10 per cent more
than any rival"* among midfielders. Separately, Opta Analyst (28 Nov 2025) had him at **8.17
ball recoveries per 90 — the only outfield player in the top 19 of the whole league** on that
metric. See §4.3 before using any of that.

**3. The split, as measured:** of the ten players who earned the most DC points in 2025/26,
**five were centre-backs and five were defensive midfielders.** Nothing else.

### 4.2 The archetypes DC does *not* pay

- **Forwards. Essentially never.** Eligible since day one, and *"relatively few forwards get DC
  points"* — **zero forwards in the 2025/26 top ten.** 12 CBIRT is simply not reachable from a
  front line on a repeatable basis. **Treat FWD DC as a near-zero term with an occasional
  outlier, not as a modelling target.** Hard-pressing wide forwards are the only near-miss:
  Iliman Ndiaye 6.4 recoveries/90, Bukayo Saka 5.7/90 (Opta Analyst, 28 Nov 2025) — real numbers,
  but recoveries alone at 6/90 leaves a long way to 12 CBIRT.
- **Full-backs and wing-backs — weaker than intuition suggests.** They tackle and press well
  (Tyrick Mitchell 5.59 recoveries/90) but as **defenders they get no recovery credit**, and
  their clearance volume is far below a centre-back's. High-volume defensive full-backs at deep
  sides are the exception, not the profile. A cheap starting full-back at a promoted side still
  has a **non-trivial DC floor** even in a hopeless attacking fixture — that is the correct and
  bounded version of the argument.
- **Goalkeepers — ignore entirely.** GKP DC scoring is **0**, yet keepers post the *highest*
  recoveries per 90 in the league (Robin Roefs 10/90, Emiliano Martínez 8.7/90). Any pipeline
  that computes DC without gating on position will surface keepers at the top of a leaderboard
  worth exactly nothing. Cheap, avoidable, embarrassing.
- **Centre-backs and midfielders at possession-dominant elite sides.** The volume is not there.
  This is the mirror image of archetype 1 and it is the trap in §4.3.

### 4.3 The role-vs-numbers divergence you asked me to flag

**This is the most decision-relevant item in the document.**

**Elliot Anderson has left Nottingham Forest for Manchester City for a reported £116m.**
Confirmed by **both clubs** (nottinghamforest.co.uk, 2 Jul 2026; mancity.com), a five-year
contract, widely reported as a British record and a City club record. **Tier: HIGH — official
club announcement.**

Anderson's DC profile — 70.3% hit rate, 52 DC points, 8.17 recoveries/90, the best in the league
— **is a property of Nottingham Forest's system, not of Elliot Anderson.** Recoveries and CBIT
are out-of-possession counters. They are generated by not having the ball. Manchester City are
the most possession-dominant side in the division. The same player, in the same nominal role, at
City, will see a large fraction of that volume simply cease to exist.

Any model that carries his 2025/26 per-90 DC rate forward will rank him as the premier DC
midfielder in FPL. **He may well not clear the threshold half as often.** This is exactly the
case where the underlying number and the role disagree, and the role is right.

The same logic applies, less severely, to **Marcos Senesi (Bournemouth → Tottenham, free
transfer, confirmed by tottenhamhotspur.com; officially joined 1 Jul 2026 on contract expiry;
De Zerbi's second signing after Andy Robertson — Tier: HIGH, club announcement).** He is the
top-rated DC defender in the game moving from a mid-block Bournemouth to a De Zerbi side that
will hold materially more of the ball. Directionally the same downgrade; smaller, because
defenders' CBIT is less possession-elastic than midfielders' recoveries, and because
Tottenham are not Manchester City.

**The generalisable rule, and the honest headline:**

> **DC is a team-style statistic wearing a player's name.** The single strongest predictor of a
> player's DC yield is how little of the ball his team has, filtered through whether his role
> puts him in the defensive third. Player identity is the *second* input, not the first.

Consequences worth stating plainly:
- A DC model that conditions on player history alone will be systematically wrong for every
  player who changed clubs across the possession spectrum — and it will be wrong *most
  confidently* about the highest-profile ones, because they are the ones who move.
- Promoted-side defenders (Hull, Ipswich, Coventry) are the mirror case: their DC rate should
  be *revised up* relative to a naive Championship-to-PL translation, because they will have
  less of the ball in the Premier League than they did in the Championship.
- **This is where the modelling investment belongs.** Team possession/style conditioning will
  buy more accuracy than five seasons of historical backfill, and it is available free.

---

## 5. Could not confirm — read before consuming anything above

1. **2026/27 per-match DC data actually flowing.** GW1 is being played as this is written. The
   fields' presence in `element_stats` makes it near-certain, but **no 2026/27 per-match row has
   been observed by anyone yet.** Verify after GW1 settles.
2. **The 10/12 thresholds from a machine-readable source.** Press-confirmed at HIGH, but absent
   from the API. `event/1/live/` `explain` after GW1 is the first chance to pin them empirically.
3. **Whether any `overrides` block alters DC for any event.** No reporting found; cannot rule out.
4. **The "1 BPS per 3 CBI actions" figure** (§1.4). Carried from the project's own s002 pass,
   **not independently re-verified this pass.**
5. **The Sports Reference / Stats Perform primary announcement.** sports-reference.com 403s to
   this agent. §2.4 rests on secondary reporting.
6. **The Opta February 2026 rewording being genuinely zero-impact.** One specialist outlet's
   characterisation. Plausible and internally consistent, but not an Opta statement I read.
7. **Club attributions in the premierleague.com DC table.** The page rendered inconsistently
   across two fetches (one pass showed transfer pairs like "Tottenham/Bournemouth", another
   showed single clubs). I re-verified the **two transfers that change a conclusion** — Anderson
   and Senesi — from club sources. **The remaining club labels in §4.1 should be re-checked
   before being used for player selection.** In particular one row was labelled "Ethan Ampadu
   (Leeds\*)" with an asterisk denoting Championship 2025/26, which I could not reconcile and
   have therefore excluded from the tables above.
8. **Whether Championship DC rates translate to the Premier League** for Hughes, Egan, O'Shea and
   Matusiwa. Directionally they should hold or improve. I found no study quantifying it.

---

## 6. Sources

**HIGH — official Premier League / official club**
- premierleague.com — *What's happening with defensive contribution points in 2026/27 Fantasy?*, **20 Jul 2026** — 10 CBIT / 12 CBIRT / 2 pts, unchanged
- premierleague.com — *What's new for 2025/26: Changes in Fantasy Premier League*, **10 Aug 2025** — forwards eligible from launch (§1.2 discrepancy)
- premierleague.com — *Who's best at earning defensive contribution points in Fantasy?*, **30 Jul 2026** — all §4.1 hit rates and DC point totals
- premierleague.com — promotion coverage (Coventry, Ipswich, Hull for 2026/27)
- nottinghamforest.co.uk, **2 Jul 2026** + mancity.com — Elliot Anderson to Manchester City
- tottenhamhotspur.com — Marcos Senesi signing (also ESPN, Sky Sports)
- FPL API `game_config` — DC point values, read live by this project **2026-08-19**

**HIGH — project's own measurement**
- `docs/wiki/data-requirements.md` §P2 — 29,338 in-store 2025-26 per-player-per-gameweek DC rows, zero for earlier seasons
- `docs/wiki/data-sources.md` §1.2, §1.3, §4 — `element_stats` vocabulary; thresholds absent from API
- `docs/wiki/provider-evaluation.md` §6.2, §8.1, §8.2 — per-source component coverage

**MEDIUM**
- Opta Analyst — *Opta Football Stats Definitions*; Stats Perform — *Opta Event Definitions* (ball recovery definition)
- Fantasy Football Scout, **23 Feb 2026** — *Opta's new 'ball recovery' definition: What does it mean for FPL?* (rewording is clarificatory only)
- Opta Analyst, **28 Nov 2025** — recoveries per 90 leaders (Anderson 8.17, Ndiaye 6.4, Saka 5.7, Mitchell 5.59, Roefs 10, Martínez 8.7)
- fploracle.team, **24 Jul 2026** — 2026/27 rule-change round-up (DC unchanged; BPS tackled-penalty removed)
- vaastav/Fantasy-Premier-League `DATA_DICTIONARY.md` — field grains (a mirror of the FPL API, not independent)
- The IX Sports and community reporting of the **20 Jan 2026** Stats Perform / Sports Reference termination (primary blog post 403s)

**LOW / not used for any claim**
- draftfantasy.com, Operation Sports, fpledits.com — corroborative only on the 10/12 thresholds. fpledits.com's DefCon page is stamped 22/08/2026 but is a tool landing page with no data behind the fetch; no claim rests on it.
