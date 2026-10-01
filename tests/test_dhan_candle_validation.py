from __future__ import annotations

from datetime import datetime, timedelta

from common.models import Candle
from common.utils import IST, epoch
from dhan.market_data import DhanMarketData
from market.candles import CandleEngine


def _stamp(minute: int) -> int:
    start = datetime(2026, 9, 15, 9, 15, tzinfo=IST)
    return epoch(start + timedelta(minutes=minute - 15))


def test_dhan_candle_parser_rejects_nonfinite_ohlc_bad_envelopes_and_volumes():
    rows = [
        # Valid first-session candle; Dhan's first-trade timestamp is snapped.
        (_stamp(17), 100, 104, 99, 103, 10),
        (_stamp(32), float("inf"), 104, 99, 103, 10),
        (_stamp(47), 100, float("nan"), 99, 103, 10),
        (_stamp(62), 100, 104, 99, 105, 10),
        (_stamp(77), 100, 104, 99, 103, -1),
        (_stamp(92), 100, 104, 99, 103, 1.5),
        (_stamp(107), 100, 104, 99, 103, "bad"),
        (_stamp(122), 100, 104, 99, 103, None),
    ]
    response = {
        "timestamp": [row[0] for row in rows],
        "open": [row[1] for row in rows],
        "high": [row[2] for row in rows],
        "low": [row[3] for row in rows],
        "close": [row[4] for row in rows],
        "volume": [row[5] for row in rows],
    }

    candles = DhanMarketData(rest=None)._parse_candles("OPT-1", response)

    assert len(candles) == 1
    assert candles[0] == Candle(
        "OPT-1", _stamp(15), open=100.0, high=104.0, low=99.0,
        close=103.0, volume=10,
    )


def test_candle_engine_rejects_malformed_closed_candle_without_advancing_watermark():
    engine = CandleEngine(15)
    bad = Candle("OPT-1", _stamp(15), open=100, high=104, low=99, close=105, volume=10)
    good = Candle("OPT-1", _stamp(15), open=100, high=105, low=99, close=103, volume=10)

    assert engine.ingest_closed(bad) is None
    assert engine.ingest_closed(good) is good

    negative_volume = Candle("OPT-2", _stamp(30), 100, 105, 99, 103, -1)
    nonfinite_price = Candle("OPT-3", _stamp(45), 100, float("inf"), 99, 103, 10)
    assert engine.ingest_closed(negative_volume) is None
    assert engine.ingest_closed(nonfinite_price) is None


def test_candle_engine_drops_invalid_ticks_before_building_partial_candles():
    engine = CandleEngine(15)

    engine.ingest_tick("OPT-1", _stamp(16), float("nan"), 10)
    engine.ingest_tick("OPT-1", _stamp(17), 100, -1)
    assert engine._partial == {}

    engine.ingest_tick("OPT-1", _stamp(18), 100, 10)
    assert engine._partial["OPT-1"]["open"] == 100
