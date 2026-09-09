"""Decoder registry — E2b story 10 (blueprint §12, "the polymorphism goes
below the capability contract, not in it").

`Provider` composes a `Transport` (fetches raw bytes) and a `Decoder`
(turns those bytes into a Python value — a `pl.DataFrame` for tabular
formats, a parsed object for JSON). This module owns the second half only:
a name -> decode-function registry, deliberately separate from
`transport.py` so neither axis needs to know about the other.

Adding a new format later (e.g. `.xlsx`, `.parquet`) is `register_decoder
("xlsx", fn)` — one line, nothing else touched. No xlsx decoder is added
here: it would need a new dependency (`openpyxl`/`xlsxwriter`/`fastexcel`),
which the story brief explicitly forbids adding. `csv` needs no new
dependency — polars reads CSV natively (`pl.read_csv`) — and `json` needs
only the standard library. Proving the registration SHAPE is the point;
adding xlsx support is a one-line follow-up, not a redesign.

Nothing here makes a network call or touches the filesystem — decoders
take raw `bytes` (already fetched by a transport) and return a decoded
value. That keeps this module trivially unit-testable with literal bytes,
independent of `transport.FileTransport`.
"""

from __future__ import annotations

import io
import json
from typing import Any, Callable

import polars as pl

Decoder = Callable[[bytes], Any]


class DecoderError(RuntimeError):
    """Requested a decoder that isn't registered, or a registered decoder
    failed to parse the bytes it was given."""


_REGISTRY: dict[str, Decoder] = {}


def register_decoder(name: str, fn: Decoder) -> None:
    """Register `fn` under `name` (e.g. `"csv"`, `"json"`). Overwrites a
    prior registration for the same name — last write wins, same as
    `CapabilityRegistry.register`'s append-only-but-explicit model; there
    is no protection against a caller accidentally re-registering the same
    name with a different function, because nothing in this slice needs
    that protection (registration happens once, at import time, below)."""
    _REGISTRY[name] = fn


def decode(name: str, raw: bytes) -> Any:
    """Look up the decoder registered as `name` and run it on `raw`.
    Raises `DecoderError` — naming every registered decoder — if `name`
    isn't registered, rather than a bare `KeyError` a caller has to go
    read this module's source to explain."""
    fn = _REGISTRY.get(name)
    if fn is None:
        raise DecoderError(
            f"no decoder registered for {name!r}. Registered: {sorted(_REGISTRY)}. "
            "Register a new format with fplai.decoders.register_decoder(name, fn)."
        )
    try:
        return fn(raw)
    except DecoderError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed error, not swallowed
        raise DecoderError(f"decoder {name!r} failed on {len(raw)} bytes: {exc}") from exc


def decode_csv(raw: bytes) -> pl.DataFrame:
    """CSV -> `pl.DataFrame`. `infer_schema_length=None` scans every row
    (not just the default first 100) before choosing a dtype per column —
    the same reasoning as `providers/fpl.py`'s `_records_to_df`: a column
    that is empty for the first 100 rows but populated later (e.g. `news`,
    empty for most players) must not silently get typed all-null.
    `try_parse_dates=False` deliberately leaves date/datetime-shaped
    columns (e.g. `kickoff_time`) as strings — parsing them is the
    adapter's job, not this generic decoder's, and archive schemas drift
    across seasons (story brief) in ways a blind auto-parse could get
    wrong without anyone noticing."""
    return pl.read_csv(io.BytesIO(raw), infer_schema_length=None, try_parse_dates=False)


def decode_json(raw: bytes) -> Any:
    """JSON -> whatever `json.loads` returns (dict or list) — no
    normalisation. Turning that into canonical rows is the adapter's job,
    exactly as `providers/pl.py`/`providers/fpl.py` already do for
    HTTP-sourced JSON."""
    return json.loads(raw.decode("utf-8"))


register_decoder("csv", decode_csv)
register_decoder("json", decode_json)
