#!/usr/bin/env python
"""Transfer-churn diagnostic for the E7 gate's `--decision-log` output.
Session `s008`, story `S10a`.

**Why this exists.** The E7 gate ran `09-06` and FAILED, 0 of 3 seasons
(`docs/wiki/e7-gate-results.jsonl`): H=6 wins on GROSS in every season and
loses on NET in every season, purely on paid hits. The leading hypothesis
is classic receding-horizon over-trading -- `optimise_multi_period` books
a transfer's benefit across up to six rounds, but `SeasonReplay` commits
round `t` only and re-solves, so the -4 is paid in full immediately while
the modelled benefit is only realised if the player is actually held.

**THIS SCRIPT IS EVIDENCE, NOT TUNING.** It measures the hypothesis; it
does not test it against a pass/fail bar, does not change any objective,
horizon, cost, or model, and is not itself a gate. Anything it surfaces
that looks like a fix is a finding for a SEPARATE decision, taken on its
own merits -- quietly tuning against these same three seasons is exactly
what this project's `09-03` ruling forbids.

## D4 -- pure report, no re-run, no store, no model fitting

This script NEVER calls `scripts/run_e7_gate.py` and never touches the
store. Given a `--decision-log` JSONL file (one line per (season, arm,
gameweek), written by `run_e7_gate.py --decision-log PATH`, S10a D1), it
reads it, computes four metrics per (season, arm), and prints a table.
Given the same log, it prints the same table -- `--report-only`-style
purity, `run_e7_gate.py`'s own D7 convention. ASCII output only (a cp1252
console has already corrupted a script's output in this project once).

## The four metrics (D2)

- **M1 -- realised hold length.** For every element bought at round `b`
  and later sold at round `s` (both drawn from `transfers_in`/
  `transfers_out`, matched FIFO per element -- at most one purchase of a
  given element can be open at a time, FPL squads never hold two copies
  of the same player), the hold length is `s - b`. Reported per arm:
  count, mean, median, and quartiles of COMPLETED holds only. An element
  still held at the last gameweek present in the log is CENSORED --
  reported as its own count, NEVER folded into the mean as a hold of
  `season_end - b`, and never silently dropped either. The true mean hold
  is therefore an under-estimate whenever any purchase is censored --
  the docstring says so and the printed table repeats it, because this is
  the one number a bug would most plausibly get wrong in the direction
  that flatters the over-trading hypothesis.
- **M2 -- fraction of purchases sold within fewer than 6 rounds** (the
  horizon whose benefit the objective booked), plus `<1`/`<2`/`<3` for
  shape. The DENOMINATOR is every purchase, including censored ones --
  a censored purchase has not been observed to sell quickly, so it
  belongs in "not (yet) sold within X", not excluded from the count
  entirely. This is the conservative direction: it can only UNDER-state
  churn relative to dropping censored purchases from the denominator,
  which is the direction that must not be flattered (mirrors M1's own
  censoring rule).
- **M3 -- per-gameweek transfers and hits**, per arm, so a season's shape
  is visible rather than only its total.
- **M4 -- round trips**: an element sold and later bought back in the
  same season, per arm -- count, and the distribution of the gap. Needs
  no counterfactual to interpret and is the sharpest single signature of
  over-trading this script computes.

**D5 -- no hit is attributed to a specific player.** When two transfers
happen in one gameweek off one free transfer, which transfer "was the
paid one" is not defined by anything in the log or the optimiser output.
M3 reports hits per gameweek alongside that gameweek's transfer count and
stops there -- an invented per-player attribution would be the most
quotable number this script could produce and the least true.

**D3 -- both arms, same season, always.** `h6` alone has no baseline; the
report always prints `h1` beside it for the identical season so the
comparison is legible without cross-referencing two runs.

## Free build vs. transfer (pinned decision, S010a follow-up)

The initial squad (round `t0`, empty `incoming_state`) is logged by
`HorizonStrategy.decide` as `transfers_in = result.squad_element_ids` --
i.e. all 15 initial-squad elements show up as "purchases" at the free
build, indistinguishable from a genuine mid-season transfer unless this
script says otherwise. **A 15-element opening squad is not fifteen
transfers.** A logged gameweek is a free build STRUCTURALLY (never by
gameweek number: a season's first round is not always `1` --
`SeasonData.rounds()` can start elsewhere) iff `transfers_out` is empty
AND `len(transfers_in) == len(squad_element_ids)`. Free-build elements are
excluded from M1's hold-length population, M2's fractions (numerator and
denominator), and M4's round-trip purchase set -- and the exclusion is
printed per arm, loudly, never silent. A sale that matches no open
purchase because the element was part of an excluded free build is its
own category, `initial_squad_sales`, reported per arm and NEVER
back-matched by the FIFO map to some later, unrelated purchase.

## Usage

    uv run python scripts/analyse_transfer_churn.py --decision-log PATH
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

# ---------------------------------------------------------------------------
# Reading -- mirrors `run_e7_gate._read_records`'s own tolerance: a
# malformed line is skipped, never fatal (an interrupted `write` mid-line
# must not block reading back what completed before it).
# ---------------------------------------------------------------------------


def read_decision_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def group_by_season_arm(records: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """`{(season, arm): [record, ...]}`, each group's records sorted by
    `gameweek` ascending -- every metric below assumes chronological
    order within a group and does not re-sort itself."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        key = (r["season"], r["arm"])
        groups.setdefault(key, []).append(r)
    for key in groups:
        groups[key] = sorted(groups[key], key=lambda r: r["gameweek"])
    return groups


