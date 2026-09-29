"""Signal engine: evaluates each completed 15-minute candle against the
AVWAP rules and emits ENTRY_SELL / EXIT_BUY signals.

Core loop (spec §43):
    FOR every completed candle of a monitored option:
        update that contract's AVWAP
        IF an open short exists for this contract:
            IF close > avwap:  EXIT_BUY
        ELSE:
            IF prev_close >= prev_avwap AND close < avwap:  ENTRY_SELL

Duplicate protection (spec §28): a signal is persisted with a unique key
(security_id, candle_ts, action). Re-delivered candles (reconnect, polling
retry, restart) can never produce a second signal for the same candle.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional
from datetime import time as dtime
from common.utils import from_epoch

from common.models import Candle, OptionContract, Signal, ENTRY_SELL, EXIT_BUY
from strategy.avwap import AvwapStore
from strategy import rules

log = logging.getLogger("avwap.strategy.engine")


class SignalEngine:
    def __init__(
        self,
        db,
        avwap: AvwapStore,
        positions,           # portfolio.positions.PositionManager
        journal,             # storage.journal.Journal
        on_signal: Optional[Callable[[Signal], None]] = None,
        now_fn=None,         # injectable clock (tests / sim); default = real IST
    ):
        from common.utils import now_ist
        self.db = db
        self.avwap = avwap
        self.positions = positions
        self.journal = journal
        self.on_signal = on_signal
        self.now_fn = now_fn or now_ist
        self.contracts: dict[str, OptionContract] = {}
        # transient per-security status for the dashboard (not persisted)
        self.last_signal_by_security: dict[str, Signal] = {}

    # ------------------------------------------------------------- universe
    def register_contract(self, contract: OptionContract) -> None:
        self.contracts[contract.security_id] = contract

    def register_contracts(self, contracts: list[OptionContract]) -> None:
        for c in contracts:
            self.register_contract(c)

    def contract_for(self, security_id: str) -> Optional[OptionContract]:
        return self.contracts.get(security_id)

    def forget_contract(self, security_id: str) -> None:
        # NOTE: forgetting a contract from the *scanner* is fine; open
        # positions keep their own monitoring path. We only drop the lookup
        # entry if there is no open position and no known contract.
        if self.db.find_position_by_security(security_id) is None:
            self.contracts.pop(security_id, None)

    # ------------------------------------------------------------ main path
    def on_candle(self, candle: Candle) -> Optional[Signal]:
        """Process one COMPLETED 15-min candle. Returns a Signal or None."""
        if not self._candle_is_complete(candle):
            log.warning(
                "Refusing incomplete candle %s ts=%s (no intracandle signals)",
                candle.security_id, candle.ts,
            )
            return None

        state = self.avwap.get(candle.security_id)
        prev_close = state.last_close
        prev_avwap = state.last_avwap

        # Fold candle into the contract's own AVWAP (idempotent).
        avwap_now = state.update(candle)
        self.avwap.save(state)

        # Persist the candle + resulting AVWAP (spec §32).
        if not self.db.candle_exists(candle.security_id, candle.ts):
            self.db.save_candle(candle.to_row(avwap=avwap_now))

        contract = self.contracts.get(candle.security_id)
        symbol = contract.name if contract else f"{candle.security_id}"

        open_pos = self.positions.find_by_security(candle.security_id)
        if open_pos:
            entry_price = open_pos.get("entry_price")
            underlying = contract.underlying if contract else open_pos.get("underlying")
            
            if rules.is_exit_close(candle.close, avwap_now, entry_price, underlying):
                sig = self._emit(
                    candle, EXIT_BUY, rules.EXIT_REASON,
                    prev_close, prev_avwap, avwap_now, contract,
                )
                if sig:
                    log.info(
                        "EXIT  %s close=%.2f > avwap=%.4f", symbol, candle.close, avwap_now
                    )
                return sig
            return None

        if rules.is_entry_cross(prev_close, prev_avwap, candle.close, avwap_now):
            sig = self._emit(
                candle, ENTRY_SELL, rules.ENTRY_REASON,
                prev_close, prev_avwap, avwap_now, contract,
            )
            if sig:
                log.info(
                    "ENTRY %s close=%.2f < avwap=%.4f (prev %.2f vs %.4f)",
                    symbol, candle.close, avwap_now,
                    prev_close if prev_close is not None else -1,
                    prev_avwap if prev_avwap is not None else -1,
                )
            return sig
        return None

    # -------------------------------------------------------------- helpers
    def _candle_is_complete(self, candle: Candle) -> bool:
        """A candle is complete only when the current wall clock is at or past
        its close time. Guards against feeds delivering partial candles."""
        from common.utils import candle_end_for, from_epoch
        return self.now_fn() >= candle_end_for(from_epoch(candle.ts))

    def _emit(
        self,
        candle: Candle,
        action: str,
        reason: str,
        prev_close,
        prev_avwap,
        avwap_now,
        contract: Optional[OptionContract],
    ) -> Optional[Signal]:
        sig = Signal(
            security_id=candle.security_id,
            action=action,
            candle_ts=candle.ts,
            signal_price=candle.close,
            avwap=avwap_now,
            prev_close=prev_close,
            prev_avwap=prev_avwap,
            reason=reason,
            symbol=contract.name if contract else f"{candle.security_id}",
            underlying=contract.underlying if contract else "",
            strike=contract.strike if contract else 0.0,
            option_type=contract.option_type if contract else "",
            expiry=contract.expiry if contract else "",
            created_at=int(self.now_fn().timestamp()),
        )
        if self.db.signal_exists(sig.signal_key):
            log.info(
                "Duplicate signal suppressed: %s (candle %s already signalled)",
                sig.signal_key, sig.candle_ts,
            )
            return None
        self.db.save_signal(sig.to_row())
        self.last_signal_by_security[candle.security_id] = sig
        self.journal.write(
            "SIGNAL",
            ts=sig.candle_ts,  # market timeline: the candle that produced it
            security_id=sig.security_id,
            symbol=sig.symbol,
            detail=sig.to_row(),
        )
        if self.on_signal is not None:
            try:
                self.on_signal(sig)
            except Exception:
                log.exception("on_signal callback failed for %s", sig.signal_key)
        return sig
