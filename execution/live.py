"""Live broker: real Dhan orders. Same strategy engine as paper (spec §26).

Order lifecycle (spec §27):
    SIGNAL -> ORDER SENT -> (poll) ACCEPTED/FILLED / PARTIAL / REJECTED
Position state is reconciled with Dhan; an order being sent is NEVER
assumed to be filled.

Duplicate protection (spec §28):
    * one PENDING entry order per contract at a time
    * one PENDING exit order per position at a time
    * signals are already deduped upstream by (contract, candle, action)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from common.models import ENTRY_SELL, EXIT_BUY, FillResult, OptionContract, Signal
from dhan.orders import DhanOrders
from execution.broker import Broker

log = logging.getLogger("avwap.execution.live")


class DhanBroker(Broker):
    mode = "LIVE"

    def __init__(self, rest, orders: DhanOrders, db, positions, journal,
                 product_type: str = "MARGIN", order_type: str = "MARKET",
                 limit_offset_ticks: int = 1, tick_size: float = 0.05,
                 confirm: str = "I_UNDERSTAND_LIVE_TRADING",
                 live_confirmed: bool = False):
        if not live_confirmed:
            raise RuntimeError(
                "Refusing to construct DhanBroker without live confirmation. "
                "Set live.confirm_phrase in config AND pass --i-understand-live."
            )
        self.rest = rest
        self.orders = orders
        self.db = db
        self.positions = positions
        self.journal = journal
        self.product_type = product_type
        self.order_type = order_type
        self.limit_offset_ticks = int(limit_offset_ticks)
        self.tick_size = float(tick_size)

    # -------------------------------------------------------------- helpers
    def _limit_price(self, ref: float, is_sell: bool) -> float:
        off = self.limit_offset_ticks * self.tick_size
        return round(ref + off if is_sell else ref - off, 2)

    def _save_order(self, order_id: str, position_id: Optional[str],
                    security_id: str, action: str, side: str, quantity: int,
                    status: str, raw: dict) -> None:
        now = int(time.time())
        self.db.save_order({
            "order_id": order_id,
            "position_id": position_id,
            "security_id": security_id,
            "action": action,
            "side": side,
            "quantity": quantity,
            "order_type": self.order_type,
            "status": status,
            "filled_qty": 0,
            "avg_price": None,
            "placed_at": now,
            "updated_at": now,
            "raw": raw,
        })

    # ------------------------------------------------------------- entry
    def execute_entry(self, contract: OptionContract, quantity: int,
                      signal: Signal, fill_price: Optional[float] = None) -> FillResult:
        # duplicate protection: only one open/pending short per contract
        existing = self.db.find_position_by_security(
            contract.security_id, statuses=("OPEN", "PENDING"))
        if existing is not None:
            log.info("LIVE entry skipped, position already open/pending: %s",
                     contract.name)
            return FillResult(order_id="", status="REJECTED",
                              raw={"reason": "position_exists"})

        price = self._limit_price(signal.signal_price, is_sell=True) \
            if self.order_type == "LIMIT" else 0.0
        try:
            order_id = self.orders.place(
                security_id=contract.security_id,
                transaction_type="SELL",
                quantity=quantity,
                product_type=self.product_type,
                order_type=self.order_type,
                price=price,
            )
        except Exception as e:
            log.error("LIVE entry order FAILED for %s: %s", contract.name, e)
            self.journal.write(
                "ORDER_FAILED", ts=int(time.time()),
                security_id=contract.security_id, symbol=contract.name,
                detail={"action": ENTRY_SELL, "error": str(e)},
                level=logging.ERROR,
            )
            return FillResult(order_id="", status="REJECTED", raw={"error": str(e)})

        pos = self.positions.open_position(
            contract=contract,
            quantity=quantity,
            entry_price=None,
            ts=int(time.time()),
            reason=signal.reason,
            avwap=signal.avwap,
            mode=self.mode,
            order_id=order_id,
            status="PENDING",
        )
        self._save_order(order_id, pos["position_id"], contract.security_id,
                         ENTRY_SELL, "SELL", quantity, "PENDING",
                         {"signal": signal.to_row()})
        self.journal.write(
            "ORDER_SENT",
            ts=int(time.time()),
            position_id=pos["position_id"],
            security_id=contract.security_id,
            symbol=contract.name,
            detail={"order_id": order_id, "action": ENTRY_SELL,
                    "side": "SELL", "quantity": quantity,
                    "order_type": self.order_type, "price": price or None},
        )
        return FillResult(order_id=order_id, status="PENDING")

    # -------------------------------------------------------------- exit
    def execute_exit(self, position: dict, signal: Optional[Signal],
                     fill_price: Optional[float] = None,
                     reason: str = "CLOSE_ABOVE_AVWAP") -> FillResult:
        if position["status"] != "OPEN":
            return FillResult(order_id="", status="REJECTED",
                              raw={"reason": f"status_{position['status']}"})
        if position.get("exit_order_id"):
            log.info("LIVE exit skipped, exit order already pending: %s",
                     position["position_id"])
            return FillResult(order_id=position["exit_order_id"], status="PENDING")

        ref = fill_price if fill_price is not None else (
            signal.signal_price if signal else position.get("entry_price"))
        if ref is None:
            return FillResult(order_id="", status="REJECTED",
                              raw={"reason": "no_reference_price"})
        price = self._limit_price(ref, is_sell=False) if self.order_type == "LIMIT" else 0.0
        try:
            order_id = self.orders.place(
                security_id=position["security_id"],
                transaction_type="BUY",
                quantity=position["quantity"],
                product_type=self.product_type,
                order_type=self.order_type,
                price=price,
            )
        except Exception as e:
            log.error("LIVE exit order FAILED for %s: %s",
                      position["symbol"], e)
            self.journal.write(
                "ORDER_FAILED", ts=int(time.time()),
                position_id=position["position_id"],
                security_id=position["security_id"], symbol=position["symbol"],
                detail={"action": EXIT_BUY, "error": str(e)},
                level=logging.ERROR,
            )
            return FillResult(order_id="", status="REJECTED", raw={"error": str(e)})

        self.positions.update(position["position_id"], exit_order_id=order_id)
        self._save_order(order_id, position["position_id"], position["security_id"],
                         EXIT_BUY, "BUY", position["quantity"], "PENDING",
                         {"signal": signal.to_row() if signal else None})
        self.journal.write(
            "ORDER_SENT",
            ts=int(time.time()),
            position_id=position["position_id"],
            security_id=position["security_id"],
            symbol=position["symbol"],
            detail={"order_id": order_id, "action": EXIT_BUY, "side": "BUY",
                    "quantity": position["quantity"], "reason": reason,
                    "order_type": self.order_type, "price": price or None},
        )
        return FillResult(order_id=order_id, status="PENDING")

    # ------------------------------------------------------------ polling
    def poll_fills(self) -> None:
        """Poll all PENDING orders; apply fills to positions."""
        open_orders = self.db.get_orders(statuses=("PENDING", "PARTIAL"), limit=100)
        for o in open_orders:
            try:
                raw = self.orders.status(o["order_id"])
                st = self.orders.normalize_status(raw)
            except Exception as e:
                log.warning("order status poll failed %s: %s", o["order_id"], e)
                continue
            status, filled, avg = st["status"], st["filled_qty"], st["avg_price"]
            now = int(time.time())
            self.db.update_order(o["order_id"], status=status,
                                 filled_qty=filled, avg_price=avg, updated_at=now)

            pos = self.db.get_position(o["position_id"]) if o["position_id"] else None
            if pos is None:
                continue
            if status == "COMPLETE":
                self._apply_fill(o, pos, filled or pos["quantity"], avg, now)
            elif status in ("REJECTED", "CANCELLED"):
                self._apply_rejection(o, pos, st.get("raw", {}), now)
            elif status == "PARTIAL":
                self.journal.write(
                    "ORDER_PARTIAL", ts=now,
                    position_id=pos["position_id"],
                    security_id=pos["security_id"], symbol=pos["symbol"],
                    detail={"order_id": o["order_id"], "filled_qty": filled,
                            "avg_price": avg},
                )

    def _apply_fill(self, o, pos: dict, filled_qty: int, avg: Optional[float], now: int) -> None:
        if o["action"] == ENTRY_SELL:
            if pos["status"] != "PENDING":
                return
            if pos["security_id"] != o["security_id"]:
                return
            self.positions.update(pos["position_id"], status="OPEN",
                                  entry_price=avg, entry_time=now)
            self.journal.write(
                "FILL", ts=now,
                position_id=pos["position_id"],
                security_id=pos["security_id"], symbol=pos["symbol"],
                detail={"order_id": o["order_id"], "action": ENTRY_SELL,
                        "filled_qty": filled_qty, "avg_price": avg},
            )
            log.info("LIVE FILL (entry) %s qty=%d @ %s",
                     pos["symbol"], filled_qty, f"{avg:.2f}" if avg else "?")
        elif o["action"] == EXIT_BUY:
            if pos["status"] != "OPEN":
                return
            pnl = None
            if avg is not None and pos.get("entry_price") is not None:
                pnl = (pos["entry_price"] - avg) * pos["quantity"]
            self.positions.close_position(
                pos["position_id"], exit_price=avg, ts=now,
                reason=pos.get("exit_reason") or "CLOSE_ABOVE_AVWAP",
                avwap=None, order_id=o["order_id"], pnl_override=pnl,
            )
            self.journal.write(
                "FILL", ts=now,
                position_id=pos["position_id"],
                security_id=pos["security_id"], symbol=pos["symbol"],
                detail={"order_id": o["order_id"], "action": EXIT_BUY,
                        "filled_qty": filled_qty, "avg_price": avg, "pnl": pnl},
            )
            log.info("LIVE FILL (exit) %s qty=%d @ %s pnl=%s",
                     pos["symbol"], filled_qty, f"{avg:.2f}" if avg else "?",
                     f"{pnl:.2f}" if pnl is not None else "?")

    def _apply_rejection(self, o, pos: dict, raw: dict, now: int) -> None:
        if o["action"] == ENTRY_SELL and pos["status"] == "PENDING":
            self.positions.update(pos["position_id"], status="REJECTED")
            self.journal.write(
                "ORDER_REJECTED", ts=now,
                position_id=pos["position_id"],
                security_id=pos["security_id"], symbol=pos["symbol"],
                detail={"order_id": o["order_id"], "raw": str(raw)[:500]},
                level=logging.ERROR,
            )
            log.error("LIVE entry REJECTED: %s order=%s raw=%s",
                      pos["symbol"], o["order_id"], str(raw)[:300])
        elif o["action"] == EXIT_BUY and pos["status"] == "OPEN":
            # an exit that was rejected: position stays OPEN, alert loudly
            self.positions.update(pos["position_id"], exit_order_id=None)
            self.journal.write(
                "ORDER_REJECTED_EXIT", ts=now,
                position_id=pos["position_id"],
                security_id=pos["security_id"], symbol=pos["symbol"],
                detail={"order_id": o["order_id"], "raw": str(raw)[:500],
                        "note": "position still OPEN - manual attention required"},
                level=logging.ERROR,
            )
            log.error("LIVE exit REJECTED for %s - position still OPEN, "
                      "manual attention required", pos["symbol"])

    # -------------------------------------------------------- reconciliation
    def reconcile_positions(self) -> None:
        """Align DB positions with Dhan (restart recovery, spec §39)."""
        try:
            dhan_pos = self.orders.positions()
        except Exception as e:
            log.warning("position reconciliation failed: %s", e)
            return
        dhan_shorts = {p["security_id"]: p for p in dhan_pos
                       if p.get("quantity", 0) < 0}

        # 1) my open/pending positions that Dhan no longer holds
        for pos in self.db.get_positions(statuses=("OPEN",), limit=500):
            sec = pos["security_id"]
            if sec not in dhan_shorts:
                log.warning(
                    "RECONCILE: position %s (%s) OPEN in DB but not found on "
                    "Dhan - marking CLOSED/RECONCILED (verify manually!)",
                    pos["position_id"], pos["symbol"],
                )
                self.positions.update(pos["position_id"], status="CLOSED",
                                      exit_reason="RECONCILED_NOT_FOUND")
                self.journal.write(
                    "RECONCILE", ts=int(time.time()),
                    position_id=pos["position_id"],
                    security_id=sec, symbol=pos["symbol"],
                    detail={"note": "not found on Dhan; marked CLOSED/RECONCILED"},
                    level=logging.WARNING,
                )

        # 2) Dhan short positions the system does not know about
        for sec, p in dhan_shorts.items():
            known = self.db.find_position_by_security(sec, statuses=("OPEN", "PENDING"))
            if known is not None:
                continue
            log.warning(
                "RECONCILE: found UNMANAGED short on Dhan: %s qty=%d @ %s - "
                "adopting into the position universe so it gets AVWAP exit "
                "monitoring (verify manually!)",
                p.get("symbol"), p["quantity"], p.get("avg_price"),
            )
            pos = self.positions.open_position(
                contract=None,  # unknown contract metadata
                security_id=sec,
                quantity=abs(int(p["quantity"])),
                entry_price=p.get("avg_price"),
                ts=int(time.time()),
                reason="ADOPTED_ON_RECONCILE",
                avwap=None,
                mode="LIVE",
                order_id=None,
                status="OPEN",
                source="dhan_external",
            )
            self.journal.write(
                "RECONCILE_ADOPT", ts=int(time.time()),
                position_id=pos["position_id"],
                security_id=sec, symbol=p.get("symbol"),
                detail={"quantity": abs(int(p["quantity"])),
                        "avg_price": p.get("avg_price")},
                level=logging.WARNING,
            )

    # ----------------------------------------------------------- emergency
    def exit_all(self, ltp_getter, reason: str) -> int:
        n = 0
        for pos in self.db.get_positions(statuses=("OPEN",), limit=500):
            ltp = ltp_getter(pos["security_id"]) or pos.get("entry_price")
            if ltp is None:
                log.error("exit_all: no price for %s; SKIPPED (manual!) ", pos["symbol"])
                continue
            res = self.execute_exit(pos, None, fill_price=ltp, reason=reason)
            if res.status != "REJECTED":
                n += 1
        return n
