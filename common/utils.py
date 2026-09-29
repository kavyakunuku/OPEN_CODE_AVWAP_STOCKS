"""Shared utilities: time handling (IST), session math, logging."""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    # Windows: Python's zoneinfo has no OS timezone database and needs
    # `pip install tzdata`. Rather than crash at import, fall back to a
    # fixed UTC+05:30 offset - IST has no DST, so this is exactly
    # equivalent to Asia/Kolkata.
    IST = timezone(timedelta(hours=5, minutes=30), "IST")
    logging.getLogger("avwap.utils").warning(
        "Timezone database not found for 'Asia/Kolkata' - using fixed UTC+05:30 "
        "(identical for IST). To silence this: pip install tzdata")

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)

CANDLES_PER_SESSION = 26  # 09:15 -> 15:30 in 15-minute candles


def now_ist() -> datetime:
    return datetime.now(IST)


def session_state(now: datetime) -> str:
    """PRE_OPEN | OPEN | CLOSED for the given IST datetime.

    Weekends are CLOSED (NSE is shut) - without this, a restart on a
    Saturday/Sunday would poll Dhan all day for quotes that can never
    come and flood the log with "NO quotes" warnings.
    """
    if is_weekend(now):
        return "CLOSED"
    t = now.time()
    if t < SESSION_OPEN:
        return "PRE_OPEN"
    if t < SESSION_CLOSE:
        return "OPEN"
    return "CLOSED"


def is_weekend(dt: datetime) -> bool:
    return dt.weekday() >= 5  # Sat/Sun; NSE holidays are not modelled (logged instead)


def candle_start_for(dt: datetime, interval_minutes: int = 15):
    """Return the start datetime of the 15-min candle containing `dt`,
    or None if `dt` falls outside the NSE session / on the boundary."""
    t = dt.time()
    if t < SESSION_OPEN or t >= SESSION_CLOSE:
        return None
    base = dt.replace(hour=9, minute=15, second=0, microsecond=0)
    step = interval_minutes * 60
    delta = (dt - base).total_seconds()
    start = base + timedelta(seconds=int(delta // step) * step)
    if start.time() >= SESSION_CLOSE:
        return None
    return start


def candle_end_for(start_dt: datetime, interval_minutes: int = 15) -> datetime:
    return start_dt + timedelta(minutes=interval_minutes)


def all_candle_starts(day: datetime, interval_minutes: int = 15) -> list[datetime]:
    """All 15-min candle start times for a session day."""
    start = day.replace(hour=9, minute=15, second=0, microsecond=0)
    end = day.replace(hour=15, minute=30, second=0, microsecond=0)
    step = interval_minutes * 60
    out = []
    cur = start
    while cur < end:
        out.append(cur)
        cur += timedelta(seconds=step)
    return out


def epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def from_epoch(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, IST)


def fmt_ist(ts: int) -> str:
    return from_epoch(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_ist_short(ts: int) -> str:
    return from_epoch(ts).strftime("%H:%M")


def parse_epoch(value, tz: ZoneInfo = IST) -> int | None:
    """Accept epoch seconds, epoch millis, or an ISO string; return epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # millis
            v /= 1000.0
        return int(v)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return parse_epoch(int(s), tz)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz)
                return int(dt.timestamp())
            except ValueError:
                continue
    return None


def round_sig(x: float, digits: int = 6) -> float:
    if x == 0:
        return 0.0
    return round(x, digits - int(math.floor(math.log10(abs(x)))) - 1)


def setup_logging(log_file: str | None = None, level: str = "INFO") -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.handlers = [sh]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logging.getLogger("avwap")


def utc_log_now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
