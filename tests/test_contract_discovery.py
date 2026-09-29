from __future__ import annotations

from datetime import date, datetime

import pytest

from common.models import Candle, OptionContract
from common.utils import IST
from market.contract_discovery import (
    HistoricalContractSnapshotUnavailable,
    OptionContractCatalog,
    OptionContractDiscovery,
    contract_birth_date_from_candles,
    UnvalidatedUnderlying,
    detect_strike_interval,
    discover_option_contracts,
    get_atm_contract,
    get_call_contracts,
    get_expiry,
    get_put_contracts,
)


def make_contract(
    underlying: str,
    strike: float,
    option_type: str,
    *,
    expiry: str = "2026-09-29",
    token: str | None = None,
    birth: str | None = "2026-09-01",
    lot_size: int = 25,
    tick_size: float | None = 0.05,
    strike_interval: float | None = None,
) -> OptionContract:
    return OptionContract(
        security_id=token or f"{underlying}-{strike:g}-{option_type}-{expiry}",
        symbol=f"{underlying} {strike:g} {option_type}",
        underlying=underlying,
        strike=float(strike),
        option_type=option_type,
        expiry=expiry,
        lot_size=lot_size,
        exchange="NSE",
        exchange_segment="NSE_FNO",
        tick_size=tick_size,
        contract_birth_date=birth,
        strike_interval=strike_interval,
    )


def contracts_for_grid(
    underlying: str,
    strikes: list[float],
    *,
    expiry: str = "2026-09-29",
    birth: str | None = "2026-09-01",
    step: float | None = None,
) -> list[OptionContract]:
    return [
        make_contract(
            underlying, strike, option_type, expiry=expiry,
            birth=birth, strike_interval=step,
        )
        for strike in strikes
        for option_type in ("CE", "PE")
    ]


def engine_for(contracts, as_of="2026-09-01T08:45:00+05:30", *, symbols=("RELIANCE",)):
    catalog = OptionContractCatalog()
    catalog.add_snapshot(as_of, contracts, source="test master snapshot")
    return OptionContractDiscovery(catalog, validated_symbols=symbols)


def test_atm_uses_nearest_real_strike_and_ties_go_lower():
    strikes = list(range(900, 1101, 25))
    engine = engine_for(contracts_for_grid("RELIANCE", strikes))
    result = engine.discover_option_contracts(
        "RELIANCE", "2026-09-15 10:00", max_itm=0, underlying_price=1012.5
    )
    assert result.atm_strike == 1000
    assert result.get_atm_contract("CE").strike == 1000
    assert result.get_atm_contract("PE").strike == 1000


def test_ce_put_itm_directions_and_actual_nonstandard_interval():
    strikes = list(range(900, 1101, 25))
    engine = engine_for(contracts_for_grid("RELIANCE", strikes))
    result = engine.discover_option_contracts(
        "RELIANCE", datetime(2026, 9, 15, 10, 0, tzinfo=IST),
        max_itm=3, underlying_price=1012,
    )
    assert result.atm_strike == 1000
    assert result.call_strike_interval == 25
    assert result.put_strike_interval == 25
    assert [c.strike for c in result.get_call_contracts()] == [1000, 975, 950, 925]
    assert [c.strike for c in result.get_put_contracts()] == [1000, 1025, 1050, 1075]
    assert [c.moneyness for c in result.calls] == ["ATM", "ITM1", "ITM2", "ITM3"]
    assert [c.moneyness for c in result.puts] == ["ATM", "ITM1", "ITM2", "ITM3"]


def test_strike_interval_detection_uses_listed_strikes_not_fixed_50_or_100():
    assert detect_strike_interval([1000, 1025, 1050, 1075]) == 25
    assert detect_strike_interval([1000, 1025, 1050], atm=1025) == 25
    assert detect_strike_interval([10.0, 12.5, 15.0]) == 2.5
    # An omitted strike does not force an assumed step of 50/100.
    assert detect_strike_interval([100, 120, 130]) == 10
    assert detect_strike_interval([100]) is None


