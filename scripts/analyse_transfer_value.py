#!/usr/bin/env python
"""Pure E7 S10b report over `run_e7_gate.py --value-log` JSONL.

This is evidence, not tuning. For each paid-hit gameweek the runner records
the chosen H=6/H=1 plan and the best plan under the SAME inputs/objective with
CURRENT-round hits capped at zero. The difference is an EX-ANTE planned-value
counterfactual. It is not a claim about what would actually have happened on
the pitch under the alternative squad.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def read_value_log(path: Path) -> list[dict]:
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


def _stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "mean": None, "median": None, "q1": None, "q3": None, "min": None, "max": None}
    if len(values) == 1:
        value = float(values[0])
        return {"count": 1, "mean": value, "median": value, "q1": value, "q3": value, "min": value, "max": value}
    q1, _q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "q1": q1,
        "q3": q3,
        "min": min(values),
        "max": max(values),
    }


def analyse(records: list[dict]) -> dict[tuple[str, str], dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        groups.setdefault((record["season"], record["arm"]), []).append(record)

    out: dict[tuple[str, str], dict] = {}
    for key, group in groups.items():
        ordered = sorted(group, key=lambda r: r["gameweek"])
        paid = [r for r in ordered if r.get("no_current_hit_alternative") is not None]
        paid_hit_points = sum(int(r["current_hit_points"]) for r in paid)
        early_hit_points = sum(int(r["current_hit_points"]) for r in paid if r["gameweek"] in (2, 3))
        denominator = abs(paid_hit_points)
        alternatives = [r["no_current_hit_alternative"] for r in paid]
        out[key] = {
            "n_gameweeks": len(ordered),
            "paid_hit_gameweeks": [r["gameweek"] for r in paid],
            "paid_hit_points": paid_hit_points,
            "early_gw2_3_hit_points": early_hit_points,
            "early_gw2_3_fraction_of_paid_hit_points": (abs(early_hit_points) / denominator) if denominator else None,
            "horizon_net_premium_stats": _stats([float(a["horizon_net_delta_chosen_minus_no_hit"]) for a in alternatives]),
            "horizon_gross_premium_stats": _stats([float(a["horizon_gross_delta_chosen_minus_no_hit"]) for a in alternatives]),
            "current_gross_premium_stats": _stats([float(a["current_gross_delta_chosen_minus_no_hit"]) for a in alternatives]),
            "future_gross_premium_stats": _stats([float(a["future_gross_delta_chosen_minus_no_hit"]) for a in alternatives]),
            "realised_points_on_paid_hit_gameweeks": sum(int(r["realised_points"]) for r in paid),
            "paid_hit_rows": paid,
        }
    return out


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _fmt_stats(stats: dict) -> str:
    return (
        f"n={stats['count']} mean={_fmt(stats['mean'])} median={_fmt(stats['median'])} "
        f"q1={_fmt(stats['q1'])} q3={_fmt(stats['q3'])} min={_fmt(stats['min'])} max={_fmt(stats['max'])}"
    )


def render_report(analysis: dict[tuple[str, str], dict]) -> str:
    if not analysis:
        return "(empty transfer-value log -- nothing to report)"
    lines: list[str] = []
    for season in sorted({season for season, _arm in analysis}):
        lines.append(f"=== {season} ===")
        for arm in sorted(arm for s, arm in analysis if s == season):
            row = analysis[(season, arm)]
            lines.append(f"  -- arm {arm} --")
            paid_gws = ",".join(f"gw{gw}" for gw in row["paid_hit_gameweeks"]) or "none"
            lines.append(
                f"  paid-hit gameweeks: {paid_gws}; hit_points={row['paid_hit_points']}; "
                f"gw2-3_hit_points={row['early_gw2_3_hit_points']} "
                f"fraction={_fmt(row['early_gw2_3_fraction_of_paid_hit_points'])}"
            )
            lines.append(f"  EX-ANTE horizon NET premium vs best no-current-hit plan: {_fmt_stats(row['horizon_net_premium_stats'])}")
            lines.append(f"  EX-ANTE horizon GROSS premium: {_fmt_stats(row['horizon_gross_premium_stats'])}")
            lines.append(f"  EX-ANTE current-round gross premium: {_fmt_stats(row['current_gross_premium_stats'])}")
            lines.append(f"  EX-ANTE future-plan gross premium: {_fmt_stats(row['future_gross_premium_stats'])}")
            lines.append(
                f"  realised points on those paid-hit GWs (descriptive only): "
                f"{row['realised_points_on_paid_hit_gameweeks']}"
            )
            for rec in row["paid_hit_rows"]:
                alt = rec["no_current_hit_alternative"]
                lines.append(
                    f"    gw{rec['gameweek']}: hits={rec['current_hits']} hit_points={rec['current_hit_points']} "
                    f"xfers={len(rec['transfers_out'])} FT={rec['current_free_transfers_available']} "
                    f"gross_delta={alt['horizon_gross_delta_chosen_minus_no_hit']:.2f} "
                    f"net_premium={alt['horizon_net_delta_chosen_minus_no_hit']:.2f} "
                    f"current_gross_delta={alt['current_gross_delta_chosen_minus_no_hit']:.2f} "
                    f"future_gross_delta={alt['future_gross_delta_chosen_minus_no_hit']:.2f}"
                )
        lines.append("")
    lines.append(
        "NOTE: the no-current-hit comparison is an EX-ANTE solver PLAN under the same forecasts/state/objective; "
        "it is not a realised counterfactual. Realised points are shown only as descriptive context."
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--value-log", required=True, type=Path)
    args = parser.parse_args()
    print(render_report(analyse(read_value_log(args.value_log))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
