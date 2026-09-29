"""AVWAP math (spec §10-§14, §48): first candle, multiple candles,
multiple days, zero volume, contract independence, restart recovery."""
from __future__ import annotations

from conftest import C, ts

from common.models import Candle
from strategy.avwap import AvwapState, AvwapStore


def test_first_candle_is_anchor():
    s = AvwapState(security_id="X")
    c = C("X", 15, 9, 15, close=100, vol=10)
    av = s.update(c)
    assert av == 100.0
    assert s.anchor_ts == c.ts
    assert s.cumulative_volume == 10
    assert s.cumulative_price_volume == 1000.0


def test_multiple_candles():
    s = AvwapState(security_id="X")
    s.update(C("X", 15, 9, 15, close=100, vol=10))
    # TP2 = (105+103+104)/3 = 104
    av = s.update(C("X", 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103))
    assert abs(av - (1000 + 1040) / 20) < 1e-9
    assert s.cumulative_volume == 20


def test_no_daily_reset_cumulative_across_days():
    s = AvwapState(security_id="X")
    s.update(C("X", 15, 9, 15, close=100, vol=10))     # day 1
    s.update(C("X", 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103))
    v_day1_end = s.cumulative_volume
    pv_day1_end = s.cumulative_price_volume

    # day 2: cumulators MUST continue, not reset
    s.update(C("X", 16, 9, 15, close=102, vol=20, open_=104, high=105, low=101))
    assert s.cumulative_volume == v_day1_end + 20
    tp2 = (105 + 101 + 102) / 3
    assert abs(s.cumulative_price_volume - (pv_day1_end + tp2 * 20)) < 1e-9
    assert s.anchor_ts == ts(15, 9, 15)  # anchor stays at the first candle


def test_zero_volume_first_candle():
    s = AvwapState(security_id="X")
    av = s.update(C("X", 15, 9, 15, close=100, vol=0))
    assert av is None                      # no volume -> no AVWAP
    assert s.anchor_ts is not None         # anchor still set at first candle
    av = s.update(C("X", 15, 9, 30, close=102, vol=10, open_=100, high=102, low=100))
    # zero-volume candle contributed nothing; TP = (102+100+102)/3 = 101.333
    assert abs(av - 101.33333333333333) < 1e-9


def test_zero_volume_mid_series_does_not_disturb():
    s = AvwapState(security_id="X")
    s.update(C("X", 15, 9, 15, close=100, vol=10))
    av1 = s.update(C("X", 15, 9, 30, close=0, vol=0))  # degenerate prices, no volume
    s.update(C("X", 15, 9, 45, close=100, vol=0))
    assert s.cumulative_volume == 10
    assert abs(s.last_avwap - 100.0) < 1e-9


def test_duplicate_candle_idempotent():
    s = AvwapState(security_id="X")
    c = C("X", 15, 9, 15, close=100, vol=10)
    s.update(c)
    before = s.to_dict()
    s.update(c)  # replay (reconnect / restart)
    assert s.to_dict() == before
    # older out-of-order candle ignored
    s.update(C("X", 15, 9, 15, close=55, vol=999))
    assert s.cumulative_volume == 10


def test_contract_states_never_shared():
    a = AvwapState(security_id="CE_1500")
    b = AvwapState(security_id="PE_1520")
    a.update(C("CE_1500", 15, 9, 15, close=100, vol=10))
    b.update(C("PE_1520", 15, 9, 15, close=50, vol=3))
    assert a.cumulative_price_volume == 1000.0
    assert b.cumulative_price_volume == 150.0
    assert a.last_avwap == 100.0
    assert b.last_avwap == 50.0


def test_initialize_from_history_matches_incremental(tmp_path):
    from storage.database import Database

    db = Database(str(tmp_path / "a.db"))
    store = AvwapStore(db)
    candles = [
        C("X", 15, 9, 15, close=100, vol=10),
        C("X", 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C("X", 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),
    ]
    hist = store.initialize_from_candles("X", candles)
    ref = AvwapState(security_id="X")
    for c in candles:
        ref.update(c)
    assert abs(hist.last_avwap - ref.last_avwap) < 1e-12
    assert hist.anchor_ts == ref.anchor_ts
    # re-initializing with the same history must not double count
    hist2 = store.initialize_from_candles("X", candles)
    assert hist2.cumulative_volume == hist.cumulative_volume
    # new candles on top merge correctly
    c4 = C("X", 15, 10, 0, close=99, vol=10, open_=101, high=101, low=98.9)
    hist3 = store.initialize_from_candles("X", [c4])
    ref.update(c4)
    assert abs(hist3.last_avwap - ref.last_avwap) < 1e-12


def test_state_persists_and_reloads(tmp_path):
    from storage.database import Database

    db = Database(str(tmp_path / "b.db"))
    store = AvwapStore(db)
    s = store.get("X")
    s.update(C("X", 15, 9, 15, close=100, vol=10))
    s.update(C("X", 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103))
    store.save(s)

    store2 = AvwapStore(db)  # "restart"
    s2 = store2.get("X")
    assert abs(s2.last_avwap - s.last_avwap) < 1e-12
    assert s2.cumulative_volume == s.cumulative_volume
    assert s2.anchor_ts == s.anchor_ts
