"""Tests for the configured-liquid-stock universe filter (app.filter_configured_universe)."""
import pytest

from app import filter_configured_universe

MASTER = ["RELIANCE", "HDFCBANK", "INFY", "M&M", "BAJAJ-AUTO", "TATAMOTORS", "MCX"]


def test_empty_allowed_keeps_all():
    kept, skipped = filter_configured_universe(MASTER, [])
    assert kept == MASTER
    assert skipped == []


def test_partial_filter_case_insensitive():
    kept, skipped = filter_configured_universe(
        MASTER, ["reliance", "HDFCBANK", "m&m", "NOTLISTED"]
    )
    assert kept == ["RELIANCE", "HDFCBANK", "M&M"]
    assert skipped == ["NOTLISTED"]


def test_symbols_with_ampersand_and_dash():
    kept, skipped = filter_configured_universe(MASTER, ["M&M", "BAJAJ-AUTO"])
    assert kept == ["M&M", "BAJAJ-AUTO"]
    assert skipped == []


def test_all_missing():
    kept, skipped = filter_configured_universe(MASTER, ["NOPE1", "NOPE2"])
    assert kept == []
    assert skipped == ["NOPE1", "NOPE2"]
