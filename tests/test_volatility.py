from datetime import datetime, timedelta
from math import log, sqrt
from statistics import stdev
from zoneinfo import ZoneInfo

import pytest

from common.models import OptionContract
from market.volatility import (
    ImpliedVolatilityObservation,
    OHLCBar,
    OptionVolatilityInput,
    PriceObservation,
    UnderlyingVolatilityInput,
    VolatilityAnalysisEngine,
    VolatilityConfigError,
    calculate_atr,
    calculate_atr_percent,
    calculate_intraday_volatility,
    calculate_realized_volatility,
)

IST = ZoneInfo("Asia/Kolkata")
AS_OF = datetime(2026, 9, 30, 10, 0, tzinfo=IST)


def make_bar(at, open_, high, low, close):
    return OHLCBar(int(at.timestamp()), open_, high, low, close)


def make_contract(security_id="OPT1", side="CE", strike=100.0):
    return OptionContract(
        security_id=security_id,
        symbol=f"TEST {strike:g} {side}",
        underlying="TEST",
        strike=strike,
        option_type=side,
        expiry="2026-10-29",
        lot_size=1,
    )


def short_config(**overrides):
    cfg = {
        "windows": {
            "atr_period": 2,
            "atr_15m_period": 2,
            "realized_volatility_periods": 2,
            "intraday_volatility_periods": 2,
            "option_atr_period": 2,
            "option_realized_volatility_periods": 2,
            "option_movement_bars": 3,
        }
    }
    cfg.update(overrides)
    return cfg


def test_wilder_atr_and_atr_percent_match_hand_calculation():
    bars = [
        make_bar(AS_OF - timedelta(days=2), 10, 12, 9, 10),  # TR 3
        make_bar(AS_OF - timedelta(days=1), 10, 14, 10, 13),  # TR 4
        make_bar(AS_OF, 13, 15, 12, 14),  # TR 3; Wilder ATR = 3.25
    ]

    assert calculate_atr(bars, period=2) == pytest.approx(3.25)
    assert calculate_atr_percent(bars, period=2) == pytest.approx(3.25 / 14 * 100)
    assert calculate_atr(bars[:1], period=2) is None


def test_underlying_daily_and_15_minute_metrics_are_both_returned():
    daily = [
        make_bar(datetime(2026, 9, 25, 9, 15, tzinfo=IST), 100, 103, 99, 102),
        make_bar(datetime(2026, 9, 28, 9, 15, tzinfo=IST), 102, 106, 101, 105),
        make_bar(datetime(2026, 9, 29, 9, 15, tzinfo=IST), 105, 110, 104, 109),
    ]
    intraday = [
        make_bar(AS_OF.replace(hour=9, minute=15), 100, 102, 99, 101),
        make_bar(AS_OF.replace(hour=9, minute=30), 101, 104, 100, 103),
        make_bar(AS_OF.replace(hour=9, minute=45), 103, 104, 102, 103.5),
    ]
    data = UnderlyingVolatilityInput(
        symbol="TEST",
        daily_bars=daily,
        bars_15m=intraday,
        price_observations=(PriceObservation(AS_OF, 110),),
    )

    result = VolatilityAnalysisEngine(short_config()).analyze(data, AS_OF)

    assert result.atr == pytest.approx(5.25)
    assert result.atr_percent == pytest.approx(5.25 / 110 * 100)
    assert result.atr_15m == pytest.approx(2.75)
    assert result.atr_15m_percent == pytest.approx(2.75 / 110 * 100)
    assert result.realized_volatility is not None
    assert result.intraday_volatility is not None
    assert 0 <= result.volatility_score <= 100
    assert result.to_dict()["symbol"] == "TEST"


def test_rolling_realized_volatility_uses_only_the_requested_return_window():
    closes = [100, 102, 101, 105, 103, 109]
    periods = 4
    selected = closes[-(periods + 1):]
    returns = [log(right / left) for left, right in zip(selected, selected[1:])]
    expected = stdev(returns) * sqrt(252) * 100

    assert calculate_realized_volatility(closes, periods, 252) == pytest.approx(expected)
    assert calculate_realized_volatility(closes[:4], periods, 252) is None


def test_intraday_volatility_excludes_overnight_gaps_and_requires_enough_returns():
    day_one = datetime(2026, 9, 29, 9, 15, tzinfo=IST)
    bars = [
        make_bar(day_one, 100, 101, 99, 100),
        make_bar(day_one + timedelta(minutes=15), 100, 102, 99, 101),
        make_bar(day_one + timedelta(minutes=30), 101, 102, 100, 100.5),
        make_bar(datetime(2026, 9, 30, 9, 15, tzinfo=IST), 200, 202, 199, 200),
        make_bar(datetime(2026, 9, 30, 9, 30, tzinfo=IST), 200, 203, 199, 202),
    ]

    vol = calculate_intraday_volatility(bars, 2, 25, 252)
    returns = [log(101 / 100), log(100.5 / 101), log(202 / 200)]
    expected = stdev(returns[-2:]) * sqrt(25 * 252) * 100

    assert vol == pytest.approx(expected)
    assert calculate_intraday_volatility(bars[:3], 3, 25, 252) is None


