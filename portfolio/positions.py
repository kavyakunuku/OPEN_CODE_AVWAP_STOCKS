"""Position persistence and lifecycle.

One position row per trade (spec §22, §29). Statuses:
    PENDING   - live order sent, fill not yet reported
    OPEN      - short established (paper: instantly; live: after fill)
    CLOSED    - bought to close, P&L computed
    REJECTED  - live order rejected (no position ever existed)

Rule (spec §44): at most one open short per individual option contract.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from common.models import (
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_PENDING,
    STATUS_REJECTED,
    OptionContract,
)

log = logging.getLogger("avwap.portfolio.positions")


class PositionManager:
    def __init__(self, db):
        self.db = db

    # -------------------------------------------------------------- create
    def open_position(
        self,
        contract: Optional[OptionContract],
        quantity: int,
        entry_price: Optional[float],
        ts: int,
        reason: str,
        avwap: Optional[float],
        mode: str,
        order_id: Optional[str] = None,
        status: str = STATUS_OPEN,
        security_id: Optional[str] = None,
        source: str = "system",
    ) -> dict:
        pid = uuid.uuid4().hex
        p = {
            "position_id": pid,
            "security_id": security_id or (contract.security_id if contract else ""),
            "symbol": contract.symbol if contract else (security_id or "UNKNOWN"),
            "underlying": contract.underlying if contract else "",
            "strike": contract.strike if contract else None,
            "option_type": contract.option_type if contract else "",
            "expiry": contract.expiry if contract else "",
            "quantity": int(quantity),
            "entry_price": entry_price,
            "entry_time": ts,
            "entry_avwap": avwap,
            "entry_reason": reason,
            "entry_order_id": order_id,
            "exit_price": None,
            "exit_time": None,
            "exit_avwap": None,
            "exit_reason": None,
            "exit_order_id": None,
            "pnl": None,
            "status": status,
            "mode": mode,
            "source": source,
        }
        self.db.save_position(p)
        return p

    def update(self, position_id: str, **fields) -> None:
        self.db.update_position(position_id, **fields)

    # --------------------------------------------------------------- close
    def close_position(
        self,
        position_id: str,
        exit_price: Optional[float],
        ts: int,
        reason: str,
        avwap: Optional[float],
        order_id: Optional[str] = None,
        pnl_override: Optional[float] = None,
    ) -> dict:
        pos = self.db.get_position(position_id)
        if pos is None:
            raise KeyError(f"position {position_id} not found")
        entry = pos["entry_price"]
        qty = pos["quantity"]
        pnl = pnl_override
        if pnl is None and entry is not None and exit_price is not None:
            # short P&L = (entry - exit) * qty
            pnl = (entry - exit_price) * qty
        self.db.update_position(
            position_id,
            exit_price=exit_price,
            exit_time=ts,
            exit_avwap=avwap,
            exit_reason=reason,
            exit_order_id=order_id or pos["exit_order_id"],
            pnl=pnl,
            status=STATUS_CLOSED,
        )
        updated = self.db.get_position(position_id)
        return dict(updated)

    # ------------------------------------------------------------- queries
    def has_open(self, security_id: str) -> bool:
        """True if an OPEN (or PENDING live) position exists for the contract.
        Used to route candles to the exit branch instead of the entry branch."""
        return self.db.find_position_by_security(
            security_id, statuses=("OPEN", "PENDING")) is not None

    def find_by_security(self, security_id: str) -> Optional[dict]:
        row = self.db.find_position_by_security(
            security_id, statuses=("OPEN", "PENDING"))
        return dict(row) if row else None

    def get_open(self) -> list[dict]:
        out = []
        for status in ("OPEN", "PENDING"):
            for r in self.db.get_positions(status=status, limit=500):
                out.append(dict(r))
        return out

    def all_positions(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.db.get_positions(limit=limit)]
