"""olbauday/FPL-Core-Insights archive adapter — E2b story 10b (blueprint
§12, §3.4, §12.1, §12.5). Second archive/`FileTransport` provider, mirroring
`providers/vaastav.py`'s pattern exactly: `transport.FileTransport` +
`decoders.decode("csv")` + a thin `Provider` adapter + `register()`. Zero
changes to `registry.py`, `transport.py`, or `decoders.py`.

**Repo layout — verified live, 2026-08-21, NOT assumed from
`docs/wiki/data-sources.md` §3.2** (that page is a survey; it sampled one
season and got two structural facts wrong — see below). Branch is `main`,
NOT `master` (vaastav's branch). Every data file lives under a `data/`
prefix that §3.2's listing omitted:

```
data/{season}/playerstats.csv           <- one row per player per gameweek,
                                            THAT gameweek's own bootstrap-
                                            static 'elements' snapshot
                                            (2025-2026, 2026-2027 only —
                                            see the 2024-2025 note below)
data/{season}/playerstats/playerstats.csv  <- SAME capability, 2024-2025's
                                            OWN (nested) path
data/{season}/gameweek_summaries.csv    <- one row per gameweek, the
                                            archived 'events[]' object
                                            (2025-2026, 2026-2027 only —
                                            does not exist for 2024-2025)
```

`{season}` is olbauday's OWN "YYYY-YYYY" string (e.g. `"2025-2026"`) — NOT
vaastav's "YYYY-YY" ("2025-26"). Never hardcoded; every call site takes
`season` as an explicit parameter (CLAUDE.md rule 4).

**Two capabilities only, by design.** Shot-level data under
`By Gameweek/GW{N}/` (`shots.csv`, `xg_by_minute.csv`, `lineups.csv`, ...)
is explicitly deferred to a later story — not built, not schema'd here.

**2024-2025's directory layout is a different generation of this repo's
scraper, not a minor path difference.** Its season directory has NO flat
`playerstats.csv`/`gameweek_summaries.csv` at all — instead:
`matches/`, `playermatchstats/`, `players/`, `playerstats/`, `teams/`
subdirectories. `playerstats.csv` DOES exist there, just nested one level
deeper (`playerstats/playerstats.csv`); this adapter tries the flat path
first and falls back to the nested one on a 404 (`_fetch_playerstats_df`
below) rather than hardcoding a season->layout lookup table — self-
adapting to a repo that has already restructured once. `gameweek_summaries
.csv` has NO equivalent anywhere in 2024-2025's tree; that season has NO
IN-ARCHIVE DEADLINE SOURCE at all (see the `observed_at` section below —
the reason 2024-2025 is unfetchable is now about deadlines, not
snapshots), even though `playerstats.csv` itself is fetchable there.

**Schema drift is real, verified, and severe for 2024-2025's
`playerstats.csv`** — not a column or two. 2025-2026/2026-2027 both have 87
columns (identical headers, both checked live); 2024-2025 has 58. The 29
columns missing from 2024-2025 include `web_name`/`first_name`/
`second_name` (NO player name in this file for that season, at all),
`news`/`news_added`, `minutes`/`goals_scored`/`assists`/`clean_sheets`, and
the entire defensive-contribution family (`defensive_contribution`,
`tackles`, `clearances_blocks_interceptions`, `recoveries` — consistent
with DC not existing as an FPL scoring category before 2025/26, blueprint
§11, not a scraping bug). `schemas.py`'s `required_fields` for
`player.attributes@gameweek` is deliberately the intersection verified
present in ALL THREE seasons — see that module's comment for the full
column list and the vaastav-opta_code-style reasoning for what's NOT
required.

**`observed_at` — corrected 2026-08-21, after live evidence showed the
original ruling this adapter first shipped with was wrong.** The first
version of this module joined `gameweek_summaries.csv`'s `snapshot_time`
column straight in as `observed_at`. Two live checks overturned that:

1. **Polarity check (Architect-directed, 2026-08-21):** compared
   `playerstats.csv`'s cumulative `total_points` at `gw=N` against
   vaastav's per-gameweek `total_points` for the SAME season/player, summed
   through N-1 vs. through N (`data/store/vaastav_player_gameweek_stats`,
   already in the store — no live call needed for this check). 8 players x
   4 gameweeks (2025-26), 32 comparisons: `playerstats.csv`'s `total_points`
   at gw=N matches vaastav's cumulative sum THROUGH gw=N in every case
   (e.g. Haaland id=430, gw=10: olbauday=98, vaastav cum-through-gw9=85,
   cum-through-gw10=98 — an exact match to "through N", never "through
   N-1"). **`playerstats.csv` is a POST-gameweek snapshot, not a
   pre-deadline one.**

2. **`snapshot_time` itself is not an observation timestamp at all — it is
   a stale file-GENERATION stamp.** The 2025-2026 `gameweek_summaries.csv`
   stamps every one of its 38 rows `2025-08-17T04:46:20Z` even though
   `finished=True` and non-zero `average_entry_score` are present for GW38
   (deadline 2026-05-24) — the file was clearly regenerated well after the
   season ended and simply kept the original stamp; the 2026-2027 file
   confirms it from the other direction (stamped 2026-07-23, all 38
   `finished=False`, no scores yet). Treating this as `observed_at` was not
   merely imprecise, it was **leakage, and the dangerous kind**:
   `BitemporalStore.as_of()` filters `WHERE observed_at <= ts` partitioned
   by entity key; with entity key `(season, gw, id)` every gameweek is its
   own entity, and with every gameweek stamped at the SAME constant
   instant, `as_of(GW10's deadline)` would have returned the GW38 row —
   May-2026 price/ownership/injury state surfaced in an October-2025 query
   — with nothing downstream able to detect it, because the leakage
   primitive was behaving exactly as designed against a bad input.

**The corrected rule: `observed_at` is IMPUTED from `gameweek_summaries
.csv`'s own `deadline_time` column (verified correct per-row, unlike
`snapshot_time`), never from `snapshot_time`.** Both capabilities here are
POST-gameweek content (point 1 above, and `gameweek.field_summary@
gameweek`'s `average_entry_score`/`chip_plays`/`most_captained` are
definitionally only knowable once a gameweek has been played), so for a
row describing gameweek N, `observed_at` is the LATEST instant by which
gameweek N's information is certainly public without being claimed any
earlier than verified: **gameweek N+1's own `deadline_time`** (`_impute_
observed_at` below). This is deliberately conservative — the true
publication instant is probably somewhat earlier (shortly after GW N's
own matches finish) — but blueprint §12.5 requires "no earlier than
verified", not "as early as plausible". For the LAST gameweek in a
season's file (no N+1 row to read a deadline from), falls back to gw N's
OWN `deadline_time` plus a documented, declared offset
(`_FINAL_GAMEWEEK_OBSERVED_AT_OFFSET`, 7 days) — a convention, not a
measured fact, same category as `backfill.py`'s own `_season_start_date`.
`rung 1` (a genuine PER-ROW capture timestamp on `playerstats.csv` itself)
is unaffected by this correction and is checked first, exactly as before —
none of the three seasons checked has one, so every fetch today reaches
the imputation rung. **If neither a per-row timestamp NOR
`gameweek_summaries.csv` exists for a season (2024-2025), this raises
`ProviderError` — 2024-2025 is unfetchable through `player.attributes@
gameweek` because it has NO IN-ARCHIVE DEADLINE SOURCE, not because it
lacks a snapshot; other in-repo sources (vaastav `kickoff_time`, FPL
`events`, PL API fixtures) could supply one but wiring that in is a
deferred decision, not built here.**

**The imputation is labelled ON THE ROWS, not just in `meta`** —
`observed_at_source` and `observed_at_imputed` are REQUIRED columns on
both schemas (CLAUDE.md rule 3: modelled/imputed data is labelled as such
at the schema level, not left to a caller who might drop `meta`). The raw,
misleading `snapshot_time` value is kept in `FetchResult.meta` under the
key `file_generation_stamp` — for forensics only, explicitly NOT named or
used as an observation time anywhere in this module. `lag_hours` (this
module's first version) has been REMOVED — it measured distance to a
stale stamp, which measures nothing real.

**Licence undeclared** (`docs/wiki/data-sources.md` §3.2) — internal use
only, same discipline as vaastav.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

from fplai.decoders import decode
from fplai.providers.base import FetchResult, ProviderError
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    GAMEWEEK_FIELD_SUMMARY_GAMEWEEK,
    PLAYER_ATTRIBUTES_GAMEWEEK,
    CapabilityKey,
)
from fplai.store import content_hash
from fplai.transport import BulkFilePolicy, FileTransport, TransportError

logger = logging.getLogger("fplai.providers.olbauday")

# Verified live 2026-08-21: branch is 'main', not 'master' (vaastav's
# branch) — never assume the two archive providers share a convention.
BASE_URL = "https://raw.githubusercontent.com/olbauday/FPL-Core-Insights/main/"

# 2025-2026 and 2026-2027's own layout (verified live). 2024-2025 uses a
# different, nested path for playerstats.csv (see module docstring) — that
# fallback is tried on a 404, not looked up from a season string here, so a
# season this adapter has never seen still gets a sensible first attempt.
_PLAYERSTATS_PATH_FLAT = "data/{season}/playerstats.csv"
_PLAYERSTATS_PATH_NESTED = "data/{season}/playerstats/playerstats.csv"
_GAMEWEEK_SUMMARIES_PATH = "data/{season}/gameweek_summaries.csv"

# Candidate column names that would satisfy resolution chain rung 1 (a
# genuine PER-ROW capture timestamp on playerstats.csv itself) if any
# olbauday season ever adds one. None of the three seasons checked
# (2024-2025, 2025-2026, 2026-2027) has any of these — verified live, not
# assumed — so every fetch today falls through to the imputation rung.
# `news_added` is deliberately NOT a candidate: it is a per-PLAYER
# injury-news timestamp (null for most rows), not a per-ROW capture
# instant — using it would silently misrepresent what it means for every
# player without live news. `snapshot_time` is deliberately NOT a
# candidate either, for the opposite reason: it IS present, but verified
# (module docstring) to be a stale file-generation stamp, not a capture
# time — including it here would silently resurrect the bug this module
# was corrected to remove.
_PER_ROW_TIMESTAMP_CANDIDATES = ("observed_at", "captured_at", "scraped_at")

# Declared convention, not a measured fact (same category as backfill.py's
# _season_start_date) — used only when a season's LAST gameweek has no
# N+1 deadline to impute from. Documented in the schema descriptions too,
# per the Architect's ruling, so it's never an implicit magic number.
_FINAL_GAMEWEEK_OBSERVED_AT_OFFSET = timedelta(days=7)


def _parse_iso(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text or text.lower() == "none":
        raise ProviderError(f"cannot parse an empty/None timestamp value: {value!r}")
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _generation_stamp_near(gws_df: pl.DataFrame, gameweek: int) -> str | None:
    """Best-effort raw `snapshot_time` for `FetchResult.meta`'s forensic
    `file_generation_stamp` field — NOT used for `observed_at`. Reads
    whichever of gw or gw+1's row is present (module docstring: verified
    constant across a whole season's file anyway, so which row it comes
    from doesn't change the value in practice)."""
    row = gws_df.filter(pl.col("id").cast(pl.Utf8).is_in([str(int(gameweek)), str(int(gameweek) + 1)]))
    if row.is_empty():
        return None
    value = row.to_dicts()[0].get("snapshot_time")
    return str(value) if value is not None else None


