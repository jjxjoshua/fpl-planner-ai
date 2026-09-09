"""Tests for fplai.providers.odds — The Odds API provider adapter (E2b
story 8). `HttpTransport` is replaced with a fake `.get()` (no network) for
every test except the secret-redaction test, which mounts a real
`requests.Session` against an offline transport adapter — still zero real
network I/O, but real enough to prove the key genuinely reaches the
outgoing request while never reaching anything this module writes to disk
or raises in an error message.

Fixture payloads are trimmed, hand-shaped copies of the REAL response
shapes verified live 2026-08-21 (see providers/odds.py's module docstring
and docs/wiki/provider-framework.md) — not invented shapes."""

from __future__ import annotations

import json
import logging

import polars as pl
import pytest

from fplai.identity import IdentityError
from fplai.providers.base import ProviderError
from fplai.providers.odds import (
    MATCH_ODDS_FIXTURE,
    _PlayerNameIndex,
    PLAYER_GOAL_ODDS_FIXTURE,
    OddsProvider,
    build_transport,
    register,
    suppress_leaky_third_party_debug_logging,
)
from fplai.registry import CapabilityRegistry
from fplai.schemas import CANONICAL_SCHEMAS


class FakeTransport:
    """Stands in for transport.HttpTransport. `responses` maps an endpoint
    PATH (exactly what OddsProvider.fetch builds, including the query
    string) to a (status, body_dict_or_str). Records the `cost` passed on
    every call so credit-accounting is asserted, not assumed."""

    def __init__(self, responses: dict[str, tuple[int, object]]):
        self.responses = responses
        self.calls: list[tuple[str, int]] = []

    def get(self, path: str, *, force_refresh: bool = False, cost: int = 1):
        self.calls.append((path, cost))
        if path not in self.responses:
            raise AssertionError(f"unexpected request: {path}. Known: {list(self.responses)}")
        status, body = self.responses[path]
        text = body if isinstance(body, str) else json.dumps(body)
        return status, text


# -- fixtures shared across tests --------------------------------------------

# `id` (season-local 1-20 slot) and `code` (stable, e.g. Arsenal=3) are
# DELIBERATELY different numbers here, on purpose — real bug found live,
# 2026-08-21, first run of scripts/verify_odds_provider.py: `elements[].
# team` holds the team's `id`, NOT its `code`, but `_TeamNameIndex.resolve`
# returns `code` (matching this codebase's "team_code" convention
# elsewhere, e.g. identity.py's TeamIdentityMap). If any test fixture used
# the SAME numbers for both, this class of bug would have passed the
# entire suite silently — the way it did before this live run caught it.
TEAMS = pl.DataFrame(
    [
        {"id": 1, "code": 3, "name": "Arsenal"},
        {"id": 7, "code": 9, "name": "Coventry City"},
        {"id": 11, "code": 43, "name": "Man City"},
        {"id": 17, "code": 6, "name": "Spurs"},
    ]
)

ELEMENTS = pl.DataFrame(
    [
        {"id": 1, "team": 1, "first_name": "Bukayo", "second_name": "Saka", "web_name": "Saka"},
        {"id": 2, "team": 1, "first_name": "Gabriel", "second_name": "dos Santos Magalhães", "web_name": "Gabriel"},
        {"id": 3, "team": 1, "first_name": "Benjamin", "second_name": "White", "web_name": "White"},
        {"id": 4, "team": 7, "first_name": "Ellis", "second_name": "Simms", "web_name": "Simms"},
        {"id": 5, "team": 7, "first_name": "Frank", "second_name": "Onyeka", "web_name": "Onyeka"},
        {"id": 6, "team": 1, "first_name": "Kaine", "second_name": "Kesler-Hayden", "web_name": "Kesler-Hayden"},
        {"id": 7, "team": 1, "first_name": "Martin", "second_name": "Ødegaard", "web_name": "Ødegaard"},
    ]
)

MATCH_ODDS_ENDPOINT = "sports/soccer_epl/odds?markets=h2h,totals&regions=uk"

