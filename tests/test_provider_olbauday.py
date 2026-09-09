"""Tests for fplai.providers.olbauday — the second archive adapter (E2b
story 10b, `observed_at` logic corrected 2026-08-21 after a live-data
review found the original ruling leaked). `FileTransport` is replaced with
a fake `.fetch()` returning canned bytes keyed by URL, raising
`TransportError` for anything not provided (standing in for a real 404) —
no network, no dependency on transport.py's own internals (those are
covered by test_transport.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fplai.providers.base import ProviderError
from fplai.providers.olbauday import BASE_URL, OlbaudayProvider, _parse_iso, register
from fplai.registry import CapabilityRegistry
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    GAMEWEEK_FIELD_SUMMARY_GAMEWEEK,
    PLAYER_ATTRIBUTES_GAMEWEEK,
)
from fplai.transport import TransportError

UTC = timezone.utc


class FakeFileTransport:
    """Stands in for transport.FileTransport. `files` maps a URL to raw
    bytes. Any URL not in `files` raises TransportError, mirroring a real
    404 after FileTransport's own retries are exhausted — this is the
    signal providers/olbauday.py's flat->nested path fallback and its
    observed_at-imputation join both key off."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.calls: list[str] = []

    def fetch(self, url: str, *, force_refresh: bool = False) -> bytes:
        self.calls.append(url)
        if url not in self.files:
            raise TransportError(f"GET {url} returned 404, expected 200")
        return self.files[url]


# -- fixtures: real column shapes (trimmed), verified live 2026-08-21 ------

# 2025-2026's real header has 87 columns; trimmed here to exactly the
# required_fields set (schemas.py, minus the two observed_at_* columns the
# ADAPTER adds, not the raw file) plus a handful of the 2025-26-only
# columns (web_name, news, defensive_contribution, tackles, recoveries) so
# the "present but not required" half of the drift handling is exercised.
_PLAYERSTATS_2025_2026_HEADER = (
    "id,status,chance_of_playing_next_round,chance_of_playing_this_round,now_cost,"
    "cost_change_event,selected_by_percent,total_points,bonus,bps,form,ep_next,ep_this,"
    "expected_goals,expected_assists,expected_goal_involvements,expected_goals_conceded,"
    "corners_and_indirect_freekicks_order,direct_freekicks_order,penalties_order,gw,"
    "set_piece_threat,web_name,news,news_added,defensive_contribution,tackles,recoveries"
)
_PLAYERSTATS_2025_2026_ROWS = (
    "1,a,100,100,145,0,45.2,10,3,30,5.0,6.0,5.5,0.5,0.3,0.8,1.1,1,1,1,1,90,Haaland,,,2,1,3\n"
    "1,a,100,100,145,0,46.0,15,3,35,5.2,6.1,5.6,0.7,0.3,1.0,1.1,1,1,1,2,88,Haaland,,,3,2,4\n"
)
_PLAYERSTATS_2025_2026 = (_PLAYERSTATS_2025_2026_HEADER + "\n" + _PLAYERSTATS_2025_2026_ROWS).encode("utf-8")

# 2024-2025: verified live — NO web_name/news/news_added/defensive_contribution/
# tackles/recoveries columns at all (58 vs. 87 columns; DC didn't exist as a
# scoring category before 2025/26).
_PLAYERSTATS_2024_2025_HEADER = (
    "id,status,chance_of_playing_next_round,chance_of_playing_this_round,now_cost,"
    "cost_change_event,selected_by_percent,total_points,bonus,bps,form,ep_next,ep_this,"
    "expected_goals,expected_assists,expected_goal_involvements,expected_goals_conceded,"
    "corners_and_indirect_freekicks_order,direct_freekicks_order,penalties_order,gw,"
    "set_piece_threat"
)
_PLAYERSTATS_2024_2025_ROW = "1,a,100,100,50,0,2.1,3,0,10,1.0,1.0,1.0,0.0,0.0,0.0,0.0,,,,1,\n"
_PLAYERSTATS_2024_2025 = (_PLAYERSTATS_2024_2025_HEADER + "\n" + _PLAYERSTATS_2024_2025_ROW).encode("utf-8")

