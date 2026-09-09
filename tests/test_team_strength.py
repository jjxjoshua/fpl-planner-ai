"""Tests for fplai.models.team_strength — Phase 2, E5 (blueprint §4, §4.2,
§4.3, §12.2).

Three groups, matching this session's standing rules:
  1. Pure math / determinism / synthetic parameter recovery — no store.
  2. Bitemporal leakage — a temp store with fabricated rows, including an
     adversarial attempt to smuggle a future match past `as_of` by a route
     the public API does not sanction (backdating `observed_at`, and the
     exact-boundary case).
  3. Real-store-gated sanity + the derived-capability write path — skipped
     if `data/store/` is absent, matching `tests/test_derived.py`'s own
     `requires_real_store` pattern. Read-only against `data/store/`; all
     `write_team_strength` calls in this file target an isolated `tmp_path`
     store, never the real one.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from fplai.models.team_strength import (
    MatchRecord,
    ScorelinePMF,
    TeamStrengthConfig,
    TeamStrengthError,
    _dc_tau,
    _fit_rho,
    _poisson_pmf,
    build_match_table,
    fit_team_strength,
    params_to_rows,
    predict_scoreline,
    shrink_to_odds,
    write_team_strength,
)
from fplai.identity import TeamNameCanonicalisationMap
from fplai.schemas import CANONICAL_SCHEMAS, DATASET_ENTITY_KEYS, TEAM_STRENGTH_RATING_GAMEWEEK
from fplai.store import BitemporalError, BitemporalStore

UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Pure math: _dc_tau, _poisson_pmf, ScorelinePMF, determinism, synthetic
#    parameter recovery. No store involved.
# ---------------------------------------------------------------------------


def test_dc_tau_matches_the_dixon_coles_formula_exactly():
    lam, mu, rho = 1.3, 0.9, 0.1
    assert _dc_tau(0, 0, lam, mu, rho) == pytest.approx(1 - lam * mu * rho)
    assert _dc_tau(0, 1, lam, mu, rho) == pytest.approx(1 + lam * rho)
    assert _dc_tau(1, 0, lam, mu, rho) == pytest.approx(1 + mu * rho)
    assert _dc_tau(1, 1, lam, mu, rho) == pytest.approx(1 - rho)
    # Every other scoreline: no correction, regardless of rho.
    for h, a in [(0, 2), (2, 0), (2, 2), (3, 1)]:
        assert _dc_tau(h, a, lam, mu, rho) == 1.0


def test_dc_tau_rho_zero_is_the_identity_correction():
    for h, a in [(0, 0), (0, 1), (1, 0), (1, 1), (2, 2)]:
        assert _dc_tau(h, a, 1.1, 0.8, 0.0) == 1.0


def test_poisson_pmf_sums_to_one_over_a_wide_enough_range():
    lam = 1.7
    total = sum(_poisson_pmf(k, lam) for k in range(60))
    assert total == pytest.approx(1.0, abs=1e-9)


def test_poisson_pmf_matches_hand_computed_values():
    # P(0; lambda) = exp(-lambda); P(1; lambda) = lambda * exp(-lambda).
    lam = 1.4
    assert _poisson_pmf(0, lam) == pytest.approx(math.exp(-lam))
    assert _poisson_pmf(1, lam) == pytest.approx(lam * math.exp(-lam))


def _uniform_grid(max_goals: int, home_team: str = "H", away_team: str = "A") -> ScorelinePMF:
    n = max_goals + 1
    p = 1.0 / (n * n)
    grid = tuple(tuple(p for _ in range(n)) for _ in range(n))
    return ScorelinePMF(home_team=home_team, away_team=away_team, grid=grid, mass_before_truncation=1.0)


def test_scoreline_pmf_sums_to_one_and_is_nonnegative():
    pmf = _uniform_grid(4)
    total = sum(pmf.prob(h, a) for h in range(5) for a in range(5))
    assert total == pytest.approx(1.0)
    assert all(pmf.prob(h, a) >= 0 for h in range(5) for a in range(5))


def test_scoreline_pmf_marginals_and_expectations_on_a_known_grid():
    # A degenerate PMF: certainty of a 2-1 scoreline.
    n = 4
    grid = [[0.0] * n for _ in range(n)]
    grid[2][1] = 1.0
    pmf = ScorelinePMF(home_team="H", away_team="A", grid=tuple(tuple(r) for r in grid), mass_before_truncation=1.0)
    assert pmf.most_likely_scoreline() == (2, 1)
    assert pmf.expected_home_goals() == pytest.approx(2.0)
    assert pmf.expected_away_goals() == pytest.approx(1.0)
    assert pmf.home_win == pytest.approx(1.0)
    assert pmf.draw == pytest.approx(0.0)
    assert pmf.away_win == pytest.approx(0.0)


def test_scoreline_pmf_to_polars_is_long_format_and_sums_to_one():
    pmf = _uniform_grid(2)
    df = pmf.to_polars()
    assert set(df.columns) == {"home_team", "away_team", "home_goals", "away_goals", "probability"}
    assert df.height == 9  # (max_goals+1)^2
    assert df["probability"].sum() == pytest.approx(1.0)


def test_predict_scoreline_rejects_an_unknown_team():
    matches = _known_matches_for_recovery()
    params = fit_team_strength_from_matches(matches, teams=["Strong", "Mid", "Weak"])
    with pytest.raises(TeamStrengthError):
        predict_scoreline(params, "Strong", "NeverSeen")


def test_shrink_to_odds_is_the_documented_unimplemented_seam():
    pmf = _uniform_grid(3)
    with pytest.raises(NotImplementedError):
        shrink_to_odds(pmf, market_home_win=0.5, market_draw=0.25, market_away_win=0.25, weight=0.3)


# -- synthetic parameter recovery -------------------------------------------


def _known_matches_for_recovery() -> tuple[MatchRecord, ...]:
    """Three teams with KNOWN ground-truth log-scale attack/defence and a
    known home advantage, xG target set EXACTLY to the ground-truth
    lambda/mu (no noise), repeated across many fixtures so the fit's L2
    regularisation has negligible relative pull. If `fit_team_strength`'s
    MLE is implemented correctly, it must recover the correct RANKING
    (Strong > Mid > Weak on attack; Strong best defence) and land close to
    the true home-advantage value."""
    true_attack = {"Strong": 0.6, "Mid": 0.0, "Weak": -0.6}
    true_defence = {"Strong": -0.4, "Mid": 0.0, "Weak": 0.4}
    true_home_adv = 0.2

    records: list[MatchRecord] = []
    fixture_id = 0
    base = datetime(2020, 1, 1)
    teams = ["Strong", "Mid", "Weak"]
    for repeat in range(25):
        for home in teams:
            for away in teams:
                if home == away:
                    continue
                fixture_id += 1
                lam = math.exp(true_attack[home] + true_defence[away] + true_home_adv)
                mu = math.exp(true_attack[away] + true_defence[home])
                records.append(
                    MatchRecord(
                        season="synthetic",
                        fixture=fixture_id,
                        kickoff=base.replace(year=2020 + repeat % 5, month=1 + (repeat % 12)),
                        home_team=home,
                        away_team=away,
                        home_goals=round(lam),
                        away_goals=round(mu),
                        home_xg=lam,
                        away_xg=mu,
                    )
                )
    return tuple(records)


def fit_team_strength_from_matches(matches, *, teams=None, config: TeamStrengthConfig | None = None):
    """Test-only helper: runs the same fitting internals `fit_team_strength`
    runs, but against an in-memory match list instead of a store — keeps
    the synthetic-recovery tests from needing a store at all, while still
    exercising the exact same `_fit_attack_defence_home`/`_fit_rho`
    functions the store-driven path uses."""
    from fplai.models.team_strength import _fit_attack_defence_home, _fit_rho

    cfg = config or TeamStrengthConfig(n_adam_iterations=300)
    # Deliberately NOT max(kickoff).replace(year=9999): that was tried
    # first and produced a fit stuck exactly at its 0.0 initial
    # parameters -- a real bug, caught by this test, not a hypothetical
    # one. A ~2.9-million-day gap makes exp(-ln2*days/half_life) underflow
    # to a literal 0.0 in every match's weight, so every gradient term is
    # exactly zero and Adam never moves. `fit_team_strength` now raises
    # TeamStrengthError on a near-zero total weight instead of silently
    # returning that no-op result -- see its 'total_weight <= 1e-9' guard.
    # This helper avoids the same footgun by anchoring as_of just after
    # the last match, matching how a real caller would use it.
    as_of_naive = max(m.kickoff for m in matches) + timedelta(days=1)
    as_of = as_of_naive.replace(tzinfo=UTC)  # TeamStrengthParams.as_of is always tz-aware,
    # exactly like fit_team_strength's own `as_of` parameter -- this helper
    # bypasses that entry point but must still honour its contract, since
    # write_team_strength passes params.as_of straight through to
    # store.write()'s valid_at, which rejects a naive datetime.
    weights, targets_home, targets_away = [], [], []
    for m in matches:
        days_before = (as_of_naive - m.kickoff).total_seconds() / 86400.0
        decay = math.exp(-math.log(2.0) * days_before / cfg.half_life_days)
        source_weight = 1.0 if m.is_xg_sourced else cfg.goals_source_weight
        weights.append(decay * source_weight)
        targets_home.append(m.home_xg if m.is_xg_sourced else float(m.home_goals))
        targets_away.append(m.away_xg if m.is_xg_sourced else float(m.away_goals))

    seen_teams = {m.home_team for m in matches} | {m.away_team for m in matches}
    team_universe = sorted(set(teams) | seen_teams) if teams is not None else sorted(seen_teams)
    # Mirrors fit_team_strength's own pre-fit computation (session s003) so
    # this helper actually exercises the promoted-team-prior code path,
    # not just its bookkeeping — see the tests below that check the PRIOR's
    # VALUE, not just presence in teams_with_no_history.
    no_history_teams = frozenset(t for t in team_universe if t not in seen_teams)

    attack, defence, home_advantage = _fit_attack_defence_home(
        matches, team_universe, weights, targets_home, targets_away, cfg,
        no_history_teams=no_history_teams,
    )
    rho = _fit_rho(matches, weights, attack, defence, home_advantage, cfg)

    from fplai.models.team_strength import TeamStrengthParams

    teams_with_history = seen_teams
    residuals = []
    for m in matches:
        lam = math.exp(attack[m.home_team] + defence[m.away_team] + home_advantage)
        mu = math.exp(attack[m.away_team] + defence[m.home_team])
        residuals.append(m.home_goals - lam)
        residuals.append(m.away_goals - mu)
    mean_res = sum(residuals) / len(residuals)
    var_res = sum((r - mean_res) ** 2 for r in residuals) / len(residuals)
    return TeamStrengthParams(
        teams=tuple(team_universe),
        attack=attack,
        defence=defence,
        home_advantage=home_advantage,
        rho=rho,
        config=cfg,
        as_of=as_of,
        n_matches_used=len(matches),
        seasons_used=("synthetic",),
        teams_with_no_history=tuple(t for t in team_universe if t not in teams_with_history),
        in_sample_residual_mean=mean_res,
        in_sample_residual_std=math.sqrt(var_res),
    )


def test_fit_recovers_the_correct_attack_and_defence_ranking():
    matches = _known_matches_for_recovery()
    params = fit_team_strength_from_matches(matches)

    assert params.attack["Strong"] > params.attack["Mid"] > params.attack["Weak"]
    # Defence is "leakiness" on this log scale — LOWER is better.
    assert params.defence["Strong"] < params.defence["Mid"] < params.defence["Weak"]
    # Home advantage should land close to the true 0.2 with near-noiseless,
    # heavily-repeated training data.
    assert params.home_advantage == pytest.approx(0.2, abs=0.05)


def test_fit_is_deterministic_given_the_same_inputs():
    matches = _known_matches_for_recovery()
    p1 = fit_team_strength_from_matches(matches)
    p2 = fit_team_strength_from_matches(matches)
    assert p1.attack == p2.attack
    assert p1.defence == p2.defence
    assert p1.home_advantage == p2.home_advantage
    assert p1.rho == p2.rho


def test_promoted_team_with_no_history_gets_the_league_average_prior():
    matches = _known_matches_for_recovery()
    params = fit_team_strength_from_matches(matches, teams=["Strong", "Mid", "Weak", "NewlyPromoted"])
    assert "NewlyPromoted" in params.teams_with_no_history


def test_promoted_team_gets_exactly_the_configured_prior_value_not_zero():
    # Session s003: the default prior moved off 0.0 (league average) to an
    # ESTIMATED value (TeamStrengthConfig.promoted_team_attack_prior/
    # _defence_prior). A team with genuinely zero training-window matches
    # must land EXACTLY there -- not merely "somewhere in
    # teams_with_no_history" (the older, weaker assertion above).
    matches = _known_matches_for_recovery()
    config = TeamStrengthConfig(n_adam_iterations=300, promoted_team_attack_prior=-0.5, promoted_team_defence_prior=0.3)
    params = fit_team_strength_from_matches(matches, teams=["Strong", "Mid", "Weak", "NewlyPromoted"], config=config)
    assert params.attack["NewlyPromoted"] == pytest.approx(-0.5)
    assert params.defence["NewlyPromoted"] == pytest.approx(0.3)


def test_promoted_team_prior_can_still_be_set_to_exactly_zero():
    # The pre-s003 behaviour (league-average prior) is not lost, just no
    # longer the default -- a caller can still ask for it explicitly.
    matches = _known_matches_for_recovery()
    config = TeamStrengthConfig(n_adam_iterations=300, promoted_team_attack_prior=0.0, promoted_team_defence_prior=0.0)
    params = fit_team_strength_from_matches(matches, teams=["Strong", "Mid", "Weak", "NewlyPromoted"], config=config)
    assert params.attack["NewlyPromoted"] == 0.0
    assert params.defence["NewlyPromoted"] == 0.0


def test_adding_a_no_history_team_does_not_perturb_any_other_teams_fit():
    # The isolation argument in _fit_attack_defence_home's docstring,
    # proven directly: a team with zero training-window matches never
    # appears in home_idx/away_idx, so it can affect no other team's
    # likelihood gradient. Fitted values for Strong/Mid/Weak must be
    # BIT-IDENTICAL whether or not a fourth, historyless team is present
    # in the target universe.
    matches = _known_matches_for_recovery()
    without_promoted = fit_team_strength_from_matches(matches, teams=["Strong", "Mid", "Weak"])
    with_promoted = fit_team_strength_from_matches(matches, teams=["Strong", "Mid", "Weak", "NewlyPromoted"])
    for team in ("Strong", "Mid", "Weak"):
        assert with_promoted.attack[team] == without_promoted.attack[team]
        assert with_promoted.defence[team] == without_promoted.defence[team]
    assert with_promoted.home_advantage == without_promoted.home_advantage
    assert with_promoted.rho == without_promoted.rho


def test_predict_scoreline_pmf_sums_to_one_for_a_real_fitted_pair():
    matches = _known_matches_for_recovery()
    params = fit_team_strength_from_matches(matches)
    pmf = predict_scoreline(params, "Strong", "Weak")
    total = sum(pmf.prob(h, a) for h in range(pmf.max_goals + 1) for a in range(pmf.max_goals + 1))
    assert total == pytest.approx(1.0, abs=1e-9)
    # A strong home side against a weak away side should be heavily favoured.
    assert pmf.home_win > 0.6
    assert pmf.expected_home_goals() > pmf.expected_away_goals()


def test_fit_rho_moves_away_from_zero_when_zero_zero_is_overrepresented():
    """Construct data where every match is a 0-0 (far more than an
    independent-Poisson model with these lambda/mu would predict) and
    confirm the fitted rho makes tau(0,0) = 1 - lambda*mu*rho INCREASE
    (i.e. rho goes negative, since lambda*mu > 0)."""
    lam, mu = 1.2, 1.1
    attack = {"H": math.log(lam) / 2, "A": -math.log(lam) / 2}
    # Simplify: build matches directly with fixed team roles so lambda/mu
    # are exactly reproduced by predict's own formula (home_advantage=0).
    attack = {"H": 0.0, "A": 0.0}
    defence = {"H": 0.0, "A": 0.0}
    home_advantage = 0.0
    # Force lambda=mu=1.2 by scaling attack/defence isn't needed: _fit_rho
    # only needs attack/defence/home_advantage to RECOMPUTE lambda/mu per
    # match from the team names, so encode the target lambda/mu directly.
    attack = {"H": math.log(1.2), "A": 0.0}
    defence = {"H": 0.0, "A": 0.0}
    matches = tuple(
        MatchRecord(
            season="synthetic",
            fixture=i,
            kickoff=datetime(2020, 1, 1) ,
            home_team="H",
            away_team="A",
            home_goals=0,
            away_goals=0,
            home_xg=None,
            away_xg=None,
        )
        for i in range(50)
    )
    weights = [1.0] * len(matches)
    config = TeamStrengthConfig()
    rho = _fit_rho(matches, weights, attack, defence, home_advantage, config)
    lam = math.exp(attack["H"] + defence["A"] + home_advantage)
    mu = math.exp(attack["A"] + defence["H"])
    assert _dc_tau(0, 0, lam, mu, rho) > 1.0
    assert rho < 0.0


def test_fit_rho_returns_zero_when_no_low_score_matches_present():
    matches = tuple(
        MatchRecord(
            season="synthetic",
            fixture=i,
            kickoff=datetime(2020, 1, 1),
            home_team="H",
            away_team="A",
            home_goals=3,
            away_goals=2,
            home_xg=None,
            away_xg=None,
        )
        for i in range(5)
    )
    rho = _fit_rho(matches, [1.0] * 5, {"H": 0.0, "A": 0.0}, {"H": 0.0, "A": 0.0}, 0.0, TeamStrengthConfig())
    assert rho == 0.0


# ---------------------------------------------------------------------------
# 2. Bitemporal leakage — temp store, fabricated rows, adversarial attempts.
# ---------------------------------------------------------------------------


def _row(
    season: str,
    fixture: int,
    kickoff: str,
    team: str,
    was_home: bool,
    home_goals: int,
    away_goals: int,
    expected_goals: float | None = None,
    *,
    round_: int = 1,
    element: int | None = None,
) -> dict:
    # `round`/`element` are part of vaastav_player_gameweek_stats's DECLARED
    # entity key (season, round, element, fixture; fplai.schemas.
    # PLAYER_GAMEWEEK_STATS_GAMEWEEK) -- store.as_of() partitions on them
    # regardless of whether a given test actually reads via as_of(). Omitting
    # them here previously broke any test that did (BinderException:
    # "Referenced column round not found"), caught live, not hypothesised.
    return {
        "season": season,
        "round": round_,
        "element": element if element is not None else fixture * 10 + (0 if was_home else 1),
        "fixture": fixture,
        "kickoff_time": kickoff,
        "team": team,
        "was_home": was_home,
        "team_h_score": home_goals,
        "team_a_score": away_goals,
        "expected_goals": expected_goals,
    }


@pytest.fixture
def temp_store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def _write_fixture(store, rows, *, observed_at, valid_at=None):
    df = pl.DataFrame(rows)
    store.write(
        "vaastav_player_gameweek_stats",
        df,
        valid_at=valid_at or observed_at,
        observed_at=observed_at,
        source="test",
    )


def test_build_match_table_requires_timezone_aware_as_of(temp_store):
    _write_fixture(
        temp_store,
        [_row("2024-25", 1, "2024-08-16T19:00:00Z", "TeamA", True, 2, 1), _row("2024-25", 1, "2024-08-16T19:00:00Z", "TeamB", False, 2, 1)],
        observed_at=dt(2026, 1, 1),
    )
    with pytest.raises(TeamStrengthError):
        build_match_table(temp_store, as_of=datetime(2026, 1, 1))  # naive, no tzinfo


def test_build_match_table_excludes_a_fixture_that_kicks_off_after_as_of(temp_store):
    _write_fixture(
        temp_store,
        [
            _row("2024-25", 1, "2024-08-16T19:00:00Z", "TeamA", True, 2, 1),
            _row("2024-25", 1, "2024-08-16T19:00:00Z", "TeamB", False, 2, 1),
            _row("2024-25", 2, "2024-08-23T19:00:00Z", "TeamA", True, 0, 0),
            _row("2024-25", 2, "2024-08-23T19:00:00Z", "TeamC", False, 0, 0),
        ],
        observed_at=dt(2026, 1, 1),
    )
    matches = build_match_table(temp_store, as_of=dt(2024, 8, 20))
    assert len(matches) == 1
    assert matches[0].fixture == 1


def test_as_of_boundary_is_strictly_before_not_at_kickoff(temp_store):
    """The exact-boundary adversarial case: as_of == kickoff must NOT
    include that fixture (the match had not yet been played at the
    deadline instant itself)."""
    kickoff_iso = "2024-08-16T19:00:00Z"
    _write_fixture(
        temp_store,
        [_row("2024-25", 1, kickoff_iso, "TeamA", True, 2, 1), _row("2024-25", 1, kickoff_iso, "TeamB", False, 2, 1)],
        observed_at=dt(2026, 1, 1),
    )
    as_of_exact = datetime(2024, 8, 16, 19, 0, tzinfo=UTC)
    with pytest.raises(TeamStrengthError):
        build_match_table(temp_store, as_of=as_of_exact)  # nothing strictly before it


def test_backdated_observed_at_cannot_smuggle_a_future_kickoff_into_the_training_window(temp_store):
    """Attack the module's own guarantee from outside the sanctioned path
    (this session's new standing rule): write a fixture whose KICKOFF is
    after as_of, but whose observed_at is set FAR IN THE PAST -- if
    build_match_table gated on observed_at (the way store.as_of() would),
    this row would leak straight through. It must not, because
    build_match_table filters on the archive's own kickoff_time column,
    never on observed_at."""
    _write_fixture(
        temp_store,
        [
            _row("2024-25", 1, "2024-08-16T19:00:00Z", "TeamA", True, 2, 1),
            _row("2024-25", 1, "2024-08-16T19:00:00Z", "TeamB", False, 2, 1),
            # This fixture kicks off well AFTER as_of below, but claims to
            # have been "observed" years earlier -- an attacker's attempt
            # to make it look like known-in-advance information.
            _row("2024-25", 2, "2025-05-01T19:00:00Z", "TeamA", True, 5, 0),
            _row("2024-25", 2, "2025-05-01T19:00:00Z", "TeamC", False, 5, 0),
        ],
        observed_at=dt(2020, 1, 1),  # backdated, far before either kickoff
    )
    matches = build_match_table(temp_store, as_of=dt(2024, 9, 1))
    assert len(matches) == 1
    assert matches[0].fixture == 1
    assert all(m.fixture != 2 for m in matches)


def test_build_match_table_pools_a_renamed_clubs_history_under_one_canonical_name(temp_store):
    """Session s003, the Ipswich/Ipswich Town regression test: without a
    canonicaliser, a club that changed FPL-assigned name between seasons
    (here 'Ipswich' -> 'Ipswich Town') is treated as two unrelated teams --
    each of its own two seasons is a fixture list against a team the OTHER
    season's fixture never mentions. With one, both seasons' matches pool
    under the SAME name."""
    _write_fixture(
        temp_store,
        [
            _row("2023-24", 1, "2023-08-12T14:00:00Z", "Ipswich", True, 1, 0),
            _row("2023-24", 1, "2023-08-12T14:00:00Z", "Norwich", False, 1, 0),
            _row("2024-25", 1, "2024-08-17T14:00:00Z", "Ipswich", True, 2, 2),
            _row("2024-25", 1, "2024-08-17T14:00:00Z", "Arsenal", False, 2, 2),
        ],
        observed_at=dt(2026, 1, 1),
    )
    as_of = dt(2026, 1, 1)

    without = build_match_table(temp_store, as_of=as_of)
    names_without = {m.home_team for m in without} | {m.away_team for m in without}
    assert "Ipswich" in names_without and "Ipswich Town" not in names_without

    canonicaliser = TeamNameCanonicalisationMap.build(
        pl.DataFrame(
            [
                {"season": "2023-24", "id": 10, "code": 40, "name": "Ipswich", "short_name": "IPS"},
                {"season": "2024-25", "id": 10, "code": 40, "name": "Ipswich", "short_name": "IPS"},
            ]
        ),
        live_teams=pl.DataFrame([{"code": 40, "name": "Ipswich Town", "short_name": "IPS"}]),
    )
    with_canon = build_match_table(temp_store, as_of=as_of, team_name_canonicaliser=canonicaliser)
    names_with = {m.home_team for m in with_canon} | {m.away_team for m in with_canon}
    assert "Ipswich Town" in names_with and "Ipswich" not in names_with
    # Both seasons' matches now carry the SAME home-team name.
    ipswich_seasons = {m.season for m in with_canon if m.home_team == "Ipswich Town"}
    assert ipswich_seasons == {"2023-24", "2024-25"}


def test_store_as_of_is_empty_for_a_genuinely_historical_deadline_on_this_dataset(temp_store):
    """Regression-proves the module docstring's central bitemporal-fitting
    claim: this store's bulk-ingested archive has observed_at ~= ingestion
    time, so store.as_of() (which filters on observed_at) is the WRONG
    primitive here -- verified directly, not assumed, per 'prove it fails
    first'."""
    _write_fixture(
        temp_store,
        [_row("2020-21", 1, "2020-09-12T14:00:00Z", "TeamA", True, 1, 0), _row("2020-21", 1, "2020-09-12T14:00:00Z", "TeamB", False, 1, 0)],
        observed_at=dt(2026, 1, 1),  # bulk-ingested "today", long after the match
    )
    historical_deadline = dt(2020, 9, 10)  # before the match, in real historical time
    out = temp_store.as_of("vaastav_player_gameweek_stats", historical_deadline)
    assert out.is_empty(), "as_of() gated on observed_at must NOT see this row at a real historical deadline"


def test_wholly_null_team_season_is_excluded_not_guessed(temp_store):
    # `round`/`element` are part of PLAYER_GAMEWEEK_STATS_GAMEWEEK's
    # DECLARED entity key (season, round, element, fixture) -- omitting
    # them here previously slid through because build_match_table read via
    # observations() (no entity-key reference at all); it now reads via
    # store.effective_at(), which partitions on the full declared entity
    # key and raises a DuckDB BinderException on a payload missing a key
    # column, the same trap this file's own _row() helper's comment already
    # documents for a different test. Caught live migrating to effective_at().
    rows = [
        {
            "season": "2019-20",
            "round": 1,
            "element": 10,
            "fixture": 1,
            "kickoff_time": "2019-08-10T14:00:00Z",
            "team": None,
            "was_home": True,
            "team_h_score": 1,
            "team_a_score": 1,
            "expected_goals": None,
        },
        {
            "season": "2019-20",
            "round": 1,
            "element": 11,
            "fixture": 1,
            "kickoff_time": "2019-08-10T14:00:00Z",
            "team": None,
            "was_home": False,
            "team_h_score": 1,
            "team_a_score": 1,
            "expected_goals": None,
        },
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    with pytest.raises(TeamStrengthError):
        build_match_table(temp_store, as_of=dt(2026, 1, 1))


def test_xg_present_both_sides_uses_xg_target_not_goals(temp_store):
    _write_fixture(
        temp_store,
        [
            _row("2022-23", 1, "2022-08-06T14:00:00Z", "TeamA", True, 3, 0, expected_goals=1.4),
            _row("2022-23", 1, "2022-08-06T14:00:00Z", "TeamB", False, 3, 0, expected_goals=0.6),
        ],
        observed_at=dt(2026, 1, 1),
    )
    matches = build_match_table(temp_store, as_of=dt(2026, 1, 1))
    assert len(matches) == 1
    m = matches[0]
    assert m.is_xg_sourced
    assert m.home_xg == pytest.approx(1.4)
    assert m.away_xg == pytest.approx(0.6)
    assert m.home_goals == 3 and m.away_goals == 0


def test_fit_team_strength_end_to_end_against_a_temp_store(temp_store):
    rows = []
    for i in range(1, 11):
        rows.append(_row("2022-23", i, "2023-01-01T15:00:00Z", "Big", i % 2 == 0, 2, 0, expected_goals=1.8))
        rows.append(_row("2022-23", i, "2023-01-01T15:00:00Z", "Small", i % 2 != 0, 2, 0, expected_goals=0.4))
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    params = fit_team_strength(temp_store, as_of=dt(2026, 1, 1), config=TeamStrengthConfig(n_adam_iterations=150))
    assert params.attack["Big"] > params.attack["Small"]
    pmf = predict_scoreline(params, "Big", "Small")
    assert sum(pmf.prob(h, a) for h in range(pmf.max_goals + 1) for a in range(pmf.max_goals + 1)) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 3. Real-store-gated sanity + the derived-capability write path.
# ---------------------------------------------------------------------------


def _real_store_available() -> bool:
    path = Path(__file__).resolve().parents[1] / "data" / "store" / "vaastav_player_gameweek_stats"
    return path.exists() and any(path.glob("**/*.parquet"))


requires_real_store = pytest.mark.skipif(
    not _real_store_available(), reason="data/store/vaastav_player_gameweek_stats absent"
)


@pytest.fixture
def registered_capability():
    """The real team-strength derived capability, registered at
    `fplai.models.team_strength`'s own IMPORT time (Architect ruling,
    2026-08-22 -- see `fplai.schemas`'s 'Phase 2, E5' section for the full
    resolution). No register/unregister-in-`finally` step here any more:
    unlike the earlier lazy-registration workaround, this capability is
    process-wide and permanent for the life of the test session, same as
    every E2b-era observed capability -- there is nothing to tear down."""
    return CANONICAL_SCHEMAS[TEAM_STRENGTH_RATING_GAMEWEEK]


def test_registering_the_capability_does_not_leak_across_tests(registered_capability):
    assert registered_capability.is_modelled is True
    assert TEAM_STRENGTH_RATING_GAMEWEEK in CANONICAL_SCHEMAS


def test_importing_the_module_registers_the_capability_at_import_time():
    """Proves the 'import-time registration' design decision (Architect
    ruling, 2026-08-22 -- see fplai.schemas' 'Phase 2, E5' section; this is
    the direct INVERSE of what this test asserted before that ruling) in a
    genuinely fresh process -- not just 'some earlier test in this SAME
    pytest session already imported the module', which a same-process
    assertion could not distinguish from the real guarantee once import
    caching is in play."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = (
        "import fplai.models.team_strength\n"
        "from fplai.schemas import CANONICAL_SCHEMAS, TEAM_STRENGTH_RATING_GAMEWEEK\n"
        "assert TEAM_STRENGTH_RATING_GAMEWEEK in CANONICAL_SCHEMAS, "
        "'importing team_strength must register the derived capability at import time'\n"
        "schema = CANONICAL_SCHEMAS[TEAM_STRENGTH_RATING_GAMEWEEK]\n"
        "assert schema.is_modelled is True\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_write_team_strength_round_trips_through_write_derived(temp_store, registered_capability):
    matches = _known_matches_for_recovery()
    params = fit_team_strength_from_matches(matches)
    result = write_team_strength(temp_store, params, season="2026-27", gameweek=1)
    assert result.written
    assert result.n_rows == len(params.teams)

    # write_team_strength stamps observed_at=datetime.now(UTC) (fplai.derived's
    # own convention -- "this genuinely is when THIS system learned the fact").
    # A fixed dt(2026, ...) cutoff would silently stop working the moment real
    # wall-clock time passes it -- query strictly after "now" instead.
    out = temp_store.as_of("derived_team_strength_rating", datetime.now(UTC) + timedelta(days=1))
    assert out.height == len(params.teams)
    assert out["is_modelled"].all()
    assert set(out["team"].to_list()) == set(params.teams)


def test_derived_team_strength_rows_are_absent_from_the_observed_dataset(temp_store, registered_capability):
    # Seed the observed dataset too, so both live in the same store.
    _write_fixture(
        temp_store,
        [_row("2024-25", 1, "2024-08-16T19:00:00Z", "TeamA", True, 2, 1), _row("2024-25", 1, "2024-08-16T19:00:00Z", "TeamB", False, 2, 1)],
        observed_at=dt(2026, 1, 1),
    )
    matches = _known_matches_for_recovery()
    params = fit_team_strength_from_matches(matches)
    write_team_strength(temp_store, params, season="2026-27", gameweek=1)

    observed_out = temp_store.as_of("vaastav_player_gameweek_stats", dt(2026, 1, 2))
    assert "is_modelled" not in observed_out.columns
    assert "attack" not in observed_out.columns


@requires_real_store
def test_build_match_table_against_the_real_store_produces_a_plausible_dataset():
    real_store = BitemporalStore()  # default path = data/store/, read-only here
    matches = build_match_table(real_store, as_of=dt(2026, 8, 21))
    assert len(matches) > 2000  # six full seasons' worth, verified live: 2280
    assert all(m.season != "2019-20" for m in matches)
    assert any(m.is_xg_sourced for m in matches)
    assert any(not m.is_xg_sourced for m in matches)


@requires_real_store
def test_fit_against_the_real_store_ranks_a_known_strong_side_above_a_known_weak_one():
    """Sanity check on real data, per this task's brief: 'does the fit rank
    teams plausibly'. Man City's attack (dominant, high-xG side across
    this window) must rank above Southampton's (relegated with a heavily
    negative goal difference in this window) -- if this ever fails, that
    is the fit telling us something is wrong, not a flaky assertion to
    loosen."""
    real_store = BitemporalStore()
    params = fit_team_strength(real_store, as_of=dt(2026, 8, 21))
    assert params.attack["Man City"] > params.attack["Southampton"]
    assert params.defence["Arsenal"] < params.defence["Southampton"]
    pmf = predict_scoreline(params, "Man City", "Southampton")
    assert pmf.home_win > pmf.away_win


@requires_real_store
def test_team_name_canonicaliser_maps_both_ipswich_spellings_to_the_live_name():
    """Live-verified (session s003, this task's brief): vaastav's 2024-25
    archive uses `name == "Ipswich"`; the live 2026-27 `teams` dataset uses
    `name == "Ipswich Town"` -- same club (code 40), same provider
    lineage, genuinely different string. The canonicaliser must fold both
    onto the SAME name."""
    real_store = BitemporalStore()
    team_identity_rows = real_store.latest("vaastav_team_identity")
    live_teams = real_store.latest("teams")
    canonicaliser = TeamNameCanonicalisationMap.build(team_identity_rows, live_teams=live_teams)
    assert canonicaliser.canonicalise("2024-25", "Ipswich") == "Ipswich Town"
    assert canonicaliser.canonicalise("2026-27", "Ipswich Town") == "Ipswich Town"


@requires_real_store
def test_fit_with_canonicaliser_gives_ipswich_town_real_history_not_the_promoted_prior():
    """The end-to-end regression test for task 2: WITHOUT the
    canonicaliser, 'Ipswich Town' (the name `teams=` asks for, since that
    is the live dataset's own name) has literally zero matches under that
    exact string -- its real 2024-25 relegation-season history is filed
    under the archive's own 'Ipswich' instead -- so it gets the
    promoted-team prior despite having real, recent, informative history.
    WITH it, that history pools under 'Ipswich Town' and the team is NOT
    in teams_with_no_history."""
    real_store = BitemporalStore()
    live_teams = real_store.latest("teams")
    teams = live_teams["name"].to_list()
    as_of = dt(2026, 8, 21)

    without = fit_team_strength(real_store, as_of=as_of, teams=teams)
    assert "Ipswich Town" in without.teams_with_no_history

    team_identity_rows = real_store.latest("vaastav_team_identity")
    canonicaliser = TeamNameCanonicalisationMap.build(team_identity_rows, live_teams=live_teams)
    with_canon = fit_team_strength(real_store, as_of=as_of, teams=teams, team_name_canonicaliser=canonicaliser)
    assert "Ipswich Town" not in with_canon.teams_with_no_history
    # A real, data-driven rating -- not the promoted-team-prior constant.
    assert with_canon.attack["Ipswich Town"] != pytest.approx(TeamStrengthConfig().promoted_team_attack_prior)
    assert with_canon.defence["Ipswich Town"] != pytest.approx(TeamStrengthConfig().promoted_team_defence_prior)
    # Only the two clubs with GENUINELY zero history in this store's
    # 7-season window remain -- verified live, docs/wiki/model-team-strength.md.
    assert set(with_canon.teams_with_no_history) == {"Coventry City", "Hull City"}