MATCH_ODDS_PAYLOAD = [
    {
        "id": "evt1",
        "commence_time": "2026-08-21T19:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Coventry City",
        "bookmakers": [
            {
                "key": "betfair_ex_uk",
                "title": "Betfair",
                "last_update": "2026-08-21T03:55:06Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-08-21T03:55:06Z",
                        "outcomes": [
                            {"name": "Arsenal", "price": 1.21},
                            {"name": "Coventry City", "price": 20.0},
                            {"name": "Draw", "price": 8.0},
                        ],
                    },
                    # An UNREQUESTED market (only h2h/totals were asked for)
                    # — verified live, Betfair Exchange returns this
                    # unprompted. Must not break ingestion.
                    {
                        "key": "h2h_lay",
                        "last_update": "2026-08-21T03:55:06Z",
                        "outcomes": [{"name": "Arsenal", "price": 1.22}],
                    },
                    {
                        "key": "totals",
                        "last_update": "2026-08-21T03:54:02Z",
                        "outcomes": [
                            {"name": "Over", "price": 1.53, "point": 2.5},
                            {"name": "Under", "price": 2.4, "point": 2.5},
                        ],
                    },
                ],
            }
        ],
    },
    {
        "id": "evt2",
        "commence_time": "2026-08-22T14:00:00Z",
        # Both odds-api-style full names that need the alias table, not
        # the noise-word normaliser alone (blueprint §3.5's already-known
        # exceptions, verified live for a second provider this story).
        "home_team": "Manchester City",
        "away_team": "Tottenham Hotspur",
        "bookmakers": [
            {
                "key": "coral",
                "title": "Coral",
                "last_update": "2026-08-21T04:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-08-21T04:00:00Z",
                        "outcomes": [
                            {"name": "Manchester City", "price": 1.3},
                            {"name": "Tottenham Hotspur", "price": 9.0},
                            {"name": "Draw", "price": 6.0},
                        ],
                    }
                ],
            }
        ],
    },
]

GOALSCORER_ENDPOINT_EVT1 = "sports/soccer_epl/events/evt1/odds?markets=player_goal_scorer_anytime&regions=uk"

GOALSCORER_PAYLOAD_RESOLVABLE = {
    "id": "evt1",
    "commence_time": "2026-08-21T19:00:00Z",
    "home_team": "Arsenal",
    "away_team": "Coventry City",
    "bookmakers": [
        {
            "key": "williamhill",
            "title": "William Hill",
            "last_update": "2026-08-21T03:54:22Z",
            "markets": [
                {
                    "key": "player_goal_scorer_anytime",
                    "last_update": "2026-08-21T03:54:22Z",
                    "outcomes": [
                        {"name": "Yes", "description": "Bukayo Saka", "price": 2.1},
                        # Surname-first, no full-name match possible (FPL's
                        # full name is "Gabriel dos Santos Magalhães") —
                        # resolves via the surname/web_name fallback rung
                        # (last token "gabriel" == web_name "Gabriel").
                        {"name": "Yes", "description": "Magalhaes Gabriel", "price": 3.5},
                        {"name": "Yes", "description": "Ben White", "price": 6.0},
                        {"name": "Yes", "description": "Kaine Hayden", "price": 8.0},
                        {"name": "Yes", "description": "Martin Odegaard", "price": 3.0},
                        {"name": "Yes", "description": "Ellis Simms", "price": 4.0},
                    ],
                }
            ],
        }
    ],
}

GOALSCORER_PAYLOAD_WITH_UNRESOLVABLE = {
    "id": "evt1",
    "commence_time": "2026-08-21T19:00:00Z",
    "home_team": "Arsenal",
    "away_team": "Coventry City",
    "bookmakers": [
        {
            "key": "williamhill",
            "title": "William Hill",
            "last_update": "2026-08-21T03:54:22Z",
            "markets": [
                {
                    "key": "player_goal_scorer_anytime",
                    "last_update": "2026-08-21T03:54:22Z",
                    "outcomes": [
                        {"name": "Yes", "description": "Bukayo Saka", "price": 2.1},
                        # Real, live-verified miss (see providers/odds.py's
                        # module docstring): a fully reordered legal name
                        # with an extra given name, defeating every
                        # deterministic rung.
                        # Genuinely absent from FPL's element list — verified against the
                        # real 27 Aug capture, where this name stayed unresolved after
                        # rung 3 landed. It replaced "Ogochukwu Onyeka Frank" on
                        # 2026-08-27: that name was chosen as a stand-in for "unknown
                        # player", but it is Frank Onyeka (element 5 in ELEMENTS) with
                        # the feed's name order, and rung 3 correctly resolves it. The
                        # fixture was wrong, not the resolver.
                        {"name": "Yes", "description": "Tyrell Sellars-Fleming", "price": 5.5},
                    ],
                }
            ],
        }
    ],
}