# gameweek_summaries.csv — verified live: snapshot_time is a CONSTANT
# stale file-generation stamp (2025-08-10 here, standing in for the real
# archive's own constant), deliberately BEFORE gw1's deadline — this
# fixture exists precisely to prove the corrected adapter never uses it
# for observed_at, and to reproduce the artefact for the leakage tests
# below. Only gw1 and gw2 present, so a gw=2 fetch naturally exercises the
# "no gw+1 row -> fall back to this gw's own deadline + offset" rung.
_GWS_HEADER = "id,name,deadline_time,average_entry_score,finished,chip_plays,most_captained,most_vice_captained,ranked_count,snapshot_time"
_GWS_2025_2026 = (
    _GWS_HEADER + "\n"
    '1,Gameweek 1,2025-08-15T17:30:00+00:00,54,True,"[]",427,8,10000000,2025-08-10T04:46:20.565427+00:00\n'
    '2,Gameweek 2,2025-08-22T17:30:00+00:00,51,True,"[]",427,8,10752422,2025-08-10T04:46:20.565427+00:00\n'
).encode("utf-8")


def _provider(files: dict[str, bytes]) -> tuple[OlbaudayProvider, FakeFileTransport]:
    transport = FakeFileTransport(files)
    return OlbaudayProvider(transport), transport


# -- provider shape ------------------------------------------------------


def test_provider_id_and_policy():
    provider, _ = _provider({})
    assert provider.provider_id == "olbauday_archive"
    from fplai.transport import BulkFilePolicy

    assert isinstance(provider.policy, BulkFilePolicy)


def test_capabilities_lists_exactly_the_two_served():
    provider, _ = _provider({})
    assert set(provider.capabilities()) == {PLAYER_ATTRIBUTES_GAMEWEEK, GAMEWEEK_FIELD_SUMMARY_GAMEWEEK}


def test_supports_true_for_served_capability_false_otherwise():
    provider, _ = _provider({})
    assert provider.supports(PLAYER_ATTRIBUTES_GAMEWEEK) is True
    from fplai.schemas import TEAM_ATTRIBUTES_CURRENT

    assert provider.supports(TEAM_ATTRIBUTES_CURRENT) is False


# -- player.attributes@gameweek -------------------------------------------


def test_fetch_player_attributes_filters_by_gw_and_injects_season():
    playerstats_url = BASE_URL + "data/2025-2026/playerstats.csv"
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    provider, transport = _provider({playerstats_url: _PLAYERSTATS_2025_2026, gws_url: _GWS_2025_2026})

    result = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=1)

    assert result.rows.height == 1  # only the gw=1 row, not gw=2's
    assert result.rows["gw"][0] == 1
    assert result.rows["season"][0] == "2025-2026"
    assert result.rows["selected_by_percent"][0] == 45.2  # a PERCENTAGE, not a raw count
    assert result.provider_id == "olbauday_archive"
    assert result.capability == PLAYER_ATTRIBUTES_GAMEWEEK
    assert playerstats_url in transport.calls
    assert gws_url in transport.calls  # the deadline-imputation source, fetched for observed_at


def test_fetch_player_attributes_validates_against_schema():
    playerstats_url = BASE_URL + "data/2025-2026/playerstats.csv"
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    provider, _ = _provider({playerstats_url: _PLAYERSTATS_2025_2026, gws_url: _GWS_2025_2026})
    result = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=1)
    CANONICAL_SCHEMAS[PLAYER_ATTRIBUTES_GAMEWEEK].validate(result.rows)  # must not raise


def test_fetch_player_attributes_observed_at_is_imputed_from_next_gameweek_deadline():
    # THE corrected ruling (2026-08-21): observed_at is imputed from
    # gameweek N+1's own deadline_time, NEVER from snapshot_time (verified
    # live to be a stale, constant file-generation stamp — see module
    # docstring). gw=1's row -> gw=2's deadline.
    playerstats_url = BASE_URL + "data/2025-2026/playerstats.csv"
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    provider, _ = _provider({playerstats_url: _PLAYERSTATS_2025_2026, gws_url: _GWS_2025_2026})
    result = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=1)

    assert result.observed_at == datetime(2025, 8, 22, 17, 30, tzinfo=UTC)  # gw2's deadline, NOT snapshot_time
    assert result.rows["observed_at_source"][0].endswith("deadline_time(gw=2)")
    assert result.rows["observed_at_imputed"][0] is True
    # The raw stamp is kept for forensics ONLY, clearly labelled, never used above.
    assert result.meta["file_generation_stamp"] == "2025-08-10T04:46:20.565427+00:00"
    assert "lag_hours" not in result.meta  # dropped — distance to a stale stamp measures nothing


