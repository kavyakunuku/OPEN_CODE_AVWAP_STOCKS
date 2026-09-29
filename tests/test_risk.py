"""Risk controls (spec §35): gates are separate from the strategy signal."""
from __future__ import annotations

from common.models import OptionContract
from conftest import ts
from portfolio.risk import RiskManager
from storage.database import Database


def _rm(tmp_path, **over):
    risk_cfg = {
        "quantity_per_trade": None,
        "max_open_positions": 8,
        "max_trades_per_day": 20,
        "max_daily_loss": 25000,
        "default_lot_size": 250,
    }
    risk_cfg.update(over)
    db = Database(str(tmp_path / "risk.db"))
    return RiskManager(db, {"risk": risk_cfg})


def test_no_blocks_by_default(tmp_path):
    r = _rm(tmp_path)
    assert r.entry_block_reasons(open_positions=0, trades_today=0, daily_pnl=0.0) == []
    assert r.flags() == {"emergency_stop": False, "disable_new_entries": False}


def test_emergency_stop_blocks_entries(tmp_path):
    r = _rm(tmp_path)
    r.set_emergency_stop(True)
    reasons = r.entry_block_reasons(0, 0, 0.0)
    assert "EMERGENCY_STOP_ENGAGED" in reasons
    # persists (kv table)
    r2 = RiskManager(r.db, {"risk": r.cfg})
    assert r2.flags()["emergency_stop"] is True
    r2.set_emergency_stop(False)
    assert RiskManager(r.db, {"risk": r.cfg}).flags()["emergency_stop"] is False


def test_disable_entries_switch(tmp_path):
    r = _rm(tmp_path)
    r.set_disable_entries(True)
    assert "NEW_ENTRIES_DISABLED" in r.entry_block_reasons(0, 0, 0.0)
    r.set_disable_entries(False)
    assert r.entry_block_reasons(0, 0, 0.0) == []


def test_max_open_positions(tmp_path):
    r = _rm(tmp_path)
    assert r.entry_block_reasons(7, 0, 0.0) == []
    assert "MAX_OPEN_POSITIONS(8)" in r.entry_block_reasons(8, 0, 0.0)


def test_max_trades_per_day(tmp_path):
    r = _rm(tmp_path)
    assert r.entry_block_reasons(0, 19, 0.0) == []
    assert "MAX_TRADES_PER_DAY(20)" in r.entry_block_reasons(0, 20, 0.0)


def test_max_daily_loss(tmp_path):
    r = _rm(tmp_path)
    assert r.entry_block_reasons(0, 0, -24999.0) == []
    assert "MAX_DAILY_LOSS(25000)" in r.entry_block_reasons(0, 0, -25000.0)
    assert "MAX_DAILY_LOSS(25000)" in r.entry_block_reasons(0, 0, -90000.0)


def test_quantity_defaults_to_lot_size(tmp_path):
    r = _rm(tmp_path)
    c = OptionContract("S", "T 1500 CE", "T", 1500.0, "CE", "2026-09-29", 25)
    assert r.quantity_for(c) == 25  # lot size
    r2 = _rm(tmp_path, quantity_per_trade=75)
    assert r2.quantity_for(c) == 75  # explicit config wins
    c_nolot = OptionContract("S", "T 1500 CE", "T", 1500.0, "CE", "2026-09-29", 0)
    assert r.quantity_for(c_nolot) == 250  # fallback default


def test_daily_pnl_includes_unrealized(tmp_path):
    r = _rm(tmp_path)
    pos = [
        {"security_id": "A", "entry_price": 100.0, "quantity": 25},
        {"security_id": "B", "entry_price": 50.0, "quantity": 100},
    ]
    ltp = {"A": 90.0, "B": 60.0}
    # realized 0 + (100-90)*25 + (50-60)*100 = 250 - 1000 = -750
    assert r.daily_pnl("2026-09-15", pos, lambda s: ltp.get(s)) == -750.0
