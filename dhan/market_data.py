"""Dhan market data: intraday candles + market quotes.

Endpoints (Dhan v2) — request/response shapes verified LIVE against the
official dhanhq SDK and Dhan servers on 2026-09-15:

    POST /v2/charts/intraday
        body: {"securityId": int, "exchangeSegment": "NSE_FNO",
               "instrument": "OPTSTK" (NSE stock options) | "OPTIDX" (index),
               "interval": 15, "oi": false,
               "fromDate": "YYYY-MM-DD", "toDate": "YYYY-MM-DD"}
        response: columnar arrays at the TOP level:
        {"timestamp": [...], "open": [...], "high": [...], "low": [...],
         "close": [...], "volume": [...]}
        (parsed defensively; a list-of-objects shape is also tolerated).

    POST /v2/marketfeed/quote   (Market Quote API; 1000 ids/request, 1 req/s)
        body:   {"NSE_FNO": [security_id, ...]}
        data:   {"NSE_FNO": {"<id>": {"last_price": x, "ohlc": {...},
                                      "oi": n, "volume": n,
                                      "depth": {"buy": [{...}], "sell": [{...}]}}}}
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime
from typing import Optional

from common.models import Candle, Quote
from common.utils import candle_start_for, epoch, from_epoch, now_ist, parse_epoch

log = logging.getLogger("avwap.dhan.market_data")


def _finite_number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_count(value) -> Optional[int]:
    number = _finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _depth_levels(value) -> list[dict]:
    if isinstance(value, dict):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    out = []
    for level in candidates:
        if not isinstance(level, dict):
            continue
        price = _finite_number(level.get("price"))
        quantity = _nonnegative_count(level.get("quantity", level.get("qty")))
        # Dhan pads its five-level response with zero-price empty levels.
        if price is None or price <= 0 or quantity is None:
            continue
        out.append({"price": price, "quantity": quantity})
    return out


class DhanMarketData:
    def __init__(self, rest, quote_batch_size: int = 50):
        self.rest = rest
        self.quote_batch_size = max(10, int(quote_batch_size))
        self._quote_request_lock = threading.Lock()
        self._last_quote_request_at = 0.0

    # ------------------------------------------------------------- candles
    def intraday_candles(
        self,
        security_id: str,
        from_dt: datetime,
        to_dt: datetime,
        interval_minutes: int = 15,
        instrument: str = "OPTSTK",
        retries: int = 4,
        exchange_segment: str = "NSE_FNO",
    ) -> list[Candle]:
        """Fetch intraday candles for NSE options or underlying equities.

        Request shape: securityId + exchangeSegment + instrument (OPTSTK for
        NSE stock options; EQUITY with NSE_EQ for underlying stocks) + date-only
        fromDate/toDate. Candle parsing and completed-window alignment are
        shared so callers cannot accidentally apply different 15-minute grids.

        History fetches use extra retries: a failed month-start fetch would
        silently re-anchor a contract's AVWAP to a late date, so history
        calls are worth more 429 backoffs than live-path calls."""
        payload = {
            "securityId": int(security_id),
            "exchangeSegment": str(exchange_segment),
            "instrument": instrument,
            "interval": int(interval_minutes),
            "oi": False,
            "fromDate": from_dt.strftime("%Y-%m-%d"),
            "toDate": to_dt.strftime("%Y-%m-%d"),
        }
        resp = self.rest.post("/v2/charts/intraday", payload=payload, retries=retries)
        return self._parse_candles(security_id, resp, interval_minutes)

    def _parse_candles(self, security_id: str, resp,
                       interval_minutes: int = 15) -> list[Candle]:
        if resp is None:
            return []
        # Live-verified shape (2026-09): columnar arrays at the TOP level,
        # e.g. {"timestamp": [...], "open": [...], ...} (no "data" wrapper).
        if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
            data = resp["data"]
        else:
            data = resp
        out: list[Candle] = []
        dropped = {"outside_session": 0, "bad_row": 0}

        def add(ts, o, h, l, c, v):
            try:
                ts_i = parse_epoch(ts)
            except (TypeError, ValueError, OverflowError, OSError):
                ts_i = None
            if ts_i is None:
                dropped["bad_row"] += 1
                return
            # LIVE-VERIFIED DHAN QUIRK (2026-09): the first candle of the day
            # is timestamped at the first trade (e.g. 09:17 IST) instead of
            # the grid start (09:15). Snap every candle to the start of the
            # session window it belongs to, so dedupe keys, closed-window
            # matching and AVWAP state keys all stay on the 15-min grid.
            try:
                ws = candle_start_for(from_epoch(ts_i), interval_minutes)
            except (TypeError, ValueError, OverflowError, OSError):
                ws = None
            if ws is None:
                # outside the 09:15-15:30 session (e.g. a closing-auction
                # candle labeled 15:30): not a signal candle - drop silently.
                dropped["outside_session"] += 1
                return
            ts_i = epoch(ws)
            prices = tuple(_finite_number(value) for value in (o, h, l, c))
            if any(value is None or value <= 0 for value in prices):
                dropped["bad_row"] += 1
                return
            o, h, l, c = prices
            if h < max(o, l, c) or l > min(o, h, c):
                dropped["bad_row"] += 1
                return
            # Volume is part of the AVWAP input contract. Missing,
            # fractional, negative, malformed, or non-finite counts are
            # rejected rather than silently treated as zero or truncated.
            volume = _nonnegative_count(v)
            if volume is None:
                dropped["bad_row"] += 1
                return
            out.append(Candle(
                security_id=security_id, ts=ts_i,
                open=o, high=h, low=l, close=c, volume=volume,
            ))

        if isinstance(data, dict):
            # columnar form. Pad short/missing fields so malformed rows are
            # counted and dropped rather than raising IndexError mid-response.
            ts_list = data.get("timestamp") or data.get("timestamps")
            if isinstance(ts_list, list) and ts_list:
                rows = len(ts_list)

                def column(name, default):
                    values = data.get(name)
                    if not isinstance(values, list):
                        values = []
                    return values + [default] * max(0, rows - len(values))

                open_values = column("open", None)
                high_values = column("high", None)
                low_values = column("low", None)
                close_values = column("close", None)
                volume_values = column("volume", None)
                for i in range(rows):
                    add(
                        ts_list[i], open_values[i], high_values[i], low_values[i],
                        close_values[i], volume_values[i],
                    )
                if dropped["bad_row"]:
                    log.warning(
                        "intraday candles %s: %d/%d rows dropped as invalid; raw keys=%s",
                        security_id, dropped["bad_row"], rows, list(data.keys())[:12],
                    )
        elif isinstance(data, list):
            # list-of-objects form
            for row in data:
                if not isinstance(row, dict):
                    continue
                add(
                    row.get("timestamp") or row.get("time") or row.get("candle_timestamp"),
                    row.get("open"), row.get("high"), row.get("low"),
                    row.get("close"), row.get("volume"),
                )
        else:
            log.warning(
                "intraday candles %s: unexpected response shape %s; raw=%s",
                security_id, type(data).__name__, str(resp)[:300],
            )
        out.sort(key=lambda c: c.ts)
        return out

    # -------------------------------------------------------------- quotes
    def quotes(self, security_ids: list[str]) -> dict[str, Quote]:
        """Batch LTP, volume, OI, and available top-of-book data via Market Quote API.

        Verified (Dhan v2 docs + live, 2026-09):
            POST /v2/marketfeed/quote
            body:   {"NSE_FNO": [security_id, ...]}   (up to 1000 ids,
                                                         1 request / second)
            data:   {"NSE_FNO": {"<id>": {"last_price": x,
                                          "ohlc": {...}, "oi": n,
                                          "volume": n, ...}}}
        """
        return self.quotes_segment(security_ids, "NSE_FNO")

    def quotes_segment(
        self,
        security_ids: list[str],
        exchange_segment: str = "NSE_FNO",
    ) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        ids = [s for s in dict.fromkeys(security_ids) if s]
        # Market Quote API allows up to 1000 instruments per request, 1 req/s.
        # Default to the max (one request for the whole universe) to stay well
        # under the rate limit; any overflow is paced to >=1.1 s apart.
        batch_size = min(self.quote_batch_size, 1000)
        for i in range(0, len(ids), batch_size):
            batch = ids[i : i + batch_size]
            try:
                data = self._paced_quotes_batch(batch, exchange_segment)
            except Exception as e:
                log.warning("quotes batch failed (%d ids): %s", len(batch), e)
                continue
            # Stamp after the response arrives; do not make an observation
            # appear available before it was actually received.
            now_ts = epoch(now_ist())
            for sec, v in data.items():
                price = _finite_number(v.get("last_price") or v.get("lastPrice"))
                if price is None or price <= 0:
                    continue
                cum = v.get("volume")
                if cum is None:
                    cum = v.get("totalTradedVolume")
                cumulative_volume = _nonnegative_count(cum) if cum is not None else None
                depth = v.get("depth") if isinstance(v.get("depth"), dict) else {}
                bid_depth = _depth_levels(depth.get("buy"))
                ask_depth = _depth_levels(depth.get("sell"))
                best_bid = bid_depth[0] if bid_depth else {}
                best_ask = ask_depth[0] if ask_depth else {}
                out[sec] = Quote(
                    security_id=sec,
                    price=price,
                    cum_volume=cumulative_volume,
                    ts=now_ts,
                    bid=best_bid.get("price"),
                    ask=best_ask.get("price"),
                    bid_quantity=best_bid.get("quantity"),
                    ask_quantity=best_ask.get("quantity"),
                    open_interest=_nonnegative_count(v.get("oi")),
                    bid_depth=bid_depth,
                    ask_depth=ask_depth,
                )
        return out

    def _paced_quotes_batch(self, batch: list[str], exchange_segment: str) -> dict:
        """Serialize all quote-segment requests through the shared 1 req/s limit."""
        with self._quote_request_lock:
            if self._last_quote_request_at:
                elapsed = time.monotonic() - self._last_quote_request_at
                delay = 1.1 - elapsed
                if delay > 0:
                    time.sleep(delay)
            self._last_quote_request_at = time.monotonic()
            return self._quotes_batch(batch, exchange_segment)

    def _quotes_batch(self, batch: list[str], exchange_segment: str) -> dict:
        resp = self.rest.post(
            "/v2/marketfeed/quote",
            payload={exchange_segment: [int(x) for x in batch]},
        )
        if not isinstance(resp, dict):
            return {}
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        seg = data.get(exchange_segment)
        if not isinstance(seg, dict):
            log.warning(
                "quotes: unexpected response shape; keys=%s", list(data.keys())[:8]
            )
            return {}
        return {str(k): v for k, v in seg.items() if isinstance(v, dict)}