def test_fetch_player_attributes_last_gameweek_in_file_falls_back_to_own_deadline_plus_offset():
    # gw=2 has no gw=3 row in this fixture (mirrors a season's real final
    # gameweek) -> falls back to gw2's OWN deadline_time + the documented
    # _FINAL_GAMEWEEK_OBSERVED_AT_OFFSET (7 days), not gw2's snapshot_time.
    playerstats_url = BASE_URL + "data/2025-2026/playerstats.csv"
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    provider, _ = _provider({playerstats_url: _PLAYERSTATS_2025_2026, gws_url: _GWS_2025_2026})
    result = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=2)

    assert result.observed_at == datetime(2025, 8, 22, 17, 30, tzinfo=UTC) + timedelta(days=7)
    assert "deadline_time(gw=2)+7 days" in result.rows["observed_at_source"][0]
    assert result.rows["observed_at_imputed"][0] is True


def test_fetch_player_attributes_gw1_and_gw2_get_different_observed_at():
    # The corrected behaviour is the OPPOSITE of the original bug: rows
    # for different gameweeks now get genuinely DIFFERENT observed_at
    # values (unlike the original snapshot_time join, which stamped every
    # gameweek in a season identically — see the leakage tests below for
    # why that was dangerous, not merely imprecise).
    playerstats_url = BASE_URL + "data/2025-2026/playerstats.csv"
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    provider, _ = _provider({playerstats_url: _PLAYERSTATS_2025_2026, gws_url: _GWS_2025_2026})
    r1 = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=1)
    r2 = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=2)
    assert r1.observed_at != r2.observed_at
    assert r1.observed_at < r2.observed_at


def test_fetch_player_attributes_2024_2025_falls_back_to_nested_path():
    # 2024-2025's playerstats.csv lives at .../playerstats/playerstats.csv,
    # not the flat path 2025-2026/2026-2027 use. The flat URL 404s
    # (TransportError) and the adapter must retry the nested one.
    flat_url = BASE_URL + "data/2024-2025/playerstats.csv"
    nested_url = BASE_URL + "data/2024-2025/playerstats/playerstats.csv"
    gws_url = BASE_URL + "data/2024-2025/gameweek_summaries.csv"
    provider, transport = _provider({nested_url: _PLAYERSTATS_2024_2025, gws_url: _GWS_2025_2026})

    result = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2024-2025", gameweek=1)

    assert result.endpoint == "data/2024-2025/playerstats/playerstats.csv"
    assert flat_url in transport.calls  # the failed attempt still happened
    assert nested_url in transport.calls
    assert "web_name" not in result.rows.columns  # confirms genuinely the old, narrower shape


def test_fetch_player_attributes_2024_2025_missing_columns_still_validates():
    flat_url = BASE_URL + "data/2024-2025/playerstats.csv"
    gws_url = BASE_URL + "data/2024-2025/gameweek_summaries.csv"
    provider, _ = _provider({flat_url: _PLAYERSTATS_2024_2025, gws_url: _GWS_2025_2026})
    result = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2024-2025", gameweek=1)
    CANONICAL_SCHEMAS[PLAYER_ATTRIBUTES_GAMEWEEK].validate(result.rows)  # must not raise


def test_fetch_player_attributes_raises_provider_error_when_no_deadline_source_exists():
    # The real 2024-2025 case: playerstats.csv IS fetchable, but
    # gameweek_summaries.csv (the only in-archive deadline source) does
    # not exist for that season at all — no per-row timestamp, nothing to
    # impute from, so this MUST raise ProviderError, never fall back to
    # now() or a wrong deadline (blueprint §12.5).
    flat_url = BASE_URL + "data/2024-2025/playerstats.csv"
    provider, _ = _provider({flat_url: _PLAYERSTATS_2024_2025})  # no gameweek_summaries.csv at all
    with pytest.raises(ProviderError, match="NO IN-ARCHIVE DEADLINE SOURCE"):
        provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2024-2025", gameweek=1)