def _impute_observed_at(gws_df: pl.DataFrame, gws_path: str, gameweek: int) -> tuple[datetime, str, bool]:
    """The corrected resolution rung (module docstring): `observed_at` for
    gameweek N is gameweek N+1's own `deadline_time` — the latest instant
    by which N's post-gameweek content is certainly public, without
    claiming an earlier, unverified one. Falls back to gw N's OWN
    `deadline_time` + `_FINAL_GAMEWEEK_OBSERVED_AT_OFFSET` when there is no
    N+1 row (the season's last gameweek). Raises `ProviderError` if even gw
    N's own row is missing — nothing to impute from at all.

    Returns `(observed_at, observed_at_source, observed_at_imputed=True)` —
    every path through this function is an imputation, never a genuine
    per-row observation (that's rung 1, checked before this is ever
    called).
    """
    next_gw = int(gameweek) + 1
    next_row = gws_df.filter(pl.col("id").cast(pl.Utf8) == str(next_gw))
    if not next_row.is_empty():
        deadline = _parse_iso(next_row.to_dicts()[0]["deadline_time"])
        return deadline, f"{gws_path}:deadline_time(gw={next_gw})", True

    this_row = gws_df.filter(pl.col("id").cast(pl.Utf8) == str(int(gameweek)))
    if this_row.is_empty():
        raise ProviderError(
            f"cannot impute observed_at for gw={gameweek}: {gws_path} has no row for "
            f"gw={gameweek} or gw={next_gw} to derive a deadline from."
        )
    own_deadline = _parse_iso(this_row.to_dicts()[0]["deadline_time"])
    imputed_at = own_deadline + _FINAL_GAMEWEEK_OBSERVED_AT_OFFSET
    source = f"{gws_path}:deadline_time(gw={gameweek})+{_FINAL_GAMEWEEK_OBSERVED_AT_OFFSET}"
    return imputed_at, source, True


