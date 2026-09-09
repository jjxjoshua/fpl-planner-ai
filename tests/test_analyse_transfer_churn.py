"""Tests for `scripts/analyse_transfer_churn.py` -- the S10a churn
diagnostic (session `s008`). Fast unit tests only, against FABRICATED
decision-log records (plain dicts, the exact shape `run_e7_gate.py
--decision-log` writes) -- this script never touches the store or a real
gate run, per its own D4.

`scripts/` is not a package -- imported by path, `tests/test_run_e7_gate.
py`'s own established pattern.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRIPT_PATH = _REPO_ROOT / "scripts" / "analyse_transfer_churn.py"
_spec = importlib.util.spec_from_file_location("analyse_transfer_churn", _SCRIPT_PATH)
churn = importlib.util.module_from_spec(_spec)
sys.modules["analyse_transfer_churn"] = churn
_spec.loader.exec_module(churn)


def _rec(*, season="2023-24", arm="h6", gameweek, transfers_in=(), transfers_out=(), hit_points=0, points=50, squad_element_ids=()):
    return {
        "season": season,
        "arm": arm,
        "gameweek": gameweek,
        "transfers_in": list(transfers_in),
        "transfers_out": list(transfers_out),
        "hit_points": hit_points,
        "points": points,
        "squad_element_ids": list(squad_element_ids),
    }


# ---------------------------------------------------------------------------
# read_decision_log -- same tolerance as run_e7_gate._read_records.
# ---------------------------------------------------------------------------


def test_read_decision_log_empty_when_file_absent(tmp_path: Path):
    assert churn.read_decision_log(tmp_path / "missing.jsonl") == []


def test_read_decision_log_skips_malformed_lines(tmp_path: Path):
    out = tmp_path / "log.jsonl"
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_rec(gameweek=1)) + "\n")
        f.write("{not json\n")
        f.write("\n")
        f.write(json.dumps(_rec(gameweek=2)) + "\n")
    records = churn.read_decision_log(out)
    assert [r["gameweek"] for r in records] == [1, 2]


def test_group_by_season_arm_sorts_by_gameweek(tmp_path):
    records = [_rec(gameweek=3), _rec(gameweek=1), _rec(gameweek=2)]
    groups = churn.group_by_season_arm(records)
    gws = [r["gameweek"] for r in groups[("2023-24", "h6")]]
    assert gws == [1, 2, 3]


def test_group_by_season_arm_separates_arms_and_seasons():
    records = [
        _rec(season="2023-24", arm="h6", gameweek=1),
        _rec(season="2023-24", arm="h1", gameweek=1),
        _rec(season="2024-25", arm="h6", gameweek=1),
    ]
    groups = churn.group_by_season_arm(records)
    assert set(groups.keys()) == {("2023-24", "h6"), ("2023-24", "h1"), ("2024-25", "h6")}


# ---------------------------------------------------------------------------
# M1 -- realised hold length, ATTACKED on the censoring rule specifically:
# a purchase never sold must be counted (censored_count) and must NOT be
# silently folded into the completed-hold mean as season_end - buy_round,
# and must NOT be dropped from the record entirely.
# ---------------------------------------------------------------------------


def test_m1_completed_hold_length_is_sell_round_minus_buy_round():
    records = [
        _rec(gameweek=1, transfers_in=[10]),
        _rec(gameweek=4, transfers_out=[10]),
    ]
    m1 = churn.compute_m1(records)
    assert m1["count"] == 1
    assert m1["mean"] == 3.0
    assert m1["censored_count"] == 0


def test_m1_attack_a_never_sold_purchase_is_censored_not_dropped_not_treated_as_a_long_hold():
    """The attack: element 99 is bought at gw1 and never appears in any
    later transfers_out. If the censoring rule were wrong in the
    flattering direction, this purchase would either vanish from the
    count entirely (under-counting churn) or get counted as a hold of
    `last_gw - 1` (over-stating how long H6 "successfully" holds
    players -- exactly the direction the hypothesis must not be
    flattered in, per the brief). Neither must happen."""
    records = [
        _rec(gameweek=1, transfers_in=[99]),
        _rec(gameweek=2, transfers_in=[5]),
        _rec(gameweek=3, transfers_out=[5]),
        _rec(gameweek=10),  # last gameweek present -- element 99 still open
    ]
    m1 = churn.compute_m1(records)
    # Element 5's hold (gw2 -> gw3, length 1) is the ONLY completed hold.
    assert m1["count"] == 1
    assert m1["mean"] == 1.0
    # Element 99 is censored -- counted on its own, not folded into mean
    # (which would be (1 + 9) / 2 = 5.0 if wrongly treated as a length-9 hold,
    # or 1.0 with count staying 1 if silently dropped and hidden -- the
    # censored_count field is what distinguishes "correct" from "silently
    # dropped": both would print mean=1.0, only one also reports censored=1).
    assert m1["censored_count"] == 1
    assert "under-estimate" in m1["note"]


def test_m1_no_censoring_note_differs_when_nothing_is_censored():
    records = [_rec(gameweek=1, transfers_in=[1]), _rec(gameweek=2, transfers_out=[1])]
    m1 = churn.compute_m1(records)
    assert m1["censored_count"] == 0
    assert "no censored" in m1["note"]


def test_m1_selling_an_element_never_seen_as_bought_is_not_a_hold_event():
    """Part of the initial free-build squad, sold mid-season -- it was
    never a `transfers_in` in this log, so it must not manufacture a
    hold-length event or a spurious censored purchase."""
    records = [_rec(gameweek=1, transfers_out=[7])]
    m1 = churn.compute_m1(records)
    assert m1["count"] == 0
    assert m1["censored_count"] == 0


def test_m1_stats_include_quartiles_with_enough_data():
    records = [
        _rec(gameweek=0, transfers_in=[1, 2, 3, 4]),
        _rec(gameweek=1, transfers_out=[1]),  # hold 1
        _rec(gameweek=3, transfers_out=[2]),  # hold 3
        _rec(gameweek=5, transfers_out=[3]),  # hold 5
        _rec(gameweek=7, transfers_out=[4]),  # hold 7
    ]
    m1 = churn.compute_m1(records)
    assert m1["count"] == 4
    assert m1["median"] == 4.0


def test_m1_single_completed_hold_reports_itself_for_every_stat():
    records = [_rec(gameweek=1, transfers_in=[1]), _rec(gameweek=2, transfers_out=[1])]
    m1 = churn.compute_m1(records)
    assert m1["mean"] == m1["median"] == m1["q1"] == m1["q3"] == 1.0


def test_m1_no_purchases_at_all_reports_none_stats_not_a_crash():
    m1 = churn.compute_m1([_rec(gameweek=1)])
    assert m1["count"] == 0
    assert m1["mean"] is None


# ---------------------------------------------------------------------------
# Free build vs. transfer -- pinned decision, S010a follow-up (D1-D4).
# ---------------------------------------------------------------------------


def test_free_build_gameweek_excluded_from_m1_and_m2():
    """D1/D2: the free build sells nothing and buys the whole squad -- it
    must not appear as `total_purchases`, hold-length population, or
    censored_count. A later genuine sale of an initial-squad element is
    its own category, not a completed hold."""
    squad = list(range(1, 16))  # 15-element opening squad
    records = [
        _rec(gameweek=1, transfers_in=squad, squad_element_ids=squad),
        _rec(gameweek=5, transfers_in=[20], transfers_out=[squad[0]]),  # genuine transfer
    ]
    m1 = churn.compute_m1(records)
    m2 = churn.compute_m2(records)
    assert m1["count"] == 0
    assert m1["censored_count"] == 1  # element 20, still held, not free build
    assert m1["initial_squad_sales"] == 1  # squad[0], sold, never a purchase
    assert m2["total_purchases"] == 1  # only element 20
    assert m2["censored_count"] == 1
    assert m2["initial_squad_sales"] == 1


def test_free_build_detected_structurally_regardless_of_gameweek_number():
    """D1's own reason for existing: a season's first round is not always
    `1` (`SeasonData.rounds()` can start elsewhere). The same structural
    free build at gameweek 7 must be detected and excluded exactly as one
    at gameweek 1 would be."""
    squad = list(range(101, 116))
    records = [_rec(gameweek=7, transfers_in=squad, squad_element_ids=squad)]
    m1 = churn.compute_m1(records)
    assert m1["count"] == 0
    assert m1["censored_count"] == 0  # nothing counted as a purchase at all
    assert m1["free_build_exclusions"] == [{"gameweek": 7, "count": 15}]


def test_mid_season_one_for_one_swap_with_15_element_squad_still_counted():
    """A mid-season decision that sells 1 and buys 1, in a season where the
    squad also happens to have 15 elements, must never be mistaken for a
    free build -- D1's structural test requires `transfers_out` to be
    EMPTY, which a real swap never satisfies."""
    squad = list(range(1, 16))
    records = [
        _rec(gameweek=1, transfers_in=squad, squad_element_ids=squad),
        _rec(gameweek=3, transfers_in=[50], transfers_out=[1]),
        _rec(gameweek=6, transfers_out=[50]),
    ]
    m1 = churn.compute_m1(records)
    assert m1["count"] == 1  # element 50: bought gw3, sold gw6, hold=3
    assert m1["mean"] == 3.0
    assert m1["initial_squad_sales"] == 1  # element 1, part of the free build


def test_d4_attack_free_build_sale_never_back_matched_to_a_different_purchase():
    """ATTACK (D4): sell a free-build element and buy a DIFFERENT element in
    the SAME gameweek. FIFO matching must not pair the sale with that
    purchase, nor with any later purchase of any element -- the sale must
    land in initial_squad_sales and nowhere else."""
    squad = list(range(1, 16))
    records = [
        _rec(gameweek=1, transfers_in=squad, squad_element_ids=squad),
        _rec(gameweek=4, transfers_out=[3], transfers_in=[999]),  # sell free-build elt 3, buy 999
        _rec(gameweek=9, transfers_out=[999]),  # element 999 sold later
    ]
    m1 = churn.compute_m1(records)
    # element 3's sale must NOT be paired with element 999's purchase, nor
    # with anything else -- only element 999's own completed hold counts.
    assert m1["count"] == 1
    assert m1["mean"] == 5.0  # gw9 - gw4
    assert m1["initial_squad_sales"] == 1
    assert m1["censored_count"] == 0


def test_free_build_excluded_from_m4_round_trip_purchase_set():
    """D2: the free build's own transfers_in must never register as the
    "bought back" half of a round trip. A GENUINE later re-purchase of a
    formerly-free-build element, after it has actually been sold, is a
    real round trip and still counts."""
    squad = list(range(1, 16))
    records = [
        _rec(gameweek=1, transfers_in=squad, squad_element_ids=squad),
        _rec(gameweek=5, transfers_out=squad[:1]),  # sell one free-build element
        _rec(gameweek=8, transfers_in=squad[:1]),  # genuine buy-back, same element
    ]
    m4 = churn.compute_m4(records)
    assert m4["count"] == 1
    assert m4["gap_stats"]["mean"] == 3.0  # gw8 - gw5


def test_render_report_shows_free_build_exclusion_count():
    squad = list(range(1, 16))
    records = [_rec(gameweek=1, transfers_in=squad, squad_element_ids=squad)]
    report = churn.render_report(churn.analyse(records))
    assert "FREE BUILD EXCLUDED" in report
    assert "15" in report


def test_render_report_states_no_free_build_when_none_detected():
    records = [_rec(gameweek=1, transfers_in=[1]), _rec(gameweek=2, transfers_out=[1])]
    report = churn.render_report(churn.analyse(records))
    assert "FREE BUILD EXCLUDED: none detected" in report


# ---------------------------------------------------------------------------
# M2 -- churn fraction thresholds, denominator includes censored purchases.
# ---------------------------------------------------------------------------


def test_m2_fraction_sold_within_threshold():
    records = [
        _rec(gameweek=0, transfers_in=[1, 2]),
        _rec(gameweek=1, transfers_out=[1]),  # hold 1 -- < 6, < 3, < 2, not < 1
        _rec(gameweek=8, transfers_out=[2]),  # hold 8 -- not < anything
    ]
    m2 = churn.compute_m2(records)
    assert m2["total_purchases"] == 2
    assert m2["fractions"]["lt_1"] == 0.0
    assert m2["fractions"]["lt_2"] == 0.5
    assert m2["fractions"]["lt_3"] == 0.5
    assert m2["fractions"]["lt_6"] == 0.5


def test_m2_denominator_includes_censored_purchases_the_conservative_direction():
    """Attack: a censored purchase must inflate the denominator (making
    the "sold quickly" fraction SMALLER, never larger) -- the direction
    that does not flatter the over-trading hypothesis."""
    records = [
        _rec(gameweek=0, transfers_in=[1]),
        _rec(gameweek=1, transfers_out=[1]),  # hold 1, sold fast
        _rec(gameweek=2, transfers_in=[2]),  # never sold -- censored
        _rec(gameweek=20),
    ]
    m2 = churn.compute_m2(records)
    assert m2["total_purchases"] == 2
    assert m2["censored_count"] == 1
    # If censored purchases were wrongly excluded from the denominator,
    # lt_6 would read 1.0 (1/1) instead of the correct 0.5 (1/2).
    assert m2["fractions"]["lt_6"] == 0.5


def test_m2_zero_purchases_reports_none_fractions_not_division_by_zero():
    m2 = churn.compute_m2([_rec(gameweek=1)])
    assert m2["total_purchases"] == 0
    assert m2["fractions"]["lt_6"] is None


# ---------------------------------------------------------------------------
# M3 -- per-gameweek shape, no attribution of hits to a specific transfer.
# ---------------------------------------------------------------------------


def test_m3_reports_transfer_count_and_hit_points_per_gameweek():
    records = [
        _rec(gameweek=1, transfers_out=[1, 2], hit_points=-4),
        _rec(gameweek=2, transfers_out=[], hit_points=0),
    ]
    m3 = churn.compute_m3(records)
    assert m3 == [
        {"gameweek": 1, "n_transfers": 2, "hit_points": -4},
        {"gameweek": 2, "n_transfers": 0, "hit_points": 0},
    ]


# ---------------------------------------------------------------------------
# M4 -- round trips, ATTACKED with bought, sold, bought again.
# ---------------------------------------------------------------------------


def test_m4_detects_a_sold_and_later_rebought_element():
    records = [
        _rec(gameweek=1, transfers_in=[42]),
        _rec(gameweek=3, transfers_out=[42]),
        _rec(gameweek=6, transfers_in=[42]),
    ]
    m4 = churn.compute_m4(records)
    assert m4["count"] == 1
    assert m4["gap_stats"]["mean"] == 3.0  # gw6 - gw3


def test_m4_two_round_trips_for_the_same_element_both_counted():
    """Attack: bought, sold, bought again, sold again, bought a third
    time -- TWO round trips, not one, and not silently collapsed."""
    records = [
        _rec(gameweek=1, transfers_in=[42]),
        _rec(gameweek=2, transfers_out=[42]),
        _rec(gameweek=4, transfers_in=[42]),  # round trip 1: gap 2
        _rec(gameweek=5, transfers_out=[42]),
        _rec(gameweek=9, transfers_in=[42]),  # round trip 2: gap 4
    ]
    m4 = churn.compute_m4(records)
    assert m4["count"] == 2
    assert m4["gap_stats"]["mean"] == 3.0  # (2 + 4) / 2


def test_m4_a_purchase_never_previously_sold_is_not_a_round_trip():
    records = [_rec(gameweek=1, transfers_in=[1]), _rec(gameweek=5, transfers_in=[2])]
    m4 = churn.compute_m4(records)
    assert m4["count"] == 0


def test_m4_different_elements_never_cross_match():
    records = [_rec(gameweek=1, transfers_out=[1]), _rec(gameweek=2, transfers_in=[2])]
    m4 = churn.compute_m4(records)
    assert m4["count"] == 0


# ---------------------------------------------------------------------------
# analyse / render_report -- D3 (both arms, same season), ASCII-only.
# ---------------------------------------------------------------------------


def test_analyse_produces_one_entry_per_season_arm_pair():
    records = [
        _rec(season="2023-24", arm="h6", gameweek=1),
        _rec(season="2023-24", arm="h1", gameweek=1),
    ]
    result = churn.analyse(records)
    assert set(result.keys()) == {("2023-24", "h6"), ("2023-24", "h1")}
    for entry in result.values():
        assert set(entry.keys()) == {"m1_hold_length", "m2_churn_fractions", "m3_per_gameweek", "m4_round_trips"}


def test_render_report_is_ascii_only():
    records = [
        _rec(season="2023-24", arm="h6", gameweek=1, transfers_in=[1]),
        _rec(season="2023-24", arm="h6", gameweek=2, transfers_out=[1]),
        _rec(season="2023-24", arm="h1", gameweek=1),
    ]
    report = churn.render_report(churn.analyse(records))
    report.encode("ascii")


def test_render_report_shows_both_arms_for_the_same_season():
    records = [
        _rec(season="2023-24", arm="h6", gameweek=1),
        _rec(season="2023-24", arm="h1", gameweek=1),
    ]
    report = churn.render_report(churn.analyse(records))
    assert "arm h6" in report
    assert "arm h1" in report


def test_render_report_empty_log_says_so_without_raising():
    report = churn.render_report(churn.analyse([]))
    assert "empty" in report.lower()


def test_render_report_labels_itself_diagnostic_not_a_gate():
    records = [_rec(gameweek=1)]
    report = churn.render_report(churn.analyse(records))
    assert "not a gate" in report.lower()
