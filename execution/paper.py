"""Paper broker: virtual fills on real market data. NO live orders.

Fill conventions (explicit, per spec §18) - configured via paper.fill_mode:
  * "candle_close" (default): fill at the close of the completed 15-min
    candle that generated the signal. Deterministic; matches the signal
    price. Optional slippage_bps applied against the trader.
  * "next_quote": fill at the first LTP quote observed after the candle
    close (within next_quote_timeout_seconds); falls back to the candle
    close if no quote arrives in time.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from common.models import ENTRY_SELL, EXIT_BUY, FillResult, OptionContract, Signal
from execution.broker import Broker

log = logging.getLogger("avwap.execution.paper")


class PaperBroker(Broker):
    mode = "PAPER"

    def __init__(self, db, positions, journal, fill_mode: str = "candle_close",
                 slippage_bps: float = 0.0):
        self.db = db
        self.positions = positions
        self.journal = journal
        self.fill_mode = fill_mode
        self.slippage_bps = float(slippage_bps)

    def _apply_slippage(self, price: float, is_sell: bool) -> float:
        if self.slippage_bps <= 0:
            return price
        if is_sell:
            return price * (1 - self.slippage_bps / 10_000)
        return price * (1 + self.slippage_bps / 10_000)

    def execute_entry(self, contract: OptionContract, quantity: int,
                      signal: Signal, fill_price: Optional[float] = None) -> FillResult:
        if self.positions.has_open(contract.security_id):
            log.info("Paper entry skipped (position already exists): %s", contract.name)
            return FillResult(order_id="", status="REJECTED", raw={"reason": "position_exists"})

        ref = fill_price if fill_price is not None else signal.signal_price
        price = self._apply_slippage(ref, is_sell=True)
        # Position timestamps are the candle's close time (candle_ts), NOT the
        # wall-clock moment the signal was processed (created_at). During
        # startup catch-up many historical candles are processed within the
        # same second - using created_at would stamp unrelated trades with
        # identical/misleading times. candle_ts is the actual market time of
        # the 15-min close that generated the trade (deterministic across
        # restarts; matches the candle_close fill).
        pos = self.positions.open_position(
            contract=contract,
            quantity=quantity,
            entry_price=price,
            ts=signal.candle_ts,
            reason=signal.reason,
            avwap=signal.avwap,
            mode=self.mode,
            order_id=f"PAPER-{uuid.uuid4().hex[:12]}",
            status="OPEN",
        )
        self.db.save_order({
            "order_id": pos["entry_order_id"],
            "position_id": pos["position_id"],
            "security_id": contract.security_id,
            "action": ENTRY_SELL,
            "side": "SELL",
            "quantity": quantity,
            "order_type": "PAPER",
            "status": "COMPLETE",
            "filled_qty": quantity,
            "avg_price": price,
            "placed_at": signal.candle_ts,
            "updated_at": signal.candle_ts,
            "raw": {"fill_mode": self.fill_mode, "signal_price": signal.signal_price},
        })
        self.journal.write(
            "PAPER_ENTRY",
            ts=signal.candle_ts,
            position_id=pos["position_id"],
            security_id=contract.security_id,
            symbol=contract.name,
            detail={
                "fill_price": price,
                "signal_price": signal.signal_price,
                "fill_mode": self.fill_mode,
                "avwap": signal.avwap,
                "quantity": quantity,
                "reason": signal.reason,
            },
        )
        log.info("PAPER SELL %s qty=%d @ %.2f (signal %.2f, avwap %s)",
                 contract.name, quantity, price, signal.signal_price,
                 f"{signal.avwap:.2f}" if signal.avwap else "n/a")
        return FillResult(order_id=pos["entry_order_id"], status="COMPLETE",
                          filled_qty=quantity, avg_price=price)

    def execute_exit(self, position: dict, signal: Optional[Signal],
                     fill_price: Optional[float] = None,
                     reason: str = "CLOSE_ABOVE_AVWAP") -> FillResult:
        if position["status"] not in ("OPEN", "PENDING"):
            log.info("Paper exit skipped (position %s not open)", position["position_id"])
            return FillResult(order_id="", status="REJECTED",
                              raw={"reason": "not_open"})
        ref = fill_price
        if ref is None and signal is not None:
            ref = signal.signal_price
        if ref is None:
            return FillResult(order_id="", status="REJECTED", raw={"reason": "no_fill_price"})
        price = self._apply_slippage(ref, is_sell=False)
        # Signal-driven exit: the close time of the exit candle. Forced exit
        # (no signal - exit-all / emergency): the real wall-clock execution
        # time (never the entry time, which previously made exit == entry).
        ts = signal.candle_ts if signal is not None else int(time.time())
        avwap = signal.avwap if signal is not None else None
        closed = self.positions.close_position(
            position_id=position["position_id"],
            exit_price=price,
            ts=ts,
            reason=reason,
            avwap=avwap,
            order_id=f"PAPER-{uuid.uuid4().hex[:12]}",
        )
        self.journal.write(
            "PAPER_EXIT",
            ts=ts or 0,
            position_id=position["position_id"],
            security_id=position["security_id"],
            symbol=position["symbol"],
            detail={
                "fill_price": price,
                "signal_price": signal.signal_price if signal else None,
                "fill_mode": self.fill_mode,
                "avwap": avwap,
                "pnl": closed["pnl"],
                "reason": reason,
            },
        )
        log.info("PAPER BUY-TO-CLOSE %s @ %.2f -> P&L %.2f (%s)",
                 position["symbol"], price, closed["pnl"], reason)
        return FillResult(order_id=closed["exit_order_id"], status="COMPLETE",
                          filled_qty=position["quantity"], avg_price=price)
