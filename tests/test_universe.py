"""Expiry selection, ATM, ATM+6 ITM, dynamic ATM + position universe
(spec §4-§7, §30, §48)."""
from __future__ import annotations

from datetime import date

from common.models import OptionContract
from market.universe import (
    UniverseManager,
    ce_universe_strikes,
    find_atm,
    pe_universe_strikes,
    select_monthly_expiry,
)

SEP = [date(2026, 9, d) for d in (1, 8, 15, 29)]
OCT = [date(2026, 10, d) for d in (6, 13, 27)]


def test_expiry_current_month_before_switch():
    exps = SEP + OCT
    assert select_monthly_expiry(date(2026, 9, 1), exps, 24) == date(2026, 9, 29)
    assert select_monthly_expiry(date(2026, 9, 15), exps, 24) == date(2026, 9, 29)
    assert select_monthly_expiry(date(2026, 9, 23), exps, 24) == date(2026, 9, 29)


def test_expiry_switch_on_24th():
    exps = SEP + OCT
    assert select_monthly_expiry(date(2026, 9, 24), exps, 24) == date(2026, 10, 27)
    assert select_monthly_expiry(date(2026, 9, 30), exps, 24) == date(2026, 10, 27)


def test_expiry_current_month_passed_falls_forward():
    # only earlier September expiries left (e.g. 22nd is a trading day,
    # expiry was the 15th), today is the 23rd -> must use next month
    exps = [date(2026, 9, 15)] + OCT
    assert select_monthly_expiry(date(2026, 9, 23), exps, 24) == date(2026, 10, 27)
    # nothing in the future at all
    assert select_monthly_expiry(date(2026, 9, 23), [date(2026, 9, 15)], 24) is None


def test_expiry_uses_actual_dates_not_assumptions():
    # unusual expiry grid (weekly expiries all over the place)
    exps = [date(2026, 9, 25), date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 20)]
    assert select_monthly_expiry(date(2026, 9, 2), exps, 24) == date(2026, 9, 29)
    assert select_monthly_expiry(date(2026, 9, 24), exps, 24) == date(2026, 10, 20)


def test_atm_exact_and_tie():
    assert find_atm(1520.0, [1500, 1520, 1540]) == 1520
    assert find_atm(1510.0, [1500, 1520]) == 1500  # tie -> lower strike
    assert find_atm(1511.0, [1500, 1520]) == 1520  # closer wins
    assert find_atm(1519.0, [1500, 1520]) == 1520
    assert find_atm(0.0, [1500]) is None
    assert find_atm(1520.0, []) is None


def test_atm_plus_6_itm_each_side():
    strikes = list(range(1400, 1641, 20))
    ce = ce_universe_strikes(1520.0, strikes, 6)
    pe = pe_universe_strikes(1520.0, strikes, 6)
    assert ce == [1520, 1500, 1480, 1460, 1440, 1420, 1400]
    assert pe == [1520, 1540, 1560, 1580, 1600, 1620, 1640]


def test_atm_plus_6_when_fewer_strikes_available():
    strikes = [1500, 1520, 1540]
    assert ce_universe_strikes(1520.0, strikes, 6) == [1520, 1500]
    assert pe_universe_strikes(1520.0, strikes, 6) == [1520, 1540]


def test_dynamic_atm_does_not_drop_open_positions():
    u = UniverseManager(it_count=1)
    ce_atm = OptionContract("S1", "T 1500 CE", "T", 1500.0, "CE", "2026-09-29", 25)
    ce_below = OptionContract("S2", "T 1480 CE", "T", 1480.0, "CE", "2026-09-29", 25)
    ce_above = OptionContract("S3", "T 1520 CE", "T", 1520.0, "CE", "2026-09-29", 25)
    ce_top = OptionContract("S4", "T 1540 CE", "T", 1540.0, "CE", "2026-09-29", 25)
    contracts = {
        (1480.0, "CE"): ce_below, (1500.0, "CE"): ce_atm,
        (1520.0, "CE"): ce_above, (1540.0, "CE"): ce_top,
    }
    u.update("T", date(2026, 9, 29), 1500.0, [1480.0, 1500.0, 1520.0, 1540.0], contracts)
    assert u.scanner_ids() == {"S1", "S2"}  # ATM + 1 ITM

    # underlying rallies, ATM moves 1500 -> 1540: 1480 CE falls OUT of the
    # scanner universe
    u.update("T", date(2026, 9, 29), 1540.0, [1480.0, 1500.0, 1520.0, 1540.0], contracts)
    u.add_position_id("S2")
    assert "S2" not in u.scanner_ids()
    assert "S2" in u.monitored_ids()  # position universe takes priority

    # position closed -> back to scanner-only monitoring
    u.remove_position_id("S2")
    assert "S2" not in u.monitored_ids()


def test_scanner_and_position_universe_are_separate():
    u = UniverseManager(it_count=1)
    c_atm = OptionContract("B", "T 1460 CE", "T", 1460.0, "CE", "2026-09-29", 25)
    c_other = OptionContract("A", "T 1500 CE", "T", 1500.0, "CE", "2026-09-29", 25)
    contracts = {(1460.0, "CE"): c_atm, (1500.0, "CE"): c_other}
    # spot at 1460 -> ATM = 1460 -> CE scanner = [1460] only (nothing ITM below)
    u.update("T", date(2026, 9, 29), 1460.0, [1460.0, 1500.0], contracts)
    assert u.scanner_ids() == {"B"}
    # open a position in a contract that is NOT in the scanner universe
    u.add_position_id("A")
    assert u.monitored_ids() == {"A", "B"}
    assert u.scanner_ids() == {"B"}  # scanner unchanged
    u.remove_position_id("A")
    assert u.monitored_ids() == {"B"}
