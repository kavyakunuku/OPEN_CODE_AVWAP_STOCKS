from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from common.models import OptionContract
from market.contract_discovery import DiscoveredContract, OptionContractDiscoveryResult
from market.liquidity import (
    ContractLiquidityData,
    ContractLiquidityResult,
    LiquidityObservation,
    OptionChainLiquidityReport,
    OptionChainLiquidityResult,
    OptionLiquidityEngine,
)
from market.opportunity import (
    OpportunityConfigError,
    OpportunityInputError,
    OpportunityRankingEngine,
    OpportunityScannerInput,
)
from market.volatility import (
    OHLCBar,
    OptionVolatilityResult,
    PriceObservation,
    UnderlyingVolatilityInput,
    VolatilityAnalysisEngine,
    VolatilityAnalysisResult,
)

IST = ZoneInfo("Asia/Kolkata")
AS_OF = datetime(2026, 9, 30, 10, 0, tzinfo=IST)
EXPIRY = date(2026, 10, 29)


def make_contract(symbol, security_id, side="CE", strike=650.0, expiry=EXPIRY):
    return OptionContract(
        security_id=security_id,
        symbol=f"{symbol} {strike:g} {side}",
        underlying=symbol,
        strike=strike,
        option_type=side,
        expiry=expiry.isoformat(),
        lot_size=250,
        tick_size=0.05,
    )


