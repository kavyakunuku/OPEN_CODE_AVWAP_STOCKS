from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common.models import OptionContract, Quote
from common.utils import epoch
from dhan.liquidity import DhanLiquidityAdapter
from dhan.option_chain import parse_chain_response
from market.liquidity import OptionLiquidityEngine

IST = ZoneInfo("Asia/Kolkata")
CAPTURED_AT = datetime(2026, 9, 30, 10, 0, tzinfo=IST)


def _contract():
    return OptionContract(
        security_id="12345",
        symbol="TEST 100 CE",
        underlying="TEST",
        strike=100.0,
        option_type="CE",
        expiry="2026-10-29",
        lot_size=1,
    )


def test_dhan_chain_parser_preserves_observation_and_available_liquidity_fields():
    payload = {
        "data": {
            "last_price": 100.0,
            "oc": {
                "100": {
                    "ce": {
                        "security_id": "12345",
                        "last_price": 5.1,
                        "oi": 1200,
                        "volume": 450,
                        "change_in_oi": 50,
                        "top_bid_price": 5.0,
                        "top_ask_price": 5.1,
                        "top_bid_quantity": 20,
                        "top_ask_quantity": 30,
                        "market_depth": {
                            "buy": [{"price": 5.0, "quantity": 20}],
                            "sell": [{"price": 5.1, "quantity": 30}],
                        },
                    }
                }
            },
        }
    }

    chain = parse_chain_response(payload, observed_at=CAPTURED_AT)
    option = chain.ce[100.0]

    assert chain.observed_at == CAPTURED_AT
    assert option.oi == 1200 and option.oi_available is True
    assert option.volume == 450 and option.volume_available is True
    assert option.oi_change == 50
    assert option.bid_quantity == 20 and option.ask_quantity == 30
    assert option.bid_depth == [{"price": 5.0, "quantity": 20}]
    assert option.ask_depth == [{"price": 5.1, "quantity": 30}]


def test_dhan_adapter_combines_captured_chain_quote_and_project_contract():
    payload = {
        "data": {
            "last_price": 100.0,
            "oc": {
                "100": {
                    "ce": {
                        "security_id": "12345",
                        "last_price": 5.0,
                        "oi": 1200,
                        "volume": 450,
                        "change_in_oi": 50,
                        "top_bid_price": 5.0,
                        "top_ask_price": 5.01,
                        "top_bid_quantity": 20,
                        "top_ask_quantity": 30,
                    }
                }
            },
        }
    }
    chain = parse_chain_response(payload, observed_at=CAPTURED_AT)
    quote_at = CAPTURED_AT + timedelta(seconds=5)
    quote = Quote("12345", 5.02, cum_volume=475, ts=epoch(quote_at))
    data = DhanLiquidityAdapter().from_option_chain(
        [_contract()], chain, quote_snapshots={"12345": quote}
    )

    result = OptionLiquidityEngine().score_contract(data[0], quote_at)

    assert result.last_price == 5.02
    assert result.volume == 475
    assert result.open_interest == 1200
    assert result.oi_change == 50
    assert result.depth == 20
    assert result.premium == 5.0  # executable sell-side bid
    assert result.observation_at == quote_at
    assert result.qualified is True


def test_raw_chain_without_capture_time_is_not_backdated_by_adapter():
    payload = {
        "data": {
            "oc": {
                "100": {
                    "ce": {
                        "security_id": "12345",
                        "last_price": 5.0,
                        "oi": 100,
                        "volume": 10,
                        "top_bid_price": 4.9,
                        "top_ask_price": 5.0,
                    }
                }
            }
        }
    }
    chain = parse_chain_response(payload, observed_at=None)
    # parse_chain_response stamps the real local capture time when available;
    # explicitly remove it here to model an imported, un-timestamped snapshot.
    chain.observed_at = None
    data = DhanLiquidityAdapter().from_option_chain([_contract()], chain)
    result = OptionLiquidityEngine({"max_quote_age_seconds": None}).score_contract(
        data[0], CAPTURED_AT
    )

    assert data[0].observations == ()
    assert "INVALID_DATA" in result.rejection_reasons
