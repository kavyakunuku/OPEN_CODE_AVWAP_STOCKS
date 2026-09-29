"""Option chain + expiry list (Dhan v2).

Verified against Dhan v2 docs (2026):
    POST /v2/optionchain
        body: {"UnderlyingScrip": int, "UnderlyingSeg": "NSE_FNO", "Expiry": "YYYY-MM-DD"}
        resp: {"data": {"last_price": <spot>,
                        "oc": {"<strike>": {"ce": {...security_id, last_price, oi, volume...},
                                            "pe": {...}}}},
               "status": "success"}
    POST /v2/optionchain/expirylist
        body: {"UnderlyingScrip": int, "UnderlyingSeg": "NSE_FNO"}
        resp: {"data": ["YYYY-MM-DD", ...], "status": "success"}

IMPORTANT: the option-chain API is rate-limited to 1 unique request per 3
seconds. Callers must pace themselves (the bootstrap does).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

log = logging.getLogger("avwap.dhan.option_chain")

CHAIN_RATE_LIMIT_SECONDS = 3.0


@dataclass
class ChainOption:
    strike: float
    option_type: str          # CE | PE
    security_id: str
    ltp: Optional[float] = None
    oi: int = 0
    volume: int = 0
    bid: Optional[float] = None
    ask: Optional[float] = None
    iv: Optional[float] = None


@dataclass
class OptionChainData:
    spot: Optional[float] = None
    expiry: Optional[str] = None
    ce: dict = field(default_factory=dict)   # strike -> ChainOption
    pe: dict = field(default_factory=dict)
    expiry_dates: list = field(default_factory=list)

    @property
    def strikes(self) -> list[float]:
        return sorted(set(list(self.ce.keys()) + list(self.pe.keys())))


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def parse_chain_response(payload: dict) -> OptionChainData:
    data = (payload or {}).get("data") or {}
    out = OptionChainData()
    out.spot = _f(data.get("last_price") or data.get("lastPrice")
                  or data.get("underlying_ltp"))
    oc = data.get("oc") or data.get("optionChain") or {}
    for strike_str, side in oc.items():
        try:
            strike = float(str(strike_str))
        except (TypeError, ValueError):
            continue
        if not isinstance(side, dict):
            continue
        for t, key in (("CE", "ce"), ("PE", "pe")):
            node = side.get(key) or side.get(t.lower())
            if not node:
                continue
            opt = ChainOption(
                strike=strike,
                option_type=t,
                security_id=str(node.get("security_id") or node.get("securityId") or ""),
                ltp=_f(node.get("last_price") or node.get("lastPrice")),
                oi=_i(node.get("oi") or node.get("open_interest")),
                volume=_i(node.get("volume") or node.get("total_traded_volume")),
                bid=_f(node.get("top_bid_price") or node.get("bid_price")),
                ask=_f(node.get("top_ask_price") or node.get("ask_price")),
                iv=_f(node.get("implied_volatility")),
            )
            if opt.security_id:
                (out.ce if t == "CE" else out.pe)[strike] = opt
    return out


def fetch_expiry_list(rest, underlying_security_id: int) -> list[date]:
    payload = rest.post(
        "/v2/optionchain/expirylist",
        payload={
            "UnderlyingScrip": int(underlying_security_id),
            "UnderlyingSeg": "NSE_FNO",
        },
    )
    dates = (payload or {}).get("data") or []
    out = []
    for d in dates:
        try:
            out.append(date.fromisoformat(str(d)[:10]))
        except ValueError:
            continue
    return sorted(out)


def fetch_option_chain(rest, underlying_security_id: int, expiry: str) -> OptionChainData:
    payload = rest.post(
        "/v2/optionchain",
        payload={
            "UnderlyingScrip": int(underlying_security_id),
            "UnderlyingSeg": "NSE_FNO",
            "Expiry": expiry,
        },
    )
    data = parse_chain_response(payload)
    data.expiry = expiry
    return data


class PacedChainClient:
    """Enforces the 1-req-per-3s option-chain rate limit across callers."""

    def __init__(self, rest, min_interval: float = CHAIN_RATE_LIMIT_SECONDS):
        self.rest = rest
        self.min_interval = min_interval
        self._last_call = 0.0

    def _wait(self) -> None:
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def expiry_list(self, underlying_id: int) -> list[date]:
        self._wait()
        try:
            out = fetch_expiry_list(self.rest, underlying_id)
        finally:
            self._last_call = time.time()
        return out

    def chain(self, underlying_id: int, expiry: str) -> OptionChainData:
        self._wait()
        try:
            out = fetch_option_chain(self.rest, underlying_id, expiry)
        finally:
            self._last_call = time.time()
        return out