def make_discovery(symbol, contracts, *, timestamp=AS_OF, snapshot_as_of=None):
    snapshot_as_of = snapshot_as_of or timestamp - timedelta(days=1)
    calls, puts = [], []
    for index, contract in enumerate(contracts):
        target = calls if contract.option_type == "CE" else puts
        target.append(DiscoveredContract(contract, "ATM" if index == 0 else f"ITM{index}"))
    return OptionContractDiscoveryResult(
        underlying=symbol,
        timestamp=timestamp,
        underlying_price=1000.0,
        expiry=EXPIRY,
        atm_strike=contracts[0].strike,
        call_strike_interval=50.0,
        put_strike_interval=50.0,
        calls=calls,
        puts=puts,
        missing_contracts=[],
        max_itm=max(0, (len(contracts) + 1) // 2 - 1),
        snapshot_as_of=snapshot_as_of,
        metadata_warnings=[],
    )


def make_liquidity(contract, score=80.0, *, qualified=True, spread=0.30, age=0.0, rejection=()):
    return ContractLiquidityResult(
        security_id=contract.security_id,
        underlying=contract.underlying,
        strike=contract.strike,
        option_type=contract.option_type,
        expiry=contract.expiry,
        evaluated_at=AS_OF,
        observation_at=AS_OF - timedelta(seconds=age),
        quote_age_seconds=age,
        last_price=20.0,
        premium=19.9,
        volume=100000.0,
        open_interest=50000.0,
        oi_change=1000.0,
        depth=3000.0,
        spread_percent=spread,
        component_scores={"volume": 80.0, "oi": 80.0, "spread": 80.0, "depth": 80.0, "consistency": 80.0},
        score=score,
        qualified=qualified,
        rejection_reasons=tuple(rejection),
    )


def make_volatility(symbol, score=70.0, *, evaluated_at=AS_OF, option_results=()):
    return VolatilityAnalysisResult(
        symbol=symbol,
        evaluated_at=evaluated_at,
        current_price=1000.0,
        atr=10.0,
        atr_percent=1.0,
        atr_15m=2.0,
        atr_15m_percent=0.2,
        realized_volatility=20.0,
        intraday_volatility=18.0,
        option_movement_percent=5.0,
        volatility_score=score,
        component_scores={
            "atr_15m_percent": 75.0,
            "realized_volatility": 65.0,
            "intraday_volatility": 60.0,
            "option_movement": 50.0,
        },
        component_values={"atr_15m_percent": 0.2, "realized_volatility": 20.0},
        daily_bars_used=21,
        intraday_bars_used=80,
        qualified_options_used=1,
        insufficient_data=(),
        data_quality=(),
        option_results=tuple(option_results),
    )


def make_input(
    specs,
    *,
    as_of=AS_OF,
    existing_position_underlyings=(),
    existing_position_security_ids=(),
):
    """specs: (symbol, liquidity, volatility, chain, contracts[, quote spread, age])."""
    discoveries = []
    liquidity_rows = []
    chains = []
    volatilities = {}
    for spec in specs:
        symbol, liq_score, vol_score, chain_score, contracts = spec[:5]
        spread, age = spec[5:7] if len(spec) > 5 else (0.30, 0.0)
        discoveries.append(make_discovery(symbol, contracts, timestamp=as_of))
        liquidity_rows.extend(
            make_liquidity(contract, liq_score, spread=spread, age=age)
            for contract in contracts
        )
        chains.append(OptionChainLiquidityResult(
            underlying=symbol,
            expiry=EXPIRY.isoformat(),
            call_score=chain_score,
            put_score=chain_score,
            chain_score=chain_score,
            imbalance_ratio=0.0,
            penalty_points=0.0,
            call_contract_count=sum(c.option_type == "CE" for c in contracts),
            put_contract_count=sum(c.option_type == "PE" for c in contracts),
        ))
        volatilities[symbol] = make_volatility(symbol, vol_score, evaluated_at=as_of)
    return OpportunityScannerInput(
        as_of=as_of,
        discoveries=discoveries,
        liquidity_report=OptionChainLiquidityReport(tuple(liquidity_rows), tuple(chains)),
        volatility_by_underlying=volatilities,
        existing_position_underlyings=tuple(existing_position_underlyings),
        existing_position_security_ids=tuple(existing_position_security_ids),
    )


def test_opportunity_score_uses_default_weights_and_execution_inputs():
    contract = make_contract("VEDL", "V1", strike=650.0)
    data = make_input([("VEDL", 80.0, 60.0, 40.0, [contract], 0.40, 15.0)])

    result = OpportunityRankingEngine().rank(data)
    ranked = result.contracts[0]

    # Execution = 70% * 50 spread + 30% * 75 freshness = 57.5.
    # Opportunity = .40*80 + .30*60 + .20*40 + .10*57.5 = 63.75.
    assert ranked.execution_quality_score == pytest.approx(57.5)
    assert ranked.opportunity_score == pytest.approx(63.75)
    assert ranked.component_scores == {
        "liquidity": 80.0,
        "volatility": 60.0,
        "chain_quality": 40.0,
        "execution_quality": 57.5,
    }


def test_scanner_consumes_real_phase2_phase3_and_phase4_results():
    contract = make_contract("VEDL", "V1", strike=650.0)
    discovery = make_discovery("VEDL", [contract])
    liquidity_input = ContractLiquidityData(
        contract=contract,
        observations=(LiquidityObservation(
            observed_at=AS_OF - timedelta(seconds=2),
            last_price=19.95,
            volume=100000,
            open_interest=50000,
            bid=19.95,
            ask=20.0,
            bid_quantity=1000,
            ask_quantity=1000,
        ),),
    )
    phase3 = OptionLiquidityEngine().score_option_chain([liquidity_input], AS_OF)
    daily = [
        OHLCBar(datetime(2026, 9, day, 9, 15, tzinfo=IST), 100, 103 + i, 99, 101 + i)
        for i, day in enumerate((25, 28, 29))
    ]
    intraday = [
        OHLCBar(AS_OF.replace(hour=9, minute=15) + timedelta(minutes=15 * i),
                100 + i, 102 + i, 99 + i, 101 + i)
        for i in range(3)
    ]
    phase4 = VolatilityAnalysisEngine({"windows": {
        "atr_period": 2,
        "atr_15m_period": 2,
        "realized_volatility_periods": 2,
        "intraday_volatility_periods": 2,
    }}).analyze(
        UnderlyingVolatilityInput(
            "VEDL",
            daily_bars=daily,
            bars_15m=intraday,
            price_observations=(PriceObservation(AS_OF, 104.0),),
        ),
        AS_OF,
    )
    data = OpportunityScannerInput(
        as_of=AS_OF,
        discoveries=[discovery],
        liquidity_report=phase3,
        volatility_by_underlying={"VEDL": phase4},
    )

    result = OpportunityRankingEngine().rank(data)

    assert phase3.contracts[0].qualified is True
    assert result.contracts[0].security_id == "V1"
    assert result.contracts[0].volatility_score == phase4.volatility_score
    assert result.contracts[0].liquidity_score == phase3.contracts[0].score
    assert 0 <= result.contracts[0].opportunity_score <= 100


def test_weights_are_configurable_and_normalized():
    contract = make_contract("VEDL", "V1", strike=650.0)
    data = make_input([("VEDL", 80.0, 60.0, 40.0, [contract])])
    engine = OpportunityRankingEngine({"weights": {
        "liquidity": 2.0,
        "volatility": 0.0,
        "chain_quality": 0.0,
        "execution_quality": 0.0,
    }})

    result = engine.rank(data)

    assert result.contracts[0].opportunity_score == 80.0
    assert sum(result.weights.values()) == pytest.approx(1.0)


def test_unqualified_liquidity_is_a_hard_gate_even_with_maximum_volatility():
    contract = make_contract("VEDL", "V1", strike=650.0)
    discovery = make_discovery("VEDL", [contract])
    liquidity = replace(make_liquidity(contract, 99.0), qualified=False,
                        rejection_reasons=("WIDE_SPREAD",))
    data = OpportunityScannerInput(
        as_of=AS_OF,
        discoveries=[discovery],
        liquidity_report=OptionChainLiquidityReport(
            (liquidity,),
            (OptionChainLiquidityResult("VEDL", EXPIRY.isoformat(), 100, 100, 100, 0, 0, 1, 1),),
        ),
        volatility_by_underlying={"VEDL": make_volatility("VEDL", 100)},
    )

    result = OpportunityRankingEngine().rank(data)

    assert result.contracts == ()
    assert result.stocks == ()
    assert result.excluded_contracts[0].reasons == ("LIQUIDITY_NOT_QUALIFIED",)
    assert result.excluded_contracts[0].liquidity_rejection_reasons == ("WIDE_SPREAD",)


def test_call_and_put_qualify_independently_without_forcing_both_sides():
    call = make_contract("VEDL", "CALL1", "CE", 650.0)
    put = make_contract("VEDL", "PUT1", "PE", 650.0)
    discovery = make_discovery("VEDL", [call, put])
    liq_call = make_liquidity(call, 90.0, qualified=True)
    liq_put = replace(make_liquidity(put, 10.0, qualified=False),
                      rejection_reasons=("LOW_OI",))
    data = OpportunityScannerInput(
        as_of=AS_OF,
        discoveries=[discovery],
        liquidity_report=OptionChainLiquidityReport(
            (liq_call, liq_put),
            (OptionChainLiquidityResult("VEDL", EXPIRY.isoformat(), 90, 10, 42.5, 0.8, 20, 1, 1),),
        ),
        volatility_by_underlying={"VEDL": make_volatility("VEDL", 75)},
    )

    result = OpportunityRankingEngine().rank(data)

    assert [item.option_type for item in result.contracts] == ["CE"]
    assert len(result.excluded_contracts) == 1
    assert result.excluded_contracts[0].option_type == "PE"


def test_existing_position_blocks_new_opportunities_for_the_underlying():
    call = make_contract("VEDL", "OPEN1", "CE", 650.0)
    put = make_contract("VEDL", "NEW1", "PE", 650.0)
    data = make_input([("VEDL", 90.0, 95.0, 90.0, [call, put])],
                      existing_position_security_ids=["OPEN1"])

    result = OpportunityRankingEngine().rank(data)

    assert result.contracts == ()
    assert result.stocks == ()
    assert result.excluded_position_underlyings == ("VEDL",)
    assert {item.security_id for item in result.excluded_contracts} == {"OPEN1", "NEW1"}
    assert all(item.reasons == ("EXISTING_POSITION_UNDERLYING",)
               for item in result.excluded_contracts)


def test_top_n_stocks_contracts_and_top_opportunities_are_configurable():
    specs = []
    for symbol, score in (("VEDL", 90), ("RELIANCE", 80), ("SBIN", 70)):
        contracts = [
            make_contract(symbol, f"{symbol}-CE", "CE", 650.0),
            make_contract(symbol, f"{symbol}-PE", "PE", 650.0),
        ]
        specs.append((symbol, score, score, score, contracts))
    data = make_input(specs)
    engine = OpportunityRankingEngine({"selection": {
        "max_stocks": 2,
        "max_contracts_per_stock": 1,
        "max_top_opportunities": 1,
    }})

    result = engine.rank(data)

    assert [item.symbol for item in result.stocks] == ["VEDL", "RELIANCE"]
    assert [item.rank for item in result.stocks] == [1, 2]
    assert len(result.contracts) == 2
    assert {item.symbol for item in result.contracts} == {"VEDL", "RELIANCE"}
    assert all(item.qualified_contracts == 1 for item in result.stocks)
    assert len(result.top_opportunities) == 1
    assert result.top_opportunities[0] == result.contracts[0]


def test_historical_ranking_accepts_prior_master_but_rejects_future_or_mismatched_data():
    contract = make_contract("VEDL", "V1", strike=650.0)
    historical = make_discovery(
        "VEDL", [contract], snapshot_as_of=AS_OF - timedelta(days=4)
    )
    data = make_input([("VEDL", 80.0, 70.0, 60.0, [contract])])
    data = replace(data, discoveries=[historical])

    assert len(OpportunityRankingEngine().rank(data).contracts) == 1

    future_snapshot = replace(historical, snapshot_as_of=AS_OF + timedelta(seconds=1))
    with pytest.raises(OpportunityInputError, match="snapshot.*future"):
        OpportunityRankingEngine().rank(replace(data, discoveries=[future_snapshot]))

    future_volatility = replace(
        make_volatility("VEDL", 70), evaluated_at=AS_OF + timedelta(seconds=1)
    )
    with pytest.raises(OpportunityInputError, match="not evaluated at requested timestamp"):
        OpportunityRankingEngine().rank(
            replace(data, volatility_by_underlying={"VEDL": future_volatility})
        )


def test_missing_chain_score_is_zero_and_reported_not_redistributed():
    contract = make_contract("VEDL", "V1", strike=650.0)
    source = make_input([("VEDL", 80.0, 60.0, 40.0, [contract])])
    source = replace(source, liquidity_report=OptionChainLiquidityReport(
        source.liquidity_report.contracts, ()
    ))

    result = OpportunityRankingEngine().rank(source)
    ranked = result.contracts[0]

    assert ranked.chain_quality_score is None
    assert ranked.component_scores["chain_quality"] == 0.0
    assert ranked.missing_components == ("chain_quality",)
    assert ranked.opportunity_score == pytest.approx(60.0)


def test_deterministic_output_and_json_csv_serialization(tmp_path):
    contracts = [
        make_contract("VEDL", "V-CE", "CE", 650.0),
        make_contract("VEDL", "V-PE", "PE", 650.0),
    ]
    data = make_input([("VEDL", 80.0, 70.0, 60.0, contracts)])
    result = OpportunityRankingEngine().rank(data)
    reversed_data = replace(
        data,
        discoveries=list(reversed(data.discoveries)),
        liquidity_report=OptionChainLiquidityReport(
            tuple(reversed(data.liquidity_report.contracts)), data.liquidity_report.chains
        ),
    )
    reversed_result = OpportunityRankingEngine().rank(reversed_data)

    assert result.to_json() == reversed_result.to_json()
    payload = json.loads(result.to_json())
    assert set(("timestamp", "stocks", "contracts", "top_opportunities")) <= set(payload)
    assert "rank,symbol,contract_name" in result.to_csv("contracts")
    assert "rank,symbol,liquidity_score" in result.to_csv("stocks")
    json_path = tmp_path / "ranking.json"
    csv_path = tmp_path / "ranking.csv"
    result.write_json(json_path)
    result.write_csv(csv_path, "contracts")
    assert json.loads(json_path.read_text()) == payload
    assert csv_path.read_text() == result.to_csv("contracts")
    with pytest.raises(ValueError, match="section"):
        result.to_csv("unknown")


def test_option_movement_percent_is_a_raw_metric_not_a_bounded_score():
    contract = make_contract("VEDL", "V1", strike=650.0)
    option_result = OptionVolatilityResult(
        security_id="V1",
        option_type="CE",
        strike=650.0,
        expiry=EXPIRY.isoformat(),
        current_price=2.0,
        price_movement=3.0,
        price_movement_percent=150.0,
        atr=1.0,
        atr_percent=50.0,
        realized_volatility=100.0,
        implied_volatility=None,
        liquidity_qualified=True,
        used_for_score=True,
        insufficient_data=(),
    )
    data = make_input([("VEDL", 80.0, 70.0, 60.0, [contract])])
    data = replace(data, volatility_by_underlying={
        "VEDL": make_volatility("VEDL", 70, option_results=(option_result,))
    })

    result = OpportunityRankingEngine().rank(data)

    assert result.contracts[0].option_movement_percent == 150.0


def test_invalid_weights_and_date_only_as_of_are_rejected():
    with pytest.raises(OpportunityConfigError):
        OpportunityRankingEngine({"weights": {
            "liquidity": 0,
            "volatility": 0,
            "chain_quality": 0,
            "execution_quality": 0,
        }})
    with pytest.raises(OpportunityConfigError, match="liquidity weight"):
        OpportunityRankingEngine({"weights": {
            "liquidity": 0.20,
            "volatility": 0.40,
            "chain_quality": 0.30,
            "execution_quality": 0.10,
        }})
    contract = make_contract("VEDL", "V1", strike=650.0)
    data = make_input([("VEDL", 80.0, 70.0, 60.0, [contract])])
    with pytest.raises(OpportunityInputError, match="include a time"):
        OpportunityRankingEngine().rank(replace(data, as_of=date(2026, 9, 30)))