def test_as_of_boundary_includes_just_completed_bar_but_excludes_partial_and_future_bars():
    base = [
        make_bar(AS_OF.replace(hour=9, minute=15), 100, 102, 99, 101),
        make_bar(AS_OF.replace(hour=9, minute=30), 101, 104, 100, 103),
        make_bar(AS_OF.replace(hour=9, minute=45), 103, 104, 102, 103.5),
    ]
    partial = make_bar(AS_OF, 103.5, 5000, 10, 4000)
    future = make_bar(AS_OF + timedelta(minutes=15), 4000, 9000, 1, 8000)
    data = UnderlyingVolatilityInput(
        "TEST",
        bars_15m=tuple(base + [partial, future]),
        price_observations=(PriceObservation(AS_OF + timedelta(seconds=1), 99999),),
    )
    engine = VolatilityAnalysisEngine(short_config())

    result_with_future = engine.analyze(data, AS_OF)
    result_without_future = engine.analyze(
        UnderlyingVolatilityInput("TEST", bars_15m=tuple(base)), AS_OF
    )

    assert result_with_future.intraday_bars_used == 3
    assert result_with_future.current_price == pytest.approx(103.5)
    assert result_with_future.atr_15m == pytest.approx(result_without_future.atr_15m)
    assert result_with_future.intraday_volatility == pytest.approx(
        result_without_future.intraday_volatility
    )


def test_daily_bar_for_today_is_unavailable_until_session_close():
    past = [
        make_bar(datetime(2026, 9, day, 9, 15, tzinfo=IST), 100, 110 + day, 99, 100 + day)
        for day in (25, 28, 29)
    ]
    current_eod = make_bar(datetime(2026, 9, 30, 9, 15, tzinfo=IST), 100, 10000, 1, 9000)
    data = UnderlyingVolatilityInput("TEST", daily_bars=tuple(past + [current_eod]))
    engine = VolatilityAnalysisEngine(short_config())

    before_close = engine.analyze(data, AS_OF)
    after_close = engine.analyze(data, AS_OF.replace(hour=15, minute=30))

    assert before_close.daily_bars_used == 3
    assert after_close.daily_bars_used == 4
    assert before_close.atr == pytest.approx(calculate_atr(past, period=2))
    assert after_close.atr != before_close.atr


def test_full_15_minute_sessions_can_supply_completed_daily_bars():
    session_day = datetime(2026, 9, 29, 9, 15, tzinfo=IST)
    intraday = [
        make_bar(session_day + timedelta(minutes=15 * index),
                 100 + index, 102 + index, 99 + index, 101 + index)
        for index in range(25)
    ]
    result = VolatilityAnalysisEngine(short_config()).analyze(
        UnderlyingVolatilityInput("TEST", bars_15m=intraday), AS_OF
    )

    assert result.daily_bars_used == 1
    assert result.atr is None  # one daily close is insufficient for the configured ATR
    assert result.atr_15m is not None


def test_insufficient_history_is_reported_without_fabricating_metrics():
    data = UnderlyingVolatilityInput(
        "TEST",
        daily_bars=(make_bar(AS_OF - timedelta(days=1), 100, 101, 99, 100),),
        bars_15m=(make_bar(AS_OF.replace(hour=9, minute=15), 100, 101, 99, 100),),
    )

    result = VolatilityAnalysisEngine(short_config()).analyze(data, AS_OF)

    assert result.atr is None
    assert result.realized_volatility is None
    assert result.atr_15m is None
    assert result.intraday_volatility is None
    assert {"ATR", "REALIZED_VOLATILITY", "ATR_15M", "INTRADAY_VOLATILITY"} <= set(
        result.insufficient_data
    )


def test_option_atr_movement_and_realized_volatility_are_reported():
    option_bars = [
        make_bar(AS_OF.replace(hour=9, minute=15), 10, 12, 9, 11),
        make_bar(AS_OF.replace(hour=9, minute=30), 11, 14, 10, 13),
        make_bar(AS_OF.replace(hour=9, minute=45), 13, 15, 12, 14),
    ]
    option = OptionVolatilityInput(
        contract=make_contract(),
        bars_15m=option_bars,
        price_observations=(PriceObservation(AS_OF, 14),),
        implied_volatility_observations=(
            ImpliedVolatilityObservation(AS_OF - timedelta(minutes=1), 28.5),
            ImpliedVolatilityObservation(AS_OF + timedelta(seconds=1), 999.0),
        ),
        liquidity_qualified=True,
    )
    result = VolatilityAnalysisEngine(short_config()).analyze(
        UnderlyingVolatilityInput("TEST", options=(option,)), AS_OF
    )
    metrics = result.option_results[0]

    assert metrics.atr == pytest.approx(3.25)
    assert metrics.atr_percent == pytest.approx(3.25 / 14 * 100)
    assert metrics.realized_volatility is not None
    assert metrics.price_movement == pytest.approx(6)
    assert metrics.price_movement_percent == pytest.approx(6 / 14 * 100)
    assert metrics.implied_volatility == pytest.approx(28.5)
    assert metrics.used_for_score is True
    assert result.qualified_options_used == 1


