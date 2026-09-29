"""Risk controls (spec §35) - SEPARATE from the strategy signal.

The strategy engine only sees AVWAP crosses. These gates decide whether a
signal may become an order, and provide the emergency controls:

    * quantity per trade          (default: the contract's lot size)
    * max open positions
    * max trades per day
    * max daily loss (realized today + unrealized at LTP)
    * disable new entries (manual switch)
    * emergency stop              (blocks ALL new entries; exits still work)
    * exit all positions          (operational action, not a strategy signal)

Flags persist in the kv table so they survive restarts.
"""
from __future__ import annotations

import logging
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
    def quantity_for(self, contract, cfg_quantity: int | None = None) -> int:
        q = cfg_quantity
        if q is None:
            q = self.cfg.get("quantity_per_trade")
        if q and int(q) > 0:
            return int(q)
        lot = getattr(contract, "lot_size", 0) or int(self.cfg.get("default_lot_size", 250))
        return int(lot)

    # --------------------------------------------------------------- gates
    def entry_block_reasons(
        self,
        open_positions: int,
        trades_today: int,
        daily_pnl: float,
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
        if max_loss and daily_pnl <= -abs(max_loss):
            reasons.append(f"MAX_DAILY_LOSS({max_loss:g})")
        return reasons

    def daily_pnl(self, ist_day: str, open_positions: list[dict], ltp_getter) -> float:
        """Realized today + unrealized at current LTP (conservative gate)."""
        realized = self.db.realized_pnl_today(ist_day)
        unreal = 0.0
        for p in open_positions:
            ltp = ltp_getter(p["security_id"])
            if ltp and p.get("entry_price") is not None:
                unreal += (p["entry_price"] - ltp) * p["quantity"]
        return realized + unreal