def test_nonuniform_grids_follow_actual_available_strikes_and_report_local_gaps():
    strikes = [900, 925, 950, 1000, 1050, 1100]
    result = engine_for(contracts_for_grid("RELIANCE", strikes)).discover_option_contracts(
        "RELIANCE", "2026-09-15 10:00", max_itm=4, underlying_price=1001
    )
    assert [item.contract.strike for item in result.calls] == [1000, 950, 925, 900]
    assert [item.strike_interval for item in result.calls] == [None, 50, 25, 25]
    assert [item.contract.strike for item in result.puts] == [1000, 1050, 1100]
    assert [item.strike_interval for item in result.puts] == [None, 50, 50]
    assert result.missing_contracts == ["ITM4 CE", "ITM3 PE", "ITM4 PE"]


def test_explicit_contract_interval_metadata_is_preferred():
    rows = contracts_for_grid("RELIANCE", [1400, 1450, 1500, 1550, 1600], step=50)
    # Make the metadata interval intentionally distinct from a common stock grid.
    rows = [
        OptionContract(**{**c.__dict__, "strike_interval": 25})
        for c in rows
    ]
    engine = engine_for(rows)
    result = engine.discover_option_contracts(
        "RELIANCE", "2026-09-15 10:00", max_itm=2, underlying_price=1500
    )
    assert result.call_strike_interval == 25
    assert result.put_strike_interval == 25
    assert [x.strike for x in result.get_call_contracts()] == [1500, 1450]
    assert [x.strike for x in result.get_put_contracts()] == [1500, 1550]
    assert result.missing_contracts == ["ITM1 CE", "ITM1 PE"]


def test_existing_monthly_expiry_rule_is_reused_with_24th_switch():
    expiries = ["2026-09-08", "2026-09-29", "2026-10-06", "2026-10-27"]
    rows = [
        c for expiry in expiries
        for c in contracts_for_grid(
            "RELIANCE", [1450, 1500, 1550], expiry=expiry
        )
    ]
    engine = engine_for(rows)
    before_switch = engine.discover_option_contracts(
        "RELIANCE", "2026-09-23 10:00", max_itm=0, underlying_price=1500
    )
    on_switch = engine.discover_option_contracts(
        "RELIANCE", "2026-09-24 10:00", max_itm=0, underlying_price=1500
    )
    assert before_switch.expiry == date(2026, 9, 29)
    assert on_switch.expiry == date(2026, 10, 27)
    assert before_switch.get_expiry() == date(2026, 9, 29)


def test_expired_contracts_are_ignored_and_expiry_day_expires_at_close():
    expiries = ["2026-09-14", "2026-09-23", "2026-10-27"]
    rows = [
        c for expiry in expiries
        for c in contracts_for_grid("RELIANCE", [1450, 1500, 1550], expiry=expiry)
    ]
    engine = engine_for(rows)
    before_close = engine.discover_option_contracts(
        "RELIANCE", "2026-09-23 15:29:59", max_itm=0, underlying_price=1500
    )
    at_close = engine.discover_option_contracts(
        "RELIANCE", "2026-09-23 15:30:00", max_itm=0, underlying_price=1500
    )
    assert before_close.expiry == date(2026, 9, 23)
    assert at_close.expiry == date(2026, 10, 27)
    assert all(c.expiry != "2026-09-14" for c in at_close.contracts)


def test_missing_strikes_are_reported_without_fabricating_contracts():
    strikes = list(range(1200, 1801, 50))
    rows = contracts_for_grid("RELIANCE", strikes)
    rows = [
        c for c in rows
        if not (c.option_type == "CE" and c.strike == 1200)
        and not (c.option_type == "PE" and c.strike == 1750)
    ]
    result = engine_for(rows).discover_option_contracts(
        "RELIANCE", "2026-09-15 10:00", max_itm=6, underlying_price=1499
    )
    assert result.requested_count == 14
    assert result.available_count == 12
    assert result.missing_contracts == ["ITM6 CE", "ITM5 PE"]
    assert all(c.security_id in {row.security_id for row in rows} for c in result.contracts)
    assert not any(c.option_type == "CE" and c.strike == 1200 for c in result.contracts)
    assert not any(c.option_type == "PE" and c.strike == 1750 for c in result.contracts)


