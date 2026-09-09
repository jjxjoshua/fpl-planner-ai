"""EO (effective ownership) sampling — blueprint §3.4.

Pure, testable pieces used by `scripts/sample_picks.py`:

  - `find_max_valid_entry_id`: ~25-request binary search for the current
    upper edge of the entry-id space (grows every week as managers join).
  - `draw_uniform_entry_ids`: seeded, deterministic uniform draw over that
    space. NOT league-314 paging — paging is rank-ordered and yields
    clustered, non-independent samples (blueprint §3.4).
  - `calibration_check`: sampled ownership vs bootstrap-static's
    `selected_by_percent`. The blueprint calls a mismatch a hard failure,
    not a warning — see docstring.
  - `already_sampled_entry_ids`: the resumability half of pre-deadline
    gate-repair session s003's incremental-persistence fix — see its own
    docstring for why this belongs here rather than being invented ad hoc
    in `scripts/sample_picks.py`.

Network access lives in `FPLClient`; nothing here does I/O except through the
client instance it's handed (or, for `already_sampled_entry_ids`, the STORE
instance it's handed), which keeps this module mockable without a network
mock and, for the store case, testable against a real temp
`BitemporalStore` rather than a mock of one (CLAUDE.md lesson 6 — a mock of
the store cannot catch a real query-shape bug the way the real store class
can).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import polars as pl

from fplai.client import FPLClient

logger = logging.getLogger("fplai.sampling")


def find_max_valid_entry_id(
    client: FPLClient,
    *,
    lower_bound: int = 1,
    initial_upper: int | None = None,
    hard_cap: int = 100_000_000,
) -> int:
    """Find the highest entry id that currently resolves (`entry/{id}/` !=
    404). Two phases: exponential growth to bracket the boundary, then binary
    search within the bracket. ~25 requests total for a range in the low
    millions (blueprint §3.4, wiki §2.4).

    Assumes `lower_bound` itself is valid — raises if not, since that
    assumption is exactly what makes the exponential phase correct.
    """
    if client.entry(lower_bound) is None:
        raise RuntimeError(
            f"entry id {lower_bound} (the assumed lower bound) does not resolve — "
            "cannot bracket the search. The FPL entry-id space may have changed shape."
        )

    lo = lower_bound
    hi = initial_upper or max(lower_bound * 2, 1_000_000)
    while client.entry(hi) is not None:
        lo = hi
        hi *= 2
        if hi > hard_cap:
            raise RuntimeError(f"entry id search exceeded hard cap {hard_cap} without finding a 404")

    while hi - lo > 1:
        mid = (lo + hi) // 2
        if client.entry(mid) is not None:
            lo = mid
        else:
            hi = mid

    return lo


def draw_uniform_entry_ids(max_id: int, n: int, seed: int) -> list[int]:
    """Uniform draw of `n` distinct ids from [1, max_id], seeded and
    deterministic (CLAUDE.md rule 7). `random.Random(seed).sample` is used
    over a `range` object so this stays cheap even for max_id in the
    millions — it never materialises the full population."""
    if n > max_id:
        raise ValueError(f"cannot draw {n} distinct ids from a population of {max_id}")
    rng = random.Random(seed)
    return rng.sample(range(1, max_id + 1), n)


@dataclass(frozen=True)
class CalibrationResult:
    passed: bool
    threshold_pp: float
    max_deviation_pp: float
    worst_offenders: list[dict] = field(default_factory=list)
    n_compared: int = 0


def calibration_check(
    sampled_ownership_pct: pl.DataFrame,
    bootstrap_elements: pl.DataFrame,
    *,
    threshold_pp: float = 3.0,
    min_selected_by_percent: float = 1.0,
) -> CalibrationResult:
    """The blueprint's free calibration check (§3.4): sampled ownership must
    reproduce `selected_by_percent` from bootstrap-static within sampling
    error. A mismatch is a HARD FAILURE — it means the sample is biased
    (e.g. the id space assumption is wrong, or the draw isn't actually
    uniform) — never treat it as a soft warning.

    `sampled_ownership_pct` must have columns [element, sampled_pct].
    `bootstrap_elements` must have columns [id, selected_by_percent] (the
    latter as the API's string-encoded percentage).

    Comparison restricted to players with `selected_by_percent >=
    min_selected_by_percent` — sampling noise on near-zero-owned players is
    not informative and would swamp the check with expected noise.
    """
    bs = bootstrap_elements.select(
        pl.col("id").alias("element"),
        pl.col("selected_by_percent").cast(pl.Float64).alias("bootstrap_pct"),
    ).filter(pl.col("bootstrap_pct") >= min_selected_by_percent)

    joined = bs.join(sampled_ownership_pct, on="element", how="left").with_columns(
        pl.col("sampled_pct").fill_null(0.0)
    )
    joined = joined.with_columns(
        (pl.col("sampled_pct") - pl.col("bootstrap_pct")).abs().alias("deviation_pp")
    )

    if joined.is_empty():
        return CalibrationResult(passed=True, threshold_pp=threshold_pp, max_deviation_pp=0.0, n_compared=0)

    max_dev = float(joined["deviation_pp"].max())
    worst = (
        joined.sort("deviation_pp", descending=True)
        .head(10)
        .select(["element", "bootstrap_pct", "sampled_pct", "deviation_pp"])
        .to_dicts()
    )
    return CalibrationResult(
        passed=max_dev <= threshold_pp,
        threshold_pp=threshold_pp,
        max_deviation_pp=max_dev,
        worst_offenders=worst,
        n_compared=joined.height,
    )


def already_sampled_entry_ids(
    store, event_id: int, *, now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
) -> set[int]:
    """Distinct `entry_id`s already persisted in the `picks` dataset for
    `event_id`, from ANY prior batch — the resumability half of blueprint
    §3.4's incremental-persistence fix (pre-deadline gate-repair session
    s003).

    **Why this belongs at the store-query layer, not "just retry the whole
    10,000 again":** `run()`'s entry-id draw is fully deterministic given
    (event, seed) — `draw_uniform_entry_ids` always returns the identical
    list for the identical inputs (CLAUDE.md rule 7). A crash partway
    through the ~83-minute run therefore has an honest, sanctioned way to
    resume: recompute the SAME entry_ids list, then skip whichever prefix
    was already safely persisted to disk before the crash, so a restart
    costs only the time already spent, not the whole run over again — a
    second, larger-scale expression of the same "bound the loss to what was
    actually lost" principle chunked persistence already gives at the
    single-chunk level.

    Uses `observations()`, not `as_of()` — this is explicitly NOT a
    bitemporal state read (there is no single "as of" instant that makes
    sense here); it is "was this entry_id ever captured for this event, at
    all, by any batch". `observations()` already returns an empty frame for
    a dataset with no data yet, so the "very first run, store is empty"
    case needs no special-casing here.

    A genuine MISS (the entry existed but 404'd on `entry_picks`, no row
    ever written for it) is NOT distinguishable from "never attempted" by
    this query — a resumed run will re-issue live requests for prior
    misses. This is a deliberate, documented trade-off: correctness-safe
    (no data loss either way) at the cost of some redundant requests for
    entries that will very likely miss again, never the reverse (silently
    skipping an entry that might now have a real picks row).

    Deliberately does NOT filter on `source` — every writer of the `picks`
    dataset today is `scripts/sample_picks.py` via this one `SOURCE`
    constant; scoping by event alone is sufficient and avoids a second
    assumption (an exact source string) a future writer could silently
    violate.

    `now_fn` (CLAUDE.md rule 7 — deterministic, seeded, no hidden wall-clock
    dependency) is the `until=` cutoff passed to `observations()`; it
    defaults to the real clock for production use but MUST be injected by
    any test that writes rows with a fixed `observed_at` — a test using a
    literal fixed date and the real `datetime.now()` is only correct until
    that literal date arrives, and silently "self-heals" into passing
    before then for the wrong reason (a rule-5 "test that cannot fail is
    worse than no test" instance, not just a flaky one).
    """
    now = now_fn()
    df = store.observations("picks", until=now)
    if df.is_empty():
        return set()
    df = df.filter(pl.col("event") == event_id)
    if df.is_empty():
        return set()
    return set(df["entry_id"].unique().to_list())
