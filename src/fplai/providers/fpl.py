"""FPL provider adapter — story 5 of E2b: port the existing FPL client
onto the interface (blueprint §12).

Wraps the existing, tested `FPLClient` (client.py — unchanged by this
story) behind the `Provider` interface (`provider_id`, `policy`,
`supports()`, `fetch()`). This is the proof story: the abstraction is
exercised against code that already works in production — the two ingest
scripts that have been writing to the bitemporal store since before this
slice existed — not against a new, unverified source. Adding a genuinely
new provider (PL API, story 6) should look like this file plus a registry
entry, with zero changes to `schemas.py`, `registry.py` or
`providers/base.py`.

Deliberately NOT rewired onto the shared `transport.HttpTransport` in this
slice. `FPLClient`'s own request loop is exactly what
`scripts/snapshot_bootstrap.py` runs on a 30-minute Task Scheduler cadence
ahead of the GW1 deadline (CLAUDE.md, "the one irreversible deadline") —
real production parquet files already exist on disk, written by that
schedule, *right now*. Rewriting `FPLClient._get`'s internals during that
window is a risk with no benefit today: the behaviour this story needs
(rate limit, cache, backoff) already exists, is already tested, and is
already running correctly. `transport.HttpTransport` exists, generalised
and independently tested, ready for the next HTTP-based adapter (PL API,
API-Football, The Odds API — stories 6/8) to build on directly instead of
writing a bespoke request loop the way `FPLClient` did. See
docs/wiki/provider-framework.md for the reasoning written out in full.

Capabilities covered here are exactly the seven writes the two existing
scripts already make: `elements`, `teams`, `events`, `chips`,
`game_config`, `game_settings` (all sliced from one `bootstrap-static/`
call) and picks (`entry/{id}/event/{gw}/picks/`). `fixtures`, `entry`
(manager profile), and `event_status` remain reachable on `FPLClient`
directly — unwrapped here, because nothing in this slice persists them to
the store; wrapping them as capabilities now would be speculative surface
with no adapter test behind it. See the session report for the explicit
scope call.

## Session s005 — an EIGHTH capability, `player.gameweek_stats@gameweek`,
## and why `event_live` finally gets wrapped

CLAUDE.md's own line 116 names the gap this session closes: "nothing
persists `event/{gw}/live/` to the store". That endpoint is per-player-
per-gameweek PERFORMANCE at exactly the grain vaastav's archive already
serves as `player.gameweek_stats@gameweek` — this is therefore a SECOND
PROVIDER of an EXISTING capability (blueprint §12.6 swappability), not a
new capability. See `fplai.schemas.CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_
STATS_GAMEWEEK]`'s docstring for the full column-alignment story and
`fplai.gameweek_stats` for the capability-level reader that unions this
provider's rows with vaastav's.

**The fixture-attribution problem, and how `_fetch_gameweek_stats`
resolves it.** `event/{gw}/live/` gives two different views of the same
gameweek, and neither alone matches vaastav's own entity key `(season,
round, element, fixture)`:

- `elements[].stats` is per-ELEMENT-per-GAMEWEEK — complete (every raw
  count present, whether or not it scored), but on a double gameweek it
  is the SUM across both fixtures and cannot be split.
- `elements[].explain` is per-FIXTURE — exact when present (`value`
  agrees with `stats` on every identifier that actually scored, verified
  live against real settled GW1, same finding `scripts/pin_dc_
  thresholds.py` already recorded for `defensive_contribution`
  specifically) — but it **lists only identifiers that scored a nonzero
  point value**. A raw count that scores exactly 0 points (0 minutes; a
  saves/goals_conceded count that floor-divides to 0; a defensive_
  contribution count below its pinned threshold) is simply ABSENT from
  `explain`, indistinguishable at that layer from "never happened".

Resolution, split by how many fixtures the element's own explain block —
or, for a genuinely unused player (empty explain), their CURRENT
bootstrap team's fixture list — names for this gameweek:

1. **Exactly one fixture (the overwhelming majority, and the ONLY case
   verified live so far — GW1 2026/27 had zero multi-fixture elements).**
   The gameweek's `stats` block IS that one fixture's complete picture,
   with no ambiguity — used wholesale. `attribution_complete=True`.
2. **Two or more fixtures (a double gameweek).** One row is still
   emitted PER FIXTURE — never dropped (CLAUDE.md lesson 2: vaastav's own
   entity key once omitted `fixture` and silently dropped 7,141 real
   double-gameweek rows; the same mistake is not repeated here by
   aggregating instead). Per fixture:
   - `total_points` is the sum of `explain`'s own per-fixture points —
     ALWAYS exact, no reconstruction needed (this is literally what
     `explain` records).
   - `minutes` and every "linear" identifier (no divisor, no threshold —
     `goals_scored`, `assists`, `clean_sheets`, `own_goals`, `yellow_
     cards`, `red_cards`, `penalties_saved`, `penalties_missed`,
     `bonus`) are exact when present in that fixture's `explain` block,
     and correctly inferred as **0** when absent — for THESE identifiers
     specifically, absence structurally cannot mean a nonzero raw count
     scored zero points, because none of them have a divisor or a
     threshold (any nonzero count scores a nonzero point value).
   - `saves`, `goals_conceded`, `defensive_contribution` are exact when
     present in that fixture's `explain` block, and **NULL** (never
     guessed as 0) when absent — a nonzero raw count for any of these
     CAN legitimately score 0 points (saves // 3, goals_conceded // 2, a
     defensive_contribution count below its pinned threshold), so
     absence from `explain` means genuinely UNKNOWN, not zero. This is
     the exact same structural argument `pin_dc_thresholds.py` already
     established for `defensive_contribution` alone, generalised here to
     every divisor/threshold identifier.
   - `bps`, `clearances_blocks_interceptions`, `recoveries`, `tackles`,
     `starts`, and the `expected_*` fields are NULL — none of these are
     ever explain identifiers at all, on a single or double gameweek, so
     there is nothing to reconstruct them from per fixture.
   - `attribution_complete=False` on every row this path produces —
     visible in the data, not just in this docstring, exactly what the
     brief that authorised this required.
   **This path is UNTESTED against real data** — no double gameweek has
   occurred in 2026/27 as of this session (verified: GW1, the only
   settled gameweek, has zero multi-fixture elements). It is exercised
   only by a synthetic, hand-built payload in `tests/test_provider_fpl.py`
   that mirrors the verified real single-fixture JSON shape, doubled.
3. **Zero fixtures (the element's team had a blank gameweek).** No row
   — there is no fixture to key the entity on, matching vaastav's own
   implicit behaviour (a blank-gameweek player has no row either, since
   the archive's own entity key also requires a real `fixture`).

**`was_home`/`opponent_team` — resolved, session s005 continued.** Closes
the leak the position/team join story flagged out of its own scope and
left open: `was_home`/`opponent_team` used to be derived from the
element's CURRENT bootstrap-static `team`, not a per-gameweek team
snapshot — for a player who has since transferred clubs, a historical
row's home/away and opponent were computed against their NEW club, not
the one they actually played for. **Verified LIVE, not latent: 2026-08-30,
8 real elements in the GW1 2026/27 store already differ between their
GW1-deadline-resolved team and their bootstrap-static team as of a later
re-ingest** (a real transfer window is open; e.g. Ethan Pinnock, resolved
Brentford at the GW1 deadline, shows as Coventry City in a later live
call) — the pre-fix code, run at that later ingest time, silently wrote
`opponent_team=4` (Brentford — the player's OWN deadline-resolved club)
and `was_home=False` onto his real GW1 row, when the true fixture
(Brentford home vs Spurs away) says `was_home=True`,
`opponent_team=19` (Spurs). Same fix as `position`/`team`: resolved from
`elements_as_of` — the SAME already-resolved, AS-OF-THE-DEADLINE snapshot
— never `bs_el`'s live team. `_resolved_team_id()` / `_fixture_home_away()`
below are the two functions this lives in; `None` (no as-of snapshot, or
this element absent from it) now correctly degrades `was_home`/
`opponent_team` to `None`, never a guess. This does not affect
`total_points` or `minutes`, which come from `explain`'s own authoritative
`fixture` id when present.

**What this fix does NOT touch: fixture ATTRIBUTION for a genuinely
UNUSED player (empty `explain`) still falls back to the CURRENT
bootstrap-static team** to find which fixture(s) that team played this
gameweek — there is no as-of-deadline FIXTURE history to look up instead
(fixtures are looked up by team id from a single live `fixtures/?event=`
call, not a bitemporal store read). A transferred player who did not
play at all this gameweek can therefore still be attributed to the wrong
fixture entirely (or none, if their new club had no fixture this
gameweek) — narrower than the was_home/opponent_team leak this session
closed (it can only ever bite a player who BOTH transferred AND did not
play), and still an open, separately-documented risk for a future
session.

**What the FPL live API structurally cannot give you, and why this
provider does not fake it.** `selected`/`value` (ownership/price) are
always NULL on this provider's rows — `event/{gw}/live/` carries neither,
and `CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK]`'s own docstring
already names this as the reason the vaastav capability exists at all.
A supplementary join against the `elements` time series (available for
2026-27 onward, since `snapshot_bootstrap.py` has captured it every 30
minutes since before GW1) COULD approximate a gameweek's price/ownership
as-of its deadline — deliberately not built here; a real bitemporal join
belongs to its own story, not a silent fallback bolted onto this one.

**`position`/`team` — resolved, session s005 continued.** Unlike
`selected`/`value`, `event/{gw}/live/` has no bearing on this gap at all
— `position`/`team` were simply never joined, even though every model in
`fplai.models` filters or groups by position, which made the union reader
unusable for the current season. `_fetch_gameweek_stats` below now takes
two OPTIONAL parameters, `elements_as_of`/`teams_as_of` — already-
resolved `elements`/`teams` snapshots, AS OF THE GAMEWEEK'S OWN DEADLINE,
never "today's" state (CLAUDE.md rule 2). This provider does NOT read the
store itself to produce them — every other provider in this codebase
(`providers/pl.py`'s own `PLProvider` is the direct precedent) takes
already-resolved reference snapshots as plain DataFrames rather than a
store handle, and this follows the same convention rather than being the
first provider to blur it. `fplai.gameweek_stats.
resolve_elements_and_teams_as_of_deadline()` is the one sanctioned way to
build them (full bitemporal reasoning lives in that module's own comment
block); `scripts/snapshot_gameweek_stats.py` is the only live caller.
Omitting either parameter (the default) reproduces exactly this
provider's pre-fix behaviour: `position`/`team` stay NULL, never guessed.

The `position` VALUE itself is read live off `bootstrap["element_types"]`
— the SAME already-fetched payload this method calls `bootstrap_static()`
for anyway, zero extra requests — never a hardcoded `{1: "GKP", ...}`
literal (CLAUDE.md rule 4): an `element_type` code with no matching entry
in that list (e.g. a future season's Assistant-Manager-only type) resolves
to `None` rather than being silently mapped onto an outfield position, the
exact failure mode the brief authorising this warned against.

**Gated on settlement, same signal `scripts/pin_dc_thresholds.py` already
uses.** `_fetch_gameweek_stats` refuses (raises `ProviderError`, not a
crash) unless `bootstrap-static()`'s own `events[gw]` reports both
`finished` and `data_checked` — `event/{gw}/live/` is not final before
then, and a `ProviderError` here is exactly what `fplai.backfill.
BackfillOrchestrator` already treats as "absent, try again later" rather
than a halt (see `backfill.py`'s own module docstring §2).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import polars as pl

from fplai.client import FPLClient
from fplai.providers.base import FetchResult, ProviderError
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    CHIP_WINDOW_SEASON,
    GAME_CONFIG_CURRENT,
    GAME_SETTINGS_CURRENT,
    GAMEWEEK_ATTRIBUTES_SEASON,
    MANAGER_PICKS_SELECTION_GAMEWEEK,
    PLAYER_ATTRIBUTES_CURRENT,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    TEAM_ATTRIBUTES_CURRENT,
    CapabilityKey,
    SchemaError,
)
from fplai.store import content_hash
from fplai.transport import RatePolicy

logger = logging.getLogger("fplai.providers.fpl")

# capability -> bootstrap-static payload key, for the "one row per record" shape
_BOOTSTRAP_RECORD_CAPABILITIES: dict[CapabilityKey, str] = {
    PLAYER_ATTRIBUTES_CURRENT: "elements",
    TEAM_ATTRIBUTES_CURRENT: "teams",
    GAMEWEEK_ATTRIBUTES_SEASON: "events",
    CHIP_WINDOW_SEASON: "chips",
}
# capability -> bootstrap-static payload key, for the "one JSON blob" shape
_BOOTSTRAP_SINGLETON_CAPABILITIES: dict[CapabilityKey, str] = {
    GAME_CONFIG_CURRENT: "game_config",
    GAME_SETTINGS_CURRENT: "game_settings",
}

_BOOTSTRAP_ENDPOINT = "bootstrap-static/"
_ENTRY_PICKS_ENDPOINT = "entry/{entry_id}/event/{event}/picks/"
_EVENT_LIVE_ENDPOINT = "event/{event}/live/"

# -- session s005: player.gameweek_stats@gameweek, the fixture-attribution
#    split — see this module's docstring for the full argument. -----------

# Absence from a fixture's `explain` block means the raw count really was
# 0 FOR THAT FIXTURE, because none of these have a divisor or a threshold
# — any nonzero count scores a nonzero point value, so a zero-point
# outcome can only mean a zero raw count. "minutes" is included: 0 minutes
# scores 0 points, and any minutes>0 always scores at least `short_play`
# (verified live, GW1: the "minutes" identifier is present in `explain`
# whenever `stats.minutes > 0`, absent only when it is exactly 0).
_LINEAR_GAMEWEEK_STAT_IDENTIFIERS: tuple[str, ...] = (
    "minutes",
    "goals_scored",
    "assists",
    "clean_sheets",
    "own_goals",
    "yellow_cards",
    "red_cards",
    "penalties_saved",
    "penalties_missed",
    "bonus",
)

# Absence from a fixture's `explain` block does NOT mean the raw count was
# 0 — a nonzero count can legitimately floor-divide (saves // 3,
# goals_conceded // 2) or fall below a pinned threshold (defensive_
# contribution) and still score exactly 0 points. Genuinely UNKNOWN per
# fixture on a double gameweek unless the identifier happens to appear;
# never guessed as 0 (this module's docstring; the identical structural
# argument `scripts/pin_dc_thresholds.py` already established for
# `defensive_contribution` alone).
_DIVISOR_OR_THRESHOLD_STAT_IDENTIFIERS: tuple[str, ...] = (
    "saves",
    "goals_conceded",
    "defensive_contribution",
)

# Present in `stats` (single-fixture path) but never an `explain`
# identifier at all, on any gameweek — nothing to reconstruct these from
# per fixture on a double gameweek, so they are always NULL there.
_STATS_ONLY_FIELDS: tuple[str, ...] = (
    "bps",
    "clearances_blocks_interceptions",
    "recoveries",
    "tackles",
    "starts",
)
_STATS_ONLY_EXPECTED_FIELDS: tuple[str, ...] = (
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
)


def _gw_settled(bootstrap: dict, gameweek: int) -> tuple[bool, str]:
    events = {e["id"]: e for e in bootstrap.get("events", [])}
    event = events.get(gameweek)
    if event is None:
        return False, f"gameweek {gameweek} not found in bootstrap-static() events list"
    finished = bool(event.get("finished"))
    data_checked = bool(event.get("data_checked"))
    return (finished and data_checked), f"finished={finished}, data_checked={data_checked}"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _element_type_labels(bootstrap: dict) -> dict[int, str]:
    """`element_type` code -> FPL's own `singular_name_short` (e.g. `1 ->
    "GKP"`), read LIVE off `bootstrap["element_types"]` — never a
    hardcoded literal (CLAUDE.md rule 4; verified live, 2026-08-30: this
    key is a real, separate top-level bootstrap-static payload entry, not
    embedded in `game_config`). An `element_type` code with no entry here
    (e.g. a future season's Assistant-Manager-only type, or bootstrap-
    static genuinely omitting the block) is a real, honest miss for the
    caller to resolve to `None` — this function never invents a label."""
    labels: dict[int, str] = {}
    for entry in bootstrap.get("element_types") or []:
        et_id, label = entry.get("id"), entry.get("singular_name_short")
        if et_id is not None and label is not None:
            labels[int(et_id)] = label
    return labels


def _elements_as_of_lookup(elements_as_of: pl.DataFrame | None) -> dict[int, dict]:
    """`elements_as_of["id"]` -> `{"element_type": ..., "team": ...}`, or
    `{}` if the caller supplied nothing (this provider's pre-fix
    behaviour — position/team stay NULL). Raises `ProviderError` (not a
    bare `KeyError` three lines deep) if a NON-empty frame is missing a
    column this join needs — `store.as_of("elements", ...)` always
    carries both (`PLAYER_ATTRIBUTES_CURRENT.required_fields`), so a miss
    here means a caller passed something else entirely."""
    if elements_as_of is None or elements_as_of.is_empty():
        return {}
    missing = [c for c in ("id", "element_type", "team") if c not in elements_as_of.columns]
    if missing:
        raise ProviderError(f"elements_as_of is missing column(s) {missing} needed for position/team resolution")
    return {
        int(row["id"]): row
        for row in elements_as_of.select("id", "element_type", "team").to_dicts()
        if row["id"] is not None
    }


def _team_name_lookup(teams_as_of: pl.DataFrame | None) -> dict[int, str]:
    """`teams_as_of["id"]` -> `teams_as_of["name"]` (FPL's own team
    display name, e.g. "Man Utd" — the same convention vaastav's archive
    uses, verified live against the real store), or `{}` if the caller
    supplied nothing. Same "raise on a malformed non-empty frame, return
    empty for an omitted one" posture as `_elements_as_of_lookup`."""
    if teams_as_of is None or teams_as_of.is_empty():
        return {}
    missing = [c for c in ("id", "name") if c not in teams_as_of.columns]
    if missing:
        raise ProviderError(f"teams_as_of is missing column(s) {missing} needed for team-name resolution")
    return {
        int(row["id"]): row["name"]
        for row in teams_as_of.select("id", "name").to_dicts()
        if row["id"] is not None and row["name"] is not None
    }


def _resolve_position_team(
    element_id: int,
    *,
    elements_as_of_by_id: dict[int, dict],
    team_name_by_id: dict[int, str],
    element_type_labels: dict[int, str],
) -> tuple[str | None, str | None]:
    """`(position, team)` for one element, AS OF the deadline the caller's
    `elements_as_of`/`teams_as_of` snapshots were resolved against — see
    this module's docstring. `None` for either half is a real, honest
    miss (element absent from the as-of snapshot; `element_type` code not
    in `element_type_labels`; resolved team id not in `team_name_by_id`),
    never guessed or backfilled from a different source."""
    resolved = elements_as_of_by_id.get(element_id)
    if resolved is None:
        return None, None
    et_code = resolved.get("element_type")
    position = element_type_labels.get(int(et_code)) if et_code is not None else None
    team_id = resolved.get("team")
    team_name = team_name_by_id.get(int(team_id)) if team_id is not None else None
    return position, team_name


def _resolved_team_id(element_id: int, elements_as_of_by_id: dict[int, dict]) -> int | None:
    """The element's own `team` id, AS OF the deadline `elements_as_of_by_id`
    was resolved against (see `resolve_elements_and_teams_as_of_deadline` /
    this module's docstring, "was_home/opponent_team — resolved, session
    s005 continued"). `None` is a real, honest miss (element absent from
    the as-of snapshot, or no snapshot supplied at all) — never
    backfilled from `bs_el`'s CURRENT bootstrap-static team, which is
    exactly the leakage this function exists to avoid reintroducing."""
    resolved = elements_as_of_by_id.get(element_id)
    if resolved is None:
        return None
    team_id = resolved.get("team")
    return int(team_id) if team_id is not None else None


def _fixture_home_away(fixture: dict, team: int | None) -> tuple[bool | None, int | None]:
    """`(was_home, opponent_team)` for `team` within `fixture`.

    `team` MUST be the element's team AS OF THE GAMEWEEK'S OWN DEADLINE
    (`_resolved_team_id`'s return value) — see this module's docstring,
    "was_home/opponent_team — resolved, session s005 continued". `None`
    (the element's deadline-resolved team is itself unknown — no
    `elements_as_of` snapshot was supplied, or this element is absent from
    it) returns `(None, None)`, never a guess derived from `bs_el`'s
    CURRENT bootstrap-static team: that current-state substitution is
    EXACTLY the leak this function was rewritten to close (verified live,
    2026-08-30: 8 real GW1 elements had already transferred clubs by the
    time of a later re-ingest, and the pre-fix code silently attributed
    their own new club as their GW1 OPPONENT on the stored row — see
    docs/wiki/runbook-ingest.md for the full live evidence)."""
    if team is None:
        return None, None
    if fixture.get("team_h") == team:
        return True, fixture.get("team_a")
    return False, fixture.get("team_h")


def _build_single_fixture_gameweek_row(
    *, season: str | None, gameweek: int, element_id: int, name: str, stats: dict, fixture: dict,
    home_away_team_id: int | None, position: str | None, resolved_team: str | None,
) -> dict:
    """The gameweek's own `stats` block IS this one fixture's complete
    picture — see this module's docstring, case 1.

    `home_away_team_id` is the element's team id AS OF THE GAMEWEEK'S OWN
    DEADLINE (`_resolved_team_id`'s return value) — used ONLY for
    `_fixture_home_away`'s was_home/opponent_team resolution (see
    "was_home/opponent_team — resolved, session s005 continued" above;
    this was the CURRENT bootstrap-static team id before that fix, which
    is exactly the leakage this parameter closes). `resolved_team` is a
    DIFFERENT thing: the `team` OUTPUT COLUMN's value (a team NAME, not
    id), already resolved by the caller the same deadline-resolved way —
    never derived from bs_el here, which would silently reintroduce the
    exact leakage this session closed."""
    was_home, opponent_team = _fixture_home_away(fixture, home_away_team_id)
    row: dict[str, Any] = {
        "season": season,
        "round": gameweek,
        "element": element_id,
        "fixture": fixture.get("id"),
        "name": name,
        "total_points": stats.get("total_points", 0),
        "minutes": stats.get("minutes", 0),
        "selected": None,
        "value": None,
        "was_home": was_home,
        "team_a_score": fixture.get("team_a_score"),
        "team_h_score": fixture.get("team_h_score"),
        "kickoff_time": fixture.get("kickoff_time"),
        "opponent_team": opponent_team,
        "attribution_complete": True,
        "position": position,
        "team": resolved_team,
    }
    for identifier in _LINEAR_GAMEWEEK_STAT_IDENTIFIERS:
        if identifier == "minutes":
            continue  # already set above
        row[identifier] = stats.get(identifier)
    for identifier in _DIVISOR_OR_THRESHOLD_STAT_IDENTIFIERS:
        row[identifier] = stats.get(identifier)
    for identifier in _STATS_ONLY_FIELDS:
        row[identifier] = stats.get(identifier)
    for identifier in _STATS_ONLY_EXPECTED_FIELDS:
        row[identifier] = _to_float(stats.get(identifier))
    return row


def _build_degraded_dgw_gameweek_row(
    *, season: str | None, gameweek: int, element_id: int, name: str, fixture: dict,
    home_away_team_id: int | None, fixture_explain: dict | None, position: str | None, resolved_team: str | None,
) -> dict:
    """One fixture's degraded row on a double gameweek — see this module's
    docstring, case 2. Only `total_points`, `minutes` and the linear
    identifiers are exact; the divisor/threshold identifiers and the
    stats-only fields are NULL, never guessed. `position`/`resolved_team`/
    `home_away_team_id` — see `_build_single_fixture_gameweek_row`'s
    docstring for why these are all deadline-resolved, never derived from
    bs_el's CURRENT bootstrap-static team; a double-gameweek row gets the
    SAME as-of-deadline resolution as a single-fixture one — it is
    per-ELEMENT-per-GAMEWEEK, not per-fixture, so both of this element's
    rows this gameweek carry the identical resolved value."""
    was_home, opponent_team = _fixture_home_away(fixture, home_away_team_id)
    explain_stats = (fixture_explain or {}).get("stats", [])
    values_by_identifier = {s["identifier"]: s.get("value") for s in explain_stats}
    total_points = sum(int(s.get("points") or 0) for s in explain_stats)

    row: dict[str, Any] = {
        "season": season,
        "round": gameweek,
        "element": element_id,
        "fixture": fixture.get("id"),
        "name": name,
        "total_points": total_points,
        "selected": None,
        "value": None,
        "was_home": was_home,
        "team_a_score": fixture.get("team_a_score"),
        "team_h_score": fixture.get("team_h_score"),
        "kickoff_time": fixture.get("kickoff_time"),
        "opponent_team": opponent_team,
        "attribution_complete": False,
        "position": position,
        "team": resolved_team,
    }
    for identifier in _LINEAR_GAMEWEEK_STAT_IDENTIFIERS:
        row[identifier] = values_by_identifier.get(identifier, 0)
    for identifier in _DIVISOR_OR_THRESHOLD_STAT_IDENTIFIERS:
        row[identifier] = values_by_identifier.get(identifier)  # None if absent -- unknown, never guessed
    for identifier in _STATS_ONLY_FIELDS:
        row[identifier] = None
    for identifier in _STATS_ONLY_EXPECTED_FIELDS:
        row[identifier] = None
    return row


def _cast_gameweek_stats_dtypes(df: pl.DataFrame) -> pl.DataFrame:
    """`selected`/`value` are ALWAYS None on every row this provider ever
    produces (see this module's docstring) — polars infers an all-None
    column as dtype `Null`, which passes `FactTableSchema.validate()` (the
    `null` family) but would fail a plain `pl.concat` against vaastav's
    real `Int64` columns of the same name in `fplai.gameweek_stats`'s
    union reader. Cast explicitly here, once, at the adapter boundary.

    `position`/`team` get the SAME treatment, for the same reason — a
    caller that omits `elements_as_of`/`teams_as_of` (or one whose lookup
    misses every element) produces an all-None batch that would otherwise
    infer as `Null`, not `Utf8`, and fail to align with vaastav's real
    String columns of the same name.

    `was_home`/`opponent_team` get the SAME treatment, session s005
    continued — since the was_home/opponent_team leakage fix, a caller
    that omits `elements_as_of` (or whose lookup misses every element,
    e.g. every element genuinely absent from a pre-deadline snapshot) now
    produces an all-None batch for these two columns too (previously
    always populated, from the CURRENT bootstrap team, which is exactly
    the leak this fix closed)."""
    return df.with_columns(
        pl.col("selected").cast(pl.Int64),
        pl.col("value").cast(pl.Int64),
        pl.col("attribution_complete").cast(pl.Boolean),
        pl.col("position").cast(pl.Utf8),
        pl.col("team").cast(pl.Utf8),
        pl.col("was_home").cast(pl.Boolean),
        pl.col("opponent_team").cast(pl.Int64),
    )


def _flatten_row(record: dict) -> dict:
    """Identical to the helper `snapshot_bootstrap.py` used to define
    locally — moved here so it is only defined once. Nested lists/dicts
    (`chip_plays`, `overrides`, `price_change_projections`, ...) are
    JSON-serialised into a string column; nothing is dropped, it just
    isn't further exploded into its own table in this slice."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, (dict, list)):
            out[key] = json.dumps(value, sort_keys=True)
        else:
            out[key] = value
    return out


def _records_to_df(records: list[dict]) -> pl.DataFrame:
    rows = [_flatten_row(r) for r in records]
    # infer_schema_length=None: scan every row, not just the first 100 — a
    # field null for the first 100 elements but populated later (e.g.
    # penalties_order) must not silently get typed as all-null.
    return pl.DataFrame(rows, infer_schema_length=None)


def _picks_to_rows(entry_id: int, event_id: int, picks_response: dict) -> list[dict]:
    """Identical to `sample_picks.py`'s helper of the same purpose — kept
    here as the adapter's normalisation logic. `sample_picks.py` itself
    still calls `FPLClient.entry_picks()` directly for its live sampling
    loop (see this module's docstring for why that hot, still-unverified
    path was left untouched); this function exists so the picks capability
    is genuinely implemented and unit-tested here regardless."""
    entry_history = picks_response.get("entry_history") or {}
    active_chip = picks_response.get("active_chip")
    rows = []
    for pick in picks_response.get("picks", []):
        rows.append(
            {
                "event": event_id,
                "entry_id": entry_id,
                "element": pick.get("element"),
                "position": pick.get("position"),
                "multiplier": pick.get("multiplier"),
                "is_captain": pick.get("is_captain"),
                "is_vice_captain": pick.get("is_vice_captain"),
                "active_chip": active_chip,
                "bank": entry_history.get("bank"),
                "team_value": entry_history.get("value"),
                "event_transfers": entry_history.get("event_transfers"),
                "event_transfers_cost": entry_history.get("event_transfers_cost"),
                "points_on_bench": entry_history.get("points_on_bench"),
                "overall_rank": entry_history.get("overall_rank"),
            }
        )
    return rows


class FPLProvider:
    """Provider adapter over the FPL Official API (`client.py`'s
    `FPLClient`). Live-only for the bootstrap-static-backed capabilities
    and picks — no season parameter, no history; see `registry.
    CoverageSpec`'s docstring for why that's expressed as `seasons=None`
    rather than a hardcoded season string.

    Session s005's `PLAYER_GAMEWEEK_STATS_GAMEWEEK` is the one exception
    to "no history" — `event/{gw}/live/` genuinely can answer for any
    ALREADY-SETTLED gameweek of the CURRENT season, so unlike the other
    seven capabilities it DOES carry a real season concept (vaastav's
    archive also serves it, for every OTHER season). See `register()`'s
    own docstring for why this capability is therefore registered with a
    season-RESTRICTED `CoverageSpec`, not the blanket `seasons=None` the
    other seven use."""

    provider_id = "fpl_api"
    policy = RatePolicy(requests_per_second=2.0, jitter_fraction=0.20)

    def __init__(self, client: FPLClient | None = None) -> None:
        self.client = client or FPLClient()

    def capabilities(self) -> list[CapabilityKey]:
        """Every capability this provider serves — used by `register()`
        below so the registry entry never drifts out of sync with what
        `fetch()` actually implements."""
        return [
            *_BOOTSTRAP_RECORD_CAPABILITIES.keys(),
            *_BOOTSTRAP_SINGLETON_CAPABILITIES.keys(),
            MANAGER_PICKS_SELECTION_GAMEWEEK,
            PLAYER_GAMEWEEK_STATS_GAMEWEEK,
        ]

    def supports(
        self,
        capability: CapabilityKey,
        *,
        season: str | None = None,
        competition: str | None = None,
    ) -> bool:
        if capability not in self.capabilities():
            return False
        if competition is not None and competition != "PL":
            return False
        return True

    # -- fetch -----------------------------------------------------------

    def fetch(self, capability: CapabilityKey, *, force_refresh: bool = False, **params: Any) -> FetchResult:
        if capability in _BOOTSTRAP_RECORD_CAPABILITIES or capability in _BOOTSTRAP_SINGLETON_CAPABILITIES:
            return self.fetch_batch([capability], force_refresh=force_refresh)[capability]
        if capability == MANAGER_PICKS_SELECTION_GAMEWEEK:
            return self._fetch_picks(force_refresh=force_refresh, **params)
        if capability == PLAYER_GAMEWEEK_STATS_GAMEWEEK:
            return self._fetch_gameweek_stats(force_refresh=force_refresh, **params)
        raise ProviderError(f"{self.provider_id} does not serve {capability}")

    def fetch_batch(
        self, capabilities: list[CapabilityKey], *, force_refresh: bool = False
    ) -> dict[CapabilityKey, FetchResult]:
        """Fetch several bootstrap-static-backed capabilities from ONE
        upstream call. This exists because `bootstrap-static/` genuinely
        serves all six of them in a single response — calling `fetch()`
        once per capability with `force_refresh=True` would multiply live
        requests by the number of capabilities requested, which is exactly
        the kind of module-boundary regression that quietly costs rate
        budget (CLAUDE.md's "FPL API discipline"). A capability whose
        payload key is missing or fails canonical-schema validation is
        SKIPPED with a warning, not fatal to the batch — this matches
        `snapshot_bootstrap.py`'s pre-existing per-dataset resilience
        (a quiet gap in one dataset must not stop the other five from
        being captured before the GW1 deadline)."""
        unsupported = [
            c for c in capabilities if c not in _BOOTSTRAP_RECORD_CAPABILITIES and c not in _BOOTSTRAP_SINGLETON_CAPABILITIES
        ]
        if unsupported:
            raise ProviderError(f"fetch_batch only serves bootstrap-static-backed capabilities; got {unsupported}")

        data = self.client.bootstrap_static(force_refresh=force_refresh)
        now = datetime.now(timezone.utc)

        results: dict[CapabilityKey, FetchResult] = {}
        for capability in capabilities:
            try:
                results[capability] = self._build_bootstrap_result(capability, data, now)
            except (ProviderError, SchemaError) as exc:
                logger.warning("%s — skipping capability %s", exc, capability)
        return results

    def _build_bootstrap_result(self, capability: CapabilityKey, data: dict, observed_at: datetime) -> FetchResult:
        if capability in _BOOTSTRAP_RECORD_CAPABILITIES:
            payload_key = _BOOTSTRAP_RECORD_CAPABILITIES[capability]
            records = data.get(payload_key)
            if not records:
                raise ProviderError(f"bootstrap-static payload has no {payload_key!r}")
            df = _records_to_df(records)
        else:
            payload_key = _BOOTSTRAP_SINGLETON_CAPABILITIES[capability]
            payload = data.get(payload_key)
            if payload is None:
                raise ProviderError(f"bootstrap-static payload has no {payload_key!r}")
            df = pl.DataFrame([{"payload": json.dumps(payload, sort_keys=True)}])

        CANONICAL_SCHEMAS[capability].validate(df)
        return FetchResult(
            rows=df,
            capability=capability,
            provider_id=self.provider_id,
            endpoint=_BOOTSTRAP_ENDPOINT,
            observed_at=observed_at,
            content_hash=content_hash(df),
        )

    def _fetch_picks(
        self,
        *,
        force_refresh: bool,
        entry_ids: list[int],
        event: int,
        progress_every: int = 500,
        on_progress: Callable[[int, int, int, int], None] | None = None,
    ) -> FetchResult:
        """Fetch picks for an explicit list of entry ids at one gameweek.
        Deliberately takes `entry_ids` rather than doing the id-space
        search / uniform draw / probe itself — that orchestration
        (blueprint §3.4) lives in `sampling.py` and is out of scope for a
        provider adapter to own; this method's job is only "turn these
        already-chosen entries into canonical rows"."""
        rows: list[dict] = []
        hits = misses = 0
        for i, entry_id in enumerate(entry_ids, start=1):
            picks_response = self.client.entry_picks(entry_id, event, force_refresh=force_refresh)
            if picks_response is None:
                misses += 1
            else:
                hits += 1
                rows.extend(_picks_to_rows(entry_id, event, picks_response))
            if on_progress is not None and i % progress_every == 0:
                on_progress(i, len(entry_ids), hits, misses)

        if not rows:
            raise ProviderError(f"zero picks collected out of {len(entry_ids)} entries sampled for GW{event}")

        df = pl.DataFrame(rows, infer_schema_length=None)
        CANONICAL_SCHEMAS[MANAGER_PICKS_SELECTION_GAMEWEEK].validate(df)
        now = datetime.now(timezone.utc)
        return FetchResult(
            rows=df,
            capability=MANAGER_PICKS_SELECTION_GAMEWEEK,
            provider_id=self.provider_id,
            endpoint=_ENTRY_PICKS_ENDPOINT.format(entry_id="*", event=event),
            observed_at=now,
            content_hash=content_hash(df),
            meta={"hits": hits, "misses": misses, "n_requested": len(entry_ids)},
        )

    def _fetch_gameweek_stats(
        self, *, force_refresh: bool, gameweek: int, season: str | None = None,
        elements_as_of: pl.DataFrame | None = None, teams_as_of: pl.DataFrame | None = None,
    ) -> FetchResult:
        """`player.gameweek_stats@gameweek`, sourced from `event/{gw}/
        live/` — session s005, second provider of an existing capability.
        See this module's docstring for the fixture-attribution design in
        full; `season` is carried through only as provenance on the
        output rows (`fplai.schemas.CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_
        STATS_GAMEWEEK].entity_key` includes it) — this endpoint itself
        has no season selector, so `season` is never used to choose what
        to fetch, only to label what was fetched. `fplai.backfill._build_
        fpl_gameweek_stats_units` is where the "never silently mislabel
        another season" guard actually lives (this method trusts its
        caller, same posture `_fetch_picks` already takes toward its own
        caller).

        `elements_as_of`/`teams_as_of` — OPTIONAL, session s005 continued.
        Already-resolved `elements`/`teams` snapshots, AS OF THE
        GAMEWEEK'S OWN DEADLINE (see this module's docstring, "position/
        team — resolved"). This method makes no store call and does not
        decide "as of what" — it trusts its caller exactly the way it
        already trusts `season`. Omit both (the default) to reproduce this
        provider's pre-fix behaviour: `position`/`team` stay NULL on every
        row, never guessed."""
        bootstrap = self.client.bootstrap_static(force_refresh=force_refresh)
        settled, reason = _gw_settled(bootstrap, gameweek)
        if not settled:
            raise ProviderError(
                f"gameweek {gameweek} has not settled yet ({reason}) — event/{gameweek}/live/ "
                "is not final until both finished and data_checked are True; this is an "
                "expected, retryable state, not a bug."
            )

        elements_by_id = {e["id"]: e for e in bootstrap.get("elements", [])}

        # position/team lookups -- built once per call, from the SAME
        # already-fetched bootstrap payload (element_types) plus whatever
        # already-resolved as-of snapshots the caller supplied. See this
        # module's docstring, "position/team — resolved, session s005
        # continued", for why these are NOT built from `elements_by_id`
        # above (that dict is bootstrap-static's CURRENT, live state --
        # exactly the leakage this session closed).
        element_type_labels = _element_type_labels(bootstrap)
        elements_as_of_by_id = _elements_as_of_lookup(elements_as_of)
        team_name_by_id = _team_name_lookup(teams_as_of)

        live = self.client.event_live(gameweek, force_refresh=force_refresh)
        live_elements = live.get("elements", [])
        if not live_elements:
            raise ProviderError(
                f"event/{gameweek}/live/ returned no elements for a settled gameweek — genuine anomaly"
            )

        fixtures = self.client.fixtures(event=gameweek, force_refresh=force_refresh)
        if not fixtures:
            raise ProviderError(f"fixtures/?event={gameweek} returned no fixtures for a settled gameweek")
        fixtures_by_id = {f["id"]: f for f in fixtures}
        fixtures_by_team: dict[int, list[int]] = {}
        for f in fixtures:
            fixtures_by_team.setdefault(f["team_h"], []).append(f["id"])
            fixtures_by_team.setdefault(f["team_a"], []).append(f["id"])

        rows: list[dict] = []
        n_dgw_elements = 0
        n_blank_elements = 0
        n_position_team_unresolved = 0
        n_was_home_opponent_unresolved = 0
        for live_el in live_elements:
            element_id = live_el.get("id")
            bs_el = elements_by_id.get(element_id)
            if bs_el is None:
                raise ProviderError(
                    f"element {element_id} present in event/{gameweek}/live/ but absent from "
                    "bootstrap-static() elements — genuine anomaly, both calls hit the same live API"
                )
            team = bs_el.get("team")
            name = f"{bs_el.get('first_name', '')} {bs_el.get('second_name', '')}".strip()
            stats = live_el.get("stats") or {}
            explain = live_el.get("explain") or []

            # explain's own fixture ids are AUTHORITATIVE when present —
            # correct even for a player whose CURRENT bootstrap team
            # differs from who they played for historically. Only an
            # entirely-unused player (empty explain) falls back to the
            # current-team-based fixture lookup below — the ONE place in
            # this method that still deliberately reads `team` (bs_el's
            # CURRENT bootstrap team), because there is no as-of-deadline
            # fixture list to look up otherwise. This is now the sole
            # remaining, narrower, separately-documented risk (see this
            # module's docstring): it can only ever pick the WRONG fixture
            # for a player who (a) truly did not play at all this
            # gameweek AND (b) has since transferred clubs. It does not
            # affect was_home/opponent_team on any row that DOES get
            # produced — those are fixed below to use the deadline-
            # resolved team, never this one.
            if explain:
                seen: list[int] = []
                for e in explain:
                    fid = e.get("fixture")
                    if fid not in seen:
                        seen.append(fid)
                fixture_ids = seen
            else:
                fixture_ids = fixtures_by_team.get(team, [])

            if not fixture_ids:
                n_blank_elements += 1
                continue

            # AS-OF-DEADLINE resolution (never `bs_el`, `team` above, or
            # any other CURRENT-bootstrap field — see this module's
            # docstring). Only computed once this element is known to
            # produce at least one row — a blank-gameweek element that
            # never reaches here is not "unresolved", it simply has no row
            # to attach a position/team to. One resolution per element,
            # reused for every row this element produces this gameweek
            # (identical on a double gameweek too — this is a per-element-
            # per-gameweek fact, not a per-fixture one).
            position, resolved_team = _resolve_position_team(
                element_id, elements_as_of_by_id=elements_as_of_by_id,
                team_name_by_id=team_name_by_id, element_type_labels=element_type_labels,
            )
            if position is None or resolved_team is None:
                n_position_team_unresolved += 1

            # was_home/opponent_team — resolved, session s005 continued.
            # SAME as-of-deadline snapshot as position/team above, NOT
            # `team` (bs_el's current bootstrap team, still used only for
            # the empty-explain fixture-lookup fallback above). `None`
            # here means "genuinely unknown as of the deadline" and
            # produces was_home=None/opponent_team=None on this row's
            # output — never a guess from the current-live team.
            home_away_team_id = _resolved_team_id(element_id, elements_as_of_by_id)
            if home_away_team_id is None:
                n_was_home_opponent_unresolved += 1

            if len(fixture_ids) == 1:
                fixture = fixtures_by_id.get(fixture_ids[0])
                if fixture is None:
                    raise ProviderError(
                        f"element {element_id}: explain names fixture {fixture_ids[0]!r}, not "
                        f"present in fixtures/?event={gameweek} — genuine anomaly"
                    )
                rows.append(
                    _build_single_fixture_gameweek_row(
                        season=season, gameweek=gameweek, element_id=element_id, name=name,
                        stats=stats, fixture=fixture, home_away_team_id=home_away_team_id,
                        position=position, resolved_team=resolved_team,
                    )
                )
            else:
                n_dgw_elements += 1
                explain_by_fixture = {e.get("fixture"): e for e in explain}
                for fid in fixture_ids:
                    fixture = fixtures_by_id.get(fid)
                    if fixture is None:
                        raise ProviderError(
                            f"element {element_id}: explain names fixture {fid!r}, not present in "
                            f"fixtures/?event={gameweek} — genuine anomaly"
                        )
                    rows.append(
                        _build_degraded_dgw_gameweek_row(
                            season=season, gameweek=gameweek, element_id=element_id, name=name,
                            fixture=fixture, home_away_team_id=home_away_team_id,
                            fixture_explain=explain_by_fixture.get(fid),
                            position=position, resolved_team=resolved_team,
                        )
                    )

        if not rows:
            raise ProviderError(
                f"gameweek {gameweek}: every element resolved to zero rows (all blank-gameweek?) — "
                "nothing to write"
            )

        df = pl.DataFrame(rows, infer_schema_length=None)
        df = _cast_gameweek_stats_dtypes(df)
        CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK].validate(df)
        now = datetime.now(timezone.utc)
        return FetchResult(
            rows=df,
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            provider_id=self.provider_id,
            endpoint=_EVENT_LIVE_ENDPOINT.format(event=gameweek),
            observed_at=now,
            content_hash=content_hash(df),
            meta={
                "season": season,
                "gameweek": gameweek,
                "n_rows": df.height,
                "n_elements": len(live_elements),
                "n_dgw_elements": n_dgw_elements,
                "n_blank_elements": n_blank_elements,
                # session s005 continued -- elements with position AND/OR
                # team unresolved (never guessed; see _resolve_position_
                # team). Always 0 when elements_as_of/teams_as_of cover
                # every element that scored a row; equal to n_elements
                # when neither snapshot was supplied at all (this
                # provider's pre-fix behaviour, still reachable by omitting
                # both parameters).
                "n_position_team_unresolved": n_position_team_unresolved,
                # session s005 continued -- elements with was_home/
                # opponent_team unresolved (never guessed from the CURRENT
                # bootstrap team; see _resolved_team_id/_fixture_home_away).
                # Same shape as n_position_team_unresolved above -- usually
                # equal to it (both key off the SAME elements_as_of lookup)
                # but not guaranteed identical: position/team can also miss
                # on an unrecognised element_type code alone, which does
                # not affect was_home/opponent_team.
                "n_was_home_opponent_unresolved": n_was_home_opponent_unresolved,
            },
        )


def register(registry, client: FPLClient | None = None, *, season: str | None = None) -> FPLProvider:
    """The 'registry entry' half of the E2b gate: adding a provider is an
    adapter (this file) plus this one call, with zero changes to
    `schemas.py`, `registry.py` or `providers/base.py`.

    `registry` is typed loosely (not `registry.CapabilityRegistry`) to
    avoid a circular import — `registry.py` imports `Provider` from
    `providers/base.py`, and this module must not import back into
    `registry.py` at module scope for that reason.

    **Session s005 — `PLAYER_GAMEWEEK_STATS_GAMEWEEK` cannot share the
    other seven capabilities' blanket `seasons=None` coverage.** `None`
    means "unrestricted", and `registry.CoverageSpec.matches()` treats an
    unrestricted provider as matching EVERY season query — correct for
    the other seven (they have no season concept at all, see `FPLProvider`'s
    own docstring), but WRONG here: vaastav's archive also serves this
    capability, for every OTHER season, and this provider's priority (10)
    is already numerically better (lower) than vaastav's (20,
    `providers/vaastav.py::register`). Registering `seasons=None` for this
    capability would make `registry.resolve(PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    season="2019-20")` silently PREFER this provider — which would then
    fetch and mislabel the CURRENT season's `event/{gw}/live/` payload as
    2019-20 (CLAUDE.md rule 2: this is leakage, not a crash). So this
    capability is registered SEPARATELY, with `seasons=frozenset({season})`
    — a real restriction — and ONLY when a caller supplies `season`
    explicitly (never inferred, same "caller supplies it, this module
    never guesses it" convention `scripts/pin_dc_thresholds.py` already
    established for this identical live payload). Omitting `season` is
    backward-compatible with every caller that predates this session
    (`FPLProvider.capabilities()` grew a member, but `register()` without
    `season` registers exactly the same seven capabilities it always did,
    logged rather than raised for the one it skips) — see `tests/test_
    provider_fpl.py` for both the opt-in and the omitted-season cases.
    """
    provider = FPLProvider(client=client)
    from fplai.registry import CoverageSpec  # local import: see docstring above

    coverage = CoverageSpec(seasons=None, competitions=frozenset({"PL"}), priority=10)
    for capability in provider.capabilities():
        if capability == PLAYER_GAMEWEEK_STATS_GAMEWEEK:
            continue  # registered separately below, with season-restricted coverage
        registry.register(capability, provider, coverage)

    if PLAYER_GAMEWEEK_STATS_GAMEWEEK in provider.capabilities():
        if season is None:
            logger.warning(
                "%s also serves %s (session s005) but register() was called with no "
                "season -- skipping its registration (it cannot safely share the other "
                "capabilities' seasons=None coverage; see this function's docstring). "
                "Call register(..., season='2026-27') naming the CURRENT live season "
                "explicitly to register it.",
                provider.provider_id,
                PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            )
        else:
            gw_coverage = CoverageSpec(seasons=frozenset({season}), competitions=frozenset({"PL"}), priority=10)
            registry.register(PLAYER_GAMEWEEK_STATS_GAMEWEEK, provider, gw_coverage)

    return provider