def test_contract_identity_and_instrument_token_are_preserved():
    row = make_contract(
        "RELIANCE", 1500, "CE", token="987654321", lot_size=250,
        tick_size=0.05, birth="2026-09-01",
    )
    result = engine_for([row]).discover_option_contracts(
        "RELIANCE", "2026-09-15 10:00", max_itm=0, underlying_price=1500
    )
    selected = result.get_atm_contract("CE")
    assert selected.security_id == "987654321"
    assert selected.instrument_token == "987654321"
    payload = result.calls[0].to_dict()
    assert payload["underlying"] == "RELIANCE"
    assert payload["symbol"] == row.symbol
    assert payload["expiry"] == "2026-09-29"
    assert payload["strike"] == 1500
    assert payload["option_type"] == "CE"
    assert payload["instrument_token"] == "987654321"
    assert payload["exchange"] == "NSE"
    assert payload["lot_size"] == 250
    assert payload["tick_size"] == 0.05
    assert payload["contract_birth_date"] == "2026-09-01"


def test_option_candle_birth_date_helper_uses_first_contract_candle():
    later = int(datetime(2026, 9, 3, 9, 15, tzinfo=IST).timestamp())
    earlier = int(datetime(2026, 9, 2, 9, 15, tzinfo=IST).timestamp())
    candles = [
        Candle("R-OPT", later, 1, 1, 1, 1),
        Candle("R-OPT", earlier, 1, 1, 1, 1),
    ]
    assert contract_birth_date_from_candles(candles) == "2026-09-02"
    assert contract_birth_date_from_candles([]) is None


def test_birth_date_provider_enriches_only_from_option_contract_source():
    rows = contracts_for_grid("RELIANCE", [1450, 1500, 1550], birth=None)
    catalog = OptionContractCatalog()
    catalog.add_snapshot("2026-09-01T08:45:00+05:30", rows)
    engine = OptionContractDiscovery(
        catalog,
        validated_symbols=["RELIANCE"],
        birth_date_provider=lambda contract, at: "2026-09-01",
    )
    result = engine.discover_option_contracts(
        "RELIANCE", "2026-09-15 10:00", max_itm=0, underlying_price=1500
    )
    assert result.get_atm_contract("CE").contract_birth_date == "2026-09-01"
    assert not result.metadata_warnings


def test_birth_date_provider_excludes_a_contract_not_born_by_asof():
    rows = contracts_for_grid("RELIANCE", [1450, 1500, 1550], birth=None)
    catalog = OptionContractCatalog()
    catalog.add_snapshot("2026-09-01T08:45:00+05:30", rows)
    engine = OptionContractDiscovery(
        catalog,
        validated_symbols=["RELIANCE"],
        birth_date_provider=lambda contract, at: (
            "2026-09-16" if contract.option_type == "CE" else "2026-09-01"
        ),
    )
    result = engine.discover_option_contracts(
        "RELIANCE", "2026-09-15 10:00", max_itm=0, underlying_price=1500
    )
    assert result.get_atm_contract("CE") is None
    assert result.get_atm_contract("PE").contract_birth_date == "2026-09-01"
    assert result.missing_contracts == ["ATM CE"]


