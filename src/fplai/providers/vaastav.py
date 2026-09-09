"""vaastav/Fantasy-Premier-League archive adapter — E2b story 10 (blueprint
§12, §3.4, §3.5, §12.5). First non-HTTP provider: composes `transport.
FileTransport` (fetch-and-cache a whole CSV file) with `decoders.decode`
("csv") — the transport/decoder split blueprint §12's Architect note asked
for, proven against real, verified data rather than a hypothetical.

**Repo layout — verified live, 2026-08-20, NOT assumed from the brief.**
`https://github.com/vaastav/Fantasy-Premier-League`, seasons 2016-17
through 2025-26 checked directly (`data-sources.md` §3.1 only sampled
GW10 2025-26; this session additionally pulled 2016-17's and 2025-26's
`players_raw.csv`, `teams.csv`, and `gws/gw1.csv` headers to confirm the
minimum stable column set across the widest span the archive has):

```
data/{season}/players_raw.csv     one row per player -- THAT season's own
                                   bootstrap-static 'elements' snapshot
data/{season}/teams.csv            one row per club, that season (code, name --
                                    same shape as FPL's own teams[])
data/{season}/gws/gw{N}.csv        one row per player, one gameweek
```

`{season}` is FPL's OWN "YYYY-YY" string (e.g. `"2025-26"`) — NOT
`providers/pl.py`'s starting-year convention. Never hardcoded; every call
site takes `season` as an explicit parameter (CLAUDE.md rule 4).

**Schema drift across seasons is real, verified, and handled explicitly —
not coerced.** 2016-17's `players_raw.csv` has no `opta_code` column at
all (the FPL API had no Opta join key that far back); 2016-17's gw CSV has
no `position`/`team`/`xP`/`expected_*`/`defensive_contribution` columns
that 2025-26's has. `schemas.py`'s `required_fields` for both capabilities
here are deliberately the columns verified present in BOTH the oldest and
newest seasons checked — see `schemas.py`'s module-level comment for the
full reasoning. A season missing even one of those genuinely-required
columns fails `FactTableSchema.validate()` loudly; this adapter does not
attempt to backfill/null-pad a missing column to make an old season look
like a new one.

**`player.identity@season` is the highest-value deliverable here** — it is
what unblocks §12.5's season-scoped identity resolution
(`fplai.identity.build_player_identity_map_for_season`): a historical
season's `PlayerIdentityMap` can now be built from THAT season's own
player list instead of today's, so resolving e.g. a 2025/26 match no
longer raises on players who have since left the league (that raise was
correct behaviour against the WRONG snapshot, per §12.5 — see identity.py).

**E2b story 7b adds `team.identity@season`** from `data/{season}/teams.csv`
— the team half of the same asymmetry §12.5 flags explicitly: team codes
persist across seasons so resolving against TODAY's team list happened to
work for any match involving only clubs still in the league, masking the
same missing-snapshot bug players had. It breaks the moment a fixture
involves a since-relegated club (verified live, story 11: PL numeric team
id 21 = West Ham, season 2025/26, not in the current 2026/27 20). Real,
verified drift found for THIS file specifically, distinct from
`players_raw.csv`'s: `teams.csv` does not exist at all for
2016-17/2017-18/2018-19 (confirmed 404 on all three; the archive only
started keeping it from 2019-20) — `players_raw.csv` has no such gap, it
goes back to 2016-17. A pre-2019-20 season therefore cannot be
team-identity-resolved from this archive at all; `FileTransport.fetch`
surfaces that as a `TransportError`, not a silent empty result. Column set
is otherwise stable 2019-20 -> 2025-26 (`id`, `code`, `name`, `short_name`
all present unchanged; 2025-26 adds `link_url`, not required).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import polars as pl

from fplai.decoders import decode
from fplai.providers.base import FetchResult, ProviderError
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    PLAYER_IDENTITY_SEASON,
    TEAM_IDENTITY_SEASON,
    CapabilityKey,
)
from fplai.store import content_hash
from fplai.transport import BulkFilePolicy, FileTransport

logger = logging.getLogger("fplai.providers.vaastav")

# Cloning once and pointing FileTransport at the local clone is the polite
# option (docs/wiki/data-sources.md §3.1: "do not hit raw.githubusercontent
# in a loop"); raw.githubusercontent.com remains the default because
# nothing in this codebase has cloned the repo yet, and FileTransport's own
# disk cache already means a given file is fetched at most once per path
# regardless of which base is used.
BASE_URL = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/"

_GAMEWEEK_STATS_PATH = "{season}/gws/gw{gameweek}.csv"
_PLAYER_IDENTITY_PATH = "{season}/players_raw.csv"
_TEAM_IDENTITY_PATH = "{season}/teams.csv"


class VaastavProvider:
    """Provider adapter over the vaastav/Fantasy-Premier-League community
    archive. Not an HTTP API in the request/response sense `providers/fpl.py`
    and `providers/pl.py` are — `policy` is `BulkFilePolicy` (blueprint
    §12.4), and `fetch()` reads a whole file per call rather than answering
    one entity per request. This is exactly the "download-once, read-many"
    shape `FileTransport` exists for: one `gws/gw{N}.csv` fetch answers
    every player's stats for that gameweek in a single `FetchResult`, the
    same "one call answers many capabilities/entities" pattern
    `providers/fpl.py`'s `fetch_batch` uses for `bootstrap-static/`.
    """

    provider_id = "vaastav_archive"
    policy = BulkFilePolicy(
        description=(
            "vaastav/Fantasy-Premier-League GitHub archive — no rate limit, "
            "bulk CSV files, cache aggressively (blueprint §12.4)."
        )
    )

    def __init__(self, transport: FileTransport, *, base_url: str = BASE_URL) -> None:
        self.transport = transport
        self.base_url = base_url

    def capabilities(self) -> list[CapabilityKey]:
        return [PLAYER_GAMEWEEK_STATS_GAMEWEEK, PLAYER_IDENTITY_SEASON, TEAM_IDENTITY_SEASON]

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
        if capability == PLAYER_GAMEWEEK_STATS_GAMEWEEK:
            return self._fetch_gameweek_stats(force_refresh=force_refresh, **params)
        if capability == PLAYER_IDENTITY_SEASON:
            return self._fetch_player_identity(force_refresh=force_refresh, **params)
        if capability == TEAM_IDENTITY_SEASON:
            return self._fetch_team_identity(force_refresh=force_refresh, **params)
        raise ProviderError(f"{self.provider_id} does not serve {capability}")

    def _fetch_gameweek_stats(self, *, force_refresh: bool, season: str, gameweek: int) -> FetchResult:
        path = _GAMEWEEK_STATS_PATH.format(season=season, gameweek=int(gameweek))
        url = self.base_url + path
        raw = self.transport.fetch(url, force_refresh=force_refresh)
        df = decode("csv", raw)
        if df.is_empty():
            raise ProviderError(f"{url} decoded to zero rows")

        # Drop exact duplicate rows (identical across all source columns).
        # This handles artefacts like Junior Kroupi (element 100) and Ben Gannon-Doak
        # (element 391) appearing twice in 2025-26/gws/gw1.csv with identical values.
        # Rows that differ in any value but share an entity key must still reach the
        # invariant and fail it — only deduplicate exact matches.
        before_dedup = len(df)
        df = df.unique(maintain_order=True)
        after_dedup = len(df)
        if before_dedup != after_dedup:
            dropped_count = before_dedup - after_dedup
            logger.warning(
                f"Dropped {dropped_count} exact duplicate rows from {path} "
                f"(season={season}, gameweek={gameweek})"
            )

        # 'season' is not a column in the raw file (it's implied by the
        # directory it lives in) — inject it so the capability's rows are
        # self-describing without a caller having to remember which fetch
        # call produced them (§12.2's spirit — provenance travels WITH the
        # data, not alongside it in a variable a caller might drop).
        df = df.with_columns(pl.lit(str(season)).alias("season"))

        CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK].validate(df)
        return FetchResult(
            rows=df,
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            provider_id=self.provider_id,
            endpoint=path,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
            meta={"season": str(season), "gameweek": int(gameweek), "n_players": df.height},
        )

    def _fetch_player_identity(self, *, force_refresh: bool, season: str) -> FetchResult:
        path = _PLAYER_IDENTITY_PATH.format(season=season)
        url = self.base_url + path
        raw = self.transport.fetch(url, force_refresh=force_refresh)
        df = decode("csv", raw)
        if df.is_empty():
            raise ProviderError(f"{url} decoded to zero rows")

        df = df.with_columns(pl.lit(str(season)).alias("season"))

        CANONICAL_SCHEMAS[PLAYER_IDENTITY_SEASON].validate(df)
        has_opta = "opta_code" in df.columns and df["opta_code"].null_count() < df.height
        return FetchResult(
            rows=df,
            capability=PLAYER_IDENTITY_SEASON,
            provider_id=self.provider_id,
            endpoint=path,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
            meta={"season": str(season), "n_players": df.height, "has_opta_code": has_opta},
        )

    def _fetch_team_identity(self, *, force_refresh: bool, season: str) -> FetchResult:
        path = _TEAM_IDENTITY_PATH.format(season=season)
        url = self.base_url + path
        raw = self.transport.fetch(url, force_refresh=force_refresh)
        df = decode("csv", raw)
        if df.is_empty():
            raise ProviderError(f"{url} decoded to zero rows")

        df = df.with_columns(pl.lit(str(season)).alias("season"))

        CANONICAL_SCHEMAS[TEAM_IDENTITY_SEASON].validate(df)
        return FetchResult(
            rows=df,
            capability=TEAM_IDENTITY_SEASON,
            provider_id=self.provider_id,
            endpoint=path,
            observed_at=datetime.now(timezone.utc),
            content_hash=content_hash(df),
            meta={"season": str(season), "n_teams": df.height},
        )


def register(
    registry,
    transport: FileTransport,
    *,
    seasons: frozenset[str] | None = None,
    priority: int = 20,
) -> VaastavProvider:
    """The 'registry entry' half of the E2b gate — mirrors `providers/
    fpl.py::register` and `providers/pl.py::register`. `seasons=None`
    means unrestricted (the archive spans 2016-17 -> present and adds a
    new season every year without this adapter changing); pass an explicit
    `frozenset` to pin a caller to seasons known-good for a given use.

    `priority=20` — lower priority (worse) than both `FPLProvider` and
    `PLProvider` (priority=10): the archive is the FALLBACK/historical
    source, never preferred over a live provider for a capability both
    could theoretically serve. No overlap exists today (neither other
    provider serves `player.gameweek_stats@gameweek`,
    `player.identity@season`, or `team.identity@season`), so this is a
    stated intent for when it does, not something currently exercised —
    same honesty as `providers/pl.py::register`'s docstring about its own
    priority=10.
    """
    provider = VaastavProvider(transport)
    from fplai.registry import CoverageSpec  # local import: see docstring above

    coverage = CoverageSpec(seasons=seasons, competitions=frozenset({"PL"}), priority=priority)
    for capability in provider.capabilities():
        registry.register(capability, provider, coverage)
    return provider