def make_provider(match_odds_payload=MATCH_ODDS_PAYLOAD, goalscorer_responses=None, elements=ELEMENTS, teams=TEAMS):
    responses = {MATCH_ODDS_ENDPOINT: (200, match_odds_payload)}
    if goalscorer_responses:
        responses.update(goalscorer_responses)
    transport = FakeTransport(responses)
    provider = OddsProvider(transport, season="2026-27", elements=elements, teams=teams)
    return provider, transport


# -- capability coverage ------------------------------------------------------


def test_capabilities_lists_exactly_the_two_served():
    provider, _ = make_provider()
    assert provider.capabilities() == [MATCH_ODDS_FIXTURE, PLAYER_GOAL_ODDS_FIXTURE]


def test_supports_rejects_unknown_capability():
    provider, _ = make_provider()
    from fplai.schemas import CapabilityKey

    assert provider.supports(CapabilityKey("player", "shots_on_target", "fixture")) is False


# -- match.odds@fixture --------------------------------------------------


def test_fetch_match_odds_builds_canonical_rows_and_resolves_teams():
    provider, transport = make_provider()
    result = provider.fetch(MATCH_ODDS_FIXTURE, force_refresh=True)
    CANONICAL_SCHEMAS[MATCH_ODDS_FIXTURE].validate(result.rows)

    # cost accounting: ONE call, cost=2 (markets=2 x regions=1) regardless
    # of how many fixtures/bookmakers/outcomes it contains.
    assert transport.calls == [(MATCH_ODDS_ENDPOINT, 2)]

    df = result.rows
    assert df.height == (3 + 1 + 2) + 3  # evt1: h2h(3)+h2h_lay(1)+totals(2)=6; evt2: h2h(3)
    evt1 = df.filter(pl.col("provider_event_id") == "evt1")
    assert set(evt1["home_team_code"].unique().to_list()) == {3}  # Arsenal's stable code, NOT its id=1
    assert set(evt1["away_team_code"].unique().to_list()) == {9}  # Coventry's stable code, NOT its id=7

    # totals outcomes carry a point; h2h/h2h_lay do not.
    totals_rows = evt1.filter(pl.col("market_key") == "totals")
    assert totals_rows["outcome_point"].to_list() == [2.5, 2.5]
    h2h_rows = evt1.filter(pl.col("market_key") == "h2h")
    assert all(p is None for p in h2h_rows["outcome_point"].to_list())

    # evt2 exercises the alias table (Manchester City/Tottenham Hotspur).
    evt2 = df.filter(pl.col("provider_event_id") == "evt2")
    assert evt2["home_team_code"].unique().to_list() == [43]
    assert evt2["away_team_code"].unique().to_list() == [6]

    # h2h_lay — an unrequested market Betfair Exchange returns anyway —
    # survives untouched, not dropped or coerced into 'h2h'.
    assert "h2h_lay" in df["market_key"].to_list()


def test_fetch_match_odds_entity_key_is_unique_within_the_batch():
    provider, _ = make_provider()
    result = provider.fetch(MATCH_ODDS_FIXTURE, force_refresh=True)
    key_cols = CANONICAL_SCHEMAS[MATCH_ODDS_FIXTURE].entity_key
    df = result.rows.select(list(key_cols))
    assert df.height == df.unique().height


def test_fetch_match_odds_timestamps_are_naive_not_tz_aware():
    # Real bug found live, 2026-08-21 (first run of scripts/snapshot_odds.py):
    # a tz-AWARE commence_time/market_last_update wrote as Parquet TIMESTAMP
    # WITH TIME ZONE — the first capability in this store to do so — and
    # reading it back via BitemporalStore.observations()/as_of() raised
    # `InvalidInputException: Required module 'pytz' failed to import`
    # (pytz is not a project dependency). See providers/odds.py's
    # _parse_iso docstring for the full incident record. This is a
    # regression guard on the fixture-level mock path; the store round-trip
    # test below proves it against a REAL BitemporalStore/DuckDB read.
    provider, _ = make_provider()
    result = provider.fetch(MATCH_ODDS_FIXTURE, force_refresh=True)
    for col in ("commence_time", "market_last_update"):
        assert result.rows.schema[col].time_zone is None, f"{col} must be naive UTC, not tz-aware"


