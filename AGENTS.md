# FPL-AI

A rank-aware Fantasy Premier League decision system: statistical models produce
distributions, a MILP solver optimises squad and transfers over a rolling horizon, and LLMs
are confined to news extraction and explanation.

**[docs/HANDOFF.md](docs/HANDOFF.md) is where a new session starts — current state,
time-critical deadlines, open bugs, and the lessons that cost real debugging time.**
**This file is the operating manual — how we work.**
**[docs/BLUEPRINT.md](docs/BLUEPRINT.md) is the design — what we're building and why.**
**[docs/wiki/dispatch-protocol.md](docs/wiki/dispatch-protocol.md) is how a story is dispatched —
read it before writing any brief.**
Read the handoff, then the blueprint, before doing anything substantive.

---

## Operating context

- **Live season 2026/27, in progress.** The user plays while the system is built.
- **Target: top 10% overall.** Moderate aggression — the career baseline is the 24th
  percentile, so top-10k is not a plan. Full reasoning: blueprint §10.
- **Two tracks.** Track A is manual weekly decisions plus EO snapshotting; Track B is the
  phased system. **Track A output is never presented as system output.**
- **Knowledge boundary.** Training data ends May 2026; the season is 2026/27. Squads,
  transfers, prices and rules are all outside it. Fetch, or ask `fpl-elite` — never answer
  football questions from recall.

---

## Non-negotiable rules

Load-bearing. Working around these produces code that runs and results that lie.

1. **LLMs never touch the numbers.** Prediction is statistical models; optimisation is the
   solver. LLMs do news extraction, explanation and scenario framing. Never write a path
   where a language model selects players or computes points.
2. **Bitemporal integrity.** Every fact carries a valid time and an `observed_at`. Nothing
   updates in place. Training and backtest queries resolve *as of a deadline*. Reading
   present-day prices, ownership or injury flags into a historical context is leakage — it
   does not crash, it silently invalidates everything downstream.
3. **Modelled data is labelled as modelled.** Captaincy backcasts especially must never be
   queryable in a way that lets them pass as observation. Schema requirement, not
   convention.
4. **Nothing hardcoded.** Prices, budget, chip inventory, scoring, squad limits, DC
   thresholds — all read from live config. 2026/27 already differs from 2025/26 in several
   ways; the current values are in blueprint §11.
5. **Models emit distributions.** A scalar `xPts` at a module boundary is a design error.
6. **Phase gates are not optional.** A failing gate stops work and goes to the Architect.
   It is never patched around.
7. **Deterministic and seeded.** Every result reproducible from a commit hash plus a seed.

### FPL API discipline

2 req/s, single connection, ±20% jitter, realistic User-Agent, exponential backoff on
429/403, full response caching. There are no rate-limit headers and the ceiling is unknown.
**Do not probe it.**

### The one irreversible deadline

Ownership snapshots and post-gameweek EO samples **cannot be backfilled** — FPL re-issues
entry IDs annually, so captaincy has no archive and no recovery path. Every unsampled
gameweek is a permanent hole in the only dataset that can calibrate the captaincy model.
Evidence and sampling design: blueprint §3.4.

---

## Agents

Coordination is **convention-enforced**, not tooling-enforced. No lock daemon, no punch-in
scripts. The Architect enforces it. Formalise only if collisions actually occur.

The **Architect** is the coordinating ChatGPT/Codex thread. It plans, sets scopes and owned paths,
maintains the blueprint and wiki, and is the **only entity that spawns agents**. It does not
write production code unless explicitly told to. It has no `.codex/agents/architect.toml` file because it
is not a subagent — giving it one would permit exactly the recursion the roster forbids.

