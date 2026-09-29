"""Dhan order placement, order status and position retrieval (live mode).

    POST /v2/oddl/requests/order
        body: {correlationId, clientOrderId, transactionType: BUY|SELL,
               exchangeSegment: NSE_FNO, productType: MARGIN|INTRA_DAY|SWING,
               orderType: MARKET|LIMIT|SL|SL-M, securityId, quantity, price,
               validTill: DAY|IOC|DAY_3}
        resp: {"orderId": "..."}
    GET  /v2/oddl/requests/order/get/{orderId}  -> order status/fills
    GET  /v2/positions                          -> held positions

Never assume an order was filled just because it was sent (spec §27).
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

log = logging.getLogger("avwap.dhan.orders")


class DhanOrders:
    def __init__(self, rest):
        self.rest = rest

    def place(
        self,
        security_id: str,
        transaction_type: str,
        quantity: int,
        product_type: str = "MARGIN",
        order_type: str = "MARKET",
        price: float = 0.0,
        trigger_price: float = 0.0,
        valid_till: str = "DAY",
    ) -> str:
        if transaction_type not in ("BUY", "SELL"):
            raise ValueError("transaction_type must be BUY or SELL")
        if quantity <= 0:
            raise ValueError("quantity must be > 0")
        payload = {
            "correlationId": uuid.uuid4().hex,
            "clientOrderId": uuid.uuid4().hex[:16],
            "transactionType": transaction_type,
            "exchangeSegment": "NSE_FNO",
            "productType": product_type,
            "orderType": order_type,
            "securityId": int(security_id),
            "quantity": int(quantity),
            "price": float(price),
            "triggerPrice": float(trigger_price),
            "validTill": valid_till,
        }
        resp = self.rest.post("/v2/oddl/requests/order", payload=payload)
        order_id = (resp or {}).get("orderId")
        if not order_id:
            log.error("Dhan place order got no orderId; raw=%s payload=%s", resp, payload)
            raise RuntimeError(f"Dhan did not return orderId: {resp}")
        log.info(
            "LIVE ORDER placed: %s %s qty=%d %s @%s -> orderId=%s",
            transaction_type, security_id, quantity, order_type,
            f"{price:g}" if price else "MKT", order_id,
        )
        return order_id

    def status(self, order_id: str) -> dict:
        resp = self.rest.get(f"/v2/oddl/requests/order/get/{order_id}")
        if not isinstance(resp, dict):
            return {"status": "UNKNOWN", "raw": resp}
        return resp

    @staticmethod
    def normalize_status(resp: dict) -> dict:
        """Map a raw order-status payload to {status, filled_qty, avg_price}."""
        raw_status = str(resp.get("status") or resp.get("orderStatus") or "").upper()
        filled = resp.get("filledQuantity") or resp.get("filled_quantity") or 0
        avg = resp.get("avgPrice") or resp.get("avg_price") or resp.get("averagePrice")
        try:
            filled = int(float(filled))
        except (TypeError, ValueError):
            filled = 0
        try:
            avg = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            avg = None

        if "COMPLETE" in raw_status or raw_status == "FILLED":
            status = "COMPLETE"
        elif raw_status in ("CANCELLED", "CANCELED", "CANCEL"):
            status = "CANCELLED"
        elif "REJECT" in raw_status:
            status = "REJECTED"
        elif "PARTIAL" in raw_status:
            status = "PARTIAL"
        else:
            status = "PENDING"
        return {"status": status, "filled_qty": filled, "avg_price": avg, "raw": resp}

    def positions(self) -> list[dict]:
        resp = self.rest.get("/v2/positions")
        data = (resp or {}).get("data") or []
        if not isinstance(data, list):
            log.warning("positions: unexpected response shape: %s", str(resp)[:300])
            return []
        out = []
        for p in data:
            if not isinstance(p, dict):
                continue
            out.append(
                {
                    "security_id": str(p.get("securityId") or p.get("security_id") or ""),
                    "symbol": p.get("symbol") or "",
                    "quantity": int(p.get("quantity") or 0),   # negative = short
                    "avg_price": float(p.get("avgPrice") or p.get("average_price") or 0),
                    "pnl": float(p.get("pnl") or 0),
                }
            )
        return out
