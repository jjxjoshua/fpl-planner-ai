#!/usr/bin/env python
"""E6 gate runner -- runs `fplai.optimiser.ModelStackStrategy` (the real
seven-model composition) against the real store across every usable
historical season, alongside two controls (`MILPStrategy`, the trailing-
empirical-points proxy; `TemplateBaseline`), and records the result.
Blueprint SS6.1/SS7.2, CLAUDE.md rules 2/4/5/7. Session `s006`, task
`e6-gate-runner`.

**This script is the harness. It does not decide whether the gate
passes.** `docs/HANDOFF.md` SS3.5/SS3.6 names the gate itself: beat the
template over 2+ backtested seasons (~2,100 points, ideally the 2,146
career average). Read the printed summary and the results file; nothing
here emits a verdict string.

## Usage

    # The real gate -- ALL FOUR default seasons, no --limit-gameweeks:
    uv run python scripts/run_e6_gate.py

    # A specific subset:
    uv run python scripts/run_e6_gate.py --seasons 2024-25,2025-26

    # Smoke-test aid ONLY -- see the flag's own help text:
    uv run python scripts/run_e6_gate.py --seasons 2025-26 --limit-gameweeks 2

## Resumability (pinned decision 1)

Results are written to `--out` (JSONL, default
`docs/wiki/e6-gate-results.jsonl`) INCREMENTALLY -- one line appended the
moment a season finishes, flushed to disk immediately. **To resume an
interrupted run, run the exact same command again.** On start, this
script reads every `season` value already present in `--out` and skips
recomputing any season that appears there, regardless of what
`--limit-gameweeks`/`--seed` that existing line was produced with -- the
skip key is `season` alone, deliberately simple, per this task's brief
("skip seasons already present in the results file"). This means: if you
need to re-run a season with different settings (a different seed, or
graduating a smoke-tested season to the real gate), either delete that
season's line from `--out` by hand first, or point `--out` at a fresh
file. Do not "fix" this by making the skip key smarter -- the brief is
explicit that resumability exists so a ~3-hour run survives an
interruption, not so two differently-configured runs can interleave in
one file.

## The DC-neutralisation design decision (pinned decision 3, resolved)

`defensive_contribution` (the CBIT/CBIRT column vaastav carries) is
non-null ONLY for 2025-26 in the real store (verified live this task:
2022-23/2023-24/2024-25 each have 0 non-null rows out of 26505/29725/27283;
2025-26 has 29757/29757). `fplai.models.defensive_contribution.
fit_dc_model` filters to non-null `defensive_contribution` rows and RAISES
if that leaves zero rows (`DCModelError("no player gameweek stats..."`) --
so for a walk-forward `as_of` inside any of 2022-23/2023-24/2024-25, no
non-leaking `as_of` cutoff can EVER see DC training data, because none
exists at any earlier date for those seasons; this is a structural, not a
temporary, absence.

Per the pinned decision, those seasons' `ScoringConfig.
defensive_contribution` is zeroed for every position (`dataclasses.
replace`), so a candidate's DC prediction contributes exactly `dc_met *
0 == 0` to its predicted points in `simulate_fixture_points_pmfs`
regardless of what the DC model predicts -- proven by construction, not
merely argued: the point value IS the multiplier, and it is zero.

Given that, this script fits ONE real `DCModelParams` (`as_of=datetime.
now(timezone.utc)`, which resolves against the ONLY season with real DC
data, 2025-26 -- 2.97s live-verified this task) and REUSES it, unmodified,
for every gameweek of every neutralised season. This is a judgment call
this task's brief did not spell out (it named the ScoringConfig zeroing,
not what to feed `fit_dc_model` for a season that cannot fit one at all),
made because the alternative -- hand-fabricating a `DCGroupParams` with
invented regression coefficients -- would be a rule-5/rule-1-adjacent
fabrication this codebase does not otherwise permit, whereas reusing a
REAL fit whose output is PROVABLY inert on every neutralised season's
predicted points is a bounded, documented substitution, not a silent one.
Flagged to the Architect as a finding, not smuggled in.

**Correction after Architect review, session s006: this reused bundle
applies to WHOLE-SEASON neutralisation ONLY, never to a single
in-season cold-start gameweek.** The first version of this script also
used it as the fallback for 2025-26's own gameweek 1 (see
`_fit_params_by_gameweek`'s docstring for that separate case) -- WRONG,
because 2025-26's `ScoringConfig` is NOT zeroed, so feeding a
future-fitted (`as_of=now()`) DC model into a live, non-zeroed scoring
path is genuine leakage under CLAUDE.md rule 2 ("does not crash, it
silently invalidates everything downstream"), not the provably-inert
substitution this section describes. The in-season cold-start case now
uses `_degenerate_dc_bundle` instead -- fits nothing, reads nothing, and
forces DC ineligible (contributing zero) for that one gameweek by
construction, leaving the season's real `ScoringConfig` untouched. Only
the WHOLE-SEASON case above (2022-23/2023-24/2024-25) still uses this
function, and only because the zeroed `ScoringConfig` is what makes its
`as_of=now()` leakage provably harmless there.

## What is NOT exposed (pinned decision 6)

No `n_simulations`/fidelity knob. `--limit-gameweeks` truncates WHICH
gameweeks run, never HOW faithfully each one is computed -- it exists so
this script can be smoke-tested in seconds, not so the real gate run can
be made cheaper. Every record this script writes when `--limit-gameweeks`
is set carries `"smoke_test": true` so a reader of the results file
cannot mistake a truncated run for a real gate season.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import polars as pl  # noqa: E402

from fplai.backtest.baselines import TemplateBaseline  # noqa: E402
from fplai.backtest.data import SeasonCoverage, SeasonData, SeasonDataError, load_season  # noqa: E402
from fplai.backtest.replay import SeasonReplay, _build_view  # noqa: E402
from fplai.backtest.report import SeasonSummary, summarise  # noqa: E402
from fplai.backtest.rules import rules_for_season  # noqa: E402
from fplai.features import FixtureFeatureAssemblyParams  # noqa: E402
from fplai.models.attacking import fit_attacking_model  # noqa: E402
from fplai.models.bonus import fit_bonus_model  # noqa: E402
from fplai.models.cards import fit_cards_model  # noqa: E402
from fplai.models.defensive_contribution import (  # noqa: E402
    DCModelError,
    DCModelParams,
    DCThresholdSet,
    build_dc_threshold_set,
    fit_dc_model,
)
from fplai.models.minutes import fit_minutes_model  # noqa: E402
from fplai.models.saves import fit_saves_model  # noqa: E402
from fplai.models.team_strength import fit_team_strength  # noqa: E402
from fplai.optimiser import (  # noqa: E402
    MILPStrategy,
    ModelStackParams,
    ModelStackStrategy,
    OptimiserConfig,
    OptimiserError,
    _parse_kickoff_time,
)
from fplai.scoring import ScoringConfig, load_scoring_config  # noqa: E402
from fplai.store import BitemporalStore  # noqa: E402

# The four seasons this task's brief names for the real gate. Declared
# independently rather than imported from scripts/run_optimiser.py (this
# codebase's own "house vocabulary, declared independently" convention --
# see fplai.optimiser's module docstring, POSITIONS) so this script has no
# import-time coupling to another script's own constant list.
GATE_SEASONS = ("2022-23", "2023-24", "2024-25", "2025-26")

# The gate's own stated bar (docs/HANDOFF.md SS2/SS3.5): "~2,100 points,
# ideally the 2,146 career average." Reported alongside the template
# comparison, never substituted for it -- this script prints it, it does
# not gate on it.
GATE_BAR = 2100


def _git_commit(repo_root: Path) -> str:
    """Best-effort git commit hash for provenance (pinned decision 5,
    CLAUDE.md rule 7). Never raises -- a missing/broken git binary must
    not crash a multi-hour run; the result file records the failure
    string instead."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        return f"UNKNOWN ({exc})"