| Agent | Codex routing policy | Lane |
|---|---|---|
| `fpl-s-coder` | Moderate reasoning, workspace-write | Mechanical, fully-specified coding. Graphify maintenance. Escalates design-bearing work with the plan verbatim |
| `fpl-xl-coder` | High reasoning, workspace-write | Design-bearing implementation, wiki, skills. Consumes S-Coder's graphify rather than regenerating it |
| `fpl-data-scout` | High reasoning, research-first | Source discovery and verification. Reports; does not build |
| `fpl-elite` | High reasoning, research-first | Football knowledge. Cited, tiered, never from memory. Knowledge only, never code |
| `fpl-debugger` | High reasoning, read-only sandbox | Root cause analysis. Diagnoses; does not fix unless told |
| `fpl-scrum-master` | High reasoning, scoped workspace-write | **Post-phase only.** Audits the record against evidence when a phase gate is claimed passed, returns questions on items still open, writes the phase summary, and may **withhold** the phase-done mark. Owns `docs/retro/`. Never codes, never designs, never grooms |

**No agent spawns another.** No cascading, no sub-sub-agents. This is the most important
rule here — it is what keeps agent count and token burn bounded. It is enforced structurally:
Codex custom-agent instructions forbid recursive project dispatch; only the coordinating Architect may delegate project work.

### Routing

Route by **task class**, not by file count — a one-file change can be the hardest change in
the codebase.

| Task class | Goes to |
|---|---|
| Mechanical, fully specified, no design latitude — rename, port, boilerplate, test scaffolding | `fpl-s-coder` |
| Design-bearing — new module, interface choice, non-obvious tradeoff, cross-cutting change | `fpl-xl-coder` |
| Ambiguous, scope unclear, or blueprint-affecting | Architect handles it |
| Story grooming, estimation, progress tracking | Architect — never delegated |
| **A phase gate claimed passed** — audit, phase summary, phase-done mark | `fpl-scrum-master` |

**If a brief needs design decisions pinned into it, the task is not mechanical** — it goes to
`fpl-xl-coder`, never `fpl-s-coder`. Added after s006, where a story carrying three pinned
decisions and twenty-two files was routed to the Haiku lane on the strength of the word
"mechanical" in a planning note. The tell was visible before the dispatch: the brief had a
DECISIONS section.

### Before any dispatch — three rules that bind first

Full protocol in [docs/wiki/dispatch-protocol.md](docs/wiki/dispatch-protocol.md). These three
apply before anyone opens it:

1. **Probe before brief.** Run a read-only probe against the real store and paste its output into
   the brief verbatim — `scripts/probe_store_shape.py` for the usual questions. In s006 a design
   decision was pinned from reading code rather than running it, and was wrong in a way thirty
   seconds of execution would have exposed; it was found after seven modules had implemented it.
2. **Numbers come from generators, never from prose.** `docs/HANDOFF.md` and `PROGRESS.md` are
   indexes, not citation sources. A gate quoted from the handoff's summary table was one fix
   stale, and a *correct* agent result was nearly recorded as a failure.
3. **Checkpoint before dispatch.** Commit on a story branch before any agent writes, so rollback
   costs one file instead of the whole story.

**Commits are local restore points; pushing is a phase-close act.** Commit freely during a phase,
**push only at post-Sprint** once the phase gate has passed. Push, PR and merge timing are the
Architect's and the Scrum Master's call — standing authority, granted 2026-09-01, exercised
without asking and reported afterwards. **This applies to the Architect only**: agents still never
commit, push, or touch git working-tree state unless a brief says so.

And afterwards: **a claim is not evidence; pasted output is.** The Architect runs the gate, or
the report carries the raw output and the command that produced it. Anything else is recorded as
*not done*. Four claimed passes in s006 were each checkable in under a minute, and none held.

### Grooming and estimation — Architect only

**A sprint is a phase.** Not a date range. The current sprint runs until its phase gate passes.

The Architect grooms stories into dispatchable tasks and estimates each in Fibonacci, measuring
**compound difficulty** — scope plus uncertainty plus blast radius — not hours. Anchors,
calibrated on work already done: **1** one mechanical file · **3** one module, known shape, one
gate (`scripts/check_edit_integrity.py`) · **5** a new interface or non-obvious tradeoff
(`src/fplai/scoring.py`) · **8** cross-cutting, several files, real design latitude.

