"""Shared test fixtures: a deterministic in-memory trading world."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from common.models import Candle, OptionContract  # noqa: E402
from common.utils import IST  # noqa: E402
from execution.paper import PaperBroker  # noqa: E402
from market.candles import CandleEngine  # noqa: E402
from market.universe import UniverseManager  # noqa: E402
from portfolio.positions import PositionManager  # noqa: E402
from storage.database import Database  # noqa: E402
from storage.journal import Journal  # noqa: E402
from strategy.avwap import AvwapStore  # noqa: E402
from strategy.engine import SignalEngine  # noqa: E402


def ts(day=15, h=9, m=15) -> int:
    return int(datetime(2026, 9, day, h, m, tzinfo=IST).timestamp())


def C(sec, day, h, m, close, vol, open_=None, high=None, low=None) -> Candle:
    o = close if open_ is None else open_
    hi = high if high is not None else max(o, close)
    lo = low if low is not None else min(o, close)
    return Candle(security_id=sec, ts=ts(day, h, m), open=o, high=hi, low=lo,
                  close=close, volume=int(vol))


class FakeNow:
    """Deterministic clock that the test advances by hand."""

    def __init__(self, start=None):
        self.t = start or datetime(2026, 9, 15, 9, 30, 1, tzinfo=IST)

    def __call__(self):
        return self.t

    def advance(self, minutes: float) -> None:
        self.t = self.t + timedelta(minutes=minutes)


class World:
    """A complete minimal trading world: db + strategy + paper broker."""

    def __init__(self, tmp_path, with_broker_execution=True):
        import os as _os
        self.db = Database(_os.path.join(str(tmp_path), "world.db"))
        self.journal = Journal(self.db)
        self.positions = PositionManager(self.db)
        self.avwap = AvwapStore(self.db)
        self.now = FakeNow()
        self.signals = []

        def _exec(sig):
            self.signals.append(sig)
            if with_broker_execution:
                if sig.action == "ENTRY_SELL":
                    c = self.engine.contract_for(sig.security_id)
                    self.paper.execute_entry(c, 25, sig)
                else:
                    p = self.positions.find_by_security(sig.security_id)
                    self.paper.execute_exit(p, sig)

        self.engine = SignalEngine(
            self.db, self.avwap, self.positions, self.journal,
            on_signal=_exec, now_fn=self.now,
        )
        self.paper = PaperBroker(self.db, self.positions, self.journal,
                                 fill_mode="candle_close")
        self.ce = OptionContract("SEC_CE_ATM", "TESTA 1500 CE", "TESTA", 1500.0,
                                 "CE", "2026-09-29", 25)
        self.pe = OptionContract("SEC_PE_ATM", "TESTA 1500 PE", "TESTA", 1500.0,
                                 "PE", "2026-09-29", 25)
        self.ce2 = OptionContract("SEC_CE_1520", "TESTA 1520 CE", "TESTA", 1520.0,
                                  "CE", "2026-09-29", 25)
        self.engine.register_contracts([self.ce, self.pe, self.ce2])
        self.universe = UniverseManager(6)
        self.candles = CandleEngine(15)

    def feed(self, *candles):
        """Feed completed candles (advancing the fake clock past each close)."""
        out = []
        for c in candles:
            from common.utils import candle_end_for, from_epoch
            self.now.t = candle_end_for(from_epoch(c.ts)) + timedelta(seconds=5)
            accepted = self.candles.ingest_closed(c)
            assert accepted is not None, f"candle not accepted: {c}"
            out.append(self.engine.on_candle(accepted))
        return out

    def open_positions(self):
        return self.positions.get_open()

    def closed_positions(self):
        return [dict(r) for r in self.db.get_positions(status="CLOSED")]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)
