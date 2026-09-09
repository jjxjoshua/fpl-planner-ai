#!/usr/bin/env python
"""E7 gate runner -- the receding-horizon MILP's own gate (blueprint SS6.1/
SS7.2, CLAUDE.md rules 1/2/4/5/7). Session `s007`, story S10.

**The gate, verbatim (registered, not re-derived here)**: H=6 must beat a
myopic H=1 arm carrying the IDENTICAL signal, both inside the STATEFUL
replay harness, both paying identical transfer costs and hits, over
2023-24 / 2024-25 / 2025-26. Run once, report whatever it says. H is not
tuned -- blueprint SS6.1 pins `t..t+5`.

**This script is the harness. It does not decide whether the gate
passes.** It prints the per-season and three-season-mean comparison; a
human (the Architect) reads it and forms the verdict.

## Why this script IMPORTS `scripts/run_e6_gate.py` rather than
re-implementing its own fitting loop (S10, D2)

`run_e6_gate.py` is loaded the way `tests/test_run_e6_gate.py` already
loads it -- `importlib.util.spec_from_file_location`, this repo's own
established pattern for a `scripts/` module with no `__init__.py`. It ends
in a proper `if __name__ == "__main__":` guard, so importing it executes
nothing.

Reused, never reimplemented: `_fit_params_by_gameweek`, `_needs_dc_
neutralisation`, `_neutralise_dc`, `_fit_shared_dc_bundle`, `_degenerate_
dc_bundle`, `_truncate_season`, `_git_commit`, `_load_completed_seasons`.

**Why this is worth an underscore-crossing import**: `_fit_params_by_
gameweek` carries leakage reasoning that took an Architect review to
settle in session s006 -- a cold-start gameweek falls back to `_degenerate_
dc_bundle(as_of)` and NOT to the shared whole-season bundle, because the
shared bundle is fitted at `as_of=now()` and has seen the whole of 2025-26
and 2026-27; feeding it into a gameweek whose `ScoringConfig` is still
live (not zeroed) would be genuine leakage under CLAUDE.md rule 2, not the
provably-inert substitution the whole-season case gets. A second copy of
that logic in this file is a leakage bug waiting to happen -- one
implementation, imported, is the only acceptable posture. `run_e6_gate.py`
itself is NEVER edited by this script or this story (its own
reproducibility claim for the Phase 3 gate record depends on staying
untouched).

## The one optimiser change this story makes (S10, D1)

`HorizonCandidateSource.horizon_candidates(view, *, max_rounds=None)` --
`None` assembles every round (today's behaviour, byte for byte); an int
assembles only the first `max_rounds` rounds. `HorizonStrategy.decide`
passes `max_rounds=self._horizon` and STILL truncates the result
afterwards (defensive belt-and-braces -- a source that ignores the hint
cannot change a decision). This is what makes the myopic H=1 arm assemble
ONE round instead of six -- ~2.7 of the ~3.3 assembly-hours a naive H=1
arm would otherwise cost across three seasons (this story's own probe 1).
See `fplai.optimiser`'s own module section, "S10, D1", for the full
writeup; that change lives in `src/fplai/optimiser.py`, not here.

## D3 -- ONE model layer, two arms

Exactly one `ModelStackStrategy` per season, wrapped in TWO independent
`HorizonStrategy` instances (`horizon=6`, `horizon=1`) SHARING that one
instance. This is what makes "both arms carry the identical signal" true
BY CONSTRUCTION, not by assertion -- see `tests/test_optimiser.py::
test_max_rounds_one_is_bit_identical_to_the_full_horizon_at_round_t_on_the_
real_store` (S10's own G2) for the proof this relies on.

## D4 -- both arms run STATEFUL, and only on three sourced seasons

`SeasonReplay(season_data, rules).run(arm, initial_state=free_build_state(
rules))`, with `transfer_rules = transfer_rules_for_season(season)`. That
call RAISES `KeyError` for any season outside 2023-24/2024-25/2025-26 --
this script lets it raise. Stateful mode is unavailable outside those
three seasons, not degraded, and a season this script cannot honestly
complete is never recorded as a result (mirrors `run_e6_gate.py`'s own
`run_season` docstring, verbatim posture).

## D5 -- H is not tuned

The arms are H=6 and H=1, full stop. No `--horizons` flag is exposed --
there is nothing to develop against that `--limit-gameweeks` does not
already cover (a full season replay is deterministic given a season and a
seed; a partial one is the smoke-test aid below).

## D6 -- incremental JSONL, per-season resume, E6's own shape

Default `--out` is `data/gate/e7_gate_results.jsonl`. One line appended
the MOMENT a season finishes, flushed immediately. Re-running the same
command SKIPS seasons already present (`run_e6_gate._load_completed_
seasons`, imported, never reimplemented -- skip key is `season` alone,
same simplicity argument `run_e6_gate.py`'s own docstring makes).

**Per-season resume is deliberate, not an oversight.** The real run is
~7.5 hours (fit ~0.83h + H=6 solve ~0.83h + H=1 solve ~1 minute, all times
three seasons, per this story's own probes 2/3); a crash costs at most
~2.5 hours of the LAST unfinished season, not the whole run. Per-gameweek
checkpointing would need a generator refactor of the leakage-critical
replay harness this story's own brief explicitly declined to build (S10's
ruling: `max_rounds` recovers ~2.7h of ~3.3h of assembly waste for about
ten lines; a full lockstep-and-memo cross-arm share would recover the
remaining ~0.55h at the cost of that refactor plus a cache -- not worth it
here, and per-gameweek resume would need the identical refactor).

## D7 -- the report renders FROM the JSONL, as a pure function

`--report-only` prints the comparison table from an existing `--out` file
WITHOUT running anything -- `_render_report_table` is a pure function over
plain dicts (this story's own record shape, below), independently
unit-testable in milliseconds with fabricated records
(`tests/test_run_e7_gate.py`), never requiring a real multi-hour run to
exercise its formatting. ASCII only in console output (a cp1252 console
has already corrupted a script's output in this project once).

## D8/D9 -- what this script does NOT claim

Phase 3's 2,280.3-point mean (E6's own `model_stack` totals) is a CEILING,
scored with free weekly re-picks and no transfer costs -- this script
never compares E7's stateful, transfer-cost-paying totals against it as a
pass/fail bar; if E6's numbers are printed at all they are labelled
plainly as "free re-pick, no transfer cost -- CONTEXT, not the bar."
`TrailingProxyHorizonSource` (S9's own fixture-blind proxy) appears
NOWHERE in this script -- S9's own gate totals (H=1 beating H=6) are an
artefact of that proxy's fixture-blindness, not an E7 result, and reusing
them here would be exactly the leak this story's brief forbids.

## D1 (S10a) -- `--decision-log PATH`, an opt-in per-decision trace

The E7 gate FAILED (`09-06`, `docs/wiki/e7-gate-results.jsonl`): H=6 wins on
GROSS in all three seasons and loses on NET in all three, purely on paid
hits. The leading hypothesis is classic receding-horizon over-trading --
`optimise_multi_period` books a transfer's benefit across up to six
rounds, but `SeasonReplay` commits round `t` only and re-solves, so the
-4 is paid in full immediately while the modelled benefit is only earned
if the player is actually held. `--decision-log PATH` (default `None` =
today's behaviour byte-for-byte) makes that hypothesis measurable without
touching the optimiser or the replay harness: `_TransferCountingStrategy`
(already wrapping every arm to count transfers for the `--out` record)
optionally also RECORDS every `Decision` it observes
(`record_decisions=True`), in order; once `SeasonReplay.run` returns that
arm's full `list[GameweekResult]` (same length, same order, one per
round), `run_season` zips the two together and appends one JSONL line per
(season, arm, gameweek) to `--decision-log`, flushed immediately. This is
NOT hooked into `decide()` itself -- `points`/`hit_points` are not known
at decide-time (`SeasonReplay` computes and scores them strictly after
`decide()` returns), and recomputing the hit-cost formula independently
inside the wrapper would risk silent drift from the one true computation
in `fplai.backtest.replay.SeasonReplay.run`. Reading the real
`GameweekResult` instead of recomputing is both simpler and the only
version that cannot disagree with the `--out` totals it must reconcile
against. See `scripts/analyse_transfer_churn.py` for what reads this log.
**This story is evidence-gathering only** -- no objective, horizon, cost,
or model changes; see that script's own module docstring for the ruling.

## Usage

    # The real gate -- all three sourced seasons, no --limit-gameweeks:
    uv run python scripts/run_e7_gate.py

    # A specific subset:
    uv run python scripts/run_e7_gate.py --seasons 2024-25,2025-26

    # Smoke-test aid ONLY -- see the flag's own help text:
    uv run python scripts/run_e7_gate.py --seasons 2025-26 --limit-gameweeks 3

    # Re-print the comparison table from an existing results file, without
    # running anything:
    uv run python scripts/run_e7_gate.py --report-only

    # S10a evidence run -- also write a per-decision trace:
    uv run python scripts/run_e7_gate.py --decision-log data/gate/e7_decisions.jsonl
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# S10, D2: scripts/ is not a package -- imported by path, the same pattern
# tests/test_run_e6_gate.py already establishes. run_e6_gate.py's own
# `if __name__ == "__main__":` guard means this executes nothing.
_E6_SCRIPT_PATH = _REPO_ROOT / "scripts" / "run_e6_gate.py"
_e6_spec = importlib.util.spec_from_file_location("run_e6_gate", _E6_SCRIPT_PATH)
run_e6_gate = importlib.util.module_from_spec(_e6_spec)
sys.modules["run_e6_gate"] = run_e6_gate
_e6_spec.loader.exec_module(run_e6_gate)

from fplai.backtest.data import SeasonDataError, load_season  # noqa: E402
from fplai.backtest.replay import GameweekResult, SeasonReplay, free_build_state  # noqa: E402
from fplai.backtest.rules import rules_for_season, transfer_rules_for_season  # noqa: E402
from fplai.optimiser import HorizonStrategy, ModelStackStrategy, OptimiserConfig, OptimiserError  # noqa: E402
from fplai.scoring import load_scoring_config  # noqa: E402
from fplai.store import BitemporalStore  # noqa: E402

# S10, D4: the only three seasons with a SOURCED TransferRules -- the only
# three stateful mode can honestly run against. Declared independently
# rather than imported from fplai.backtest.rules (this codebase's own
# "house vocabulary, declared independently" convention, fplai.optimiser's
# own module docstring, POSITIONS) -- this script's OWN claim about which
# seasons it targets, not a re-export of another module's dict keys.
GATE_SEASONS = ("2023-24", "2024-25", "2025-26")

ARM_NAMES = ("h6", "h1")
_HORIZON_BY_ARM = {"h6": 6, "h1": 1}


# ---------------------------------------------------------------------------
# Transfer-count bookkeeping (S10, D6's own `n_transfers` field). Neither
# `GameweekResult` nor `SeasonSummary` carries a per-season transfer count
# today -- this wrapper is the cheapest way to get one without widening
# either of those READ-ONLY types. Structural, not `Strategy`-typed: it
# only ever reads `.transfers_out` off whatever `decide()` returns and
# passes the object straight through unchanged.
# ---------------------------------------------------------------------------


class _TransferCountingStrategy:
    """Wraps a `Strategy`, counting `len(decision.transfers_out)` across
    every `decide()` call. This is the SAME quantity `SeasonReplay.run`'s
    own hit-cost computation already reads off each decision (`n_transfers
    = len(decision.transfers_out)`) -- this class only accumulates it
    across a season, it does not recompute what a hit costs.

    S10a, D1: optionally ALSO records every `Decision` it observes, in
    order, when `record_decisions=True`. Default `False` leaves
    `self.decisions` `None` and every existing call site (E7's own
    `--out` bookkeeping, every test predating S10a) behaves byte-for-byte
    as before -- this class still never computes a hit cost or a points
    total itself; `run_season` pairs `self.decisions` with the real
    `list[GameweekResult]` `SeasonReplay.run` returns (same order, same
    length, one per round) to get those."""

    def __init__(self, inner, *, record_decisions: bool = False):
        self.name = inner.name
        self._inner = inner
        self.n_transfers = 0
        self._record_decisions = record_decisions
        self.decisions: list | None = [] if record_decisions else None

    def decide(self, view):
        decision = self._inner.decide(view)
        self.n_transfers += len(decision.transfers_out)
        if self._record_decisions:
            self.decisions.append(decision)
        return decision


# ---------------------------------------------------------------------------
# Pure functions -- record shape, resume bookkeeping (reused from
# run_e6_gate, never reimplemented), and table rendering. Exercised by
# `tests/test_run_e7_gate.py` against fabricated inputs in milliseconds,
# per this story's own cost-question ruling: the real 7.5-hour run belongs
# to the Architect, not to this script's own test suite.
# ---------------------------------------------------------------------------


def _summarise_gameweek_results(results: list[GameweekResult]) -> dict:
    """`total_points`/`hit_points` for one arm's one season -- pure over a
    plain list of `GameweekResult`-like objects (only `.points`/`.
    transfer_hit_points` are read), so a test can fabricate these directly
    rather than running a real `SeasonReplay`."""
    if not results:
        raise ValueError("no results to summarise -- an empty season is a caller bug, not a valid arm result")
    return {
        "total_points": sum(r.points for r in results),
        "hit_points": sum(r.transfer_hit_points for r in results),
    }


def _build_season_record(
    *,
    season: str,
    generated_at: str,
    seed: int,
    git_commit: str,
    smoke_test: bool,
    limit_gameweeks: int | None,
    n_gameweeks: int,
    dc_neutralised: bool,
    dc_cold_start_gameweeks: list[int],
    fit_wall_seconds: float,
    arms: dict[str, dict],
) -> dict:
    """The JSON-serialisable record this script appends to `--out` (S10,
    D6). Pure -- takes plain values/dicts, builds a plain dict, never
    touches the store, the clock (`generated_at` is the CALLER's to
    supply, so a test can pass a fixed string), or the filesystem."""
    if set(arms) != set(ARM_NAMES):
        raise ValueError(f"expected exactly the arms {ARM_NAMES}, got {sorted(arms)}")
    for name in ARM_NAMES:
        missing = {"decide_wall_seconds", "total_points", "hit_points", "n_transfers"} - set(arms[name])
        if missing:
            raise ValueError(f"arm {name!r} record is missing field(s): {sorted(missing)}")
    return {
        "season": season,
        "generated_at": generated_at,
        "seed": seed,
        "git_commit": git_commit,
        "smoke_test": smoke_test,
        "limit_gameweeks": limit_gameweeks,
        "n_gameweeks": n_gameweeks,
        "dc_neutralised": dc_neutralised,
        "dc_cold_start_gameweeks": list(dc_cold_start_gameweeks),
        "fit_wall_seconds": fit_wall_seconds,
        "arms": {name: dict(arms[name]) for name in ARM_NAMES},
        "h6_beats_h1": arms["h6"]["total_points"] > arms["h1"]["total_points"],
    }


def _build_decision_log_record(*, season: str, arm: str, gameweek: int, decision, result) -> dict:
    """S10a, D1: one `--decision-log` line, pure over a `Decision`-shaped
    object (`.transfers_in`/`.transfers_out`/`.squad.players[*].id` are the
    only attributes read) and a `GameweekResult`-shaped object
    (`.transfer_hit_points`/`.points` are the only attributes read) --
    structural, not type-checked, so a test can fabricate both with plain
    stand-in objects (`tests/test_run_e7_gate.py`'s own `_FakeDecision`
    convention). `hit_points`/`points` are read off the REAL, already-
    scored `GameweekResult` -- never recomputed -- so this record can never
    disagree with the `--out` totals `_summarise_gameweek_results` builds
    from the identical `list[GameweekResult]`."""
    return {
        "season": season,
        "arm": arm,
        "gameweek": gameweek,
        "transfers_in": list(decision.transfers_in),
        "transfers_out": list(decision.transfers_out),
        "hit_points": result.transfer_hit_points,
        "points": result.points,
        "squad_element_ids": [p.id for p in decision.squad.players],
    }


def _read_records(out_path: Path) -> list[dict]:
    """Every well-formed JSON line in `out_path`, in file order. A
    malformed line is skipped, never fatal -- mirrors `run_e6_gate.
    _load_completed_seasons`'s own tolerance, for the same reason (an
    interrupted `write` mid-line must not block reading back what
    completed before it)."""
    if not out_path.exists():
        return []
    records: list[dict] = []
    with out_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _render_report_table(records: list[dict]) -> str:
    """D7: the comparison table, as a pure function of already-written
    records -- never re-derives anything from the store. ASCII only (a
    cp1252 console has already corrupted a script's own output in this
    project once). Sorted by season; a trailing MEAN row averages
    `total_points` over however many records were passed (the real gate
    always passes exactly the three `GATE_SEASONS`, but this function
    itself does not assume that)."""
    ordered = sorted(records, key=lambda r: r["season"])
    lines = [
        f"{'season':10} {'H6 total':>9} {'H1 total':>9} {'H6 hits':>8} {'H1 hits':>8} "
        f"{'H6 xfers':>9} {'H1 xfers':>9} {'H6>H1':>6}"
    ]
    for r in ordered:
        h6, h1 = r["arms"]["h6"], r["arms"]["h1"]
        lines.append(
            f"{r['season']:10} {h6['total_points']:9d} {h1['total_points']:9d} "
            f"{h6['hit_points']:8d} {h1['hit_points']:8d} {h6['n_transfers']:9d} {h1['n_transfers']:9d} "
            f"{str(r['h6_beats_h1']):>6}"
        )
    if ordered:
        n = len(ordered)
        mean_h6 = sum(r["arms"]["h6"]["total_points"] for r in ordered) / n
        mean_h1 = sum(r["arms"]["h1"]["total_points"] for r in ordered) / n
        n_h6_beats_h1 = sum(1 for r in ordered if r["h6_beats_h1"])
        lines.append(
            f"{'MEAN(' + str(n) + ')':10} {mean_h6:9.1f} {mean_h1:9.1f} {'':8} {'':8} {'':9} {'':9} "
            f"{n_h6_beats_h1}/{n}"
        )
    else:
        lines.append("(no completed seasons in the results file -- nothing to report)")
    lines.append("")
    lines.append(
        "NOTE: Phase 3's 2,280.3-point mean (E6's own model_stack totals) is a CEILING, not the "
        "bar -- free weekly re-picks, no transfer costs. Not comparable to the stateful, "
        "transfer-cost-paying totals above."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The impure part -- fits the model stack, runs both arms stateful, times
# each, and returns one season's record. Not exercised by the fast test
# suite; the real store / real solve is what makes this expensive.
# ---------------------------------------------------------------------------


def run_season(
    store: BitemporalStore,
    season: str,
    *,
    seed: int,
    limit_gameweeks: int | None,
    scoring_config_base,
    shared_dc_cache: dict,
    git_commit: str,
    decision_log_path: Path | None = None,
) -> dict:
    """Fit ONE `ModelStackStrategy` (S10, D3), run it stateful under TWO
    `HorizonStrategy` wrappers (`horizon=6`, `horizon=1`), and return the
    JSON-serialisable record (S10, D6). Raises `SeasonDataError`/
    `OptimiserError`/`KeyError` outward -- a season this script cannot
    honestly complete is never silently recorded as a result (mirrors
    `run_e6_gate.run_season`'s own posture, verbatim).

    S10a, D1: `decision_log_path=None` (default) leaves this function's
    behaviour byte-for-byte unchanged. Given a path, each arm's decisions
    (captured by `_TransferCountingStrategy(..., record_decisions=True)`)
    are zipped against that arm's own `list[GameweekResult]` -- same
    order, same length, one per round, both produced by the identical
    `SeasonReplay.run` call below -- and appended, one JSONL line per
    round, flushed immediately per line."""
    season_data_full = load_season(store, season)
    season_data = run_e6_gate._truncate_season(season_data_full, limit_gameweeks)
    rules = rules_for_season(season)
    transfer_rules = transfer_rules_for_season(season)  # S10, D4: raises for an unsourced season -- let it raise.

    needs_dc_neutralisation = run_e6_gate._needs_dc_neutralisation(season_data)
    scoring_config = (
        run_e6_gate._neutralise_dc(scoring_config_base) if needs_dc_neutralisation else scoring_config_base
    )

    def get_shared_dc():
        if "bundle" not in shared_dc_cache:
            shared_dc_cache["bundle"] = run_e6_gate._fit_shared_dc_bundle(store)
        return shared_dc_cache["bundle"]

    params_by_gameweek, fit_wall_seconds, cold_start_gameweeks = run_e6_gate._fit_params_by_gameweek(
        store, season_data, needs_dc_neutralisation=needs_dc_neutralisation, get_shared_dc=get_shared_dc
    )

    config = OptimiserConfig(random_seed=seed)
    # S10, D3: exactly ONE ModelStackStrategy, shared by both HorizonStrategy
    # wrappers below -- what makes "both arms carry the identical signal"
    # true by construction, not by assertion.
    model_stack = ModelStackStrategy(
        rules=rules, params_by_gameweek=params_by_gameweek, scoring_config=scoring_config, config=config
    )

    arms: dict[str, dict] = {}
    for arm_name in ARM_NAMES:
        horizon_strategy = HorizonStrategy(
            model_stack, rules, transfer_rules, horizon=_HORIZON_BY_ARM[arm_name], config=config
        )
        counting = _TransferCountingStrategy(horizon_strategy, record_decisions=decision_log_path is not None)
        t0 = time.time()
        # S10, D4: BOTH arms stateful, identical initial ledger.
        results = SeasonReplay(season_data, rules).run(counting, initial_state=free_build_state(rules))
        decide_wall_seconds = time.time() - t0
        summary = _summarise_gameweek_results(results)
        arms[arm_name] = {
            "decide_wall_seconds": decide_wall_seconds,
            "total_points": summary["total_points"],
            "hit_points": summary["hit_points"],
            "n_transfers": counting.n_transfers,
        }

        # S10a, D1: `counting.decisions` and `results` are the SAME length,
        # SAME order (one per round, both produced by the `run` call
        # above) -- `zip` pairs them without needing the round number from
        # either object directly.
        if decision_log_path is not None:
            rounds = season_data.rounds()
            with decision_log_path.open("a", encoding="utf-8") as f:
                for gameweek, decision, result in zip(rounds, counting.decisions, results):
                    record = _build_decision_log_record(
                        season=season, arm=arm_name, gameweek=gameweek, decision=decision, result=result
                    )
                    f.write(json.dumps(record) + "\n")
                    f.flush()

    return _build_season_record(
        season=season,
        generated_at=datetime.now(timezone.utc).isoformat(),
        seed=seed,
        git_commit=git_commit,
        smoke_test=limit_gameweeks is not None,
        limit_gameweeks=limit_gameweeks,
        n_gameweeks=len(season_data.rounds()),
        dc_neutralised=needs_dc_neutralisation,
        dc_cold_start_gameweeks=cold_start_gameweeks,
        fit_wall_seconds=fit_wall_seconds,
        arms=arms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--seasons",
        type=str,
        default=",".join(GATE_SEASONS),
        help=f"Comma-separated seasons (default: {','.join(GATE_SEASONS)}, the real gate's three sourced "
        "stateful-mode seasons -- an unsourced season raises rather than degrading, S10 D4).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(_REPO_ROOT / "data" / "gate" / "e7_gate_results.jsonl"),
        help="JSONL results file, appended incrementally, resumable by re-running the same command",
    )
    parser.add_argument("--seed", type=int, default=0, help="OptimiserConfig.random_seed, shared by both arms (default 0)")
    parser.add_argument(
        "--limit-gameweeks",
        type=int,
        default=None,
        help=(
            "SMOKE-TEST AID ONLY. Truncates each requested season to its earliest N gameweeks. "
            "Records are marked smoke_test=true. NEVER use this for the gate result -- it changes "
            "which weeks run, not how faithfully any one week is computed, but a partial season's "
            "totals are not comparable to a full season's and must never be read as a gate outcome."
        ),
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print the H6-vs-H1 comparison table from an existing --out file WITHOUT running anything.",
    )
    parser.add_argument(
        "--decision-log",
        type=str,
        default=None,
        help=(
            "S10a, D1. OPT-IN. Append one JSONL line per (season, arm, gameweek) -- transfers_in/out, "
            "hit_points, points, resulting squad element ids -- as each arm's season completes. Default "
            "None leaves this script's behaviour byte-for-byte unchanged. Read by "
            "scripts/analyse_transfer_churn.py. Never point this at data/gate/ or docs/wiki/."
        ),
    )
    args = parser.parse_args()
    out_path = Path(args.out)
    decision_log_path = Path(args.decision_log) if args.decision_log else None
    if decision_log_path is not None:
        decision_log_path.parent.mkdir(parents=True, exist_ok=True)

    if args.report_only:
        print(_render_report_table(_read_records(out_path)))
        return 0

    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.limit_gameweeks is not None:
        print(
            f"WARNING: --limit-gameweeks={args.limit_gameweeks} is a SMOKE-TEST AID. "
            "Records written this run are marked smoke_test=true and must NEVER be read as a gate result."
        )

    store = BitemporalStore()
    commit = run_e6_gate._git_commit(_REPO_ROOT)
    scoring_config_base = load_scoring_config(store, datetime.now(timezone.utc))
    shared_dc_cache: dict = {}

    already_done = run_e6_gate._load_completed_seasons(out_path)
    if already_done:
        print(f"Resuming against {out_path}: already present, will be SKIPPED: {sorted(already_done)}")

    print(f"E7 gate runner (H6 vs H1, stateful) -- seed={args.seed}, commit={commit}, out={out_path}")
    print()

    for season in seasons:
        if season in already_done:
            print(f"{season}: SKIPPED (already in {out_path})")
            continue
        try:
            t0 = time.time()
            record = run_season(
                store,
                season,
                seed=args.seed,
                limit_gameweeks=args.limit_gameweeks,
                scoring_config_base=scoring_config_base,
                shared_dc_cache=shared_dc_cache,
                git_commit=commit,
                decision_log_path=decision_log_path,
            )
        except SeasonDataError as exc:
            print(f"{season}: SKIPPED -- {exc}")
            continue
        except OptimiserError as exc:
            print(f"{season}: FAILED -- {exc}")
            raise

        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        wall = time.time() - t0
        h6, h1 = record["arms"]["h6"], record["arms"]["h1"]
        print(
            f"{season}: wrote result to {out_path} (wall {wall:.1f}s, dc_neutralised={record['dc_neutralised']}, "
            f"dc_cold_start_gameweeks={record['dc_cold_start_gameweeks']})"
        )
        print(
            f"  h6 total={h6['total_points']:5d} hits={h6['hit_points']:4d} xfers={h6['n_transfers']:3d} "
            f"decide_wall={h6['decide_wall_seconds']:.1f}s"
        )
        print(
            f"  h1 total={h1['total_points']:5d} hits={h1['hit_points']:4d} xfers={h1['n_transfers']:3d} "
            f"decide_wall={h1['decide_wall_seconds']:.1f}s"
        )
        print(f"  h6_beats_h1: {record['h6_beats_h1']}")
        print()

    print("=== Summary (every completed season in the results file, including prior runs) ===")
    print(_render_report_table(_read_records(out_path)))
    print()
    print("This script prints totals; it does not itself declare the gate passed or failed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