class OlbaudayProvider:
    """Provider adapter over the olbauday/FPL-Core-Insights community
    archive. Same shape as `providers/vaastav.py::VaastavProvider` —
    `BulkFilePolicy`, composes `FileTransport` (bytes) + `decoders.decode`
    (shape) — but each source file here covers a WHOLE SEASON (every
    gameweek in one CSV), not one file per gameweek: `fetch()` still takes
    a `gameweek` per call (matching this capability's declared GRAIN,
    blueprint §12.1) but filters the season-wide, `FileTransport`-cached
    frame down to that gameweek rather than requesting a per-gameweek URL —
    the "download once, read many" pattern still holds, one level up.
    """

    provider_id = "olbauday_archive"
    policy = BulkFilePolicy(
        description=(
            "olbauday/FPL-Core-Insights GitHub archive — no rate limit, "
            "bulk CSV files, cache aggressively (blueprint §12.4)."
        )
    )

    def __init__(self, transport: FileTransport, *, base_url: str = BASE_URL) -> None:
        self.transport = transport
        self.base_url = base_url

    def capabilities(self) -> list[CapabilityKey]:
        return [PLAYER_ATTRIBUTES_GAMEWEEK, GAMEWEEK_FIELD_SUMMARY_GAMEWEEK]

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
        if capability == PLAYER_ATTRIBUTES_GAMEWEEK:
            return self._fetch_player_attributes(force_refresh=force_refresh, **params)
        if capability == GAMEWEEK_FIELD_SUMMARY_GAMEWEEK:
            return self._fetch_gameweek_field_summary(force_refresh=force_refresh, **params)
        raise ProviderError(f"{self.provider_id} does not serve {capability}")

    # -- shared file fetch/decode helpers ---------------------------------

    def _fetch_playerstats_df(self, *, season: str, force_refresh: bool) -> tuple[pl.DataFrame, str]:
        """Whole-season `playerstats.csv`, decoded. Tries the flat path
        (2025-2026/2026-2027's layout) first; on a `TransportError` (a real
        404, not a network fluke — `FileTransport` has already exhausted
        its own retries) falls back to the nested path 2024-2025 uses
        (module docstring). Returns `(df, endpoint_used)` so the caller's
        `FetchResult.endpoint` reflects which path actually served the
        request, not just the one guessed first."""
        flat = _PLAYERSTATS_PATH_FLAT.format(season=season)
        try:
            raw = self.transport.fetch(self.base_url + flat, force_refresh=force_refresh)
            path = flat
        except TransportError:
            nested = _PLAYERSTATS_PATH_NESTED.format(season=season)
            raw = self.transport.fetch(self.base_url + nested, force_refresh=force_refresh)
            path = nested
        df = decode("csv", raw)
        if df.is_empty():
            raise ProviderError(f"{self.base_url}{path} decoded to zero rows")
        return df, path

    def _fetch_gameweek_summaries_df(self, *, season: str, force_refresh: bool) -> tuple[pl.DataFrame, str]:
        path = _GAMEWEEK_SUMMARIES_PATH.format(season=season)
        raw = self.transport.fetch(self.base_url + path, force_refresh=force_refresh)
        df = decode("csv", raw)
        if df.is_empty():
            raise ProviderError(f"{self.base_url}{path} decoded to zero rows")
        return df, path

    def _resolve_player_attributes_observed_at(
        self, *, season: str, gameweek: int, playerstats_rows: pl.DataFrame, force_refresh: bool
    ) -> tuple[datetime, str, bool, str | None]:
        """Blueprint §12.5's resolution chain, in order — see module
        docstring for the live evidence behind the imputation rung. Never
        falls back to `now()`; the last rung is always a raise. Returns
        `(observed_at, observed_at_source, observed_at_imputed,
        file_generation_stamp)`.
        """
        # Rung 1: a genuine per-row capture timestamp on playerstats.csv
        # itself, if this season/export ever has one (none checked here
        # do — see _PER_ROW_TIMESTAMP_CANDIDATES's comment).
        present = [c for c in _PER_ROW_TIMESTAMP_CANDIDATES if c in playerstats_rows.columns]
        if present:
            column = present[0]
            values = playerstats_rows[column].drop_nulls()
            if values.len() > 0:
                observed_at = _parse_iso(values[0])
                return observed_at, f"playerstats.csv:{column}", False, None

        # Rung 2 (corrected): impute from gameweek_summaries.csv's own
        # deadline_time — never its snapshot_time (module docstring).
        try:
            gws_df, gws_path = self._fetch_gameweek_summaries_df(season=season, force_refresh=force_refresh)
        except TransportError as exc:
            raise ProviderError(
                f"{self.provider_id}: cannot resolve observed_at for player.attributes@gameweek "
                f"season={season!r} gw={gameweek} — no per-row timestamp on playerstats.csv, and "
                f"{_GAMEWEEK_SUMMARIES_PATH.format(season=season)} (the only in-archive deadline "
                f"source) is unavailable for this season: {exc}. This season has NO IN-ARCHIVE "
                "DEADLINE SOURCE, not merely no snapshot — refusing to fall back to now() "
                "(blueprint §12.5)."
            ) from exc

        observed_at, source, imputed = _impute_observed_at(gws_df, gws_path, gameweek)
        generation_stamp = _generation_stamp_near(gws_df, gameweek)
        return observed_at, source, imputed, generation_stamp

    # -- capability fetches -------------------------------------------------

    def _fetch_player_attributes(self, *, force_refresh: bool, season: str, gameweek: int) -> FetchResult:
        df, path = self._fetch_playerstats_df(season=season, force_refresh=force_refresh)
        rows = df.filter(pl.col("gw").cast(pl.Utf8) == str(int(gameweek)))
        if rows.is_empty():
            raise ProviderError(f"{self.base_url}{path}: no rows for gw={gameweek} (season={season!r})")

        observed_at, source, imputed, generation_stamp = self._resolve_player_attributes_observed_at(
            season=season, gameweek=int(gameweek), playerstats_rows=rows, force_refresh=force_refresh
        )

        # 'season' is not a column in the raw file (implied by the
        # directory it lives in) — inject it, same discipline as
        # providers/vaastav.py, so rows are self-describing. The two
        # observed_at_* columns are the CLAUDE.md rule 3 label: an imputed
        # timestamp must be visible on the row itself, not only in
        # FetchResult.meta (which the store does not persist).
        rows = rows.with_columns(
            pl.lit(str(season)).alias("season"),
            pl.lit(source).alias("observed_at_source"),
            pl.lit(imputed).alias("observed_at_imputed"),
        )

        CANONICAL_SCHEMAS[PLAYER_ATTRIBUTES_GAMEWEEK].validate(rows)
        return FetchResult(
            rows=rows,
            capability=PLAYER_ATTRIBUTES_GAMEWEEK,
            provider_id=self.provider_id,
            endpoint=path,
            observed_at=observed_at,
            content_hash=content_hash(rows),
            meta={
                "season": str(season),
                "gameweek": int(gameweek),
                "n_players": rows.height,
                # Forensics only — verified to be a stale file-generation
                # stamp, NEVER used for observed_at. See module docstring.
                "file_generation_stamp": generation_stamp,
            },
        )

    def _fetch_gameweek_field_summary(self, *, force_refresh: bool, season: str, gameweek: int) -> FetchResult:
        df, path = self._fetch_gameweek_summaries_df(season=season, force_refresh=force_refresh)
        rows = df.filter(pl.col("id").cast(pl.Utf8) == str(int(gameweek)))
        if rows.is_empty():
            raise ProviderError(f"{self.base_url}{path}: no row for gw={gameweek} (season={season!r})")

        # gameweek.field_summary@gameweek's own content (average_entry_score,
        # chip_plays, most_captained, ...) is POST-gameweek results — there
        # is no "rung 1" here (unlike player.attributes@gameweek, this
        # capability IS gameweek_summaries.csv; its own snapshot_time is
        # exactly the stale stamp module docstring rejects, not a candidate
        # observation time). Always imputed from gw+1's deadline_time.
        observed_at, source, imputed = _impute_observed_at(df, path, int(gameweek))
        generation_stamp = rows.to_dicts()[0].get("snapshot_time")
        generation_stamp = str(generation_stamp) if generation_stamp is not None else None

        rows = rows.with_columns(
            pl.lit(str(season)).alias("season"),
            pl.lit(source).alias("observed_at_source"),
            pl.lit(imputed).alias("observed_at_imputed"),
        )

        CANONICAL_SCHEMAS[GAMEWEEK_FIELD_SUMMARY_GAMEWEEK].validate(rows)
        return FetchResult(
            rows=rows,
            capability=GAMEWEEK_FIELD_SUMMARY_GAMEWEEK,
            provider_id=self.provider_id,
            endpoint=path,
            observed_at=observed_at,
            content_hash=content_hash(rows),
            meta={
                "season": str(season),
                "gameweek": int(gameweek),
                "file_generation_stamp": generation_stamp,
            },
        )