def test_fetch_match_odds_rows_round_trip_through_a_real_store(tmp_path):
    # The test that would have caught the pytz bug BEFORE a live capture
    # ever ran — writes through a REAL, tmp_path-backed BitemporalStore
    # (not a mock) and reads it back via the same DuckDB path
    # scripts/snapshot_odds.py uses. Handoff lesson #6: "tests on fresh
    # fixtures cannot catch production bugs" — this one deliberately goes
    # through Parquet + DuckDB, not just polars-in-memory assertions.
    from datetime import datetime, timezone

    from fplai.store import BitemporalStore

    provider, _ = make_provider()
    result = provider.fetch(MATCH_ODDS_FIXTURE, force_refresh=True)

    store = BitemporalStore(base_path=tmp_path / "store")
    now = datetime.now(timezone.utc)
    store.write(
        "odds_match_odds",
        result.rows,
        valid_at=now,
        observed_at=result.observed_at,
        source="odds_api:match_odds",
        provider_id=result.provider_id,
        capability=str(result.capability),
        endpoint=result.endpoint,
    )

    read_back = store.observations("odds_match_odds", until=datetime.now(timezone.utc))
    assert read_back.height == result.rows.height


def test_fetch_match_odds_raises_on_unresolved_team_name():
    bad_payload = [
        {
            "id": "evt9",
            "commence_time": "2026-08-21T19:00:00Z",
            "home_team": "Definitely Not A Real Club FC",
            "away_team": "Coventry City",
            "bookmakers": [
                {
                    "key": "coral",
                    "title": "Coral",
                    "last_update": "2026-08-21T04:00:00Z",
                    "markets": [{"key": "h2h", "last_update": "2026-08-21T04:00:00Z", "outcomes": [{"name": "Draw", "price": 5.0}]}],
                }
            ],
        }
    ]
    provider, _ = make_provider(match_odds_payload=bad_payload)
    with pytest.raises(IdentityError, match="Definitely Not A Real Club FC"):
        provider.fetch(MATCH_ODDS_FIXTURE, force_refresh=True)


# -- player.goal_odds@fixture ---------------------------------------------


def test_fetch_player_goal_odds_resolves_every_name_and_reports_full_match():
    provider, transport = make_provider(goalscorer_responses={GOALSCORER_ENDPOINT_EVT1: (200, GOALSCORER_PAYLOAD_RESOLVABLE)})
    result = provider.fetch(PLAYER_GOAL_ODDS_FIXTURE, event_id="evt1", force_refresh=True)
    CANONICAL_SCHEMAS[PLAYER_GOAL_ODDS_FIXTURE].validate(result.rows)

    assert (GOALSCORER_ENDPOINT_EVT1, 1) in transport.calls  # cost=1/event

    df = result.rows
    assert df.height == 6  # every one of the 6 outcomes resolved
    resolved_ids = set(df["player_element_id"].to_list())
    assert resolved_ids == {1, 2, 3, 4, 6, 7}  # Saka, Gabriel, White, Simms, Kesler-Hayden, Ødegaard
    assert df["identity_resolved"].to_list() == [True] * 6
    # player_name_raw is kept verbatim, not overwritten by the resolved FPL name.
    assert "Magalhaes Gabriel" in df["player_name_raw"].to_list()
    assert "Martin Odegaard" in df["player_name_raw"].to_list()
    # A fully-resolved fetch reports zero unresolved names.
    assert result.meta["unresolved_player_names"] == []
    assert result.meta["n_unresolved_rows"] == 0


def test_fetch_player_goal_odds_entity_key_is_unique_within_the_batch():
    provider, _ = make_provider(goalscorer_responses={GOALSCORER_ENDPOINT_EVT1: (200, GOALSCORER_PAYLOAD_RESOLVABLE)})
    result = provider.fetch(PLAYER_GOAL_ODDS_FIXTURE, event_id="evt1", force_refresh=True)
    key_cols = CANONICAL_SCHEMAS[PLAYER_GOAL_ODDS_FIXTURE].entity_key
    df = result.rows.select(list(key_cols))
    assert df.height == df.unique().height


# -- blueprint §12.5's re-fetchability exception (amended 2026-08-21) ------
# player.goal_odds@fixture is live-only with no historical endpoint
# (blueprint §3.3) — raising on one unresolved name discarded 41 good
# observations permanently on this adapter's first live capture. The fix:
# preserve the row (null id, identity_resolved=false, raw name kept),
# never raise, never silently drop. These tests prove all three
# constraints the coordinator set, not just the "doesn't raise" headline.