def _needs_dc_neutralisation(season_data: SeasonData) -> bool:
    """Derived from the data, never a hardcoded season list (pinned
    decision 3, explicit). A season with the column absent entirely, or
    present but wholly null, cannot train a DC model at any leakage-safe
    `as_of` inside it."""
    if "defensive_contribution" not in season_data.frame.columns:
        return True
    return season_data.frame["defensive_contribution"].drop_nulls().len() == 0


def _neutralise_dc(scoring_config: ScoringConfig) -> ScoringConfig:
    """Zero every position's `defensive_contribution` point value via
    `dataclasses.replace` (pinned decision 3, verbatim)."""
    zeroed = {position: 0 for position in scoring_config.defensive_contribution}
    return dataclasses.replace(scoring_config, defensive_contribution=zeroed)


def _fit_shared_dc_bundle(store: BitemporalStore) -> tuple[DCThresholdSet, DCModelParams]:
    """Fit the ONE real `DCModelParams` this script reuses across every
    DC-neutralised WHOLE SEASON -- see module docstring, "The
    DC-neutralisation design decision". `as_of=now` is the only `as_of`
    in the entire store that can see the 2025-26 DC training data at
    all. Safe ONLY where the caller has also zeroed that season's
    `ScoringConfig.defensive_contribution` (`_neutralise_dc`) -- the
    zeroed points value is what makes this fit's own leakage
    (`as_of=now` sees the whole of 2025-26 and 2026-27) provably inert on
    predicted points. **Never use this for a single-gameweek cold-start
    inside a season whose scoring is NOT neutralised** -- that is a
    different case with a different, leakage-free fix; see
    `_degenerate_dc_bundle` and `_fit_params_by_gameweek`'s own
    docstring (Architect correction, this task, session s006: the
    original version of this module reused THIS function for the
    cold-start case too, which fed real future-fitted DC params into a
    live, non-zeroed scoring path -- genuine leakage, not merely
    style)."""
    as_of = datetime.now(timezone.utc)
    threshold_set = build_dc_threshold_set(store, as_of=as_of)
    dc_params = fit_dc_model(store, as_of=as_of, threshold_set=threshold_set)
    return threshold_set, dc_params


