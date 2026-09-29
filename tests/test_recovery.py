"""Restart recovery (spec §39, §48): open positions and AVWAP state must
survive an application restart; an open position outside the current
ATM+6 scanner universe must keep being monitored for its exit."""
from __future__ import annotations

from datetime import date

from common.models import OptionContract
from common.utils import candle_end_for, from_epoch
from conftest import C, World, ts
from market.universe import UniverseManager

CE = "SEC_CE_ATM"
CE2 = "SEC_CE_1520"


def test_positions_and_avwap_survive_restart(tmp_path):
    # ---- run 1: entry happens, then the app dies with the position OPEN
    w1 = World(tmp_path)
    w1.feed(
        C(CE, 15, 9, 15, close=100, vol=10),
        C(CE, 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C(CE, 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),  # entry
        C(CE, 15, 10, 0, close=99, vol=10, open_=101, high=101, low=98.9),      # hold
    )
    assert len(w1.open_positions()) == 1
    state1 = w1.avwap.get(CE)
    assert state1.last_close == 99.0
    assert abs(state1.last_avwap - 101.44167) < 1e-4
    pos_before = w1.positions.find_by_security(CE)
    assert pos_before is not None and pos_before["status"] == "OPEN"

    # ---- run 2: everything rebuilt from the database
    w2 = World(tmp_path)
    # AVWAP state restored exactly
    state2 = w2.avwap.get(CE)
    assert abs(state2.last_avwap - state1.last_avwap) < 1e-12
    assert state2.cumulative_volume == state1.cumulative_volume
    assert state2.cumulative_price_volume == state1.cumulative_price_volume
    assert state2.anchor_ts == state1.anchor_ts == ts(15, 9, 15)
    # open position restored
    pos2 = w2.positions.find_by_security(CE)
    assert pos2 is not None
    assert pos2["position_id"] == pos_before["position_id"]
    assert pos2["status"] == "OPEN"

    # the next candle routes to the EXIT branch using RESTORED prev state
    w2.now.t = candle_end_for(from_epoch(ts(15, 10, 15)))
    out = w2.feed(C(CE, 15, 10, 15, close=106, vol=10, open_=99, high=106, low=99))
    sigs = [s for s in out if s is not None]
    assert len(sigs) == 1
    assert sigs[0].action == "EXIT_BUY"
    assert abs(sigs[0].prev_close - 99.0) < 1e-9          # from restored state
    assert abs(sigs[0].prev_avwap - 101.44167) < 1e-4     # from restored state
    closed = w2.closed_positions()
    assert len(closed) == 1
    assert abs(closed[0]["pnl"] - (-125.0)) < 1e-9


def test_open_position_outside_scanner_universe_is_monitored(tmp_path):
    """The position universe takes priority over the entry scanner universe."""
    w = World(tmp_path)
    # hold a short of CE2 (1520 CE) ...
    w.positions.open_position(
        contract=w.ce2, quantity=25, entry_price=100.0, ts=ts(15, 9, 30),
        reason="CROSS_BELOW_AVWAP", avwap=101.0, mode="PAPER",
    )
    w.feed(
        C(CE2, 15, 9, 15, close=100, vol=10),
        C(CE2, 15, 9, 30, close=98, vol=10, open_=100, high=100, low=97.9),
    )
    assert len(w.open_positions()) == 1

    # fresh universe on restart: scanner is built around the CURRENT atm
    # (1560, ITM depth 1) and does NOT include 1520 CE ...
    u = UniverseManager(it_count=1)
    u.update("TESTA", date(2026, 9, 29), 1560.0,
             [1480.0, 1500.0, 1520.0, 1540.0, 1560.0],
             {(1520.0, "CE"): w.ce2,
              (1500.0, "CE"): w.ce,
              (1560.0, "CE"): OptionContract("S5", "TESTA 1560 CE", "TESTA",
                                             1560.0, "CE", "2026-09-29", 25)})
    assert "SEC_CE_1520" not in u.scanner_ids()  # not in the scanner anymore
    # ... but the restored open position keeps it monitored:
    for p in w.positions.get_open():
        u.add_position_id(p["security_id"])
    assert "SEC_CE_1520" in u.monitored_ids()

    # and the exit still works for a contract that left the scanner
    out = w.feed(C(CE2, 15, 9, 45, close=102, vol=10, open_=98, high=102, low=98))
    sigs = [s for s in out if s is not None]
    assert len(sigs) == 1 and sigs[0].action == "EXIT_BUY"


def test_no_forgetting_across_multiple_restarts(tmp_path):
    w1 = World(tmp_path)
    w1.feed(
        C(CE, 15, 9, 15, close=100, vol=10),
        C(CE, 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C(CE, 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),
    )
    w2 = World(tmp_path)  # restart
    w2.now.t = candle_end_for(from_epoch(ts(15, 10, 0)))
    w2.feed(C(CE, 15, 10, 0, close=99, vol=10, open_=101, high=101, low=98.9))
    w3 = World(tmp_path)  # restart again
    assert len(w3.open_positions()) == 1  # position never forgotten
    st = w3.avwap.get(CE)
    assert st.last_candle_ts == ts(15, 10, 0)
    assert abs(st.last_close - 99.0) < 1e-9