def test_fetch_player_goal_odds_preserves_unresolved_rows_never_raises_or_drops():
    provider, _ = make_provider(
        goalscorer_responses={GOALSCORER_ENDPOINT_EVT1: (200, GOALSCORER_PAYLOAD_WITH_UNRESOLVABLE)}
    )
    # Must NOT raise — this is the whole point of the exception.
    result = provider.fetch(PLAYER_GOAL_ODDS_FIXTURE, event_id="evt1", force_refresh=True)
    CANONICAL_SCHEMAS[PLAYER_GOAL_ODDS_FIXTURE].validate(result.rows)

    df = result.rows
    # GOALSCORER_PAYLOAD_WITH_UNRESOLVABLE has 2 outcomes: Bukayo Saka
    # (resolves) and Tyrell Sellars-Fleming (does not). BOTH rows must be
    # present — nothing dropped, resolved or not.
    assert df.height == 2

    unresolved = df.filter(pl.col("player_name_raw") == "Tyrell Sellars-Fleming")
    assert unresolved.height == 1
    assert unresolved["player_element_id"].to_list() == [None]
    assert unresolved["identity_resolved"].to_list() == [False]

    resolved = df.filter(pl.col("player_name_raw") == "Bukayo Saka")
    assert resolved.height == 1
    assert resolved["player_element_id"].to_list() == [1]
    assert resolved["identity_resolved"].to_list() == [True]


def test_fetch_player_goal_odds_reports_unresolved_names_in_meta_loud_place_2_of_3():
    # Blueprint §12.5's "loud in three places": (1) the row is flagged,
    # (2) the raw name is kept, (3) the CAPTURE reports every unresolved
    # entity by name. This is (3)'s data source — scripts/snapshot_odds.py
    # reads this to log the run-level summary the coordinator asked for.
    provider, _ = make_provider(
        goalscorer_responses={GOALSCORER_ENDPOINT_EVT1: (200, GOALSCORER_PAYLOAD_WITH_UNRESOLVABLE)}
    )
    result = provider.fetch(PLAYER_GOAL_ODDS_FIXTURE, event_id="evt1", force_refresh=True)
    assert result.meta["unresolved_player_names"] == ["Tyrell Sellars-Fleming"]
    assert result.meta["n_unresolved_rows"] == 1


def test_fetch_player_goal_odds_unresolved_rows_are_unusable_by_default_via_plain_join():
    # "Unusable by default": a consumer doing the ORDINARY thing (an
    # equi-join against elements on player_element_id == id) must get
    # NOTHING for unresolved rows, without needing to know to filter
    # first. This is the structural guarantee, proven against a real
    # polars join, not asserted in prose.
    provider, _ = make_provider(
        goalscorer_responses={GOALSCORER_ENDPOINT_EVT1: (200, GOALSCORER_PAYLOAD_WITH_UNRESOLVABLE)}
    )
    result = provider.fetch(PLAYER_GOAL_ODDS_FIXTURE, event_id="evt1", force_refresh=True)
    df = result.rows
    assert df.height == 2  # both present in the raw fetch (preserved, not discarded)

    naive_join = df.join(ELEMENTS, left_on="player_element_id", right_on="id", how="inner")
    # The unresolved row (null player_element_id) is structurally excluded
    # by ordinary INNER JOIN semantics (NULL never equals anything) — the
    # consumer did nothing special and still never sees it.
    assert naive_join.height == 1
    assert naive_join["player_name_raw"].to_list() == ["Bukayo Saka"]


def test_fetch_player_goal_odds_unresolved_rows_are_reachable_via_explicit_opt_in():
    # The other half of "unusable by default, never unusable at all": a
    # consumer that DOES explicitly opt in (filter identity_resolved) can
    # still see the preserved row and its raw name, e.g. to repair it
    # offline — this is the whole reason the row was kept instead of
    # dropped.
    provider, _ = make_provider(
        goalscorer_responses={GOALSCORER_ENDPOINT_EVT1: (200, GOALSCORER_PAYLOAD_WITH_UNRESOLVABLE)}
    )
    result = provider.fetch(PLAYER_GOAL_ODDS_FIXTURE, event_id="evt1", force_refresh=True)
    opted_in = result.rows.filter(~pl.col("identity_resolved"))
    assert opted_in.height == 1
    assert opted_in["player_name_raw"].to_list() == ["Tyrell Sellars-Fleming"]
    assert opted_in["player_element_id"].to_list() == [None]


