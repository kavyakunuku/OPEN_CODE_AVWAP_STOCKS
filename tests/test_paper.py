"""Paper broker: fill conventions and P&L (spec §18, §25, §45)."""
from __future__ import annotations

import tempfile
from datetime import datetime

from common.models import Signal
from common.utils import IST
from conftest import C, World, ts


def _entry_signal(sec= "SEC_CE_ATM"):
    return Signal(
        security_id=sec, action="ENTRY_SELL", candle_ts=ts(15, 9, 45),
        signal_price=101.0, avwap=102.0, prev_close=104.0, prev_avwap=102.0,
        reason="CROSS_BELOW_AVWAP", symbol="TESTA 1500 CE", underlying="TESTA",
        strike=1500.0, option_type="CE", expiry="2026-09-29",
        created_at=ts(15, 10, 0),
    )


def test_paper_pnl_formula_spec_example():
    """SELL at 100, BUY-TO-CLOSE at 80, qty 250 -> P&L = 5000 (spec §45)."""
    d = tempfile.mkdtemp()
    w = World(d, with_broker_execution=False)
    res = w.paper.execute_entry(w.ce, 250, _entry_signal(), fill_price=100.0)
    assert res.is_filled and res.avg_price == 100.0
    pos = w.positions.find_by_security("SEC_CE_ATM")
    exit_sig = Signal(
        security_id="SEC_CE_ATM", action="EXIT_BUY", candle_ts=ts(15, 13, 45),
        signal_price=80.0, avwap=79.0, prev_close=78.0, prev_avwap=81.0,
        reason="CLOSE_ABOVE_AVWAP", symbol="TESTA 1500 CE", underlying="TESTA",
        strike=1500.0, option_type="CE", expiry="2026-09-29",
        created_at=ts(15, 13, 45),
    )
    res2 = w.paper.execute_exit(pos, exit_sig, fill_price=80.0)
    assert res2.is_filled
    closed = w.db.get_position(pos["position_id"])
    assert abs(closed["pnl"] - 5000.0) < 1e-9
    assert closed["status"] == "CLOSED"
    assert closed["entry_time"] is not None and closed["exit_time"] is not None


def test_paper_defaults_to_candle_close():
    d = tempfile.mkdtemp()
    w = World(d, with_broker_execution=False)
    res = w.paper.execute_entry(w.ce, 25, _entry_signal())  # no fill_price given
    assert res.is_filled
    assert res.avg_price == 101.0  # signal (candle close) price


def test_paper_next_quote_fill_mode():
    d = tempfile.mkdtemp()
    w = World(d, with_broker_execution=False)
    from execution.paper import PaperBroker
    nb = PaperBroker(w.db, w.positions, w.journal, fill_mode="next_quote")
    res = nb.execute_entry(w.ce, 25, _entry_signal(), fill_price=100.5)
    assert res.is_filled and res.avg_price == 100.5


def test_paper_slippage_against_trader():
    d = tempfile.mkdtemp()
    w = World(d, with_broker_execution=False)
    from execution.paper import PaperBroker
    nb = PaperBroker(w.db, w.positions, w.journal, fill_mode="candle_close",
                     slippage_bps=10)
    res = nb.execute_entry(w.ce, 25, _entry_signal())  # sell fills lower
    assert abs(res.avg_price - 101.0 * (1 - 10 / 10_000)) < 1e-9
    pos = w.positions.find_by_security("SEC_CE_ATM")
    exit_sig = Signal(
        security_id="SEC_CE_ATM", action="EXIT_BUY", candle_ts=ts(15, 13, 45),
        signal_price=80.0, avwap=79.0, prev_close=78.0, prev_avwap=81.0,
        reason="CLOSE_ABOVE_AVWAP", symbol="TESTA 1500 CE", underlying="TESTA",
        strike=1500.0, option_type="CE", expiry="2026-09-29",
        created_at=ts(15, 13, 45),
    )
    res2 = nb.execute_exit(pos, exit_sig)  # buy fills higher
    assert abs(res2.avg_price - 80.0 * (1 + 10 / 10_000)) < 1e-9


def test_paper_no_duplicate_entry():
    d = tempfile.mkdtemp()
    w = World(d, with_broker_execution=False)
    sig = _entry_signal()
    r1 = w.paper.execute_entry(w.ce, 25, sig)
    assert r1.is_filled
    r2 = w.paper.execute_entry(w.ce, 25, sig)  # duplicate
    assert r2.status == "REJECTED"
    assert len(w.open_positions()) == 1


def test_paper_journey_from_signals(world: World):
    """Signal -> paper sell -> paper buy-to-close -> journal entries."""
    world.feed(
        C("SEC_CE_ATM", 15, 9, 15, close=100, vol=10),
        C("SEC_CE_ATM", 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C("SEC_CE_ATM", 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),
        C("SEC_CE_ATM", 15, 10, 0, close=99, vol=10, open_=101, high=101, low=98.9),
        C("SEC_CE_ATM", 15, 10, 15, close=106, vol=10, open_=99, high=106, low=99),
    )
    journal = [dict(r) for r in world.db._query("SELECT * FROM journal")]
    events = {j["event"] for j in journal}
    assert "SIGNAL" in events
    assert "PAPER_ENTRY" in events
    assert "PAPER_EXIT" in events
    closed = world.closed_positions()
    assert len(closed) == 1
    assert abs(closed[0]["pnl"] - (-125.0)) < 1e-9
