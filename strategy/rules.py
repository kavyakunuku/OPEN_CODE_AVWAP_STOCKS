"""The ONLY strategy rules. Nothing else may be added without a strategy
revision (spec §42).

ENTRY (short) - a true cross below AVWAP on completed 15-min candles:
    previous_close >= previous_avwap  AND  current_close < current_avwap

EXIT (buy-to-close the short):
    current_close > current_avwap

No fixed stop-loss, no other indicator, no intracandle evaluation.
"""
from __future__ import annotations

from typing import Optional

ENTRY_REASON = "CROSS_BELOW_AVWAP"
EXIT_REASON = "CLOSE_ABOVE_AVWAP"


def is_entry_cross(
    prev_close: Optional[float],
    prev_avwap: Optional[float],
    cur_close: Optional[float],
    cur_avwap: Optional[float],
) -> bool:
    """True only for a genuine ABOVE/AT -> BELOW transition."""
    if prev_close is None or prev_avwap is None or cur_close is None or cur_avwap is None:
        return False
    return (prev_close >= prev_avwap) and (cur_close < cur_avwap)


def is_exit_close(
    cur_close: Optional[float],
    cur_avwap: Optional[float],
    entry_price: Optional[float] = None,
    underlying: Optional[str] = None
) -> bool:
    """True when a completed candle closes above effective SL (AVWAP)."""
    if cur_close is None or cur_avwap is None:
        return False
    
    return cur_close > cur_avwap
