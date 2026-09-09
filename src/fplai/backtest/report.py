"""Distribution summaries over a season's per-gameweek scores — blueprint
§2/§7.2: "report distributions, not just totals." A baseline's variance is
as informative as its mean; rank is driven by the shape of the outcome, not
its centre.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from fplai.backtest.replay import GameweekResult


@dataclass(frozen=True)
class SeasonSummary:
    season: str
    strategy_name: str
    n_gameweeks: int
    total_points: int
    mean: float
    median: float
    stdev: float
    minimum: int
    maximum: int
    p5: float
    p25: float
    p75: float
    p95: float
    zero_point_gameweeks: int

    def describe(self) -> str:
        return (
            f"{self.season} {self.strategy_name}: total={self.total_points} "
            f"over {self.n_gameweeks} GW (mean={self.mean:.1f}, median={self.median:.1f}, "
            f"stdev={self.stdev:.1f}, range=[{self.minimum},{self.maximum}], "
            f"p5={self.p5:.1f}, p25={self.p25:.1f}, p75={self.p75:.1f}, p95={self.p95:.1f})"
        )


def _percentile(sorted_values: list[int], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return float(sorted_values[f])
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def summarise(season: str, strategy_name: str, results: list[GameweekResult]) -> SeasonSummary:
    if not results:
        raise ValueError(f"no results to summarise for {season} / {strategy_name}")
    points = [r.points for r in results]
    sorted_points = sorted(points)
    return SeasonSummary(
        season=season,
        strategy_name=strategy_name,
        n_gameweeks=len(points),
        total_points=sum(points),
        mean=statistics.fmean(points),
        median=statistics.median(points),
        stdev=statistics.pstdev(points) if len(points) > 1 else 0.0,
        minimum=min(points),
        maximum=max(points),
        p5=_percentile(sorted_points, 0.05),
        p25=_percentile(sorted_points, 0.25),
        p75=_percentile(sorted_points, 0.75),
        p95=_percentile(sorted_points, 0.95),
        zero_point_gameweeks=sum(1 for p in points if p == 0),
    )
