"""TraderApp: the orchestrator.

Data flow (spec §1):

    Dhan market data  ->  Market Data Engine (feed)
                       ->  15-min Candle Engine
                       ->  Option Contract Management (universe)
                       ->  AVWAP Calculation Engine
                       ->  Strategy / Signal Engine
                       ->  PAPER execution  |  LIVE execution
                       ->  Positions / P&L / Journal / Dashboard

The strategy logic is IDENTICAL in both modes; only the broker and data
source change.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

from common.config import get as cfg_get
from common.version import APP_VERSION
from common.models import (
    ENTRY_SELL,
    EXIT_BUY,
    OptionContract,
)
from common.utils import (
    IST,
    SESSION_CLOSE,
    SESSION_OPEN,
    candle_end_for,
    candle_start_for,
    epoch,
    from_epoch,
    is_weekend,
    now_ist,
    session_state,
)
from dhan.client import DhanAPIError, DhanREST
from dhan.feed import DhanCandlePollFeed, MockFeed
from dhan.instruments import (
    fo_stock_universe,
    is_stock_underlying,
    load_master_bundle,
)
from dhan.market_data import DhanMarketData
from dhan.option_chain import PacedChainClient
from dhan.orders import DhanOrders
from execution.broker import Broker
from execution.live import DhanBroker
from execution.paper import PaperBroker
from market.candles import CandleEngine, last_closed_window_start
from market.universe import UniverseManager, select_monthly_expiry
from portfolio.positions import PositionManager
from portfolio.risk import RiskManager
from storage.database import Database
from storage.journal import Journal
from strategy.avwap import AvwapStore
from strategy.engine import SignalEngine

log = logging.getLogger("avwap.app")


def _safe_json_list(v) -> list:
    """Parse a JSON list from the kv store; never raises."""
    if not v:
        return []
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


# ===========================================================================
class SimClock:
    """Simulated IST clock for the mock feed (development only).

    Starts at today's 09:15 IST and runs `speed`x faster than real time.
    Closed periods (after 15:30, nights, weekends) are skipped forward to
    the next session open so a full demo day takes minutes.
    """

    def __init__(self, start: Optional[datetime] = None, speed: float = 240.0):
        self.speed = float(speed)
        if start is None:
            start = now_ist()
            if is_weekend(start) or start.time() >= SESSION_CLOSE:
                start = self._next_session_open(start)
            elif start.time() < SESSION_OPEN:
                start = start.replace(hour=9, minute=15, second=0, microsecond=0)
        self._start_sim = start
        self._start_real = time.monotonic()
        log.info("SimClock started at %s (speed %.0fx)", start, self.speed)

    @staticmethod
    def _next_session_open(dt: datetime) -> datetime:
        d = dt.date()
        if dt.time() >= SESSION_CLOSE:
            d = d + timedelta(days=1)
        while is_weekend(d):
            d += timedelta(days=1)
        return datetime(d.year, d.month, d.day, 9, 15, 0, tzinfo=IST)

    def now(self) -> datetime:
        sim = self._start_sim + timedelta(
            seconds=(time.monotonic() - self._start_real) * self.speed
        )
        # compress closed periods (pre-open, post-close, weekends)
        for _ in range(30):
            t = sim.time()
            if is_weekend(sim):
                sim = self._next_session_open(sim)
                continue
            if t < SESSION_OPEN:
                sim = sim.replace(hour=9, minute=15, second=0, microsecond=0)
                continue
            if t >= SESSION_CLOSE:
                sim = self._next_session_open(sim)
                continue
            return sim
        return sim


# ===========================================================================
def filter_configured_universe(underlyings: list[str], allowed: list[str]) -> tuple[list[str], list[str]]:
    """Restrict the F&O stock universe to the configured liquid-stock list
    (market_data.universe_stocks). Case-insensitive, preserves master order.
    Returns (kept, skipped_not_in_master). Empty `allowed` = no restriction."""
    if not allowed:
        return list(underlyings), []
    allowed_set = {str(a).strip().upper() for a in allowed if str(a).strip()}
    present = {u.upper() for u in underlyings}
    kept = [u for u in underlyings if u.upper() in allowed_set]
    skipped = [str(a).strip() for a in allowed if str(a).strip() and str(a).strip().upper() not in present]
    return kept, skipped


class TraderApp:
    def __init__(self, cfg: dict, live_confirmed: bool = False):
        self.cfg = cfg
        self.mode = cfg["trading_mode"]
        self.loop_tick = float(cfg_get(cfg, "market_data.loop_tick_seconds", 1.0))
        self.interval = int(cfg_get(cfg, "strategy.candle_interval_minutes", 15))
        self.boundary_grace = float(cfg_get(cfg, "market_data.boundary_grace_seconds", 8))

        # ---- storage
        db_path = cfg_get(cfg, "storage.db_path", "data/trader.db")
        self.db = Database(db_path)
        self.journal = Journal(self.db)
        self.positions = PositionManager(self.db)
        self.avwap = AvwapStore(self.db)
        self.risk = RiskManager(self.db, cfg)

        # ---- feed + clock + broker
        source = cfg_get(cfg, "market_data.source", "dhan")
        self.source = source
        self.rest = None
        self.md = None
        self.orders_api = None
        self.chain_client: Optional[PacedChainClient] = None
        self.underlying_ids: dict[str, str] = {}
        self.tick_sizes: dict[str, float] = {}
        self.master_contracts: list[OptionContract] = []

        if source == "mock":
            speed = float(cfg_get(cfg, "market_data.mock.speed", 240))
            self.clock = SimClock(speed=speed)
            self.feed = MockFeed(cfg, clock=self.clock)
            log.info("Using MOCK market feed (development only)")
        else:
            self.clock = None
            rest = DhanREST(
                cfg_get(cfg, "dhan.client_id"),
                cfg_get(cfg, "dhan.access_token"),
                base_url=cfg_get(cfg, "dhan.rest_base_url"),
                timeout=float(cfg_get(cfg, "dhan.http_timeout_seconds", 10)),
            )
            self.rest = rest
            self.md = DhanMarketData(
                rest,
                quote_batch_size=int(cfg_get(cfg, "market_data.quote_batch_size", 1000)),
            )
            self.feed = DhanCandlePollFeed(self.md, rest, cfg)
            self.orders_api = DhanOrders(rest)
            self.chain_client = PacedChainClient(rest)

        self.broker: Broker = self._make_broker(live_confirmed)

        # ---- market / strategy
        self.candles = CandleEngine(self.interval)
        self.universe = UniverseManager(
            it_count=int(cfg_get(cfg, "strategy.itm_strikes_per_side", 4))
        )
        self.engine = SignalEngine(
            self.db, self.avwap, self.positions, self.journal,
            on_signal=self._on_signal,
            now_fn=self.now,
        )

        # ---- runtime state
        self.ltp: dict[str, float] = {}
        self._ltp_lock = threading.Lock()
        self._processed: dict[str, int] = {}      # sec_id -> last processed window start
        self._pending_fills: list[dict] = []      # paper next_quote fills awaiting LTP
        self._pending_fill_lock = threading.Lock()
        self.started_at = int(time.time())
        self.last_update_ts = 0
        self.last_error: Optional[str] = None
        self._last_universe_refresh = 0.0
        self._last_order_poll = 0.0
        self._last_ltp_poll = 0.0
        self._last_ltp_warn = 0.0
        self._last_ltp_ok = 0.0
        self._day_processed: dict[str, set] = {}
        self._stop = threading.Event()
        # startup progress (shown by the dashboard while bootstrapping)
        self.startup: dict = {"phase": "starting", "detail": "", "progress": 0, "total": 0}
        # ---- V2 observability state (dashboard DATA + OPERATIONS zones)
        self._started_mono = time.monotonic()
        self.startup_timeline: list[dict] = []     # timestamped bootstrap phases
        self._feeds: dict[str, dict] = {           # last-ok/fail + errors per feed
            name: {"last_ok": 0, "last_fail": 0, "errors": 0, "last_latency_ms": 0.0}
            for name in ("ltp", "chain", "candles", "master")
        }
        self._feed_errors: dict[str, dict] = {}    # sec -> {ts, err} (recent)
        self._last_vol: dict[str, int] = {}        # sec -> last closed-candle volume
        self._oi: dict[tuple, int] = {}            # (u, expiry, strike, CE|PE) -> OI
        self._oi_ts = 0
        self._last_candle_ts = 0                   # newest processed candle (any sec)

    # ------------------------------------------------------------------ now
    def now(self) -> datetime:
        return self.feed.now()

    def _make_broker(self, live_confirmed: bool) -> Broker:
        if self.mode == "PAPER":
            return PaperBroker(
                self.db,
                self.positions,
                self.journal,
                fill_mode=cfg_get(self.cfg, "paper.fill_mode", "candle_close"),
                slippage_bps=float(cfg_get(self.cfg, "paper.slippage_bps", 0)),
            )
        # LIVE
        if self.rest is None:
            raise RuntimeError("LIVE mode requires Dhan credentials")
        return DhanBroker(
            self.rest,
            self.orders_api,
            self.db,
            self.positions,
            self.journal,
            product_type=cfg_get(self.cfg, "live.product_type", "MARGIN"),
            order_type=cfg_get(self.cfg, "live.order_type", "MARKET"),
            limit_offset_ticks=int(cfg_get(self.cfg, "live.limit_price_offset_ticks", 1)),
            live_confirmed=live_confirmed,
        )

    # ------------------------------------------------------------- bootstrap
    def _startup(self, phase: str, detail: str, progress: int = 0, total: int = 0) -> None:
        self.startup = {"phase": phase, "detail": detail, "progress": progress, "total": total}
        # V2: keep a timestamped timeline of the bootstrap (Recovery Center).
        # The per-underlying loop calls this ~69x; replace the previous
        # per-underlying entry instead of appending (the page stays readable)
        # while keeping every other phase verbatim.
        if phase == "bootstrapping" and detail.startswith("universe "):
            for i, e in enumerate(reversed(self.startup_timeline)):
                if e["phase"] == "bootstrapping" and e["detail"].startswith("universe "):
                    del self.startup_timeline[len(self.startup_timeline) - 1 - i]
                    break
        self.startup_timeline.append(
            {"ts": int(time.time()), "phase": phase, "detail": detail,
             "progress": progress, "total": total}
        )
        if len(self.startup_timeline) > 200:
            self.startup_timeline = self.startup_timeline[-200:]

    # ------------------------------------------------- V2 feed health
    def _touch_feed(self, name: str, ok: bool, latency_ms: float = 0.0,
                    err: str = "") -> None:
        f = self._feeds.get(name)
        if f is None:
            return
        ts = int(time.time())
        if ok:
            f["last_ok"] = ts
            if latency_ms:
                f["last_latency_ms"] = latency_ms
        else:
            f["last_fail"] = ts
            f["errors"] += 1

    def _capture_oi(self, u: str, expiry: str, chain) -> None:
        """V2: remember the latest per-strike OI/volume from each chain fetch
        (bootstrap + hourly universe refresh). The scanner shows it as a
        snapshot with its age - Dhan's chain rate limit (1 req/3 s) rules out
        per-contract live OI polling for ~700 contracts."""
        for side, node_map in (("CE", chain.ce), ("PE", chain.pe)):
            for strike, opt in node_map.items():
                self._oi[(u, expiry, float(strike), side)] = int(opt.oi or 0)
        self._oi_ts = int(time.time())

    def bootstrap(self) -> None:
        log.info("=== BOOTSTRAP (mode=%s, source=%s) ===", self.mode, self.source)
        self._startup("bootstrapping", "restoring open positions")
        # open positions from a previous run join the monitoring universe
        for p in self.positions.get_open():
            self.universe.add_position_id(p["security_id"])
            contract = self.engine.contract_for(p["security_id"])
            if contract is None:
                self._adopt_position_contract(p)

        self._startup("bootstrapping", "loading instrument master")
        if self.source == "mock":
            self._bootstrap_mock()
        else:
            self._bootstrap_dhan()

        # V2: AVWAP rebuilds scheduled from the dashboard (Data Health page)
        # are applied NOW - the engine is not running yet, so rebuilding is
        # safe. Dropped states are re-initialized (re-anchored) below.
        self._process_rebuild_queue()
        # AVWAP initialization for every monitored contract (spec §15)
        self._startup("bootstrapping", "building AVWAP history")
        self._init_avwap_for_monitored()
        self._seed_processed_from_db()
        # V2: persist the completed startup timeline (Recovery Center shows
        # the LAST startup even after a restart, before the new one finishes)
        try:
            self.db.kv_set("startup_timeline", json.dumps(self.startup_timeline))
        except Exception:
            log.warning("startup timeline persist failed", exc_info=True)
        self._startup("running", "ready")
        log.info("=== BOOTSTRAP DONE: %d underlyings, %d monitored contracts ===",
                 len(self.universe.by_underlying), len(self.universe.monitored_ids()))

    def _adopt_position_contract(self, p: dict) -> None:
        """Rebuild a minimal OptionContract for a restored position when the
        contract is no longer in the scanner universe (spec §7, §39)."""
        try:
            strike = float(p["strike"]) if p.get("strike") else 0.0
        except (TypeError, ValueError):
            strike = 0.0
        c = OptionContract(
            security_id=p["security_id"],
            symbol=p.get("symbol") or p["security_id"],
            underlying=p.get("underlying") or "",
            strike=strike,
            option_type=p.get("option_type") or "",
            expiry=p.get("expiry") or "",
            lot_size=int(cfg_get(self.cfg, "risk.default_lot_size", 250)),
        )
        self.engine.register_contract(c)
        log.info("Adopted contract for open position: %s", c.name)

    def _bootstrap_mock(self) -> None:
        self.feed.initialize()
        now = self.now()
        today = now.date()
        expiry = date.fromisoformat(
            self.feed.monthly_expiry(today).isoformat()
        )
        for u in self.feed.underlyings:
            strikes = self.feed.strikes_for(u)
            contracts_by_key = {}
            for sid, c in self.feed.contracts.items():
                if c.underlying == u:
                    contracts_by_key[(c.strike, c.option_type)] = c
            self.universe.update(u, expiry, self.feed.spot_for(u), strikes,
                                 contracts_by_key)
            self.engine.register_contracts(
                [c for c in contracts_by_key.values()]
            )

    def _bootstrap_dhan(self) -> None:
        cache_dir = cfg_get(self.cfg, "storage.instrument_cache_dir", "data/instruments")
        ttl = float(cfg_get(self.cfg, "dhan.instrument_master_ttl_hours", 12))
        default_lot = int(cfg_get(self.cfg, "risk.default_lot_size", 250))
        _t0 = time.perf_counter()
        try:
            bundle = load_master_bundle(self.rest, cache_dir, ttl, default_lot)
        except Exception:
            self._touch_feed("master", False)
            raise
        self._touch_feed("master", True, (time.perf_counter() - _t0) * 1000)
        self.master_contracts = bundle.contracts
        self.underlying_ids = bundle.underlying_ids
        self.tick_sizes = bundle.tick_sizes
        # tell the feed which contracts are index options (candle API needs
        # instrument=OPTIDX for them, OPTSTK for stock options)
        self.feed.instrument_map = {
            c.security_id: c.instrument
            for c in bundle.contracts
            if c.instrument != "OPTSTK"
        }

        underlyings = fo_stock_universe(self.master_contracts)
        configured = cfg_get(self.cfg, "market_data.universe_stocks", None)
        if configured is not None:
            if len(configured) == 0:
                underlyings, skipped = [], []
                log.info("Universe stocks explicitly cleared (empty list in config).")
            else:
                underlyings, skipped = filter_configured_universe(underlyings, configured)
                log.info(
                    "Universe restricted to %d configured liquid stocks "
                    "(%d of %d configured symbols found in the live F&O master)",
                    len(underlyings), len(underlyings), len(configured),
                )
                if skipped:
                    log.warning(
                        "universe_stocks: not in live F&O master, skipped: %s", skipped
                    )
        log.info("F&O stock universe: %d stocks", len(underlyings))

        today = self.now().date()
        switch_day = int(cfg_get(self.cfg, "strategy.expiry_switch_day", 24))
        # group master contracts by (underlying, expiry)
        master_index: dict[tuple, dict] = {}
        for c in self.master_contracts:
            master_index.setdefault((c.underlying, c.expiry), {})[
                (c.strike, c.option_type)
            ] = c

        ok = 0
        for i, u in enumerate(underlyings, 1):
            self._startup("bootstrapping", f"universe {i}/{len(underlyings)} · {u}",
                          i, len(underlyings))
            try:
                self._bootstrap_underlying_dhan(
                    u, today, switch_day, master_index
                )
                ok += 1
            except Exception as e:
                # An auth failure (HTTP 401 / Dhan error 808) will hit EVERY
                # remaining underlying - fail fast with an actionable message
                # instead of marching through 69 identical errors.
                if isinstance(e, DhanAPIError) and e.status == 401:
                    raise RuntimeError(
                        "Dhan authentication failed (HTTP 401, error 808): the "
                        "access token is expired or invalid. Tokens expire after "
                        "a few days - generate a new one at dhan.co (My Profile "
                        "-> Manage Access Token -> Generate Token), put it in "
                        "config/config.json under dhan.access_token "
                        "(or set DHAN_ACCESS_TOKEN), then restart."
                    ) from e
                # error isolation (spec §38): one bad stock must not stop the rest
                log.error("Bootstrap failed for %s: %s (continuing)", u, e)
            if i % 20 == 0 or i == len(underlyings):
                log.info("Bootstrap progress: %d/%d underlyings", i, len(underlyings))
        if ok == 0:
            raise RuntimeError("Bootstrap failed for all underlyings; aborting")

    def _expiry_legs_for(
        self, u: str, today: date, switch_day: int, expiries: list[date]
    ) -> list[date]:
        """Expiry legs to trade for an underlying - single monthly expiry with
        the 24th premium-preservation switch."""
        m = select_monthly_expiry(today, expiries, switch_day)
        return [m] if m else []

    def _bootstrap_underlying_dhan(
        self, u: str, today: date, switch_day: int, master_index: dict
    ) -> None:
        uid = self.underlying_ids.get(u)
        if not uid:
            log.warning("No NSE_EQ security id for %s; skipping", u)
            return
        _t0 = time.perf_counter()
        try:
            expiries = self.chain_client.expiry_list(int(uid))
        except Exception as e:
            self._touch_feed("chain", False, err=str(e))
            raise
        if not expiries:
            log.warning("No expiries for %s; skipping", u)
            return
        legs = self._expiry_legs_for(u, today, switch_day, expiries)
        if not legs:
            log.warning("No selectable expiry for %s; skipping", u)
            return
        self._touch_feed("chain", True, (time.perf_counter() - _t0) * 1000)

        for expiry in legs:
            _t0 = time.perf_counter()
            try:
                chain = self.chain_client.chain(int(uid), str(expiry))
            except Exception as e:
                self._touch_feed("chain", False, err=str(e))
                raise
            self._touch_feed("chain", True, (time.perf_counter() - _t0) * 1000)
            if chain.spot is None:
                log.warning("Option chain for %s %s has no spot; skipping leg", u, expiry)
                continue
            # V2: capture per-strike OI / volume from the chain snapshot
            self._capture_oi(u, str(expiry), chain)
            # contract metadata: master is authoritative; fill gaps from chain
            contracts_by_key = dict(master_index.get((u, str(expiry)), {}))
            instr = "OPTSTK" if is_stock_underlying(u) else "OPTIDX"
            for strike, opt in chain.ce.items():
                key = (strike, "CE")
                if key not in contracts_by_key:
                    contracts_by_key[key] = OptionContract(
                        security_id=opt.security_id,
                        symbol=f"{u} {strike:g} CE",
                        underlying=u, strike=strike, option_type="CE",
                        expiry=str(expiry), lot_size=int(cfg_get(self.cfg, "risk.default_lot_size", 250)),
                        instrument=instr,
                    )
                    log.warning("Master missing %s %s; using chain security_id=%s",
                                key, expiry, opt.security_id)
            for strike, opt in chain.pe.items():
                key = (strike, "PE")
                if key not in contracts_by_key:
                    contracts_by_key[key] = OptionContract(
                        security_id=opt.security_id,
                        symbol=f"{u} {strike:g} PE",
                        underlying=u, strike=strike, option_type="PE",
                        expiry=str(expiry), lot_size=int(cfg_get(self.cfg, "risk.default_lot_size", 250)),
                        instrument=instr,
                    )
                    log.warning("Master missing %s %s; using chain security_id=%s",
                                key, expiry, opt.security_id)
            self.universe.update(u, expiry, chain.spot, chain.strikes, contracts_by_key)
            self.engine.register_contracts(list(contracts_by_key.values()))
        # drop legs that fell out of the selection (weekly roll / 24th switch)
        removed = self.universe.drop_expiries(u, {str(e) for e in legs})
        if removed:
            log.info("Universe %s: dropped stale legs %s (open positions on them "
                     "remain monitored)", u, removed)
        uinfo = self.universe.get(u)
        log.info("Universe %s: legs=%s spot=%s",
                 u, [str(e) for e in legs], uinfo.spot if uinfo else None)

    # ---------------------------------------------------------- AVWAP init
    def _init_avwap_for_monitored(self) -> None:
        # NOTE: intentionally NOT gated on session OPEN - this pass only folds
        # in COMPLETED historical candles (month start to now), which the Dhan
        # intraday API serves 24/7. Starting the app pre-open (the recommended
        # ~15-20 min before 09:15) or overnight must still build the correct
        # month-anchored AVWAP state; skipping it would silently re-anchor
        # every contract on the first live candle of the day the app happens
        # to be running, violating the anchor rule.
        now = self.now()
        monitored = sorted(self.universe.monitored_ids())
        # backfill the symbol column for states created before it existed
        for sec in monitored:
            st = self.db.get_avwap_state(sec)
            if st is not None and not st["symbol"]:
                c = self.engine.contract_for(sec)
                if c is not None:
                    self.db._exec(
                        "UPDATE avwap_state SET symbol=? WHERE security_id=?",
                        (c.name, sec))
        from_dt = self._history_from_dt(now)
        need = [
            s for s in monitored
            if self.db.get_avwap_state(s) is None
        ]
        # States that exist but may have MISSED candles: the app was stopped
        # across a whole trading day (last candle predates the most recent
        # fully-elapsed session). Same-day downtime is fine - the live
        # catch-up pass refetches today's windows. Multi-day downtime is not:
        # every missed candle is a permanent divergence from a
        # chart-anchored VWAP, so we backfill them here (bootstrap is the
        # only place that happens - it is outside live trading).
        stale: list[tuple[str, int]] = []
        last_ended = self._last_ended_session_candle_ts(now)
        if last_ended:
            for s in monitored:
                st = self.db.get_avwap_state(s)
                if (st and st["last_candle_ts"]
                        and int(st["last_candle_ts"]) < last_ended):
                    stale.append((s, int(st["last_candle_ts"])))
        log.info("AVWAP initialization needed for %d/%d contracts (%d stale "
                 "state(s) to backfill)", len(need), len(monitored), len(stale))
        self._startup("bootstrapping", f"AVWAP history 0/{len(need)}", 0, len(need))
        hist_gap = float(cfg_get(self.cfg, "market_data.history_request_gap_seconds", 1.0))
        partial = set(self.db.kv_get("avwap_partial_history", []) or [])
        done = 0
        for i, sec in enumerate(need):
            c = self.engine.contract_for(sec)
            sym = c.name if c else sec
            self._startup("bootstrapping", f"AVWAP history {done}/{len(need)} · {sym}",
                          done, len(need))
            if i > 0 and hist_gap > 0:
                # Respect Dhan's rate limits. A 429 storm that exhausts retries
                # makes history_for fall back to a SHORT window, which silently
                # re-anchors the contract's AVWAP to a late date.
                time.sleep(hist_gap)
            try:
                history, full_range = self.feed.history_for(sec, from_dt, to_dt=now)
                if history:
                    # drop the in-progress candle (if any) - only completed candles
                    cur_start = candle_start_for(now, self.interval)
                    if cur_start is not None:
                        history = [x for x in history if x.ts < epoch(cur_start)]
                    if history:
                        self.avwap.initialize_from_candles(sec, history, symbol=sym)
                        done += 1
                    if not full_range:
                        partial.add(sec)
                        log.error("AVWAP for %s anchored from PARTIAL history (first "
                                  "candle %s, wanted %s) - flagged for re-anchor via "
                                  "tools/reanchor_avwap.py",
                                  sym,
                                  from_epoch(history[0].ts).strftime("%Y-%m-%d %H:%M"),
                                  from_dt.strftime("%Y-%m-%d"))
                else:
                    partial.add(sec)
                    log.error("AVWAP init: no history at all for %s - state stays "
                              "empty, will retry on next start", sym)
            except Exception as e:
                partial.add(sec)
                log.warning("AVWAP init failed for %s: %s", sec, e)
        if stale:
            self._backfill_avwap_gaps(now, stale, partial, hist_gap)
        self.db.kv_set("avwap_partial_history", sorted(partial))
        self._startup("bootstrapping", f"AVWAP history {done}/{len(need)}", done, len(need))
        log.info("AVWAP initialization complete: %d contracts (%d flagged PARTIAL history)",
                 done, len(partial))

    def _history_from_dt(self, now: datetime) -> datetime:
        if self.source == "mock":
            # the mock generates history from the first trading day of the month
            return now.replace(day=1, hour=9, minute=0, second=0)
        # Fetch 90 days (~3 months) so the AVWAP is anchored from the option's
        # born date. The API will simply return data starting from its creation.
        return (now - timedelta(days=90)).replace(hour=9, minute=0, second=0)

    def _last_ended_session_candle_ts(self, now: datetime) -> Optional[int]:
        """ts of the LAST candle of the most recent FULLY-ENDED session.

        Used to decide whether an existing AVWAP state can be missing
        candles. Holiday-agnostic on purpose: if the "previous session" was
        actually an NSE holiday the backfill fetch simply returns nothing
        new (a no-op)."""
        d = now.date()
        if is_weekend(now) or now.time() < SESSION_CLOSE:
            d = d - timedelta(days=1)  # today's session has not fully ended yet
        while is_weekend(d):
            d = d - timedelta(days=1)
        mins = SESSION_CLOSE.hour * 60 + SESSION_CLOSE.minute - self.interval
        h, m = divmod(mins, 60)
        return epoch(datetime(d.year, d.month, d.day, h, m, tzinfo=IST))

    def _backfill_avwap_gaps(self, now: datetime,
                             stale: list[tuple[str, int]],
                             partial: set, hist_gap: float) -> None:
        """Fold missed candles into existing AVWAP states (app was down
        across a trading day). Merge-only: initialize_from_candles never
        double-counts, and the anchor is untouched. Only clears the PARTIAL
        flag when the gap fetch served the full requested range."""
        n = len(stale)
        self._startup("bootstrapping", f"AVWAP gap backfill 0/{n}", 0, n)
        repaired = 0
        for i, (sec, last_ts) in enumerate(stale):
            c = self.engine.contract_for(sec)
            sym = c.name if c else sec
            self._startup("bootstrapping", f"AVWAP gap backfill {i}/{n} · {sym}", i, n)
            if i > 0 and hist_gap > 0:
                time.sleep(hist_gap)
            from_dt = max(self._history_from_dt(now), from_epoch(last_ts))
            try:
                history, full_range = self.feed.history_for(sec, from_dt, to_dt=now)
            except Exception as e:
                log.warning("AVWAP gap backfill fetch failed for %s: %s", sym, e)
                continue
            if not history:
                continue
            cur_start = candle_start_for(now, self.interval)
            if cur_start is not None:  # never fold the in-progress candle
                history = [x for x in history if x.ts < epoch(cur_start)]
            new_n = sum(1 for x in history if x.ts > last_ts)
            if not new_n:
                continue  # e.g. the "missed" day was a holiday - no candles
            self.avwap.initialize_from_candles(sec, history, symbol=sym)
            if full_range:
                partial.discard(sec)
            repaired += 1
            log.info("AVWAP gap backfill %s: +%d missed candle(s) folded in "
                     "(state was at %s)%s",
                     sym, new_n, from_epoch(last_ts).strftime("%Y-%m-%d %H:%M"),
                     "" if full_range else " [gap fetch was PARTIAL - still flagged]")
        self._startup("bootstrapping", f"AVWAP gap backfill done ({repaired} repaired)",
                      n, n)

    def _seed_processed_from_db(self) -> None:
        """Remember which candles are already processed. A previous day's
        candles must NOT suppress today's processing (each session is a new
        pass; AVWAP accumulation stays idempotent via the state's last ts)."""
        today = self.now().date()
        for sec in self.universe.monitored_ids():
            st = self.db.get_avwap_state(sec)
            last = int(st["last_candle_ts"] or 0) if st else 0
            if last and from_epoch(last).date() != today:
                last = 0
            self._processed[sec] = last

    # ----------------------------------------------------------------- main
    def run(self, stop_after: Optional[float] = None) -> None:
        self.bootstrap()
        if self.mode == "LIVE" and self.source == "dhan":
            self.broker.reconcile_positions()
        self.journal.write("APP_START", ts=int(time.time()),
                           detail={"mode": self.mode, "source": self.source,
                                   "open_positions": len(self.positions.get_open()),
                                   "monitored_contracts": len(self.universe.monitored_ids())})
        log.info("=== TRADING ENGINE RUNNING (mode=%s) ===", self.mode)

        started_real = time.monotonic()
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as e:
                self.last_error = f"{e.__class__.__name__}: {e}"
                log.exception("Main loop error: %s", e)
            self.last_update_ts = int(time.time())
            if stop_after is not None and (time.monotonic() - started_real) >= stop_after:
                log.info("stop_after reached; shutting down")
                break
            elapsed = time.monotonic() - t0
            sleep_s = max(0.05, self.loop_tick - elapsed)
            self._stop.wait(sleep_s)

        self.journal.write("APP_STOP", ts=int(time.time()))
        log.info("Trading engine stopped")

    def stop(self) -> None:
        self._stop.set()

    # ----------------------------------------------------------------- tick
    def _tick(self) -> None:
        now = self.now()
        state = session_state(now)

        if state == "OPEN":
            # Intraday Exits at 15:15 PM
            if now.time() >= dtime(15, 15) and not getattr(self, '_intraday_exited', False):
                log.info("Intraday time reached (15:15). Closing all positions.")
                self.journal.write("INTRADAY_EXIT", ts=int(time.time()), detail={"note": "15:15 achieved"})
                self.exit_all_positions(reason="INTRADAY_EXIT_15_15")
                self.set_disable_entries(True)
                self._intraday_exited = True
            elif now.time() < dtime(15, 15):
                if getattr(self, '_intraday_exited', False):
                    self.set_disable_entries(False)
                    self._intraday_exited = False

            self._process_closed_windows(now)
            if self.source == "mock":
                self.feed.step()

        self._maybe_refresh_universe(now)
        self._poll_ltp(now)
        self._process_pending_fills(now)

        if self.mode == "LIVE" and self.source == "dhan":
            poll_every = float(cfg_get(self.cfg, "live.order_poll_seconds", 3))
            if time.time() - self._last_order_poll >= poll_every:
                self._last_order_poll = time.time()
                self.broker.poll_fills()

        # keep the position universe in sync (cheap, every few ticks)
        self._sync_position_universe()

    def _ensure_processed(self, sec: str) -> int:
        """Lazily seed the processed-window marker from persisted AVWAP state
        (for contracts that join the monitored set mid-day, e.g. after an ATM
        move). Candles already consumed by the AVWAP state are never
        re-processed - this is the second layer of duplicate protection."""
        if sec in self._processed:
            return self._processed[sec]
        st = self.db.get_avwap_state(sec)
        last = int(st["last_candle_ts"] or 0) if st else 0
        if last and from_epoch(last).date() != self.now().date():
            last = 0
        self._processed[sec] = last
        return last

    def _process_closed_windows(self, now: datetime) -> None:
        last_closed = last_closed_window_start(now, self.interval)
        if last_closed is None:
            return
        last_ts = epoch(last_closed)
        # require a small grace so candle APIs can publish the fresh candle
        if (now - candle_end_for(last_closed, self.interval)).total_seconds() < self.boundary_grace:
            return
        monitored = sorted(self.universe.monitored_ids())
        first_ts = epoch(last_closed.replace(hour=9, minute=15, second=0, microsecond=0))
        for sec in monitored:
            processed = self._ensure_processed(sec)
            if last_ts <= processed:
                continue
            # catch-up windows (oldest first), capped to avoid floods after downtime
            windows: list[int] = []
            w = last_ts
            while w > processed and w >= first_ts and len(windows) < 26:
                windows.append(w)
                w -= self.interval * 60
            windows.reverse()
            for win_ts in windows:
                # safety: never re-evaluate a candle the AVWAP state already
                # consumed (restart / contract joined the universe mid-day)
                st = self.db.get_avwap_state(sec)
                consumed = int(st["last_candle_ts"] or 0) if st else 0
                if consumed and win_ts <= consumed:
                    self._processed[sec] = max(self._processed.get(sec, 0), win_ts)
                    continue
                try:
                    candle = self.feed.closed_candle(sec, win_ts)
                except Exception as e:
                    log.warning("closed_candle(%s, %s) error: %s", sec, win_ts, e)
                    # V2: record the per-contract feed error (scanner shows
                    # an ERROR state; Data Health aggregates the feed)
                    self._feed_errors[sec] = {"ts": int(time.time()), "err": str(e)[:200]}
                    self._touch_feed("candles", False, err=str(e))
                    continue
                if candle is None:
                    log.debug("no candle for %s win=%s yet", sec, win_ts)
                    continue
                c = self.candles.ingest_closed(candle)
                if c is None:
                    continue
                self.engine.on_candle(c)
                self._persist_candle(sec, c)
                self._touch_feed("candles", True)
                self._processed[sec] = win_ts

    def _maybe_refresh_universe(self, now: datetime) -> None:
        refresh_min = float(cfg_get(self.cfg, "market_data.universe_refresh_minutes", 60))
        if time.time() - self._last_universe_refresh < refresh_min * 60:
            return
        self._last_universe_refresh = time.time()
        if self.source == "mock":
            for u in self.feed.underlyings:
                self.universe.refresh_atm(u, self.feed.spot_for(u))
            return
        if self.source == "dhan":
            # refresh in the background so the loop never blocks on the
            # 1-request-per-3s chain rate limit
            threading.Thread(target=self._refresh_universe_dhan, daemon=True,
                             name="universe-refresh").start()

    def _refresh_universe_dhan(self) -> None:
        try:
            today = self.now().date()
            switch_day = int(cfg_get(self.cfg, "strategy.expiry_switch_day", 24))
            master_index: dict[tuple, dict] = {}
            for c in self.master_contracts:
                master_index.setdefault((c.underlying, c.expiry), {})[
                    (c.strike, c.option_type)
                ] = c
            for u in list(self.universe.by_underlying.keys()):
                try:
                    self._bootstrap_underlying_dhan(u, today, switch_day, master_index)
                except Exception as e:
                    log.warning("Universe refresh failed for %s: %s", u, e)
                # dynamic ATM from fresh spot
                uinfo = self.universe.get(u)
                if uinfo and uinfo.spot:
                    self.universe.refresh_atm(u, uinfo.spot)
            log.info("Universe refresh complete: %d underlyings",
                     len(self.universe.by_underlying))
        except Exception:
            log.exception("Universe refresh crashed")

    # ------------------------------------------------------------------ LTP
    def _poll_ltp(self, now: datetime) -> None:
        if session_state(now) != "OPEN":
            return  # no LTP polling outside market hours (rate-limit hygiene)
        ltp_every = float(cfg_get(self.cfg, "market_data.ltp_poll_seconds", 30))
        if time.time() - self._last_ltp_poll < ltp_every:
            return
        self._last_ltp_poll = time.time()
        monitored = list(self.universe.monitored_ids())
        if monitored:
            _t0 = time.perf_counter()
            try:
                quotes = self.feed.poll_ltp(monitored)
                self._touch_feed("ltp", True, (time.perf_counter() - _t0) * 1000)
            except Exception as e:
                self._touch_feed("ltp", False, err=str(e))
                log.warning("LTP poll failed: %s", e)
                quotes = {}
            got = len(quotes)
            with self._ltp_lock:
                for sec, q in quotes.items():
                    self.ltp[sec] = q.price
            # An empty/partial quote response used to be SILENT (dashboard
            # LTP column stuck at "–" with nothing in the log). Log it.
            if got == 0:
                if time.time() - self._last_ltp_warn >= 300:
                    self._last_ltp_warn = time.time()
                    log.warning(
                        "LTP poll: Dhan quote API returned NO quotes for %d "
                        "monitored contracts - LTP column will show '–' "
                        "until this resolves", len(monitored))
            elif got < len(monitored):
                if time.time() - self._last_ltp_warn >= 300:
                    self._last_ltp_warn = time.time()
                    log.warning("LTP poll: only %d/%d monitored contracts "
                                "have quotes (LTP '–' for the rest)",
                                got, len(monitored))
            elif time.time() - self._last_ltp_ok >= 600:
                self._last_ltp_ok = time.time()
                log.info("LTP poll: %d/%d contracts quoted", got,
                         len(monitored))
        # DYNAMIC ATM (spec §7): track spot continuously between chain
        # refreshes so the scanner universe follows the underlying.
        if self.source == "mock":
            for u in self.feed.underlyings:
                self.universe.refresh_atm(u, self.feed.spot_for(u))
        elif self.source == "dhan" and self.md is not None:
            # STOCKS: live LTP from the quote API (30 s cadence).
            uids = [self.underlying_ids[u] for u in self.universe.by_underlying
                    if u in self.underlying_ids]
            if uids:
                try:
                    uq = self.md.quotes_segment(uids, "NSE_EQ")
                    for u, uid in self.underlying_ids.items():
                        q = uq.get(str(uid))
                        if q and u in self.universe.by_underlying:
                            self.universe.refresh_atm(u, q.price)
                except Exception as e:
                    log.warning("underlying spot poll failed: %s", e)

    def ltp_getter(self, security_id: str) -> Optional[float]:
        with self._ltp_lock:
            return self.ltp.get(security_id)

    def _sync_position_universe(self) -> None:
        open_ids = {p["security_id"] for p in self.positions.get_open()}
        current = self.universe.position_security_ids
        for sec in current - open_ids:
            self.universe.remove_position_id(sec)
        for sec in open_ids - current:
            self.universe.add_position_id(sec)
            if self.engine.contract_for(sec) is None:
                p = self.positions.find_by_security(sec)
                if p:
                    self._adopt_position_contract(p)

    # ------------------------------------------------------------ signals
    def _on_signal(self, sig) -> None:
        log.info("SIGNAL %s %s @ %.2f (avwap %s) reason=%s",
                 sig.action, sig.symbol, sig.signal_price,
                 f"{sig.avwap:.2f}" if sig.avwap else "n/a", sig.reason)
        if sig.action == ENTRY_SELL:
            self._handle_entry(sig)
        elif sig.action == EXIT_BUY:
            self._handle_exit(sig)

    def _risk_gate(self) -> list[str]:
        open_pos = self.positions.get_open()
        day = self.now().strftime("%Y-%m-%d")
        trades_today = self.db.trades_today_count(day)
        daily_pnl = self.risk.daily_pnl(day, open_pos, self.ltp_getter)
        return self.risk.entry_block_reasons(len(open_pos), trades_today, daily_pnl)

    def _handle_entry(self, sig) -> None:
        contract = self.engine.contract_for(sig.security_id)
        if contract is None:
            log.error("Entry signal for unknown contract %s; skipped", sig.security_id)
            return
        if self.positions.has_open(sig.security_id):
            return  # one short per contract (spec §44)
        reasons = self._risk_gate()
        if reasons:
            log.warning("ENTRY BLOCKED by risk controls %s %s: %s",
                        sig.symbol, contract.name, reasons)
            self.journal.write(
                "ENTRY_BLOCKED", ts=sig.created_at,
                security_id=sig.security_id, symbol=contract.name,
                detail={"reasons": reasons},
            )
            return
        qty = self.risk.quantity_for(contract)
        if self.mode == "PAPER":
            if self.broker.fill_mode == "next_quote":
                deadline = sig.created_at + int(cfg_get(self.cfg, "paper.next_quote_timeout_seconds", 45))
                with self._pending_fill_lock:
                    self._pending_fills.append({
                        "kind": "entry", "contract": contract,
                        "quantity": qty, "signal": sig, "deadline": deadline,
                    })
                return
            self.broker.execute_entry(contract, qty, sig)
        else:
            self.broker.execute_entry(contract, qty, sig)

    def _handle_exit(self, sig) -> None:
        pos = self.positions.find_by_security(sig.security_id)
        if pos is None:
            log.error("Exit signal with no open position: %s", sig.symbol)
            return
        if self.mode == "PAPER":
            if self.broker.fill_mode == "next_quote":
                deadline = sig.created_at + int(cfg_get(self.cfg, "paper.next_quote_timeout_seconds", 45))
                with self._pending_fill_lock:
                    self._pending_fills.append({
                        "kind": "exit", "position": pos,
                        "signal": sig, "deadline": deadline,
                    })
                return
            self.broker.execute_exit(pos, sig)
        else:
            self.broker.execute_exit(pos, sig)

    def _process_pending_fills(self, now: datetime) -> None:
        now_ts = epoch(now)
        with self._pending_fill_lock:
            due = [p for p in self._pending_fills
                   if self.ltp_getter(p["signal"].security_id) is not None
                   or now_ts >= p["deadline"]]
            for p in due:
                self._pending_fills.remove(p)
        for p in due:
            sec = p["signal"].security_id
            price = self.ltp_getter(sec)
            fallback = p["signal"].signal_price
            use = price if price is not None else fallback
            note = "quote" if price is not None else "fallback_candle_close"
            log.info("Paper fill (%s) for %s @ %.2f", note, p["signal"].symbol, use or -1)
            if p["kind"] == "entry":
                res = self.broker.execute_entry(p["contract"], p["quantity"],
                                                p["signal"], fill_price=use)
                if not res.is_filled:
                    log.error("Paper entry fill failed: %s", res.raw)
            else:
                res = self.broker.execute_exit(p["position"], p["signal"],
                                               fill_price=use)
                if not res.is_filled:
                    log.error("Paper exit fill failed: %s", res.raw)

    # ---------------------------------------------------------- emergency
    def exit_all_positions(self, reason: str = "MANUAL_EXIT_ALL") -> int:
        log.warning("EXIT ALL POSITIONS requested (%s)", reason)
        self.journal.write("EXIT_ALL_REQUESTED", ts=int(time.time()), detail={"reason": reason})
        if self.mode == "PAPER":
            n = 0
            for pos in self.positions.get_open():
                price = self.ltp_getter(pos["security_id"]) or pos.get("entry_price")
                res = self.broker.execute_exit(pos, None, fill_price=price, reason=reason)
                if res.is_filled:
                    n += 1
            return n
        return self.broker.exit_all(self.ltp_getter, reason)

    def set_emergency_stop(self, on: bool) -> None:
        self.risk.set_emergency_stop(on)

    def set_disable_entries(self, on: bool) -> None:
        self.risk.set_disable_entries(on)

    # ------------------------------------------------- V2 operational ops
    def _process_rebuild_queue(self) -> None:
        """Apply AVWAP rebuilds scheduled from the dashboard (Data Health).

        Runs at bootstrap, BEFORE AVWAP init and while the engine is idle, so
        rebuilding a contract's state is safe: delete the (possibly
        late-anchored) state and clear its PARTIAL flag; _init_avwap_for_
        monitored() then re-initializes it from the full history window."""
        queued = self.db.kv_get("avwap_rebuild_queue", []) or []
        if not queued:
            return
        self._startup("bootstrapping", f"applying {len(queued)} scheduled AVWAP rebuild(s)")
        for sec in list(dict.fromkeys(queued)):
            try:
                self.avwap.delete(sec)
                partial = set(self.db.kv_get("avwap_partial_history", []) or [])
                if sec in partial:
                    partial.discard(sec)
                    self.db.kv_set("avwap_partial_history", sorted(partial))
                self.journal.write(
                    "AVWAP_REBUILD_APPLIED", ts=int(time.time()), security_id=sec,
                    detail={"note": "state dropped at startup; re-initializing "
                                    "from full history window"},
                )
                log.info("AVWAP rebuild applied for %s (dashboard queue)", sec)
            except Exception as e:
                log.warning("AVWAP rebuild failed for %s: %s", sec, e)
        self.db.kv_set("avwap_rebuild_queue", [])

    def schedule_rebuild(self, security_id: str) -> str:
        """V2 (Data Health): queue a contract's AVWAP for rebuild at the NEXT
        application start. Deliberately NOT done live - the running engine's
        in-memory state must never be mutated out from under it."""
        queued = set(self.db.kv_get("avwap_rebuild_queue", []) or [])
        queued.add(security_id)
        self.db.kv_set("avwap_rebuild_queue", sorted(queued))
        c = self.engine.contract_for(security_id)
        self.journal.write(
            "AVWAP_REBUILD_SCHEDULED", ts=int(time.time()), security_id=security_id,
            symbol=c.name if c else security_id,
            detail={"note": "will be rebuilt from full history at next start"},
        )
        log.info("AVWAP rebuild scheduled for %s (applies at next start)", security_id)
        return "scheduled - applies at next application start"

    def close_position_manual(self, position_id: str) -> str:
        """V2 (Positions page): operational buy-to-close of one position.
        Uses the normal broker exit path (paper: virtual fill; live: real
        BUY order through the standard lifecycle) - never a bypass."""
        pos = self.db.get_position(position_id)
        if pos is None:
            return "position not found"
        pos = dict(pos)
        if pos["status"] not in ("OPEN", "PENDING"):
            return f"position is not open (status: {pos['status']})"
        price = self.ltp_getter(pos["security_id"]) or pos.get("entry_price")
        self.journal.write(
            "MANUAL_CLOSE_REQUESTED", ts=int(time.time()),
            position_id=position_id, security_id=pos["security_id"],
            symbol=pos["symbol"], detail={"price": price},
        )
        res = self.broker.execute_exit(pos, None, fill_price=price,
                                       reason="MANUAL_CLOSE")
        if res.is_filled:
            return f"closed (fill {res.status})"
        return f"exit order placed: {res.order_id} ({res.status})" if res.order_id \
            else f"exit failed: {res.raw}"

    def _next_candle_close_ts(self, now: datetime) -> int:
        """When the CURRENT (in-progress) 15-min candle will close."""
        base = now.replace(second=0, microsecond=0)
        offset = self.interval - (base.minute % self.interval)
        return int((base + timedelta(minutes=offset)).timestamp())

    def _persist_candle(self, sec: str, c) -> None:
        """V2: persist every completed candle (with the AVWAP the strategy
        computed INCLUDING this candle) so the dashboard can chart and replay
        history from the database instead of the live API alone."""
        try:
            st = self.db.get_avwap_state(sec)
            self.db.save_candle({
                "security_id": sec, "ts": c.ts,
                "open": c.open, "high": c.high, "low": c.low,
                "close": c.close, "volume": int(c.volume or 0),
                "avwap": st["last_avwap"] if st else None,
            })
            self._last_vol[sec] = int(c.volume or 0)
            if c.ts > self._last_candle_ts:
                self._last_candle_ts = c.ts
        except Exception:
            log.warning("candle persist failed for %s", sec, exc_info=True)

    # ------------------------------------------------------------- dashboard
    def state_for_dashboard(self) -> dict:
        now = self.now()
        state = session_state(now)
        bootstrapping = self.startup.get("phase") == "bootstrapping"

        # While bootstrapping the DB is busy building AVWAP history, so we do
        # NOT touch the DB at all - report startup progress with empty tables.
        # This guarantees /api/state can never block no matter what the engine
        # is doing (WAL or not, fast or slow machine, antivirus locking files).
        if bootstrapping:
            return {
                "mode": self.mode,
                "source": self.source,
                "market_state": state,
                "clock": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "dhan": ("connected" if self.source == "dhan" else "mock (no live data)"),
                "last_update": self.last_update_ts,
                "last_error": self.last_error,
                "started_at": self.started_at,
                "version": APP_VERSION,
                "uptime_s": int(time.monotonic() - self._started_mono),
                "startup": {
                    **self.startup,
                    "timeline": list(self.startup_timeline),
                    "prev_timeline": _safe_json_list(self.db.kv_get("startup_timeline")),
                },
                "universe": {
                    "underlyings": len(self.universe.by_underlying),
                    "monitored_contracts": len(self.universe.monitored_ids()),
                    "details": self.universe.info(),
                },
                "scanner": [],
                "positions": [],
                "closed_positions": [],
                "signals": [],
                "orders": [],
                "journal": [],
                "alerts": [],
                "indices": {},
                "feeds": {k: dict(v) for k, v in self._feeds.items()},
                "summary": {
                    "pnl_today": 0.0, "unrealized": 0.0, "realized_today": 0.0,
                    "realized_total": 0.0, "wins_today": 0, "losses_today": 0,
                    "orders_today": 0, "exposure": 0.0,
                },
                "system": {
                    "engine": {"last_tick": self.last_update_ts,
                                "last_error": self.last_error, "running": True},
                    "last_candle_ts": 0,
                    "next_candle_close": self._next_candle_close_ts(now),
                    "rest": self.rest.stats() if self.rest is not None else None,
                    "db": {"ok": True, "size_mb": None, "path": self.db.path},
                    "process": None,
                },
                "avwap_health": {"monitored": 0, "complete": 0, "partial": [],
                                  "queue": []},
                "risk": {
                    "emergency_stop": False,
                    "disable_new_entries": False,
                    "trades_today": 0,
                    "daily_pnl": 0.0,
                    "max_open_positions": self.cfg.get("risk", {}).get("max_open_positions"),
                    "max_trades_per_day": self.cfg.get("risk", {}).get("max_trades_per_day"),
                    "max_daily_loss": self.cfg.get("risk", {}).get("max_daily_loss"),
                },
            }

        open_pos = self.positions.get_open()
        open_by_sec = {p["security_id"]: p for p in open_pos}
        flags = self.risk.flags()
        day = now.strftime("%Y-%m-%d")

        # Full payload (engine is running, universe + AVWAP history are ready).
        scanner_rows = []
        partial_set = set(self.db.kv_get("avwap_partial_history", []) or [])
        for sec in sorted(self.universe.monitored_ids()):
            c = self.engine.contract_for(sec)
            st = self.db.get_avwap_state(sec)
            ltp = self.ltp_getter(sec)
            pos = open_by_sec.get(sec)
            last_sig = self.engine.last_signal_by_security.get(sec)
            if pos is not None:
                row_status = "PENDING" if pos["status"] == "PENDING" else "OPEN"
            elif last_sig is not None and (now.timestamp() - last_sig.candle_ts) < self.interval * 60:
                row_status = "ENTRY" if last_sig.action == ENTRY_SELL else "EXIT"
            else:
                row_status = "WATCH"
            dte = None
            try:
                dte = (date.fromisoformat(str(c.expiry)) - now.date()).days if c else None
            except ValueError:
                dte = None
            scanner_rows.append({
                "security_id": sec,
                "underlying": c.underlying if c else (pos or {}).get("underlying", ""),
                "symbol": c.symbol if c else (pos or {}).get("symbol", sec),
                "strike": c.strike if c else (pos or {}).get("strike"),
                "type": c.option_type if c else (pos or {}).get("option_type"),
                "expiry": c.expiry if c else (pos or {}).get("expiry"),
                "dte": dte,
                "ltp": ltp,
                "avwap": st["last_avwap"] if st else None,
                "last_close": st["last_close"] if st else None,
                # values the DASHBOARD uses for trigger-proximity display only
                # (strategy evaluation happens in the engine, not here):
                # for the NEXT candle, prev_close/prev_avwap = the state's
                # last_close/last_avwap (values after the last completed candle)
                "prev_close": st["last_close"] if st else None,
                "prev_avwap": st["last_avwap"] if st else None,
                "last_close_ts": st["last_candle_ts"] if st else None,
                # AVWAP anchor transparency: which candle the cumulative VWAP
                # starts from + whether its history window was incomplete
                # (a late anchor makes the AVWAP look "wrong" vs a chart that
                # is anchored at the contract's real first candle)
                "avwap_anchor_ts": st["anchor_ts"] if st else None,
                "hist_partial": sec in partial_set,
                "status": row_status,
                # V2: liquidity snapshot (OI from the latest chain fetch,
                # volume from the last closed candle) + recent feed error
                "oi": (self._oi.get((c.underlying, str(c.expiry), c.strike, c.option_type))
                       if c else None),
                "vol": self._last_vol.get(sec),
                "error": (self._feed_errors.get(sec) or {}).get("err"),
            })
        # prune stale per-contract feed errors (>30 min)
        _now_ts = int(time.time())
        self._feed_errors = {s: e for s, e in self._feed_errors.items()
                             if _now_ts - e["ts"] < 1800}

        pos_rows = []
        for p in open_pos:
            ltp = self.ltp_getter(p["security_id"])
            from portfolio.pnl import unrealized_pnl
            upnl = unrealized_pnl(p["entry_price"], ltp, p["quantity"])
            st_row = self.db.get_avwap_state(p["security_id"])
            st_now = dict(st_row) if st_row else None
            pos_rows.append({
                "position_id": p["position_id"],
                "security_id": p["security_id"],
                "underlying": p["underlying"],
                "option": p["symbol"],
                "expiry": p["expiry"],
                "strike": p["strike"],
                "type": p["option_type"],
                "quantity": p["quantity"],
                "entry": p["entry_price"],
                "current": ltp,
                "avwap": p["entry_avwap"],
                "avwap_now": st_now["last_avwap"] if st_now else None,
                "unrealized_pnl": upnl,
                "status": p["status"],
                "mode": p["mode"],
                "entry_time": p["entry_time"],
                "duration_s": (now.timestamp() - (p["entry_time"] or now.timestamp()))
                              if p.get("entry_time") else None,
                # DISPLAY-ONLY hint: the last COMPLETED candle closed above
                # the contract's AVWAP (the strategy's own exit condition)
                "exit_due": bool(st_now and st_now.get("last_close") is not None
                                 and st_now.get("last_avwap") is not None
                                 and st_now["last_close"] > st_now["last_avwap"]),
            })

        closed = [dict(r) for r in self.db.get_positions(status="CLOSED", limit=1000)]
        day_start_ts = int(now.replace(hour=0, minute=0, second=0,
                                       microsecond=0).timestamp())
        closed_today = [p for p in closed
                        if (p.get("exit_time") or 0) >= day_start_ts]

        # V2: enrich signals with their SIGNAL -> ORDER -> POSITION chain
        signals = []
        for s in [dict(r) for r in self.db.get_signals(limit=30)]:
            chain = {"order": None, "position": None, "blocked": None}
            o = self.db.find_order_for_security(s["security_id"],
                                                s["candle_ts"] - 120)
            if o is not None:
                chain["order"] = {"order_id": o["order_id"], "status": o["status"],
                                  "action": o["action"], "placed_at": o["placed_at"]}
            p = open_by_sec.get(s["security_id"])
            if p is not None:
                chain["position"] = {"status": p["status"],
                                     "entry_price": p["entry_price"],
                                     "entry_time": p["entry_time"]}
            if s["action"] == ENTRY_SELL:
                j = self.db.journal_find("ENTRY_BLOCKED", s["security_id"],
                                         s["candle_ts"] - 60, s["candle_ts"] + 900)
                if j is not None:
                    try:
                        chain["blocked"] = (json.loads(j["detail"] or "{}")
                                            .get("reasons") or [])
                    except (TypeError, ValueError):
                        chain["blocked"] = []
            s["chain"] = chain
            signals.append(s)

        # V2: order book for the Orders page
        orders_rows = []
        for o in self.db.get_orders(limit=100):
            c = self.engine.contract_for(o["security_id"])
            raw = o["raw"] or ""
            try:
                raw_d = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
            except (TypeError, ValueError):
                raw_d = {}
            orders_rows.append({
                "order_id": o["order_id"],
                "symbol": c.name if c else (o["security_id"] or ""),
                "security_id": o["security_id"],
                "action": o["action"],
                "side": o["side"],
                "quantity": o["quantity"],
                "status": o["status"],
                "filled_qty": o["filled_qty"],
                "avg_price": o["avg_price"],
                "placed_at": o["placed_at"],
                "raw_snippet": json.dumps(raw_d)[:200] if raw_d else "",
            })

        # ---- V2: summary numbers
        daily_pnl = self.risk.daily_pnl(day, open_pos, self.ltp_getter)
        wins_today = sum(1 for p in closed_today if (p.get("pnl") or 0) > 0)
        losses_today = sum(1 for p in closed_today if (p.get("pnl") or 0) < 0)
        exposure = sum((p.get("entry_price") or 0) * (p.get("quantity") or 0)
                       for p in open_pos)
        summary = {
            "pnl_today": daily_pnl,
            "unrealized": sum((r.get("unrealized_pnl") or 0) for r in pos_rows),
            "realized_today": self.db.realized_pnl_today(day),
            "realized_total": sum((p.get("pnl") or 0) for p in closed),
            "wins_today": wins_today,
            "losses_today": losses_today,
            "orders_today": self.db.orders_count_since(day_start_ts),
            "exposure": exposure,  # notional premium (PAPER: margin n/a)
        }

        # ---- V2: system health
        try:
            db_size_mb = round(os.path.getsize(self.db.path) / 1e6, 2)
        except OSError:
            db_size_mb = None
        process = None
        try:
            import psutil  # optional - System page degrades gracefully
            _p = psutil.Process()
            process = {
                "cpu_pct": _p.cpu_percent(None),
                "ram_mb": round(_p.memory_info().rss / 1e6, 1),
                "disk_pct": psutil.disk_usage(
                    os.path.dirname(os.path.abspath(self.db.path))).percent,
            }
        except Exception:
            process = None
        system = {
            "engine": {"last_tick": self.last_update_ts,
                        "last_error": self.last_error, "running": True},
            "last_candle_ts": self._last_candle_ts,
            "next_candle_close": self._next_candle_close_ts(now),
            "rest": self.rest.stats() if self.rest is not None else None,
            "db": {"ok": True, "size_mb": db_size_mb, "path": self.db.path},
            "process": process,
        }

        # ---- V2: AVWAP data health
        monitored = set(self.universe.monitored_ids())
        partial_rows = []
        for sec in sorted(partial_set & monitored):
            c = self.engine.contract_for(sec)
            st = self.db.get_avwap_state(sec)
            partial_rows.append({
                "security_id": sec,
                "symbol": c.name if c else sec,
                "anchor_ts": st["anchor_ts"] if st else None,
                "expiry": str(c.expiry) if c else None,
            })
        avwap_health = {
            "monitored": len(monitored),
            "complete": len(monitored) - len(partial_rows),
            "partial": partial_rows,
            "queue": self.db.kv_get("avwap_rebuild_queue", []) or [],
        }

        alerts = self._compute_alerts(state, open_pos, daily_pnl,
                                      partial_count=len(partial_rows))

        # ---- V2: read-only settings display (OPERATIONS zone).
        # Editable surface is deliberately EMPTY by design: anything touching
        # strategy/risk/execution/mode is config-file + restart only.
        settings = {
            "version": APP_VERSION,
            "trading_mode": self.mode,
            "strategy": {
                "candle_interval_minutes": self.interval,
                "itm_strikes_per_side": cfg_get(self.cfg, "strategy.itm_strikes_per_side", 4),
                "expiry_switch_day": cfg_get(self.cfg, "strategy.expiry_switch_day", 24),
            },
            "market_data": {
                "source": self.source,
                "ltp_poll_seconds": cfg_get(self.cfg, "market_data.ltp_poll_seconds", 30),
                "universe_refresh_minutes": cfg_get(self.cfg, "market_data.universe_refresh_minutes", 60),
                "loop_tick_seconds": cfg_get(self.cfg, "market_data.loop_tick_seconds", 1),
                "universe_stocks_count": len(cfg_get(self.cfg, "market_data.universe_stocks", []) or []),
                "history_start": cfg_get(self.cfg, "market_data.history_start", "month_start"),
            },
            "risk": {k: self.cfg.get("risk", {}).get(k) for k in
                     ("quantity_per_trade", "max_open_positions",
                      "max_trades_per_day", "max_daily_loss")},
            "paper": self.cfg.get("paper", {}),
            "live": self.cfg.get("live", {}),
            "storage": {"db_path": self.db.path},
        }

        return {
            "mode": self.mode,
            "source": self.source,
            "market_state": state,
            "clock": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "dhan": ("connected" if self.source == "dhan" else "mock (no live data)"),
            "last_update": self.last_update_ts,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "version": APP_VERSION,
            "uptime_s": int(time.monotonic() - self._started_mono),
            "startup": {
                **self.startup,
                "timeline": list(self.startup_timeline),
                # the PREVIOUS run's completed timeline (persisted at its
                # bootstrap end) - shown by the Recovery Center
                "prev_timeline": _safe_json_list(self.db.kv_get("startup_timeline")),
            },
            "universe": {
                "underlyings": len(self.universe.by_underlying),
                "monitored_contracts": len(self.universe.monitored_ids()),
                "details": self.universe.info(),
            },
            "scanner": scanner_rows,
            "positions": pos_rows,
            "closed_positions": closed,
            "signals": signals,
            "orders": orders_rows,
            "journal": self.journal.tail(limit=50),
            "alerts": alerts,
            "feeds": {k: dict(v) for k, v in self._feeds.items()},
            "summary": summary,
            "system": system,
            "avwap_health": avwap_health,
            "settings": settings,
            "oi_ts": self._oi_ts,
            "risk": {
                **flags,
                "trades_today": self.db.trades_today_count(day),
                "daily_pnl": daily_pnl,
                "max_open_positions": self.cfg.get("risk", {}).get("max_open_positions"),
                "max_trades_per_day": self.cfg.get("risk", {}).get("max_trades_per_day"),
                "max_daily_loss": self.cfg.get("risk", {}).get("max_daily_loss"),
            },
        }

    # ------------------------------------------------- V2 alerts
    def _compute_alerts(self, state: str, open_pos: list, daily_pnl: float,
                        partial_count: int = 0) -> list:
        """Derived alert list (Data + Operations zones). Computed, not stored
        - the journal below provides the persistent incident history."""
        out: list[dict] = []
        now_ts = int(time.time())

        def add(sev: str, msg: str) -> None:
            out.append({"severity": sev, "message": msg, "ts": now_ts})

        cfg_risk = self.cfg.get("risk", {})
        max_pos = cfg_risk.get("max_open_positions")
        max_day = cfg_risk.get("max_daily_loss")
        if state == "OPEN":
            ltp_f = self._feeds.get("ltp", {})
            ltp_poll = float(cfg_get(self.cfg, "market_data.ltp_poll_seconds", 30))
            if ltp_f.get("last_ok") and now_ts - ltp_f["last_ok"] > 3 * ltp_poll:
                add("WARNING", f"Quote feed stale: last successful LTP poll "
                               f"{now_ts - ltp_f['last_ok']}s ago")
            elif not ltp_f.get("last_ok"):
                add("WARNING", "No successful LTP poll yet this session")
            ch_f = self._feeds.get("chain", {})
            if ch_f.get("last_ok") and now_ts - ch_f["last_ok"] > 300:
                add("WARNING", f"Option chain stale: last successful fetch "
                               f"{now_ts - ch_f['last_ok']}s ago")
            cd_f = self._feeds.get("candles", {})
            if cd_f.get("last_fail") and cd_f["last_fail"] > cd_f.get("last_ok", 0) \
                    and now_ts - cd_f["last_fail"] < 600:
                add("WARNING", "Candle feed reporting failures (see Data Health)")
        if partial_count:
            add("WARNING", f"{partial_count} contract(s) have PARTIAL AVWAP "
                           f"history (Data Health page)")
        if max_pos and len(open_pos) >= 0.8 * int(max_pos):
            add("WARNING", f"Open positions {len(open_pos)}/{max_pos} "
                           f"(>=80% of limit)")
        if max_day and daily_pnl <= -0.75 * float(max_day):
            add("WARNING", f"Daily loss {daily_pnl:.0f} is >=75% of limit "
                           f"{max_day:.0f}")
        if self.risk.flags().get("emergency_stop"):
            add("CRITICAL", "EMERGENCY STOP engaged (entries disabled, exits run)")
        rest = self.rest.stats() if self.rest is not None else None
        if rest:
            last_err = rest.get("last_error") or ""
            if "401" in last_err or "Authentication" in last_err:
                add("CRITICAL", f"Dhan authentication failure: {last_err[:140]}")
        # recent journal incidents (persistent history is the Journal page)
        for j in self.journal.tail(limit=30):
            ev = j.get("event") or ""
            if ev in ("ORDER_FAILED", "ORDER_REJECTED"):
                add("CRITICAL", f"{ev}: {j.get('symbol') or ''} "
                                f"{json.dumps(j.get('detail') or {})[:120]}")
            elif ev == "RECONCILE":
                add("WARNING", f"RECONCILE: {j.get('symbol') or ''} "
                               f"{json.dumps(j.get('detail') or {})[:120]}")
        return out

    # ------------------------------------------------- V2 endpoints
    def contract_payload(self, security_id: str) -> dict:
        """Contract detail page: state + persisted candles (with the AVWAP
        each candle produced) + signals + position, for the chart."""
        c = self.engine.contract_for(security_id)
        st = self.db.get_avwap_state(security_id)
        partial = security_id in set(
            self.db.kv_get("avwap_partial_history", []) or [])
        candles = [dict(r) for r in self.db.get_candles(security_id, limit=400)]
        signals = [dict(r) for r in self.db.get_signals_for(security_id, limit=30)]
        pos = self.positions.find_by_security(security_id)
        return {
            "security_id": security_id,
            "contract": None if c is None else {
                "symbol": c.name, "underlying": c.underlying,
                "strike": c.strike, "type": c.option_type,
                "expiry": str(c.expiry),
            },
            "avwap": None if st is None else {
                "last_avwap": st["last_avwap"],
                "last_close": st["last_close"],
                "last_candle_ts": st["last_candle_ts"],
                "anchor_ts": st["anchor_ts"],
                "partial": partial,
            },
            "ltp": self.ltp_getter(security_id),
            "oi": (self._oi.get((c.underlying, str(c.expiry), c.strike, c.option_type))
                   if c else None),
            "candles": candles,
            "signals": signals,
            "position": dict(pos) if pos else None,
        }

    def journal_payload(self, limit: int = 200, event: str = None,
                        symbol: str = None, security_id: str = None,
                        since_ts: int = 0, until_ts: int = None) -> list:
        rows = self.db.journal_query(limit=limit, event=event, symbol=symbol,
                                     security_id=security_id,
                                     since_ts=since_ts, until_ts=until_ts)
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail") or "{}")
            except (TypeError, ValueError):
                d["detail"] = {}
            out.append(d)
        return out

    def pnl_history_payload(self) -> dict:
        rows = self.db.get_positions(status="CLOSED", limit=10000)
        from datetime import timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        def est_charges(en_p, ex_p, q):
            if not en_p or not ex_p or not q: return 0.0
            sp = en_p * q
            bp = ex_p * q
            br = 20.0 * 2
            stt = sp * 0.001
            exc = (sp + bp) * 0.00035
            sebi = (sp + bp) * 0.000001
            stamp = bp * 0.00003
            gst = (br + exc + sebi) * 0.18
            return br + stt + exc + sebi + stamp + gst
        from collections import defaultdict
        daily = defaultdict(lambda: {"trades": 0, "gross_pnl": 0.0, "charges": 0.0, "net_pnl": 0.0})
        for row_raw in rows:
            r = dict(row_raw)
            if not r.get("exit_time"): continue
            day = datetime.fromtimestamp(int(r["exit_time"]), ist).strftime("%Y-%m-%d")
            g_pnl = r.get("pnl") or 0.0
            chg = est_charges(r.get("entry_price"), r.get("exit_price"), r.get("quantity"))
            daily[day]["trades"] += 1
            daily[day]["gross_pnl"] += g_pnl
            daily[day]["charges"] += chg
            daily[day]["net_pnl"] += (g_pnl - chg)
        out = []
        for day in sorted(daily.keys(), reverse=True):
            s = daily[day]
            out.append({
                "date": day, "trades": s["trades"],
                "gross_pnl": round(s["gross_pnl"], 2),
                "charges": round(s["charges"], 2),
                "net_pnl": round(s["net_pnl"], 2)
            })
        return {"days": out}
