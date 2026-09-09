#!/usr/bin/env python
"""One-off manual verification of the Odds API provider (E2b story 8)
against LIVE data — not a scheduled entry point, not run in CI, not part
of the test suite (all network access there is mocked). Run by hand when
you need to re-confirm the adapter still works against the real API.

Deliberately gentle (CLAUDE.md — "do not probe the ceiling"): ONE
match.odds@fixture call (cost=2, covers ALL fixtures) and ONE
player.goal_odds@fixture call for a single event (cost=1) — 3 credits per
run, cached under cache/odds/ (gitignored) so a second run with the same
--event-id makes zero further live requests for that event (match odds is
always force_refresh=True — see the inline comment where it's called for
why a verify script deliberately does NOT rely on the cache there).

Prints, and never logs/prints THE_ODDS_API_KEY itself:
  - team identity match rate (expect 20/20 — see providers/odds.py)
  - player identity match rate for the one event fetched, INCLUDING every
    unresolved name if any (blueprint §12.5 — a finding, not smoothed over)
  - actual credits spent, read from the x-requests-remaining/-used response
    headers (not estimated) — see fplai.transport.CreditTracker.reconcile

Usage:
    python scripts/verify_odds_provider.py [--event-index N]

THE_ODDS_API_KEY must be set in the environment or in a `.env` file at the
repo root (KEY=VALUE, one per line) — this script reads that file itself
(a tiny inline parser, no new dependency) and NEVER prints or logs its
value, only whether it was found.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from fplai.client import FPLClient  # noqa: E402
from fplai.identity import IdentityError  # noqa: E402
from fplai.providers.base import ProviderError  # noqa: E402
from fplai.providers.fpl import FPLProvider  # noqa: E402
from fplai.providers.odds import MATCH_ODDS_FIXTURE, PLAYER_GOAL_ODDS_FIXTURE, OddsProvider, build_transport  # noqa: E402
from fplai.schemas import PLAYER_ATTRIBUTES_CURRENT, TEAM_ATTRIBUTES_CURRENT  # noqa: E402
from fplai.transport import TransportError  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# SECURITY: capped unconditionally, same fix as scripts/snapshot_odds.py —
# urllib3's own connection-pool DEBUG log line prints the full request URL,
# which for this provider includes "&apiKey=<key>" in the query string.
# This script hardcodes INFO above (no -v flag exists here), so it was
# never exposed to the exact leak found in snapshot_odds.py's --verbose
# run, but this line closes the same class of risk pre-emptively rather
# than relying on "this script happens not to have a verbose flag today."
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger("verify_odds_provider")

_ODDS_CACHE_DIR = _PROJECT_ROOT / "cache" / "odds"


def _load_dotenv_into_environ(env_path: Path) -> None:
    """Tiny inline .env loader — no python-dotenv dependency exists in this
    project yet, and one file's worth of KEY=VALUE parsing doesn't warrant
    adding one. Only sets a variable if it ISN'T already in the
    environment (a real env var always wins over the file). NEVER logs or
    prints any value it reads."""
    import os

    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--event-index",
        type=int,
        default=0,
        help="which fixture (by soonest kickoff) to fetch player.goal_odds@fixture for (default: 0, the soonest)",
    )
    args = parser.parse_args(argv)

    _load_dotenv_into_environ(_PROJECT_ROOT / ".env")

    import os

    if "THE_ODDS_API_KEY" not in os.environ or not os.environ["THE_ODDS_API_KEY"]:
        logger.error("THE_ODDS_API_KEY is not set (checked environment and .env) — cannot verify live")
        return 1
    logger.info("THE_ODDS_API_KEY: found (value never logged)")

    fpl_provider = FPLProvider(client=FPLClient())
    elements = fpl_provider.fetch(PLAYER_ATTRIBUTES_CURRENT).rows
    teams = fpl_provider.fetch(TEAM_ATTRIBUTES_CURRENT).rows
    logger.info("FPL snapshot: %d elements, %d teams", elements.height, teams.height)

    transport = build_transport(_ODDS_CACHE_DIR)  # key read from os.environ, never logged
    provider = OddsProvider(transport, season="2026-27", elements=elements, teams=teams)

    try:
        # force_refresh=True deliberately — a VERIFY run's whole point is to
        # confirm the adapter still works against a REAL, current response
        # shape; a cache hit would silently verify nothing but yesterday's
        # bytes. scripts/snapshot_odds.py (the capture script) makes the
        # same choice for the opposite reason (never miss real market
        # movement between two scheduled captures).
        match_odds = provider.fetch(MATCH_ODDS_FIXTURE, force_refresh=True)
    except (ProviderError, IdentityError, TransportError) as exc:
        logger.error("match.odds@fixture FAILED: %s", exc)
        return 1

    logger.info(
        "match.odds@fixture: %d rows across %d fixtures (all %d FPL teams resolved: 20/20 or this would have raised)",
        match_odds.rows.height,
        match_odds.rows["provider_event_id"].n_unique(),
        teams.height,
    )
    if transport._last_response_headers:
        logger.info(
            "credit headers after match.odds call: x-requests-remaining=%s x-requests-used=%s",
            transport._last_response_headers.get("x-requests-remaining"),
            transport._last_response_headers.get("x-requests-used"),
        )
    logger.info("CreditTracker.remaining_this_month (reconciled): %d", transport._credits.remaining_this_month)

    events = match_odds.rows.select("provider_event_id", "commence_time").unique().sort("commence_time")
    if args.event_index >= events.height:
        logger.error("--event-index %d out of range (%d fixtures available)", args.event_index, events.height)
        return 1
    event_id = events["provider_event_id"][args.event_index]
    logger.info("fetching player.goal_odds@fixture for event_id=%s (index %d)", event_id, args.event_index)

    try:
        goal_odds = provider.fetch(PLAYER_GOAL_ODDS_FIXTURE, event_id=event_id, force_refresh=True)
    except IdentityError as exc:
        # Blueprint §12.5's re-fetchability exception means this can ONLY
        # be a TEAM-identity failure now (effectively theoretical — 20/20
        # live-verified) — an unresolved PLAYER name no longer raises here
        # at all (see providers/odds.py's module docstring and the
        # PRESERVED-row report in the `else` branch below).
        logger.error("player.goal_odds@fixture: team-identity IdentityError (NOT the player re-fetchability case):\n%s", exc)
        return 1
    except (ProviderError, TransportError) as exc:
        logger.error("player.goal_odds@fixture FAILED (non-identity): %s", exc)
        return 1
    else:
        unresolved = goal_odds.meta.get("unresolved_player_names", [])
        n_unresolved = goal_odds.meta.get("n_unresolved_rows", 0)
        logger.info(
            "player.goal_odds@fixture: %d total rows (%d resolved, %d unresolved-but-preserved), "
            "%d distinct bookmakers",
            goal_odds.rows.height,
            goal_odds.rows.height - n_unresolved,
            n_unresolved,
            goal_odds.rows["bookmaker_key"].n_unique(),
        )
        if unresolved:
            # Blueprint §12.5's "loud in three places" — printed in full,
            # never smoothed over. See providers/odds.py's module
            # docstring for the live-verified 41/42 match rate and this
            # exact miss.
            logger.warning(
                "player.goal_odds@fixture: %d distinct player name(s) preserved UNRESOLVED "
                "(identity_resolved=false, raw name kept, re-fetchability exception): %s",
                len(unresolved),
                unresolved,
            )
        if transport._last_response_headers:
            logger.info(
                "credit headers after player.goal_odds call: x-requests-remaining=%s x-requests-used=%s",
                transport._last_response_headers.get("x-requests-remaining"),
                transport._last_response_headers.get("x-requests-used"),
            )

    logger.info("CreditTracker.remaining_this_month (final, reconciled): %d", transport._credits.remaining_this_month)
    logger.info("VERIFICATION RUN COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
