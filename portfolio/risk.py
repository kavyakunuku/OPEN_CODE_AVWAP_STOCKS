"""Risk controls (spec §35) - SEPARATE from the strategy signal.

The strategy engine only sees AVWAP crosses. These gates decide whether a
signal may become an order, and provide the emergency controls:

    * quantity per trade          (default: the contract's lot size)
    * max open positions
    * max trades per day
    * max daily loss (realized today + unrealized at fresh LTP; fail closed when unpriced)
    * disable new entries (manual switch)
    * emergency stop              (blocks ALL new entries; exits still work)
    * exit all positions          (operational action, not a strategy signal)

Flags persist in the kv table so they survive restarts.
"""
from __future__ import annotations

import logging
import math
import time

log = logging.getLogger("avwap.portfolio.risk")

KEY_EMERGENCY = "risk.emergency_stop"
KEY_DISABLE_ENTRIES = "risk.disable_new_entries"


class RiskManager:
    def __init__(self, db, cfg: dict):
        self.db = db
        self.cfg = cfg.get("risk", {})

    # --------------------------------------------------------------- flags
    def flags(self) -> dict:
        return {
            "emergency_stop": bool(self.db.kv_get(KEY_EMERGENCY, False)),
            "disable_new_entries": bool(self.db.kv_get(KEY_DISABLE_ENTRIES, False)),
        }

    def set_emergency_stop(self, on: bool) -> None:
        self.db.kv_set(KEY_EMERGENCY, bool(on))
        log.warning("EMERGENCY STOP %s", "ENGAGED" if on else "released")

    def set_disable_entries(self, on: bool) -> None:
        self.db.kv_set(KEY_DISABLE_ENTRIES, bool(on))
        log.warning("New entries %s", "DISABLED" if on else "enabled")

    # ------------------------------------------------------------ quantity
    @staticmethod
    def _positive_integer(value) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or number <= 0 or not number.is_integer():
            return None
        return int(number)

    def quantity_for(self, contract, cfg_quantity: int | None = None) -> int:
        """Return a valid contract-lot multiple, or zero for bad sizing config."""
        lot = self._positive_integer(getattr(contract, "lot_size", None))
        if lot is None:
            lot = self._positive_integer(self.cfg.get("default_lot_size", 250)) or 250

        q = cfg_quantity
        if q is None:
            q = self.cfg.get("quantity_per_trade")
        if q is None or q == "":
            return lot

        configured = self._positive_integer(q)
        if configured is not None:
            return configured if configured % lot == 0 else 0

        # Preserve the existing default-lot behavior for non-positive values;
        # positive fractional/malformed values fail closed instead of rounding
        # into a quantity that differs from the operator's explicit setting.
        try:
            number = float(q)
        except (TypeError, ValueError, OverflowError):
            return 0
        if not math.isfinite(number) or number > 0:
            return 0
        return lot

    # --------------------------------------------------------------- gates
    def entry_block_reasons(
        self,
        open_positions: int,
        trades_today: int,
        daily_pnl: float | None,
    ) -> list[str]:
        reasons = []
        f = self.flags()
        if f["emergency_stop"]:
            reasons.append("EMERGENCY_STOP_ENGAGED")
        if f["disable_new_entries"]:
            reasons.append("NEW_ENTRIES_DISABLED")
        max_pos = int(self.cfg.get("max_open_positions", 0) or 0)
        if max_pos and open_positions >= max_pos:
            reasons.append(f"MAX_OPEN_POSITIONS({max_pos})")
        max_trades = int(self.cfg.get("max_trades_per_day", 0) or 0)
        if max_trades and trades_today >= max_trades:
            reasons.append(f"MAX_TRADES_PER_DAY({max_trades})")
        max_loss = float(self.cfg.get("max_daily_loss", 0) or 0)
        if max_loss:
            if daily_pnl is None:
                reasons.append("UNPRICED_OPEN_POSITION_DRAWDOWN_GATE")
            elif daily_pnl <= -abs(max_loss):
                reasons.append(f"MAX_DAILY_LOSS({max_loss:g})")
        return reasons

    def daily_pnl(self, ist_day: str, open_positions: list[dict], ltp_getter) -> float | None:
        """Return realized + fully marked unrealized P&L, or None if incomplete."""
        realized = self.db.realized_pnl_today(ist_day)
        try:
            realized = float(realized)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(realized):
            return None
        unreal = 0.0
        for position in open_positions:
            entry = position.get("entry_price")
            ltp = ltp_getter(position["security_id"])
            quantity = position.get("quantity")
            if entry is None or ltp is None or quantity is None:
                return None
            try:
                entry, ltp = float(entry), float(ltp)
                quantity_number = float(quantity)
            except (TypeError, ValueError, OverflowError):
                return None
            if (not math.isfinite(entry) or not math.isfinite(ltp)
                    or not math.isfinite(quantity_number) or quantity_number <= 0
                    or not quantity_number.is_integer()):
                return None
            unreal += (entry - ltp) * int(quantity_number)
        return realized + unreal
