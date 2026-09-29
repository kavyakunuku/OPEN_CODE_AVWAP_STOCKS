from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.models import Candle, OptionContract
from market.liquidity import (
    ContractLiquidityData,
    LiquidityObservation,
    LiquidityConfigError,
    OptionLiquidityEngine,
)

IST = ZoneInfo("Asia/Kolkata")
AS_OF = datetime(2026, 9, 30, 10, 0, tzinfo=IST)


def make_contract(security_id, side="CE", strike=100.0, underlying="TEST", expiry="2026-10-29"):
    return OptionContract(
        security_id=str(security_id),
        symbol=f"{underlying} {strike:g} {side}",
        underlying=underlying,
        strike=float(strike),
        option_type=side,
        expiry=expiry,
        lot_size=1,
    )


def observation(
    at=AS_OF,
    *,
    last=10.0,
    volume=100.0,
    oi=1000.0,
    oi_change=0.0,
    bid=9.9,
    ask=10.0,
    bid_qty=20.0,
    ask_qty=25.0,
):
    return LiquidityObservation(
        observed_at=at,
        last_price=last,
        volume=volume,
        open_interest=oi,
        oi_change=oi_change,
        bid=bid,
        ask=ask,
        bid_quantity=bid_qty,
        ask_quantity=ask_qty,
    )


def make_data(
    security_id,
    *,
    side="CE",
    strike=100.0,
    volume=100.0,
    oi=1000.0,
    bid=9.9,
    ask=10.0,
    bid_qty=20.0,
    ask_qty=25.0,
    observations=None,
    candles=(),
    underlying="TEST",
    expiry="2026-10-29",
):
    contract = make_contract(security_id, side, strike, underlying, expiry)
    if observations is None:
        observations = [observation(
            volume=volume, oi=oi, bid=bid, ask=ask,
            bid_qty=bid_qty, ask_qty=ask_qty,
        )]
    return ContractLiquidityData(contract, tuple(observations), tuple(candles))


def test_percentile_components_rank_only_comparable_contracts():
    items = [
        make_data("a", strike=90, volume=100, oi=1000, bid_qty=10, ask_qty=10),
        make_data("b", strike=100, volume=200, oi=2000, bid_qty=20, ask_qty=20),
        make_data("c", strike=110, volume=300, oi=3000, bid_qty=30, ask_qty=30),
    ]

    results = OptionLiquidityEngine().score_contracts(items, AS_OF)

    assert [result.component_scores["volume"] for result in results] == [0, 50, 100]
    assert [result.component_scores["oi"] for result in results] == [0, 50, 100]
    assert [result.component_scores["depth"] for result in results] == [0, 50, 100]
    assert all(0 <= result.score <= 100 for result in results)


def test_percentile_ties_and_unmatched_contract_use_neutral_midrank():
    tied = [make_data("a", volume=50), make_data("b", volume=50)]
    results = OptionLiquidityEngine().score_contracts(tied, AS_OF)
    assert [result.component_scores["volume"] for result in results] == [50, 50]

    lone = OptionLiquidityEngine().score_contract(tied[0], AS_OF)
    assert lone.component_scores["volume"] == 50


def test_configurable_weights_change_overall_score_without_changing_components():
    items = [make_data("low", volume=10), make_data("high", volume=100)]
    volume_only = OptionLiquidityEngine({
        "weights": {name: (1 if name == "volume" else 0) for name in
                    ("volume", "oi", "spread", "depth", "consistency")}
    })

    low, high = volume_only.score_contracts(items, AS_OF)

    assert low.component_scores["volume"] == 0
    assert high.component_scores["volume"] == 100
    assert low.score == 0
    assert high.score == 100


def test_spread_score_uses_midpoint_percent_and_hard_maximum():
    tight = make_data("tight", bid=100.0, ask=100.2)
    wide = make_data("wide", bid=100.0, ask=101.0)
    tight_result, wide_result = OptionLiquidityEngine().score_contracts([tight, wide], AS_OF)

    assert tight_result.spread_percent == pytest.approx(100 * 0.2 / 100.1)
    assert tight_result.component_scores["spread"] == 100
    assert wide_result.spread_percent == pytest.approx(100 * 1 / 100.5)
    assert wide_result.component_scores["spread"] == 0
    assert "WIDE_SPREAD" in wide_result.rejection_reasons
    assert "WIDE_SPREAD" not in tight_result.rejection_reasons


@pytest.mark.parametrize(
    "bid,ask",
    [(-1, 1), (1, 0), (11, 10), (None, 10), (10, None)],
)
def test_invalid_or_incomplete_quotes_are_rejected(bid, ask):
    result = OptionLiquidityEngine().score_contract(
        make_data("bad", bid=bid, ask=ask), AS_OF
    )
    assert result.qualified is False
    assert "INVALID_DATA" in result.rejection_reasons


