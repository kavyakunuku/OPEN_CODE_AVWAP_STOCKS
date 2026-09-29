"""Shared helpers for the avwap tools (config + Dhan credentials loading)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

IST = timezone(timedelta(hours=5, minutes=30))


def load_config() -> dict:
    path = os.path.join(ROOT, "config", "config.json")
    with open(path) as f:
        return json.load(f)


def dhan_creds(cfg: dict) -> tuple[str, str, str]:
    """(client_id, access_token, base_url) - env vars win over config.json,
    same convention as main.py."""
    client_id = os.environ.get("DHAN_CLIENT_ID") or cfg.get("dhan", {}).get("client_id", "")
    token = os.environ.get("DHAN_ACCESS_TOKEN") or cfg.get("dhan", {}).get("access_token", "")
    base = cfg.get("dhan", {}).get("rest_base_url", "https://api.dhan.co")
    return str(client_id), str(token), base


def db_path(cfg: dict) -> str:
    p = cfg.get("storage", {}).get("db_path", "data/trader.db")
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def ist(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts), IST).strftime("%Y-%m-%d %H:%M IST")


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)


def instrument_for(symbol: str) -> str:
    # All options are stock options (OPTSTK)
    return "OPTSTK"


def vwap_of(candles, mode: str = "typical"):
    """Anchored VWAP over `candles` (oldest->newest). mode: typical|close."""
    num = den = 0.0
    for c in candles:
        p = (c.high + c.low + c.close) / 3.0 if mode == "typical" else c.close
        v = float(c.volume or 0)
        num += p * v
        den += v
    return (num / den) if den > 0 else None


def total_volume(candles) -> float:
    return sum(float(c.volume or 0) for c in candles)
