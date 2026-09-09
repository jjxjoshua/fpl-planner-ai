"""Premier League API provider adapter — E2b stories 6-7 (blueprint §12,
§3.6, §12.5).

Base: `https://sdp-prem-prod.premier-league-prod.pulselive.com/api/` —
verified live, unauthenticated, HTTP 200 on every route below, by the
Architect (docs/wiki/provider-evaluation.md, "Architect verification"
section). Competition id `8` = Premier League; season id is the PL API's
own convention — a starting-year int/string (`"2025"` = 2025/26), NOT
FPL's `"YYYY-YY"` string. `fetch()`'s `season` kwarg is always in the PL
API's convention; nowhere in this module is a season hardcoded — every
call site must pass one explicitly (constructor `season` too), per
CLAUDE.md rule 4.

Built on `transport.HttpTransport` directly (`providers/fpl.py`'s module
docstring flagged this as the next HTTP provider's job — it is). Same
`RatePolicy(requests_per_second=2.0, jitter_fraction=0.20)` discipline as
the FPL client; §3.6's binding conditions (private/personal use, never
redistributed, `data/` stays gitignored, polite access) apply to every
request this module makes.

Six capabilities served (blueprint §12.1 — grain is part of the key):
`match.lineups@match`, `match.substitutions@match`,
`team.match_stats@match`, `player.season_stats@season`,
`match.fixtures@matchweek`, `match.officials@match` (added session s004).
`player.defensive_actions@match` is DELIBERATELY not implemented and not
registered — no per-player-per-match statistics route exists on this
platform (confirmed negative, docs/wiki/provider-evaluation.md), and
leaving the capability unregistered means a query for it fails loudly via
`registry.RegistryError` rather than silently being answered by a
different grain (blueprint §12.1's protection, exercised for real here
rather than only in the abstract).

**`match.officials@match` is a SEPARATE live request, not a free ride on
an already-fetched payload — verified, not assumed (session s004).** Before
adding it, both `v1/matches/{id}/events` (already fetched for
`match.substitutions@match`) and `v3/matches/{id}/lineups` (already fetched
for `match.lineups@match`) were checked live for referee/official data
anywhere in their response bodies: neither contains it. The evaluation
record this project inherited (docs/wiki/provider-evaluation.md §2.1)
describes `GET /football/fixtures/{id}` on the OLDER `footballapi.
pulselive.com` host bundling lineups + substitutions + officials in ONE
response — true for that host, and NOT the host this adapter targets. This
adapter was built against the newer `sdp-prem-prod...` host (chosen
deliberately — see this module's own history), which splits the same data
across `v3/matches/{id}/lineups`, `v1/matches/{id}/events` and a THIRD,
distinct route, `v1/matches/{id}/officials`. `_fetch_officials` therefore
costs one additional request per match on top of the three already paid
for lineups/substitutions/team-stats, not zero.

Identity resolution (`fplai.identity`) is mandatory, not optional
enrichment: every row this adapter produces carries FPL-native ids
(`player_element_id`, `team_code`), never raw PL API ids alone, so a
consumer never has to re-derive the join. `player.defensive_actions@match`
aside, the actual gate this story sat behind was resolving IDENTITY, not
finding the routes (those were already verified going in) — see
`fplai.identity`'s module docstring for what was free and what had to be
built and verified.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import polars as pl

from fplai.identity import IdentityError, IdentityResolver
from fplai.providers.base import FetchResult, ProviderError
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    MATCH_FIXTURES_MATCHWEEK,
    MATCH_LINEUPS_MATCH,
    MATCH_OFFICIALS_MATCH,
    MATCH_SUBSTITUTIONS_MATCH,
    PLAYER_SEASON_STATS_SEASON,
    TEAM_MATCH_STATS_MATCH,
    CapabilityKey,
)
from fplai.store import content_hash
from fplai.transport import HttpTransport, RatePolicy

logger = logging.getLogger("fplai.providers.pl")

BASE_URL = "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/"

# The Premier League's own competition id on this platform — an identifier
# for WHICH competition, not a per-season rule (CLAUDE.md rule 4 targets
# things like prices/budget/scoring/squad-limits/DC-thresholds that change
# season to season; "8 is the Premier League" does not). Same category of
# constant as fpl.py's CoverageSpec(competitions=frozenset({"PL"})).
PL_COMPETITION_ID = 8

_LINEUPS_ENDPOINT = "v3/matches/{match_id}/lineups"
_EVENTS_ENDPOINT = "v1/matches/{match_id}/events"
_OFFICIALS_ENDPOINT = "v1/matches/{match_id}/officials"
_TEAM_STATS_ENDPOINT = "v3/matches/{match_id}/stats"
_PLAYER_SEASON_STATS_ENDPOINT = "v2/competitions/{competition}/seasons/{season}/players/{player_id}/stats"
_MATCHWEEK_FIXTURES_ENDPOINT = "v1/competitions/{competition}/seasons/{season}/matchweeks/{matchweek}/matches"


def _scalarize(value: Any) -> tuple[float | None, str | None]:
    """Most of the 185 `/v3/matches/{id}/stats` keys are plain numbers, but
    NOT all of them — `fastestPlayer` was found live to be a nested object
    (`{"topSpeed": 35.53, "playerId": "592031"}`), not a scalar. `value`
    (this capability's LONG-format numeric column) cannot hold that without
    lying about its type, and simply dropping it would violate blueprint
    §3.2/rule 5's 'nothing is silently discarded'. Returns `(value, None)`
    for a genuine number, or `(None, value_raw)` — the original JSON-encoded
    — for anything else, so every one of the 185 keys survives one way or
    the other, captured in full per the story brief ('capture all 185 keys,
    not a chosen subset')."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, json.dumps(value)
    if isinstance(value, (int, float)):
        return float(value), None
    return None, json.dumps(value, sort_keys=True)


def _extract_pl_teams(matchweek_payload: dict) -> list[dict]:
    """Every unique `{id, name, shortName}` team seen in a matchweek
    fixtures response — this is what `TeamIdentityMap.build` verifies
    against the FPL `teams` snapshot. A single matchweek's fixtures cover
    all 20 clubs (10 fixtures), so this needs exactly one request."""
    teams: dict[str, dict] = {}
    for match in matchweek_payload.get("data") or []:
        for side in ("homeTeam", "awayTeam"):
            team = match.get(side) or {}
            team_id = team.get("id")
            if team_id is None:
                continue
            teams.setdefault(team_id, {"id": team_id, "name": team.get("name"), "shortName": team.get("shortName")})
    return list(teams.values())



# The PL API returns a fixture's kickoff as a bare wall-clock string with no
# offset ("2025-08-15 20:00:00"), and it is EUROPE/LONDON local time, not UTC.
# Treating it as UTC — which this adapter did until 2026-08-29 — silently
# shifts every British-Summer-Time fixture one hour EARLY.
#
# Measured, not guessed: joining PL fixtures to vaastav's own FPL-sourced
# `kickoff_time` (explicitly Z/UTC) on (away team code, kickoff) matched
# exactly 223 of 375 fixtures in 2025-26. The delta on the rest was EXACTLY
# -60 minutes, and it split perfectly on the calendar — 0 minutes in Nov-Mar
# (GMT), -60 in Apr, May, Aug, Sep (BST), with October appearing on both
# sides of the changeover. Two independent sources agreeing all winter and
# differing by precisely one hour all summer is not a coincidence.
#
# This mattered beyond the join: `fplai.backfill._valid_at_for` anchors
# lineups, substitutions, team-match-stats AND officials on this same
# kickoff, so a BST error puts their `valid_at` an hour EARLY — the LEAKAGE
# direction under blueprint §3.2, where overstating how early a fact became
# true is the dangerous one and understating it is merely conservative.
#
# Stored tz-naive UTC, per CLAUDE.md's storage boundary ("every stored time
# is UTC"). The autumn DST fold makes one wall-clock hour ambiguous each
# year; `fold=0` resolves it to the first (BST) occurrence. No PL fixture
# has ever kicked off in the 01:00-02:00 UK window, so this is documented
# for correctness rather than because it can bite.
_PL_KICKOFF_TZ = ZoneInfo("Europe/London")


def _parse_pl_kickoff(raw: str) -> datetime:
    """`"2025-08-15 20:00:00"` (Europe/London wall clock) -> tz-naive UTC."""
    local = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_PL_KICKOFF_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


class PLProvider:
    """Provider adapter over the Premier League API. Requires an
    `IdentityResolver` (or the FPL `elements`/`teams` snapshots to build
    one lazily on first use) — every capability this provider serves
    needs at least one of the two identities it resolves.

    `season` and `identity_season` are DELIBERATELY separate (found live,
    2026-08-20 — see docs/wiki/provider-framework.md). `season` is the
    default PL API season for data fetches (fixtures, player-season-stats)
    and can be overridden per-call. `identity_season` is which season's
    fixture list `TeamIdentityMap` is verified against, and it must match
    whichever season the `elements`/`teams` snapshots themselves describe
    — NOT necessarily the season of the match being fetched. A live probe
    fetching a 2025/26 match (`season="2025"`) against a CURRENT (2026/27)
    `elements` snapshot raised `IdentityError` on exactly the three
    promoted clubs (Coventry City, Hull City, Ipswich Town) that are in the
    current snapshot but not in the 2025/26 fixture list used to verify
    team identity — correctly, per §12.5, but it is a caller error to
    conflate the two seasons, not a resolver bug. Defaults to `season` for
    the common case (fetching the current season's own data)."""

    provider_id = "pl_api"
    policy = RatePolicy(requests_per_second=2.0, jitter_fraction=0.20)

    def __init__(
        self,
        transport: HttpTransport,
        *,
        season: str,
        identity_season: str | None = None,
        elements: pl.DataFrame | None = None,
        teams: pl.DataFrame | None = None,
        identity: IdentityResolver | None = None,
    ) -> None:
        if identity is None and (elements is None or teams is None):
            raise ProviderError(
                "PLProvider needs either a pre-built IdentityResolver, or both "
                "'elements' and 'teams' FPL snapshots to build one lazily — "
                "every capability this provider serves resolves at least one "
                "identity (blueprint §12.5)."
            )
        self.transport = transport
        self.season = season
        self.identity_season = identity_season if identity_season is not None else season
        self._elements = elements
        self._teams = teams
        self._identity = identity

    def capabilities(self) -> list[CapabilityKey]:
        return [
            MATCH_LINEUPS_MATCH,
            MATCH_SUBSTITUTIONS_MATCH,
            TEAM_MATCH_STATS_MATCH,
            PLAYER_SEASON_STATS_SEASON,
            MATCH_FIXTURES_MATCHWEEK,
            MATCH_OFFICIALS_MATCH,
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

    # -- identity ----------------------------------------------------------

    def identity(self) -> IdentityResolver:
        """Built once, lazily, on first use — NOT at construction, so
        constructing a `PLProvider` never makes a network call by itself
        (consistent with `FPLProvider`, whose constructor is also
        network-free). One request (`_fetch_pl_teams_for_identity`),
        cached by `HttpTransport` for every subsequent call in the
        process."""
        if self._identity is None:
            pl_teams = self._fetch_pl_teams_for_identity()
            self._identity = IdentityResolver.from_fpl_snapshots(self._elements, self._teams, pl_teams)
        return self._identity

    def _fetch_pl_teams_for_identity(self) -> list[dict]:
        endpoint = _MATCHWEEK_FIXTURES_ENDPOINT.format(
            competition=PL_COMPETITION_ID, season=self.identity_season, matchweek=1
        )
        status, text = self.transport.get(endpoint)
        if status != 200:
            raise ProviderError(f"GET {endpoint} returned {status} while bootstrapping team identity: {text[:200]}")
        return _extract_pl_teams(json.loads(text))

    # -- fetch ---------------------------------------------------------------

    def fetch(self, capability: CapabilityKey, *, force_refresh: bool = False, **params: Any) -> FetchResult:
        if capability == MATCH_LINEUPS_MATCH:
            return self._fetch_lineups(force_refresh=force_refresh, **params)
        if capability == MATCH_SUBSTITUTIONS_MATCH:
            return self._fetch_substitutions(force_refresh=force_refresh, **params)
        if capability == TEAM_MATCH_STATS_MATCH:
            return self._fetch_team_match_stats(force_refresh=force_refresh, **params)
        if capability == PLAYER_SEASON_STATS_SEASON:
            return self._fetch_player_season_stats(force_refresh=force_refresh, **params)
        if capability == MATCH_FIXTURES_MATCHWEEK:
            return self._fetch_fixtures(force_refresh=force_refresh, **params)
        if capability == MATCH_OFFICIALS_MATCH:
            return self._fetch_officials(force_refresh=force_refresh, **params)
        raise ProviderError(f"{self.provider_id} does not serve {capability}")

    def _fetch_lineups(self, *, force_refresh: bool, match_id: int | str) -> FetchResult:
        identity = self.identity()
        endpoint = _LINEUPS_ENDPOINT.format(match_id=match_id)
        status, text = self.transport.get(endpoint, force_refresh=force_refresh)
        if status != 200:
            raise ProviderError(f"GET {endpoint} returned {status}: {text[:200]}")
        data = json.loads(text)

        rows: list[dict] = []
        unresolved: list[str] = []
        for side_key in ("home_team", "away_team"):
            team = data.get(side_key)
            if team is None:
                continue
            pl_team_id = team.get("teamId")
            if pl_team_id is None:
                raise ProviderError(f"{endpoint}: {side_key} payload is missing 'teamId'")
            team_code = identity.teams.resolve_pl_to_fpl(pl_team_id)
            for player in team.get("players", []):
                pl_player_id = player.get("id")
                # Collect EVERY unresolved player across the whole match
                # before raising, rather than aborting on the first one —
                # still raises (§12.5, never silently drops), just names
                # every offender in one error instead of only the first
                # (real-world value: a historical match's squad routinely
                # has more than one departed player — see docs/wiki/
                # provider-framework.md for the live case this came from).
                try:
                    element_id = identity.players.resolve(pl_player_id)
                except IdentityError:
                    unresolved.append(str(pl_player_id))
                    continue
                is_bench = player.get("position") == "Substitute"
                rows.append(
                    {
                        "match_id": str(match_id),
                        "team_code": team_code,
                        "player_element_id": element_id,
                        "player_code": int(pl_player_id),
                        "role": "bench" if is_bench else "start",
                        "position": player.get("subPosition") if is_bench else player.get("position"),
                        "shirt_number": player.get("shirtNum"),
                        "is_captain": bool(player.get("isCaptain", False)),
                    }
                )
        if unresolved:
            raise IdentityError(
                f"{endpoint}: {len(unresolved)} player(s) in this match could not be resolved to a "
                f"current FPL element (blueprint §12.5): PL player ids {unresolved}. Expected for a "
                "historical match against a current-season identity snapshot — see fplai.identity."
                "PlayerIdentityMap.resolve's docstring."
            )
        if not rows:
            raise ProviderError(f"{endpoint} contained no players for either side")

        df = pl.DataFrame(rows, infer_schema_length=None)
        CANONICAL_SCHEMAS[MATCH_LINEUPS_MATCH].validate(df)
        return FetchResult(
            rows=df,
            capability=MATCH_LINEUPS_MATCH,
            provider_id=self.provider_id,
            endpoint=endpoint,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
        )

    def _fetch_substitutions(self, *, force_refresh: bool, match_id: int | str) -> FetchResult:
        identity = self.identity()
        endpoint = _EVENTS_ENDPOINT.format(match_id=match_id)
        status, text = self.transport.get(endpoint, force_refresh=force_refresh)
        if status != 200:
            raise ProviderError(f"GET {endpoint} returned {status}: {text[:200]}")
        data = json.loads(text)

        rows: list[dict] = []
        unresolved: list[str] = []
        for side_key in ("homeTeam", "awayTeam"):
            team = data.get(side_key)
            if team is None:
                continue
            pl_team_id = team.get("id")
            if pl_team_id is None:
                raise ProviderError(f"{endpoint}: {side_key} payload is missing 'id'")
            team_code = identity.teams.resolve_pl_to_fpl(pl_team_id)
            for sub in team.get("subs", []):
                on_id, off_id = sub.get("playerOnId"), sub.get("playerOffId")
                # `playerOffId` is this schema's entity-key disambiguator
                # (blueprint §12.1) — never null in any match observed so
                # far. A null here is genuinely unprecedented, so it raises
                # loudly rather than being guessed at, same posture as the
                # missing-'id'/-'teamId' checks elsewhere in this class.
                if off_id is None:
                    raise ProviderError(f"{endpoint}: a substitution is missing 'playerOffId': {sub}")
                try:
                    off_element_id = identity.players.resolve(off_id)
                except IdentityError:
                    unresolved.append(str(off_id))
                    continue

                # `playerOnId` CAN be null — verified live, 2026-08-28,
                # Crystal Palace v Aston Villa 2025-26 (match_id 2561915,
                # minute 90): a player subbed OFF with no one coming ON (all
                # 5 subs already used, or a deliberate 10-men finish). This
                # is a real football fact, not a malformed payload — rule 5/
                # §3.2 forbids silently dropping the row, so it is kept with
                # player_on_element_id/player_on_code genuinely null, NOT
                # treated as an identity-resolution miss (a different
                # failure class entirely — see IdentityError's docstring).
                on_element_id: int | None = None
                on_code: int | None = None
                if on_id is not None:
                    try:
                        on_element_id = identity.players.resolve(on_id)
                    except IdentityError:
                        unresolved.append(str(on_id))
                        continue
                    on_code = int(on_id)

                rows.append(
                    {
                        "match_id": str(match_id),
                        "team_code": team_code,
                        "period": sub.get("period"),
                        "minute": int(sub["time"]) if sub.get("time") is not None else None,
                        "player_on_element_id": on_element_id,
                        "player_off_element_id": off_element_id,
                        "player_on_code": on_code,
                        "player_off_code": int(off_id),
                    }
                )
        if unresolved:
            raise IdentityError(
                f"{endpoint}: substitution(s) involving unresolved player id(s) {unresolved} "
                "(blueprint §12.5). Expected for a historical match — see fplai.identity."
                "PlayerIdentityMap.resolve's docstring."
            )
        if not rows:
            # A professional match with literally zero substitutions is not
            # something this adapter has observed; treat it the same way
            # fpl.py's _fetch_picks treats zero hits — a real ProviderError,
            # not an empty batch (store.write() refuses those outright).
            raise ProviderError(f"{endpoint} contained zero substitutions for match {match_id}")

        df = pl.DataFrame(rows, infer_schema_length=None)
        CANONICAL_SCHEMAS[MATCH_SUBSTITUTIONS_MATCH].validate(df)
        return FetchResult(
            rows=df,
            capability=MATCH_SUBSTITUTIONS_MATCH,
            provider_id=self.provider_id,
            endpoint=endpoint,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
        )

    def _fetch_team_match_stats(
        self,
        *,
        force_refresh: bool,
        match_id: int | str,
        home_team_code: int | None = None,
        away_team_code: int | None = None,
    ) -> FetchResult:
        """`home_team_code`/`away_team_code` are OPTIONAL enrichment — the
        raw endpoint reports `side` ('Home'/'Away') only, no team id at
        all, so there is nothing to resolve identity FROM here. A caller
        that already knows the fixture (typically from
        `match.fixtures@matchweek`) can pass both codes through and get
        them attached to every row; a caller that doesn't still gets a
        fully valid, schema-passing result with `team_code = null`."""
        endpoint = _TEAM_STATS_ENDPOINT.format(match_id=match_id)
        status, text = self.transport.get(endpoint, force_refresh=force_refresh)
        if status != 200:
            raise ProviderError(f"GET {endpoint} returned {status}: {text[:200]}")
        data = json.loads(text)
        if not isinstance(data, list) or not data:
            raise ProviderError(f"{endpoint} returned an unexpected shape (expected a non-empty list): {type(data)}")

        team_code_by_side = {"Home": home_team_code, "Away": away_team_code}
        rows: list[dict] = []
        for side_block in data:
            side = side_block.get("side")
            stats = side_block.get("stats") or {}
            for stat_key, raw_value in stats.items():
                value, value_raw = _scalarize(raw_value)
                rows.append(
                    {
                        "match_id": str(match_id),
                        "side": side,
                        "team_code": team_code_by_side.get(side),
                        "stat_key": stat_key,
                        "value": value,
                        "value_raw": value_raw,
                    }
                )
        if not rows:
            raise ProviderError(f"{endpoint} contained no stats for either side of match {match_id}")

        df = pl.DataFrame(rows, infer_schema_length=None)
        CANONICAL_SCHEMAS[TEAM_MATCH_STATS_MATCH].validate(df)
        return FetchResult(
            rows=df,
            capability=TEAM_MATCH_STATS_MATCH,
            provider_id=self.provider_id,
            endpoint=endpoint,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
            meta={"n_stat_keys_per_side": {b.get("side"): len(b.get("stats") or {}) for b in data}},
        )

    def _fetch_player_season_stats(
        self, *, force_refresh: bool, player_element_id: int, season: str | None = None
    ) -> FetchResult:
        season = season or self.season
        identity = self.identity()
        pl_player_id = identity.players.resolve_reverse(player_element_id)
        endpoint = _PLAYER_SEASON_STATS_ENDPOINT.format(
            competition=PL_COMPETITION_ID, season=season, player_id=pl_player_id
        )
        status, text = self.transport.get(endpoint, force_refresh=force_refresh)
        if status != 200:
            raise ProviderError(f"GET {endpoint} returned {status}: {text[:200]}")
        data = json.loads(text)
        stats = data.get("stats") or {}
        if not stats:
            raise ProviderError(f"{endpoint} returned no 'stats' for player element {player_element_id}, season {season}")

        rows = []
        for stat_key, raw_value in stats.items():
            value, value_raw = _scalarize(raw_value)
            rows.append(
                {
                    "season": str(season),
                    "player_element_id": int(player_element_id),
                    "player_code": pl_player_id,
                    "stat_key": stat_key,
                    "value": value,
                    "value_raw": value_raw,
                }
            )
        df = pl.DataFrame(rows, infer_schema_length=None)
        CANONICAL_SCHEMAS[PLAYER_SEASON_STATS_SEASON].validate(df)
        return FetchResult(
            rows=df,
            capability=PLAYER_SEASON_STATS_SEASON,
            provider_id=self.provider_id,
            endpoint=endpoint,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
        )

    def _fetch_fixtures(self, *, force_refresh: bool, season: str | None = None, matchweek: int) -> FetchResult:
        season = season or self.season
        identity = self.identity()
        endpoint = _MATCHWEEK_FIXTURES_ENDPOINT.format(competition=PL_COMPETITION_ID, season=season, matchweek=matchweek)
        status, text = self.transport.get(endpoint, force_refresh=force_refresh)
        if status != 200:
            raise ProviderError(f"GET {endpoint} returned {status}: {text[:200]}")
        data = json.loads(text)
        matches = data.get("data") or []
        if not matches:
            raise ProviderError(f"{endpoint} contained no matches for matchweek {matchweek}, season {season}")

        rows = []
        for match in matches:
            home, away = match.get("homeTeam") or {}, match.get("awayTeam") or {}
            match_id = match.get("matchId")
            kickoff = match.get("kickoff")
            if match_id is None or kickoff is None or home.get("id") is None or away.get("id") is None:
                raise ProviderError(f"{endpoint}: fixture missing matchId/kickoff/homeTeam.id/awayTeam.id: {match}")
            rows.append(
                {
                    "match_id": str(match_id),
                    "season": str(season),
                    "matchweek": int(matchweek),
                    "kickoff": _parse_pl_kickoff(kickoff),
                    "home_team_code": identity.teams.resolve_pl_to_fpl(home["id"]),
                    "away_team_code": identity.teams.resolve_pl_to_fpl(away["id"]),
                    "home_team_name_pl": home.get("name"),
                    "away_team_name_pl": away.get("name"),
                    "ground": match.get("ground"),
                    "period": match.get("period"),
                }
            )

        df = pl.DataFrame(rows, infer_schema_length=None)
        CANONICAL_SCHEMAS[MATCH_FIXTURES_MATCHWEEK].validate(df)
        return FetchResult(
            rows=df,
            capability=MATCH_FIXTURES_MATCHWEEK,
            provider_id=self.provider_id,
            endpoint=endpoint,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
        )

    def _fetch_officials(self, *, force_refresh: bool, match_id: int | str) -> FetchResult:
        """No identity resolution here — deliberately. Unlike lineups/
        substitutions, `matchOfficials[]` carries no PL numeric id at all
        (only a name), so there is no `self.identity()` call to make and
        nothing that can raise `IdentityError`; this is the only match-grain
        fetch method in this class that isn't gated by team/player identity
        (see MATCH_OFFICIALS_MATCH's schema comment for the consequence:
        joining onto an FPL fixture happens downstream, via the SAME
        `fplai.identity.resolve_match` every other match-grain capability
        here already uses, keyed on `match_id`)."""
        endpoint = _OFFICIALS_ENDPOINT.format(match_id=match_id)
        status, text = self.transport.get(endpoint, force_refresh=force_refresh)
        if status != 200:
            raise ProviderError(f"GET {endpoint} returned {status}: {text[:200]}")
        data = json.loads(text)
        officials = data.get("matchOfficials") or []
        if not officials:
            raise ProviderError(f"{endpoint} contained no matchOfficials for match {match_id}")

        rows: list[dict] = []
        for entry in officials:
            role = entry.get("type")
            if role is None:
                raise ProviderError(f"{endpoint}: an official entry is missing 'type' (role): {entry}")
            official = entry.get("official") or {}
            rows.append(
                {
                    "match_id": str(match_id),
                    "role": role,
                    "is_referee": role == "Referee",
                    "official_name": official.get("name"),
                    "official_first_name": official.get("firstName"),
                    "official_last_name": official.get("lastName"),
                }
            )

        df = pl.DataFrame(rows, infer_schema_length=None)
        CANONICAL_SCHEMAS[MATCH_OFFICIALS_MATCH].validate(df)
        return FetchResult(
            rows=df,
            capability=MATCH_OFFICIALS_MATCH,
            provider_id=self.provider_id,
            endpoint=endpoint,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
        )


def register(
    registry,
    transport: HttpTransport,
    *,
    season: str,
    identity_season: str | None = None,
    elements: pl.DataFrame | None = None,
    teams: pl.DataFrame | None = None,
    identity: IdentityResolver | None = None,
    priority: int = 10,
) -> PLProvider:
    """The 'registry entry' half of the E2b gate — mirrors
    `providers/fpl.py::register`. `registry` is typed loosely for the same
    reason (avoids a circular import into `registry.py`).

    `priority=10` matches `FPLProvider`'s: there is no overlap in
    capabilities served today (the FPL provider serves none of these five),
    so priority ordering is not actually exercised — set the same way for
    consistency, not because a fallback decision was made here.
    """
    provider = PLProvider(
        transport, season=season, identity_season=identity_season, elements=elements, teams=teams, identity=identity
    )
    from fplai.registry import CoverageSpec  # local import: see docstring above

    coverage = CoverageSpec(seasons=frozenset({season}), competitions=frozenset({"PL"}), priority=priority)
    for capability in provider.capabilities():
        registry.register(capability, provider, coverage)
    return provider
