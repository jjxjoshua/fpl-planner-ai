#!/usr/bin/env python
"""One-off manual verification of the olbauday archive adapter (E2b story
10b, `observed_at` logic CORRECTED 2026-08-21) against LIVE data — not a
scheduled entry point, not run in CI, not part of the test suite (all
network access there is mocked). Run by hand to re-confirm the adapter
still works against the real archive.

**This script's own first version was wrong** — it measured a "snapshot_
time lag distribution" that turned out to be measuring an artefact
(`snapshot_time` is a stale file-generation stamp, not an observation
time; see `providers/olbauday.py`'s module docstring for the live evidence
and the corrected ruling). This version demonstrates the CORRECTED
behaviour instead: `observed_at` imputed from `gameweek_summaries.csv`'s
own `deadline_time`, differing per gameweek (not constant), the
`observed_at_source`/`observed_at_imputed` labels now present ON every
row, and the 2024-2025 raise now correctly attributed to "no in-archive
deadline source" rather than "no snapshot source".

Live requests made this run (all cached under cache/olbauday/, gitignored):
  1. data/2025-2026/playerstats.csv        (~9.6 MB)
  2. data/2025-2026/gameweek_summaries.csv  (~19 KB)
  3. data/2026-2027/playerstats.csv         (~0.6 MB, pre-season, 599 rows)
  4. data/2026-2027/gameweek_summaries.csv  (~19 KB)
  5. data/2024-2025/playerstats.csv         (404 — flat path doesn't exist for this season)
  6. data/2024-2025/playerstats/playerstats.csv (success — the nested fallback)
  7. data/2024-2025/gameweek_summaries.csv  (404 — genuinely absent for this season)
Seven live requests total. Requests 1-4 and 6 (the 200s) land in
`FileTransport`'s disk cache — a second run makes no NEW live request for
those five. Requests 5 and 7 (404s) do NOT get cached (`transport.FileCache`
only ever stores a successful body) so a second run still re-issues them.

Usage:
    python scripts/verify_olbauday_provider.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.providers.base import ProviderError  # noqa: E402
from fplai.providers.olbauday import OlbaudayProvider  # noqa: E402
from fplai.schemas import GAMEWEEK_FIELD_SUMMARY_GAMEWEEK, PLAYER_ATTRIBUTES_GAMEWEEK  # noqa: E402
from fplai.transport import BulkFilePolicy, FileTransport, TransportError  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("verify_olbauday_provider")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CACHE_DIR = _PROJECT_ROOT / "cache" / "olbauday"

# CLAUDE.md rule 4: seasons currently known to exist in this archive
# (verified live, 2026-08-21 — docs/wiki/data-sources.md §3.2). Not
# hardcoded anywhere the adapter itself runs — only this manual script's
# choice of which three to demonstrate.
_CURRENT_LAYOUT_SEASONS = ("2025-2026", "2026-2027")
_OLD_LAYOUT_SEASON = "2024-2025"


def main() -> int:
    transport = FileTransport(cache_dir=_CACHE_DIR, policy=BulkFilePolicy())
    provider = OlbaudayProvider(transport)

    logger.info("=" * 70)
    logger.info("PART 1 — observed_at is IMPUTED from deadline_time, and DIFFERS per gameweek")
    logger.info("=" * 70)

    for season in _CURRENT_LAYOUT_SEASONS:
        for gw in (1, 2):
            try:
                attr = provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season=season, gameweek=gw)
            except ProviderError as exc:
                logger.info("player.attributes@gameweek season=%s gw=%d -> ProviderError (expected if this season has < %d gameweeks): %s", season, gw, gw, str(exc)[:160])
                continue
            source = attr.rows["observed_at_source"][0]
            imputed = attr.rows["observed_at_imputed"][0]
            logger.info(
                "player.attributes@gameweek: season=%s gw=%d -> %d rows, endpoint=%s",
                season, gw, attr.rows.height, attr.endpoint,
            )
            logger.info(
                "  observed_at=%s  observed_at_source=%s  observed_at_imputed=%s  "
                "file_generation_stamp(forensics only, NOT used)=%s",
                attr.observed_at.isoformat(), source, imputed, attr.meta["file_generation_stamp"],
            )

        summary1 = provider.fetch(GAMEWEEK_FIELD_SUMMARY_GAMEWEEK, season=season, gameweek=1)
        logger.info(
            "gameweek.field_summary@gameweek: season=%s gw=1 -> observed_at=%s source=%s imputed=%s",
            season, summary1.observed_at.isoformat(), summary1.rows["observed_at_source"][0], summary1.rows["observed_at_imputed"][0],
        )
        logger.info(
            "  (gw1's own deadline_time=%s — observed_at is LATER than that, by design: gw1's "
            "results aren't public until gw2's deadline at the latest)",
            summary1.rows["deadline_time"][0],
        )

    logger.info("=" * 70)
    logger.info("PART 2 — 2024-2025: flat->nested path fallback + 'no in-archive deadline source'")
    logger.info("=" * 70)

    # 2024-2025 has NO gameweek_summaries.csv at all, so
    # player.attributes@gameweek for it MUST raise ProviderError (blueprint
    # §12.5's "else raise", never a fallback to now()). Demonstrate that
    # raise explicitly, THEN prove the nested-path fallback works in
    # isolation by fetching playerstats.csv alone.
    try:
        provider.fetch(PLAYER_ATTRIBUTES_GAMEWEEK, season=_OLD_LAYOUT_SEASON, gameweek=1)
        logger.error("UNEXPECTED: fetch succeeded for a season with no gameweek_summaries.csv")
        return 1
    except ProviderError as exc:
        logger.info("EXPECTED ProviderError (season=%s): %s", _OLD_LAYOUT_SEASON, str(exc)[:220])
        assert "NO IN-ARCHIVE DEADLINE SOURCE" in str(exc), "error message should say deadline, not snapshot"

    df, path = provider._fetch_playerstats_df(season=_OLD_LAYOUT_SEASON, force_refresh=False)
    logger.info(
        "playerstats.csv IS fetchable for %s despite the above — nested-path fallback succeeded: "
        "endpoint=%s, %d rows, %d columns (vs. 87 for 2025-2026/2026-2027 — schema drift, verified)",
        _OLD_LAYOUT_SEASON, path, df.height, len(df.columns),
    )
    logger.info("  'web_name' in columns: %s (expected False — verified live drift)", "web_name" in df.columns)

    try:
        provider._fetch_gameweek_summaries_df(season=_OLD_LAYOUT_SEASON, force_refresh=False)
        logger.error("UNEXPECTED: gameweek_summaries.csv fetch succeeded for 2024-2025")
        return 1
    except TransportError as exc:
        logger.info("EXPECTED TransportError (gameweek_summaries.csv genuinely absent for %s): %s", _OLD_LAYOUT_SEASON, str(exc)[:160])

    logger.info("VERIFICATION RUN COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
