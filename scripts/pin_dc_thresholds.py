#!/usr/bin/env python
"""Pin the real DC count thresholds BY OBSERVATION, from
`event/{gw}/live/`'s `explain` blocks, once a gameweek has settled —
`fplai.models.defensive_contribution`'s module docstring, "What is
verified, and what is not": the 10/12 CBIT/CBIRT thresholds are press-only
(`PRESS_DC_COUNT_THRESHOLDS`, `verified=False`) because the string
"threshold" appears zero times anywhere in `game_config` (checked
directly). This is the script that closes that loop — it either CONFIRMS
10/12 by direct observation of real points-vs-count pairs, or
CONTRADICTS them, and it never guesses in between.

## Why this needs the live API, not the store

No provider ingests `event/{gw}/live/` into the bitemporal store today
(checked: `fplai.providers.fpl`'s own module docstring states plainly that
`event_live`/`event_status` are "reachable on `FPLClient` directly ...
unwrapped here, because nothing in this slice persists them to the
store"). This script therefore imports `fplai.client.FPLClient` directly
— the SAME, unmodified client every other live-API script in this project
uses (`scripts/sample_picks.py`, `scripts/snapshot_bootstrap.py`,
`fplai.providers.fpl` itself) — never a bespoke request loop, so the
2 req/s rate limiting, caching and 429/403/503 backoff CLAUDE.md's "FPL
API discipline" requires are inherited, not reimplemented. `client.py`
itself is untouched by this task (this task's OWNED PATHS name it
FORBIDDEN to edit; it is used exactly as `fplai.providers.fpl` already
uses it — as a library, read-only, imported not modified).

## Settlement check — this is an expected state, not an error

A gameweek that has not finished scoring is the NORMAL case for most of
the week (blueprint §3.4's "one irreversible deadline" section already
established this exact discipline for `scripts/sample_picks.py`'s own
readiness probe). This script checks BOTH the store's own `events`
snapshot (`finished`/`data_checked`, refreshed every 30 min by the
Task Scheduler snapshotter — cheap, no live call) and, belt-and-braces,
a live `event_status()` call before trusting the store's snapshot is
current enough. Either signal saying "not ready" exits **cleanly, code
0**, with a clear message — never raises, never a non-zero exit for an
expected state.

## Inference logic — bounds, not a guess

For each position group (DEF_CBIT / MID_FWD_CBIRT), every element's
PER-FIXTURE `explain` block gives a (raw CBIT/CBIRT count, DC points
awarded) pair. Because DC is capped at 2 points regardless of how far past
threshold a count goes (blueprint §11 / the wiki), the true threshold T
satisfies: `T <= min(count | points == 2)` and `T > max(count | points ==
0)`. If those two bounds meet exactly (`min(count|points=2) - 1 ==
max(count|points=0)`), T is PINNED exactly. If there is a gap, both bounds
are reported honestly as inconclusive-but-informative — never resolved by
guessing the press value is right. If the bounds CONTRADICT each other
(the same count has points=2 in one row and points=0 in another) that is
a genuine anomaly (an `overrides` block, a scoring bug, or a parsing
mistake in this script) and is raised loudly, never silently resolved.

## Session s004 — the observation is now persisted, not just printed

Previously this script only PRINTED its pin attempt — the evidence existed
and the system never knew it, so `PRESS_DC_COUNT_THRESHOLDS.verified`
stayed `False` forever regardless of what this script found.
`fplai.models.defensive_contribution.write_dc_threshold_observations` now
persists every attempt (group, gameweek, season, bounds, pinned value or
null, n_observations, n_unattributable, contradiction) to the store's
`dc_threshold_observations` dataset — INCLUDING inconclusive/contradictory
attempts, recorded honestly rather than omitted. `build_dc_threshold_set`
resolves it back bitemporally, `as_of` a deadline
(`fplai.models.defensive_contribution.resolve_dc_threshold_observations`)
— see that module's docstring, "Session s004", for the observed-vs-derived
design decision and why this is not a `CANONICAL_SCHEMAS`-registered
capability. `--season` is a required, explicit CLI argument, never
inferred: FPL's own API carries no season-string field anywhere (checked
directly against every payload this script already touches) — the same
"caller supplies it, this module never guesses it" convention `fplai.
identity`'s `current_season` parameter already establishes elsewhere in
this codebase.

## What IS verified, as of 2026-08-27 (GW1, the first settled gameweek)

Run against the real settled GW1 `event/1/live/` payload. The `explain`
block field names (`identifier`, `value`, `points`) are CONFIRMED — 31
real `defensive_contribution` entries parsed, `value` agreeing exactly
with `stats["defensive_contribution"]` on every one. The "not settled ->
exit 0" branch was already live-verified in s003.

What that same run ALSO showed is that `explain` never carries a
`points=0` entry for any identifier, which broke the original upper-bound
inference outright — see `pin_thresholds`'s docstring for the correction
and for why double-gameweek elements are excluded from it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.client import FPLClient, FPLApiError  # noqa: E402
from fplai.models.defensive_contribution import (  # noqa: E402
    DC_GROUPS,
    GROUP_POSITIONS,
    PRESS_DC_COUNT_THRESHOLDS,
    write_dc_threshold_observations,
)
from fplai.store import BitemporalStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pin_dc_thresholds")

DC_IDENTIFIER = "defensive_contribution"


class PinDCThresholdsError(RuntimeError):
    """A genuine anomaly in the live payload's shape or content — never
    raised for 'gameweek not settled yet' (that is an expected state,
    handled by a clean exit, not this exception)."""


def _gw_settled_per_store(store: BitemporalStore, gw: int) -> tuple[bool, str]:
    events = store.latest("events")
    row = events.filter(events["id"] == gw)
    if row.is_empty():
        return False, f"no row for gameweek {gw} in the store's 'events' snapshot"
    finished = bool(row["finished"][0])
    data_checked = bool(row["data_checked"][0])
    if finished and data_checked:
        return True, "store's events snapshot: finished=True, data_checked=True"
    return False, f"store's events snapshot: finished={finished}, data_checked={data_checked}"


def _gw_settled_per_live_status(client: FPLClient, gw: int) -> tuple[bool, str]:
    """Belt-and-braces live check, same signal `scripts/sample_picks.py`
    already uses to decide when a gameweek's picks are safe (blueprint
    §3.4). `event-status/` reports bonus-confirmation state PER DAY, not
    per gameweek directly -- this checks that every reported day's
    `bonus_added` is True AND that the live `events` bootstrap payload
    (fetched fresh, not the store's) agrees the gameweek finished."""
    try:
        status = client.event_status(force_refresh=True)
    except FPLApiError as exc:
        return False, f"event-status/ call failed ({exc}) -- treating as not-yet-settled, not an error"
    statuses = status.get("status", [])
    if not statuses:
        return False, "event-status/ returned no status entries"
    all_bonus_added = all(bool(s.get("bonus_added")) for s in statuses)
    if not all_bonus_added:
        return False, f"event-status/: not every reported day has bonus_added=True ({statuses})"

    bootstrap = client.bootstrap_static(force_refresh=True)
    live_events = {e["id"]: e for e in bootstrap.get("events", [])}
    event = live_events.get(gw)
    if event is None:
        return False, f"gameweek {gw} not found in a fresh bootstrap-static() events list"
    if not (event.get("finished") and event.get("data_checked")):
        return False, f"fresh bootstrap-static(): finished={event.get('finished')}, data_checked={event.get('data_checked')}"
    return True, "live event-status/ + fresh bootstrap-static(): all settled signals True"


def _position_labels(client: FPLClient) -> dict[int, str]:
    """`element_type` id -> short position label, read LIVE from
    `bootstrap-static()['element_types']` -- never hardcoded (CLAUDE.md
    rule 4). No provider persists `element_types` to the store today (only
    elements/teams/events/chips/game_config/game_settings are captured,
    per `fplai.providers.fpl`'s own module docstring), so this is fetched
    directly, same client, same cache."""
    bootstrap = client.bootstrap_static()
    element_types = bootstrap.get("element_types", [])
    if not element_types:
        raise PinDCThresholdsError("bootstrap-static() returned no element_types -- cannot map element_type -> position")
    labels = {}
    for et in element_types:
        short = et["singular_name_short"]
        labels[et["id"]] = "GK" if short == "GKP" else short
    return labels


def _group_for_position(position: str) -> str | None:
    for group, positions in GROUP_POSITIONS.items():
        if position in positions:
            return group
    return None


def pin_thresholds(client: FPLClient, gw: int) -> dict[str, dict]:
    """Returns, per group: `{"count_threshold": int | None, "lower_bound":
    int | None, "upper_bound": int | None, "n_observations": int,
    "n_unattributable": int, "contradiction": bool}`. `count_threshold` is
    set only when the bounds meet exactly.

    ## The upper bound cannot come from `explain` -- corrected 2026-08-27

    Verified against the real settled GW1 payload: `explain` lists ONLY
    identifiers that actually SCORED. All 610 elements carry 31
    `defensive_contribution` entries between them and every one is
    `points=2`; there are ZERO `points=0` entries anywhere. A defender
    with 9 CBIT has no DC entry at all -- not a `points=0` one. The
    original `max(count | points == 0)` was therefore unobservable BY
    CONSTRUCTION, and the script would have reported INCONCLUSIVE every
    gameweek forever, however many settled gameweeks it was run against.

    The unscored count lives in `stats["defensive_contribution"]`, which
    is present for every element and is the RAW COUNT, not the awarded
    points -- verified two ways on the same real payload: it equals the
    `explain` `value` on every scoring row, and it takes values across
    0..21, which points (capped at 2) cannot.

    ## Why some elements are excluded rather than folded in

    `stats` is per-ELEMENT-per-GAMEWEEK; `explain` is per-FIXTURE. On a
    double gameweek an unscored element's total cannot be attributed to
    either fixture -- a 14 could be 7+7 -- and treating the total as one
    fixture's count would manufacture a false CONTRADICTION against a
    real threshold. Multi-fixture unscored elements are therefore excluded
    from the upper bound and reported as `n_unattributable`, never
    silently included. GW1 is a single gameweek so this excluded nothing,
    which is exactly why it must be written now rather than discovered on
    the first DGW.
    """
    labels = _position_labels(client)
    live = client.event_live(gw)
    elements = live.get("elements", [])
    if not elements:
        raise PinDCThresholdsError(
            f"event/{gw}/live/ returned an empty elements list -- this should not happen for a "
            "gameweek this script has already confirmed settled; treat as a genuine anomaly."
        )

    bootstrap = client.bootstrap_static()
    element_type_by_id = {e["id"]: e["element_type"] for e in bootstrap.get("elements", [])}

    zero_point_counts: dict[str, list[int]] = {g: [] for g in DC_GROUPS}
    two_point_counts: dict[str, list[int]] = {g: [] for g in DC_GROUPS}
    unattributable: dict[str, int] = {g: 0 for g in DC_GROUPS}

    for el in elements:
        element_id = el.get("id")
        element_type = element_type_by_id.get(element_id)
        position = labels.get(element_type)
        group = _group_for_position(position) if position else None
        if group is None:
            continue  # GK or unresolvable -- not DC-eligible, skip

        fixture_explains = el.get("explain", [])
        scored_this_gw = False

        for fixture_explain in fixture_explains:
            dc_stat = next(
                (s for s in fixture_explain.get("stats", []) if s.get("identifier") == DC_IDENTIFIER), None
            )
            if dc_stat is None:
                continue  # absent = did not score DC in THIS fixture; the count comes from `stats` below
            points = dc_stat.get("points")
            value = dc_stat.get("value")
            if points is None or value is None:
                raise PinDCThresholdsError(
                    f"element {element_id}, fixture explain block: defensive_contribution stat is "
                    f"missing 'points' or 'value' -- {dc_stat!r}. This module's field-name assumption "
                    "may be wrong; inspect the raw payload before trusting anything below."
                )
            if points == 2:
                scored_this_gw = True
                two_point_counts[group].append(int(value))
            elif points == 0:
                # Not observed on any real payload to date (see the docstring), but if FPL ever
                # starts emitting it, it is a direct per-fixture upper-bound observation.
                zero_point_counts[group].append(int(value))
            else:
                raise PinDCThresholdsError(
                    f"element {element_id}: defensive_contribution points={points}, expected 0 or 2 "
                    "(DC is capped at 2/match) -- a genuine anomaly, not handled by this script."
                )

        if scored_this_gw:
            continue  # its counts are already recorded per-fixture, exactly

        stats = el.get("stats") or {}
        if DC_IDENTIFIER not in stats:
            raise PinDCThresholdsError(
                f"element {element_id}: `stats` block carries no '{DC_IDENTIFIER}' key -- the payload "
                "shape this script depends on for the upper bound has changed. Inspect the raw "
                f"event/{gw}/live/ payload; do not trust any bound below."
            )
        if not stats.get("minutes"):
            continue  # never on the pitch -- a count of 0 is true but carries no information
        if len(fixture_explains) > 1:
            unattributable[group] += 1  # DGW: a gameweek total cannot be pinned to one fixture
            continue
        zero_point_counts[group].append(int(stats[DC_IDENTIFIER]))

    results: dict[str, dict] = {}
    for group in DC_GROUPS:
        two_pts, zero_pts = two_point_counts[group], zero_point_counts[group]
        lower_bound = min(two_pts) if two_pts else None
        upper_bound = max(zero_pts) if zero_pts else None
        contradiction = lower_bound is not None and upper_bound is not None and lower_bound <= upper_bound
        pinned = None
        if lower_bound is not None and upper_bound is not None and not contradiction and lower_bound - 1 == upper_bound:
            pinned = lower_bound
        results[group] = {
            "count_threshold": pinned,
            "lower_bound": lower_bound,
            "upper_bound": upper_bound,
            "n_observations": len(two_pts) + len(zero_pts),
            "n_unattributable": unattributable[group],
            "contradiction": contradiction,
        }
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gw", type=int, required=True, help="Gameweek to pin thresholds for.")
    parser.add_argument(
        "--season",
        required=True,
        help="Season string this gameweek belongs to (e.g. '2026-27') -- required, never inferred: "
        "FPL's own API carries no season field anywhere in this script's payloads (checked directly). "
        "Only consulted if a pin attempt is actually persisted (i.e. the gameweek is settled).",
    )
    parser.add_argument(
        "--skip-live-status-check",
        action="store_true",
        help="Trust the store's own events snapshot alone (skip the belt-and-braces live event-status/ "
        "+ fresh bootstrap-static() call) -- for testing only.",
    )
    parser.add_argument(
        "--skip-persist",
        action="store_true",
        help="Print the pin attempt but do not write it to the store -- for testing/inspection only.",
    )
    args = parser.parse_args()

    store = BitemporalStore()  # default path = data/store/ -- used for BOTH the settlement read
    # checks below AND, once a pin attempt is computed, persisting the observation itself.
    store_settled, store_reason = _gw_settled_per_store(store, args.gw)
    logger.info("store settlement check: settled=%s (%s)", store_settled, store_reason)
    if not store_settled:
        print(f"Gameweek {args.gw} has not settled per the store's own events snapshot: {store_reason}")
        print("This is an EXPECTED state, not an error. Nothing to pin yet.")
        return 0

    client = FPLClient()
    if not args.skip_live_status_check:
        live_settled, live_reason = _gw_settled_per_live_status(client, args.gw)
        logger.info("live settlement check: settled=%s (%s)", live_settled, live_reason)
        if not live_settled:
            print(f"Gameweek {args.gw} has not settled per a fresh live check: {live_reason}")
            print("This is an EXPECTED state, not an error. Nothing to pin yet (the store's own "
                  "snapshot may simply be stale -- trust the live check).")
            return 0

    results = pin_thresholds(client, args.gw)

    print(f"\n=== DC threshold pin attempt, gameweek {args.gw} ===")
    any_contradiction = False
    for group, r in results.items():
        press = PRESS_DC_COUNT_THRESHOLDS[group]
        print(f"\n{group}:")
        print(f"  press-sourced (unverified): {press.count_threshold}")
        print(f"  observed lower_bound (min count w/ points=2): {r['lower_bound']}")
        print(f"  observed upper_bound (max count, DC not awarded): {r['upper_bound']}")
        print(f"  n_observations: {r['n_observations']}")
        if r["n_unattributable"]:
            print(f"  excluded from the upper bound: {r['n_unattributable']} multi-fixture element(s) "
                  "-- a gameweek total cannot be attributed to a single fixture")
        if r["contradiction"]:
            any_contradiction = True
            print(f"  CONTRADICTION: a count appears with BOTH points=0 and points=2 across different "
                  f"observations -- this is a genuine anomaly (overrides block? scoring bug? parsing "
                  f"error in this script?), not resolved automatically.")
        elif r["count_threshold"] is not None:
            match = "MATCHES" if r["count_threshold"] == press.count_threshold else "CONTRADICTS"
            print(f"  PINNED: count_threshold={r['count_threshold']} -- {match} the press-sourced value.")
        else:
            print(f"  INCONCLUSIVE this gameweek: bounds do not meet exactly "
                  f"(gap between upper_bound+1 and lower_bound). More observations needed.")

    if any_contradiction:
        print("\nAt least one group produced a contradiction -- inspect the raw event_live payload "
              "before trusting any pinned value this run.")

    if args.skip_persist:
        print("\n--skip-persist set: not writing this attempt to the store.")
        return 0

    observed_at = datetime.now(timezone.utc)
    write_result = write_dc_threshold_observations(
        store, results, season=args.season, round_=args.gw, observed_at=observed_at,
    )
    print(
        f"\nPersisted to dataset {write_result.dataset!r}: written={write_result.written}, "
        f"n_rows={write_result.n_rows}"
        + ("" if write_result.written else " (skip_if_unchanged: identical to the most recent batch)")
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