# Declared independently rather than imported from fplai.optimiser -- this
# codebase's own "house vocabulary, declared independently" convention
# (fplai.optimiser's own module docstring, POSITIONS).
_POSITIONS: tuple[str, ...] = ("GK", "DEF", "MID", "FWD")


def _degenerate_dc_bundle(as_of: datetime) -> tuple[DCThresholdSet, DCModelParams]:
    """A synthetic, PROVABLY ZERO-leakage stand-in for a single gameweek
    where `fit_dc_model` cannot be fit leakage-free -- e.g. a season's
    own gameweek 1, whose `as_of` (that round's earliest kickoff)
    necessarily precedes every one of that gameweek's own rows, and
    which therefore cannot see ANY prior DC data if it also happens to
    be the first gameweek DC exists anywhere in the store (verified
    live, 2025-26 GW1).

    Reads no data and fits nothing -- there is nothing here that COULD
    leak, unlike `_fit_shared_dc_bundle` (which is real, fitted, and
    correctly scoped to whole-season neutralisation only, never this
    case -- see that function's own docstring). Built entirely by
    reusing `fplai.models.defensive_contribution.predict_dc_pmf`'s own
    EXISTING, already-tested ineligibility short-circuit (that module's
    own docstring: "A position outside `params.threshold_set.
    points_by_position` (or with zero points there -- GKP) returns a
    DEGENERATE PMF (`eligible=False`, spike at count=0,
    `p_dc_awarded()==0` structurally, not by convention) rather than
    raising") -- `points_by_position` here is zero for EVERY position,
    not just GKP, so `DCThresholdSet.is_eligible(position)` is False for
    every player this gameweek. `predict_dc_pmf` checks eligibility and
    returns the degenerate PMF BEFORE it ever reads `params.groups`, so
    `groups={}` is safe (verified by reading the source: `defensive_
    contribution.py`'s `predict_dc_pmf`, the `is_eligible` check
    precedes the `group not in params.groups` check by construction).
    `fplai.features.assemble_fixture_player_features_from_frame` applies
    the identical `threshold_set.is_eligible(position)` gate BEFORE
    calling the feature assembler at all (verified by reading the
    source, `features.py:1969`/`2060`), so `dc_feature_row` comes back
    `{}` for every player too -- the exact same code path this codebase
    already uses for a goalkeeper (module docstring of `fplai.features`,
    "a GK's dc_feature_row is `{}`"), just extended to every position
    for one gameweek instead of one position for every gameweek. Net
    effect end to end: `points.py`'s `dc_threshold` is `None` for every
    player this gameweek, so `dc_met_arr` is `False` for all of them,
    and `score_outcome` never adds a DC term for anyone -- DC
    contributes exactly zero, by construction, with the season's real
    `ScoringConfig` left completely untouched."""
    threshold_set = DCThresholdSet(
        count_thresholds={},  # never read -- is_eligible fails first for every position, see docstring
        points_by_position={position: 0 for position in _POSITIONS},
    )
    dc_params = DCModelParams(groups={}, threshold_set=threshold_set, as_of=as_of, seasons_used=())
    return threshold_set, dc_params


