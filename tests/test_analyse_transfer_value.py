"""Fast, pure tests for scripts/analyse_transfer_value.py (E7 S10b)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "analyse_transfer_value.py"
_spec = importlib.util.spec_from_file_location("analyse_transfer_value", _SCRIPT_PATH)
analyse_transfer_value = importlib.util.module_from_spec(_spec)
sys.modules["analyse_transfer_value"] = analyse_transfer_value
_spec.loader.exec_module(analyse_transfer_value)


def _record(*, gw: int, arm: str = "h6", hit_points: int = 0, premium: float | None = None, realised: int = 50):
    alternative = None
    if premium is not None:
        alternative = {
            "horizon_net_delta_chosen_minus_no_hit": premium,
            "horizon_gross_delta_chosen_minus_no_hit": premium - hit_points,
            "current_gross_delta_chosen_minus_no_hit": 2.0,
            "future_gross_delta_chosen_minus_no_hit": (premium - hit_points) - 2.0,
            "hit_points_delta_chosen_minus_no_hit": hit_points,
        }
    return {
        "season": "2023-24", "arm": arm, "gameweek": gw,
        "current_hit_points": hit_points,
        "current_hits": abs(hit_points) // 4,
        "transfers_out": [100 + gw] if hit_points else [],
        "current_free_transfers_available": 1,
        "realised_points": realised,
        "no_current_hit_alternative": alternative,
    }


def test_analyse_reports_paid_hit_premium_distribution_and_early_concentration():
    records = [
        _record(gw=1),
        _record(gw=2, hit_points=-8, premium=0.5, realised=42),
        _record(gw=3, hit_points=-4, premium=1.5, realised=55),
        _record(gw=10, hit_points=-4, premium=3.0, realised=61),
        _record(gw=2, arm="h1", hit_points=-4, premium=2.0, realised=48),
    ]
    analysis = analyse_transfer_value.analyse(records)
    h6 = analysis[("2023-24", "h6")]

    assert h6["paid_hit_gameweeks"] == [2, 3, 10]
    assert h6["paid_hit_points"] == -16
    assert h6["early_gw2_3_hit_points"] == -12
    assert h6["early_gw2_3_fraction_of_paid_hit_points"] == 0.75
    assert h6["horizon_net_premium_stats"]["count"] == 3
    assert h6["horizon_net_premium_stats"]["mean"] == 5.0 / 3.0
    assert h6["horizon_net_premium_stats"]["median"] == 1.5


def test_render_report_labels_counterfactual_as_ex_ante_plan_not_realised_result():
    analysis = analyse_transfer_value.analyse([
        _record(gw=2, hit_points=-4, premium=0.75, realised=44),
    ])
    report = analyse_transfer_value.render_report(analysis)
    report.encode("ascii")
    assert "EX-ANTE" in report
    assert "not a realised counterfactual" in report
    assert "gw2" in report


def test_empty_analysis_renders_without_raising():
    assert analyse_transfer_value.render_report({}) == "(empty transfer-value log -- nothing to report)"
