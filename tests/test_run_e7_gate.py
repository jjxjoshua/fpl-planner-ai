"""Tests for `scripts/run_e7_gate.py` -- the E7 gate harness (session
`s007`, story `S10`).

Fast unit tests only, against fabricated inputs -- record shape
(`_build_season_record`), the `total_points`/`hit_points` roll-up
(`_summarise_gameweek_results`), resume bookkeeping (`_read_records`, plus
the imported `run_e6_gate._load_completed_seasons` this script reuses
rather than reimplements, per S10 D2), transfer-count bookkeeping
(`_TransferCountingStrategy`), and table rendering (`_render_report_table`)
-- all pure functions over plain dicts/objects, per this story's own
cost-question ruling: the real ~7.5-hour gate belongs to the Architect, not
to this test suite (`scripts/run_e7_gate.py`'s own module docstring, "D7").

`scripts/` is not a package -- imported by path, the same pattern
`tests/test_run_e6_gate.py` already uses.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_SCRIPT_PATH = _REPO_ROOT / "scripts" / "run_e7_gate.py"
_spec = importlib.util.spec_from_file_location("run_e7_gate", _SCRIPT_PATH)
run_e7_gate = importlib.util.module_from_spec(_spec)
sys.modules["run_e7_gate"] = run_e7_gate
_spec.loader.exec_module(run_e7_gate)

from fplai.backtest.replay import GameweekResult  # noqa: E402


def _gw_result(*, points: int, transfer_hit_points: int = 0, gameweek: int = 1) -> GameweekResult:
    return GameweekResult(
        season="2099-00",
        gameweek=gameweek,
        points=points,
        active_xi_ids=frozenset(),
        autosubs=(),
        effective_captain_id=None,
        transfer_hit_points=transfer_hit_points,
    )


def _arm(*, total_points: int = 100, hit_points: int = 0, n_transfers: int = 0, decide_wall_seconds: float = 1.0) -> dict:
    return {
        "decide_wall_seconds": decide_wall_seconds,
        "total_points": total_points,
        "hit_points": hit_points,
        "n_transfers": n_transfers,
    }


def _record(
    *,
    season: str = "2023-24",
    h6_total: int = 2000,
    h1_total: int = 1900,
    **overrides,
) -> dict:
    arms = {"h6": _arm(total_points=h6_total), "h1": _arm(total_points=h1_total)}
    arms.update(overrides.pop("arms", {}))
    base = dict(
        season=season,
        generated_at="2026-09-06T00:00:00+00:00",
        seed=0,
        git_commit="deadbeef",
        smoke_test=False,
        limit_gameweeks=None,
        n_gameweeks=38,
        dc_neutralised=False,
        dc_cold_start_gameweeks=[],
        fit_wall_seconds=1.0,
        arms=arms,
    )
    base.update(overrides)
    return run_e7_gate._build_season_record(**base)


# ---------------------------------------------------------------------------
# _summarise_gameweek_results -- pure roll-up over fabricated GameweekResults.
# ---------------------------------------------------------------------------


def test_summarise_gameweek_results_sums_points_and_hit_points():
    results = [
        _gw_result(points=10, gameweek=1),
        _gw_result(points=5, transfer_hit_points=-4, gameweek=2),
        _gw_result(points=8, gameweek=3),
    ]
    summary = run_e7_gate._summarise_gameweek_results(results)
    assert summary == {"total_points": 23, "hit_points": -4}


def test_summarise_gameweek_results_raises_on_empty_list():
    with pytest.raises(ValueError, match="no results to summarise"):
        run_e7_gate._summarise_gameweek_results([])


# ---------------------------------------------------------------------------
# _build_season_record -- the D6 record shape, and h6_beats_h1's own
# derivation.
# ---------------------------------------------------------------------------


def test_build_season_record_has_every_d6_field():
    record = _record()
    assert record["season"] == "2023-24"
    assert record["arms"]["h6"]["total_points"] == 2000
    assert record["arms"]["h1"]["total_points"] == 1900
    for field in (
        "season", "generated_at", "seed", "git_commit", "smoke_test", "limit_gameweeks",
        "n_gameweeks", "dc_neutralised", "dc_cold_start_gameweeks", "fit_wall_seconds", "arms",
        "h6_beats_h1",
    ):
        assert field in record, f"missing D6 field: {field}"
    for arm_name in ("h6", "h1"):
        for field in ("decide_wall_seconds", "total_points", "hit_points", "n_transfers"):
            assert field in record["arms"][arm_name], f"arm {arm_name} missing field: {field}"


def test_build_season_record_h6_beats_h1_true_when_h6_scores_more():
    record = _record(h6_total=2100, h1_total=1900)
    assert record["h6_beats_h1"] is True


def test_build_season_record_h6_beats_h1_false_when_h1_scores_more():
    record = _record(h6_total=1800, h1_total=1900)
    assert record["h6_beats_h1"] is False


def test_build_season_record_h6_beats_h1_false_on_an_exact_tie():
    """A tie is not a win -- strict `>`, per D6's own field name ("h6 beats
    h1", not "h6 at least matches h1")."""
    record = _record(h6_total=2000, h1_total=2000)
    assert record["h6_beats_h1"] is False