def _truncate_season(season_data: SeasonData, limit_gameweeks: int | None) -> SeasonData:
    """Smoke-test aid only (pinned decision 6) -- keeps only the
    EARLIEST `limit_gameweeks` rounds. Truncating from the tail never
    changes the leakage boundary for any round that survives: `round <
    t` for a surviving `t` is identical whether or not later rounds were
    ever loaded, because `fplai.backtest.replay.rows_before_round` only
    ever looks backward."""
    if limit_gameweeks is None:
        return season_data
    keep_rounds = sorted(season_data.coverage.rounds_present)[:limit_gameweeks]
    frame = season_data.frame.filter(pl.col("round").is_in(keep_rounds))
    coverage = SeasonCoverage(
        season=season_data.season,
        rounds_present=tuple(keep_rounds),
        expected_gameweeks=season_data.coverage.expected_gameweeks,
        missing_rounds=season_data.coverage.missing_rounds,
    )
    return SeasonData(season=season_data.season, frame=frame, coverage=coverage)


def _fit_params_by_gameweek(
    store: BitemporalStore,
    season_data: SeasonData,
    *,
    needs_dc_neutralisation: bool,
    get_shared_dc,
) -> tuple[dict[int, ModelStackParams], float, list[int]]:
    """Refit every gameweek, as-of that gameweek's own deadline (pinned
    decision 2, the user's ruling). `as_of` is the EARLIEST kickoff of
    that round's own fixtures -- the same convention `tests/
    test_optimiser.py`'s real-store test already established and this
    task's brief points to directly.

    **A second, narrower cold-start case, found live running this
    script's own smoke test (not anticipated by the brief), and
    CORRECTED after Architect review (session s006).** Even in a season
    whose `ScoringConfig` is NOT neutralised (2025-26, which DOES have DC
    data), that season's OWN gameweek 1 fails: `as_of` = GW1's earliest
    kickoff excludes every one of GW1's own rows by definition, and GW1
    is the single earliest gameweek DC data exists ANYWHERE in the store
    -- so there is no leakage-safe `as_of` that can see any DC row at
    all for it (verified live: GW1 raises `DCModelError`, GW2 onward fit
    cleanly off GW1's now-settled rows).

    **This gameweek falls back to `_degenerate_dc_bundle(as_of)`, NOT
    `get_shared_dc()`.** The first version of this function used
    `get_shared_dc()` here too -- WRONG, per Architect review: that
    bundle is fitted at `as_of=now()`, which has seen the whole of
    2025-26 and 2026-27, so feeding it into a gameweek whose
    `ScoringConfig` is still LIVE (not zeroed) is genuine leakage under
    CLAUDE.md rule 2, not the provably-inert substitution the
    whole-season case gets. `_degenerate_dc_bundle` fits nothing and
    reads nothing -- it forces DC ineligible for everyone that one
    gameweek via `predict_dc_pmf`'s own existing "GKP-style" degenerate
    path (see that function's own docstring), so DC contributes zero
    WITHOUT touching the season's real `ScoringConfig` and without any
    dependency on data at or after this gameweek's own deadline.
    `cold_start_gameweeks` records which gameweek(s) got this treatment
    so the result file carries its own caveat -- a documented, BOUNDED
    exception (one gameweek out of 152 in the full 4-season gate, and
    `fplai.optimiser` strategies are re-decided independently every
    gameweek with no squad-continuity between weeks, so even a fidelity
    loss here cannot propagate to any OTHER gameweek's decision), never
    a silent one."""
    params_by_gameweek: dict[int, ModelStackParams] = {}
    fit_wall_seconds = 0.0
    cold_start_gameweeks: list[int] = []
    for gameweek in season_data.rounds():
        view = _build_view(season_data, gameweek)
        if view.fixtures.is_empty():
            # No fixtures at all this round in the (possibly truncated)
            # season -- nothing to fit or decide for it. Should not occur
            # for a round `SeasonData.rounds()` reports present, but
            # guarded rather than assumed.
            continue
        as_of = min(_parse_kickoff_time(k) for k in view.fixtures["kickoff_time"].to_list())

        t0 = time.time()
        minutes_params = fit_minutes_model(store, as_of=as_of)
        attacking_params = fit_attacking_model(store, as_of=as_of)
        cards_params = fit_cards_model(store, as_of=as_of)
        bonus_params = fit_bonus_model(store, as_of=as_of)
        saves_params = fit_saves_model(store, as_of=as_of)
        # `teams=` explicit, derived from THIS gameweek's own current
        # attributes (never hardcoded, CLAUDE.md rule 4) -- found live
        # running this script's own smoke test: fit_team_strength's own
        # default team universe is "every team observed in the training
        # window", which silently excludes a newly-promoted club (e.g.
        # Sunderland, 2025-26) that has no PRIOR-season match history,
        # and `predict_scoreline` then raises rather than guess. Passing
        # the round's own roster of teams lets a team with no history get
        # the documented promoted-team prior instead of being dropped.
        teams = sorted(view.current_attributes["team"].unique().to_list())
        team_params = fit_team_strength(store, as_of=as_of, teams=teams)
        if needs_dc_neutralisation:
            threshold_set, dc_params = get_shared_dc()
        else:
            try:
                threshold_set = build_dc_threshold_set(store, as_of=as_of)
                dc_params = fit_dc_model(store, as_of=as_of, threshold_set=threshold_set)
            except DCModelError as exc:
                print(
                    f"  WARNING: gameweek {gameweek} DC fit failed ({exc}) -- DC forced ineligible "
                    "(contributes zero) for THIS gameweek only, via _degenerate_dc_bundle -- NOT the "
                    "shared future-fitted bundle. See _fit_params_by_gameweek's own docstring."
                )
                threshold_set, dc_params = _degenerate_dc_bundle(as_of)
                cold_start_gameweeks.append(gameweek)
        fit_wall_seconds += time.time() - t0

        params_by_gameweek[gameweek] = ModelStackParams(
            team_strength_params=team_params,
            minutes_params=minutes_params,
            attacking_params=attacking_params,
            dc_params=dc_params,
            cards_params=cards_params,
            bonus_params=bonus_params,
            saves_params=saves_params,
            assembly_params=FixtureFeatureAssemblyParams(threshold_set=threshold_set),
        )
    return params_by_gameweek, fit_wall_seconds, cold_start_gameweeks


