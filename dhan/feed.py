"""Mock market feed - DEVELOPMENT ONLY (market_data.source = "mock").

Generates a synthetic NSE-like market so the ENTIRE pipeline (universe,
candles, AVWAP, signals, paper execution, dashboard, persistence) can be
exercised without Dhan credentials.

It simulates:
  * a few fake underlying stocks with monthly options (last-Tuesday expiry)
  * a random-walk underlying spot
  * option premiums = intrinsic + time premium + noise (=> AVWAP crosses)
  * volume concentrated near the money
  * 15-min candles for the last few trading days, anchored at the first
    trading day of the current month (so AVWAP anchors behave realistically)

Nothing in this module touches strategy logic; it only produces data.
"""
from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta
from typing import Optional

from common.models import Candle, OptionContract, Quote
from common.utils import (
    IST,
    all_candle_starts,
    candle_start_for,
    epoch,
    from_epoch,
    is_weekend,
    now_ist,
    session_state,
)

log = logging.getLogger("avwap.dhan.feed.mock")


# ===========================================================================
# DHAN FEED (default, real market data)
#
# Default Dhan feed ("candle_poll"):
#   * authoritative COMPLETED 15-min candles from the Dhan intraday API,
#     fetched shortly after each boundary closes (no intracandle data ever
#     drives a signal);
#   * LTP + cumulative volume from batched quote polling for the dashboard
#     and for the paper "next_quote" fill convention.
# ===========================================================================
class DhanCandlePollFeed:
    source = "dhan"

    def __init__(self, market_data, rest, cfg: dict, clock=None):
        self.md = market_data
        self.rest = rest
        self.cfg = cfg
        self.clock = clock
        self.interval = int(cfg.get("strategy", {}).get("candle_interval_minutes", 15))
        self.history_start_mode = cfg.get("market_data", {}).get("history_start", "month_start")
        self.lookback_days = int(cfg.get("market_data", {}).get("history_lookback_days_fallback", 5))
        # security_id -> candle-API instrument (set by the app after the
        # instrument master loads; index options need OPTIDX, stocks OPTSTK)
        self.instrument_map: dict[str, str] = {}

    def _instrument(self, security_id: str) -> str:
        return self.instrument_map.get(security_id, "OPTSTK")

    def now(self) -> datetime:
        return self.clock.now() if self.clock is not None else now_ist()

    def history_from_dt(self, now: datetime) -> datetime:
        """AVWAP anchor = first tradable candle of the contract. New monthly
        expiries list at the start of the month, so start there; the caller
        falls back to a shorter window if the API cannot serve that far back."""
        if self.history_start_mode == "month_start":
            return now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
        return (now - timedelta(days=self.lookback_days)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )

    def history_for(self, security_id: str, from_dt: datetime,
                    to_dt: Optional[datetime] = None) -> tuple[list[Candle], bool]:
        """Returns (candles, full_range). full_range=False means the caller's
        requested start (from_dt) could NOT be served (API failure + shorter
        fallback, or total failure) - any AVWAP anchored from such a window
        is LATE and must be flagged for re-anchoring."""
        to = to_dt or self.now()
        instr = self._instrument(security_id)
        try:
            return self.md.intraday_candles(security_id, from_dt, to, self.interval,
                                            instrument=instr), True
        except Exception as e:
            log.error("HISTORY FAILED for %s (from %s): %s - AVWAP anchor for this "
                      "contract will be LATE unless repaired via tools/reanchor_avwap.py",
                      security_id, from_dt, e)
            try:
                fb_from = (to - timedelta(days=self.lookback_days)).replace(
                    hour=9, minute=0, second=0, microsecond=0
                )
                log.warning("retrying %s with %s-day lookback (PARTIAL history)",
                            security_id, self.lookback_days)
                return self.md.intraday_candles(security_id, fb_from, to, self.interval,
                                                instrument=instr), False
            except Exception as e2:
                log.error("history fallback FAILED for %s: %s", security_id, e2)
                return [], False

    def closed_candle(self, security_id: str, win_start_ts: int) -> Optional[Candle]:
        """Fetch the completed candle for the window that just closed."""
        win_start = from_epoch(win_start_ts)
        win_end = win_start + timedelta(minutes=self.interval)
        to_dt = win_end + timedelta(seconds=5)  # buffer for API publication
        try:
            candles = self.md.intraday_candles(
                security_id, win_start - timedelta(seconds=5), to_dt, self.interval,
                instrument=self._instrument(security_id),
            )
        except Exception as e:
            log.warning("closed_candle fetch failed %s win=%s: %s", security_id, win_start, e)
            return None
        for c in candles:
            if c.ts == win_start_ts:
                return c
        if candles:
            log.debug("closed_candle: no exact match for %s (got %d, last ts=%s)",
                      security_id, len(candles), candles[-1].ts)
        return None

    def poll_ltp(self, security_ids: list[str]) -> dict[str, Quote]:
        return self.md.quotes(security_ids)


