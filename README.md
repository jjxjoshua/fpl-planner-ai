# fpl-planner-ai

Rank-aware Fantasy Premier League decision system built around probabilistic statistical models, bitemporal data, and HiGHS/MILP optimisation.

## Architecture boundary

- Statistical models produce distributions/PMFs.
- HiGHS/MILP selects squads, transfers, captaincy, bench, and later chips.
- LLMs are restricted to news-to-structured-facts, explanation, and scenario framing.
- LLMs must never select players, calculate xPts/points, replace statistical models, or replace the optimiser.

`docs/BLUEPRINT.md` is the source of truth for scope, architecture, and phase gates.
`AGENTS.md` is the ChatGPT/Codex operating manual.
`docs/HANDOFF.md` is the current-session starting point.

## Codex project setup

Project-scoped worker definitions live in `.codex/agents/`.
Project MCP configuration lives in `.codex/config.toml`.
The legacy WebStorm MCP endpoint is left disabled until its local transport is verified.

No OpenAI SDK dependency is installed yet. The blueprint's News LLM phase has not started, so an OpenAI runtime will be added only when that phase is formally opened and gated.

## Development

Python 3.11+ with `uv`.

```text
uv sync
uv run pytest
```

Static dashboard:

```text
scripts\run_dashboard.bat
```

or directly:

```text
python -m http.server 8731 --directory src/fplai-dashboard
```