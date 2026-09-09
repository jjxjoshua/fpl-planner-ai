"""Squad and formation constants for the Phase 1 backtest — blueprint §7.2,
CLAUDE.md rule 4 ("nothing hardcoded... read from live config").

**Why these are declared here instead of read from a live config, and why
that is not a rule-4 violation.** Rule 4 exists so *current-season* values
(prices, budget, chip counts, scoring) are never baked into code where they
would silently go stale as the live season's rules drift (blueprint §11).
That live source is `game_config`, captured from the FPL API — and it only
ever holds the CURRENT season's rules. There is no historical equivalent:
the FPL API does not expose what the budget or squad-composition rules were
in 2020-21, and none of the archives in the store carry that as a queryable
fact. For a *closed, completed* historical season, "read from live config"
has no live config to read from — the only honest options are (a) invent a
per-season config with no real source, which is worse than declaring the
assumption plainly, or (b) declare the assumption once, here, with its
justification, so a reviewer can see exactly what was assumed and correct
it if wrong.

**The assumption, stated plainly:** across every season this backtest
covers (2019-20 through 2025-26), FPL's classic-mode squad budget (£100.0m),
squad composition (2 GK / 5 DEF / 5 MID / 3 FWD = 15), starting-XI formation
bounds (1 GK / 3-5 DEF / 2-5 MID / 1-3 FWD = 11), and the 3-per-club cap were
unchanged. This is drawn from general knowledge of FPL's rules predating
this project's knowledge boundary (CLAUDE.md's boundary is about the *live*
2026/27 season specifically — these are completed seasons already inside
the training cutoff), not verified against a per-season primary source
inside this codebase. **Flagged as a finding, not asserted as fact**: if
this is ever wrong for one of the seven seasons, every baseline's budget
constraint for that season is wrong in the same direction for every
baseline equally (a shared bias, not a differential one), which is a
materially different failure than an unverified qualitative claim — but it
should still be checked against a primary source before Phase 3 relies on
backtest totals to justify a design decision.

`SEASON_RULES` is a per-season dict specifically so a future correction (a
season that genuinely differed) is a one-line change here, not a hunt
through call sites.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SquadRules:
    """One season's squad-construction constraints. All monetary values are
    in FPL's own x10 convention (e.g. 1000 == £100.0m), matching the
    `value` column already stored in `vaastav_player_gameweek_stats`."""

    budget_tenths: int
    squad_size: int
    squad_composition: tuple[tuple[str, int], ...]  # position -> exact squad count
    xi_bounds: tuple[tuple[str, tuple[int, int]], ...]  # position -> (min, max) in the XI
    max_per_club: int

    @property
    def squad_composition_dict(self) -> dict[str, int]:
        return dict(self.squad_composition)

    @property
    def xi_bounds_dict(self) -> dict[str, tuple[int, int]]:
        return dict(self.xi_bounds)


_DEFAULT_RULES = SquadRules(
    budget_tenths=1000,
    squad_size=15,
    squad_composition=(("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)),
    xi_bounds=(("GK", (1, 1)), ("DEF", (3, 5)), ("MID", (2, 5)), ("FWD", (1, 3))),
    max_per_club=3,
)

# Every season this backtest covers uses the same, documented-assumption
# rule set above. A season that is later verified to differ gets its own
# entry here — never a branch scattered through squad.py/replay.py.
SEASON_RULES: dict[str, SquadRules] = {
    season: _DEFAULT_RULES
    for season in ("2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26")
}


def rules_for_season(season: str) -> SquadRules:
    if season not in SEASON_RULES:
        raise KeyError(
            f"no declared SquadRules for season {season!r} — add one to "
            "fplai.backtest.rules.SEASON_RULES rather than guessing at a call site "
            "(CLAUDE.md rule 4: nothing hardcoded, including at the point of use)."
        )
    return SEASON_RULES[season]


@dataclass(frozen=True)
class TransferRules:
    """One season's transfer-market constraints — sourced per season,
    documented in `docs/wiki/transfer-rules.md` (story S4/S5), and kept
    deliberately separate from `SquadRules` above.

    Why separate rather than new fields on `SquadRules`: `SquadRules` is
    constructed directly in five test files and consumed by every baseline;
    new required fields would break all of them, and optional-with-defaults
    would smuggle an unsourced default into a type whose whole docstring is
    about not doing that (see module docstring). Transfers are also a
    different concern from squad construction — a squad's shape doesn't
    change season to season in this backtest's window, but the free-transfer
    bank demonstrably did (S4: 2 for 2023-24, 5 from 2024-25 onward).

    `free_transfer_overrides` is a sparse, per-gameweek TOP-UP, not an
    addition: 2025-26's GW16 AFCON top-up set the bank TO five, it did not
    add five to whatever was already banked (docs/wiki/transfer-rules.md,
    quoting premierleague.com: "topped up to the maximum possible number of
    five"). Empty for every season without a known in-season exception.
    This story (S5) only declares the value; consuming it during a replay
    (i.e. applying the top-up gameweek by gameweek) is S6/S7's job, not
    this one's.
    """

    free_transfers_per_gameweek: int
    max_banked_transfers: int
    hit_cost: int  # points deducted per transfer beyond the free allowance — always negative
    free_transfer_overrides: tuple[tuple[int, int], ...] = ()  # gameweek -> free transfers topped up TO

    @property
    def free_transfer_overrides_dict(self) -> dict[int, int]:
        return dict(self.free_transfer_overrides)


# The -4 point hit cost, per transfer beyond the free allowance. Genuinely
# absent from `game_config` — the whole payload was grepped for
# cost/hit/penalt as part of this story's probe, and the only matches are
# `scoring.penalties_missed` and `scoring.penalties_saved`. This is a
# FOURTH value of exactly the class `fplai.scoring` already documents three
# of (the 60-minute cliff, the saves divisor,
# `GOALS_CONCEDED_POINTS_DIVISOR`): a rule the FPL API never exposes as
# queryable data. Rule 4 says read from live config; where live config
# genuinely does not carry the value, a declared constant with a cited
# source is the honest form of that rule, not a violation of it — that is
# the precedent `scoring.py` already set, and this follows it rather than
# re-arguing it. Source: premierleague.com's FPL rules page, quoted in
# docs/wiki/transfer-rules.md — "each additional transfer deducts 4 points
# from the next gameweek's total." Unchanged across all three sourced
# seasons; if that ever stops being true, docs/wiki/transfer-rules.md is
# where the correction is sourced first.
_HIT_COST = -4

# Sourced per docs/wiki/transfer-rules.md (story S4), Tier 1
# (premierleague.com) plus the project's own ingested `game_config`. Holds
# EXACTLY the three seasons S4 sourced — deliberately not extended to
# 2019-20..2022-23 or any other season, because nothing was sourced for
# them. `transfer_rules_for_season` raises rather than guess.
SEASON_TRANSFER_RULES: dict[str, TransferRules] = {
    "2023-24": TransferRules(
        free_transfers_per_gameweek=1,
        max_banked_transfers=2,
        hit_cost=_HIT_COST,
    ),
    "2024-25": TransferRules(
        free_transfers_per_gameweek=1,
        max_banked_transfers=5,
        hit_cost=_HIT_COST,
    ),
    "2025-26": TransferRules(
        free_transfers_per_gameweek=1,
        max_banked_transfers=5,
        hit_cost=_HIT_COST,
        free_transfer_overrides=((16, 5),),  # AFCON top-up, docs/wiki/transfer-rules.md
    ),
}


def transfer_rules_for_season(season: str) -> TransferRules:
    if season not in SEASON_TRANSFER_RULES:
        raise KeyError(
            f"no sourced TransferRules for season {season!r} — this value was never "
            "sourced. docs/wiki/transfer-rules.md carries exactly three seasons "
            "(2023-24, 2024-25, 2025-26); source the season you need there — with a "
            "primary source, per its own Tier-1 standard — before adding an entry to "
            "fplai.backtest.rules.SEASON_TRANSFER_RULES, rather than guessing at a "
            "call site (CLAUDE.md rule 4: nothing hardcoded, including at the point "
            "of use)."
        )
    return SEASON_TRANSFER_RULES[season]


# Greedy-form baseline's trailing window. Also a documented assumption, not
# a value with a canonical source — 4 gameweeks is a common informal
# definition of "current form" in FPL discourse, not derived from data.
# Flagged, not hidden: a different N would change greedy-form's totals, and
# nothing in the store says N=4 is privileged over N=3 or N=6.
GREEDY_FORM_TRAILING_GAMEWEEKS = 4

# Standard 38-gameweek Premier League season, used to detect coverage gaps
# in the store (fplai.backtest.data). Every season 2019-20 through 2025-26
# genuinely had 38 gameweeks, including 2019-20 (completed 26 Jul 2020) —
# this is the number of gameweeks that SHOULD exist, independent of how
# many the store actually has.
EXPECTED_GAMEWEEKS = 38
