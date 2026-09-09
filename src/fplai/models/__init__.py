"""Statistical model layer — blueprint §4. Prediction lives here, never in
an LLM (CLAUDE.md rule 1) and never as a bare scalar at a module boundary
(rule 5 — every model here emits a distribution).
"""