def test_multiple_configured_rejection_reasons_are_reported():
    engine = OptionLiquidityEngine({
        "minimums": {
            "liquidity_score": 95,
            "volume": 1000,
            "volume_percentile": 90,
            "oi": 5000,
            "oi_percentile": 90,
            "premium": 5,
            "depth": 100,
        }
    })
    result = engine.score_contract(
        make_data("reject", volume=10, oi=20, bid=0.1, ask=0.3,
                  bid_qty=2, ask_qty=3),
        AS_OF,
    )

    assert result.qualified is False
    assert set(result.rejection_reasons) >= {
        "WIDE_SPREAD", "LOW_VOLUME", "LOW_OI", "LOW_PREMIUM", "LOW_DEPTH",
        "LOW_LIQUIDITY_SCORE",
    }
    assert result.rejection_codes == result.rejection_reasons


def test_call_and_put_percentiles_are_independent():
    items = [
        make_data("ce-low", side="CE", strike=90, volume=10),
        make_data("ce-high", side="CE", strike=100, volume=20),
        make_data("pe-low", side="PE", strike=100, volume=1000),
        make_data("pe-high", side="PE", strike=110, volume=2000),
    ]
    result_by_id = {
        result.security_id: result
        for result in OptionLiquidityEngine().score_contracts(items, AS_OF)
    }

    assert result_by_id["ce-high"].component_scores["volume"] == 100
    assert result_by_id["pe-high"].component_scores["volume"] == 100
    assert result_by_id["ce-low"].component_scores["volume"] == 0
    assert result_by_id["pe-low"].component_scores["volume"] == 0


def test_chain_imbalance_penalty_does_not_modify_individual_side_score():
    call = make_data("call-only", side="CE")
    engine = OptionLiquidityEngine()
    individual = engine.score_contract(call, AS_OF)
    report = engine.score_option_chain([call], AS_OF)
    chain = report.chains[0]

    assert report.contracts[0].score == individual.score
    assert chain.call_score == individual.score
    assert chain.put_score is None
    assert chain.imbalance_ratio == 1
    assert chain.penalty_points == 25
    assert chain.chain_score < chain.call_score


def test_historical_evaluation_excludes_future_observations_and_incomplete_bars():
    old_oi = observation(
        AS_OF - timedelta(minutes=65), last=None, volume=None, oi=100,
        oi_change=None, bid=None, ask=None, bid_qty=None, ask_qty=None,
    )
    book_at_t = observation(
        AS_OF - timedelta(seconds=10), last=9.0, volume=None, oi=130,
        oi_change=None, bid=8.98, ask=9.0, bid_qty=5, ask_qty=6,
    )
    future_quote = LiquidityObservation(
        observed_at=AS_OF + timedelta(seconds=1),
        last_price=999.0,
        volume=999999.0,
        open_interest=999999.0,
        bid=900.0,
        ask=901.0,
    )
    candles = [
        Candle("historic", int((AS_OF.replace(hour=9, minute=15)).timestamp()), 1, 2, 1, 2, 10),
        Candle("historic", int((AS_OF.replace(hour=9, minute=45)).timestamp()), 2, 3, 2, 3, 20),
        Candle("historic", int((AS_OF.replace(hour=10, minute=0)).timestamp()), 3, 4, 3, 4, 1000),
        Candle("historic", int((AS_OF.replace(hour=10, minute=15)).timestamp()), 4, 5, 4, 5, 5000),
    ]
    data = ContractLiquidityData(
        make_contract("historic"),
        (old_oi, book_at_t, future_quote),
        tuple(candles),
    )

    result = OptionLiquidityEngine().score_contract(data, AS_OF)

    assert result.observation_at == AS_OF - timedelta(seconds=10)
    assert result.last_price == 9.0
    assert result.volume == 30
    assert result.open_interest == 130
    assert result.oi_change == 30
    assert result.component_scores["consistency"] == 100
    assert result.quote_age_seconds == 10
    assert result.qualified is True


def test_stale_book_quote_is_rejected_with_invalid_data():
    stale = make_data(
        "stale",
        observations=[observation(at=AS_OF - timedelta(seconds=61))],
    )
    result = OptionLiquidityEngine().score_contract(stale, AS_OF)
    assert "INVALID_DATA" in result.rejection_reasons


def test_weight_and_spread_configuration_is_validated():
    with pytest.raises(LiquidityConfigError):
        OptionLiquidityEngine({"weights": {name: 0 for name in
                                            ("volume", "oi", "spread", "depth", "consistency")}})
    with pytest.raises(LiquidityConfigError):
        OptionLiquidityEngine({"preferred_spread_percent": 0.6})
    with pytest.raises(LiquidityConfigError):
        OptionLiquidityEngine({"minimums": {"liquidity_score": 101}})