def test_build_season_record_raises_on_missing_arm():
    with pytest.raises(ValueError, match="expected exactly the arms"):
        run_e7_gate._build_season_record(
            season="2023-24", generated_at="x", seed=0, git_commit="x", smoke_test=False,
            limit_gameweeks=None, n_gameweeks=38, dc_neutralised=False, dc_cold_start_gameweeks=[],
            fit_wall_seconds=1.0, arms={"h6": _arm()},
        )


def test_build_season_record_raises_on_arm_missing_a_required_field():
    incomplete = {"decide_wall_seconds": 1.0, "total_points": 100}  # missing hit_points, n_transfers
    with pytest.raises(ValueError, match="missing field"):
        run_e7_gate._build_season_record(
            season="2023-24", generated_at="x", seed=0, git_commit="x", smoke_test=False,
            limit_gameweeks=None, n_gameweeks=38, dc_neutralised=False, dc_cold_start_gameweeks=[],
            fit_wall_seconds=1.0, arms={"h6": incomplete, "h1": _arm()},
        )


# ---------------------------------------------------------------------------
# Resume bookkeeping -- _read_records (this script's own report-only
# reader) AND the imported run_e6_gate._load_completed_seasons (S10, D2:
# reused, never reimplemented -- attacked here by driving it through THIS
# module's own reference, not just trusting test_run_e6_gate.py's coverage
# of the same function).
# ---------------------------------------------------------------------------


def test_read_records_empty_when_file_absent(tmp_path: Path):
    assert run_e7_gate._read_records(tmp_path / "does_not_exist.jsonl") == []


def test_read_records_reads_back_written_records_in_order(tmp_path: Path):
    out = tmp_path / "results.jsonl"
    r1 = _record(season="2023-24", h6_total=2000, h1_total=1900)
    r2 = _record(season="2024-25", h6_total=2100, h1_total=2050)
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps(r1) + "\n")
        f.write(json.dumps(r2) + "\n")
    records = run_e7_gate._read_records(out)
    assert [r["season"] for r in records] == ["2023-24", "2024-25"]


def test_read_records_skips_malformed_lines_without_raising(tmp_path: Path):
    out = tmp_path / "results.jsonl"
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_record(season="2023-24")) + "\n")
        f.write("{not valid json\n")
        f.write("\n")
        f.write(json.dumps(_record(season="2024-25")) + "\n")
    records = run_e7_gate._read_records(out)
    assert [r["season"] for r in records] == ["2023-24", "2024-25"]


def test_load_completed_seasons_is_the_imported_e6_function_reused_not_reimplemented(tmp_path: Path):
    """S10, D2: attacked, not merely asserted -- writes via THIS module's
    own `run_e6_gate` reference and reads back via the identical function,
    proving run_e7_gate did not silently fork its own copy under the same
    name."""
    out = tmp_path / "results.jsonl"
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"season": "2023-24"}) + "\n")
    assert run_e7_gate.run_e6_gate._load_completed_seasons(out) == {"2023-24"}
    # And it is the SAME function object test_run_e6_gate.py's own import sees.
    import run_e6_gate as _direct_e6_import

    assert run_e7_gate.run_e6_gate._load_completed_seasons is _direct_e6_import._load_completed_seasons


# ---------------------------------------------------------------------------
# _TransferCountingStrategy -- D6's own n_transfers bookkeeping, attacked
# with a fake inner strategy returning canned decisions (structural, not
# `Decision`-typed -- only `.transfers_out` is ever read).
# ---------------------------------------------------------------------------


