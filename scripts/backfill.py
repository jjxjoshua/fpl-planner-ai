#!/usr/bin/env python
"""CLI entry point for `fplai.backfill.BackfillOrchestrator` — E2b story 11.

**Default action is `--dry-run`.** Nothing is fetched unless `--execute` is
passed explicitly — the story's whole point is seeing the cost before
spending it (blueprint §3.6 / CLAUDE.md's FPL/PL API discipline: no
unattended multi-season backfill is ever the accidental default).

Two providers wired here, matching this story's PL-API + vaastav-archive
scope:

  --provider pl_api          match.fixtures@matchweek, match.lineups@match,
                              match.substitutions@match, team.match_stats@match
                              (seasons in the PL API's own starting-year
                              convention, e.g. "2025" = 2025/26)
  --provider vaastav_archive  player.gameweek_stats@gameweek,
                              player.identity@season, team.identity@season
                              (seasons in FPL's "YYYY-YY" convention, e.g.
                              "2025-26")

`pl_api`'s `provider_factory` builds a SEASON-SCOPED `PLProvider` per season
requested — BOTH identities now season-scoped (E2b story 7b): PLAYER
identity from `player.identity@season` (vaastav, story 10), TEAM identity
from `team.identity@season` (vaastav, story 7b), for any season other than
the current one. This is deliberately not a shortcut — it is the concrete
fix to blueprint §12.5 / provider-framework.md §9.3/§11.4 this story was
asked to prove.

**§11.4's finding, closed here.** Story 11 proved live that TEAM identity
was still built once, from the CURRENT season's snapshot only, and reused
for every season `factory()` was asked for — team codes persisting across
seasons made this "happen to work" for any historical match whose clubs
are all still in the league, and halt (correctly, but against the WRONG
snapshot) the moment one wasn't (PL numeric team id 21 = West Ham, season
2025/26). `factory()` below now fetches a season-MATCHED `pl_teams` (that
season's own matchweek-1 fixtures) and a season-MATCHED FPL-side team list
(`team.identity@season` for a historical season, the live snapshot for the
current one) every time it is asked for a season — one extra PL API
request per DISTINCT season (memoised by `BackfillOrchestrator`'s own
`_provider_cache`, never per `WorkUnit`), not per fetch.

Usage:
    # cost only, no network:
    python scripts/backfill.py --provider pl_api \
        --capabilities match.fixtures@matchweek,match.lineups@match \
        --seasons 2025

    # 10-season cost projection (still no network):
    python scripts/backfill.py --provider pl_api \
        --capabilities match.fixtures@matchweek,match.lineups@match,match.substitutions@match,team.match_stats@match,player.season_stats@season \
        --seasons 2015,2016,2017,2018,2019,2020,2021,2022,2023,2024

    # a real, gentle live run (vaastav, no rate limit, bulk files):
    python scripts/backfill.py --provider vaastav_archive \
        --capabilities player.gameweek_stats@gameweek --seasons 2025-26 \
        --gameweeks-per-season 3 --execute

    # simulate an interruption after 2 units, then resume:
    python scripts/backfill.py --provider vaastav_archive \
        --capabilities player.gameweek_stats@gameweek --seasons 2025-26 \
        --gameweeks-per-season 5 --execute --max-units 2
    python scripts/backfill.py --provider vaastav_archive \
        --capabilities player.gameweek_stats@gameweek --seasons 2025-26 \
        --gameweeks-per-season 5 --execute
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.backfill import (  # noqa: E402
    BackfillHalted,
    BackfillOrchestrator,
    BackfillSpec,
    CheckpointStore,
)
from fplai.client import FPLClient  # noqa: E402
from fplai.identity import (  # noqa: E402
    IdentityResolver,
    build_player_identity_map_for_season,
    build_team_identity_map_for_season,
)
from fplai.providers.fpl import FPLProvider  # noqa: E402
from fplai.providers.pl import PLProvider  # noqa: E402
from fplai.providers.vaastav import VaastavProvider  # noqa: E402
from fplai.schemas import (  # noqa: E402
    CANONICAL_SCHEMAS,
    PLAYER_ATTRIBUTES_CURRENT,
    PLAYER_IDENTITY_SEASON,
    TEAM_ATTRIBUTES_CURRENT,
    TEAM_IDENTITY_SEASON,
)
from fplai.store import BitemporalStore  # noqa: E402
from fplai.transport import BulkFilePolicy, FileTransport, HttpTransport, RatePolicy  # noqa: E402

logger = logging.getLogger("backfill_cli")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CAPABILITY_BY_NAME = {str(k): k for k in CANONICAL_SCHEMAS}


def _pl_season_to_archive_season(pl_season: str) -> str:
    """`"2025"` (PL API's starting-year convention) -> `"2025-26"` (FPL's /
    vaastav's own convention). A pure, deterministic format conversion
    between two providers' documented conventions — not a game rule
    (CLAUDE.md rule 4's targets), so it is fine as a small helper here
    rather than live config; still never assumed silently, only used when
    wiring pl_api's provider_factory to vaastav's identity capability."""
    year = int(pl_season)
    return f"{year}-{str(year + 1)[-2:]}"


def _parse_capabilities(raw: str) -> tuple:
    keys = []
    for name in raw.split(","):
        name = name.strip()
        if name not in _CAPABILITY_BY_NAME:
            raise SystemExit(f"unknown capability {name!r}. Known: {sorted(_CAPABILITY_BY_NAME)}")
        keys.append(_CAPABILITY_BY_NAME[name])
    return tuple(keys)


def _build_vaastav_factory(cache_dir: Path):
    transport = FileTransport(cache_dir=cache_dir, policy=BulkFilePolicy())
    provider = VaastavProvider(transport)
    return lambda season: provider  # stateless — every season reads its own file, no identity to build


class _LazyPlayerIdentityMap:
    """Defers `build_player_identity_map_for_season` until something
    actually resolves a player.

    **Why, found live 2026-08-28.** vaastav's archive carries NO
    `opta_code` column at all for 2019-20 through 2023-24 (verified in the
    real store: 666/713/737/778/865 rows, 100% null), so
    `PlayerIdentityMap.build()` raises for the whole season — correctly,
    since identity must never be guessed (CLAUDE.md lesson 4). But this
    factory built the player map EAGERLY for every season regardless of
    which capabilities were requested, so capabilities that need no player
    identity whatsoever were blocked by a map nothing had asked for:
    `match.fixtures@matchweek`, `team.match_stats@match`, and
    `match.officials@match` (whose payload carries official NAMES and no
    numeric player id at all). That is what limited the first real PL
    ingest to 2025-26.

    **Deliberately lazy rather than declared.** The alternative was a
    hand-maintained set of "capabilities that need player identity", which
    is a second place to be wrong: get it out of step with the adapter and
    a capability that DOES need identity would silently run without one.
    Laziness needs no such declaration and cannot drift — a capability
    that resolves a player triggers the build and, on an `opta_code`-less
    season, gets the same loud failure it would have got before; one that
    never resolves a player never builds it. The failure is deferred, not
    suppressed.

    TEAM identity is NOT made lazy: it is cheap, needed by essentially
    every PL capability including officials (which stamps team codes), and
    has no equivalent archive gap — it resolved 20/20 live.
    """

    def __init__(self, build):
        self._build = build
        self._map = None

    def _resolved(self):
        if self._map is None:
            self._map = self._build()
        return self._map

    def __getattr__(self, name):
        # only reached for attributes this proxy does not define itself
        return getattr(self._resolved(), name)


def _build_pl_factory(*, current_season_pl: str, cache_dir: Path, vaastav_cache_dir: Path):
    """See this module's docstring. `current_season_pl` is the PL-API-
    convention identifier of the LIVE season — used to fetch the current
    elements/teams snapshot (the live branch both identities fall back to)
    and as the boundary that decides whether PLAYER/TEAM identity comes
    from that live snapshot or the archive (blueprint §12.5).

    **E2b story 7b**: there is no longer a separate `identity_season_pl`
    knob. Before this story, team identity was verified against ONE fixed
    season's matchweek-1 fixtures regardless of which season was actually
    being fetched — the bug §11.4 found live. Now every identity (player
    AND team) is resolved for the SAME season as the data being fetched,
    so the only season concept left is "which one is current" — a second,
    independently-overridable "which season verifies team identity" no
    longer means anything real to decouple.
    """
    pl_transport = HttpTransport(
        base_url="https://sdp-prem-prod.premier-league-prod.pulselive.com/api/",
        policy=RatePolicy(requests_per_second=2.0, jitter_fraction=0.20),
        cache_dir=cache_dir,
    )
    fpl_provider = FPLProvider(client=FPLClient())
    current_elements = fpl_provider.fetch(PLAYER_ATTRIBUTES_CURRENT).rows
    current_teams = fpl_provider.fetch(TEAM_ATTRIBUTES_CURRENT).rows
    logger.info(
        "current FPL snapshot for the live branch of BOTH identities: %d elements, %d teams",
        current_elements.height, current_teams.height,
    )

    vaastav_transport = FileTransport(cache_dir=vaastav_cache_dir, policy=BulkFilePolicy())
    vaastav_provider = VaastavProvider(vaastav_transport)

    # Some (usually current-season, live) archive-season labels don't exist
    # yet on vaastav (e.g. the season is mid-flight) — current_season_pl is
    # handled by the live-elements/live-teams branch instead, so these
    # callables are only ever invoked for a genuinely historical season.
    def archive_elements_fetch(archive_season: str):
        return vaastav_provider.fetch(PLAYER_IDENTITY_SEASON, season=archive_season).rows

    def archive_teams_fetch(archive_season: str):
        return vaastav_provider.fetch(TEAM_IDENTITY_SEASON, season=archive_season).rows

    current_season_archive_label = _pl_season_to_archive_season(current_season_pl)

    def pl_teams_for_season(season_pl: str) -> list[dict]:
        """Raw PL API `{id, name, shortName}` team list from THIS season's
        own matchweek-1 fixtures — the PL-API side of the team join, which
        (unlike the player join) needs a season-matched counterpart on
        BOTH sides, not just the FPL one (see fplai.identity's module
        docstring, E2b story 7b addendum). Reaches into PLProvider's own
        `_fetch_pl_teams_for_identity()` rather than duplicating its
        endpoint-building logic here — `elements`/`teams` are unused
        placeholders for this call (only satisfying the constructor's
        precondition; `.identity()` is deliberately never called on this
        instance, so no PlayerIdentityMap/TeamIdentityMap is built from
        them)."""
        probe = PLProvider(
            pl_transport, season=season_pl, identity_season=season_pl,
            elements=current_elements, teams=current_teams,
        )
        return probe._fetch_pl_teams_for_identity()

    def factory(season: str) -> PLProvider:
        archive_season = _pl_season_to_archive_season(season)
        pl_teams = pl_teams_for_season(season)
        player_map = _LazyPlayerIdentityMap(
            lambda: build_player_identity_map_for_season(
                archive_season,
                current_season=current_season_archive_label,
                live_elements=current_elements,
                archive_elements_fetch=archive_elements_fetch,
            )
        )
        team_map = build_team_identity_map_for_season(
            archive_season,
            current_season=current_season_archive_label,
            pl_teams=pl_teams,
            live_teams=current_teams,
            archive_teams_fetch=archive_teams_fetch,
        )
        resolver = IdentityResolver(players=player_map, teams=team_map)
        return PLProvider(pl_transport, season=season, identity_season=season, identity=resolver)

    return factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", required=True, choices=["pl_api", "vaastav_archive"])
    parser.add_argument("--capabilities", required=True, help="comma-separated, e.g. match.fixtures@matchweek,match.lineups@match")
    parser.add_argument("--seasons", required=True, help="comma-separated, provider's own season convention")
    parser.add_argument("--matchweeks-per-season", type=int, default=38)
    parser.add_argument("--matches-per-matchweek", type=int, default=10)
    parser.add_argument("--gameweeks-per-season", type=int, default=None)
    parser.add_argument("--players-per-season-estimate", type=int, default=700)
    parser.add_argument("--execute", action="store_true", help="actually fetch and write — default is --dry-run only")
    parser.add_argument("--max-units", type=int, default=None, help="stop cleanly after N new units this call (time-boxing / resume demo)")
    parser.add_argument("--store-path", default=str(_PROJECT_ROOT / "data" / "store"))
    parser.add_argument("--checkpoint-path", default=None, help="default: data/backfill/<provider>.jsonl")
    parser.add_argument(
        "--current-season-pl", default="2026",
        help="PL-API-convention current season — the live/archive boundary for BOTH identities (E2b story 7b: team identity is now season-scoped too, so there is no separate --identity-season-pl any more)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    capabilities = _parse_capabilities(args.capabilities)
    seasons = tuple(s.strip() for s in args.seasons.split(","))
    spec = BackfillSpec(
        provider_id=args.provider, capabilities=capabilities, seasons=seasons,
        matchweeks_per_season=args.matchweeks_per_season, matches_per_matchweek=args.matches_per_matchweek,
        gameweeks_per_season=args.gameweeks_per_season, players_per_season_estimate=args.players_per_season_estimate,
    )

    checkpoint_path = Path(args.checkpoint_path) if args.checkpoint_path else _PROJECT_ROOT / "data" / "backfill" / f"{args.provider}.jsonl"
    checkpoint = CheckpointStore(checkpoint_path)
    store = BitemporalStore(base_path=Path(args.store_path))

    if not args.execute:
        # `--dry-run` MUST NOT fetch anything (story brief) — `PLProvider.
        # policy`/`VaastavProvider.policy` are read straight off the CLASS,
        # not an instance, so this branch never constructs a real provider
        # (which for pl_api would otherwise make a live identity-bootstrap
        # request before a single unit is even counted).
        policy = PLProvider.policy if args.provider == "pl_api" else VaastavProvider.policy
        from fplai.backfill import dry_run as _dry_run

        report = _dry_run(spec, policy)
        print(report.render())
        print()
        print(f"checkpoint would be: {checkpoint_path}")
        print("(dry run only — pass --execute to actually fetch. Nothing was requested.)")
        return 0

    # Only NOW — after the --dry-run branch above has already returned — do
    # we construct a provider_factory, which for pl_api makes one live
    # identity-bootstrap request (bootstrap-static + one matchweek-1
    # fixtures call). Deliberately deferred this late.
    if args.provider == "pl_api":
        cache_dir = _PROJECT_ROOT / "cache" / "pl_api"
        vaastav_cache_dir = _PROJECT_ROOT / "cache" / "vaastav"
        provider_factory = _build_pl_factory(
            current_season_pl=args.current_season_pl,
            cache_dir=cache_dir, vaastav_cache_dir=vaastav_cache_dir,
        )
    else:
        provider_factory = _build_vaastav_factory(_PROJECT_ROOT / "cache" / "vaastav")

    orch = BackfillOrchestrator(spec, provider_factory, checkpoint=checkpoint, store=store)

    logger.info("EXECUTING against checkpoint=%s store=%s", checkpoint_path, args.store_path)
    try:
        summary = orch.run(max_units=args.max_units)
    except BackfillHalted as exc:
        logger.error("RUN HALTED (%s): %s", exc.reason, exc)
        logger.error("Everything resolved before this point is checkpointed at %s — fix the cause and re-run to resume.", checkpoint_path)
        return 1

    logger.info(
        "RUN COMPLETE stopped_early=%s total=%d done=%d absent=%d already_resolved_on_entry=%d elapsed=%.1fs",
        summary.stopped_early, summary.n_total, summary.n_done, summary.n_absent,
        summary.n_already_resolved_on_entry, summary.elapsed_seconds,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