# ---------------------------------------------------------------------------
# M1 -- realised hold length, FIFO-matched per element. Also the shared
# bookkeeping M2 reuses (a purchase's completed hold length, or censored).
# ---------------------------------------------------------------------------


def _is_free_build_record(rec: dict) -> bool:
    """D1 (pinned, S010a follow-up): a free build is defined STRUCTURALLY,
    never by gameweek number -- a season's first round is not always `1`
    (`SeasonData.rounds()` can start elsewhere), and hardcoding one is
    exactly the class of assumption this project has already been bitten
    by. A logged gameweek is a free build iff it sells nothing AND buys
    exactly the whole squad: `transfers_out` empty and `len(transfers_in)
    == len(squad_element_ids)`. A genuine mid-season decision can never
    sell nothing and buy a whole squad, so this test is exact. The
    `len(transfers_in) > 0` guard keeps a no-op gameweek (nothing bought
    or sold, `squad_element_ids` unset) from matching by both sides
    reading zero."""
    t_in, t_out, squad = rec["transfers_in"], rec["transfers_out"], rec["squad_element_ids"]
    return len(t_out) == 0 and len(t_in) > 0 and len(t_in) == len(squad)


def _identify_free_build(records_sorted: list[dict]) -> tuple[set[int], list[dict]]:
    """`(free_build_elements, exclusions)` for one (season, arm) group.
    `free_build_elements` is the union of every free-build record's
    `transfers_in` -- D2's "not a purchase" set. `exclusions` is
    `[{"gameweek": gw, "count": n}, ...]`, one entry per free-build
    gameweek found, for D3's loud per-arm reporting."""
    free_build_elements: set[int] = set()
    exclusions: list[dict] = []
    for rec in records_sorted:
        if _is_free_build_record(rec):
            free_build_elements.update(rec["transfers_in"])
            exclusions.append({"gameweek": rec["gameweek"], "count": len(rec["transfers_in"])})
    return free_build_elements, exclusions


def _match_holds(records_sorted: list[dict]) -> dict:
    """FIFO-matches purchases to sales. Per D2/D4 (pinned, S010a
    follow-up): a free-build record's `transfers_in` never opens a
    purchase (D2 -- the initial squad is not fifteen transfers), so a
    later sale of one of those elements can never be matched by the FIFO
    map below and is counted separately as `initial_squad_sales` (D4) --
    NEVER back-matched to some later, unrelated purchase of a different
    element. A sale that matches neither an open purchase nor a known
    free-build element (e.g. a log that does not start at the free build)
    is left uncounted, same as before this change -- nothing in this log
    identifies what it was.

    At most one purchase of a given element is ever open at a time (an
    FPL squad cannot hold two copies of the same player), so a plain
    `{element: buy_round}` map is a correct FIFO matcher, not merely a
    convenient one. Sells are applied before buys within a single
    gameweek's record so a same-gameweek sell correctly closes off an
    EARLIER round's purchase before that gameweek's own new purchases
    open fresh entries.

    Returns `{"completed": [...], "censored_count": int, "total_purchases":
    int, "initial_squad_sales": int, "free_build_exclusions": [...]}`."""
    free_build_elements, exclusions = _identify_free_build(records_sorted)
    open_purchase: dict[int, int] = {}
    completed: list[int] = []
    total_purchases = 0
    initial_squad_sales = 0
    for rec in records_sorted:
        gw = rec["gameweek"]
        is_free_build = _is_free_build_record(rec)
        for element in rec["transfers_out"]:
            if element in open_purchase:
                completed.append(gw - open_purchase.pop(element))
            elif element in free_build_elements:
                initial_squad_sales += 1
            # else: sold something never observed as bought and never part
            # of a detected free build -- not a hold-length event and not
            # an initial-squad sale either (nothing in this log accounts
            # for it).
        for element in rec["transfers_in"]:
            if is_free_build:
                continue  # D2: free-build elements are not purchases
            open_purchase[element] = gw
            total_purchases += 1
    censored_count = len(open_purchase)
    return {
        "completed": completed,
        "censored_count": censored_count,
        "total_purchases": total_purchases,
        "initial_squad_sales": initial_squad_sales,
        "free_build_exclusions": exclusions,
    }


