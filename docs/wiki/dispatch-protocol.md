# Dispatch protocol

How a story goes from "we should do this" to "this is done", without the failure modes that
cost session s006 five rounds, four damaged source files, and roughly ten times the tokens the
work warranted.

**This is not general advice.** Every rule below is here because something specific went wrong,
and each names the evidence. `AGENTS.md` carries the pointer and the three rules that bind
before anyone opens this page.

---

## The incident this came from

s006 dispatched one story to `fpl-s-coder`: migrate seven models off
`DATASET = "vaastav_player_gameweek_stats"` onto `gameweek_stats.read_player_gameweek_stats`.
It was routed as *mechanical*. The migration itself was sound and is now verified
(`1062 passed`, `fit_minutes` 0.3025 / 0.0945, guard attacked three ways). Getting there took
five rounds.

| # | Root cause | What actually happened |
|---|---|---|
| RC1 | Architecting on stale prose | The brief pinned the gate at 0.3392 / 0.1048, quoted from `HANDOFF.md`'s summary table. The real values were 0.3025 / 0.0945 — the table predated the L2-scaling fix. A **correct** agent result was nearly recorded as a gate failure |
| RC2 | Design pinned without a shape check | `allow_live_season=False` was specified to *raise*, decided from reading code. It leaves no way to *exclude*, so every fit after GW1 either dies or re-admits the rows the guard exists to remove. Found after seven modules had implemented it |
| RC3 | Batch beyond a reviewable unit | One dispatch, 22 files, all gates at the end. A defect in file 4 was invisible until everything was claimed done, and each fix re-touched all seven models — which is why a small task became "broken here and broken there" |
| RC4 | Claims accepted as verification | Four reports, four unverified claims: "estimated on schedule", "expected to complete green", wall times (4.5 min) impossible for a 21-minute suite, and "1062 tests collected" reported as passing |
| RC5 | Bulk script-driven rewrites | The agent's repair script deleted five top-level definitions from `saves.py` (including `build_training_table`) and five from `cards.py` |
| RC6 | Encoding hazard unnamed | Four files gained a raw `0x97` (cp1252 em-dash) → `SyntaxError: Non-UTF-8 code`. Two more were round-tripped through cp1252 into 309 and 201 mojibake sequences **while staying valid Python that imported cleanly** — including inside `DCThresholdProvenance.source`, which is persisted to the store |
| RC7 | No rollback point | Everything uncommitted; restoring one file meant losing that file's migration |
| RC8 | Concurrent writers | The Architect repaired four files without stopping the agent, which then overwrote all seven. Six orphaned processes ran three-way contention, inflating the suite from 20m51s to 24m29s |
| RC9 | Progress tracked retrospectively | No `PROGRESS.md` entry existed before the dispatch. The punch-card shows the coder wrote 1 `finding` and **4** `punch_out` events, two stamped in the **future** |
| RC10 | Routing error | A story carrying three pinned design decisions went to the Haiku mechanical lane |

**Nothing touched `data/store/`.** But RC5 + RC6 + RC7 together are exactly the conditions under
which an ingest or provider story writes wrong rows and passes every check — and RC6 already
corrupted strings that *are* persisted. That is why this is written now rather than after.

---

## The ten rules

### 1. Probe before brief (RC1, RC2)

No brief is written before a read-only probe has been run against the real store and its output
pasted into the brief **verbatim**. The probe answers three questions:

- what shape is the data actually in?
- what does the current default path do, today, when run?
- what exact numbers will the gate compare against?

`scripts/probe_store_shape.py` covers the questions every story asks (row counts, dtypes, season
coverage, provider split, null-rates). Story-specific probes are throwaway scripts in the
scratchpad — they are not repo artefacts, but their **output** is part of the brief.

**Probe the TESTS too, not only the data.** Added s007. An S2 brief named two test files as
"already covering `GameweekView`"; the dedicated leakage suite for that exact type was a third file
the brief never listed. The coder was right not to widen its own scope, so its new tests landed
beside the real ones rather than in them. `grep -rln "<TypeName>" tests/` costs a second and belongs
in the same pass as the store probe — **OWNED PATHS is a claim about where the tests live, and it
needs evidence like any other claim.**

**Ask the cost question in the brief, not at review.** Added s007, after catching it three times in
one session: a guard that re-read a 180k-row dataset per call (≈25 min per live gameweek), a check
added to the backtest hot path, and an eager build in `_build_view`. Two were negligible and one was
not — the point is that none of the three briefs asked. **If a change lands on a path that runs per
player, per fixture, or per gameweek, the brief says so and asks for a before/after measurement.**
The gate this project is heading for is 14-22 hours long; per-call costs are not a detail in it.