class _FakeDecision:
    def __init__(self, transfers_out):
        self.transfers_out = transfers_out


class _FakeInnerStrategy:
    name = "fake-inner"

    def __init__(self, decisions):
        self._decisions = iter(decisions)
        self.seen_views = []

    def decide(self, view):
        self.seen_views.append(view)
        return next(self._decisions)


def test_transfer_counting_strategy_accumulates_across_decide_calls():
    inner = _FakeInnerStrategy([_FakeDecision(transfers_out=(1, 2)), _FakeDecision(transfers_out=()), _FakeDecision(transfers_out=(3,))])
    counting = run_e7_gate._TransferCountingStrategy(inner)
    for _ in range(3):
        counting.decide(view="a-view-object")
    assert counting.n_transfers == 3  # 2 + 0 + 1


def test_transfer_counting_strategy_passes_the_decision_through_unchanged():
    decision = _FakeDecision(transfers_out=(5,))
    inner = _FakeInnerStrategy([decision])
    counting = run_e7_gate._TransferCountingStrategy(inner)
    returned = counting.decide(view="v")
    assert returned is decision


def test_transfer_counting_strategy_carries_the_inner_strategys_name():
    inner = _FakeInnerStrategy([])
    inner.name = "horizon(H=6)"
    counting = run_e7_gate._TransferCountingStrategy(inner)
    assert counting.name == "horizon(H=6)"


# ---------------------------------------------------------------------------
# S10a, D1 -- record_decisions=False is the default and must leave
# behaviour byte-for-byte unchanged (n_transfers accumulation, decision
# pass-through) from every pre-S10a test above; record_decisions=True
# additionally captures every Decision in order.
# ---------------------------------------------------------------------------


def test_transfer_counting_strategy_default_does_not_record_decisions():
    inner = _FakeInnerStrategy([_FakeDecision(transfers_out=(1,))])
    counting = run_e7_gate._TransferCountingStrategy(inner)
    counting.decide(view="v")
    assert counting.decisions is None


def test_transfer_counting_strategy_record_decisions_true_captures_every_decision_in_order():
    d1, d2, d3 = _FakeDecision(transfers_out=(1,)), _FakeDecision(transfers_out=()), _FakeDecision(transfers_out=(2, 3))
    inner = _FakeInnerStrategy([d1, d2, d3])
    counting = run_e7_gate._TransferCountingStrategy(inner, record_decisions=True)
    for _ in range(3):
        counting.decide(view="v")
    assert counting.decisions == [d1, d2, d3]
    assert counting.n_transfers == 3  # unchanged accumulation alongside recording


# ---------------------------------------------------------------------------
# S10a, D1 -- _build_decision_log_record: pure over a Decision-shaped
# object (only .transfers_in/.transfers_out/.squad.players[*].id read) and
# a GameweekResult-shaped object (only .transfer_hit_points/.points read).
# hit_points/points come from the REAL, already-scored result -- never
# recomputed -- so this is attacked by fabricating a result whose values
# would NOT match the hit-cost formula, proving nothing derives them.
# ---------------------------------------------------------------------------


class _FakePlayer:
    def __init__(self, id):
        self.id = id


class _FakeSquad:
    def __init__(self, player_ids):
        self.players = tuple(_FakePlayer(i) for i in player_ids)


class _FakeDecisionForLog:
    def __init__(self, *, transfers_in, transfers_out, squad_ids):
        self.transfers_in = transfers_in
        self.transfers_out = transfers_out
        self.squad = _FakeSquad(squad_ids)


class _FakeResult:
    def __init__(self, *, transfer_hit_points, points):
        self.transfer_hit_points = transfer_hit_points
        self.points = points


class _FakeRoundPlan:
    def __init__(self, *, round, expected_points, hits, free_transfers_available, transfers_in=(), transfers_out=()):
        self.round = round
        self.expected_points = expected_points
        self.hits = hits
        self.free_transfers_available = free_transfers_available
        self.transfers_in = transfers_in
        self.transfers_out = transfers_out


class _FakeMultiPeriodResult:
    def __init__(self, *, plan, transfers_in=(), transfers_out=(), hits=0):
        self.plan = tuple(plan)
        self.transfers_in = transfers_in
        self.transfers_out = transfers_out
        self.hits = hits


class _FakeTransferRules:
    hit_cost = -4


class _FakeView:
    season = "2023-24"
    gameweek = 5


