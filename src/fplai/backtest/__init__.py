"""Phase 1 backtest replay harness — blueprint §7.2, CLAUDE.md rules 2/5/7.

Everything under this package exists to answer one question honestly:
"what would a simple, leak-free strategy have scored, gameweek by gameweek,
in a real past season?" — and to make it *structurally hard* for a strategy
to see a gameweek's own outcomes before it decides for that gameweek.

Modules:
  rules.py      Squad/formation constants for the backtest window, declared
                 and documented (no live per-season config exists for
                 completed historical seasons — see that module's docstring
                 for why these are a documented assumption, not a guess).
  data.py        Loads `vaastav_player_gameweek_stats` for one season,
                 detects and reports coverage gaps explicitly (never
                 silently degrades).
  squad.py       Player/Squad representation, constraint validation, and the
                 greedy-plus-local-search squad builder shared by all three
                 baselines.
  replay.py      `GameweekView` (the leakage boundary), `SeasonReplay`
                 (the deadline-by-deadline iterator), autosub simulation,
                 and scoring from stored `total_points` (never re-derived).
  baselines.py   Random / Template / Greedy-form strategies.
  report.py      Distribution summaries over a season's per-gameweek scores.
"""
