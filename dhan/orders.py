"""Legacy Dhan order adapter, with order placement hard-disabled in Phase 8.

Read-only status/position methods remain for compatibility; ``place`` raises
before any REST request. This module is not imported by the Phase 8 app.
"""
from __future__ import annotations

import logging

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
        """Hard stop for the legacy placement API during Phase 8."""
        raise RuntimeError(
            "Dhan live order placement is hard-disabled in Phase 8; "
            "no order-placement endpoint may be called before Phase 9 approval"
        )

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
