"""15-minute candle engine.

Two input modes feed the same engine:

1. `ingest_closed(candle)` - authoritative completed candles delivered by a
   data feed (Dhan intraday-candle polling, or the dev mock). This is the
   default path (market_data.mode = "candle_poll").

2. `ingest_tick(...)` + `close_due(now)` - candles built from live ticks
   (websocket / fast polling). Partial candles are built in memory and
   finalized exactly when the candle's 15-minute window has elapsed.

Rules enforced here (spec §8, §9):
  * NSE session boundaries: 09:15 - 15:30 IST, 15-minute candles.
  * The first possible completed candle of the day is 09:15-09:30.
  * NO signal is ever generated from an incomplete candle.
  * Duplicate / out-of-order candles are dropped.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from common.models import Candle
from common.utils import (
    SESSION_CLOSE,
    SESSION_OPEN,
    candle_end_for,
    candle_start_for,
    epoch,
    from_epoch,
)

log = logging.getLogger("avwap.market.candles")


class CandleEngine:
    def __init__(self, interval_minutes: int = 15):
        self.interval = interval_minutes
        self._partial: dict[str, dict] = {}        # sec_id -> partial candle
        self._baseline_vol: dict[str, int] = {}    # sec_id -> cumulative vol at window start
        self._last_closed_ts: dict[str, int] = {}  # sec_id -> last delivered closed ts

    # ------------------------------------------------------- closed candles
    def ingest_closed(self, candle: Candle) -> Optional[Candle]:
        """Accept a completed candle after boundary + duplicate validation."""
        # 1. must start on a valid session boundary
        start_dt = from_epoch(candle.ts)
        expected = candle_start_for(start_dt.replace(second=0, microsecond=0), self.interval)
        if expected is None:
            log.warning("Dropped candle %s ts=%s: outside session", candle.security_id, candle.ts)
            return None
        if epoch(expected) != candle.ts:
            log.warning(
                "Dropped candle %s ts=%s: not on a %s-min boundary",
                candle.security_id, candle.ts, self.interval,
            )
            return None
        # 2. duplicate / out-of-order protection
        last = self._last_closed_ts.get(candle.security_id)
        if last is not None and candle.ts <= last:
            return None
        self._last_closed_ts[candle.security_id] = candle.ts
        # sanity: OHLC consistency
        if not (candle.low <= candle.open <= candle.high and
                candle.low <= candle.close <= candle.high):
            log.warning(
                "Candle %s ts=%s OHLC inconsistent (h=%.2f l=%.2f o=%.2f c=%.2f)",
                candle.security_id, candle.ts, candle.high, candle.low, candle.open, candle.close,
            )
        return candle

    # ------------------------------------------------------------- ticks
    def ingest_tick(
        self,
        security_id: str,
        ts: int,
        price: float,
        cum_volume: Optional[int] = None,
    ) -> None:
        """Build/maintain the in-progress candle from a live tick."""
        dt = from_epoch(ts)
        start = candle_start_for(dt, self.interval)
        if start is None:
            return  # outside session
        start_ts = epoch(start)

        p = self._partial.get(security_id)
        if p is None or p["ts"] != start_ts:
            self._partial[security_id] = {
                "ts": start_ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0,
            }
            self._baseline_vol[security_id] = 0 if cum_volume is None else cum_volume
            return

        p["high"] = max(p["high"], price)
        p["low"] = min(p["low"], price)
        p["close"] = price
        if cum_volume is not None:
            base = self._baseline_vol.get(security_id, 0)
            if cum_volume < base:
                # new trading day (or feed reset): re-baseline
                log.info("Volume baseline reset for %s (new day)", security_id)
                self._baseline_vol[security_id] = cum_volume
            else:
                p["volume"] = int(cum_volume - base)

    def close_due(self, now: datetime) -> list[Candle]:
        """Finalize every partial candle whose window has fully elapsed."""
        out: list[Candle] = []
        for sec_id, p in list(self._partial.items()):
            end_dt = candle_end_for(from_epoch(p["ts"]), self.interval)
            if now >= end_dt:
                del self._partial[sec_id]
                candle = Candle(
                    security_id=sec_id,
                    ts=p["ts"],
                    open=p["open"],
                    high=p["high"],
                    low=p["low"],
                    close=p["close"],
                    volume=p["volume"],
                )
                accepted = self.ingest_closed(candle)
                if accepted is not None:
                    out.append(accepted)
        return out

    def reset_for_day(self) -> None:
        self._partial.clear()
        self._baseline_vol.clear()


def last_closed_window_start(now: datetime, interval_minutes: int = 15) -> Optional[datetime]:
    """Start time of the most recent 15-min candle that is FULLY closed at
    `now` (None if no candle has closed yet today).

    A candle whose end equals `now` exactly is considered closed.
    """
    from datetime import timedelta
    start = candle_start_for(now, interval_minutes)
    if start is None:
        return None
    prev = start - timedelta(minutes=interval_minutes)
    first = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if prev < first:
        return None
    return prev


def windows_closed_before(now: datetime, interval_minutes: int = 15) -> list[datetime]:
    """All candle start times whose window has fully closed before `now`
    (session boundaries only). Useful to know which windows a poll-based
    feed should fetch."""
    from datetime import timedelta
    last = last_closed_window_start(now, interval_minutes)
    if last is None:
        return []
    step = timedelta(minutes=interval_minutes)
    first = now.replace(hour=9, minute=15, second=0, microsecond=0)
    out = []
    cur = last
    while cur >= first:
        out.append(cur)
        cur -= step
    return list(reversed(out))
