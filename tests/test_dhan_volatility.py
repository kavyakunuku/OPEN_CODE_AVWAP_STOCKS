from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common.models import Candle, OptionContract, Quote
from common.utils import epoch
from dhan.option_chain import ChainOption, OptionChainData
from dhan.volatility import DhanVolatilityAdapter
from market.volatility import OHLCBar, VolatilityAnalysisEngine

IST = ZoneInfo("Asia/Kolkata")
AS_OF = datetime(2026, 9, 30, 10, 0, tzinfo=IST)


def contract():
    return OptionContract(
        security_id="OPT1",
        symbol="TEST 100 CE",
        underlying="TEST",
        strike=100.0,
        option_type="CE",
        expiry="2026-10-29",
        lot_size=1,
    )


def test_dhan_adapter_uses_timestamped_quotes_chain_iv_and_phase3_qualification():
    option_contract = contract()
    chain = OptionChainData(
        expiry=option_contract.expiry,
        observed_at=AS_OF,
        ce={100.0: ChainOption(
            strike=100.0,
            option_type="CE",
            security_id="OPT1",
            ltp=12.5,
            iv=27.5,
        )},
    )
    candles = [
        Candle("underlying", epoch(AS_OF.replace(hour=9, minute=15)), 100, 102, 99, 101, 10),
        Candle("underlying", epoch(AS_OF.replace(hour=9, minute=30)), 101, 104, 100, 103, 20),
    ]
    option_candles = [
        Candle("OPT1", epoch(AS_OF.replace(hour=9, minute=15)), 10, 12, 9, 11, 100),
        Candle("OPT1", epoch(AS_OF.replace(hour=9, minute=30)), 11, 14, 10, 13, 150),
    ]
    adapter = DhanVolatilityAdapter()

    data = adapter.build_input(
        "TEST",
        bars_15m=candles,
        underlying_quote=Quote("underlying", 103.25, ts=epoch(AS_OF)),
        option_contracts=[option_contract],
        option_bars_by_security_id={"OPT1": option_candles},
        option_quotes_by_security_id={
            "OPT1": Quote("OPT1", 13.1, ts=epoch(AS_OF + timedelta(seconds=1)))
        },
        option_chain=chain,
        liquidity_qualified_security_ids={"OPT1"},
    )

    result = VolatilityAnalysisEngine({
        "windows": {
            "atr_15m_period": 2,
            "option_atr_period": 2,
            "option_movement_bars": 2,
        }
    }).analyze(data, AS_OF)

    assert isinstance(data.bars_15m[0], OHLCBar)
    assert data.options[0].liquidity_qualified is True
    option = result.option_results[0]
    assert option.current_price == 12.5  # chain was captured at T; quote at T+1 is excluded
    assert option.implied_volatility == 27.5
    assert option.used_for_score is True
    assert result.current_price == 103.25


def test_timestamped_chain_spot_is_a_valid_underlying_price_observation():
    chain = OptionChainData(
        spot=104.25,
        observed_at=AS_OF,
        expiry="2026-10-29",
    )
    data = DhanVolatilityAdapter().build_input("TEST", option_chain=chain)

    result = VolatilityAnalysisEngine().analyze(data, AS_OF)

    assert result.current_price == 104.25


def test_dhan_adapter_does_not_invent_or_backdate_iv_without_chain_timestamp():
    option_contract = contract()
    chain = OptionChainData(
        expiry=option_contract.expiry,
        observed_at=None,
        ce={100.0: ChainOption(
            strike=100.0,
            option_type="CE",
            security_id="OPT1",
            ltp=12.5,
            iv=27.5,
        )},
    )

    data = DhanVolatilityAdapter().build_input(
        "TEST", option_contracts=[option_contract], option_chain=chain
    )
    result = VolatilityAnalysisEngine().analyze(data, AS_OF)

    assert data.options[0].implied_volatility_observations == ()
    assert result.option_results[0].implied_volatility is None
    assert result.current_price is None


def test_dhan_chain_expiry_or_security_mismatch_is_not_reused_for_option_volatility():
    option_contract = contract()
    wrong_expiry = OptionChainData(
        expiry="2026-11-26",
        observed_at=AS_OF,
        ce={100.0: ChainOption(
            strike=100.0,
            option_type="CE",
            security_id="OPT1",
            ltp=12.5,
            iv=27.5,
        )},
    )
    data = DhanVolatilityAdapter().build_input(
        "TEST", option_contracts=[option_contract], option_chain=wrong_expiry
    )

    assert data.options[0].price_observations == ()
    assert data.options[0].implied_volatility_observations == ()
