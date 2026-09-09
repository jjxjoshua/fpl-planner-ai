"""Tests for fplai.decoders — the format-name -> decode-function registry
(E2b story 10). Pure, no I/O, no network — everything here works off
literal bytes."""

from __future__ import annotations

import json

import polars as pl
import pytest

from fplai.decoders import DecoderError, decode, decode_csv, decode_json, register_decoder


def test_decode_csv_returns_a_dataframe():
    raw = b"id,name\n1,Arsenal\n2,Aston Villa\n"
    df = decode("csv", raw)
    assert isinstance(df, pl.DataFrame)
    assert df.columns == ["id", "name"]
    assert df.height == 2


def test_decode_csv_scans_every_row_for_dtype_inference():
    # A column empty for the first 100 rows but populated on row 101 must
    # not be silently typed all-null — infer_schema_length=None is what
    # prevents that (same reasoning as providers/fpl.py's _records_to_df).
    rows = ["id,news"] + [f"{i}," for i in range(1, 150)] + ["150,Injured"]
    raw = "\n".join(rows).encode("utf-8")
    df = decode_csv(raw)
    assert df.filter(pl.col("id") == 150)["news"][0] == "Injured"


def test_decode_json_returns_parsed_object():
    raw = json.dumps({"a": 1, "b": [1, 2, 3]}).encode("utf-8")
    assert decode_json(raw) == {"a": 1, "b": [1, 2, 3]}


def test_decode_raises_for_unregistered_name():
    with pytest.raises(DecoderError, match="xlsx"):
        decode("xlsx", b"whatever")


def test_decode_wraps_a_failing_decoder_in_decodererror():
    with pytest.raises(DecoderError):
        decode("json", b"{not valid json")


def test_register_decoder_extends_the_registry_without_touching_core():
    # Blueprint §12's Architect note: adding a new format is "registering a
    # decoder, nothing else" — proven here with a throwaway custom format,
    # not xlsx (no new dependency, per the story brief).
    register_decoder("reverse-text", lambda raw: raw.decode("utf-8")[::-1])
    try:
        assert decode("reverse-text", b"abc") == "cba"
    finally:
        # Don't leak this test-only registration into other tests' module
        # state — _REGISTRY is process-global.
        from fplai.decoders import _REGISTRY

        _REGISTRY.pop("reverse-text", None)
