from __future__ import annotations

from dhan.market_data import DhanMarketData


class _Rest:
    def __init__(self):
        self.calls = []

    def post(self, endpoint, *, payload):
        self.calls.append((endpoint, payload))
        segment, security_ids = next(iter(payload.items()))
        return {
            "data": {
                segment: {
                    str(security_id): {
                        "last_price": 10.0,
                        "volume": 100,
                        "oi": 1200,
                        "depth": {
                            "buy": [
                                {"price": 9.95, "quantity": 40},
                                {"price": 9.90, "quantity": 20},
                                {"price": 0, "quantity": 0},
                            ],
                            "sell": [
                                {"price": 10.05, "quantity": 50},
                                {"price": 10.10, "quantity": 15},
                                {"price": 0, "quantity": 0},
                            ],
                        },
                    }
                    for security_id in security_ids
                }
            }
        }


def test_quote_segments_share_one_request_per_second_pacing(monkeypatch):
    sleeps = []
    monkeypatch.setattr("dhan.market_data.time.sleep", sleeps.append)
    rest = _Rest()
    market_data = DhanMarketData(rest)

    option_quotes = market_data.quotes_segment(["101"], "NSE_FNO")
    market_data.quotes_segment(["202"], "NSE_EQ")

    quote = option_quotes["101"]
    assert (quote.bid, quote.ask) == (9.95, 10.05)
    assert (quote.bid_quantity, quote.ask_quantity, quote.open_interest) == (40, 50, 1200)
    assert quote.bid_depth == [
        {"price": 9.95, "quantity": 40}, {"price": 9.90, "quantity": 20}
    ]
    assert quote.ask_depth == [
        {"price": 10.05, "quantity": 50}, {"price": 10.10, "quantity": 15}
    ]
    assert [payload for _, payload in rest.calls] == [
        {"NSE_FNO": [101]},
        {"NSE_EQ": [202]},
    ]
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 1.1