def test_fetch_player_attributes_raises_provider_error_when_gw_and_gw_plus_1_both_missing():
    playerstats_url = BASE_URL + "data/2025-2026/playerstats.csv"
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    # gameweek_summaries.csv exists but has no row for gw=99 OR gw=100 —
    # construct a playerstats fixture that HAS a gw=99 row so the
    # imputation is the thing that fails, not the earlier "no rows for
    # this gw" filter.
    playerstats_gw99 = _PLAYERSTATS_2025_2026_HEADER.encode("utf-8") + b"\n" + (
        b"1,a,100,100,145,0,45.2,10,3,30,5.0,6.0,5.5,0.5,0.3,0.8,1.1,1,1,1,99,90,Haaland,,,2,1,3\n"
    )
    provider, _ = _provider({playerstats_url: playerstats_gw99, gws_url: _GWS_2025_2026})
    with pytest.raises(ProviderError, match="cannot impute observed_at for gw=99"):
        provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=99)


def test_fetch_player_attributes_no_rows_for_requested_gw_raises():
    playerstats_url = BASE_URL + "data/2025-2026/playerstats.csv"
    provider, _ = _provider({playerstats_url: _PLAYERSTATS_2025_2026})
    with pytest.raises(ProviderError, match="no rows for gw=99"):
        provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=99)


def test_fetch_player_attributes_empty_decoded_frame_raises():
    playerstats_url = BASE_URL + "data/2025-2026/playerstats.csv"
    header_only = (_PLAYERSTATS_2025_2026_HEADER + "\n").encode("utf-8")
    provider, _ = _provider({playerstats_url: header_only})
    with pytest.raises(ProviderError):
        provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=1)


# -- gameweek.field_summary@gameweek --------------------------------------


def test_fetch_gameweek_field_summary_filters_by_gw_and_injects_season():
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    provider, transport = _provider({gws_url: _GWS_2025_2026})

    result = provider.fetch(GAMEWEEK_FIELD_SUMMARY_GAMEWEEK, season="2025-2026", gameweek=1)

    assert result.rows.height == 1
    assert result.rows["id"][0] == 1
    assert result.rows["season"][0] == "2025-2026"
    assert result.provider_id == "olbauday_archive"
    assert result.capability == GAMEWEEK_FIELD_SUMMARY_GAMEWEEK
    assert transport.calls == [gws_url]


def test_fetch_gameweek_field_summary_validates_against_schema():
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    provider, _ = _provider({gws_url: _GWS_2025_2026})
    result = provider.fetch(GAMEWEEK_FIELD_SUMMARY_GAMEWEEK, season="2025-2026", gameweek=1)
    CANONICAL_SCHEMAS[GAMEWEEK_FIELD_SUMMARY_GAMEWEEK].validate(result.rows)  # must not raise


def test_fetch_gameweek_field_summary_observed_at_is_imputed_from_next_gameweek_deadline():
    # gw=1's post-gameweek results (average_entry_score, chip_plays,
    # most_captained) cannot be known at gw1's own deadline — imputed as
    # gw2's deadline, exactly like player.attributes@gameweek, and NEVER
    # this row's own snapshot_time (a stale generation stamp) or its own
    # deadline_time.
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    provider, _ = _provider({gws_url: _GWS_2025_2026})
    result = provider.fetch(GAMEWEEK_FIELD_SUMMARY_GAMEWEEK, season="2025-2026", gameweek=1)

    assert result.observed_at == datetime(2025, 8, 22, 17, 30, tzinfo=UTC)
    assert result.rows["observed_at_source"][0].endswith("deadline_time(gw=2)")
    assert result.rows["observed_at_imputed"][0] is True
    assert result.meta["file_generation_stamp"] == "2025-08-10T04:46:20.565427+00:00"
    assert "lag_hours" not in result.meta


