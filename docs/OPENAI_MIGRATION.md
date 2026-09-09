# OpenAI / Codex migration

## Status

Migrated from the reference package on 2026-09-10. The FPL numerical engine and blueprint architecture were intentionally left unchanged.

## Active instruction mapping

- `CLAUDE.md` operating manual -> `AGENTS.md`
- `.claude/agents/*.md` -> `.codex/agents/*.toml`
- `.claude/launch.json` -> `scripts/run_dashboard.bat` plus README instructions
- `.mcp.json` -> `.codex/config.toml` (WebStorm MCP kept disabled until verified)
- historical `.punchcard/*.jsonl` and `docs/retro/*` -> preserved unchanged

A small `CLAUDE.md` compatibility note remains so historical source comments such as `CLAUDE.md rule 2` still resolve without bulk-rewriting numerical source files. New instructions belong in `AGENTS.md`.

## Runtime AI status

There was no Anthropic SDK/client in the reference implementation and no Anthropic/OpenAI runtime dependency in `pyproject.toml`. The blueprint's News LLM phase is still unimplemented.

When that phase is formally opened, use the OpenAI Responses API with strict structured output for news extraction. Store source/provenance and treat extracted facts as modelled inputs. The OpenAI model must not select players, calculate points/xPts, replace probabilistic prediction models, or replace HiGHS/MILP optimisation.

`OPENAI_API_KEY` and the OpenAI Python SDK are intentionally not added until that phase begins.