def test_option_iv_is_none_when_unavailable_and_future_iv_is_not_used():
    option = OptionVolatilityInput(
        contract=make_contract(),
        implied_volatility_observations=(
            ImpliedVolatilityObservation(AS_OF + timedelta(seconds=1), 42.0),
        ),
    )
    result = VolatilityAnalysisEngine(short_config()).analyze(
        UnderlyingVolatilityInput("TEST", options=(option,)), AS_OF
    )

    assert result.option_results[0].implied_volatility is None
    assert result.option_movement_percent is None
    assert result.component_scores["option_movement"] == 0


def test_option_movement_requires_the_full_configured_bar_window():
    option = OptionVolatilityInput(
        contract=make_contract(),
        bars_15m=[
            make_bar(AS_OF.replace(hour=9, minute=15), 10, 12, 9, 11),
            make_bar(AS_OF.replace(hour=9, minute=30), 11, 14, 10, 13),
        ],
        liquidity_qualified=True,
    )
    result = VolatilityAnalysisEngine(short_config()).analyze(
        UnderlyingVolatilityInput("TEST", options=(option,)), AS_OF
    )

    metrics = result.option_results[0]
    assert metrics.price_movement is None
    assert metrics.used_for_score is False
    assert "OPTION_PRICE_MOVEMENT" in metrics.insufficient_data
    assert result.option_movement_percent is None


def test_unqualified_high_movement_option_cannot_raise_volatility_score():
    option_bars = [
        make_bar(AS_OF.replace(hour=9, minute=15) + timedelta(minutes=15 * i),
                 10 + 20 * i, 30 + 20 * i, 5 + 20 * i, 25 + 20 * i)
        for i in range(4)
    ]
    unqualified = OptionVolatilityInput(
        contract=make_contract(), bars_15m=option_bars, liquidity_qualified=False
    )
    qualified = OptionVolatilityInput(
        contract=make_contract("OPT2"), bars_15m=option_bars, liquidity_qualified=True
    )
    engine = VolatilityAnalysisEngine(short_config())
    without_qualified = engine.analyze(
        UnderlyingVolatilityInput("TEST", options=(unqualified,)),
        AS_OF + timedelta(minutes=15),
    )
    with_qualified = engine.analyze(
        UnderlyingVolatilityInput("TEST", options=(unqualified, qualified)),
        AS_OF + timedelta(minutes=15),
    )

    assert without_qualified.option_results[0].price_movement_percent > 0
    assert without_qualified.component_scores["option_movement"] == 0
    assert without_qualified.volatility_score == 0
    assert with_qualified.component_scores["option_movement"] > 0
    assert with_qualified.volatility_score > without_qualified.volatility_score
    assert with_qualified.qualified_options_used == 1


def test_configurable_weights_and_saturating_targets_bound_the_score():
    bars = [
        make_bar(AS_OF.replace(hour=9, minute=15), 100, 110, 90, 100),
        make_bar(AS_OF.replace(hour=9, minute=30), 100, 120, 80, 100),
        make_bar(AS_OF.replace(hour=9, minute=45), 100, 130, 70, 100),
    ]
    engine = VolatilityAnalysisEngine({
        **short_config(),
        "weights": {
            "atr_15m_percent": 1,
            "realized_volatility": 0,
            "intraday_volatility": 0,
            "option_movement": 0,
        },
        "normalization_targets": {"atr_15m_percent": 0.01},
    })
    result = engine.analyze(UnderlyingVolatilityInput("TEST", bars_15m=bars), AS_OF)

    assert result.component_scores["atr_15m_percent"] == 100
    assert result.volatility_score == 100
    assert all(0 <= value <= 100 for value in result.component_scores.values())


def test_invalid_volatility_config_is_rejected():
    with pytest.raises(VolatilityConfigError):
        VolatilityAnalysisEngine({"weights": {
            "atr_15m_percent": 0,
            "realized_volatility": 0,
            "intraday_volatility": 0,
            "option_movement": 0,
        }})
    with pytest.raises(VolatilityConfigError):
        VolatilityAnalysisEngine({"normalization_targets": {"atr_15m_percent": 0}})
    with pytest.raises(VolatilityConfigError):
        VolatilityAnalysisEngine({"windows": {"realized_volatility_periods": 1}})