def test_contracts_born_after_historical_timestamp_are_excluded():
    expiry = "2025-08-28"
    rows = contracts_for_grid(
        "RELIANCE", [1400, 1450, 1500, 1550, 1600],
        expiry=expiry, birth="2025-08-01",
    )
    # The CE at 1450 is absent from the as-of master and appears only in a
    # later metadata row; the PE at that strike still establishes the grid.
    rows = [c for c in rows if not (c.strike == 1450 and c.option_type == "CE")]
    # This exact option row did not exist yet on the requested historical date.
    future_born = make_contract(
        "RELIANCE", 1450, "CE", expiry=expiry, token="NOT-YET-LISTED",
        birth="2025-08-16",
    )
    rows.append(future_born)
    catalog = OptionContractCatalog()
    catalog.add_snapshot("2025-08-15T09:00:00+05:30", rows, "archived 2025 master")
    # A later master has a token that must never leak into the earlier replay.
    catalog.add_snapshot(
        "2026-09-01T09:00:00+05:30",
        [make_contract("RELIANCE", 1450, "CE", expiry=expiry, token="FUTURE-MASTER")],
        "present-day master",
    )
    engine = OptionContractDiscovery(catalog, validated_symbols=["RELIANCE"])
    result = engine.discover_option_contracts(
        "RELIANCE", "2025-08-15 10:00", max_itm=1, underlying_price=1512
    )
    assert result.expiry == date(2025, 8, 28)
    assert result.snapshot_as_of == datetime(2025, 8, 15, 9, 0, tzinfo=IST)
    assert "NOT-YET-LISTED" not in {c.security_id for c in result.contracts}
    assert "FUTURE-MASTER" not in {c.security_id for c in result.contracts}
    assert "ITM1 CE" in result.missing_contracts
    assert all(c.contract_birth_date == "2025-08-01" for c in result.contracts)


def test_historical_lookup_refuses_to_use_a_future_only_snapshot():
    catalog = OptionContractCatalog()
    catalog.add_snapshot(
        "2026-09-01T09:00:00+05:30",
        contracts_for_grid("RELIANCE", [1450, 1500, 1550]),
    )
    engine = OptionContractDiscovery(catalog, validated_symbols=["RELIANCE"])
    with pytest.raises(HistoricalContractSnapshotUnavailable, match="refusing to use"):
        engine.discover_option_contracts(
            "RELIANCE", "2025-08-15 10:00", underlying_price=1500
        )


def test_price_provider_receives_normalized_historical_timestamp():
    calls = []

    def price_provider(symbol, timestamp):
        calls.append((symbol, timestamp))
        return 1500

    engine = OptionContractDiscovery(
        engine_for(contracts_for_grid("RELIANCE", [1450, 1500, 1550])).catalog,
        price_provider=price_provider,
        validated_symbols=["RELIANCE"],
    )
    engine.discover_option_contracts("RELIANCE", "2026-09-15T04:30:00Z", max_itm=0)
    assert calls == [("RELIANCE", datetime(2026, 9, 15, 10, 0, tzinfo=IST))]


def test_public_discover_function_and_result_helpers():
    rows = contracts_for_grid("RELIANCE", [1450, 1500, 1550])
    catalog = OptionContractCatalog()
    catalog.add_snapshot("2026-09-01T08:45:00+05:30", rows)
    result = discover_option_contracts(
        "RELIANCE", "2026-09-15 10:00", max_itm=1,
        catalog=catalog, underlying_price=1500,
        validated_symbols=["RELIANCE"],
    )
    assert get_atm_contract(result, "CALL").strike == 1500
    assert len(get_call_contracts(result)) == 2
    assert len(get_put_contracts(result)) == 2
    assert get_expiry(result) == date(2026, 9, 29)


def test_phase1_validated_symbol_gate_is_enforced():
    engine = engine_for(
        contracts_for_grid("RELIANCE", [1450, 1500, 1550]),
        symbols=("RELIANCE",),
    )
    with pytest.raises(UnvalidatedUnderlying):
        engine.discover_option_contracts(
            "ICICIBANK", "2026-09-15 10:00", underlying_price=1500
        )


def test_catalog_save_load_preserves_asof_contract_metadata(tmp_path):
    row = make_contract(
        "RELIANCE", 1500, "CE", token="R-1500-CE", birth="2025-08-01",
        expiry="2025-08-28", lot_size=250,
    )
    catalog = OptionContractCatalog()
    catalog.add_snapshot("2025-08-15T09:00:00+05:30", [row], "archive")
    path = tmp_path / "contracts.json.gz"
    catalog.save(str(path))
    restored = OptionContractCatalog.load(str(path))
    loaded = restored.snapshot_for("2025-08-15 10:00").contracts[0]
    assert loaded.instrument_token == "R-1500-CE"
    assert loaded.contract_birth_date == "2025-08-01"
    assert loaded.exchange == "NSE"
    assert loaded.tick_size == 0.05