**1-8 is dispatchable. 13 and 21 are not tasks** — they are grooming artefacts and must be split
before any agent sees them. The s006 migration was dispatched as if it were a 3; it was a 13, and
that gap is the entire incident.

**Grooming is incremental.** A 13 does not need breaking down in one sitting: split off the first
3 or 5, dispatch it, and let the pilot re-shape the rest. Grooming ahead of evidence is how a
wrong pinned decision gets built seven times.

The Architect writes `PROGRESS.md` as work proceeds — register, checkpoint, close — using the
punch-card as its evidence base. The Scrum Master is not in this loop; it arrives after the phase
gate, cold, to check whether the record is true.

### Scoping is the safety mechanism

Timestamps record that a collision *happened*. Path ownership *prevents* it. Every brief
carries:

```
OWNED PATHS:   src/models/minutes/**, tests/models/test_minutes.py
READ-ONLY:     src/store/**, docs/BLUEPRINT.md
FORBIDDEN:     everything else
```

An agent needing to write outside its owned paths **stops and reports**. It never widens its
own scope.

**`FORBIDDEN` means do not EDIT, not do not IMPORT.** Clarified 2026-08-22 after a coder
correctly flagged the ambiguity: `scripts/pin_dc_thresholds.py` genuinely needs
`fplai.client` (nothing persists `event/{gw}/live/` to the store), but the brief had listed
that file as `FORBIDDEN`. Importing a stable public interface unmodified is always allowed —
that is what the interface is for, and `fplai.providers.fpl` already does it. Only *writes*
are scoped. If a brief means "do not depend on this at all", it must say so explicitly; the
default reading is edit-scope. Prefer listing importable modules under `READ-ONLY` so the
question does not arise. Once more than one coder agent runs in parallel on real code, they run in **git
worktrees** so they physically cannot stomp each other; until then, disjoint owned paths are
sufficient.

### Standing rules for every agent

- Never edit `docs/BLUEPRINT.md` — Architect only. Contradictions are escalated as findings.
- Never skip or patch around a phase gate.
- **Never bulk-rewrite source files with a generated script.** Edits are per-file and targeted.
  In s006 a repair script deleted five top-level definitions from `saves.py` — including
  `build_training_table` — and five more from `cards.py`. A targeted edit cannot delete a
  function it never named. Run `scripts/check_edit_integrity.py` after any multi-file change.
- **Every file read and write specifies `encoding="utf-8"`.** The default on this Windows
  checkout is cp1252, and one unspecified `open()` silently corrupts a file *while leaving it
  valid Python that imports cleanly* — two s006 files carried 309 and 201 mojibake sequences past
  a syntax check, an import, and a human diff review, into strings that are persisted to the
  store. Console output from scripts stays ASCII for the same reason.
- **Report what you ran, not what you expect.** Paste the command and its summary line, or say
  plainly that the gate was not run. "Expected to complete green" is not a result.
- **Run the measured command ALONE, and read its own exit status.** The reported status of a
  compound command belongs to its *last* element, never to the one you care about. This has now
  produced a false green three times, in three different costumes: a pipeline through `tail`
  (s003), the same again (s006), and in s007 a **semicolon chain** —
  `python script.py > out 2>&1; echo "exit=$?"; tail out` — where the harness reported the whole
  chain's status, i.e. `tail`'s `0`, for a run that had died with a `ScoringError`. The earlier
  wording of this rule said "never pipe", which is why the third one got through: it wasn't a
  pipe. **Pipes, `;` chains, `&&` chains and background wrappers all have this property.** Run the
  thing, then echo `$?` on its own line — and when a background task reports success, read the
  output before believing it.
- **Punch-card timestamps are `datetime.now(timezone.utc)`** — never composed, estimated, or
  rounded. Two s006 `punch_out` events were stamped hours into the future, which makes any
  ordering analysis over an append-only log meaningless.