def _load_completed_seasons(out_path: Path) -> set[str]:
    """Every `season` value already present in `out_path` (pinned
    decision 1). A malformed line is skipped, never fatal -- an
    interrupted `write` mid-line must not block resuming past it."""
    if not out_path.exists():
        return set()
    seasons: set[str] = set()
    with out_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            season = obj.get("season")
            if isinstance(season, str):
                seasons.add(season)
    return seasons


def _summary_dict(summary: SeasonSummary) -> dict:
    return dataclasses.asdict(summary)


def run_season(
    store: BitemporalStore,
    season: str,
    *,
    seed: int,
    limit_gameweeks: int | None,
    scoring_config_base: ScoringConfig,
    shared_dc_cache: dict[str, tuple[DCThresholdSet, DCModelParams]],
    git_commit: str,
) -> dict:
    """Fit, decide, and score all three strategies for one season, and
    return the JSON-serialisable record this script appends to `--out`.
    Raises `SeasonDataError`/`OptimiserError` outward -- a season this
    script cannot honestly complete is never silently recorded as a
    result."""
    season_data_full = load_season(store, season)
    season_data = _truncate_season(season_data_full, limit_gameweeks)
    rules = rules_for_season(season)

    needs_dc_neutralisation = _needs_dc_neutralisation(season_data)
    scoring_config = _neutralise_dc(scoring_config_base) if needs_dc_neutralisation else scoring_config_base

    def get_shared_dc() -> tuple[DCThresholdSet, DCModelParams]:
        # Lazy + cached across the whole run (module docstring, "The
        # DC-neutralisation design decision") -- fit at most ONCE total,
        # reused by every neutralised season AND every single-gameweek
        # cold-start fallback (`_fit_params_by_gameweek`'s own docstring).
        if "bundle" not in shared_dc_cache:
            shared_dc_cache["bundle"] = _fit_shared_dc_bundle(store)
        return shared_dc_cache["bundle"]

    params_by_gameweek, fit_wall_seconds, cold_start_gameweeks = _fit_params_by_gameweek(
        store, season_data, needs_dc_neutralisation=needs_dc_neutralisation, get_shared_dc=get_shared_dc
    )

    config = OptimiserConfig(random_seed=seed)
    strategies = {
        "model_stack": ModelStackStrategy(
            rules=rules, params_by_gameweek=params_by_gameweek, scoring_config=scoring_config, config=config
        ),
        "milp": MILPStrategy(rules=rules, config=config),
        "template": TemplateBaseline(rules=rules),
    }

    summaries: dict[str, SeasonSummary] = {}
    decide_wall_seconds: dict[str, float] = {}
    for name, strategy in strategies.items():
        t0 = time.time()
        gw_results = SeasonReplay(season_data, rules).run(strategy)
        decide_wall_seconds[name] = time.time() - t0
        summaries[name] = summarise(season, name, gw_results)

    return {
        "season": season,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "git_commit": git_commit,
        "smoke_test": limit_gameweeks is not None,
        "limit_gameweeks": limit_gameweeks,
        "n_gameweeks": season_data.coverage.rounds_present.__len__(),
        "dc_neutralised": needs_dc_neutralisation,
        "dc_cold_start_gameweeks": cold_start_gameweeks,
        "fit_wall_seconds": fit_wall_seconds,
        "decide_wall_seconds": decide_wall_seconds,
        "strategies": {name: _summary_dict(s) for name, s in summaries.items()},
        "model_stack_beats_template": summaries["model_stack"].total_points > summaries["template"].total_points,
        "milp_beats_template": summaries["milp"].total_points > summaries["template"].total_points,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--seasons",
        type=str,
        default=",".join(GATE_SEASONS),
        help=f"Comma-separated seasons (default: {','.join(GATE_SEASONS)}, the real gate's four seasons)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(Path(__file__).resolve().parents[1] / "docs" / "wiki" / "e6-gate-results.jsonl"),
        help="JSONL results file, appended incrementally, resumable by re-running the same command",
    )
    parser.add_argument("--seed", type=int, default=0, help="OptimiserConfig.random_seed (default 0)")
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
    args = parser.parse_args()
    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parents[1]

    if args.limit_gameweeks is not None:
        print(
            f"WARNING: --limit-gameweeks={args.limit_gameweeks} is a SMOKE-TEST AID. "
            "Records written this run are marked smoke_test=true and must NEVER be read as a gate result."
        )

    store = BitemporalStore()
    commit = _git_commit(repo_root)
    scoring_config_base = load_scoring_config(store, datetime.now(timezone.utc))
    shared_dc_cache: dict[str, tuple[DCThresholdSet, DCModelParams]] = {}

    already_done = _load_completed_seasons(out_path)
    if already_done:
        print(f"Resuming against {out_path}: already present, will be SKIPPED: {sorted(already_done)}")

    print(f"E6 gate runner -- seed={args.seed}, commit={commit}, out={out_path}")
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
        print(
            f"{season}: wrote result to {out_path} (wall {wall:.1f}s, dc_neutralised={record['dc_neutralised']}, "
            f"dc_cold_start_gameweeks={record['dc_cold_start_gameweeks']})"
        )
        for name in ("model_stack", "milp", "template"):
            s = record["strategies"][name]
            print(
                f"  {name:12} total={s['total_points']:5d} mean={s['mean']:.1f} "
                f"median={s['median']:.1f} stdev={s['stdev']:.1f}"
            )
        print(
            f"  model_stack beats template: {record['model_stack_beats_template']}   "
            f"milp beats template: {record['milp_beats_template']}"
        )
        print()

    print("=== Summary (every completed season in the results file, including prior runs) ===")
    all_records: list[dict] = []
    if out_path.exists():
        with out_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    all_records.append(json.loads(line))
    header = f"{'season':10} {'model_stack':>12} {'milp':>7} {'template':>9} {'beats_tmpl':>11} {'smoke':>6}"
    print(header)
    for r in sorted(all_records, key=lambda r: r["season"]):
        ms = r["strategies"]["model_stack"]["total_points"]
        milp = r["strategies"]["milp"]["total_points"]
        tmpl = r["strategies"]["template"]["total_points"]
        print(f"{r['season']:10} {ms:12d} {milp:7d} {tmpl:9d} {str(r['model_stack_beats_template']):>11} {str(r['smoke_test']):>6}")
    print(f"\nGate bar (docs/HANDOFF.md SS3.5): ~{GATE_BAR} points per season, ideally the 2,146 career average.")
    print("This script prints totals; it does not itself declare the gate passed or failed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
