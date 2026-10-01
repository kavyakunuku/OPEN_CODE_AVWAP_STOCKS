from datetime import date

from common.models import OptionContract
from market.universe import UniverseManager


def _contract(security_id, side, strike):
    return OptionContract(
        security_id=security_id,
        symbol=f"TEST {strike:g} {side}",
        underlying="TEST",
        strike=strike,
        option_type=side,
        expiry="2026-10-29",
        lot_size=25,
    )


def test_none_restores_legacy_scanner_and_empty_allowlist_keeps_positions():
    atm_ce = _contract("CE_ATM", "CE", 100)
    atm_pe = _contract("PE_ATM", "PE", 100)
    itm_ce = _contract("CE_ITM", "CE", 90)
    itm_pe = _contract("PE_ITM", "PE", 110)
    manager = UniverseManager(it_count=1)
    manager.update(
        "TEST", date(2026, 10, 29), 100, [90, 100, 110],
        {(90, "CE"): itm_ce, (100, "CE"): atm_ce,
         (100, "PE"): atm_pe, (110, "PE"): itm_pe},
    )
    manager.add_position_id("OPEN_POSITION")

    assert manager.scanner_ids() == {"CE_ATM", "PE_ATM", "CE_ITM", "PE_ITM"}
    manager.set_eligible_scanner_ids({"CE_ATM"})
    assert manager.monitored_ids() == {"CE_ATM", "OPEN_POSITION"}
    manager.set_eligible_scanner_ids(set())
    assert manager.scanner_ids() == set()
    assert manager.monitored_ids() == {"OPEN_POSITION"}
    manager.set_eligible_scanner_ids(None)
    assert manager.scanner_ids() == {"CE_ATM", "PE_ATM", "CE_ITM", "PE_ITM"}