def test_fetch_gameweek_field_summary_missing_row_for_gw_raises():
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    provider, _ = _provider({gws_url: _GWS_2025_2026})
    with pytest.raises(ProviderError, match="no row for gw=99"):
        provider.fetch(GAMEWEEK_FIELD_SUMMARY_GAMEWEEK, season="2025-2026", gameweek=99)


def test_fetch_gameweek_field_summary_missing_file_raises_transport_error():
    # 2024-2025 has NO gameweek_summaries.csv at all (verified live, 404) —
    # a season not in the fake transport's files dict stands in for that.
    provider, _ = _provider({})
    with pytest.raises(TransportError):
        provider.fetch(GAMEWEEK_FIELD_SUMMARY_GAMEWEEK, season="2024-2025", gameweek=1)


def test_fetch_unsupported_capability_raises():
    from fplai.schemas import TEAM_ATTRIBUTES_CURRENT

    provider, _ = _provider({})
    with pytest.raises(ProviderError):
        provider.fetch(TEAM_ATTRIBUTES_CURRENT)


# -- register() ------------------------------------------------------------


def test_register_adds_provider_to_registry_for_both_capabilities():
    registry = CapabilityRegistry()
    transport = FakeFileTransport({})
    provider = register(registry, transport)

    assert isinstance(provider, OlbaudayProvider)
    assert registry.resolve(PLAYER_ATTRIBUTES_GAMEWEEK) is provider
    assert registry.resolve(GAMEWEEK_FIELD_SUMMARY_GAMEWEEK) is provider


def test_gate_new_provider_is_adapter_plus_registry_entry():
    # E2b's gate, exercised directly (same pattern as test_provider_fpl.py/
    # test_provider_pl.py's own gate tests): constructing a registry and
    # calling register() is ALL that's needed to make both olbauday
    # capabilities resolvable AND fetchable end-to-end — no change to
    # schemas.py's OWN LOGIC, registry.py, transport.py, decoders.py, or
    # providers/base.py was required (schemas.py gained new, additive
    # CapabilityKey/FactTableSchema/DATASET_ENTITY_KEYS entries, exactly
    # what the "adding a new provider" steps already anticipate — see
    # docs/wiki/provider-framework.md §4).
    registry = CapabilityRegistry()
    playerstats_url = BASE_URL + "data/2025-2026/playerstats.csv"
    gws_url = BASE_URL + "data/2025-2026/gameweek_summaries.csv"
    transport = FakeFileTransport({playerstats_url: _PLAYERSTATS_2025_2026, gws_url: _GWS_2025_2026})
    register(registry, transport)

    provider = registry.resolve(PLAYER_ATTRIBUTES_GAMEWEEK)
    result = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season="2025-2026", gameweek=1)
    assert result.rows.height == 1


def test_register_does_not_collide_with_vaastav_player_gameweek_stats():
    # blueprint §12.1's ruling: no priority contest between olbauday's
    # attribute snapshot and vaastav's match-performance capability —
    # they are different keys entirely, so registering both providers
    # must not make either resolve() call ambiguous.
    from fplai.providers.vaastav import register as register_vaastav
    from fplai.schemas import PLAYER_GAMEWEEK_STATS_GAMEWEEK

    registry = CapabilityRegistry()
    olbauday_transport = FakeFileTransport({})
    vaastav_transport = FakeFileTransport({})
    olbauday_provider = register(registry, olbauday_transport)
    vaastav_provider = register_vaastav(registry, vaastav_transport)

    assert registry.resolve(PLAYER_ATTRIBUTES_GAMEWEEK) is olbauday_provider
    assert registry.resolve(PLAYER_GAMEWEEK_STATS_GAMEWEEK) is vaastav_provider


def test_register_priority_defers_to_live_providers():
    registry = CapabilityRegistry()
    transport = FakeFileTransport({})
    register(registry, transport)
    entries = registry._entries[PLAYER_ATTRIBUTES_GAMEWEEK]
    assert entries[0].coverage.priority == 20


