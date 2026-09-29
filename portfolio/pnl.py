"""P&L math for short option positions (spec §45, §46).

PAPER: (entry - exit) * quantity  with signal/candle or quote prices.
LIVE:  same formula with Dhan FILL prices (the broker applies these).
Transaction costs are intentionally NOT part of the signal logic; add them
as a separate configuration in a future revision if desired.
"""
from __future__ import annotations

from typing import Optional


def short_pnl(entry_price: Optional[float], exit_price: Optional[float], quantity: int) -> Optional[float]:
    if entry_price is None or exit_price is None:
        return None
    return (entry_price - exit_price) * quantity


def unrealized_pnl(entry_price: Optional[float], ltp: Optional[float], quantity: int) -> Optional[float]:
    if entry_price is None or ltp is None:
        return None
    return (entry_price - ltp) * quantity
