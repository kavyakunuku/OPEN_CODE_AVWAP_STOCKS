"""15-minute candle engine (spec §8, §9, §48): session boundaries,
no intracandle signals, duplicate/out-of-order protection, tick building."""
from __future__ import annotations

from datetime import datetime, timedelta

from common.utils import IST, candle_end_for, candle_start_for
from conftest import C, ts
from market.candles import CandleEngine, last_closed_window_start


def dt(day=15, h=9, m=15, s=0):
    return datetime(2026, 9, day, h, m, s, tzinfo=IST)


def test_candle_start_boundaries():
    assert candle_start_for(dt(15, 9, 15)) == dt(15, 9, 15)
    assert candle_start_for(dt(15, 9, 29, 59)) == dt(15, 9, 15)
    assert candle_start_for(dt(15, 9, 30)) == dt(15, 9, 30)
    assert candle_start_for(dt(15, 15, 15)) == dt(15, 15, 15)
    assert candle_start_for(dt(15, 15, 29, 59)) == dt(15, 15, 15)
    # outside session
    assert candle_start_for(dt(15, 9, 14, 59)) is None
    assert candle_start_for(dt(15, 15, 30)) is None
    assert candle_start_for(dt(15, 16, 0)) is None


def test_first_possible_signal_is_after_0930():
    # the 09:15-09:30 candle is the first that can ever close
    assert last_closed_window_start(dt(15, 9, 16)) is None
    assert last_closed_window_start(dt(15, 9, 29, 59)) is None
    assert last_closed_window_start(dt(15, 9, 30)) == dt(15, 9, 15)
    assert last_closed_window_start(dt(15, 10, 0)) == dt(15, 9, 45)


def test_ingest_closed_duplicate_and_out_of_order():
    eng = CandleEngine(15)
    c1 = C("X", 15, 9, 15, close=10, vol=5)
    c2 = C("X", 15, 9, 30, close=11, vol=5)
    assert eng.ingest_closed(c1) is not None
    assert eng.ingest_closed(c2) is not None
    # exact duplicate dropped
    assert eng.ingest_closed(c1) is None
    assert eng.ingest_closed(c2) is None
    # out-of-order (older than last) dropped
    assert eng.ingest_closed(c1) is None


def test_ingest_closed_rejects_off_boundary():
    eng = CandleEngine(15)
    bad = C("X", 15, 9, 20, close=10, vol=5)  # 09:20 is not a boundary
    assert eng.ingest_closed(bad) is None
    # outside session
    bad2 = C("X", 15, 9, 10, close=10, vol=5)
    assert eng.ingest_closed(bad2) is None


def test_tick_building_and_volume_deltas():
    eng = CandleEngine(15)
    # pre-open tick ignored
    eng.ingest_tick("X", int(dt(15, 9, 14).timestamp()), 10.0, 50)
    assert not eng._partial
    # window 09:15-09:30
    eng.ingest_tick("X", int(dt(15, 9, 16).timestamp()), 10.0, 100)
    eng.ingest_tick("X", int(dt(15, 9, 18).timestamp()), 12.0, 150)
    eng.ingest_tick("X", int(dt(15, 9, 20).timestamp()), 9.0, 210)
    eng.ingest_tick("X", int(dt(15, 9, 29).timestamp()), 9.5, 260)
    p = eng._partial["X"]
    assert p["open"] == 10.0 and p["high"] == 12.0 and p["low"] == 9.0
    assert p["close"] == 9.5 and p["volume"] == 260 - 100

    # NO candle before the boundary (no intracandle signals)
    assert eng.close_due(dt(15, 9, 29, 59)) == []
    out = eng.close_due(dt(15, 9, 30))
    assert len(out) == 1
    c = out[0]
    assert c.open == 10.0 and c.high == 12.0 and c.low == 9.0
    assert c.close == 9.5 and c.volume == 160
    # the partial is gone and the closed candle is deduped
    assert "X" not in eng._partial
    assert eng.ingest_closed(c) is None


def test_tick_new_day_resets_baseline():
    eng = CandleEngine(15)
    eng.ingest_tick("X", int(dt(15, 15, 20).timestamp()), 10.0, 5000)
    # next session day: cumulative volume restarts near 0
    eng.ingest_tick("X", int(dt(16, 9, 16).timestamp()), 11.0, 40)
    p = eng._partial["X"]
    assert p["ts"] == int(dt(16, 9, 15).timestamp())
    assert p["volume"] == 0  # first tick re-baselines (window start volume unknown)
    eng.ingest_tick("X", int(dt(16, 9, 20).timestamp()), 10.5, 90)
    assert p["volume"] == 90 - 40  # measured from the new baseline
