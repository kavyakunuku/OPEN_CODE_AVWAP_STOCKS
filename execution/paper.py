"""Paper execution adapter: virtual fills, with no broker/order API dependency.

Phase 8 defaults to marketable top-of-book execution: option sells hit the
captured bid and buy-to-close exits lift the captured ask. Configured adverse
slippage is then applied and rounded against the paper trader to the contract
tick. If a fresh two-sided quote is unavailable, the explicit default fallback
is the AVWAP signal candle close plus adverse slippage; the report labels this
fallback and never pretends it was a quote or a midpoint.

The old deterministic ``candle_close`` and ``next_quote`` modes remain
available for tests/diagnostics. Every fill and its assumptions are persisted
in SQLite. This module has no Dhan client, order-placement import, or network
access.
"""
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from common.models import ENTRY_SELL, EXIT_BUY, FillResult, OptionContract, Signal
from common.utils import IST, from_epoch
from execution.broker import Broker

log = logging.getLogger("avwap.execution.paper")


class PaperBroker(Broker):
    """Persisted paper-only execution adapter."""

    mode = "PAPER"
    FILL_MODES = {"marketable_quote", "candle_close", "next_quote"}

    def __init__(
        self,
        db,
        positions,
        journal,
        fill_mode: str = "candle_close",
        slippage_bps: float = 0.0,
        *,
        max_quote_age_seconds: float = 60.0,
        quote_fallback: str = "signal_candle_close",
        transaction_cost_per_order: float = 0.0,
        transaction_cost_bps: float = 0.0,
        margin_estimate_pct: float = 20.0,
    ):
        if fill_mode not in self.FILL_MODES:
            raise ValueError(f"paper.fill_mode must be one of {sorted(self.FILL_MODES)}")
        if quote_fallback != "signal_candle_close":
            raise ValueError("paper.quote_fallback must be signal_candle_close")
        numeric = {
            "slippage_bps": slippage_bps,
            "max_quote_age_seconds": max_quote_age_seconds,
            "transaction_cost_per_order": transaction_cost_per_order,
            "transaction_cost_bps": transaction_cost_bps,
            "margin_estimate_pct": margin_estimate_pct,
        }
        parsed = {}
        for key, value in numeric.items():
            try:
                parsed[key] = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{key} must be a finite non-negative number") from exc
            if not math.isfinite(parsed[key]) or parsed[key] < 0:
                raise ValueError(f"{key} must be a finite non-negative number")
        if parsed["max_quote_age_seconds"] <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if parsed["margin_estimate_pct"] > 100:
            raise ValueError("margin_estimate_pct cannot exceed 100")

        self.db = db
        self.positions = positions
        self.journal = journal
        self.fill_mode = fill_mode
        self.slippage_bps = parsed["slippage_bps"]
        self.max_quote_age_seconds = parsed["max_quote_age_seconds"]
        self.quote_fallback = quote_fallback
        self.transaction_cost_per_order = parsed["transaction_cost_per_order"]
        self.transaction_cost_bps = parsed["transaction_cost_bps"]
        self.margin_estimate_pct = parsed["margin_estimate_pct"]

    def _apply_slippage(self, price: float, is_sell: bool) -> float:
        """Apply configured adverse slippage before adverse tick rounding."""
        rate = self.slippage_bps / 10_000.0
        return float(price) * (1.0 - rate if is_sell else 1.0 + rate)

    @staticmethod
    def _number(value) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _quote_field(quote, *names):
        if quote is None:
            return None
        if isinstance(quote, dict):
            for name in names:
                if name in quote:
                    return quote.get(name)
            return None
        for name in names:
            value = getattr(quote, name, None)
            if value is not None:
                return value
        return None

    @staticmethod
    def _as_epoch(value) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=IST)
            return int(dt.timestamp())
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or number <= 0:
            return None
        if number > 1e12:
            number /= 1000.0
        return int(number)

    @staticmethod
    def _execution_epoch(execution_time, signal: Optional[Signal]) -> int:
        if isinstance(execution_time, datetime):
            dt = execution_time if execution_time.tzinfo else execution_time.replace(tzinfo=IST)
            return int(dt.timestamp())
        if execution_time is not None:
            try:
                value = int(execution_time)
                if value > 0:
                    return value
            except (TypeError, ValueError, OverflowError):
                pass
        if signal is not None:
            # Candle timestamps identify candle starts; the close is the first
            # time its signal can be observed. App callers pass wall-clock T.
            return int(signal.candle_ts) + 15 * 60
        return int(time.time())

    @staticmethod
    def _adverse_tick(price: float, contract: Optional[OptionContract], *, is_sell: bool) -> float:
        tick = PaperBroker._number(getattr(contract, "tick_size", None)) if contract else None
        if tick is None or tick <= 0:
            return round(float(price), 8)
        units = float(price) / tick
        if is_sell:
            rounded_units = math.floor(units + 1e-10)
        else:
            rounded_units = math.ceil(units - 1e-10)
        return round(rounded_units * tick, 8)

    def _resolve_fill(
        self,
        *,
        contract: Optional[OptionContract],
        signal: Optional[Signal],
        is_sell: bool,
        quote,
        execution_time,
        explicit_fill_price: Optional[float],
        explicit_fill_method: Optional[str] = None,
    ) -> dict:
        fill_ts = self._execution_epoch(execution_time, signal)
        signal_price = self._number(signal.signal_price if signal else None)
        explicit_price = self._number(explicit_fill_price)
        bid = self._number(self._quote_field(quote, "bid"))
        ask = self._number(self._quote_field(quote, "ask"))
        quote_ts = self._as_epoch(self._quote_field(quote, "ts", "observed_at"))
        quote_age = float(fill_ts - quote_ts) if quote_ts is not None else None
        valid_book = (
            bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
        )
        quote_is_fresh = (
            quote_ts is not None and quote_age is not None
            and 0 <= quote_age <= self.max_quote_age_seconds
        )
        spread = None
        if valid_book:
            midpoint = (bid + ask) / 2.0
            spread = ((ask - bid) / midpoint * 100.0) if midpoint > 0 else None

        touch_price = ask if not is_sell else bid
        if (valid_book and quote_is_fresh
                and (self.fill_mode != "candle_close" or signal is None)):
            reference = touch_price
            method = "TOP_OF_BOOK_ASK" if not is_sell else "TOP_OF_BOOK_BID"
        elif explicit_price is not None and explicit_price > 0:
            reference = explicit_price
            method = explicit_fill_method or "EXPLICIT_PRICE_NO_FRESH_TWO_SIDED_BOOK"
        elif self.fill_mode == "candle_close" and signal_price is not None and signal_price > 0:
            reference = signal_price
            method = "SIGNAL_CANDLE_CLOSE_CONFIGURED"
        elif signal_price is not None and signal_price > 0:
            reference = signal_price
            method = "SIGNAL_CANDLE_CLOSE_FALLBACK_NO_FRESH_BOOK"
        else:
            return {"error": "NO_SAFE_REFERENCE_PRICE"}

        adjusted = self._apply_slippage(reference, is_sell=is_sell)
        fill = self._adverse_tick(adjusted, contract, is_sell=is_sell)
        if not math.isfinite(fill) or fill <= 0:
            return {"error": "INVALID_SIMULATED_FILL_PRICE"}
        slippage_per_unit = max(0.0, reference - fill) if is_sell else max(0.0, fill - reference)
        return {
            "timestamp": fill_ts,
            "signal_timestamp": (int(signal.candle_ts) + 15 * 60) if signal else None,
            "signal_price": signal_price,
            "fill_price": fill,
            "reference_price": reference,
            "bid": bid,
            "ask": ask,
            "spread_percent": spread,
            "slippage_bps": self.slippage_bps,
            "slippage_per_unit": slippage_per_unit,
            "quote_timestamp": quote_ts,
            "quote_age_seconds": quote_age,
            "execution_method": method,
            "quote_is_fresh": bool(quote_is_fresh),
            "book_was_valid": bool(valid_book),
        }

    def _transaction_cost(self, fill_price: float, quantity: int) -> float:
        turnover = abs(float(fill_price) * int(quantity))
        return self.transaction_cost_per_order + turnover * self.transaction_cost_bps / 10_000.0

    def _margin_estimate(self, underlying_spot, quantity: int):
        spot = self._number(underlying_spot)
        if spot is None or spot <= 0:
            return None
        return spot * int(quantity) * self.margin_estimate_pct / 100.0

    def _persist_fill(
        self,
        *,
        order_id: str,
        position_id: str,
        contract: Optional[OptionContract],
        event_type: str,
        quantity: int,
        avwap,
        fill: dict,
        transaction_cost: float,
        underlying_spot=None,
        margin_estimate=None,
        extra: Optional[dict] = None,
    ) -> dict:
        security_id = str(contract.security_id if contract else "")
        lot_size = int(getattr(contract, "lot_size", 0) or 0) if contract else 0
        margin_model = (
            f"{self.margin_estimate_pct:g}% of underlying notional; indicative only, not Dhan/SPAN margin"
            if margin_estimate is not None else "unavailable_without_underlying_spot"
        )
        raw = {
            "fill_mode": self.fill_mode,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "slippage_bps_per_side": self.slippage_bps,
            "margin_estimate_pct_of_underlying_notional": self.margin_estimate_pct,
            "quote_fallback": self.quote_fallback,
            "reference_price": fill.get("reference_price"),
            "book_was_valid": fill.get("book_was_valid"),
            "quote_is_fresh": fill.get("quote_is_fresh"),
            "transaction_cost_model": {
                "per_order": self.transaction_cost_per_order,
                "bps_of_premium_turnover": self.transaction_cost_bps,
            },
            "underlying_spot": self._number(underlying_spot),
            "margin_model": margin_model,
            **(extra or {}),
        }
        event = {
            "order_id": order_id,
            "position_id": position_id,
            "security_id": security_id,
            "event_type": event_type,
            "timestamp": int(fill["timestamp"]),
            "signal_timestamp": fill.get("signal_timestamp"),
            "signal_price": fill.get("signal_price"),
            "fill_price": fill.get("fill_price"),
            "bid": fill.get("bid"),
            "ask": fill.get("ask"),
            "spread_percent": fill.get("spread_percent"),
            "slippage_bps": fill.get("slippage_bps"),
            "slippage_amount": float(fill.get("slippage_per_unit") or 0.0) * int(quantity),
            "transaction_cost": float(transaction_cost),
            "quantity": int(quantity),
            "lot_size": lot_size or None,
            "avwap": self._number(avwap),
            "quote_timestamp": fill.get("quote_timestamp"),
            "quote_age_seconds": fill.get("quote_age_seconds"),
            "execution_method": fill.get("execution_method"),
            "underlying_spot": self._number(underlying_spot),
            "margin_estimate": self._number(margin_estimate),
            "margin_model": margin_model,
            "raw": raw,
        }
        self.db.record_paper_execution(event)
        return event

    def execute_entry(
        self,
        contract: OptionContract,
        quantity: int,
        signal: Signal,
        fill_price: Optional[float] = None,
        *,
        quote=None,
        execution_time=None,
        underlying_spot=None,
    ) -> FillResult:
        if self.positions.has_open(contract.security_id):
            log.info("Paper entry skipped (position already exists): %s", contract.name)
            return FillResult(order_id="", status="REJECTED", raw={"reason": "position_exists"})
        quantity_number = self._number(quantity)
        lot_number = self._number(getattr(contract, "lot_size", None))
        if (quantity_number is None or quantity_number <= 0
                or not quantity_number.is_integer()):
            return FillResult(order_id="", status="REJECTED", raw={"reason": "invalid_quantity"})
        if lot_number is None or lot_number <= 0 or not lot_number.is_integer():
            return FillResult(order_id="", status="REJECTED", raw={"reason": "invalid_lot_size"})
        quantity = int(quantity_number)
        lot_size = int(lot_number)
        if quantity % lot_size:
            return FillResult(order_id="", status="REJECTED", raw={
                "reason": "quantity_not_lot_multiple",
                "quantity": quantity,
                "lot_size": lot_size,
            })

        fill = self._resolve_fill(
            contract=contract, signal=signal, is_sell=True, quote=quote,
            execution_time=execution_time, explicit_fill_price=fill_price,
        )
        if fill.get("error"):
            return FillResult(order_id="", status="REJECTED", raw=fill)
        price = float(fill["fill_price"])
        cost = self._transaction_cost(price, quantity)
        margin = self._margin_estimate(underlying_spot, quantity)
        order_id = f"PAPER-{uuid.uuid4().hex[:16]}"
        # Preserve the existing position timestamp convention for strategy and
        # risk compatibility; the exact execution timestamp is separately
        # stored in paper_execution_events.timestamp.
        position = self.positions.open_position(
            contract=contract,
            quantity=quantity,
            entry_price=price,
            ts=int(signal.candle_ts),
            reason=signal.reason,
            avwap=signal.avwap,
            mode=self.mode,
            order_id=order_id,
            status="OPEN",
        )
        detail = self._persist_fill(
            order_id=order_id,
            position_id=position["position_id"],
            contract=contract,
            event_type="ENTRY_SELL",
            quantity=quantity,
            avwap=signal.avwap,
            fill=fill,
            transaction_cost=cost,
            underlying_spot=underlying_spot,
            margin_estimate=margin,
            extra={"signal_reason": signal.reason, "action": ENTRY_SELL},
        )
        self.db.save_order({
            "order_id": order_id,
            "position_id": position["position_id"],
            "security_id": contract.security_id,
            "action": ENTRY_SELL,
            "side": "SELL",
            "quantity": quantity,
            "order_type": "PAPER",
            "status": "COMPLETE",
            "filled_qty": quantity,
            "avg_price": price,
            "placed_at": int(fill["timestamp"]),
            "updated_at": int(fill["timestamp"]),
            "raw": detail,
        })
        self.journal.write(
            "PAPER_ENTRY",
            ts=int(fill["timestamp"]),
            position_id=position["position_id"],
            security_id=contract.security_id,
            symbol=contract.name,
            detail={
                **detail,
                "fill_price": price,
                "signal_price": signal.signal_price,
                "avwap": signal.avwap,
                "quantity": quantity,
                "lot_size": contract.lot_size,
                "reason": signal.reason,
            },
        )
        log.info(
            "PAPER SELL %s qty=%d @ %.4f via %s (signal %.4f, bid=%s ask=%s)",
            contract.name, quantity, price, fill["execution_method"],
            signal.signal_price,
            f"{fill['bid']:.4f}" if fill.get("bid") is not None else "n/a",
            f"{fill['ask']:.4f}" if fill.get("ask") is not None else "n/a",
        )
        return FillResult(order_id=order_id, status="COMPLETE", filled_qty=quantity, avg_price=price,
                          raw={"execution_method": fill["execution_method"]})

    def execute_exit(
        self,
        position: dict,
        signal: Optional[Signal],
        fill_price: Optional[float] = None,
        reason: str = "CLOSE_ABOVE_AVWAP",
        *,
        quote=None,
        execution_time=None,
        contract: Optional[OptionContract] = None,
        fill_price_method: Optional[str] = None,
    ) -> FillResult:
        if position.get("status") not in ("OPEN", "PENDING"):
            log.info("Paper exit skipped (position %s not open)", position.get("position_id"))
            return FillResult(order_id="", status="REJECTED", raw={"reason": "not_open"})
        quantity = int(position.get("quantity") or 0)
        if quantity <= 0:
            return FillResult(order_id="", status="REJECTED", raw={"reason": "invalid_quantity"})
        contract = contract or _contract_from_position(position)
        fill = self._resolve_fill(
            contract=contract, signal=signal, is_sell=False, quote=quote,
            execution_time=execution_time, explicit_fill_price=fill_price,
            explicit_fill_method=fill_price_method,
        )
        if fill.get("error"):
            return FillResult(order_id="", status="REJECTED", raw=fill)
        price = float(fill["fill_price"])
        cost = self._transaction_cost(price, quantity)
        # Position close time follows strategy-candle timing when signal-driven;
        # forced/manual closes use the actual simulation timestamp.
        position_ts = int(signal.candle_ts) if signal is not None else int(fill["timestamp"])
        avwap = signal.avwap if signal is not None else position.get("exit_avwap")
        order_id = f"PAPER-{uuid.uuid4().hex[:16]}"
        closed = self.positions.close_position(
            position_id=position["position_id"],
            exit_price=price,
            ts=position_ts,
            reason=reason,
            avwap=avwap,
            order_id=order_id,
        )
        entry_events = self.db.get_paper_execution_for_position(position["position_id"])
        entry_event = next((row for row in entry_events if row.get("event_type") == "ENTRY_SELL"), None)
        detail = self._persist_fill(
            order_id=order_id,
            position_id=position["position_id"],
            contract=contract,
            event_type="EXIT_BUY",
            quantity=quantity,
            avwap=avwap,
            fill=fill,
            transaction_cost=cost,
            underlying_spot=entry_event.get("underlying_spot") if entry_event else None,
            margin_estimate=entry_event.get("margin_estimate") if entry_event else None,
            extra={"exit_reason": reason, "action": EXIT_BUY},
        )
        self.db.save_order({
            "order_id": order_id,
            "position_id": position["position_id"],
            "security_id": position["security_id"],
            "action": EXIT_BUY,
            "side": "BUY",
            "quantity": quantity,
            "order_type": "PAPER",
            "status": "COMPLETE",
            "filled_qty": quantity,
            "avg_price": price,
            "placed_at": int(fill["timestamp"]),
            "updated_at": int(fill["timestamp"]),
            "raw": detail,
        })
        self.journal.write(
            "PAPER_EXIT",
            ts=int(fill["timestamp"]),
            position_id=position["position_id"],
            security_id=position["security_id"],
            symbol=position.get("symbol"),
            detail={
                **detail,
                "fill_price": price,
                "signal_price": signal.signal_price if signal else fill_price,
                "avwap": avwap,
                "pnl": closed["pnl"],
                "reason": reason,
            },
        )
        log.info("PAPER BUY-TO-CLOSE %s @ %.4f -> P&L %.2f (%s; %s)",
                 position.get("symbol"), price, closed["pnl"], reason,
                 fill["execution_method"])
        return FillResult(order_id=order_id, status="COMPLETE", filled_qty=quantity,
                          avg_price=price, raw={"execution_method": fill["execution_method"]})

    def recover_incomplete_records(self) -> list[str]:
        """Repair audit rows if a crash occurred between position and event writes.

        Existing positions remain authoritative: recovered rows explicitly have
        no bid/ask and are marked as reconstructed rather than inventing a quote.
        """
        repaired = []
        for raw_position in self.db.get_positions(limit=100000):
            position = dict(raw_position)
            if position.get("mode") != "PAPER":
                continue
            entries = self.db.get_paper_execution_for_position(position["position_id"])
            event_types = {item.get("event_type") for item in entries}
            if "ENTRY_SELL" not in event_types and position.get("entry_price") is not None:
                order_id = position.get("entry_order_id") or f"PAPER-RECOVER-ENTRY-{position['position_id']}"
                self.db.record_paper_execution({
                    "order_id": order_id,
                    "position_id": position["position_id"],
                    "security_id": position.get("security_id") or "",
                    "event_type": "ENTRY_SELL",
                    "timestamp": int(position.get("entry_time") or 0),
                    "signal_timestamp": int(position.get("entry_time") or 0),
                    "signal_price": position.get("entry_price"),
                    "fill_price": position.get("entry_price"),
                    "bid": None, "ask": None, "spread_percent": None,
                    "slippage_bps": self.slippage_bps, "slippage_amount": None,
                    "transaction_cost": 0.0,
                    "quantity": int(position.get("quantity") or 0),
                    "lot_size": None, "avwap": position.get("entry_avwap"),
                    "quote_timestamp": None, "quote_age_seconds": None,
                    "execution_method": "RECOVERED_FROM_POSITION_NO_QUOTE",
                    "underlying_spot": None, "margin_estimate": None,
                    "margin_model": "unavailable_after_recovery",
                    "raw": {"reconstructed": True, "note": "no quote is synthesized"},
                })
                repaired.append(position["position_id"])
            if position.get("status") == "CLOSED" and "EXIT_BUY" not in event_types:
                order_id = position.get("exit_order_id") or f"PAPER-RECOVER-EXIT-{position['position_id']}"
                self.db.record_paper_execution({
                    "order_id": order_id,
                    "position_id": position["position_id"],
                    "security_id": position.get("security_id") or "",
                    "event_type": "EXIT_BUY",
                    "timestamp": int(position.get("exit_time") or 0),
                    "signal_timestamp": int(position.get("exit_time") or 0),
                    "signal_price": position.get("exit_price"),
                    "fill_price": position.get("exit_price"),
                    "bid": None, "ask": None, "spread_percent": None,
                    "slippage_bps": self.slippage_bps, "slippage_amount": None,
                    "transaction_cost": 0.0,
                    "quantity": int(position.get("quantity") or 0),
                    "lot_size": None, "avwap": position.get("exit_avwap"),
                    "quote_timestamp": None, "quote_age_seconds": None,
                    "execution_method": "RECOVERED_FROM_POSITION_NO_QUOTE",
                    "underlying_spot": None, "margin_estimate": None,
                    "margin_model": "unavailable_after_recovery",
                    "raw": {"reconstructed": True, "note": "no quote is synthesized"},
                })
                repaired.append(position["position_id"])
        return sorted(set(repaired))


def _contract_from_position(position: dict) -> OptionContract:
    return OptionContract(
        security_id=str(position.get("security_id") or ""),
        symbol=str(position.get("symbol") or position.get("security_id") or "UNKNOWN"),
        underlying=str(position.get("underlying") or ""),
        strike=float(position.get("strike") or 0.0),
        option_type=str(position.get("option_type") or ""),
        expiry=str(position.get("expiry") or ""),
        lot_size=int(position.get("lot_size") or position.get("quantity") or 0),
    )