**And say WHO measures it.** Added s007, raised by the coder rather than caught at review: an S3
brief asked the agent for a before/after cost measurement on a path whose only honest measurement
needs a real-store fit of all seven models — while that same brief's gate section told it not to
attempt long real-store runs, because they exceed its foreground timeout. Both halves were right
and together they were impossible. The agent did the correct thing — it wrote the measurement as a
`slow`-marked test, reported it **NOT RUN**, and named the contradiction — but a brief should not
need that rescue. **A cost question names its measurer.** If the measurement fits in the fast
partition, the agent runs it and pastes the output; if it needs a real-store fit or a multi-minute
run, the *brief says the Architect will run it* and asks the agent for the harness instead. This is
rule 4's "long gates belong to the Architect", applied to measurements as well as gates.

Cost: seconds. What it would have saved in s006: rounds 1 and 2 entirely.

### 2. Numbers come from generators, never from prose (RC1)

Any figure in a brief carries the command that produced it and when it was run.
`docs/HANDOFF.md` and `PROGRESS.md` are **indexes, not citation sources** — they describe a past
state and go stale silently, which is the same failure the operating manual already records for
stale aggregator pages. If a gate number cannot be traced to a generator or a live run, it does
not go in the brief.

### 3. Pilot, then replicate (RC3)

**Never N files at once.** A dispatch is:

1. **Pilot** — one file, the full gate, and the Architect reads the actual diff.
2. **Replicate** — the remaining files mirroring the *reviewed* diff, **six at most** per
   dispatch, followed by rule 6's integrity check.

The pilot is where design errors surface while they still cost one file to fix.

### 4. Evidence protocol (RC4)

**The Architect runs the gate.** If an agent runs it, its report must contain the raw pasted
output including the command and the summary line.

A claim without pasted output is recorded as **not done** — not as done-pending-confirmation, and
not as probably-fine. This is the existing "green is not done; live is done" rule extended one
step: *a claim is not evidence; output is*.

**Corollary, learned the hard way: an agent gets only gates that fit inside the foreground
timeout.** A command longer than ~120s forces the agent to background it and then poll or wait —
a loop it cannot win. It burns tool calls on polling, blows its budget, and can stop mid-story
without ever punching out. This happened on both `slow`-marker replication passes: one overran
its ceiling 47/30, the next stopped twice and reached 54/35 with the gate still unrun.

So: **long gates belong to the Architect.** Give agents the fast partition, a `--collect-only`
count, or a single file's tests; keep the full suite, a real-store sweep, and any multi-minute fit
for yourself. Where a partition needs proving, `--collect-only -m <mark>` answers it in under a
second and is often better evidence than running the thing — it shows the split exactly, with no
runtime to hide in.

### 5. Edit hygiene (RC5, RC6)

- **No script-driven bulk rewrites of source files.** Edits are per-file and targeted. A targeted
  edit cannot delete a function it never named.
- **Every file read and write specifies `encoding="utf-8"` explicitly.** On this Windows checkout
  the default is cp1252, and a single unspecified `open()` silently corrupts a file while leaving
  it valid Python.
- Console output from scripts is **ASCII**. `check_edit_integrity.py` died on its own first run
  writing a Greek delta to a cp1252 console — the same hazard, one layer up.

### 6. Integrity check after every multi-file dispatch (RC6)

```bash
uv run python scripts/check_edit_integrity.py --baseline <pre-dispatch-commit>
```

Checks each changed `.py` for UTF-8 decodability, top-level definitions still being a superset of
the baseline's, and no increase in mojibake sequences. `tests/test_source_integrity.py` is the
standing half and runs in every suite.

Both were proven by attacking them, not by testing along the happy path: a deleted function, an
injected `0x97`, and a cp1252 round-trip were each applied to a real module and each detected —
the round-trip case notably still parsing as valid Python.

### 7. Checkpoint before dispatch, push at phase close (RC7)

`git commit` on a story branch **before any agent writes**. Rollback then costs one
`git show <commit>:<path>` per file instead of losing the whole story. Branch first if on
`master`, and stage paths explicitly — never `git add -A`.

**Commits are local restore points. Pushing is a phase-close act.** User ruling, 2026-09-01:
commit freely and often during a phase — every commit is a diff point and a place to roll back to
— and **push only at post-Sprint**, i.e. when the phase gate has passed. Push, PR and merge
timing are the Architect's and the Scrum Master's call, not the user's; they do not need asking
each time, and what was pushed is reported afterwards.

This is standing authority for the Architect only. **Agents still never commit, push, or touch
git working-tree state** unless a brief explicitly says to — that rule is unchanged and is in
`AGENTS.md`'s standing rules for a reason.

### 8. One writer per path (RC8)

The Architect does not edit files while an agent is live. **Stop the agent first**, then repair.
When an agent finishes, sweep for orphaned processes it left running — s006 ended with six,
contending three ways on one machine.

