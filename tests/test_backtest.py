"""End-to-end determinism regression test for the Phase 1 gate — blueprint
§7.2's own gate text is "real, REPRODUCIBLE, leak-free totals" — and the
exact reported failure (blueprint §7.2 gate-repair session, s003):
`scripts/run_baselines.py --seed 42 --seasons 2025-26`, run three times on
unmodified code against identical data, produced `greedy_form` totals of
1907, 1909, 1887. `random` and `template` were stable across the same three
runs.

This is LIVE against the real, read-only `data/store/` — never a synthetic
fixture — because the bug was in how the real store's row order (256+
parquet files) interacted with polars' `.unique(maintain_order=False)`
default, which a small synthetic fixture would not reliably reproduce (see
`tests/test_backtest_squad.py`'s and `tests/test_store.py`'s own fail-first
unit tests for the isolated, minimal reproductions of each layer). This
test is the one that actually stands in for the gate: it must fail on
unpatched code and pass on the real store, not just on a fixture built to
be friendly to the fix.

Root cause traced to TWO independent layers (both fixed, per the brief's
"fix both, don't stack until the symptom disappears"):

1. Storage layer (`fplai.store.BitemporalStore.as_of`/`.observations`) had
   no canonical outer `ORDER BY`, so row order was a function of on-disk
   file/write order, not content — proven directly in `tests/test_store.py`.
   Empirically NOT the dominant contributor to the 1907/1909/1887 spread
   observed on this store's current file layout (DuckDB's
   `preserve_insertion_order` default kept a single glob scan stable
   run-to-run here) — but not a "pure function of content" either, and the
   brief is explicit that resting on a stable-by-accident file layout is
   the exact fragility that produced this bug, so it is fixed regardless.
2. Baseline layer — the DOMINANT, verified-live contributor:
   `fplai.backtest.replay._build_view` called polars' `.unique(subset=
   ["element"], keep="first")` without `maintain_order=True` (default
   False) on `current_attributes`. Verified live against data/store/: five
   repeated calls on the identical real 692-row gameweek-1 slice returned
   five COMPLETELY different row orders, every time — not an edge case.
   That candidate order fed straight into `build_squad`'s hill-climb, whose
   swap search picked "whichever candidate was listed first" among ties —
   routine for greedy_form, where many players share a trailing-window
   total of 0. Fixed at both ends: `_build_view` now sorts its output, and
   `build_squad`'s hill-climb no longer depends on input order at all (see
   `tests/test_backtest_squad.py`).
"""

from __future__ import annotations

import pytest

from fplai.backtest.baselines import GreedyFormBaseline, RandomBaseline, TemplateBaseline
from fplai.backtest.data import load_season
from fplai.backtest.replay import SeasonReplay
from fplai.backtest.report import summarise
from fplai.backtest.rules import (
    SEASON_RULES,
    SquadRules,
    rules_for_season,
    transfer_rules_for_season,
)
from fplai.store import BitemporalStore

# 2025-26 chosen for speed (in-progress season, fewest rounds in the store
# of any season with usable data) — this is a determinism check, not a
# baseline-quality check, so the smallest real season that still exercises
# the real multi-hundred-file store layout is the right one to run N times.
_SEASON = "2025-26"
_N_RUNS = 5


def _run_totals(strategy_factory) -> list[int]:
    totals = []
    for _ in range(_N_RUNS):
        store = BitemporalStore()  # fresh instance each run, like a fresh script invocation
        data = load_season(store, _SEASON)
        rules = rules_for_season(_SEASON)
        replay = SeasonReplay(data, rules)
        results = replay.run(strategy_factory(rules))
        summary = summarise(_SEASON, "x", results)
        totals.append(summary.total_points)
    return totals


@pytest.mark.slow
def test_greedy_form_baseline_is_reproducible_across_repeated_runs_on_the_real_store():
    totals = _run_totals(lambda rules: GreedyFormBaseline(rules=rules))
    assert len(set(totals)) == 1, f"greedy_form total varied across {_N_RUNS} runs: {totals}"


@pytest.mark.slow
def test_template_baseline_is_reproducible_across_repeated_runs_on_the_real_store():
    totals = _run_totals(lambda rules: TemplateBaseline(rules=rules))
    assert len(set(totals)) == 1, f"template total varied across {_N_RUNS} runs: {totals}"


@pytest.mark.slow
def test_random_baseline_is_reproducible_across_repeated_runs_on_the_real_store():
    totals = _run_totals(lambda rules: RandomBaseline(seed=42, rules=rules))
    assert len(set(totals)) == 1, f"random(seed=42) total varied across {_N_RUNS} runs: {totals}"


# --- S5: per-season transfer rules (docs/wiki/transfer-rules.md) ----------
#
# No store access here — these are pure lookups against a declared dict, so
# unlike the determinism tests above they are NOT marked slow and run under
# the gate's `-m "not slow"` filter.


def test_transfer_rules_2023_24_is_the_season_a_hardcoded_value_would_get_wrong():
    # 2023-24 banked a max of 2, not 5 — the one value that a single
    # constant shared across seasons (the pre-S4 state of the world) would
    # have gotten wrong. docs/wiki/transfer-rules.md, the 13 Aug 2024
    # premierleague.com announcement.
    rules = transfer_rules_for_season("2023-24")
    assert rules.free_transfers_per_gameweek == 1
    assert rules.max_banked_transfers == 2
    assert rules.hit_cost == -4
    assert rules.free_transfer_overrides_dict == {}


def test_transfer_rules_2024_25_sourced_values():
    rules = transfer_rules_for_season("2024-25")
    assert rules.free_transfers_per_gameweek == 1
    assert rules.max_banked_transfers == 5
    assert rules.hit_cost == -4
    assert rules.free_transfer_overrides_dict == {}


def test_transfer_rules_2025_26_sourced_values_and_gw16_override():
    rules = transfer_rules_for_season("2025-26")
    assert rules.free_transfers_per_gameweek == 1
    assert rules.max_banked_transfers == 5
    assert rules.hit_cost == -4
    # GW16 AFCON top-up TO five, not an addition of five — sparse override,
    # present only for this season and only for this gameweek.
    assert rules.free_transfer_overrides_dict == {16: 5}


def test_transfer_rules_for_an_unsourced_season_raises_and_names_the_wiki_page():
    # Deliberately a season S4 did NOT source (its scope was 2023-24
    # .. 2025-26 only). This must raise, not silently fall back to any
    # season's values or any default.
    with pytest.raises(KeyError, match="transfer-rules.md"):
        transfer_rules_for_season("2022-23")

    with pytest.raises(KeyError):
        transfer_rules_for_season("not-a-real-season")


def test_squad_rules_and_rules_for_season_are_unchanged_by_the_transfer_rules_addition():
    # TransferRules is additive per D1 — SquadRules gained no fields and
    # rules_for_season's existing behaviour (used by every baseline) must
    # be untouched.
    rules = rules_for_season("2023-24")
    assert isinstance(rules, SquadRules)
    assert rules.budget_tenths == 1000
    assert rules.squad_size == 15
    assert rules is SEASON_RULES["2023-24"]

    with pytest.raises(KeyError):
        rules_for_season("not-a-real-season")