def _distribution_stats(values: list[int]) -> dict:
    """count/mean/median/q1/q3 over `values`. `None` fields when there is
    nothing to summarise (0 values) or too few for a meaningful spread (1
    value reports itself for every stat rather than raising)."""
    if not values:
        return {"count": 0, "mean": None, "median": None, "q1": None, "q3": None}
    if len(values) == 1:
        v = float(values[0])
        return {"count": 1, "mean": v, "median": v, "q1": v, "q3": v}
    q1, _q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "q1": q1,
        "q3": q3,
    }


def compute_m1(records_sorted: list[dict]) -> dict:
    holds = _match_holds(records_sorted)
    stats = _distribution_stats(holds["completed"])
    stats["censored_count"] = holds["censored_count"]
    stats["initial_squad_sales"] = holds["initial_squad_sales"]
    stats["free_build_exclusions"] = holds["free_build_exclusions"]
    stats["note"] = (
        "mean/median cover COMPLETED holds only -- censored (still-held) purchases are excluded, "
        "never dropped from the count and never treated as a hold of season_end - buy_round, so the "
        "true mean hold is an under-estimate whenever censored_count > 0"
        if holds["censored_count"]
        else "no censored purchases in this group -- completed holds are the whole population"
    )
    return stats


# ---------------------------------------------------------------------------
# M2 -- churn fractions. Denominator is EVERY purchase, including
# censored ones (D2's own "the conservative direction" ruling above).
# ---------------------------------------------------------------------------

_M2_THRESHOLDS = (1, 2, 3, 6)


def compute_m2(records_sorted: list[dict]) -> dict:
    holds = _match_holds(records_sorted)
    completed, censored_count, total_purchases = holds["completed"], holds["censored_count"], holds["total_purchases"]
    fractions: dict[str, float | None] = {}
    for threshold in _M2_THRESHOLDS:
        n_within = sum(1 for h in completed if h < threshold)
        fractions[f"lt_{threshold}"] = (n_within / total_purchases) if total_purchases else None
    return {
        "total_purchases": total_purchases,
        "censored_count": censored_count,
        "initial_squad_sales": holds["initial_squad_sales"],
        "free_build_exclusions": holds["free_build_exclusions"],
        "fractions": fractions,
    }


# ---------------------------------------------------------------------------
# M3 -- per-gameweek shape. No aggregation beyond what is already per-row
# in the log; this is deliberately the least-processed metric.
# ---------------------------------------------------------------------------


def compute_m3(records_sorted: list[dict]) -> list[dict]:
    return [
        {"gameweek": r["gameweek"], "n_transfers": len(r["transfers_out"]), "hit_points": r["hit_points"]}
        for r in records_sorted
    ]


# ---------------------------------------------------------------------------
# M4 -- round trips: sold, then later bought back, same element, same
# (season, arm) group.
# ---------------------------------------------------------------------------