def register(
    registry,
    transport: FileTransport,
    *,
    seasons: frozenset[str] | None = None,
    priority: int = 20,
) -> OlbaudayProvider:
    """The 'registry entry' half of the E2b gate — mirrors `providers/
    vaastav.py::register`. `seasons=None` means unrestricted; pass an
    explicit `frozenset` (e.g. `frozenset({"2025-2026", "2026-2027"})` to
    exclude 2024-2025, which has no in-archive deadline source at all (see
    module docstring) and so is unfetchable for both capabilities here) to
    pin a caller to seasons known-good for a given use.

    `priority=20` — same value as `VaastavProvider`'s, and the same
    rationale: no live provider serves `player.attributes@gameweek` or
    `gameweek.field_summary@gameweek` today, and no OTHER provider serves
    them either (blueprint §12.1 — these are deliberately distinct keys
    from vaastav's `player.gameweek_stats@gameweek`, see schemas.py), so
    this is a stated intent, not something currently exercised.
    """
    provider = OlbaudayProvider(transport)
    from fplai.registry import CoverageSpec  # local import: see providers/vaastav.py's docstring for why

    coverage = CoverageSpec(seasons=seasons, competitions=frozenset({"PL"}), priority=priority)
    for capability in provider.capabilities():
        registry.register(capability, provider, coverage)
    return provider
