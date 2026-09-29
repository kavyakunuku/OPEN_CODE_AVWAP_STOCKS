"""Entry / exit rule truth table (spec §16-§17, §19, §48)."""
from __future__ import annotations

from strategy.rules import is_entry_cross, is_exit_close


def test_entry_fires_on_true_cross():
    # spec example: prev 102 vs 100, current 98 vs 100.5
    assert is_entry_cross(102.0, 100.0, 98.0, 100.5) is True


def test_entry_when_prev_exactly_at_avwap():
    # prev close >= prev avwap includes equality
    assert is_entry_cross(100.0, 100.0, 98.0, 99.0) is True


def test_no_entry_when_already_below():
    # spec example: prev 98/100, current 97/99 -> NO entry
    assert is_entry_cross(98.0, 100.0, 97.0, 99.0) is False


def test_no_entry_when_current_at_or_above():
    assert is_entry_cross(102.0, 100.0, 100.0, 100.0) is False
    assert is_entry_cross(102.0, 100.0, 101.0, 100.0) is False


def test_no_entry_when_avwap_unknown():
    assert is_entry_cross(None, 100.0, 98.0, 99.0) is False
    assert is_entry_cross(102.0, None, 98.0, 99.0) is False
    assert is_entry_cross(102.0, 100.0, 98.0, None) is False
    assert is_entry_cross(None, None, None, None) is False


def test_exit_only_when_close_above():
    assert is_exit_close(103.0, 102.0) is True
    assert is_exit_close(102.0, 102.0) is False   # equal is NOT an exit
    assert is_exit_close(101.0, 102.0) is False   # below: HOLD
    assert is_exit_close(None, 102.0) is False
    assert is_exit_close(103.0, None) is False
