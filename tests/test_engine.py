"""End-to-end strategy engine behaviour (spec §43-§44, §48):
entry cross, hold, exit, re-entry after fresh cross, CE/PE independence,
duplicate-signal protection, one position per contract."""
from __future__ import annotations

import tempfile
from datetime import datetime

from common.utils import IST
from conftest import C, World

CE = "SEC_CE_ATM"
PE = "SEC_PE_ATM"
CE2 = "SEC_CE_1520"


def _ce_sequence(day=15):
    """Deterministic candle path for SEC_CE_ATM:
    c1 anchor -> c2 above -> c3 cross-below (ENTRY) -> c4 below (HOLD)
    -> c5 close above (EXIT) -> c6 still above (no entry)
    -> c7 fresh cross below (ENTRY again).

    AVWAP after each candle (vol=10 each):
      c1: av=100.0000   c2: av=102.0000   c3: av=102.0444
      c4: av=101.4417   c5: av=101.8867   c6: av=102.4333   c7: av=102.4429
    """
    return [
        C(CE, day, 9, 15, close=100, vol=10),
        C(CE, day, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C(CE, day, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),  # entry
        C(CE, day, 10, 0, close=99, vol=10, open_=101, high=101, low=98.9),     # hold
        C(CE, day, 10, 15, close=106, vol=10, open_=99, high=106, low=99),      # exit
        C(CE, day, 10, 30, close=105, vol=10, open_=106, high=106, low=104.5),  # above: no entry
        C(CE, day, 10, 45, close=101.5, vol=10, open_=105, high=105, low=101),  # re-entry
    ]


def test_full_entry_hold_exit_reentry_cycle(world: World):
    out = world.feed(*_ce_sequence())
    sigs = [s for s in out if s is not None]
    assert [s.action for s in sigs] == ["ENTRY_SELL", "EXIT_BUY", "ENTRY_SELL"]

    # entry details
    e = sigs[0]
    assert e.reason == "CROSS_BELOW_AVWAP"
    assert e.signal_price == 101
    assert abs(e.prev_close - 104) < 1e-9
    assert abs(e.prev_avwap - 102.0) < 1e-6
    assert abs(e.avwap - 102.0444) < 1e-3
    # exit details
    x = sigs[1]
    assert x.reason == "CLOSE_ABOVE_AVWAP"
    assert x.signal_price == 106
    # re-entry required a FRESH cross (10:30 candle produced nothing)
    assert len(sigs) == 3

    open_pos = world.open_positions()
    assert len(open_pos) == 1
    assert open_pos[0]["security_id"] == CE
    closed = world.closed_positions()
    assert len(closed) == 1
    assert closed[0]["security_id"] == CE
    # P&L = (101 - 106) * 25 = -125
    assert abs(closed[0]["pnl"] - (-125.0)) < 1e-9
    assert closed[0]["exit_reason"] == "CLOSE_ABOVE_AVWAP"
    assert closed[0]["entry_reason"] == "CROSS_BELOW_AVWAP"


def test_no_entry_when_already_below():
    """previous close < previous AVWAP AND current close < current AVWAP
    must NOT trigger an entry (spec §17)."""
    d = tempfile.mkdtemp()
    w = World(d, with_broker_execution=False)
    out = w.feed(
        C(CE2, 15, 9, 15, close=100, vol=10),                                  # anchor: 100 == av
        C(CE2, 15, 9, 30, close=98, vol=10, open_=100, high=100, low=97.9),    # 100>=100, 98<99.48 -> ENTRY
        C(CE2, 15, 9, 45, close=96, vol=10, open_=98, high=98, low=95.9),      # already below: no entry
        C(CE2, 15, 10, 0, close=95, vol=10, open_=96, high=96, low=94.9),      # still below: no entry
    )
    sigs = [s for s in out if s is not None]
    assert len(sigs) == 1
    assert sigs[0].action == "ENTRY_SELL"
    assert sigs[0].candle_ts == out[1].candle_ts


def test_exit_only_on_close_above(world: World):
    """While a short is open: candles closing BELOW the AVWAP must hold;
    the exit fires only on the first candle that CLOSES above it."""
    out = world.feed(
        C(CE, 15, 9, 15, close=100, vol=10),
        C(CE, 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C(CE, 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),  # entry (av 102.0444)
        C(CE, 15, 10, 0, close=99, vol=10, open_=101, high=101, low=98.9),      # below: HOLD
        C(CE, 15, 10, 15, close=99.9, vol=10, open_=99, high=100, low=99),      # 99.9 < 101.08: HOLD
    )
    assert len(world.open_positions()) == 1
    assert [s for s in out if s is not None and s.action == "EXIT_BUY"] == []
    # close just below the (new) avwap -> still no exit
    out2 = world.feed(C(CE, 15, 10, 30, close=100.5, vol=10,
                        open_=100, high=101, low=99.9))
    assert [s for s in out2 if s is not None] == []
    assert len(world.open_positions()) == 1
    # close above the avwap -> EXIT
    out3 = world.feed(C(CE, 15, 10, 45, close=102.5, vol=10,
                        open_=101, high=103, low=100.9))
    sigs3 = [s for s in out3 if s is not None]
    assert len(sigs3) == 1 and sigs3[0].action == "EXIT_BUY"


def test_ce_and_pe_positions_independent(world: World):
    """CALL and PUT positions are independent (spec §23)."""
    spec = [
        (9, 15, 100, None, None, None),
        (9, 30, 104, 103, 105, 103),
        (9, 45, 101, 104, 104.5, 100.9),
        (10, 0, 99, 101, 101, 98.9),
        (10, 15, 106, 99, 106, 99),
    ]
    batch = []
    for (h, m, c, o, hi, lo) in spec:
        batch.append(C(CE, 15, h, m, close=c, vol=10, open_=o, high=hi, low=lo))
        batch.append(C(PE, 15, h, m, close=c, vol=10, open_=o, high=hi, low=lo))
    out = world.feed(*batch)
    sigs = [s for s in out if s is not None]
    assert len(sigs) == 4
    by_sec: dict = {}
    for s in sigs:
        by_sec.setdefault(s.security_id, []).append(s.action)
    assert by_sec[CE] == ["ENTRY_SELL", "EXIT_BUY"]
    assert by_sec[PE] == ["ENTRY_SELL", "EXIT_BUY"]
    closed = world.closed_positions()
    assert {p["security_id"] for p in closed} == {CE, PE}
    assert len(closed) == 2  # exactly one trade per contract


def test_one_position_per_contract(world: World):
    """Only one open SHORT per contract (spec §44); a cross-below while a
    short is open routes to the exit branch, never to a second entry."""
    out = world.feed(
        C(CE, 15, 9, 15, close=100, vol=10),
        C(CE, 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C(CE, 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),
    )
    assert len(world.open_positions()) == 1
    # duplicate candle delivery (reconnect) -> no second signal / position
    c3 = C(CE, 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9)
    assert world.engine.on_candle(c3) is None
    assert len(world.open_positions()) == 1
    # still-below candle while short is open -> hold, no new entry
    world.feed(C(CE, 15, 10, 0, close=102, vol=10, open_=104, high=104, low=101.5))
    assert len(world.open_positions()) == 1


def test_duplicate_signal_key_suppressed():
    """Persistent signal-key dedupe: if the same (contract, candle, action)
    signal was already recorded (e.g. order sent, crash, restart, refetch),
    re-evaluating the candle must NOT emit it again (spec §28)."""
    d = tempfile.mkdtemp()
    w = World(d, with_broker_execution=False)
    # pre-record the signal for the 09:45 candle as if a previous run had
    # already emitted and executed it
    from common.models import Signal
    from conftest import ts
    prev = Signal(
        security_id=CE, action="ENTRY_SELL", candle_ts=ts(15, 9, 45),
        signal_price=101, avwap=102.0444, prev_close=104, prev_avwap=102.0,
        reason="CROSS_BELOW_AVWAP", symbol="TESTA 1500 CE", underlying="TESTA",
        strike=1500.0, option_type="CE", expiry="2026-09-29",
        created_at=ts(15, 10, 0),
    )
    w.db.save_signal(prev.to_row())
    out = w.feed(
        C(CE, 15, 9, 15, close=100, vol=10),
        C(CE, 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C(CE, 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),
    )
    # entry rule fires at c3 but the persistent signal key already exists
    # -> suppressed, and NO position is created
    assert [s for s in out if s is not None] == []
    assert len(w.open_positions()) == 0


def test_duplicate_candle_after_restart_suppressed(tmp_path):
    """Full restart recovery: new engine + new position manager on the same
    DB; a re-delivered boundary candle must not double-signal or double-trad."""
    w1 = World(tmp_path)
    w1.feed(
        C(CE, 15, 9, 15, close=100, vol=10),
        C(CE, 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
    )
    # "restart": brand new engine on the same database
    w2 = World(tmp_path)
    w2.now.t = datetime(2026, 9, 15, 10, 0, 1, tzinfo=IST)
    sig = w2.engine.on_candle(C(CE, 15, 9, 45, close=101, vol=10,
                                open_=104, high=104.5, low=100.9))
    assert sig is not None  # valid entry (state restored from DB)
    # replay the same candle again (refetch after restart)
    sig2 = w2.engine.on_candle(C(CE, 15, 9, 45, close=101, vol=10,
                                 open_=104, high=104.5, low=100.9))
    assert sig2 is None
    # one position only
    assert len(w2.open_positions()) == 1