def test_fetch_player_goal_odds_mixed_resolved_and_unresolved_round_trips_through_a_real_store(tmp_path):
    # Same discipline as the match.odds@fixture store round-trip test —
    # proves the null player_element_id/identity_resolved combination
    # survives a REAL Parquet write + DuckDB read, not just an in-memory
    # polars assertion (handoff lesson #6).
    from datetime import datetime, timezone

    from fplai.store import BitemporalStore

    provider, _ = make_provider(
        goalscorer_responses={GOALSCORER_ENDPOINT_EVT1: (200, GOALSCORER_PAYLOAD_WITH_UNRESOLVABLE)}
    )
    result = provider.fetch(PLAYER_GOAL_ODDS_FIXTURE, event_id="evt1", force_refresh=True)

    store = BitemporalStore(base_path=tmp_path / "store")
    now = datetime.now(timezone.utc)
    store.write(
        "odds_player_goal_odds",
        result.rows,
        valid_at=now,
        observed_at=result.observed_at,
        source="odds_api:player_goal_odds",
        provider_id=result.provider_id,
        capability=str(result.capability),
        endpoint=result.endpoint,
    )
    read_back = store.observations("odds_player_goal_odds", until=datetime.now(timezone.utc))
    assert read_back.height == 2
    assert sorted(read_back["identity_resolved"].to_list()) == [False, True]


def test_fetch_player_goal_odds_outcome_name_is_always_yes_in_live_data():
    # Documents the verified live shape this schema relies on (blueprint
    # §3.3's de-vig note) — not a behavioural assertion about the adapter,
    # a regression guard on the fixture itself matching reality.
    outcomes = GOALSCORER_PAYLOAD_RESOLVABLE["bookmakers"][0]["markets"][0]["outcomes"]
    assert all(o["name"] == "Yes" for o in outcomes)
    assert all("description" in o for o in outcomes)


# -- register(): the E2b gate, exercised end to end ------------------------


def test_gate_register_is_adapter_plus_registry_entry_zero_core_changes():
    registry = CapabilityRegistry()
    transport = FakeTransport(
        {MATCH_ODDS_ENDPOINT: (200, MATCH_ODDS_PAYLOAD), GOALSCORER_ENDPOINT_EVT1: (200, GOALSCORER_PAYLOAD_RESOLVABLE)}
    )
    provider = register(registry, transport, season="2026-27", elements=ELEMENTS, teams=TEAMS)
    assert provider.provider_id == "odds_api"

    resolved = registry.resolve(MATCH_ODDS_FIXTURE, season="2026-27", competition="PL")
    assert resolved is provider
    result = resolved.fetch(MATCH_ODDS_FIXTURE, force_refresh=True)
    assert result.rows.height > 0

    resolved2 = registry.resolve(PLAYER_GOAL_ODDS_FIXTURE, season="2026-27", competition="PL")
    assert resolved2 is provider


def test_register_does_not_serve_a_season_it_was_not_given():
    registry = CapabilityRegistry()
    transport = FakeTransport({MATCH_ODDS_ENDPOINT: (200, MATCH_ODDS_PAYLOAD)})
    register(registry, transport, season="2026-27", elements=ELEMENTS, teams=TEAMS)
    from fplai.registry import RegistryError

    # Odds are live-only (blueprint §3.3, /historical/ 401) — a request for
    # a past season must NOT silently get this provider back.
    with pytest.raises(RegistryError):
        registry.resolve(MATCH_ODDS_FIXTURE, season="2025-26", competition="PL")


# -- secret redaction: THE load-bearing test for this story -----------------