def test_build_decision_log_record_has_every_d1_field():
    decision = _FakeDecisionForLog(transfers_in=(10,), transfers_out=(20,), squad_ids=(1, 2, 3))
    result = _FakeResult(transfer_hit_points=-4, points=57)
    record = run_e7_gate._build_decision_log_record(
        season="2023-24", arm="h6", gameweek=5, decision=decision, result=result
    )
    assert record == {
        "season": "2023-24",
        "arm": "h6",
        "gameweek": 5,
        "transfers_in": [10],
        "transfers_out": [20],
        "hit_points": -4,
        "points": 57,
        "squad_element_ids": [1, 2, 3],
    }


def test_build_decision_log_record_hit_points_and_points_come_from_result_not_a_formula():
    """Attack: fabricate a result whose hit_points/points do NOT match
    `transfer_rules.hit_cost * max(0, n_transfers - free_transfers)` for
    any plausible free-transfers count -- if this function were secretly
    recomputing rather than reading `result`, it could not reproduce
    these exact, formula-defying numbers."""
    decision = _FakeDecisionForLog(transfers_in=(1, 2, 3), transfers_out=(4, 5, 6), squad_ids=())
    result = _FakeResult(transfer_hit_points=-999, points=-12345)
    record = run_e7_gate._build_decision_log_record(
        season="2024-25", arm="h1", gameweek=1, decision=decision, result=result
    )
    assert record["hit_points"] == -999
    assert record["points"] == -12345


def test_build_transfer_value_record_compares_football_value_not_raw_solver_objective():
    chosen = _FakeMultiPeriodResult(
        transfers_in=(10, 11), transfers_out=(20, 21), hits=1,
        plan=(
            _FakeRoundPlan(round=5, expected_points=70.0, hits=1, free_transfers_available=1, transfers_in=(10, 11), transfers_out=(20, 21)),
            _FakeRoundPlan(round=6, expected_points=65.0, hits=0, free_transfers_available=1),
        ),
    )
    no_hit = _FakeMultiPeriodResult(
        transfers_in=(10,), transfers_out=(20,), hits=0,
        plan=(
            _FakeRoundPlan(round=5, expected_points=67.0, hits=0, free_transfers_available=1, transfers_in=(10,), transfers_out=(20,)),
            _FakeRoundPlan(round=6, expected_points=63.0, hits=0, free_transfers_available=1),
        ),
    )

    record = run_e7_gate._build_transfer_value_record(
        season="2023-24", arm="h6", gameweek=5,
        chosen=chosen, no_current_hit=no_hit, transfer_rules=_FakeTransferRules(), free_build=False,
    )

    assert record["horizon_gross_expected_points"] == 135.0
    assert record["horizon_hit_points"] == -4
    assert record["horizon_net_expected_points"] == 131.0
    assert record["current_free_transfers_available"] == 1
    assert record["current_hits"] == 1
    alternative = record["no_current_hit_alternative"]
    assert alternative["horizon_net_expected_points"] == 130.0
    assert alternative["horizon_net_delta_chosen_minus_no_hit"] == 1.0
    assert alternative["horizon_gross_delta_chosen_minus_no_hit"] == 5.0
    assert alternative["current_gross_delta_chosen_minus_no_hit"] == 3.0
    assert alternative["future_gross_delta_chosen_minus_no_hit"] == 2.0
    assert alternative["hit_points_delta_chosen_minus_no_hit"] == -4


