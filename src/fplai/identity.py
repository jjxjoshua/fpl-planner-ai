"""Cross-provider identity resolution — E2b story 7 (blueprint §12.5).

blueprint §12.5: "`opta_code` is the join key where available. Where it is
not, a per-provider ID map resolves to canonical entities. Unmatched
entities raise, they never silently drop." This module is where that rule
is implemented. It is deliberately provider-agnostic (not `providers/pl.py`
internals) because identity resolution is a cross-cutting concern — the
next non-FPL provider (odds, story 8; archives, story 10) will need the
same "raise loudly, never guess" discipline, and a shared module means
there is exactly one place that discipline lives.

Two entities, two very different join costs, measured live against the
Premier League API on 2026-08-20 (docs/wiki/provider-framework.md carries
the full record):

- **Players join for free.** FPL `elements[].code` (int) IS the Premier
  League API's own numeric player id, verified 599/599 against the LIVE
  bootstrap-static snapshot with zero opta_code-format mismatches.
  `PlayerIdentityMap` still exists as a real lookup (not "just cast to
  int") because §12.5 requires resolution to FAIL when a PL API player id
  has no current FPL element — which happens routinely for a player who has
  left the Premier League since the elements snapshot was taken (see
  `PlayerIdentityMap.resolve`'s docstring).

- **Teams do NOT join via `opta_code`** — `teams[].opta_code` is `None` for
  all 20 current clubs, confirmed live. They DO join via a second,
  undocumented equivalence discovered and verified this session: the PL
  API's own numeric team id (as returned on match/lineup/fixture payloads)
  equals FPL's *legacy* `teams[].code` field exactly — not `id` (the
  season-local 1-20 slot), `code` (e.g. Arsenal=3, Aston Villa=7,
  Bournemouth=91, Brentford=94). Verified for all 20 clubs of the
  2026/27 season directly against the PL API's own GW1 fixture list
  (`TeamIdentityMap.build`'s docstring has the exact verification method).
  This is real news for the blueprint/wiki, not merely "the mapping exists"
  — it means teams join for free too, just via a different field than the
  Opta id.

Both maps are built ONCE from a live snapshot and then used as pure
lookups — nothing here makes a network call itself; `providers/pl.py` is
responsible for fetching the raw payloads these `.build()` classmethods
consume.

**Extended 2026-08-20 (E2b story 10, blueprint §12.5's "identity maps are
season-scoped" extension).** Before this story, every call site that built
a `PlayerIdentityMap` passed whatever `elements` DataFrame it had to hand —
in practice, always the CURRENT live `bootstrap-static` snapshot, because
that was the only `elements`-shaped source that existed. Resolving a
2025/26 match against that snapshot correctly raised on players who have
since left the league (§12.5's "fail loudly" working exactly as
specified) — but the deeper bug was that nothing chose the RIGHT snapshot
in the first place. `build_player_identity_map_for_season()` below is that
missing choice: given a target season and the current season, it either
uses the live snapshot (current season) or defers to a caller-supplied
archive fetch (any other season) — e.g. `providers/vaastav.py`'s
`player.identity@season`, which is a per-season `elements` equivalent the
FPL live API simply cannot supply (it has no historical-elements
endpoint). This module still makes no network call itself and does not
import `providers/vaastav.py` (avoiding a cycle, and keeping identity
resolution provider-agnostic, same as `PlayerIdentityMap`/`TeamIdentityMap`
always were) — the archive fetch is injected as a plain callable.

**Extended again 2026-08-21 (E2b story 7b, closing the player/team
asymmetry §12.5 itself calls out).** Story 10 only fixed the PLAYER half.
Team identity kept resolving against whatever `teams`/`pl_teams` a caller
had to hand — in every real call site, the CURRENT season's, because team
CODES persist across seasons and the current 20-club list "happens to
work" for any historical match whose two clubs are both still in the
league. It stops working the moment a fixture involves a since-relegated
club — proven live, story 11 (§11.4 in the wiki): a 2025/26 fixtures fetch
halted on PL numeric team id 21 (West Ham), absent from the current
2026/27 20. `build_team_identity_map_for_season()` below is the missing
choice, symmetrical to the player function: current season -> `live_teams`
(unchanged); any other season -> `archive_teams_fetch(season)`, e.g.
`providers/vaastav.py`'s new `team.identity@season`
(`data/{season}/teams.csv`). One genuine asymmetry remains, not fixed
here because it is not a bug: `TeamIdentityMap.build` also needs
`pl_teams` (the PL API's OWN raw team ids for that season, extracted from
that season's matchweek-1 fixtures) — unlike the player join, which needs
only the FPL-side snapshot. `pl_teams` is therefore still a required
argument here, not something this function can default or fetch itself
(this module makes no network call) — the CALLER (`scripts/backfill.py`'s
`provider_factory`) is responsible for fetching a season-MATCHED
`pl_teams`, not an arbitrary one; passing 2026's `pl_teams` against 2025's
`teams.csv` would silently re-introduce a season mismatch of exactly the
kind this story exists to close.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable

import polars as pl

logger = logging.getLogger("fplai.identity")


class IdentityError(RuntimeError):
    """An entity could not be resolved across providers. Raised, never
    silently dropped or guessed (blueprint §12.5) — the specific failure
    mode this module exists to prevent is a fuzzy match that quietly
    mismatches on a transfer or an accented name, corrupting a model
    without failing a test."""


# -- players -----------------------------------------------------------


@dataclass(frozen=True)
class PlayerIdentityMap:
    """PL API numeric player id -> current-season FPL element id.

    Built from ONE FPL `elements` snapshot. The join itself needs no
    lookup at all — `code == PL player id` structurally — but `.build()`
    still verifies every row (§12.5's "unmatched entities raise" applies
    to the coverage CHECK, not just the join), and `.resolve()` still goes
    through a dict rather than a bare `int()` cast so a genuinely
    unresolvable id (see below) fails loudly instead of returning a
    plausible-looking but wrong element id.
    """

    code_to_element_id: dict[int, int]
    element_id_to_code: dict[int, int]

    @classmethod
    def build(cls, elements: pl.DataFrame) -> "PlayerIdentityMap":
        """Verify AND build the map in one pass. Raises `IdentityError` if
        any row fails the identity check — never returns a partial map
        silently missing the bad rows, because a caller iterating
        `code_to_element_id` has no way to know rows are missing."""
        required = ("id", "code", "opta_code")
        missing_cols = [c for c in required if c not in elements.columns]
        if missing_cols:
            raise IdentityError(
                f"elements snapshot is missing column(s) {missing_cols} needed "
                "for player identity resolution — did the FPL provider's "
                "'flatten and keep everything' contract change? See "
                "fplai.providers.fpl._records_to_df."
            )

        rows = elements.select("id", "code", "opta_code").to_dicts()
        code_to_element_id: dict[int, int] = {}
        element_id_to_code: dict[int, int] = {}
        failed: list[dict] = []
        for row in rows:
            elem_id, code, opta = row["id"], row["code"], row["opta_code"]
            if elem_id is None or code is None or opta is None:
                failed.append(row)
                continue
            code_int, elem_id_int = int(code), int(elem_id)
            if opta != f"p{code_int}":
                failed.append(row)
                continue
            if code_int in code_to_element_id or elem_id_int in element_id_to_code:
                failed.append(row)  # duplicate code/id — same failure class, never silently overwrite
                continue
            code_to_element_id[code_int] = elem_id_int
            element_id_to_code[elem_id_int] = code_int

        if failed:
            raise IdentityError(
                f"{len(failed)} of {len(rows)} FPL elements failed the "
                "code<->opta_code player-identity check (expected every row "
                "to satisfy opta_code == f'p{code}', unique per code and id). "
                f"First failures: {failed[:5]}"
            )

        logger.info("player identity map built: %d/%d elements resolved (100%%)", len(code_to_element_id), len(rows))
        return cls(code_to_element_id=code_to_element_id, element_id_to_code=element_id_to_code)

    def resolve(self, pl_player_id: int | str) -> int:
        """PL API player id -> current-season FPL element id.

        Raises `IdentityError` on a miss. This is EXPECTED, not a bug, for
        a player who has left the Premier League since the `elements`
        snapshot this map was built from was taken — e.g. fetching an
        old match's lineup will routinely name players no longer in any
        current bootstrap-static payload. Per blueprint §12.5 this must
        raise rather than silently drop the player from the lineup; a
        caller doing historical backfill (E2b story 11, not in scope
        here) needs a season-appropriate `elements` snapshot to resolve
        against, not today's — see docs/wiki/provider-framework.md.
        """
        key = int(pl_player_id)
        if key not in self.code_to_element_id:
            raise IdentityError(
                f"unresolved player: PL API player id {pl_player_id} has no "
                "matching FPL element in the snapshot this map was built "
                "from (blueprint §12.5 — unmatched entities raise, never "
                "silently drop). If this is a historical fetch, the player "
                "may have left the Premier League since the snapshot date; "
                "resolve against a season-appropriate elements snapshot."
            )
        return self.code_to_element_id[key]

    def resolve_reverse(self, element_id: int) -> int:
        """Current-season FPL element id -> PL API player id (== code).
        Needed by `player.season_stats@season`, whose PL API endpoint is
        keyed on the PL numeric id, not the FPL element id a caller more
        naturally has to hand (e.g. from `picks` or `elements`)."""
        key = int(element_id)
        if key not in self.element_id_to_code:
            raise IdentityError(
                f"unresolved player: FPL element id {element_id} is not in this "
                "identity map's snapshot (blueprint §12.5)."
            )
        return self.element_id_to_code[key]


def build_player_identity_map_for_season(
    season: str,
    *,
    current_season: str,
    live_elements: pl.DataFrame | None = None,
    archive_elements_fetch: Callable[[str], pl.DataFrame] | None = None,
) -> PlayerIdentityMap:
    """Build a `PlayerIdentityMap` against the snapshot valid AT `season` —
    blueprint §12.5: "`resolve(entity, as_of=T)` resolves against the
    identity snapshot valid at T — never against today's." This is the
    season-scoped selection point that was missing before E2b story 10 (see
    this module's docstring).

    `season == current_season` (the live, in-progress season): uses
    `live_elements` — the live FPL `bootstrap-static` snapshot, exactly as
    before this story. Any OTHER season: defers to `archive_elements_fetch`,
    a plain `callable(season) -> pl.DataFrame` returning that season's
    `player.identity@season` rows (e.g. `providers.vaastav.VaastavProvider
    .fetch(PLAYER_IDENTITY_SEASON, season=season).rows` — passed as a
    callable rather than a provider instance directly, so this module keeps
    importing nothing from `providers/`, matching `PlayerIdentityMap`/
    `TeamIdentityMap`'s existing provider-agnostic design).

    Raises `IdentityError` — not a bare `ValueError`/`TypeError` — if the
    caller didn't supply the argument this particular `season` needs. Both
    branches still end at `PlayerIdentityMap.build()`, which is where the
    ACTUAL per-row verification/raise (§12.5's "unmatched entities raise")
    happens; this function's own job is only "pick the right snapshot",
    never "decide whether a row resolves".
    """
    if season == current_season:
        if live_elements is None:
            raise IdentityError(
                f"season {season!r} is the CURRENT season ({current_season!r}) but no "
                "live_elements snapshot was supplied. Pass the live FPL bootstrap-static "
                "'elements' DataFrame, e.g. FPLProvider(...).fetch(PLAYER_ATTRIBUTES_CURRENT)"
                ".rows."
            )
        return PlayerIdentityMap.build(live_elements)

    if archive_elements_fetch is None:
        raise IdentityError(
            f"season {season!r} is not the current season ({current_season!r}) — resolving "
            "identity for it needs a season-scoped snapshot, which the LIVE FPL API cannot "
            "supply (no historical-elements endpoint exists). Pass archive_elements_fetch, a "
            "callable(season) -> pl.DataFrame returning that season's player.identity@season "
            "rows (e.g. VaastavProvider.fetch(PLAYER_IDENTITY_SEASON, season=season).rows)."
        )
    return PlayerIdentityMap.build(archive_elements_fetch(season))


# -- teams ---------------------------------------------------------------

_TEAM_NAME_NOISE = re.compile(r"\b(fc|afc|and|hove|albion|city|united|town|wanderers|hotspur)\b")


def _normalise_team_name(name: str) -> str:
    """Loose normalisation for the SANITY check only — never the resolution
    itself (that is the numeric id match, see `TeamIdentityMap.build`).
    Strips common club-name furniture so 'Spurs'/'Tottenham Hotspur' and
    'Man Utd'/'Manchester United' compare as plausibly-the-same without
    claiming to be a real fuzzy matcher."""
    lowered = name.lower().strip()
    stripped = _TEAM_NAME_NOISE.sub(" ", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


@dataclass(frozen=True)
class TeamIdentityMap:
    """FPL team `code` (legacy, permanent — e.g. Arsenal=3) <-> PL API
    numeric team id, as returned on match/lineup/fixture payloads.

    NOT built from `opta_code` — `teams[].opta_code` is `None` for all 20
    current clubs (verified live, 2026-08-20). Built instead from a
    directly-observed equivalence: the PL API's own team id on match
    payloads equals FPL's `code` field exactly. This was NOT given as a
    verified fact going into this story — it was derived and verified here
    by cross-referencing two live, independent sources:

    1. FPL `bootstrap-static` `teams[]` — `(code, name)` for the current
       20 clubs.
    2. PL API `/v1/competitions/8/seasons/{season}/matchweeks/1/matches` —
       `(homeTeam.id, homeTeam.name)` / `(awayTeam.id, awayTeam.name)` for
       the same 20 clubs (GW1 fixtures exist pre-deadline; verified live
       for season 2026 on 2026-08-20, kickoff dates 21-24 Aug 2026).

    Verified 20/20 for season 2026 (2026/27), including three newly
    promoted clubs (Coventry City=9, Hull City=88, Ipswich Town=40) that do
    not appear in the PRIOR season's fixture list at all — i.e. this is not
    an artefact of reusing a stale team list, both sides were read from the
    live 2026/27 fixture calendar.
    """

    fpl_code_to_pl_id: dict[int, str]
    pl_id_to_fpl_code: dict[str, int]

    @classmethod
    def build(cls, fpl_teams: pl.DataFrame, pl_teams: list[dict]) -> "TeamIdentityMap":
        """`fpl_teams`: FPL `teams` snapshot (needs `code`, `name`).
        `pl_teams`: raw `{"id": ..., "name": ...}` dicts extracted from a PL
        API matchweek fixtures response (see `providers/pl.py`'s
        `_extract_pl_teams`). Raises `IdentityError` naming every FPL team
        that could not be resolved — never returns a map silently missing
        rows."""
        required = ("code", "name")
        missing_cols = [c for c in required if c not in fpl_teams.columns]
        if missing_cols:
            raise IdentityError(f"teams snapshot is missing column(s) {missing_cols} needed for team identity resolution")

        pl_by_id = {str(t["id"]): t for t in pl_teams}

        fpl_code_to_pl_id: dict[int, str] = {}
        pl_id_to_fpl_code: dict[str, int] = {}
        unresolved: list[tuple[int, str]] = []
        name_mismatches: list[tuple[int, str, str]] = []

        for row in fpl_teams.select("code", "name").to_dicts():
            code, name = row["code"], row["name"]
            if code is None:
                unresolved.append((code, name))
                continue
            key = str(int(code))
            pl_team = pl_by_id.get(key)
            if pl_team is None:
                unresolved.append((int(code), name))
                continue
            fpl_code_to_pl_id[int(code)] = key
            pl_id_to_fpl_code[key] = int(code)
            if _normalise_team_name(name) not in _normalise_team_name(pl_team["name"]) and _normalise_team_name(
                pl_team["name"]
            ) not in _normalise_team_name(name):
                name_mismatches.append((int(code), name, pl_team["name"]))

        if unresolved:
            raise IdentityError(
                f"{len(unresolved)} FPL team(s) could not be resolved to a Premier "
                f"League API team id (blueprint §12.5 — unmatched entities raise): "
                f"{unresolved}. Either the PL API team-id-equals-FPL-code equivalence "
                "does not hold for these clubs, or the PL fixture payload used to "
                "build this map did not include them (e.g. wrong season/matchweek)."
            )
        if name_mismatches:
            # Sanity signal only — the numeric id match above is authoritative.
            # A mismatch here means the id-equivalence *coincidentally* landed on
            # the wrong club, which would be far worse than a naming-convention
            # difference, so this is loud (warning, not silent) without being
            # fatal on its own.
            logger.warning("team identity: id matched but name comparison was inconclusive for %s", name_mismatches)

        logger.info("team identity map built and verified: %d/%d FPL teams resolved (100%%)", len(fpl_code_to_pl_id), fpl_teams.height)
        return cls(fpl_code_to_pl_id=fpl_code_to_pl_id, pl_id_to_fpl_code=pl_id_to_fpl_code)

    def resolve_fpl_to_pl(self, fpl_team_code: int) -> str:
        key = int(fpl_team_code)
        if key not in self.fpl_code_to_pl_id:
            raise IdentityError(
                f"unresolved team: FPL team code {fpl_team_code} has no matching "
                "PL API team id in this map (blueprint §12.5)."
            )
        return self.fpl_code_to_pl_id[key]

    def resolve_pl_to_fpl(self, pl_team_id: int | str) -> int:
        key = str(pl_team_id)
        if key not in self.pl_id_to_fpl_code:
            raise IdentityError(
                f"unresolved team: PL API team id {pl_team_id} has no matching "
                "FPL team code in this map (blueprint §12.5). This is expected "
                "for a club not in the current season's 20 (e.g. a relegated "
                "club referenced by a historical fixture) — this map is built "
                "from one season's fixture list, not a full historical registry."
            )
        return self.pl_id_to_fpl_code[key]


def build_team_identity_map_for_season(
    season: str,
    *,
    current_season: str,
    pl_teams: list[dict],
    live_teams: pl.DataFrame | None = None,
    archive_teams_fetch: Callable[[str], pl.DataFrame] | None = None,
) -> TeamIdentityMap:
    """Build a `TeamIdentityMap` against the FPL-side team snapshot valid AT
    `season` — the team half of blueprint §12.5's "identity maps are
    season-scoped", symmetrical to `build_player_identity_map_for_season`
    above (see this module's docstring for why this was needed and how it
    was found, E2b story 7b).

    `season == current_season`: uses `live_teams` — the live FPL
    `bootstrap-static` `teams` snapshot, exactly as every call site did
    before this story. Any OTHER season: defers to `archive_teams_fetch`,
    a plain `callable(season) -> pl.DataFrame` returning that season's
    `team.identity@season` rows (e.g. `providers.vaastav.VaastavProvider
    .fetch(TEAM_IDENTITY_SEASON, season=season).rows`) — again a callable,
    not a provider instance, so this module keeps importing nothing from
    `providers/`.

    `pl_teams` is NOT chosen by this function, unlike the FPL-side
    snapshot — it must already be the raw PL API team list extracted from
    THAT SAME season's own matchweek-1 fixtures (see this module's
    docstring's note on why team identity needs both sides matched, not
    just the FPL side). Passing a `pl_teams` from a different season than
    `season` silently reintroduces the exact mismatch this function exists
    to close; this function cannot detect that misuse (it has no
    independent way to know which season a bare `pl_teams` list came
    from), so the caller carries that obligation.

    Raises `IdentityError` if the caller didn't supply the argument this
    particular `season` needs. Both branches still end at
    `TeamIdentityMap.build()`, which is where the ACTUAL per-row
    verification/raise (§12.5's "unmatched entities raise") happens; this
    function's own job is only "pick the right FPL-side snapshot", never
    "decide whether a row resolves".
    """
    if season == current_season:
        if live_teams is None:
            raise IdentityError(
                f"season {season!r} is the CURRENT season ({current_season!r}) but no "
                "live_teams snapshot was supplied. Pass the live FPL bootstrap-static "
                "'teams' DataFrame, e.g. FPLProvider(...).fetch(TEAM_ATTRIBUTES_CURRENT)"
                ".rows."
            )
        return TeamIdentityMap.build(live_teams, pl_teams)

    if archive_teams_fetch is None:
        raise IdentityError(
            f"season {season!r} is not the current season ({current_season!r}) — resolving "
            "team identity for it needs a season-scoped snapshot, which the LIVE FPL API "
            "cannot supply (no historical-teams endpoint exists). Pass archive_teams_fetch, "
            "a callable(season) -> pl.DataFrame returning that season's team.identity@season "
            "rows (e.g. VaastavProvider.fetch(TEAM_IDENTITY_SEASON, season=season).rows)."
        )
    return TeamIdentityMap.build(archive_teams_fetch(season), pl_teams)


# -- cross-season team-name canonicalisation (session s003, E5 team-strength) -


@dataclass(frozen=True)
class TeamNameCanonicalisationMap:
    """Maps a `(season, archive-name)` pair to ONE canonical name per club,
    joined internally on FPL's own cross-season-STABLE team `code` — closes
    a real, live-verified bug: `docs/store/vaastav_team_identity` shows FPL's
    OWN team-name STRING for one club can drift across seasons even though
    `code` never does. Verified live 2026-08-22: vaastav's 2024-25 archive
    has `code=40, name="Ipswich"`; the live 2026-27 `bootstrap-static` has
    `code=40, name="Ipswich Town"` — same club (Ipswich were relegated after
    2024-25 and promoted straight back for 2026-27), same provider lineage
    (both are FPL's own naming, one archived by vaastav, one fetched live),
    genuinely different strings. A full 7-season x 20-team sweep of this
    store (140 rows) found this is the ONLY such divergence — every other
    code's archived name is identical across every season it appears in,
    and every other code absent from the live 20 is absent because that
    club is relegated/not yet returned, not because it was renamed (see
    `docs/wiki/model-team-strength.md` §4's live evidence table).

    This is deliberately NOT a §12.5 cross-PROVIDER identity resolution
    (that is `TeamIdentityMap`: FPL `code` <-> the PL API's own numeric team
    id). It is a cross-SEASON canonicalisation WITHIN one provider's own
    naming convention, needed because `fplai.models.team_strength` keys its
    internal attack/defence parameters on the team NAME string, not a
    numeric id (kept that way deliberately — see that module's docstring —
    so this fix canonicalises the STRING, rather than switching the model's
    internal representation to an id, which would be a much larger change
    for the same correctness gain).

    Built from EVERY season's own `team.identity@season` archive rows, not a
    single season — a name-only join across seasons is exactly the bug this
    closes (blueprint §12.5: "identity is bitemporal... resolve as of the
    season being processed, never today's"). `(season, name) -> code` is
    therefore complete for any season the archive covers, by construction
    (`TEAM_IDENTITY_SEASON`'s own entity key is `(season, id)`, and `code`
    is one of its required fields).

    A `(season, name)` pair genuinely absent from the archive this map was
    built with `.canonicalise()`s to itself, UNCHANGED — this is a
    grouping aid for a statistical fit, not a §12.5 identity-resolution
    guarantee a caller then trusts as a match, so it degrades gracefully
    ("no worse than the pre-fix behaviour for that one name") rather than
    raising. Contrast `TeamIdentityMap`/`PlayerIdentityMap`, which DO raise
    on an unresolved entity — those resolve an entity a caller then treats
    as ground truth; this one only relabels a training-data pooling key.

    `code_to_canonical_name` picks ONE display name per code: the LIVE
    snapshot's name where supplied and the club is in the current season's
    20 (so a caller who calls `predict_scoreline(params, "Ipswich Town",
    ...)` gets a fit whose training data was pooled under that exact same
    key), else the MOST RECENT archived name for that code (by season
    string — FPL's own "YYYY-YY" seasons sort correctly as plain strings,
    since every season spans exactly one calendar-year boundary and the
    format never changes width).

    Building this map is an explicit, caller-visible step (`.build()`,
    called at the CALL SITE — e.g. `scripts/fit_team_strength.py` — never
    fetched implicitly inside `fit_team_strength`/`build_match_table`
    themselves, which stay pure functions of exactly the arguments a caller
    passes). This matches the discipline `teams=` already follows there:
    blueprint §3.2's leakage rule is about an implicit "read today's
    snapshot" INSIDE a function that also serves historical backtests: this
    keeps that decision at the caller, visible, not buried in the model.
    """

    season_name_to_code: dict[tuple[str, str], int]
    code_to_canonical_name: dict[int, str]

    def canonicalise(self, season: str, name: str) -> str:
        code = self.season_name_to_code.get((season, name))
        if code is None:
            return name
        return self.code_to_canonical_name.get(code, name)

    @classmethod
    def build(
        cls,
        team_identity_rows: pl.DataFrame,
        *,
        live_teams: pl.DataFrame | None = None,
    ) -> "TeamNameCanonicalisationMap":
        """`team_identity_rows`: `vaastav_team_identity`'s own rows, ANY
        number of seasons (typically every season the store holds — see
        this class's docstring on why a single season is not enough).
        `live_teams`: the live FPL `teams` snapshot, optional — omit it to
        canonicalise purely within the archive (every code's most-recent
        archived name), e.g. for a historical-only backtest that should
        never reference today's naming."""
        required = ("season", "code", "name")
        missing = [c for c in required if c not in team_identity_rows.columns]
        if missing:
            raise IdentityError(
                f"team_identity_rows is missing column(s) {missing} needed for "
                "cross-season team-name canonicalisation — expected "
                "TEAM_IDENTITY_SEASON's own shape (vaastav_team_identity)."
            )

        season_name_to_code: dict[tuple[str, str], int] = {}
        latest_name_by_code: dict[int, tuple[str, str]] = {}  # code -> (season, name); keeps the max season seen
        for row in team_identity_rows.select("season", "code", "name").to_dicts():
            season, code, name = row["season"], row["code"], row["name"]
            if season is None or code is None or name is None:
                continue
            code_int = int(code)
            season_name_to_code[(season, name)] = code_int
            prev = latest_name_by_code.get(code_int)
            if prev is None or season > prev[0]:
                latest_name_by_code[code_int] = (season, name)

        code_to_canonical_name = {code: name for code, (_season, name) in latest_name_by_code.items()}

        if live_teams is not None:
            missing_live = [c for c in ("code", "name") if c not in live_teams.columns]
            if missing_live:
                raise IdentityError(
                    f"live_teams is missing column(s) {missing_live} needed for "
                    "cross-season team-name canonicalisation."
                )
            for row in live_teams.select("code", "name").to_dicts():
                if row["code"] is None or row["name"] is None:
                    continue
                code_to_canonical_name[int(row["code"])] = row["name"]

        return cls(
            season_name_to_code=season_name_to_code,
            code_to_canonical_name=code_to_canonical_name,
        )


# -- matches ---------------------------------------------------------------


def resolve_match(
    fixtures: pl.DataFrame,
    *,
    kickoff_date: date,
    home_team_code: int,
    away_team_code: int,
) -> str:
    """Resolve a fixture to its PL API `match_id` by (kickoff date, home
    team, away team) — blueprint §12.5's third identity, done AFTER teams
    resolve (both team codes here are already-resolved FPL codes, exactly
    what `match.fixtures@matchweek` rows carry — see schemas.py). `fixtures`
    is that capability's canonical rows (one or more matchweeks' worth).

    Raises `IdentityError` on zero or more-than-one match — an ambiguous
    resolution is exactly as unsafe as no resolution (§12.5: never guess).
    """
    required = ("match_id", "kickoff", "home_team_code", "away_team_code")
    missing_cols = [c for c in required if c not in fixtures.columns]
    if missing_cols:
        raise IdentityError(f"fixtures frame is missing column(s) {missing_cols} needed to resolve a match")

    candidates = fixtures.filter(
        (pl.col("kickoff").dt.date() == kickoff_date)
        & (pl.col("home_team_code") == home_team_code)
        & (pl.col("away_team_code") == away_team_code)
    )
    if candidates.height == 0:
        raise IdentityError(
            f"unresolved match: no fixture on {kickoff_date} with home_team_code="
            f"{home_team_code}, away_team_code={away_team_code} (blueprint §12.5)."
        )
    if candidates.height > 1:
        raise IdentityError(
            f"ambiguous match: {candidates.height} fixtures on {kickoff_date} with "
            f"home_team_code={home_team_code}, away_team_code={away_team_code} — "
            "never guessing which one (blueprint §12.5)."
        )
    return str(candidates["match_id"][0])


@dataclass(frozen=True)
class IdentityResolver:
    """The three identities this story resolves, bundled for convenience.
    `providers/pl.py` takes one of these at construction rather than
    threading `PlayerIdentityMap`/`TeamIdentityMap` separately through
    every method."""

    players: PlayerIdentityMap
    teams: TeamIdentityMap

    @classmethod
    def from_fpl_snapshots(cls, elements: pl.DataFrame, teams: pl.DataFrame, pl_teams: list[dict]) -> "IdentityResolver":
        return cls(players=PlayerIdentityMap.build(elements), teams=TeamIdentityMap.build(teams, pl_teams))