def test_build_transport_key_reaches_the_request_but_never_the_cache_or_errors(tmp_path):
    """A real requests.Session (via build_transport), mounted against an
    offline transport adapter — proves BOTH halves at once: (1) the key
    genuinely reaches the outgoing HTTP request (auth would work live),
    and (2) nothing this module writes to disk, or raises in an error
    message, contains any substring of the key."""
    import requests
    from requests.adapters import BaseAdapter

    secret = "sk_live_THIS_MUST_NEVER_BE_CACHED_0000"

    class _RecordingAdapter(BaseAdapter):
        def __init__(self, status: int, body: str):
            super().__init__()
            self.captured_urls: list[str] = []
            self._status = status
            self._body = body

        def send(self, request, **kwargs):
            self.captured_urls.append(request.url)
            resp = requests.Response()
            resp.status_code = self._status
            resp._content = self._body.encode("utf-8")
            resp.url = request.url
            resp.headers["x-requests-remaining"] = "499"
            resp.headers["x-requests-used"] = "1"
            return resp

        def close(self) -> None:
            pass

    cache_dir = tmp_path / "cache" / "odds"
    transport = build_transport(cache_dir, api_key=secret)
    adapter = _RecordingAdapter(status=200, body=json.dumps(MATCH_ODDS_PAYLOAD))
    transport.session.mount("https://", adapter)
    transport.session.mount("http://", adapter)

    status, text = transport.get(MATCH_ODDS_ENDPOINT, force_refresh=True)
    assert status == 200

    # 1. The REAL outgoing request DID carry the key — auth genuinely works.
    assert adapter.captured_urls, "adapter never received a request"
    assert any(f"apiKey={secret}" in u for u in adapter.captured_urls)

    # 2. The cache directory — filenames AND file contents — never
    #    contains the key.
    cache_files = list(cache_dir.rglob("*"))
    cache_files = [f for f in cache_files if f.is_file()]
    assert cache_files, "expected at least one cache file to have been written"
    for f in cache_files:
        assert secret not in f.name
        assert secret not in f.read_text(encoding="utf-8")

    # 3. A raised ProviderError (non-200) never contains the key either —
    #    same transport construction, a failing response this time.
    bad_adapter = _RecordingAdapter(status=404, body="not found")
    transport2 = build_transport(tmp_path / "cache2", api_key=secret)
    transport2.session.mount("https://", bad_adapter)
    provider = OddsProvider(transport2, season="2026-27", elements=ELEMENTS, teams=TEAMS)
    with pytest.raises(ProviderError) as exc_info:
        provider.fetch(MATCH_ODDS_FIXTURE, force_refresh=True)
    assert secret not in str(exc_info.value)

    # 4. A raised TransportError (retries exhausted) never contains it either.
    from fplai.transport import TransportError

    class _AlwaysBlocked(BaseAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.status_code = 429
            resp._content = b"blocked"
            resp.url = request.url
            return resp

        def close(self) -> None:
            pass

    transport3 = build_transport(tmp_path / "cache3", api_key=secret)
    transport3.max_retries = 0
    transport3.backoff_base_seconds = 0.0
    transport3.session.mount("https://", _AlwaysBlocked())
    with pytest.raises(TransportError) as exc_info2:
        transport3.get(MATCH_ODDS_ENDPOINT, force_refresh=True)
    assert secret not in str(exc_info2.value)


def test_build_transport_raises_when_env_var_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    with pytest.raises(KeyError):
        build_transport(tmp_path / "cache")


def test_build_transport_reads_key_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("THE_ODDS_API_KEY", "env-supplied-key")
    transport = build_transport(tmp_path / "cache")
    assert transport.session.params == {"apiKey": "env-supplied-key"}


# -- the urllib3 debug-logging leak, found live 2026-08-21 -----------------
# `scripts/snapshot_odds.py --verbose` printed `THE_ODDS_API_KEY` in
# plaintext to the terminal on its FIRST real run — urllib3's own
# connection-pool logger logs the full request URL at DEBUG level,
# entirely outside `_redacted_session`'s reach (that function only stops
# the key from reaching a URL STRING this codebase builds; it has no say
# over what a third-party library logs about the request it sends). Fixed
# by capping urllib3's logger to WARNING, unconditionally, inside
# `build_transport()` — these tests prove the fix, not just narrate it.


@pytest.fixture
def _restore_urllib3_log_level():
    urllib3_logger = logging.getLogger("urllib3")
    original = urllib3_logger.level
    yield urllib3_logger
    urllib3_logger.setLevel(original)  # never leak global logging state into other tests


def test_suppress_leaky_third_party_debug_logging_caps_urllib3_below_debug(_restore_urllib3_log_level):
    urllib3_logger = _restore_urllib3_log_level
    urllib3_logger.setLevel(logging.DEBUG)  # simulate a --verbose run's root-level DEBUG propagating in
    assert urllib3_logger.isEnabledFor(logging.DEBUG)  # sanity: the leak condition is real before the fix

    suppress_leaky_third_party_debug_logging()

    assert not urllib3_logger.isEnabledFor(logging.DEBUG)
    assert urllib3_logger.isEnabledFor(logging.WARNING)


def test_build_transport_suppresses_urllib3_debug_logging_as_a_side_effect(monkeypatch, tmp_path, _restore_urllib3_log_level):
    monkeypatch.setenv("THE_ODDS_API_KEY", "env-supplied-key")
    urllib3_logger = _restore_urllib3_log_level
    urllib3_logger.setLevel(logging.DEBUG)  # simulate a --verbose run

    build_transport(tmp_path / "cache")

    assert not urllib3_logger.isEnabledFor(logging.DEBUG)


# --- _PlayerNameIndex rung 3 (added 2026-08-27) ------------------------------
#
# The odds feed supplies FULL LEGAL names; FPL's web_name is often a mononym,
# a short form, or a hyphen-split surname that is NOT the trailing token.
# Measured against the real 27 Aug capture: 17 of 26 unresolved names newly
# resolved, ZERO regressions among the ~657 already-resolved rows, every new
# match confirmed by hand against FPL's own first_name + second_name.

RUNG3_ELEMENTS = pl.DataFrame(
    [
        # web_name is the FIRST token of the odds feed's full legal name, and
        # FPL's own first+second does NOT equal the feed's string — taken
        # verbatim from the real element 472, so rung 1 genuinely cannot fire
        # here. An earlier version of this fixture used the feed's exact name
        # as first+second, which made this test pass on rung 1 with rung 3
        # deleted — i.e. it could not fail (CLAUDE.md lesson 5). Caught by
        # actually deleting the fix and re-running.
        {"id": 20, "team": 1, "first_name": "Murillo", "second_name": "Costa dos Santos", "web_name": "Murillo"},
        # ß survives NFKD folding — FPL writes "Groß", the feed writes "Gross"
        {"id": 21, "team": 1, "first_name": "Pascal", "second_name": "Groß", "web_name": "Groß"},
        # FPL splits its own hyphens; the feed sends one trailing token
        {"id": 22, "team": 1, "first_name": "Emile", "second_name": "Smith Rowe", "web_name": "Smith Rowe"},
        # two players sharing an identifying token, in the same candidate pool
        {"id": 23, "team": 7, "first_name": "Joe", "second_name": "Gomez", "web_name": "Gomez"},
        {"id": 24, "team": 7, "first_name": "Diego", "second_name": "Gomez Amarilla", "web_name": "Gomez"},
    ]
)


def _rung3_index():
    return _PlayerNameIndex(RUNG3_ELEMENTS, TEAMS, {3, 9})


def test_player_name_index_resolves_a_mononym_that_is_not_the_last_token():
    # "Murillo Santiago Costa dos Santos" — rung 2's last-token rule looks up
    # "santos" and finds nothing; the identifying token is the FIRST one.
    assert _rung3_index().resolve("Murillo Santiago Costa dos Santos") == 20


def test_player_name_index_folds_eszett_so_gross_matches_gross():
    # ß does not decompose under NFKD, so "Groß" indexed as "groß" and could
    # never meet the feed's "Gross" until ß was added to the transliteration map.
    assert _rung3_index().resolve("Pascal Gross") == 21


def test_player_name_index_resolves_a_hyphenated_surname_from_the_feed_side():
    assert _rung3_index().resolve("Emile Smith-Rowe") == 22


def test_player_name_index_refuses_an_ambiguous_token_rather_than_picking_one():
    # THE SAFETY PROPERTY, and the reason rung 3 unions across tokens instead
    # of returning the first hit: a wrong join is worse than an unresolved name
    # (CLAUDE.md lesson 4; the live incident where an identity join on the wrong
    # FPL column resolved 0/42). Two Gomezes share the pool — resolve must
    # return None, and the caller preserves the row with identity_resolved=false.
    assert _rung3_index().resolve("Diego Alexander Gomez Amarilla") is None


def test_player_name_index_ignores_tokens_too_short_to_identify_anyone():
    # Particles and initials ("dos", "da", "ta", "n") carry no identity and
    # only add collision surface. "Dos Santos" here must NOT reach Murillo
    # via the particle — it resolves only because "santos" is his own token…
    # so use a name whose ONLY overlap with the pool is a short particle.
    assert _rung3_index().resolve("Ta Bi") is None

