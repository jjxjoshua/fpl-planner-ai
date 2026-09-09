# Transfer rules, per season — sourced `2026-09-05` (story S4)

**What this page is for.** E7 pays transfer costs that Phase 3 did not. `fplai.backtest.rules`
declares squad constraints as a documented assumption because no historical config exists; the
transfer rules need the same treatment, but with a **primary source per season**, because — unlike
the budget — they are **known to have changed inside the gate window**.

**Tier 1 = the Premier League's own site** (`premierleague.com`, `fantasy.premierleague.com`).
Everything below is Tier 1 or the project's own ingested `game_config`. Nothing here is from recall.

## The table

| Season | Free transfers / GW | Max banked | Hit cost | In-season exceptions |
|---|---|---|---|---|
| **2023-24** | 1 | **2** | −4 | none found |
| **2024-25** | 1 | **5** | −4 | none found |
| **2025-26** | 1 | **5** | −4 | **GW16 top-up to 5** (AFCON) |
| 2026-27 *(live, reference only)* | 1 | 5 | −4 | not checked — out of gate scope |

## Evidence

**The 2 → 5 change, announced 13 Aug 2024** — [premierleague.com/en/news/4058895](https://www.premierleague.com/en/news/4058895):

> "Up until now, you've only been able to bank two free transfers. Well, if you want to be patient
> and plan ahead, you can now accumulate up to FIVE free transfers."

That sentence is doing double duty: it sources **5 for 2024-25** *and* **2 for 2023-24**, since
"up until now" is the season immediately preceding it. The same article adds that banked transfers
**no longer reset to zero when a Wildcard or Free Hit is played** — chips are E9/Phase 6, but the
interaction is recorded here so it is not rediscovered.

**2025-26's mid-season top-up** — [premierleague.com/en/news/4362211](https://www.premierleague.com/en/news/4362211/all-you-need-to-know-about-changes-to-fantasy-for-202526):

> "Your total number of free transfers in Fantasy will be topped up to the maximum possible number
> of five in Gameweek 16 due to the possibility that some players will leave early for the
> tournament."

**The −4 hit cost and the 1-per-gameweek base**, from FPL's own rules page: one free transfer per
gameweek after the first deadline; each additional transfer deducts **4 points** from the next
gameweek's total; **maximum 5 stored**; a separate cap of **20 transfers** in a single gameweek,
which does not apply under Wildcard or Free Hit.

**The project's own live config**, `store.as_of("game_config", now)`, `valid_at`
`2026-09-04T23:15:01Z` — the 2026-27 season:

```
rules.max_extra_free_transfers   4      -> 1 base + 4 extra = 5 max banked
rules.transfers_cap             20
rules.transfers_sell_on_fee      0.5    -> the 50% sell-on fee
rules.element_sell_at_purchase_price  False
rules.squad_total_spend       1000
rules.squad_team_limit           3
rules.squad_squadsize           15
rules.squad_squadplay           11
```

## Four findings that outlive this story

1. **The FT bank changed inside the gate window — confirmed, not merely feared.** S4 was registered
   on the suspicion; it is real. 2023-24 ran at a max of 2, 2024-25 and 2025-26 at 5. **A single
   hardcoded value is wrong for 2023-24**, so `SquadRules` must carry this per season.
   **It does NOT bias E7's gate**, because that gate compares H=6 against H=1 *within* each season
   under identical rules — a per-season difference is shared across both arms. It does change what
   the per-season totals mean when read next to each other, which is a reporting caveat, not a
   gate defect.

2. **2025-26 carries a per-GAMEWEEK exception, and `SquadRules` is per-season.** The GW16 AFCON
   top-up is not expressible as a season constant. Either the type grows a
   sparse `free_transfer_overrides: dict[int, int]`, or the deviation is declared and ignored —
   **it must not be silently dropped.**

3. **The hit cost is genuinely absent from `game_config`.** Grepped the whole payload for
   `cost`/`hit`/`penalt`: the only matches are `scoring.penalties_missed` and
   `scoring.penalties_saved`. This is a **fourth** value of exactly the class `fplai.scoring`
   already documents three of (the 60-minute cliff, the saves divisor,
   `GOALS_CONCEDED_POINTS_DIVISOR`). Rule 4 says read from live config; where live config does not
   carry the value, the honest form is a declared constant with a cited source — which is what
   `scoring.py` already established as the pattern.

4. **`game_config` corroborates `rules.py`'s budget assumption — for 2026-27 only.**
   `squad_total_spend=1000`, `squad_team_limit=3`, `squad_squadsize=15`, `squad_squadplay=11` match
   what `fplai.backtest.rules` assumed. **This is corroboration, not verification**: the assumption
   is about 2019-20…2025-26 and this config is the *current* season's. `rules.py`'s flag stays open;
   it is now slightly better supported, which is not the same as closed.

**Directly relevant to S7** (selling-price rules), recorded here so it is sourced once:
`transfers_sell_on_fee = 0.5` with `element_sell_at_purchase_price = False` — the 50% sell-on fee
on profit is live, not disabled.
