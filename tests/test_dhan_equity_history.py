from datetime import datetime
from zoneinfo import ZoneInfo

from common.utils import epoch
from dhan.market_data import DhanMarketData

IST = ZoneInfo("Asia/Kolkata")


class _Rest:
    def __init__(self):
        self.path = None
        self.payload = None

    def post(self, path, *, payload, retries=0):
        self.path = path
        self.payload = payload
        return {
            "timestamp": [epoch(datetime(2026, 9, 30, 9, 17, tzinfo=IST))],
            "open": [100.0],
            "high": [102.0],
            "low": [99.5],
            "close": [101.5],
            "volume": [5000],
        }


def test_nse_equity_intraday_request_uses_shared_15m_candle_parser():
    rest = _Rest()
    market_data = DhanMarketData(rest)

    candles = market_data.intraday_candles(
        "1001",
        datetime(2026, 9, 1, 9, 15, tzinfo=IST),
        datetime(2026, 9, 30, 10, 0, tzinfo=IST),
        interval_minutes=15,
        instrument="EQUITY",
        exchange_segment="NSE_EQ",
    )

    assert rest.path == "/v2/charts/intraday"
    assert rest.payload["securityId"] == 1001
    assert rest.payload["exchangeSegment"] == "NSE_EQ"
    assert rest.payload["instrument"] == "EQUITY"
    assert rest.payload["interval"] == 15
    # Existing session-grid snapping is retained for underlying candles.
    assert len(candles) == 1
    assert candles[0].ts == epoch(datetime(2026, 9, 30, 9, 15, tzinfo=IST))
    assert candles[0].close == 101.5
