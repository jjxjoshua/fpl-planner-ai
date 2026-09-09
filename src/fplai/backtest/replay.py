"""The backtest replay harness — blueprint §7.2. This module is where
leakage is prevented *structurally*, not just by convention.

**The leakage boundary, stated as a type.** `GameweekView` is the only
object a `Strategy` ever receives. It carries:

  - `history` — every row with `round < gameweek`. Fully in the past,
    fully knowable, every column included (a past round's own `selected`,
    `total_points`, `minutes` etc. are all settled facts by the time a
    later gameweek's deadline arrives).
  - `current_attributes` — round == `gameweek` rows, but reduced to
    `ATTRIBUTE_COLUMNS` only (identity/price/position/team — facts fixed
    before kickoff, the same way a manager sees this week's prices before
    picking). Outcome columns (`total_points`, `minutes`, `selected`,
    bonus, bps, ...) for the gameweek being decided are never present in
    this object — not filtered out by convention, structurally absent
    because they were never selected into it.

    **Schedule facts joined this story** (`fixture`, `was_home`,
    `kickoff_time`, `opponent_team`): the same class as `value` and
    `position` under the criterion above — a published fixture list is
    known weeks ahead, fixed before kickoff, and settled by nothing a
    player does on the pitch. Verified live against the real store
    (179,960 rows, `vaastav_player_gameweek_stats`): all four are present
    with zero nulls, and for every `(season, round, element, fixture)` key
    `was_home`/`kickoff_time`/`opponent_team` take exactly one value —
    they are read off the schedule, never reconstructed from an outcome.
    `total_points`, `minutes`, `selected`, `bonus`, `bps`,
    `goals_scored`, `team_a_score`, `team_h_score` and every other column
    settled only by playing the fixture remain excluded, unchanged.

    **A double/triple-gameweek player still collapses to one row here**
    (see `_build_view`'s dedup). Before the schedule-facts story that was a
    true dedup — `value`/`position`/`team` don't vary by fixture within a
    round — but `fixture`/`was_home`/`kickoff_time`/`opponent_team`
    genuinely do, so `current_attributes` for a DGW player exposes only
    their earliest-`fixture`-id match this round, deterministically, not
    every fixture they play. Duplicating rows in `current_attributes` to
    fix this was rejected: `baselines.py::_candidates_from_attributes`
    iterates every row into one `PlayerCandidate` each, and
    `squad.py::build_squad` has no duplicate-id handling — a second row
    per DGW element would silently corrupt every existing baseline's
    `chosen` counter, not raise.

    **`GameweekView.fixtures`** (double-gameweek handling story) is the
    fix instead: one row per fixture this round — `fixture`, `home_team`,
    `away_team`, `kickoff_time` — built entirely from
    `current_attributes`' own pre-dedup `team`/`was_home`/`kickoff_time`
    columns (never `opponent_team`, which is a numeric FPL team CODE, not
    a name, and would need a team-identity join this module has no access
    to and does not need — a fixture's home/away NAME is already present
    on the home/away side's own rows within the same round). A caller
    wanting a DGW player's *second* fixture filters `view.fixtures` on
    `home_team == player.team OR away_team == player.team` instead of
    reading a second row off `current_attributes` — `current_attributes`
    itself stays exactly one row per element, untouched, so every existing
    baseline (`_candidates_from_attributes`, `build_squad`) is unaffected.
    A blank gameweek (a team with zero fixtures this round) is the mirror
    image: that team's rows are simply absent from `current_attributes`
    for this round (the store carries no row for a player whose team has
    no fixture), and `view.fixtures` filtered on that team returns zero
    rows — `fplai.features.combine_gameweek_points_pmfs` turns an empty
    fixture-pmf list into a degenerate 0-point PMF rather than a crash or
    a silently dropped candidate (see that function's own docstring).

    Measured live against the real store, seasons 2022-23..2025-26 (this
    story's own verification, not assumed): 65 genuine team-round blanks
    across the four seasons (22/23/10/10 per season), concentrated in the
    SAME handful of late-season rounds DGWs cluster in (e.g. rounds 29/34
    in 2024-25, 31/34 in 2025-26) — rearranged fixture congestion produces
    a double for one team and a blank for another in the same round,
    which is exactly why both cases are handled by this one story.

    **`GameweekView.forward_fixtures`** (receding-horizon story, blueprint
    §6.1: solve over GW t..t+5, execute only week t) is the only way a
    `Strategy` can see anything about rounds *after* the one being decided.
    It is `dict[int, pl.DataFrame]`, one entry per round `t+1..t+H` that
    actually exists in the store (`H` = the `horizon` argument to
    `_build_view`, default `DEFAULT_FORWARD_HORIZON`), each value shaped
    exactly like `GameweekView.fixtures` above (`fixture`/`home_team`/
    `away_team`/`kickoff_time`, one row per fixture, built by the SAME
    `_build_fixtures_table` — no second fixture-table builder). This is
    deliberately a MUCH narrower window than `current_attributes`: a raw
    future round in the store carries every column round `t` does —
    `value`, `selected`, `total_points`, `minutes`, `bonus`, `bps`,
    `goals_scored`, everything — because historical data is simply what
    happened, and "future" here means "future relative to the gameweek
    being decided," not "not yet in the store." A future round's OWN
    price is genuinely unknowable at deadline `t` (prices drift
    intra-season — verified live against the real store: element 545/865,
    516/804 and 600/841 change price within one season) — unlike round
    `t`'s own price, which a manager really does see and which is exactly
    why `value` stays in `ATTRIBUTE_COLUMNS` for round `t` but must never
    cross this boundary for `t+1..t+H`. So each future round's raw frame is
    projected to exactly `FORWARD_FIXTURE_COLUMNS` — `fixture`, `team`,
    `was_home`, `kickoff_time` — BEFORE `_build_fixtures_table` ever sees
    it; `opponent_team` is also excluded here (unlike in
    `ATTRIBUTE_COLUMNS`) because `_build_fixtures_table` never uses it —
    home/away team NAMES come from `team`/`was_home` within each round, the
    same reasoning `fixtures` above already relies on. Verified live
    against the real store (`scripts/probe_store_shape.py`-style query
    pasted into this story's brief): the projected columns are exactly
    those four for a real round, and `value`/`selected`/`total_points`/
    `minutes`/`bonus`/`bps`/`goals_scored` are absent — not filtered by
    convention, never selected in.

    **Truncation, not padding or raising.** A horizon that runs past the
    season's last stored round (e.g. `t=36` with a 5-round horizon has only
    rounds 37/38 in the store; `t=38` has none) simply yields a shorter —
    possibly empty — dict. A genuine mid-season ingestion gap (module
    docstring of `fplai.backtest.data`, e.g. 2022-23's missing round 7)
    behaves the same way: that one round is absent from the dict and every
    other round in the horizon is still present. Never an error, never a
    fabricated row — the same "return what exists" stance `SeasonData.
    rounds()` already takes for `history`/`current_attributes`.

    **DGWs and blanks in the forward window are exactly as real as this
    round's** — verified live for 2025-26 round 33 (13 fixtures, 6 teams
    doubled: Bournemouth, Brighton, Burnley, Chelsea, Leeds, Man City) and
    round 34 (7 fixtures, the same 6 teams blank) — because `forward_
    fixtures[r]` is built by the identical `_build_fixtures_table` used for
    `fixtures`, which already handles both truthfully (one row per
    fixture; a DGW team appears in two rows; a blank team is simply
    absent). No new fixture-table logic exists for this; only the
    projection feeding it differs (schedule columns from a future round
    instead of round `t`).

    **Why this unlocks story S3, not why it does S3's job.** The rest-day
    feature `days_since_team_previous_fixture` is computed from the
    previous kickoff in whatever frame it is handed; with `history` frozen
    at `round < t`, a t+3 prediction measured 28.0 days where the true
    forward-schedule value is 3.75 — it had no visibility into the
    intervening rounds' kickoffs at all. `forward_fixtures` is what makes
    those intervening kickoff times reachable in the first place; fixing
    that feature to actually use them is S3's work, deliberately not
    attempted here.

    **`GameweekView.incoming_state`** (stateful replay, story S6) is the
    only way a `Strategy` sees anything about the manager's OWN squad, bank
    or free-transfer count carried over from a gameweek that has already
    been decided. It is `SquadState | None`, `None` in stateless mode —
    `SeasonReplay.run`'s default (`initial_state=None`) keeps every
    existing baseline byte-for-byte unchanged, and stateless mode never even
    calls `fplai.backtest.rules.transfer_rules_for_season`, so it keeps
    working for every season this harness supports, including the four
    (2019-20..2022-23) with no sourced `TransferRules`. It is NOT a leakage
    surface: it carries only what the manager already knows standing at
    gameweek `t`'s own deadline — their own 15 player ids, what they PAID
    for each (never what they are worth today), their bank, and their
    free-transfer count — nothing read from the store, and no fact about
    the gameweek being decided for. It is built entirely from a PAST
    `Decision` — this replay's own strategy output for gameweek `t-1`,
    threaded forward by `SeasonReplay.run` strictly after that gameweek's
    `decide()` had already returned (the same ordering guarantee that
    protects `outcome_rows`, described above, protects state threading too:
    a decision is final before anything is built from it, whether that is
    the score or the next gameweek's incoming state).

    **A held player's purchase price is carried forward unchanged, never
    reset to a later gameweek's price** — the entire reason purchase prices
    are tracked at all is that a player still owned was not re-bought this
    week; story S7 uses that original price to compute FPL's selling-price
    rule (profit on a price rise is halved, a price drop is not — see
    `_sell_price`). A newly-appearing element (absent from the previous
    state's ids) gets THIS gameweek's own price as its purchase price —
    that is genuinely what acquiring it now costs. Free transfers accumulate
    by `TransferRules.free_transfers_per_gameweek` each gameweek, capped at
    `TransferRules.max_banked_transfers`; a season's
    `free_transfer_overrides_dict` (e.g. 2025-26's GW16 AFCON top-up) TOPS
    UP to that value rather than adding to it (`docs/wiki/transfer-rules.md`
    — "topped up to the maximum possible number of five", not "+5").
    **Free transfers ARE now decremented, by story S7** (`_advance_state`'s
    `remaining = max(0, previous.free_transfers - n_transfers)`, decision
    D7) — S6 shipped the ledger with no concept of a transfer being spent;
    that accounting, plus hit costs and the selling-price rule itself, are
    what S7 added (decisions D2/D3/D4/D7), on top of S6's unspent
    threading. `bank_tenths` is now moved incrementally by the transfer cash
    flow itself (D3: previous bank plus each sale's `_sell_price`, minus
    each purchase's current price) rather than recomputed from `SquadRules.
    budget_tenths` minus the squad's purchase prices — S6's formula
    implicitly assumed a sold player always returns exactly what was paid
    for it, which is no longer true the instant a selling-price haircut is
    real. A held player (no transfer) never touches bank at all, which is
    exactly S6's zero-transfer case, so nothing about a season with no
    transfers this story can construct changes.

    `SquadState` deliberately carries no method for computing a transfer
    from two states — diffing `element_ids` against a previous `SquadState`
    to infer "N players changed this week" is exactly the inference D6
    rejects; `Decision.transfers_in`/`transfers_out` are declared by the
    strategy and validated against the ledger, never inferred from a diff.
    This module threads the ledger forward, it does not interpret it.

`SeasonReplay.run()` calls `strategy.decide(view)` and only constructs the
outcome frame (`rows_for_round`) *after* `decide()` has returned — the
outcome data for gameweek t does not exist in any variable, let alone get
passed to the strategy, until the strategy's decision for t is already
final. A strategy has no method, parameter, or attribute through which it
could reach `SeasonData` or the store directly; `SeasonReplay` never hands
either to it.

**Scoring sums stored `total_points`, never re-derived** (blueprint §7.2 —
scoring rules drift every season; re-deriving would silently apply today's
rules to old data). Captaincy doubles (falling back to the vice-captain if
the captain didn't play, standard FPL behaviour); bench order and autosubs
are simulated from stored `minutes` only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Protocol

import polars as pl

from fplai.backtest.data import SeasonData, rows_before_round, rows_for_round
from fplai.backtest.rules import SquadRules, TransferRules, transfer_rules_for_season
from fplai.backtest.squad import PlayerCandidate, Squad, validate_squad

# Facts fixed before kickoff — never an outcome of the gameweek being
# decided for. `current_attributes` in GameweekView is reduced to exactly
# these columns; nothing else from round == t ever reaches a strategy.
# `fixture`/`was_home`/`kickoff_time`/`opponent_team` are the published
# schedule, known weeks ahead the same way `value` (price) is known before
# the deadline — see module docstring for the verification and the
# DGW caveat this widening introduces.
ATTRIBUTE_COLUMNS = (
    "element",
    "name",
    "position",
    "team",
    "value",
    "fixture",
    "was_home",
    "kickoff_time",
    "opponent_team",
)

# `GameweekView.fixtures`' schema (double-gameweek handling story) — see
# `_build_fixtures_table` for how it is populated by `_build_view`.
_FIXTURES_SCHEMA = {"fixture": pl.Int64, "home_team": pl.Utf8, "away_team": pl.Utf8, "kickoff_time": pl.Utf8}

# The ONLY columns a future round (t+1..t+H) may be projected to before
# `_build_fixtures_table` sees it — schedule facts only, never
# `ATTRIBUTE_COLUMNS` (which carries `value`, legitimate for round `t` but
# never for a future one — see module docstring, "GameweekView.
# forward_fixtures"). Deliberately excludes `opponent_team` too:
# `_build_fixtures_table` never reads it (home/away NAMES come from
# `team`/`was_home`), so there is nothing to gain and one more numeric
# FPL-team-code column to accidentally expose.
FORWARD_FIXTURE_COLUMNS = ("fixture", "team", "was_home", "kickoff_time")

# blueprint §6.1: "solve over GW t..t+5, execute only week t" — the
# receding horizon's own length. A caller-supplied default, not a
# hardcoded fact about the harness (CLAUDE.md rule 4); see `_build_view`'s
# `horizon` parameter.
DEFAULT_FORWARD_HORIZON = 5


@dataclass(frozen=True)
class SquadState:
    """The manager's own ledger, carried from one gameweek's `Decision` into
    the next gameweek's `GameweekView.incoming_state` — story S6 (stateful
    replay, part 1). See module docstring, "GameweekView.incoming_state",
    for why this is not a leakage surface and exactly what it does and does
    not track (no transfer count, no hit cost, no selling-price haircut —
    all S7).

    Tuple-of-tuples for the dict-like purchase-price map, the same
    convention `SquadRules.squad_composition_dict` already uses, so this
    stays a frozen, hashable snapshot rather than holding a mutable `dict`
    directly."""

    element_ids: tuple[int, ...]  # the 15 squad element ids, sorted
    purchase_prices: tuple[tuple[int, int], ...]  # element -> price PAID, FPL x10 tenths
    bank_tenths: int
    free_transfers: int

    @property
    def purchase_prices_dict(self) -> dict[int, int]:
        return dict(self.purchase_prices)


def free_build_state(rules: SquadRules) -> SquadState:
    """S9, D4 — the bootstrap `SquadState` for a season's very first
    decision, where no squad yet exists to hold. An EMPTY ledger: zero
    element ids, zero purchase prices, the FULL budget banked, zero free
    transfers (a genuinely fresh manager has no prior transfer market to
    have banked anything into — the first real free transfer accrues once
    normally, at the gameweek that follows, via `_advance_state`'s own
    `+per_gameweek` step).

    Named here, not invented ad hoc by each caller, so a caller building a
    stateful `SeasonReplay.run` from scratch (S10's own harness) constructs
    the identical value `fplai.optimiser.HorizonStrategy` was proven
    against — see that class's `decide` docstring for how an EMPTY
    `SquadState` is mapped to `incoming_state=None` before ever reaching
    `optimise_multi_period` (passing it straight through would give round
    `t` fifteen phantom `in` transfers at `free_transfers=0` — fifteen
    hits, -60 points)."""
    return SquadState(element_ids=(), purchase_prices=(), bank_tenths=rules.budget_tenths, free_transfers=0)


@dataclass(frozen=True)
class HeldPlayerAttributes:
    """S9, PHASE 2, D5 -- the last-known identity/price for a HELD player
    who has fallen out of round `t`'s own `current_attributes` (a genuine
    blank gameweek for their club: no fixture, no row in the store for that
    round, per this story's probe evidence). FPL never removes a blanked
    player from a squad, so `HorizonCandidateSource` implementations and
    `SeasonReplay`'s own sell-price fallback (D6) both need SOME identity/
    price to attach to that player -- this is the one place that value is
    computed."""

    name: str
    position: str
    team: str
    price_tenths: int


def last_known_attributes(
    history: pl.DataFrame, elements: Iterable[int]
) -> dict[int, HeldPlayerAttributes]:
    """S9, PHASE 2, D5 -- THE single source of truth for "what a held
    player's identity/price last was," shared by every caller that needs
    it (`ModelStackStrategy.horizon_candidates`, `TrailingProxyHorizonSource.
    horizon_candidates`, and `SeasonReplay.run`'s own D6 sell-price
    fallback) so the ledger and the model can never read two different
    numbers for the same player's last-known price.

    `history` is the caller's own `GameweekView.history` -- structurally
    `rows_before_round(t)` (this module's own leakage boundary, see the
    module docstring) -- so "most recent row STRICTLY BEFORE the decision
    round" is simply "the last row in `history`, sorted"; this function
    cannot reach forward into round `t` or later, because `history` itself
    was never given anything at or after `t` to read. Not a new leakage
    surface, only a read of one that already exists.

    Sorted by `["element", "round", "fixture"]` and the LAST row per
    element is kept -- the most recent gameweek this player actually had a
    stored row, tie-broken by the highest fixture id within that gameweek
    (matters only for a double-gameweek player's own final fixture that
    round; price/position/team do not vary by fixture within one round).

    An element with NO row anywhere in `history` is simply ABSENT from the
    returned dict -- never fabricated. Every caller of this function
    treats that as fatal for its own purpose (a season's first-ever
    gameweek for that element is unreachable by construction: an element
    cannot be HELD without a prior gameweek in which it was acquired), and
    each raises `OptimiserError`/`ValueError` naming the element itself
    rather than silently dropping it."""
    wanted = set(elements)
    if not wanted or history.is_empty():
        return {}
    filtered = history.filter(pl.col("element").is_in(list(wanted)))
    if filtered.is_empty():
        return {}
    latest = (
        filtered.sort(["element", "round", "fixture"])
        .group_by("element", maintain_order=True)
        .agg(
            pl.col("name").last().alias("name"),
            pl.col("position").last().alias("position"),
            pl.col("team").last().alias("team"),
            pl.col("value").last().alias("value"),
        )
    )
    out: dict[int, HeldPlayerAttributes] = {}
    for row in latest.iter_rows(named=True):
        out[int(row["element"])] = HeldPlayerAttributes(
            name=row["name"], position=row["position"], team=row["team"], price_tenths=int(row["value"])
        )
    return out


@dataclass(frozen=True)
class GameweekView:
    """The only window onto the season a `Strategy` ever receives. See
    module docstring for the leakage guarantee this type encodes."""

    season: str
    gameweek: int
    history: pl.DataFrame
    current_attributes: pl.DataFrame
    fixtures: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema=_FIXTURES_SCHEMA))
    """One row per fixture this gameweek — `fixture`/`home_team`/
    `away_team`/`kickoff_time`, sorted by `fixture`. See module docstring,
    "A double/triple-gameweek player still collapses to one row here",
    for why this exists alongside `current_attributes` rather than
    widening it. Empty (zero rows, same schema) when `current_attributes`
    is empty. Defaults to an empty frame so a caller constructing a
    `GameweekView` directly (a test fixture, e.g. `tests/test_backtest_
    baselines.py`, predates this field and does not need to know about
    it) is not forced to supply one — `_build_view` (the only path a real
    `SeasonReplay` ever uses) always populates it explicitly regardless."""

    forward_fixtures: dict[int, pl.DataFrame] = field(default_factory=dict)
    """`{round_number: fixtures_table}` for every round `t+1..t+H` that
    exists in the store, `H` given by `_build_view`'s `horizon` argument
    (default `DEFAULT_FORWARD_HORIZON`). Each table has the identical
    shape as `fixtures` above and NOTHING else — see module docstring,
    "GameweekView.forward_fixtures", for the leakage argument (schedule
    facts only, `value` and every outcome column never cross this
    boundary for a future round). Truncated, never padded, at the season
    end or across a genuine ingestion gap — a missing round is simply
    absent from the dict. Defaults to `{}` for the same reason `fixtures`
    defaults to an empty frame: a caller constructing `GameweekView`
    directly predates this field and is not forced to know about it;
    `_build_view` always populates it explicitly."""

    incoming_state: SquadState | None = None
    """The manager's own squad/bank/free-transfer ledger standing at THIS
    gameweek's deadline — `None` in stateless mode (`SeasonReplay.run`'s
    default), a `SquadState` in stateful mode. See module docstring,
    "GameweekView.incoming_state", for the leakage argument and exactly
    what S6 does and does not compute from it. Defaults to `None` for the
    same reason `fixtures`/`forward_fixtures` default the way they do: a
    caller constructing `GameweekView` directly (existing test fixtures,
    e.g. `tests/test_backtest_baselines.py`) predates this field and does
    not need to know about it; `_build_view` populates it explicitly
    whenever `SeasonReplay.run` is threading state, and leaves it `None`
    otherwise."""


@dataclass(frozen=True)
class Decision:
    squad: Squad
    xi: tuple[PlayerCandidate, ...]
    bench: tuple[PlayerCandidate, ...]
    captain: PlayerCandidate
    vice_captain: PlayerCandidate
    transfers_in: tuple[int, ...] = ()
    """Element ids bought this gameweek — story S7. Defaulted to `()` so
    every existing `Decision(...)` call site (every baseline, every test
    fixture predating S7) constructs unchanged (decision D1). Empty in
    stateless mode and for any strategy that never transfers — S7 does not
    require a `Strategy` to populate these, only makes it possible to."""
    transfers_out: tuple[int, ...] = ()
    """Element ids sold this gameweek — story S7. Same defaulting rationale
    as `transfers_in`. `SeasonReplay.run`'s stateful path validates both
    against `incoming_state`/`squad` before spending anything (decision
    D6); stateless mode (`incoming_state is None`) never looks at either
    field at all."""


class Strategy(Protocol):
    name: str

    def decide(self, view: GameweekView) -> Decision:
        """Return this gameweek's squad/XI/bench/captain, using only
        `view`. Implementations must never receive or reach for anything
        else — there is nothing else to reach for; `SeasonReplay` does not
        expose `SeasonData` or the store to a `Strategy`."""
        ...


@dataclass(frozen=True)
class GameweekResult:
    season: str
    gameweek: int
    points: int
    active_xi_ids: frozenset[int]
    autosubs: tuple[tuple[int, int], ...]  # (starter_out_id, bench_in_id)
    effective_captain_id: int | None
    transfer_hit_points: int = 0
    """The hit charge already folded into `points` above (always <= 0) —
    story S7, decision D5. Kept visible separately rather than silently
    folded so a report can distinguish "scored low" from "took a hit";
    `points` already includes it, this is not an amount to subtract again.
    Defaults to 0 so every existing `GameweekResult(...)` call site
    (stateless mode, every test fixture predating S7) constructs unchanged."""


def _build_fixtures_table(current_raw: pl.DataFrame) -> pl.DataFrame:
    """The gameweek's fixture list — one row per fixture, `home_team`/
    `away_team` (team NAME strings, matching `current_attributes.team`,
    never `opponent_team`'s numeric FPL team code) and `kickoff_time`.
    Built from `current_raw` BEFORE it is deduped to one row per element,
    so a DGW player's second fixture — already collapsed out of
    `current_attributes` — is still discoverable here. See module
    docstring, "A double/triple-gameweek player still collapses to one
    row here", for the full reasoning."""
    if current_raw.is_empty():
        return pl.DataFrame(schema=_FIXTURES_SCHEMA)
    home = (
        current_raw.filter(pl.col("was_home"))
        .select(["fixture", "team", "kickoff_time"])
        .unique(subset=["fixture"])
        .rename({"team": "home_team"})
    )
    away = (
        current_raw.filter(~pl.col("was_home"))
        .select(["fixture", "team"])
        .unique(subset=["fixture"])
        .rename({"team": "away_team"})
    )
    fixtures = home.join(away, on="fixture", how="full", coalesce=True).sort("fixture")
    return fixtures.select(["fixture", "home_team", "away_team", "kickoff_time"])


def _build_forward_fixtures(
    season_data: SeasonData, gameweek: int, horizon: int
) -> dict[int, pl.DataFrame]:
    """Rounds `gameweek+1 .. gameweek+horizon`, each projected to
    `FORWARD_FIXTURE_COLUMNS` (schedule facts only — see module docstring,
    "GameweekView.forward_fixtures") before `_build_fixtures_table` (the
    SAME builder `fixtures` above uses — no second one) turns it into a
    one-row-per-fixture table. A round absent from the store — past the
    season's last round, or a genuine mid-season ingestion gap — is simply
    absent from the returned dict; never fabricated, never an error."""
    available_rounds = set(season_data.coverage.rounds_present)
    result: dict[int, pl.DataFrame] = {}
    for future_round in range(gameweek + 1, gameweek + 1 + horizon):
        if future_round not in available_rounds:
            continue
        raw = rows_for_round(season_data, future_round).select(list(FORWARD_FIXTURE_COLUMNS))
        result[future_round] = _build_fixtures_table(raw)
    return result


def _build_view(
    season_data: SeasonData,
    gameweek: int,
    *,
    horizon: int = DEFAULT_FORWARD_HORIZON,
    incoming_state: SquadState | None = None,
) -> GameweekView:
    history = rows_before_round(season_data, gameweek)
    current_raw = rows_for_round(season_data, gameweek).select(list(ATTRIBUTE_COLUMNS))
    fixtures = _build_fixtures_table(current_raw)
    forward_fixtures = _build_forward_fixtures(season_data, gameweek, horizon)
    current = current_raw
    if not current.is_empty():
        # A double/triple-gameweek player has 2-3 rows this round.
        # `element`/`name`/`position`/`team`/`value` genuinely are constant
        # across a player's fixtures in one round, so collapsing those was
        # always a true dedup. `fixture`/`was_home`/`kickoff_time`/
        # `opponent_team` (added this story) do NOT hold that property —
        # a DGW player's two fixtures have two different opponents and
        # kickoffs by definition. Deduping to one row per element therefore
        # now discards real information for those four columns; see the
        # module docstring's DGW caveat — `baselines.py`'s
        # one-candidate-per-element assumption is why this story keeps the
        # one-row-per-element shape rather than widening it, so the choice
        # of WHICH fixture survives must be made explicit and deterministic
        # rather than left to `.unique()`'s input order.
        #
        # `.unique()`'s row ORDER, not its content, is what caused
        # `scripts/run_baselines.py --seed 42` to produce 1907/1909/1887
        # across three identical runs (blueprint §7.2 gate-repair session,
        # s003). polars' `maintain_order` defaults to False; verified live
        # against data/store/: five repeated calls on the same real 692-row
        # gameweek-1 slice returned five completely different row orders —
        # not a rare edge case, every call. Sorting by `["element",
        # "fixture"]` before `.unique(subset=["element"], keep="first")`
        # makes both the row kept per element (lowest `fixture` id — the
        # gameweek's earlier fixture for a DGW player) and the final row
        # order pure functions of content, not of store read order.
        current = (
            current.sort(["element", "fixture"])
            .unique(subset=["element"], keep="first")
            .sort("element")
        )
    return GameweekView(
        season=season_data.season,
        gameweek=gameweek,
        history=history,
        current_attributes=current,
        fixtures=fixtures,
        forward_fixtures=forward_fixtures,
        incoming_state=incoming_state,
    )


def simulate_autosubs(
    xi: tuple[PlayerCandidate, ...],
    bench: tuple[PlayerCandidate, ...],
    points_by_element: dict[int, int],
    minutes_by_element: dict[int, int],
    rules: SquadRules,
) -> tuple[dict[int, PlayerCandidate], tuple[tuple[int, int], ...]]:
    """Simplified FPL autosub logic: GK-for-GK first, then outfield bench
    in order, each substitution only applied if it keeps the XI within
    `rules.xi_bounds`. Not a byte-for-byte reproduction of FPL's own
    algorithm (real FPL's has additional edge cases around simultaneous
    multi-position eligibility this backtest does not need), but the same
    shape: a non-playing (0-minute) starter is replaced by the first
    eligible (played, unused, formation-legal) bench player in bench order.
    """
    xi_bounds = rules.xi_bounds_dict
    active: dict[int, PlayerCandidate] = {p.id: p for p in xi}
    counts: Counter[str] = Counter(p.position for p in xi)
    used_bench_ids: set[int] = set()
    autosub_log: list[tuple[int, int]] = []

    def minutes_of(pid: int) -> int:
        return minutes_by_element.get(pid, 0)

    starting_gk = next((p for p in xi if p.position == "GK"), None)
    bench_gk = next((p for p in bench if p.position == "GK"), None)
    if (
        starting_gk is not None
        and minutes_of(starting_gk.id) == 0
        and bench_gk is not None
        and minutes_of(bench_gk.id) > 0
    ):
        del active[starting_gk.id]
        active[bench_gk.id] = bench_gk
        used_bench_ids.add(bench_gk.id)
        autosub_log.append((starting_gk.id, bench_gk.id))

    bench_outfield = [p for p in bench if p.position != "GK"]
    for starter in xi:
        if starter.position == "GK" or starter.id not in active:
            continue
        if minutes_of(starter.id) > 0:
            continue
        for sub in bench_outfield:
            if sub.id in used_bench_ids or minutes_of(sub.id) == 0:
                continue
            new_counts = counts.copy()
            new_counts[starter.position] -= 1
            new_counts[sub.position] += 1
            lo_out, _hi_out = xi_bounds[starter.position]
            _lo_in, hi_in = xi_bounds[sub.position]
            if new_counts[starter.position] < lo_out or new_counts[sub.position] > hi_in:
                continue
            del active[starter.id]
            active[sub.id] = sub
            used_bench_ids.add(sub.id)
            counts = new_counts
            autosub_log.append((starter.id, sub.id))
            break

    return active, tuple(autosub_log)


def score_gameweek(
    decision: Decision,
    outcome_rows: pl.DataFrame,
    rules: SquadRules,
    *,
    season: str,
    gameweek: int,
    hit_points: int = 0,
    validate_budget: bool = True,
) -> GameweekResult:
    """Score one gameweek's decision against that gameweek's actual
    outcomes. `outcome_rows` must be round == gameweek only; the caller
    (`SeasonReplay.run`) is responsible for never constructing it before
    `Strategy.decide` has already returned.

    `hit_points` (story S7, decision D5) is an already-signed (<= 0) charge
    the CALLER computed from `TransferRules.hit_cost` and this gameweek's
    transfer count — `score_gameweek` stays dumb and stateless, it has no
    concept of `SquadState` or `TransferRules` and never computes this
    itself (D5). Defaults to 0, which is exactly stateless mode's and every
    pre-S7 test's behaviour: unchanged.

    `validate_budget` (S9, PHASE 2, D7) defaults to `True`, unchanged
    behaviour for every existing call site. `validate_squad`'s flat
    `total_price() > rules.budget_tenths` check is a STATELESS-mode
    invariant — it assumes a fresh `rules.budget_tenths` build every week,
    which is false the instant a squad is genuinely HELD across gameweeks:
    a real GW1 squad, never transferred, can appreciate past the nominal
    budget as prices drift (this story's probe: 96.0m of a 100.0m cap by
    2025-26's season end, zero transfers). `SeasonReplay.run` passes
    `validate_budget=(state is None)` — stateless mode keeps this check
    exactly as before; stateful mode does not, because overspend is
    already impossible there by two INDEPENDENT mechanisms this flag does
    not touch: `_advance_state`'s own negative-bank raise (a transfer that
    cannot be paid for is rejected before a `SquadState` is ever built),
    and `fplai.optimiser.optimise_multi_period`'s `bank_r` variable's own
    `lb=0`. Every OTHER `validate_squad` check — size, duplicate ids,
    position composition, club cap — still runs in both modes, unaffected
    by this flag."""
    validate_squad(decision.squad, rules, check_budget=validate_budget)

    if outcome_rows.is_empty():
        points_by_element: dict[int, int] = {}
        minutes_by_element: dict[int, int] = {}
    else:
        totals = outcome_rows.group_by("element").agg(
            pl.col("total_points").sum().alias("total_points"),
            pl.col("minutes").sum().alias("minutes"),
        )
        points_by_element = dict(zip(totals["element"].to_list(), totals["total_points"].to_list()))
        minutes_by_element = dict(zip(totals["element"].to_list(), totals["minutes"].to_list()))

    active, autosub_log = simulate_autosubs(
        decision.xi, decision.bench, points_by_element, minutes_by_element, rules
    )
    base_points = sum(points_by_element.get(pid, 0) for pid in active)

    captain_minutes = minutes_by_element.get(decision.captain.id, 0)
    vice_minutes = minutes_by_element.get(decision.vice_captain.id, 0)
    if captain_minutes > 0:
        effective_captain_id: int | None = decision.captain.id
    elif vice_minutes > 0:
        effective_captain_id = decision.vice_captain.id
    else:
        effective_captain_id = None
    captain_bonus = points_by_element.get(effective_captain_id, 0) if effective_captain_id is not None else 0

    return GameweekResult(
        season=season,
        gameweek=gameweek,
        points=base_points + captain_bonus + hit_points,
        active_xi_ids=frozenset(active.keys()),
        autosubs=autosub_log,
        effective_captain_id=effective_captain_id,
        transfer_hit_points=hit_points,
    )


def _sell_price(purchase_price: int, current_price: int) -> int:
    """FPL's selling-price rule — story S7, decision D2. Integer tenths
    throughout, matching every other price in this module. A price that has
    fallen or held sells at its current value in full (`element_sell_at_
    purchase_price = False` plus a price DROP is never subject to the
    sell-on fee — only profit is). A price that has risen has half the
    profit clawed back by the live `game_config`'s `transfers_sell_on_fee =
    0.5`, rounded DOWN (floor division on a positive int, per D2's own
    worked examples): purchase 50, current 53 -> profit 3 -> half 1.5 ->
    floor 1 -> sell 51. Purchase 50, current 47 -> sell 47 (the drop is
    borne in full)."""
    if current_price <= purchase_price:
        return current_price
    profit = current_price - purchase_price
    return purchase_price + profit // 2


def _validate_transfer_continuity(decision: Decision, previous: SquadState) -> None:
    """Story S7, decision D6 — this story's attack surface. Every id in
    `decision.transfers_out` must be a player the manager actually held
    coming into this gameweek (`previous.element_ids`); every id in
    `decision.transfers_in` must actually be in the resulting squad; and
    the resulting squad must equal exactly `previous.element_ids -
    transfers_out + transfers_in` — no untracked substitution slipping in
    beside the declared transfers. Any violation raises `ValueError` naming
    the offending ids, never silently accepted or inferred."""
    previous_ids = set(previous.element_ids)
    squad_ids = decision.squad.ids()
    out_ids = set(decision.transfers_out)
    in_ids = set(decision.transfers_in)

    not_owned = out_ids - previous_ids
    if not_owned:
        raise ValueError(
            f"decision.transfers_out names id(s) not held coming into this gameweek: "
            f"{sorted(not_owned)}"
        )
    not_in_resulting_squad = in_ids - squad_ids
    if not_in_resulting_squad:
        raise ValueError(
            f"decision.transfers_in names id(s) not present in decision.squad: "
            f"{sorted(not_in_resulting_squad)}"
        )
    expected_ids = (previous_ids - out_ids) | in_ids
    if expected_ids != squad_ids:
        raise ValueError(
            "decision.squad does not equal previous_ids - transfers_out + transfers_in "
            f"exactly: expected {sorted(expected_ids)}, got {sorted(squad_ids)}"
        )


def _advance_state(
    decision: Decision,
    previous: SquadState,
    rules: SquadRules,
    transfer_rules: TransferRules,
    next_gameweek: int,
    *,
    outgoing_current_prices: dict[int, int],
) -> SquadState:
    """Thread `previous` (incoming at gameweek `t`) into the `SquadState`
    for gameweek `next_gameweek`, using `decision` (gameweek `t`'s already-
    final `Decision`, already validated for continuity by the caller —
    decision D6) — story S6's threading, now spending the ledger (story S7,
    decisions D2/D3/D7).

    Held players (present in BOTH `previous.element_ids` and `decision.
    squad`) keep their OLD purchase price unchanged (D8); a genuinely new
    (`transfers_in`) element gets THIS gameweek's own price, read off
    `decision.squad`'s own `PlayerCandidate.price` — the same "what the
    manager sees today" price `HoldStrategy` and every baseline already use.

    Bank moves by exactly the transfer cash flow (D3): `previous.bank_
    tenths` plus each `transfers_out` id's `_sell_price` (using `previous`'s
    OWN recorded purchase price and `outgoing_current_prices` — the
    CALLER's job to supply, since a sold player is no longer in `decision.
    squad` and so has no `PlayerCandidate` to read a current price from)
    minus each `transfers_in` id's price. A held player never touches bank
    at all, matching S6's original invariant for the zero-transfer case.
    Deliberately NOT `rules.budget_tenths - sum(new_prices.values())` (S6's
    formula) — that formula implicitly assumes a sold player always returns
    exactly what was paid for it, which stopped being true the moment a
    real selling-price haircut exists; see D3's own worked example versus
    that formula for why they now disagree the instant a price has risen.
    Raises `ValueError` if the resulting bank would go negative (D3) —
    never clamped to zero, which would silently let a strategy buy a squad
    it cannot actually pay for.

    Free transfers are now SPENT, not just advanced (D7): `remaining =
    max(0, previous.free_transfers - n_transfers)` where `n_transfers =
    len(decision.transfers_out)` (equal to `len(decision.transfers_in)` —
    guaranteed by `_validate_transfer_continuity` given the squad stays
    exactly `rules.squad_size` every gameweek). The next gameweek's normal
    +1/gw advance, cap, and `free_transfer_overrides` top-up all apply to
    `remaining`, not to the unspent `previous.free_transfers`.

    **Footnote (S9, D4) — the free build is the one legitimate exception**
    to "`len(transfers_out) == len(transfers_in)`" above: a `Decision`
    synthesised from a free build (`previous = free_build_state(rules)`,
    zero held elements) legitimately carries `transfers_out=()` against a
    full-squad `transfers_in` (D4's own synthesis in `fplai.optimiser.
    HorizonStrategy.decide`) — `n_transfers` is still correctly `0` in that
    case (it is read from `transfers_out`, never `transfers_in`), which is
    exactly what makes a free build cost no hit. The arithmetic above is
    unchanged for this case; only the usual equal-length assumption does
    not hold."""
    previous_prices = previous.purchase_prices_dict
    squad_price_by_id = {p.id: p.price for p in decision.squad.players}
    new_prices = {p.id: previous_prices.get(p.id, p.price) for p in decision.squad.players}

    n_transfers = len(decision.transfers_out)
    bank_tenths = previous.bank_tenths
    for out_id in decision.transfers_out:
        purchase_price = previous_prices[out_id]
        current_price = outgoing_current_prices[out_id]
        bank_tenths += _sell_price(purchase_price, current_price)
    for in_id in decision.transfers_in:
        bank_tenths -= squad_price_by_id[in_id]
    if bank_tenths < 0:
        raise ValueError(
            f"unaffordable transfer: bank would go to {bank_tenths / 10:.1f}m "
            f"(transfers_in={decision.transfers_in}, transfers_out={decision.transfers_out})"
        )

    remaining_free_transfers = max(0, previous.free_transfers - n_transfers)
    advanced_free_transfers = min(
        remaining_free_transfers + transfer_rules.free_transfers_per_gameweek,
        transfer_rules.max_banked_transfers,
    )
    override = transfer_rules.free_transfer_overrides_dict.get(next_gameweek)
    free_transfers = advanced_free_transfers if override is None else max(advanced_free_transfers, override)

    return SquadState(
        element_ids=tuple(sorted(new_prices)),
        purchase_prices=tuple(sorted(new_prices.items())),
        bank_tenths=bank_tenths,
        free_transfers=free_transfers,
    )


class SeasonReplay:
    """Iterates a season gameweek by gameweek, handing a `Strategy` only a
    `GameweekView` for each deadline, then scoring against that gameweek's
    real outcomes. See module docstring for the leakage guarantee."""

    def __init__(self, season_data: SeasonData, rules: SquadRules):
        self.season_data = season_data
        self.rules = rules

    def run(self, strategy: Strategy, initial_state: SquadState | None = None) -> list[GameweekResult]:
        """`initial_state=None` (the default) is stateless mode: every
        `GameweekView.incoming_state` is `None` and this method's behaviour
        is byte-for-byte what it was before story S6 — `SeasonReplay` never
        calls `transfer_rules_for_season` on this path, so every season this
        harness has ever supported (including the four with no sourced
        `TransferRules`) keeps working unchanged (decision D5).

        Passing `initial_state` switches to stateful mode: `SquadState` is
        threaded gameweek to gameweek per `_advance_state`, which now SPENDS
        the ledger (story S7 — decisions D2/D3/D4/D6/D7; S6 only carried it
        forward unspent). This calls `transfer_rules_for_season(self.
        season_data.season)` once, up front — it raises `KeyError` for a
        season with no sourced `TransferRules`, exactly as calling it
        directly would; stateful mode is simply unavailable for those
        seasons (decision D5), not silently degraded.

        Continuity is validated, not inferred (D6): every stateful-mode
        gameweek's `decision.transfers_in`/`transfers_out` are checked
        against `state` (the incoming ledger) and `decision.squad`
        immediately after `decide()` returns, and BEFORE that gameweek is
        scored or the ledger advanced — a decision transferring a player it
        does not own raises before anything downstream sees it. The hit
        charge (D4/D5) is computed here, the only caller of `score_
        gameweek`, from `state.free_transfers` (the count standing at THIS
        gameweek's deadline, before this gameweek's spend) and `transfer_
        rules.hit_cost` (already negative) — `score_gameweek` itself stays
        dumb and stateless and never computes it."""
        results: list[GameweekResult] = []
        rounds = self.season_data.rounds()
        transfer_rules = transfer_rules_for_season(self.season_data.season) if initial_state is not None else None
        state = initial_state
        for idx, gameweek in enumerate(rounds):
            view = _build_view(self.season_data, gameweek, incoming_state=state)
            decision = strategy.decide(view)

            hit_points = 0
            if state is not None:
                assert transfer_rules is not None  # state is not None implies stateful mode was entered
                _validate_transfer_continuity(decision, state)
                n_transfers = len(decision.transfers_out)
                paid = max(0, n_transfers - state.free_transfers)
                hit_points = transfer_rules.hit_cost * paid  # hit_cost is already negative (D4)

            # Outcome data is only constructed now, strictly after the
            # strategy's decision for this gameweek is final.
            outcome_rows = rows_for_round(self.season_data, gameweek)
            result = score_gameweek(
                decision,
                outcome_rows,
                self.rules,
                season=self.season_data.season,
                gameweek=gameweek,
                hit_points=hit_points,
                # D7: the flat budget cap is a stateless-mode invariant only
                # (score_gameweek's own docstring) -- a stateful season's
                # held squad legitimately appreciates past it.
                validate_budget=(state is None),
            )
            results.append(result)
            if state is not None:
                assert transfer_rules is not None
                next_gameweek = rounds[idx + 1] if idx + 1 < len(rounds) else gameweek + 1
                if decision.transfers_out:
                    price_rows = view.current_attributes.filter(
                        pl.col("element").is_in(list(decision.transfers_out))
                    )
                    outgoing_current_prices = dict(
                        zip(price_rows["element"].to_list(), price_rows["value"].to_list())
                    )
                    missing = set(decision.transfers_out) - set(outgoing_current_prices)
                    if missing:
                        # D6 (S9, PHASE 2): a blanked outgoing player has no
                        # row in THIS round's current_attributes at all (their
                        # club had no fixture) -- fall back to the same
                        # last-known-price signal D5's held-player backfill
                        # uses (last_known_attributes, THE single source of
                        # truth for "last known price"), rather than raising
                        # for a case this story's probe evidence showed
                        # firing twice in nine real rounds. Still raises,
                        # naming the ids, if even that fallback has nothing.
                        fallback = last_known_attributes(view.history, missing)
                        for element in missing:
                            if element in fallback:
                                outgoing_current_prices[element] = fallback[element].price_tenths
                        still_missing = set(decision.transfers_out) - set(outgoing_current_prices)
                        if still_missing:
                            raise ValueError(
                                f"transferred-out id(s) have no current-gameweek price and no "
                                f"prior history row to sell at: {sorted(still_missing)}"
                            )
                else:
                    outgoing_current_prices = {}
                state = _advance_state(
                    decision,
                    state,
                    self.rules,
                    transfer_rules,
                    next_gameweek,
                    outgoing_current_prices=outgoing_current_prices,
                )
        return results