- Never commit, push, or install dependencies unless the brief explicitly says to. **This
  extends to all git working-tree state** — no `stash`, `checkout`, `reset`, or `clean`, even
  transiently. In s002 a `git stash push -- <two files>` intended to revert one agent's own
  edits silently reverted *another story's* uncommitted work on the same files; it was caught
  and recovered, but nothing would have flagged it. To test against unpatched behaviour, copy
  the file aside and restore it — never move the working tree beneath work you did not write.
- Never read or print secrets. Load keys from the environment without echoing them. **This
  extends to libraries you did not write** — `urllib3`'s DEBUG logger printed a full API key
  from a query string in s002. Silence third-party loggers explicitly; do not assume your own
  discipline covers the stack beneath you.

**Read an aggregator's last-updated stamp before its contents.** Third occurrence in two
sessions: an ESPN injury page asserted a doubt on Gabriel that would have altered the squad — the
page's own stamp read **4 December 2024**, and it still listed Guimarães at Newcastle. Injury
aggregators rank well and go stale silently. A claim from an undated or stale-dated page is not
evidence, whatever tier the outlet would otherwise carry.

### Time zones — the user is GMT+8

**Every human-facing time is Asia/Kuala_Lumpur (GMT+8, no DST). Every stored time is UTC.**
The two never mix, and the boundary is exact:

| Layer | Zone | Examples |
|---|---|---|
| **Prose** — all `.md` files, agent reports, recommendations, anything a human reads | **GMT+8** | "deadline 01:30 Sat", "presser at 20:30 tonight", "in 2 days" |
| **Data** — store columns, `observed_at`/`valid_at`, punch-card `ts`, API payloads, log lines | **UTC** | `2026-08-21T06:26:24Z`, `_require_utc()` |

Where an external fact is natively UTC (FPL deadlines are UK time), **lead with local and put UTC
in parentheses**: `01:30 Sat 22 Aug (17:30 UTC Fri)`. Never UTC alone.

**Relative periods are computed in GMT+8.** "Tonight", "in 30 minutes", "in two days", "before
the deadline" are judged against the user's clock, not UTC and not the server's. This is the part
that actually broke: an instruction to capture odds "at 16:30-17:00 UTC" was **00:30-01:00 local**,
the middle of the night. It was run immediately instead, 19 minutes after the previous capture,
and the price-movement signal was lost.

**FPL deadlines are ~01:30 local.** Most deadline-adjacent work is therefore overnight for the
user and should be **scheduled, not asked of them manually**. Recommending a 2am manual run is a
failed recommendation regardless of how correct the reasoning behind it was.

**Green is not done; live is done.** Any task touching **credentials, timestamps, or a
provider's response shape** requires a live verification run before punch-out —
`scripts/verify_<provider>.py` or equivalent — and the punch-out must state what was verified
live versus assumed.

This is a gate, not advice. Six bugs across sessions s001-s002 passed a fully green mocked
suite and were caught only by a real call: an identity join on the wrong FPL column (resolved
0/42 names), a tz-aware write no schema could reject because `validate()` checks presence not
dtype, an API key printed by a third-party logger, and three store bugs before them. Fixtures
reuse the same integers for `id` and `code`, carry whatever dtype you constructed, and never
log anything. They cannot catch this class of bug **by construction** — which is the same
reason `tests/test_store_invariants.py` runs against the real store.

**A test that asserts against a literal date or gameweek is a scheduled false alarm.** Three
were found in session s004 alone. One hardcoded `--gw 1` and expired the moment GW1 settled.
One used a player name as its stand-in for "unknown" that became resolvable when the resolver
improved. The third read back with `until=dt(2026, 8, 28)` and **failed at midnight,
mid-session, between two full-suite runs, with no code change at all** — the only intervening
edits were in a subsystem that cannot touch that path. Derive bounds from the clock or from the
store, never from a literal. The cost is not the failure; it is that a suite which cries wolf
on the calendar trains you to stop reading it.