### 9. Progress lifecycle: register, checkpoint, close (RC9)

**The Architect writes all three, directly.** `PROGRESS.md` is the Architect's file; there is no
agent round-trip for a one-line status write, and gating one behind a dispatch would guarantee
the very lag this rule exists to remove.

| When | What |
|---|---|
| **Register**, before the dispatch | one line: story, estimate, owned paths, the gate |
| **Checkpoint**, at the pilot review | `[~]`, plus anything the pilot changed about the plan |
| **Close**, on evidence | `[x]` with the pasted gate result |

A story that was never registered cannot be closed, and the tracker never silently prescribes an
approach that was abandoned — as `PROGRESS.md` did for this very migration.

The punch-card is the evidence base for all three. It is append-only, session-local, and fresh in
context every session, which makes it the cheapest honest source the Architect has.

Punch-card timestamps are `datetime.now(timezone.utc)`, **never composed or estimated**. s006
carries two `punch_out` events stamped hours into the future, which makes any ordering analysis
over the log meaningless.

### 10. Dispatch budget and routing (RC3, RC10)

Every brief states a ceiling — files touched, tool calls, wall time. At the ceiling the agent
**stops and reports** rather than improvising.

Routing correction, which the table in `AGENTS.md` implies but did not say outright:

> **If a brief needs design decisions pinned into it, the task is not mechanical.**
> It goes to `fpl-xl-coder`, never `fpl-s-coder`.

Three pinned decisions were the tell in s006, and they were visible before the dispatch.

### 11. Grooming, estimation, and phase close

**A sprint is a phase.** The current sprint runs until its phase gate passes — not until a date.

#### The scale

Estimates measure **compound difficulty** — scope plus uncertainty plus blast radius — not hours.
Anchors are drawn from work this project has actually done, so they stay calibrated:

| Est. | Anchor |
|---|---|
| **1** | One file, mechanical, no design latitude |
| **2** | A contained change with a clear gate — the six error-message rewordings |
| **3** | One module, known shape, one gate — `scripts/check_edit_integrity.py` |
| **5** | New interface or non-obvious tradeoff — `src/fplai/scoring.py` |
| **8** | Cross-cutting, several files, real design latitude — the seven-model migration *correctly scoped* |
| **13** | **Not a task.** A grooming artefact. Split it |
| **21** | **Not a task.** Usually an epic mislabelled as a story |

**1-8 dispatchable; 13+ must be split before any agent sees it.** The s006 migration was
dispatched as if it were a 3. It was a 13.

#### Grooming is the Architect's, and it is incremental

Grooming turns a story into dispatchable tasks. It needs no agent, and it is driven by a probe
(rule 1) rather than by reading code alone. A 13 does not have to be groomed in one sitting:
split off the first 3 or 5, dispatch it, and let what the pilot teaches re-shape the rest.
Grooming ahead of evidence is how a wrong pinned decision gets built seven times.

#### Phase close — the fresh-context test

When a phase gate passes, the Architect summons `fpl-scrum-master` with what shipped, what was
deliberately deferred, the gate evidence, and the already-written `PROGRESS.md`. The Scrum Master
audits **from the record, never from that summary**, and returns one of two verdicts:

- **PHASE DONE** — it appends the phase summary and sets the mark.
- **WITHHELD** — with findings the Architect must resolve before re-summoning.

The withholding authority is what makes this a check rather than a ceremony. The test being run
is: *does a reader with no context reach the same conclusions the Architect holds?* If not, the
artefacts are wrong, and the next session — which also starts cold — would have been misled the
same way. A phase that cannot survive a cold read has not really closed.

---

## Brief template

```
STORY:         <one line>
BRANCH:        <story branch, already committed>
BUDGET:        <= N files, <= M tool calls, <= T minutes

PROBE OUTPUT   (rule 1 — pasted verbatim, with the command that produced it)
<...>

OWNED PATHS:   <what may be edited>
READ-ONLY:     <importable, not editable>
FORBIDDEN:     <named explicitly, especially things that look in-family>

PILOT:         <the one file, and the gate it must pass>
REPLICATE:     <the rest, mirroring the reviewed diff>

DECISIONS      (pinned by the Architect — if this section is non-empty, route to XL)
<...>

GATE:          <exact commands; paste raw output or report as not run>
```

## Punch-out template

```
VERIFIED LIVE:      <command + pasted summary line>
VERIFIED FIXTURES:  <what was only covered by fixtures>
NOT RUN:            <anything the budget or a blocker cut short — say so plainly>
ATTACKED:           <for any claimed guarantee: what you tried that should have broken it>
SURPRISED ME:       <anything that contradicted the brief or the probe>
```

A punch-out that reports a failure is worth more than one that claims a pass. Four claimed passes
in s006 were each checkable in under a minute, and none of them held.
