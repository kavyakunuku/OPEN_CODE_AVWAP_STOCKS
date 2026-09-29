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
                                      "oi": n, "volume": n, ...}}}
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from common.models import Candle, Quote
from common.utils import candle_start_for, epoch, from_epoch, parse_epoch

log = logging.getLogger("avwap.dhan.market_data")


class DhanMarketData:
    def __init__(self, rest, quote_batch_size: int = 50):
        self.rest = rest
        self.quote_batch_size = max(10, int(quote_batch_size))

    # ------------------------------------------------------------- candles
    def intraday_candles(
        self,
        security_id: str,
        from_dt: datetime,
        to_dt: datetime,
        interval_minutes: int = 15,
        instrument: str = "OPTSTK",
        retries: int = 4,
    ) -> list[Candle]:
        """Fetch 15-min candles. Request shape verified against the official
        dhanhq SDK (2026-09): securityId + exchangeSegment + instrument
        ('OPTSTK' for NSE stock options) + date-only fromDate/toDate.

        History fetches use extra retries: a failed month-start fetch would
        silently re-anchor a contract's AVWAP to a late date, so history
        calls are worth more 429 backoffs than live-path calls."""
        payload = {
            "securityId": int(security_id),
            "exchangeSegment": "NSE_FNO",
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
            ts_i = parse_epoch(ts)
            if ts_i is None:
                dropped["bad_row"] += 1
                return
            # LIVE-VERIFIED DHAN QUIRK (2026-09): the first candle of the day
            # is timestamped at the first trade (e.g. 09:17 IST) instead of
            # the grid start (09:15). Snap every candle to the start of the
            # session window it belongs to, so dedupe keys, closed-window
            # matching and AVWAP state keys all stay on the 15-min grid.
            ws = candle_start_for(from_epoch(ts_i), interval_minutes)
            if ws is None:
                # outside the 09:15-15:30 session (e.g. a closing-auction
                # candle labeled 15:30): not a signal candle - drop silently.
                dropped["outside_session"] += 1
                return
            ts_i = epoch(ws)
            try:
                o, h, l, c = float(o), float(h), float(l), float(c)
            except (TypeError, ValueError):
                dropped["bad_row"] += 1
                return
            if not (o > 0 and h >= l and c > 0):
                dropped["bad_row"] += 1
                return
            out.append(Candle(
                security_id=security_id, ts=ts_i,
                open=o, high=h, low=l, close=c, volume=int(v or 0),
            ))

        if isinstance(data, dict):
            # columnar form
            ts_list = data.get("timestamp") or data.get("timestamps")
            if ts_list:
                rows = len(ts_list)
                for i in range(rows):
                    add(
                        ts_list[i],
                        (data.get("open") or [None] * rows)[i],
                        (data.get("high") or [None] * rows)[i],
                        (data.get("low") or [None] * rows)[i],
                        (data.get("close") or [None] * rows)[i],
                        (data.get("volume") or [0] * rows)[i],
                    )
                if not out and dropped["bad_row"]:
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
        """Batch LTP + cumulative volume via the Market Quote API.

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
        now_ts = int(datetime.now().timestamp())
        first = True
        for i in range(0, len(ids), batch_size):
            if not first:
                time.sleep(1.1)  # honor the 1-request/second marketfeed limit
            first = False
            batch = ids[i : i + batch_size]
            try:
                data = self._quotes_batch(batch, exchange_segment)
            except Exception as e:
                log.warning("quotes batch failed (%d ids): %s", len(batch), e)
                continue
            for sec, v in data.items():
                price = v.get("last_price") or v.get("lastPrice")
                if price is None:
                    continue
                try:
                    price = float(price)
                    if price <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                cum = v.get("volume") or v.get("totalTradedVolume") or 0
                out[sec] = Quote(
                    security_id=sec,
                    price=price,
                    cum_volume=int(cum or 0),
                    ts=now_ts,
                )
        return out

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