# -- leakage-invariant tests (coordinator-directed correction, Task 3) -----
#
# The original suite went fully green over an adapter that, once written
# through a real BitemporalStore, leaked a later gameweek's row into an
# earlier gameweek's as_of() query — handoff lesson #5 territory (a test
# suite that cannot catch the actual defect is worse than no suite). These
# two tests exercise a REAL fplai.store.BitemporalStore (tmp_path-backed,
# runs fully offline) rather than only the adapter in isolation, because
# the leakage lives in the INTERACTION between this adapter's observed_at
# and store.as_of()'s entity-key partitioning (schemas.DATASET_ENTITY_KEYS
# = ("season", "gw", "id") for this dataset) — an adapter-only test cannot
# see it. `_buggy_join_observed_at` below reproduces EXACTLY the original,
# corrected bug (observed_at = this gameweek's own snapshot_time, joined
# straight from gameweek_summaries.csv) — deliberately duplicated here,
# not imported from anywhere, because the real fix removed that code path
# entirely; this reproduction exists solely so these two tests can prove,
# without git archaeology, that they WOULD have caught the original bug.

_GWS_LEAK_TEST = (
    _GWS_HEADER + "\n"
    '1,Gameweek 1,2025-08-15T17:30:00+00:00,54,True,"[]",427,8,10000000,2025-08-10T04:46:20.565427+00:00\n'
    '2,Gameweek 2,2025-08-22T17:30:00+00:00,51,True,"[]",427,8,10500000,2025-08-10T04:46:20.565427+00:00\n'
    '3,Gameweek 3,2025-08-29T17:30:00+00:00,58,True,"[]",8,427,10800000,2025-08-10T04:46:20.565427+00:00\n'
    '4,Gameweek 4,2025-09-05T17:30:00+00:00,60,True,"[]",8,427,11000000,2025-08-10T04:46:20.565427+00:00\n'
).encode("utf-8")

_LEAK_TEST_SEASON = "2099-2100"


def _leak_test_playerstats_row(gw: int, cum_points: int) -> bytes:
    return (
        f"1,a,100,100,145,0,45.2,{cum_points},3,30,5.0,6.0,5.5,0.5,0.3,0.8,1.1,1,1,1,{gw},90,Haaland,,,2,1,3\n"
    ).encode("utf-8")


def _leak_test_playerstats_csv() -> bytes:
    header = _PLAYERSTATS_2025_2026_HEADER.encode("utf-8") + b"\n"
    rows = b"".join(_leak_test_playerstats_row(gw, pts) for gw, pts in [(1, 5), (2, 12), (3, 20), (4, 25)])
    return header + rows


def _buggy_join_observed_at(gws_df, gameweek: int):
    """Reproduces the ORIGINAL bug this story's coordinator correction
    removed: observed_at = the gameweek's OWN snapshot_time, joined
    straight from gameweek_summaries.csv. Used ONLY by the two tests below
    to prove they would have caught it — never imported by, or reachable
    from, the real adapter."""
    import polars as pl

    row = gws_df.filter(pl.col("id").cast(pl.Utf8) == str(int(gameweek)))
    return _parse_iso(row.to_dicts()[0]["snapshot_time"])


def _write_gw_rows_to_store(store, dataset, provider, gws_url, playerstats_url, transport, *, gameweeks, use_buggy_observed_at):
    import polars as pl

    from fplai.providers.olbauday import _impute_observed_at

    for gw in gameweeks:
        result = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season=_LEAK_TEST_SEASON, gameweek=gw)
        observed_at = result.observed_at
        if use_buggy_observed_at:
            gws_df, _ = provider._fetch_gameweek_summaries_df(season=_LEAK_TEST_SEASON, force_refresh=False)
            observed_at = _buggy_join_observed_at(gws_df, gw)
            # Re-stamp the rows to reflect the buggy value (the real
            # provider no longer produces this; this loop is simulating
            # what a caller writing pre-correction FetchResults would have
            # done, matching exactly how a backfill orchestrator persists
            # FetchResult.observed_at as the store's observed_at).
        gws_all, _ = provider._fetch_gameweek_summaries_df(season=_LEAK_TEST_SEASON, force_refresh=False)
        this_gw_deadline = _parse_iso(gws_all.filter(pl.col("id").cast(pl.Utf8) == str(gw)).to_dicts()[0]["deadline_time"])
        store.write(
            dataset, result.rows, valid_at=this_gw_deadline, observed_at=observed_at,
            source="test", provider_id=provider.provider_id, capability=str(PLAYER_ATTRIBUTES_GAMEWEEK),
        )


