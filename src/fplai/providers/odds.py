"""The Odds API provider adapter — E2b story 8 (blueprint §3.3, §3.3.1,
§12, §12.5).

Base: `https://api.the-odds-api.com/v4/`. Two capabilities served, verified
live 2026-08-21 against the real API with the live key, NOT assumed from
blueprint §3.3 (which itself corrects an earlier wrong recon — see that
section's amendment history):

  - `match.odds@fixture`  <- GET sports/soccer_epl/odds, markets=h2h,totals,
    regions=uk. ONE call answers every fixture (cost = markets x regions =
    2 credits total, not per fixture).
  - `player.goal_odds@fixture` <- GET sports/soccer_epl/events/{id}/odds,
    markets=player_goal_scorer_anytime, regions=uk. PER-EVENT (~1 credit
    each).

`player_shots_on_target` is DELIBERATELY DEFERRED, not registered — another
~10 credits/gameweek, and blueprint §3.3 names de-vigged anytime-goalscorer
as the one market to keep if only one could be kept.

**Store raw market prices. Derive nothing (blueprint §3.3.1).** No de-vig,
no implied-probability column, and above all no P(start) gating here — a
market price reaching a decision without passing through the minutes model
already produced one wrong recommendation on this project (§3.3.1's "hard
rule" section). De-vig / normalisation-against-team-goal-expectation is
model-layer work, Phase 2 at the earliest. This adapter's job is a faithful
observation of what the market quoted, nothing more.

**Bitemporal: `observed_at` genuinely is `now()` here.** Unlike an archive
adapter imputing a historical timestamp, a live odds fetch IS an
observation made at that instant — see `schemas.py`'s two schema
descriptions for the full ruling, including `valid_at` (the earliest
bookmaker `last_update` in the batch — the same "anchor a multi-instant
batch at its earliest instant" convention `fplai.backfill._valid_at_for`
uses for fixtures).

**Identity resolution is the real risk here, not the HTTP plumbing** —
verified live, in full, below. The Odds API speaks team/player NAMES; FPL
speaks integer ids. `identity.py` (E2b story 7) is READ-ONLY for this
story (not touched — its numeric PL-API-id joins don't apply to a provider
that never returns a numeric id at all), so the name-based resolution
below is entirely local to this module, reusing `fplai.identity.
IdentityError` for a consistent error taxonomy without changing that
module. Per blueprint §12.5: **unmatched entities raise, never silently
drop, never fuzzy-match** — WITH ONE EXCEPTION, added 2026-08-21, that
applies to `player.goal_odds@fixture` only (see point 2 below). Two live
findings, both verified against the real GW1 2026/27 fixture list and
player pool:

1. **Teams: 20/20, but NOT via naive normalisation alone.** The Odds API's
   team-name convention is the PL API's/Opta's full-name style ('Manchester
   City'), not FPL's own short `teams[].name` ('Man City') — the SAME four
   exceptions blueprint §3.5 already documented for the PL-API-name
   mismatch (`_TEAM_NAME_ALIASES` below), not a new finding, the same
   underlying gap surfacing against a second provider. The other 16/20
   resolve via a documented noise-word normalisation (strip 'fc'/'afc'/
   'and'/'hove'/'albion'/'city'/'united'/'town'/'wanderers'). Team-name
   resolution keeps §12.5's ORIGINAL rule — an unresolved team raises for
   the whole batch, no exception, same as `match.odds@fixture`.

2. **Players: verified 41/42 on one live fixture (Arsenal v Coventry
   City), one genuine unresolved name — and the fixture that produced the
   §12.5 re-fetchability exception.** The Odds API uses full LEGAL names,
   sometimes with first/last order INVERTED relative to FPL's `(first_
   name, second_name)` pair (e.g. 'Magalhaes Gabriel' for FPL's Gabriel
   dos Santos Magalhães), sometimes truncating a hyphenated surname (e.g.
   'Kaine Hayden' for FPL's Kaine Kesler-Hayden), and Nordic/other letters
   NFKD does not fold (ø, æ, ð, ł, đ, œ — 'Martin Odegaard' for FPL's
   Martin Ødegaard). Resolution is three EXACT rungs (never a similarity/
   edit-distance fuzzy match), scoped to the fixture's own two squads
   (restricting the candidate pool to ~40-60 players instead of the full
   599 sharply reduces false-positive risk from the surname-only fallback
   rungs):

   a. Normalised "first_name second_name" == normalised Odds API name
      (whole string), unique within the two-team candidate pool.
   b. Normalised FPL `web_name` (or one of its hyphen/whitespace-split
      parts, to survive a truncated hyphenated surname) == the LAST
      whitespace token of the normalised Odds API name, unique within the
      pool.
   c. Anything else: UNRESOLVED — and, as of the §12.5 amendment below,
      NO LONGER RAISED. Never a fourth rung guessing at name-token order
      either; permuting every ordering and testing pool membership is
      exactly the guess-until-something-matches heuristic §12.5 forbids,
      re-fetchability exception or not.

   **The one live miss, and the case the exception exists for:**
   'Ogochukwu Onyeka Frank' (Odds API) for FPL's Frank Onyeka (Coventry
   City, web_name 'Onyeka') — a THREE-part name with the token FPL calls
   `first_name` ('Frank') placed LAST and an extra given name
   ('Ogochukwu') FPL does not carry at all.

**The §12.5 re-fetchability exception (blueprint, amended 2026-08-21,
after this adapter's FIRST live capture lost 9 of 10 fixtures of
`player.goal_odds@fixture` to this single unresolvable name under the OLD
all-or-nothing rule):** bookmaker odds have no historical endpoint on the
free plan (§3.3) — a fixture's pre-match prices cease to exist at kickoff
and cannot be bought back. Raising on one unresolvable name out of
forty-two therefore discarded forty-one GOOD observations permanently,
which is itself the silent, irrecoverable data loss §12.5 exists to
prevent — just arriving by a louder route. `_fetch_player_goal_odds`
(below) now PRESERVES an unresolved player row instead: `player_element_
id = None`, `identity_resolved = False`, `player_name_raw` kept verbatim,
so identity can be repaired OFFLINE from the stored string with no
re-fetch — exactly what this live-only source cannot offer. This is
**scoped to `player.goal_odds@fixture`'s player-name join ONLY** —
`match.odds@fixture`'s team-name join, and every re-fetchable source
(archives, backfill, anything with a historical endpoint) still raise
all-or-nothing, unchanged. Unresolved rows are UNUSABLE BY DEFAULT: a
genuine nullable `Int64` `player_element_id` means an ordinary equi-join
against `elements` on `player_element_id == id` structurally excludes
them (NULL never equals anything) without a consumer needing to remember
to filter first — see `tests/test_provider_odds.py` for the tests proving
this, not just asserting it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl

from fplai.identity import IdentityError
from fplai.providers.base import FetchResult, ProviderError
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    MATCH_ODDS_FIXTURE,
    PLAYER_GOAL_ODDS_FIXTURE,
    CapabilityKey,
)
from fplai.store import content_hash
from fplai.transport import CreditPolicy, HttpTransport

if TYPE_CHECKING:
    import requests

logger = logging.getLogger("fplai.providers.odds")

BASE_URL = "https://api.the-odds-api.com/v4/"
SPORT_KEY = "soccer_epl"

_MATCH_ODDS_ENDPOINT = f"sports/{SPORT_KEY}/odds"
_EVENT_ODDS_ENDPOINT = f"sports/{SPORT_KEY}/events/{{event_id}}/odds"

MATCH_ODDS_MARKETS = "h2h,totals"
GOALSCORER_MARKETS = "player_goal_scorer_anytime"
REGIONS = "uk"  # blueprint §3.3: eu doubles cost for marginal extra books — uk alone

# -- name normalisation ------------------------------------------------------
# NFKD folds most diacritics (é -> e) but NOT these — they are independent
# Latin letters in Unicode, not "base letter + combining mark", so NFKD
# leaves them untouched. Verified live: 'Martin Odegaard' (Odds API) vs FPL's
# 'Martin Ødegaard' would otherwise fail to normalise to the same key.
_LATIN_TRANSLITERATION = {
    "ø": "o", "Ø": "O",  # ø Ø
    "æ": "ae", "Æ": "AE",  # æ Æ
    "ð": "d", "Ð": "D",  # ð Ð
    "ł": "l", "Ł": "L",  # ł Ł
    "đ": "d", "Đ": "D",  # đ Đ
    "œ": "oe", "Œ": "OE",  # œ Œ
    # ß does NOT decompose under NFKD — it survives folding as itself, so
    # FPL's "Groß" indexed as "groß" while the odds feed's "Pascal Gross"
    # normalised to "gross" and the two could never meet. Found 2026-08-27
    # against a real capture, not in review.
    "ß": "ss", "ẞ": "SS",  # ß ẞ
}


def _fold(s: str) -> str:
    s = "".join(_LATIN_TRANSLITERATION.get(c, c) for c in s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


# Rung 3 of _PlayerNameIndex.resolve — see its own comment for the
# measurement this threshold came from.
_NAME_TOKEN_SPLIT = re.compile(r"[-\s]+")
_MIN_IDENTIFYING_TOKEN = 4

_TEAM_NAME_NOISE = re.compile(r"\b(fc|afc|and|hove|albion|city|united|town|wanderers)\b")


def _normalise_team_key(name: str) -> str:
    s = _fold(name).strip()
    s = _TEAM_NAME_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# Verified live 2026-08-21 against the real GW1 2026/27 fixture list (20/20
# clubs, docs/wiki/provider-framework.md has the full record): the Odds
# API's team-name convention is the PL API's/Opta's full-name style, not
# FPL's own short `teams[].name`. These are the SAME four exceptions
# blueprint §3.5 already documented for the PL-API-vs-FPL name mismatch —
# an explicit, verified lookup, never a guess. Keyed by the Odds API's own
# (lowercased) team name -> FPL `teams[].name`.
_TEAM_NAME_ALIASES: dict[str, str] = {
    "manchester united": "Man Utd",
    "manchester city": "Man City",
    "tottenham hotspur": "Spurs",
    "nottingham forest": "Nott'm Forest",
}


def _normalise_player_key(name: str) -> str:
    s = _fold(name)
    s = re.sub(r"[^a-z0-9\s-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class _TeamNameIndex:
    """Odds API team-name string -> FPL team `code`. Built once per fetch
    from the live `teams` snapshot the caller supplies (blueprint §12.5's
    'identity maps are season-scoped' collapses to a single case here:
    this provider only ever serves the LIVE season — there is no
    historical odds data to resolve against at all, blueprint §3.3's
    /historical/ 401)."""

    def __init__(self, teams: pl.DataFrame) -> None:
        required = ("code", "name")
        missing = [c for c in required if c not in teams.columns]
        if missing:
            raise IdentityError(f"teams snapshot is missing column(s) {missing} needed for odds team-name resolution")
        by_norm: dict[str, list[int]] = {}
        by_exact_name: dict[str, int] = {}
        for row in teams.select("code", "name").to_dicts():
            code, name = row["code"], row["name"]
            if code is None or name is None:
                continue
            by_exact_name[name] = int(code)
            by_norm.setdefault(_normalise_team_key(name), []).append(int(code))
        self._by_norm = by_norm
        self._by_exact_name = by_exact_name

    def resolve(self, odds_team_name: str) -> int:
        raw_key = odds_team_name.strip().lower()
        alias = _TEAM_NAME_ALIASES.get(raw_key)
        if alias is not None:
            code = self._by_exact_name.get(alias)
            if code is not None:
                return code
        hits = self._by_norm.get(_normalise_team_key(odds_team_name), [])
        if len(hits) == 1:
            return hits[0]
        raise IdentityError(
            f"unresolved team: Odds API team name {odds_team_name!r} has no unique match in "
            "the FPL teams snapshot (blueprint §12.5 — unmatched entities raise, never "
            f"silently drop or guess). Candidates found: {hits}."
        )


class _PlayerNameIndex:
    """Odds API player-name string -> FPL element id, restricted to a
    candidate pool of just the two squads playing in one fixture (sharply
    reduces false-positive risk on the surname-only fallback rung vs.
    matching against the full 599-player pool). Three exact rungs — see
    this module's docstring for the live verification and match rate.

    **`team_codes` are FPL `teams[].code` values (the stable id
    `_TeamNameIndex.resolve()` returns and `home_team_code`/
    `away_team_code` are stamped with) — but `elements[].team` is the
    season-LOCAL `teams[].id` (1-20), a DIFFERENT number
    (e.g. live-verified: Arsenal `id=1`, `code=3`).** Found live, 2026-08-21
    (`scripts/verify_odds_provider.py`'s first real run): filtering
    `elements` by `code` against the `team` column silently produced a
    near-empty/wrong candidate pool — every single outcome in the test
    fixture came back unresolved, not the ~41/42 verified in this module's
    docstring. `teams` (needs `id`, `code`) is required here specifically
    to translate code -> id before filtering; `elements[].team` is never
    compared against a `code` value anywhere in this class."""

    def __init__(self, elements: pl.DataFrame, teams: pl.DataFrame, team_codes: set[int]) -> None:
        required = ("id", "team", "first_name", "second_name", "web_name")
        missing = [c for c in required if c not in elements.columns]
        if missing:
            raise IdentityError(f"elements snapshot is missing column(s) {missing} needed for odds player-name resolution")
        missing_teams = [c for c in ("id", "code") if c not in teams.columns]
        if missing_teams:
            raise IdentityError(f"teams snapshot is missing column(s) {missing_teams} needed to translate team code -> team id")
        code_to_id = {int(row["code"]): int(row["id"]) for row in teams.select("code", "id").to_dicts() if row["code"] is not None}
        missing_codes = [c for c in team_codes if c not in code_to_id]
        if missing_codes:
            raise IdentityError(f"team code(s) {missing_codes} not found in the teams snapshot — cannot build a player candidate pool")
        team_ids = {code_to_id[c] for c in team_codes}
        pool = elements.filter(pl.col("team").is_in(list(team_ids)))
        full_keys: dict[str, set[int]] = {}
        alt_keys: dict[str, set[int]] = {}
        for row in pool.select("id", "first_name", "second_name", "web_name").to_dicts():
            eid = int(row["id"])
            full = _normalise_player_key(f"{row['first_name']} {row['second_name']}")
            full_keys.setdefault(full, set()).add(eid)
            wn = _normalise_player_key(row["web_name"])
            alt_keys.setdefault(wn, set()).add(eid)
            for part in re.split(r"[-\s]+", wn):
                if part:
                    alt_keys.setdefault(part, set()).add(eid)
        self._full = full_keys
        self._alt = alt_keys

    def resolve(self, odds_player_name: str) -> int | None:
        """Returns the resolved FPL element id, or `None` if unresolved —
        the CALLER (`_fetch_player_goal_odds`) collects every `None` across
        the whole event before raising, so one bad name doesn't hide the
        others (blueprint §12.5, same pattern as providers/pl.py's lineup
        resolution)."""
        key = _normalise_player_key(odds_player_name)
        hits = self._full.get(key)
        if not hits and key:
            last_token = key.split()[-1]
            hits = self._alt.get(last_token)
        if not hits and key:
            # Rung 3, added 2026-08-27 after measuring a real capture: the
            # odds feed supplies a player's FULL LEGAL name while FPL's
            # web_name is frequently a mononym or short form that is NOT
            # the last token — "Murillo Santiago Costa dos Santos" is FPL's
            # "Murillo", "Estevao Oliveira Goncalves" is "Estêvão",
            # "Ruben Dias" is "Rúben", "Matheus Luiz Nunes" is "Matheus N.".
            # Rung 2's last-token rule cannot reach any of them. Hyphenated
            # surnames fail the same way from the other side: the FPL index
            # already splits its own names on hyphens, but "Emile
            # Smith-Rowe" arrives here as ONE trailing token.
            #
            # So: try EVERY token, splitting on hyphens as well as spaces,
            # and accept only if the union across tokens is a single
            # element. Tokens shorter than 4 characters are dropped —
            # particles like "dos"/"da"/"ta" and initials carry no identity
            # and only add collision surface.
            #
            # A wrong join is worse than an unresolved name (CLAUDE.md
            # lesson 4, and the live incident where an identity join on the
            # wrong FPL column resolved 0/42): this rung stays inside the
            # two-squad candidate pool, and any token union of size > 1
            # resolves to None rather than picking a winner. Measured on
            # the real 27 Aug capture: 16 of 26 unresolved names newly
            # resolved, ZERO ambiguous, every one confirmed by hand against
            # FPL's own first_name + second_name; the remaining 10 are
            # genuinely outside FPL's element list or spelling variants
            # ("Yeremi" vs FPL's "Yeremy") that this must NOT guess at.
            token_hits: set[int] = set()
            for token in _NAME_TOKEN_SPLIT.split(key):
                if len(token) < _MIN_IDENTIFYING_TOKEN:
                    continue
                token_hits |= self._alt.get(token, set())
            hits = token_hits
        if hits and len(hits) == 1:
            return next(iter(hits))
        return None


class OddsProvider:
    """Provider adapter over The Odds API. Season-scoped identity is
    trivial here (always the live/current season — see `_TeamNameIndex`'s
    docstring); `season` is still an explicit, required parameter, never
    hardcoded (CLAUDE.md rule 4), used only to stamp rows for provenance.
    """

    provider_id = "odds_api"
    policy = CreditPolicy(
        monthly_credits=500,
        cost_per_call_description=(
            "cost = markets x regions per call. match.odds@fixture: ONE call for ALL "
            "fixtures, markets=h2h,totals x regions=uk = 2 credits total. "
            "player.goal_odds@fixture: ONE call PER EVENT, markets=player_goal_scorer_"
            "anytime x regions=uk = ~1 credit/fixture."
        ),
    )

    def __init__(self, transport: HttpTransport, *, season: str, elements: pl.DataFrame, teams: pl.DataFrame) -> None:
        self.transport = transport
        self.season = season
        self._elements = elements
        self._teams = teams
        self._team_index = _TeamNameIndex(teams)

    def capabilities(self) -> list[CapabilityKey]:
        return [MATCH_ODDS_FIXTURE, PLAYER_GOAL_ODDS_FIXTURE]

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
        if capability == MATCH_ODDS_FIXTURE:
            return self._fetch_match_odds(force_refresh=force_refresh)
        if capability == PLAYER_GOAL_ODDS_FIXTURE:
            return self._fetch_player_goal_odds(force_refresh=force_refresh, **params)
        raise ProviderError(f"{self.provider_id} does not serve {capability}")

    def _fetch_match_odds(self, *, force_refresh: bool) -> FetchResult:
        # cost=2: markets(h2h,totals)=2 x regions(uk)=1. See CreditPolicy's
        # cost_per_call_description above — NOT auto-computed by the
        # transport, the caller states its own request shape's cost.
        endpoint = f"{_MATCH_ODDS_ENDPOINT}?markets={MATCH_ODDS_MARKETS}&regions={REGIONS}"
        status, text = self.transport.get(endpoint, force_refresh=force_refresh, cost=2)
        if status != 200:
            raise ProviderError(f"GET {endpoint} returned {status}: {text[:200]}")
        events = json.loads(text)
        if not isinstance(events, list) or not events:
            raise ProviderError(f"{endpoint} returned an unexpected/empty shape: {type(events)}")

        rows: list[dict] = []
        unresolved_teams: list[str] = []
        for event in events:
            provider_event_id = event.get("id")
            commence_time = event.get("commence_time")
            home_name, away_name = event.get("home_team"), event.get("away_team")
            if provider_event_id is None or commence_time is None or home_name is None or away_name is None:
                raise ProviderError(f"{endpoint}: event missing id/commence_time/home_team/away_team: {event}")
            try:
                home_code = self._team_index.resolve(home_name)
                away_code = self._team_index.resolve(away_name)
            except IdentityError:
                unresolved_teams.extend([home_name, away_name])
                continue
            for bm in event.get("bookmakers", []):
                for market in bm.get("markets", []):
                    for outcome in market.get("outcomes", []):
                        rows.append(
                            {
                                "season": self.season,
                                "provider_event_id": str(provider_event_id),
                                "home_team_code": home_code,
                                "away_team_code": away_code,
                                "commence_time": _parse_iso(commence_time),
                                "bookmaker_key": bm.get("key"),
                                "bookmaker_title": bm.get("title"),
                                "market_key": market.get("key"),
                                "market_last_update": _parse_iso(market.get("last_update")),
                                "outcome_name": outcome.get("name"),
                                "outcome_price": outcome.get("price"),
                                "outcome_point": outcome.get("point"),
                            }
                        )
        if unresolved_teams:
            raise IdentityError(
                f"{endpoint}: {len(unresolved_teams)} team name(s) could not be resolved to an FPL "
                f"team code (blueprint §12.5 — unmatched entities raise, never silently drop): "
                f"{unresolved_teams}."
            )
        if not rows:
            raise ProviderError(f"{endpoint} contained no bookmaker quotes for any fixture")

        df = pl.DataFrame(rows, infer_schema_length=None)
        CANONICAL_SCHEMAS[MATCH_ODDS_FIXTURE].validate(df)
        return FetchResult(
            rows=df,
            capability=MATCH_ODDS_FIXTURE,
            provider_id=self.provider_id,
            endpoint=endpoint,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
            meta={"n_fixtures": len(events)},
        )

    def _fetch_player_goal_odds(self, *, force_refresh: bool, event_id: str) -> FetchResult:
        """**Implements blueprint §12.5's re-fetchability exception**
        (amended 2026-08-21, after this method's first live capture lost
        9 of 10 fixtures to one unresolvable player name under the OLD
        all-or-nothing rule — see this module's docstring and
        docs/wiki/provider-framework.md §14 for the full incident).

        An unresolved player name is PRESERVED, never discarded and never
        raised for the whole event: the row is still written, with
        `player_element_id = None`, `identity_resolved = False`, and
        `player_name_raw` carrying the API's raw string verbatim — so
        identity can be repaired OFFLINE later, with no re-fetch, which is
        exactly what a live-only source (blueprint §3.3, /historical/ is
        401 on the free plan) cannot offer once a fixture kicks off. This
        is DELIBERATELY NOT the rejected "skip the misses" mode — nothing
        is silently dropped; the row exists, flagged, with its raw name
        kept. TEAM-name resolution (`_team_index.resolve`, just below) is
        NOT covered by this exception and still raises all-or-nothing for
        the whole event on a miss, per §12.5's original rule — the
        exception is scoped to `player.goal_odds@fixture`'s player-name
        join only, not to this adapter's identity resolution in general."""
        endpoint = _EVENT_ODDS_ENDPOINT.format(event_id=event_id) + f"?markets={GOALSCORER_MARKETS}&regions={REGIONS}"
        # cost=1: markets(player_goal_scorer_anytime)=1 x regions(uk)=1, per event.
        status, text = self.transport.get(endpoint, force_refresh=force_refresh, cost=1)
        if status != 200:
            raise ProviderError(f"GET {endpoint} returned {status}: {text[:200]}")
        event = json.loads(text)
        provider_event_id = event.get("id")
        commence_time = event.get("commence_time")
        home_name, away_name = event.get("home_team"), event.get("away_team")
        if provider_event_id is None or commence_time is None or home_name is None or away_name is None:
            raise ProviderError(f"{endpoint}: event missing id/commence_time/home_team/away_team: {event}")
        # Team-name resolution is NOT covered by the re-fetchability
        # exception (blueprint §12.5 — "player.goal_odds@fixture ONLY"
        # refers to the PLAYER join, not this adapter's identity
        # resolution overall) — still raises all-or-nothing, same as
        # match.odds@fixture, if a team name is somehow unresolved.
        home_code = self._team_index.resolve(home_name)
        away_code = self._team_index.resolve(away_name)

        player_index = _PlayerNameIndex(self._elements, self._teams, {home_code, away_code})

        rows: list[dict] = []
        unresolved_names: list[str] = []
        for bm in event.get("bookmakers", []):
            for market in bm.get("markets", []):
                for outcome in market.get("outcomes", []):
                    # `description` carries the player's name; `name` is
                    # always 'Yes' for this market (blueprint §3.3's de-vig
                    # note — books only quote the Yes side) — see this
                    # module's docstring for the live-verified evidence.
                    raw_name = outcome.get("description")
                    if raw_name is None:
                        raise ProviderError(f"{endpoint}: outcome missing 'description' (player name): {outcome}")
                    element_id = player_index.resolve(raw_name)
                    identity_resolved = element_id is not None
                    if not identity_resolved:
                        unresolved_names.append(raw_name)
                    rows.append(
                        {
                            "season": self.season,
                            "provider_event_id": str(provider_event_id),
                            "home_team_code": home_code,
                            "away_team_code": away_code,
                            "commence_time": _parse_iso(commence_time),
                            "player_element_id": element_id,  # None when unresolved -- a genuine nullable int, never a sentinel
                            "player_name_raw": raw_name,
                            "identity_resolved": identity_resolved,
                            "bookmaker_key": bm.get("key"),
                            "bookmaker_title": bm.get("title"),
                            "market_key": market.get("key"),
                            "market_last_update": _parse_iso(market.get("last_update")),
                            "outcome_name": outcome.get("name"),
                            "outcome_price": outcome.get("price"),
                        }
                    )
        if not rows:
            raise ProviderError(f"{endpoint} contained no goalscorer outcomes for event {event_id}")

        df = pl.DataFrame(rows, infer_schema_length=None)
        # Explicit dtypes, not inferred -- the exact class of bug the
        # coordinator flagged: an all-null or mixed-null column left to
        # schema inference can silently land on the wrong dtype (store.py's
        # own _fetch_polars docstring warns of this same shape: "NULL-only
        # inference -> Null type, then first actual string value raises").
        # player_element_id must be a REAL nullable Int64 (never a
        # Null-dtype/string placeholder) for the "unusable by default"
        # structural join guarantee to hold -- a Null-dtype column doesn't
        # equi-join predictably at all.
        df = df.with_columns(
            pl.col("player_element_id").cast(pl.Int64),
            pl.col("identity_resolved").cast(pl.Boolean),
        )
        CANONICAL_SCHEMAS[PLAYER_GOAL_ODDS_FIXTURE].validate(df)
        distinct_unresolved = sorted(set(unresolved_names))
        if distinct_unresolved:
            # LOUD, per blueprint §12.5's three-places rule (row flagged,
            # raw name kept, capture reports every unresolved entity by
            # name) -- this is rung 3, "the capture reports every
            # unresolved entity by name". The caller (scripts/snapshot_
            # odds.py) also logs this at the run level; logging it here
            # too means it is impossible to fetch() this capability with
            # unresolved names and have NOTHING say so anywhere.
            logger.warning(
                "%s: %d distinct player name(s) preserved UNRESOLVED (identity_resolved=false, "
                "player_element_id=null, blueprint §12.5 re-fetchability exception -- not raised, "
                "not dropped): %s",
                endpoint,
                len(distinct_unresolved),
                distinct_unresolved,
            )
        return FetchResult(
            rows=df,
            capability=PLAYER_GOAL_ODDS_FIXTURE,
            provider_id=self.provider_id,
            endpoint=endpoint,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
            meta={"unresolved_player_names": distinct_unresolved, "n_unresolved_rows": len(unresolved_names)},
        )


def _parse_iso(value: str | None) -> datetime | None:
    """Parses the Odds API's 'Z'-suffixed ISO-8601 timestamps
    (`commence_time`, `last_update`) into NAIVE UTC `datetime` objects —
    deliberately, not tz-aware, matching this store's established
    convention for a capability's OWN data columns (e.g. `providers/pl.py`'s
    `kickoff`, built via a naive `datetime.strptime`). This is NOT the same
    axis as `FetchResult.observed_at` (bitemporal metadata, tz-aware
    everywhere, including in this module) — `store.write()` evidently
    normalises `observed_at`/`valid_at` to naive UTC internally before
    persisting them (out of scope to verify further here — `store.py` is
    READ-ONLY for this story), but it does nothing of the sort for a
    caller-supplied DATA column inside `rows`.

    **Found live, 2026-08-21, on the FIRST real capture run
    (`scripts/snapshot_odds.py`)**: an earlier version of this function
    returned a genuinely tz-aware datetime (`tzinfo=timezone.utc`).
    Polars/pyarrow wrote that as a real `TIMESTAMP WITH TIME ZONE` Parquet
    column — the first capability in this store to do so; every other
    provider's in-`rows` timestamp columns are naive. Reading it back
    (`BitemporalStore.observations()`/`as_of()`, via DuckDB) raised
    `InvalidInputException: Required module 'pytz' failed to import` —
    DuckDB's own TIMESTAMPTZ-to-Python conversion path needs `pytz`, which
    is NOT a project dependency (blueprint §3.2 only requires `tzdata`, a
    different package, for a different reason: Python's own `zoneinfo`).
    Fixed by matching the established naive-UTC convention rather than by
    adding a new dependency to close a gap only this adapter was opening —
    see docs/wiki/provider-framework.md for the full incident record,
    including the two already-written batches this affected."""
    if value is None:
        return None
    # The Odds API returns 'Z'-suffixed ISO-8601 (e.g. '2026-08-21T19:00:00Z');
    # datetime.fromisoformat only accepts '+00:00' before Python 3.11's
    # relaxed parser — normalise explicitly rather than assume the runtime.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


# -- transport construction: THE secret-redaction choke point --------------
#
# The Odds API authenticates with `?apiKey=<key>` in the query string. A
# naive integration would pass a URL/path containing that key straight to
# `HttpTransport.get()`, which builds `url = base_url + path` and then (a)
# hashes that exact string for the cache FILENAME
# (`ResponseCache._path`), (b) writes that exact string into the cache
# BODY (`ResponseCache.set`'s `{"url": url, ...}`), and (c) interpolates it
# into every `ProviderError`/`TransportError` message this module raises.
# That would put `THE_ODDS_API_KEY` in plaintext under `cache/` on every
# single call — CLAUDE.md's standing rule ("never read, print, or commit
# secrets") — and `cache/` being gitignored is not a defence (a key that
# reaches disk in plaintext is already a mistake regardless of whether the
# directory is committed).
#
# THE FIX: the key is attached via `requests.Session.params`, never via
# the URL string. `requests` merges `Session.params` into the OUTGOING
# request at send time (verified empirically —
# tests/test_provider_odds.py's leak test mounts a real `requests.Session`
# with a custom transport adapter and inspects the actual prepared request
# URL) — so authentication still works — but `path`/`url` as CONSTRUCTED
# by every method above, and therefore everything `HttpTransport.get()`
# hashes, caches, or puts in an error string, NEVER contains the key at
# all. This needed ZERO changes to `transport.py`'s `HttpTransport`/
# `ResponseCache` — the redaction is entirely a caller-side discipline
# (never build a path containing the key), not a transport-layer feature.
def _redacted_session(api_key: str) -> "requests.Session":
    import requests

    session = requests.Session()
    # Deliberately NOT session.headers — a header would be equally safe
    # from the cache-leak class of bug, but The Odds API's own documented
    # auth mechanism is the apiKey QUERY PARAMETER; matching it exactly
    # here avoids a second, undocumented behaviour to maintain.
    session.params = {"apiKey": api_key}
    return session


def suppress_leaky_third_party_debug_logging() -> None:
    """**SECURITY — found live, 2026-08-21, running `scripts/snapshot_odds.py
    --verbose` for real.** `urllib3`'s own connection-pool logger prints the
    FULL request URL at DEBUG level (e.g. `"GET /v4/sports/soccer_epl/odds
    ?markets=h2h,totals&regions=uk&apiKey=<32 hex chars> HTTP/1.1" 200
    3670`) — and for THIS provider, unlike FPL/PL API, that URL contains
    `apiKey=<THE_ODDS_API_KEY>` in plaintext. `--verbose` sets the ROOT
    logger to DEBUG; `urllib3`'s logger has no level of its own, so it
    inherits DEBUG from root and started emitting the key straight to the
    terminal — a leak in a THIRD-PARTY library's own logger, entirely
    outside `_redacted_session`'s reach (that function stops the key from
    ever reaching a `path`/`url` STRING this codebase constructs; it has no
    say over what `urllib3` itself logs about the request it sends).

    Called unconditionally from `build_transport()` below — every caller of
    this module's transport gets this protection automatically, rather
    than depending on every script remembering to call it. Idempotent
    (`setLevel` is not cumulative); safe to call more than once."""
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_transport(cache_dir: Path, *, api_key: str | None = None) -> HttpTransport:
    """Construct an `HttpTransport` for The Odds API with the key attached
    via `_redacted_session` above — never embedded in any path/URL string
    this module builds. `api_key` defaults to reading `THE_ODDS_API_KEY`
    from the environment (never a `.env` parser here — loading `.env` into
    the process environment is the CALLING SCRIPT's job, same as every
    other secret in this codebase; this function only ever reads
    `os.environ`, never a file). Raises `KeyError` naming the missing
    variable if it isn't set — never silently sends a blank key. This
    function itself never logs, prints, or returns the key in any form."""
    suppress_leaky_third_party_debug_logging()
    if api_key is None:
        api_key = os.environ["THE_ODDS_API_KEY"]
    if not api_key:
        raise ValueError("THE_ODDS_API_KEY is set but empty")
    session = _redacted_session(api_key)
    return HttpTransport(base_url=BASE_URL, policy=OddsProvider.policy, cache_dir=Path(cache_dir), session=session)


def register(
    registry,
    transport: HttpTransport,
    *,
    season: str,
    elements: pl.DataFrame,
    teams: pl.DataFrame,
    priority: int = 10,
) -> OddsProvider:
    """The 'registry entry' half of the E2b gate — mirrors `providers/pl.py`
    and `providers/vaastav.py`'s `register()` functions exactly. `registry`
    is typed loosely for the same reason those modules give (avoids a
    circular import into `registry.py`).

    No `seasons=` restriction beyond the current one in `CoverageSpec` —
    odds are LIVE-ONLY (blueprint §3.3, /historical/ is 401 on the free
    plan), so there is no meaningful multi-season coverage to declare; a
    caller asking for a past season simply won't get this provider back
    from `registry.resolve()`, which is the correct behaviour (never
    silently answer a season this provider cannot actually serve)."""
    provider = OddsProvider(transport, season=season, elements=elements, teams=teams)
    from fplai.registry import CoverageSpec  # local import: see docstring above

    coverage = CoverageSpec(seasons=frozenset({season}), competitions=frozenset({"PL"}), priority=priority)
    for capability in provider.capabilities():
        registry.register(capability, provider, coverage)
    return provider