_BASES = {
    "MOCKA": (1520.0, 20.0),
    "MOCKB": (850.0, 10.0),
    "MOCKC": (3200.0, 25.0),
}


class MockFeed:
    source = "mock"

    def __init__(self, cfg: dict, clock=None, seed: Optional[int] = None):
        self.cfg = cfg
        self.clock = clock
        self.rng = random.Random(seed if seed is not None else 42)
        self.interval = int(cfg.get("strategy", {}).get("candle_interval_minutes", 15))
        mock_cfg = cfg.get("market_data", {}).get("mock", {})
        self.underlyings: list[str] = list(mock_cfg.get("underlyings", ["MOCKA"]))
        self.speed = float(mock_cfg.get("speed", 240))
        self.history_days = 6

        self.contracts: dict[str, OptionContract] = {}
        self.spot: dict[str, float] = {}
        self.ltp: dict[str, float] = {}
        self.cum_vol: dict[str, int] = {}
        self._params: dict[str, dict] = {}
        self._win_state: dict[str, dict] = {}          # current partial window
        self._history: dict[str, list[Candle]] = {}    # completed candles (hist + day)
        self._day: Optional[date] = None
        self._last_step: Optional[datetime] = None
        self._initialized = False

        for i, u in enumerate(self.underlyings):
            base, step = _BASES.get(u, (1000.0 + i * 120, 10.0 + i * 5))
            self.spot[u] = base
            self._params[u] = {"step": step, "vol_scale": self.rng.uniform(0.6, 1.4)}

    # ----------------------------------------------------------------- time
    def now(self) -> datetime:
        return self.clock.now() if self.clock is not None else now_ist()

    # -------------------------------------------------------------- universe
    def monthly_expiry(self, today: date) -> date:
        y, m = today.year, today.month
        first_next = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        d = first_next - timedelta(days=1)
        while d.weekday() != 1:  # last Tuesday of the month
            d -= timedelta(days=1)
        return d

    def strikes_for(self, underlying: str) -> list[float]:
        p = self._params[underlying]
        atm0 = round(self.spot[underlying] / p["step"]) * p["step"]
        n = 8
        return [atm0 + k * p["step"] for k in range(-n, n + 1) if atm0 + k * p["step"] > 0]

    def build_contracts(self) -> list[OptionContract]:
        expiry = str(self.monthly_expiry(self.now().date()))
        out: list[OptionContract] = []
        for u in self.underlyings:
            for strike in self.strikes_for(u):
                for t in ("CE", "PE"):
                    sid = f"MOCK_{u}_{int(strike * 100)}_{t}"
                    if sid in self.contracts:
                        continue
                    c = OptionContract(
                        security_id=sid,
                        symbol=f"{u} {strike:g} {t}",
                        underlying=u,
                        strike=strike,
                        option_type=t,
                        expiry=expiry,
                        lot_size=100,
                    )
                    self.contracts[sid] = c
                    self.ltp[sid] = 0.0
                    self.cum_vol[sid] = 0
                    out.append(c)
        return out

    def contract_for(self, security_id: str) -> Optional[OptionContract]:
        return self.contracts.get(security_id)

    def spot_for(self, underlying: str) -> float:
        return self.spot.get(underlying, 0.0)

    # ------------------------------------------------------------- lifecycle
    def initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self.build_contracts()
        self._generate_history()
        now = self.now()
        if session_state(now) == "OPEN":
            for sid in self.contracts:
                self.ltp[sid] = max(self._price(self.contracts[sid]), 0.05)
        log.info("MockFeed initialized: %d contracts, %d underlyings",
                 len(self.contracts), len(self.underlyings))

    def _vol_scale(self, c: OptionContract, spot: float) -> float:
        step = self._params.get(c.underlying, {}).get("step", 10.0)
        if spot <= 0 or step <= 0:
            return 1.0
        dist = abs(spot - c.strike) / step
        return max(0.3, 2.0 - 0.3 * dist)

    def _price(self, c: OptionContract, spot: Optional[float] = None,
               at: Optional[datetime] = None) -> float:
        spot = self.spot[c.underlying] if spot is None else spot
        at = at or self.now()
        intrinsic = max(spot - c.strike, 0.0) if c.option_type == "CE" \
            else max(c.strike - spot, 0.0)
        days_left = max((date.fromisoformat(c.expiry) - at.date()).days, 0)
        p = self._params[c.underlying]
        time_prem = 18.0 * p["vol_scale"] * ((days_left + 1) / 30.0) ** 0.5
        wobble = time_prem * 0.35 * (1 + 0.6 * self.rng.gauss(0, 1))
        return max(intrinsic + time_prem + wobble, 0.05)

    def _generate_history(self) -> None:
        """Completed 15-min candles for recent trading days, starting at the
        first trading day of the current month (the AVWAP anchor day).

        The spot path is generated ONCE per underlying (consistent across
        strikes), then every contract is priced off that same path.
        History is keyed by security_id (per contract).
        """
        now = self.now()
        if session_state(now) != "OPEN":
            return
        today = now.date()

        # all trading days from the FIRST trading day of the month (the AVWAP
        # anchor day for a monthly expiry) through today - mirrors the real
        # system's month_start history fetch
        days: list[date] = []
        d = date(today.year, today.month, 1)
        while d <= today:
            if not is_weekend(d):
                days.append(d)
            d += timedelta(days=1)
        if len(days) > 30:  # safety cap
            days = days[-30:]
        anchor_days = days

        # 1) per-underlying spot path across every candle start
        spot_path: dict[str, list[tuple[int, float]]] = {}
        for u in self.underlyings:
            vol_scale = self._params[u]["vol_scale"]
            spot = self.spot[u] * 0.99
            path: list[tuple[int, float]] = []
            for d in anchor_days:
                is_today = d == today
                for st in all_candle_starts(
                    datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST), self.interval
                ):
                    if is_today and st >= now:
                        break
                    spot *= 1 + self.rng.gauss(0, 0.0008 * vol_scale)
                    path.append((epoch(st), spot))
            spot_path[u] = path

        # 2) price every contract off its underlying's path
        n_candles = 0
        for sid, c in self.contracts.items():
            candles: list[Candle] = []
            for ts_i, spot in spot_path[c.underlying]:
                st = from_epoch(ts_i)
                close_p = self._price(c, spot=spot, at=st)
                open_p = close_p * (1 + self.rng.gauss(0, 0.004))
                hi_p = max(open_p, close_p) * (1 + abs(self.rng.gauss(0, 0.005)))
                lo_p = min(open_p, close_p) * (1 - abs(self.rng.gauss(0, 0.005)))
                vol = max(0, int(self.rng.gauss(
                    120 * self._vol_scale(c, spot), 40)))
                candles.append(Candle(
                    security_id=sid, ts=ts_i,
                    open=round(open_p, 2), high=round(hi_p, 2),
                    low=round(lo_p, 2), close=round(close_p, 2),
                    volume=vol,
                ))
            self._history[sid] = candles
            n_candles = max(n_candles, len(candles))
        log.info("MockFeed history generated: %d anchor days, up to %d candles per contract",
                 len(anchor_days), n_candles)

    # ------------------------------------------------------------ simulation
    def step(self) -> None:
        """Advance the simulation toward `now()` (called each loop tick)."""
        now = self.now()
        if self._day != now.date():
            self._day = now.date()
            self._win_state.clear()
        if session_state(now) != "OPEN":
            return
        if self._last_step is None:
            self._last_step = now
            return
        dt_real = (now - self._last_step).total_seconds()
        if dt_real <= 0:
            return
        self._last_step = now
        dt_sim = dt_real * self.speed

        # advance spots (a few random steps)
        n_steps = max(1, min(int(dt_sim), 4000))
        for u in self.underlyings:
            for _ in range(n_steps):
                self.spot[u] *= 1 + self.rng.gauss(0, 0.00012 * self._params[u]["vol_scale"])
                if self.rng.random() < 0.0001:
                    self.spot[u] *= 1 + self.rng.choice([-1, 1]) * 0.003

        # one aggregated "tick" per contract per step into the open window
        start = candle_start_for(now, self.interval)
        if start is None:
            return
        ts = epoch(start)
        for sid, c in self.contracts.items():
            price = self._price(c)
            vol = max(0, int(self.rng.gauss(
                25 * self._vol_scale(c, self.spot[c.underlying]), 10)))
            w = self._win_state.get(sid)
            if w is not None and w["ts"] != ts:
                # previous window finished: store it as a completed candle
                self._store_window(sid, w)
                self._win_state[sid] = {"ts": ts, "o": price, "h": price,
                                        "l": price, "c": price, "v": 0}
                w = self._win_state[sid]
            if w is None:
                self._win_state[sid] = {"ts": ts, "o": price, "h": price,
                                        "l": price, "c": price, "v": vol}
            else:
                w["h"] = max(w["h"], price)
                w["l"] = min(w["l"], price)
                w["c"] = price
                w["v"] += vol
            self.ltp[sid] = price
            self.cum_vol[sid] += vol

    def _store_window(self, sid: str, w: dict) -> None:
        candle = Candle(
            security_id=sid, ts=w["ts"],
            open=w["o"], high=w["h"], low=w["l"], close=w["c"], volume=w["v"],
        )
        self._history.setdefault(sid, []).append(candle)

    # --------------------------------------------------------------- feed API
    def history_for(self, security_id: str, from_dt: datetime,
                    to_dt: Optional[datetime] = None) -> tuple[list[Candle], bool]:
        out = [c for c in self._history.get(security_id, []) if c.ts >= epoch(from_dt)]
        if to_dt is not None:
            out = [c for c in out if c.ts < epoch(to_dt)]
        return out, True  # mock always serves the full requested window

    def closed_candle(self, security_id: str, win_start_ts: int) -> Optional[Candle]:
        # 1) a partial window matching the requested start (just closed)
        w = self._win_state.get(security_id)
        if w is not None and w["ts"] == win_start_ts:
            del self._win_state[security_id]
            self._store_window(security_id, w)
            c = self._history[security_id][-1]
            return c
        # 2) already stored (history or previously finalized)
        for c in self._history.get(security_id, []):
            if c.ts == win_start_ts:
                return c
        return None

    def poll_ltp(self, security_ids: list[str]) -> dict[str, Quote]:
        now_ts = int(self.now().timestamp())
        out = {}
        for sid in security_ids:
            p = self.ltp.get(sid, 0.0)
            if p > 0:
                out[sid] = Quote(security_id=sid, price=p,
                                 cum_volume=self.cum_vol.get(sid, 0), ts=now_ts)
        return out