def test_buggy_reproduction_violates_both_invariants_before_the_correction(tmp_path):
    """Proves the ORIGINAL bug (constant snapshot_time as observed_at)
    fails both invariants, using a real BitemporalStore — handoff lesson
    #5, run before trusting the corrected assertions below."""
    from fplai.store import BitemporalStore

    playerstats_url = BASE_URL + f"data/{_LEAK_TEST_SEASON}/playerstats.csv"
    gws_url = BASE_URL + f"data/{_LEAK_TEST_SEASON}/gameweek_summaries.csv"
    provider, transport = _provider({playerstats_url: _leak_test_playerstats_csv(), gws_url: _GWS_LEAK_TEST})

    store = BitemporalStore(base_path=tmp_path / "buggy_store")
    _write_gw_rows_to_store(
        store, "olbauday_player_attributes", provider, gws_url, playerstats_url, transport,
        gameweeks=[1, 2, 3, 4], use_buggy_observed_at=True,
    )

    # Invariant 1: no row's observed_at may precede the earliest moment its
    # information could exist (that gameweek's OWN deadline_time, at the
    # earliest). The buggy constant stamp (2025-08-10) is before ALL FOUR
    # gameweeks' deadlines — every single row violates this.
    all_rows = store.observations("olbauday_player_attributes", until=datetime(2030, 1, 1, tzinfo=UTC))
    violations = all_rows.filter(all_rows["observed_at"] < all_rows["valid_at"])
    assert violations.height == 4  # ALL FOUR rows violate it under the bug

    # Invariant 2: as_of() at GW2's deadline must never return a row for a
    # gameweek later than 2. Under the bug, every gameweek shares the same
    # observed_at (<= GW2's deadline), so GW3 and GW4 leak through.
    as_of_gw2_deadline = store.as_of("olbauday_player_attributes", datetime(2025, 8, 22, 17, 30, tzinfo=UTC))
    leaked_future_gws = as_of_gw2_deadline.filter(as_of_gw2_deadline["gw"] > 2)
    assert leaked_future_gws.height == 2  # GW3 and GW4 both leak — the exact bug reported


def test_corrected_adapter_satisfies_both_invariants(tmp_path):
    """Same store, same fixtures, the REAL (corrected) adapter — both
    invariants now hold."""
    from fplai.store import BitemporalStore

    playerstats_url = BASE_URL + f"data/{_LEAK_TEST_SEASON}/playerstats.csv"
    gws_url = BASE_URL + f"data/{_LEAK_TEST_SEASON}/gameweek_summaries.csv"
    provider, transport = _provider({playerstats_url: _leak_test_playerstats_csv(), gws_url: _GWS_LEAK_TEST})

    store = BitemporalStore(base_path=tmp_path / "fixed_store")
    _write_gw_rows_to_store(
        store, "olbauday_player_attributes", provider, gws_url, playerstats_url, transport,
        gameweeks=[1, 2, 3, 4], use_buggy_observed_at=False,
    )

    # Invariant 1: every row's observed_at is at or after its own
    # gameweek's deadline (the imputed value is gw+1's deadline, or gw's
    # own deadline + the documented offset for the last gameweek in the
    # file — both are >= that gameweek's own deadline by construction).
    all_rows = store.observations("olbauday_player_attributes", until=datetime(2030, 1, 1, tzinfo=UTC))
    violations = all_rows.filter(all_rows["observed_at"] < all_rows["valid_at"])
    assert violations.height == 0

    # Invariant 2: as_of() at GW2's deadline must never return a row for a
    # gameweek later than 2.
    as_of_gw2_deadline = store.as_of("olbauday_player_attributes", datetime(2025, 8, 22, 17, 30, tzinfo=UTC))
    leaked_future_gws = as_of_gw2_deadline.filter(as_of_gw2_deadline["gw"] > 2)
    assert leaked_future_gws.height == 0
    # And GW1's row (observed_at = GW2's own deadline) IS visible exactly
    # at that instant — the invariant isn't just "return nothing".
    assert as_of_gw2_deadline.filter(as_of_gw2_deadline["gw"] == 1).height == 1