def test_transfer_value_observer_runs_no_hit_resolve_only_when_primary_decision_pays_a_hit(monkeypatch):
    chosen = _FakeMultiPeriodResult(
        transfers_in=(10, 11), transfers_out=(20, 21), hits=1,
        plan=(_FakeRoundPlan(round=5, expected_points=70.0, hits=1, free_transfers_available=1),),
    )
    no_hit = _FakeMultiPeriodResult(
        transfers_in=(10,), transfers_out=(20,), hits=0,
        plan=(_FakeRoundPlan(round=5, expected_points=66.0, hits=0, free_transfers_available=1),),
    )
    calls = []

    def fake_optimise(horizon_candidates, rules, transfer_rules, incoming_state=None, config=None, **kwargs):
        calls.append((horizon_candidates, incoming_state, kwargs))
        return no_hit

    monkeypatch.setattr(run_e7_gate, "optimise_multi_period", fake_optimise)
    observer = run_e7_gate._TransferValueObserver(
        arm="h6", rules=object(), transfer_rules=_FakeTransferRules(), config=object()
    )
    incoming = object()
    horizon = {5: ("candidate",)}
    observer(_FakeView(), horizon, incoming, chosen)

    assert len(calls) == 1
    assert calls[0][0] is horizon
    assert calls[0][1] is incoming
    assert calls[0][2]["max_current_round_hits"] == 0
    assert observer.records[0]["no_current_hit_alternative"] is not None

    no_hit_primary = _FakeMultiPeriodResult(
        transfers_in=(10,), transfers_out=(20,), hits=0,
        plan=(_FakeRoundPlan(round=6, expected_points=60.0, hits=0, free_transfers_available=1),),
    )
    class _GW6View:
        season = "2023-24"
        gameweek = 6
    observer(_GW6View(), {6: ("candidate",)}, incoming, no_hit_primary)
    assert len(calls) == 1  # no counterfactual solve for an already-hit-free optimum
    assert observer.records[1]["no_current_hit_alternative"] is None


def test_attach_realised_transfer_value_record_uses_replay_result_and_attacks_hit_mismatch():
    record = {"gameweek": 5, "current_hit_points": -4}
    result = _FakeResult(transfer_hit_points=-4, points=57)
    attached = run_e7_gate._attach_realised_transfer_value_record(record, result)
    assert attached["realised_points"] == 57
    assert attached["realised_hit_points"] == -4
    assert record == {"gameweek": 5, "current_hit_points": -4}  # pure; input not mutated

    with pytest.raises(ValueError, match=r"diagnostic hit points.*replay"):
        run_e7_gate._attach_realised_transfer_value_record(
            {"gameweek": 5, "current_hit_points": -8},
            _FakeResult(transfer_hit_points=-4, points=57),
        )


# ---------------------------------------------------------------------------
# _render_report_table -- D7's pure rendering, ASCII only, over fabricated
# records.
# ---------------------------------------------------------------------------


def test_render_report_table_is_ascii_only():
    records = [_record(season="2023-24", h6_total=2000, h1_total=1900), _record(season="2024-25", h6_total=2100, h1_total=2200)]
    table = run_e7_gate._render_report_table(records)
    table.encode("ascii")  # raises UnicodeEncodeError if anything non-ASCII slipped in


def test_render_report_table_includes_every_season_and_the_mean():
    records = [
        _record(season="2023-24", h6_total=2000, h1_total=1900),
        _record(season="2024-25", h6_total=2100, h1_total=2200),
        _record(season="2025-26", h6_total=1900, h1_total=1800),
    ]
    table = run_e7_gate._render_report_table(records)
    for season in ("2023-24", "2024-25", "2025-26"):
        assert season in table
    assert "MEAN(3)" in table
    # mean h6 = (2000+2100+1900)/3 = 2000.0, mean h1 = (1900+2200+1800)/3 = 1966.7
    assert "2000.0" in table
    assert "1966.7" in table


def test_render_report_table_sorts_by_season_regardless_of_input_order():
    records = [_record(season="2025-26"), _record(season="2023-24"), _record(season="2024-25")]
    table = run_e7_gate._render_report_table(records)
    lines = [line for line in table.splitlines() if line and line[0].isdigit()]
    assert lines[0].startswith("2023-24")
    assert lines[1].startswith("2024-25")
    assert lines[2].startswith("2025-26")


def test_render_report_table_empty_records_says_so_without_raising():
    table = run_e7_gate._render_report_table([])
    assert "no completed seasons" in table


def test_render_report_table_labels_e6_totals_as_context_not_the_bar():
    """D9: if a comparison to E6's ceiling ever shows up in this table's
    own fixed text, it must be plainly labelled -- checked here on the
    STATIC note this function always appends, so a future edit that
    silently drops or waters down the label fails this test."""
    table = run_e7_gate._render_report_table([_record()])
    assert "CEILING" in table
    assert "not the" in table.lower() or "not a" in table.lower()


# ---------------------------------------------------------------------------
# GATE_SEASONS -- D4's own claim about which three seasons stateful mode
# targets.
# ---------------------------------------------------------------------------


def test_gate_seasons_is_exactly_the_three_sourced_transfer_rules_seasons():
    from fplai.backtest.rules import SEASON_TRANSFER_RULES

    assert set(run_e7_gate.GATE_SEASONS) == set(SEASON_TRANSFER_RULES)
