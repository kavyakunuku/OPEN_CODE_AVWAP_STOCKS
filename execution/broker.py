"""Broker interface.

The strategy engine is mode-agnostic: it emits signals; the BROKER decides
whether they become virtual paper fills or real Dhan orders (spec §1, §26).

Only two transactions ever exist:
    SELL            (open a short)
    BUY to close    (close an existing short)
There is NO code path that buys an option for a long position.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from common.models import FillResult, OptionContract, Signal


class Broker(ABC):
    mode: str = "ABSTRACT"

    @abstractmethod
    def execute_entry(self, contract: OptionContract, quantity: int,
                      signal: Signal, fill_price: float | None = None) -> FillResult:
        """Open a short (SELL). Paper: instant virtual fill. Live: place
        order, position stays PENDING until Dhan reports the fill."""

    @abstractmethod
    def execute_exit(self, position: dict, signal: Signal | None,
                     fill_price: float | None = None,
                     reason: str = "CLOSE_ABOVE_AVWAP") -> FillResult:
        """Buy-to-close an existing short (BUY)."""

    def poll_fills(self) -> None:
        """Live: reconcile order statuses / fill reports. Paper: no-op."""
        return None

    def reconcile_positions(self) -> None:
        """Live: reconcile DB positions with Dhan positions. Paper: no-op."""
        return None

    def exit_all(self, ltp_getter, reason: str) -> int:
        """Emergency/manual: close every OPEN position at market.
        This is an OPERATIONAL control, not a strategy signal (spec §24)."""
        raise NotImplementedError