def compute_m4(records_sorted: list[dict]) -> dict:
    """Round trips. D2 (pinned, S010a follow-up): a free-build record's
    `transfers_in` is excluded from the round-trip "purchase" set, same as
    M1/M2, so the initial squad build itself is never the "bought back"
    half of a round trip. This is a no-op in practice -- the free build is
    always the season's first record and nothing can be sold before it is
    owned -- kept explicit because the pinned decision names M4, not
    because it changes any output. A GENUINE later re-purchase of a
    formerly-free-build element (its own, non-free-build record) still
    matches normally if it was sold in between -- D2 only excludes the
    free build's own acquisition event, not everything that ever happens
    to that element afterwards."""
    last_sell: dict[int, int] = {}
    gaps: list[int] = []
    for rec in records_sorted:
        gw = rec["gameweek"]
        is_free_build = _is_free_build_record(rec)
        for element in rec["transfers_out"]:
            last_sell[element] = gw
        for element in rec["transfers_in"]:
            if is_free_build:
                continue  # D2: free-build elements are not purchases
            if element in last_sell:
                gaps.append(gw - last_sell.pop(element))
    return {"count": len(gaps), "gap_stats": _distribution_stats(gaps)}


def analyse(records: list[dict]) -> dict[tuple[str, str], dict]:
    groups = group_by_season_arm(records)
    return {
        key: {
            "m1_hold_length": compute_m1(recs),
            "m2_churn_fractions": compute_m2(recs),
            "m3_per_gameweek": compute_m3(recs),
            "m4_round_trips": compute_m4(recs),
        }
        for key, recs in groups.items()
    }


# ---------------------------------------------------------------------------
# Rendering -- ASCII only, D3's "both arms, same season" grouping.
# ---------------------------------------------------------------------------


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def render_report(analysis: dict[tuple[str, str], dict]) -> str:
    seasons = sorted({season for season, _arm in analysis})
    lines: list[str] = []
    if not seasons:
        return "(empty decision log -- nothing to report)"
    for season in seasons:
        lines.append(f"=== {season} ===")
        arms_present = sorted(arm for s, arm in analysis if s == season)
        for arm in arms_present:
            m = analysis[(season, arm)]
            m1, m2, m4 = m["m1_hold_length"], m["m2_churn_fractions"], m["m4_round_trips"]
            lines.append(f"  -- arm {arm} --")
            fb_exclusions = m1["free_build_exclusions"]
            if fb_exclusions:
                fb_total = sum(e["count"] for e in fb_exclusions)
                fb_str = ", ".join(f"gw{e['gameweek']}:{e['count']}" for e in fb_exclusions)
                lines.append(
                    f"  FREE BUILD EXCLUDED (not counted as purchases): {fb_total} elements ({fb_str})"
                )
            else:
                lines.append("  FREE BUILD EXCLUDED: none detected in this group")
            lines.append(
                f"  M1 hold length (completed): count={m1['count']} mean={_fmt(m1['mean'])} "
                f"median={_fmt(m1['median'])} q1={_fmt(m1['q1'])} q3={_fmt(m1['q3'])} "
                f"censored={m1['censored_count']} initial_squad_sales={m1['initial_squad_sales']}"
            )
            lines.append(f"     note: {m1['note']}")
            frac_str = ", ".join(f"<{t}={_fmt(m2['fractions'][f'lt_{t}'])}" for t in _M2_THRESHOLDS)
            lines.append(
                f"  M2 churn fractions (denom=total purchases incl. censored, excl. free build): "
                f"total_purchases={m2['total_purchases']} censored={m2['censored_count']} "
                f"initial_squad_sales={m2['initial_squad_sales']} {frac_str}"
            )
            lines.append(
                f"  M4 round trips (sold, later bought back): count={m4['count']} "
                f"gap_mean={_fmt(m4['gap_stats']['mean'])} gap_median={_fmt(m4['gap_stats']['median'])}"
            )
            lines.append("  M3 per-gameweek (gameweek: n_transfers, hit_points):")
            per_gw = m["m3_per_gameweek"]
            row = "    " + " | ".join(f"gw{r['gameweek']}: {r['n_transfers']}xf,{r['hit_points']}hp" for r in per_gw)
            lines.append(row)
        lines.append("")
    lines.append(
        "NOTE: this is a DIAGNOSTIC, not a gate -- it measures the receding-horizon over-trading "
        "hypothesis, it does not decide anything and no objective/horizon/cost/model was changed to "
        "produce it."
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decision-log", type=str, required=True, help="Path to a --decision-log JSONL file written by scripts/run_e7_gate.py")
    args = parser.parse_args()
    records = read_decision_log(Path(args.decision_log))
    print(render_report(analyse(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
