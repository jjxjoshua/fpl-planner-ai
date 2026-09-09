# fplai-dashboard

Static mockup of the Phase 8 Squad Planner screen. **No framework, no build
tooling, no dependencies** — plain HTML, CSS and classic scripts, compiled by a
single stdlib Python file.

Currently drawn for **GW3, 2026-08-29**, with Phase 2 complete.

Phase 8 is FastAPI + Next.js, and this is not that. It is a design artefact for
deciding what the screen shows and how provenance is rendered, deliberately
built so it cannot turn into a parallel front-end by accident.

---

## Build

```bash
uv run python src/fplai-dashboard/build.py
```

Writes `public/index.html` — one self-contained file, no local assets. Re-run
after editing anything in this directory.

## Develop

Open `src/fplai-dashboard/index.html` directly in a browser, or serve the repo
root and visit `/src/fplai-dashboard/`. The sources load as separate files, so
edit-and-reload works with no build step. Build only when you want the
compiled artefact.

## Files

| File | What it holds |
|---|---|
| `index.html` | Markup and the app chrome. Links `styles.css`, `data.js`, `app.js` |
| `styles.css` | Design tokens and all layout. Three theme states — see below |
| `data.js` | `SEASON`, `SQUAD`, `OWN_SERIES`, `GW3_FIXTURES`, `EO_TOP`, `CANDS`. **The only file carrying numbers** |
| `app.js` | Rendering, the illustrative PMF convolution, row selection |
| `build.py` | Inlines the above into `public/index.html` |

---

## What the build actually does, and why

The compiled page is shaped for publishing as an Artifact, which imposes three
constraints the sources do not have:

1. **Self-contained.** A strict CSP blocks every external host except Google
   Fonts, so CSS and JS are inlined. The font `<link>` survives on purpose —
   that host is the one exception.
2. **No `<!doctype>`, `<html>`, `<head>` or `<body>`.** The host wraps the page
   in its own skeleton, so the output is a fragment. Browsers construct the
   missing elements themselves, which is why the compiled file still opens
   fine straight from disk.
3. **ASCII only.** Point 2 means the output cannot carry its own
   `<meta charset>`, so it must not need one. Every non-ASCII character is
   escaped in the form its zone understands — CSS escapes in the stylesheet,
   `\uXXXX` in scripts, `&#xNN;` in markup. **The sources stay readable UTF-8;
   only the compiled output is escaped.** Never hand-edit `public/index.html`.

`build.py` fails loudly if a non-ASCII character survives, or if a tag it
expects to inline is missing.

---

## The provenance rule

The screen's whole point is that it distinguishes what the store holds from
what has been invented. Every value in `data.js` sits in one of three classes,
and the class drives the styling:

| Class | Marker | Meaning |
|---|---|---|
| Observed | solid, teal | Queried from `data/store/`, with an `observed_at` stamp |
| Model built, not served | violet hatch | The model exists and passes its gate; the per-player value shown is invented because nothing serves it |
| Not built | orange hatch | No model or data exists — Phases 3–5, 7 |

In `data.js` the split is positional: everything up to and including `gw3` on
a candidate is observed; everything under the `f:` key is fabricated. **Keep
that boundary exact.** A fabricated number that renders unhatched is the one
bug this mockup cannot afford — it is the same failure mode CLAUDE.md rule 3
exists to prevent, and a screenshot of it would outlive the correction.

Sources for the observed values, as of 2026-08-29:

- `elements` @ `2026-08-29T07:45:01Z` — prices, ownership, GW1 lines, penalty
  order, transfer counts, price-change gauge, injury flags. Ownership series
  are the real 30-minute snapshots, 288 of them. GW1 per-player stats are read
  **as of the GW2 deadline** so an in-flight GW2 cannot leak into a GW1 column.
- `picks` @ `2026-08-25T13:12:19Z` — **the GW1 effective-ownership sample.**
  9,284 entries, 139,260 picks. `eo` is `sum(multiplier)/entries` and `cap` is
  captaincy share; both are computed from the sample, not modelled. This is the
  dataset with no backfill path.
- `odds_match_odds` @ `2026-08-27T13:39:14Z` — GW3 1X2 across 13–15 books,
  de-vigged by normalising median inverse prices. The odds feed carries the GW3
  fixture list that `pl_match_fixtures` does not.
- `odds_player_goal_odds` @ `2026-08-27T13:39:20Z` — **covers GW2 fixtures
  only**, at 2 books. There is no GW3 anytime-goal capture, which is why that
  column is gone from the matrix and the PMF's goal rate is now fabricated too.
- `game_config` @ `2026-08-28T23:15:01Z` — scoring constants and squad rules,
  read rather than hardcoded. The PMF uses the **midfielder** column: goal 5,
  assist 3, defensive contribution 2, clean sheet 1.
- `events` @ `2026-08-28T22:45:01Z` — the GW3 deadline and the GW2 live average.

Entry-level facts in the topbar — squad value, bank, free transfers, chip
inventory — and the Track A season strip are read from **the user's own FPL
entry**, not from the store. The `picks` sample covers 9,284 other entries and
never this one; saying so on the screen is part of the provenance rule.

Refreshing these means re-querying the store and editing `data.js` by hand.
There is deliberately no live fetch: the page must stay publishable as a
static artefact, and a screen that quietly re-reads present-day prices into a
historical context is exactly the leakage CLAUDE.md rule 2 forbids.

---

## Theming

`styles.css` defines the complete light palette on bare `:root`, redefines the
tokens under `@media (prefers-color-scheme: dark)` guarded as
`:root:not([data-theme="light"])`, and again under `:root[data-theme="dark"]`.
All three states are exercised. Components read tokens only — never a colour
declared inside a media or `[data-theme]` block, which would render one theme's
text on the other theme's ground.