**Measure the size of the prize before building the plumbing.** Blueprint §4 asserted "referee
assignment is a real feature" from football priors. It sat unmeasured for ten days and drove a
data-ingest story. Measured, it moves the cards model's log-loss by **+0.0002** — and a planned
announcement-time capability was cancelled on that evidence. A plausible prior in a design
document is not evidence; label it as unmeasured and, where a measurement is cheap, take it
before committing to the work it implies.

**Check the branch immediately before every commit, and stage paths explicitly.** In s004 the
Architect ran `git add -A && commit && push` on a "commit and push" instruction and landed
directly on `master`, because the user had merged a PR mid-session and moved the checkout. The
branch you were on an hour ago is not the branch you are on now, and `git add -A` will happily
sweep in whatever else is sitting in the tree.

**A guarantee is not proven by a test that follows the sanctioned path.** When a story claims
something is *impossible* — a value cannot be queried, a row cannot be joined, a secret cannot
be written — the claim is only earned once someone has **attacked it from outside the intended
call path** and failed.

This is a different failure mode from the one above, and the live-verification gate does not
cover it. Story 9 shipped a break-first proof that its mechanism worked, and the mechanism did
work: derived facts written through `write_derived` could not leak. The guarantee still had a
hole, because nothing stopped a caller reaching `store.write()` directly — found by trying it,
not by testing it. **"The sanctioned path is safe" is not the standard; "no path is unsafe"
is.**

The tell, per the s002 retro: the structural guarantees that shipped sound this session all
carried a break-first-then-fix proof; the one that shipped with a hole did not, until it was
supplied after the fact. If a brief claims a guarantee, the punch-out states what was attacked.

---

## Punch-card

Append-only JSONL at `.punchcard/<session-id>.jsonl`. One event per line, ISO-8601 UTC.
Never rewritten, never merge-conflicts, trivially greppable.

| Event | When |
|---|---|
| `punch_in` | Agent starts. Records role, task, owned paths |
| `finding` | Anything learned that outlives the task |
| `action` | Files touched, commands run, decisions made |
| `blocked` | Needs Architect input. Includes what and why |
| `punch_out` | Agent finishes. Includes summary and gate status |

```json
{"ts":"2026-08-19T16:40:12Z","agent":"xl-coder","event":"punch_in","task":"phase-0 ingest","owned_paths":["src/ingest/**"],"session":"s001"}
```

Reading is always allowed — any agent may check whether another is working nearby. Writing
is append-only.

**Architect obligations:** validate punch-ins and punch-outs and **append missing entries
itself** — the log must be complete even when agents are sloppy; read the log before
spawning a parallel agent to confirm no path overlap; promote durable `finding` events into
`docs/wiki/`; write an end-of-session summary event.

---

## Where things live

| Path | Purpose | Who writes |
|---|---|---|
| `AGENTS.md` | Operating manual — this file | Architect |
| `docs/BLUEPRINT.md` | Design, scope, phase gates. Changes need an amendments-table entry | **Architect only** |
| `PROGRESS.md` | Epic to story tracker with estimates. **Short by mandate** — one line per story | **Architect.** Scrum Master appends the phase summary and sets the phase-done mark at phase close, nothing else |
| `docs/retro/` | One per phase close. Open questions, stale knowledge, feedback, verdict | Scrum Master |
| `docs/HANDOFF.md` | Session state, deadlines, open bugs, hard-won lessons | Architect |
| `docs/wiki/` | Durable knowledge — sources, model decisions, calibration results | XL-Coder, Data Scout, Elite |
| `.punchcard/` | Session event stream | All agents, append-only |
| `.codex/agents/` | Subagent definitions | Architect |
| `.env` | **Secrets. Never read, print, or commit.** Gitignored | User |
| `.env.example` | Key *names* only, no values. Committed | Anyone |

---

## Conventions

Python + `uv` · Polars · DuckDB over Parquet · HiGHS via `highspy` · FastAPI · Next.js
(Phase 8, not before). Prefer boring and obvious over clever. Match the surrounding code's
naming, idiom and comment density.
