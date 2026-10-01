"""TraderApp: the orchestrator.

Phase 8 is paper-only:

    Dhan market data  ->  20-stock scanner -> 15-min candles / AVWAP
                       ->  unchanged strategy signals -> PAPER execution
                       ->  Positions / P&L / Journal / Dashboard / reports

This application deliberately refuses LIVE mode and contains no order-placement
path. Do not enable LIVE trading until a separately approved phase.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
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
from execution.broker import Broker
from execution.paper import PaperBroker
from paper_trading.reports import PaperReportWriter
from market.candles import CandleEngine, last_closed_window_start
from market.opportunity_runtime import OpportunityScanPipeline
from market.universe import UniverseManager, select_monthly_expiry
from market.universe_manager import UniverseManager as StockUniverseManager
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
        self.mode = str(cfg_get(cfg, "trading_mode", "PAPER")).upper()
        if self.mode != "PAPER":
            raise RuntimeError(
                "LIVE trading is hard-disabled in Phase 8. Set trading_mode=PAPER; "
                "no live order-placement API is available in this application."
            )
        if str(cfg_get(cfg, "paper.fill_mode", "marketable_quote")) != "marketable_quote":
            raise ValueError(
                "The Phase 8 app requires paper.fill_mode=marketable_quote; "
                "non-marketable diagnostic fills are not enabled in the persistent app"
            )
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
        self.paper_session_id = uuid.uuid4().hex
        self.paper_report_dir = cfg_get(cfg, "paper.report_dir", "reports/paper")
        self.paper_report_writer = PaperReportWriter(
            self.db, self.paper_report_dir, cfg_get(cfg, "paper", {})
        )
        self._paper_current_day: Optional[str] = None
        self._last_paper_heartbeat = 0.0
        self._last_paper_report_at = 0.0

        # ---- feed + clock + broker
        source = cfg_get(cfg, "market_data.source", "dhan")
        self.source = source
        scanner_requested = bool(cfg_get(cfg, "opportunity.enabled", True))
        # MockFeed deliberately has no option-chain book, OI or IV source. Do
        # not fabricate scanner data for it; the legacy mock AVWAP demo remains
        # available and the Dhan-backed scanner is active only on Dhan data.
        self.opportunity_enabled = scanner_requested and source == "dhan"
        self.opportunity_status = (
            "disabled_mock_source" if scanner_requested and source != "dhan"
            else "starting" if self.opportunity_enabled else "disabled"
        )
        self.opportunity_error: Optional[str] = None
        self.opportunity_result = None
        self._opportunity_service: Optional[OpportunityScanPipeline] = None
        self._opportunity_symbols: list[str] = []
        self._scanner_history_by_symbol: dict[str, list] = {}
        self._chain_snapshots: dict = {}
        self._chain_snapshot_lock = threading.RLock()
        self._opportunity_scores: dict[str, dict] = {}
        self._opportunity_lock = threading.RLock()
        self._bootstrap_complete = False
        if scanner_requested and source != "dhan":
            log.info("Opportunity scanner disabled for %s source: no timestamped option-chain book is available", source)
        self.rest = None
        self.md = None
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
            self.chain_client = PacedChainClient(rest)

        self.broker: Broker = self._make_broker()

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
        # Only real marketfeed/quote observations are eligible for quote-aware
        # fills. Cached LTP values are never treated as bid/ask or synthesized.
        self._option_quote_cache: dict[str, object] = {}
        self._underlying_quote_cache: dict[str, object] = {}
        self._quote_cache_lock = threading.RLock()
        self._processed: dict[str, int] = {}      # sec_id -> last processed window start
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

    def _make_broker(self, live_confirmed: bool = False) -> Broker:
        # Defense in depth: a caller cannot turn a PAPER app instance into a
        # live broker by changing mode after construction or passing a flag.
        if self.mode != "PAPER":
            raise RuntimeError("Only the Phase 8 PAPER broker is supported")
        broker = PaperBroker(
            self.db,
            self.positions,
            self.journal,
            fill_mode=cfg_get(self.cfg, "paper.fill_mode", "marketable_quote"),
            slippage_bps=float(cfg_get(self.cfg, "paper.slippage_bps", 5)),
            max_quote_age_seconds=float(cfg_get(self.cfg, "paper.max_quote_age_seconds", 60)),
            quote_fallback=str(cfg_get(self.cfg, "paper.quote_fallback", "signal_candle_close")),
            transaction_cost_per_order=float(cfg_get(self.cfg, "paper.transaction_cost_per_order", 0)),
            transaction_cost_bps=float(cfg_get(self.cfg, "paper.transaction_cost_bps", 0)),
            margin_estimate_pct=float(cfg_get(
                self.cfg, "paper.margin_estimate_pct_of_underlying_notional", 20
            )),
        )
        if broker.fill_mode != "marketable_quote":
            raise ValueError(
                "The Phase 8 app requires paper.fill_mode=marketable_quote; "
                "non-marketable diagnostic fills are not enabled in the persistent app"
            )
        return broker

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
        self._bootstrap_complete = True
        if self.source == "dhan":
            # Bootstrap already fetched the option chains and ran the initial
            # ranking; do not immediately repeat the same rate-limited refresh
            # on the first main-loop tick.
            self._last_universe_refresh = time.time()
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

        if self.opportunity_enabled:
            try:
                phase1 = StockUniverseManager(cfg=self.cfg, rest=self.rest)
                phase1.initialize_from_master_bundle(bundle, captured_at=self.now())
                phase1_symbols = phase1.get_enabled_symbols()
                # market_data.universe_stocks remains an optional additional
                # allow-list; the validated Phase 1 list is the scanner's base.
                if configured:
                    configured_set = {str(value).strip().upper() for value in configured
                                       if str(value).strip()}
                    phase1_symbols = [symbol for symbol in phase1_symbols
                                      if symbol.upper() in configured_set]
                self._opportunity_symbols = list(phase1_symbols)
                self._opportunity_service = OpportunityScanPipeline(
                    self.cfg,
                    market_data=self.md,
                    master_contracts=self.master_contracts,
                    validated_symbols=self._opportunity_symbols,
                    underlying_ids=self.underlying_ids,
                    master_snapshot_at=phase1.master_loaded_at,
                    now_fn=self.now,
                )
                # Scanner ON: only Phase 1's validated 20-stock universe enters
                # contract discovery. Open positions are still unioned later.
                underlyings = list(self._opportunity_symbols)
                self.opportunity_status = "collecting_inputs"
                log.info("Opportunity scanner universe: %d validated stocks", len(underlyings))
            except Exception as exc:
                self._opportunity_service = None
                self._opportunity_symbols = []
                self.opportunity_status = "error"
                self.opportunity_error = f"{exc.__class__.__name__}: {exc}"
                log.exception("Opportunity scanner initialization failed; new entries fail closed")
                underlyings = []

        log.info("F&O stock universe: %d stocks", len(underlyings))

        today = self.now().date()
        switch_day = int(cfg_get(self.cfg, "strategy.expiry_switch_day", 24))
        # group master contracts by (underlying, expiry)
        master_index: dict[tuple, dict] = {}
        for c in self.master_contracts:
            master_index.setdefault((c.underlying, c.expiry), {})[
                (c.strike, c.option_type)
            ] = c

        if self._opportunity_service is not None and self._opportunity_symbols:
            self._scanner_history_by_symbol = self._opportunity_service.fetch_underlying_bars(
                symbols=self._opportunity_symbols,
                as_of=self.now(),
            )

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
            if self.opportunity_enabled:
                log.error("No scanner underlyings could be bootstrapped; keeping new entries fail-closed")
                self._run_opportunity_scan()
                return
            raise RuntimeError("Bootstrap failed for all underlyings; aborting")

        if self.opportunity_enabled:
            self._run_opportunity_scan()

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
            with self._chain_snapshot_lock:
                self._chain_snapshots[u.upper()] = chain
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
    def _record_paper_error(
        self, *, component: str, code: str, message: str,
        severity: str = "ERROR", recovery_action: Optional[str] = None,
        recovered: bool = False, context: Optional[dict] = None,
    ) -> None:
        """Write recovery/runtime failures to both the audit table and journal."""
        try:
            ts = epoch(self.now())
            day = from_epoch(ts).date().isoformat()
            event = {
                "timestamp": ts,
                "trading_date": day,
                "session_id": self.paper_session_id,
                "component": component,
                "severity": severity,
                "code": code,
                "message": str(message)[:2000],
                "recovery_action": recovery_action,
                "recovered": recovered,
                "context": context or {},
            }
            self.db.record_paper_error(event)
            self.journal.write(
                "PAPER_ERROR", ts=ts,
                detail={key: value for key, value in event.items()
                        if key not in {"timestamp", "trading_date", "session_id"}},
            )
        except Exception:
            log.exception("could not persist paper error %s", code)

    def _begin_paper_session(self) -> None:
        previous = self.db.kv_get("paper_runtime_state", {}) or {}
        interrupted = isinstance(previous, dict) and previous.get("status") in {"STARTING", "RUNNING"}
        recovered = []
        try:
            recovered = self.broker.recover_incomplete_records()
        except Exception as exc:
            self._record_paper_error(
                component="recovery", code="AUDIT_RECOVERY_FAILED",
                message=f"{exc.__class__.__name__}: {exc}",
                recovery_action="startup will continue; inspect persistent positions and audit rows",
                context={"previous_session": previous},
            )
        if interrupted or recovered:
            self._record_paper_error(
                component="recovery",
                code="UNEXPECTED_RESTART" if interrupted else "AUDIT_ROWS_RECONSTRUCTED",
                message=("Previous paper session did not record a clean stop" if interrupted
                         else "Recovered audit rows from persisted paper positions"),
                severity="WARNING",
                recovery_action="open positions and audit history restored from SQLite; quote fields remain missing when unavailable",
                recovered=True,
                context={"previous_session": previous, "repaired_position_ids": recovered},
            )
        now = self.now()
        self._paper_current_day = now.date().isoformat()
        self.db.kv_set("paper_runtime_state", {
            "status": "RUNNING",
            "session_id": self.paper_session_id,
            "started_at": epoch(now),
            "trading_date": self._paper_current_day,
            "source": self.source,
            "mode": "PAPER",
            "live_orders_sent": False,
        })

    def _paper_heartbeat(self) -> None:
        if time.time() - self._last_paper_heartbeat < 30:
            return
        now = self.now()
        self.db.kv_set("paper_runtime_state", {
            "status": "RUNNING",
            "session_id": self.paper_session_id,
            "started_at": self.started_at,
            "heartbeat_at": epoch(now),
            "trading_date": now.date().isoformat(),
            "source": self.source,
            "mode": "PAPER",
            "live_orders_sent": False,
        })
        self._last_paper_heartbeat = time.time()

    def _refresh_paper_report(self, trading_date: Optional[str] = None) -> Optional[dict]:
        try:
            day = trading_date or self.now().date().isoformat()
            report = self.paper_report_writer.write_day(day)
            self._last_paper_report_at = time.time()
            return report
        except Exception as exc:
            log.exception("paper report generation failed")
            self._record_paper_error(
                component="report", code="REPORT_GENERATION_FAILED",
                message=f"{exc.__class__.__name__}: {exc}",
                recovery_action="retry automatically on the next report interval or run python -m paper_trading report",
                context={"trading_date": trading_date},
            )
            return None

    def _finish_paper_session(self) -> None:
        try:
            if self._paper_current_day:
                self._refresh_paper_report(self._paper_current_day)
            self.db.kv_set("paper_runtime_state", {
                "status": "STOPPED",
                "session_id": self.paper_session_id,
                "stopped_at": epoch(self.now()),
                "trading_date": self.now().date().isoformat(),
                "source": self.source,
                "mode": "PAPER",
                "live_orders_sent": False,
            })
            self.journal.write("APP_STOP", ts=epoch(self.now()),
                               detail={"mode": "PAPER", "live_orders_sent": False})
        except Exception:
            log.exception("paper shutdown persistence failed")
        log.info("Paper trading engine stopped; no live orders were sent")

    def run(self, stop_after: Optional[float] = None) -> None:
        self._begin_paper_session()
        started_real = time.monotonic()
        try:
            self.bootstrap()
            self._refresh_paper_report()
            self.journal.write("APP_START", ts=epoch(self.now()),
                               detail={"mode": "PAPER", "source": self.source,
                                       "paper_session_id": self.paper_session_id,
                                       "open_positions": len(self.positions.get_open()),
                                       "monitored_contracts": len(self.universe.monitored_ids()),
                                       "live_orders_sent": False})
            log.info("=== PAPER TRADING ENGINE RUNNING (source=%s) ===", self.source)

            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    self._tick()
                except Exception as exc:
                    self.last_error = f"{exc.__class__.__name__}: {exc}"
                    log.exception("Main loop error: %s", exc)
                    if time.time() - getattr(self, "_last_paper_loop_error", 0) >= 60:
                        self._last_paper_loop_error = time.time()
                        self._record_paper_error(
                            component="main_loop", code="TICK_FAILED",
                            message=self.last_error,
                            recovery_action="loop will retry on the next tick",
                        )
                self.last_update_ts = int(time.time())
                if stop_after is not None and (time.monotonic() - started_real) >= stop_after:
                    log.info("stop_after reached; shutting down")
                    break
                elapsed = time.monotonic() - t0
                sleep_s = max(0.05, self.loop_tick - elapsed)
                self._stop.wait(sleep_s)
        except Exception as exc:
            self._record_paper_error(
                component="startup", code="PAPER_STARTUP_FAILED",
                message=f"{exc.__class__.__name__}: {exc}",
                recovery_action="resolve startup/data error and restart; existing paper positions remain persisted",
            )
            raise
        finally:
            self._finish_paper_session()

    def stop(self) -> None:
        self._stop.set()

    # ----------------------------------------------------------------- tick
    def _tick(self) -> None:
        now = self.now()
        state = session_state(now)
        trading_day = now.date().isoformat()
        previous_day = getattr(self, "_paper_current_day", None)
        if previous_day and previous_day != trading_day:
            self._refresh_paper_report(previous_day)
        self._paper_current_day = trading_day

        if state == "OPEN":
            # Preserve the existing 15:15 mechanical exit. Failed paper exits
            # remain open and are retried while the market is open; they are
            # never silently marked closed without a usable observed price.
            if now.time() >= dtime(15, 15):
                self.set_disable_entries(True)
                retry_seconds = max(1.0, float(cfg_get(self.cfg, "paper.exit_retry_seconds", 30)))
                last_attempt = getattr(self, "_last_intraday_exit_attempt", 0.0)
                if (not getattr(self, "_intraday_exited", False)
                        and time.time() - last_attempt >= retry_seconds):
                    log.info("Intraday time reached (15:15). Closing paper positions.")
                    self.journal.write(
                        "INTRADAY_EXIT", ts=epoch(now),
                        detail={"note": "15:15 achieved", "mode": "PAPER"},
                    )
                    self.exit_all_positions(reason="INTRADAY_EXIT_15_15")
                    self._last_intraday_exit_attempt = time.time()
                    self._intraday_exited = not bool(self.positions.get_open())
            elif getattr(self, "_intraday_exited", False):
                self.set_disable_entries(False)
                self._intraday_exited = False
                self._last_intraday_exit_attempt = 0.0

            self._process_closed_windows(now)
            if self.source == "mock":
                self.feed.step()

        self._maybe_refresh_universe(now)
        self._poll_ltp(now)

        # Keep the position universe in sync (cheap, every few ticks), keep a
        # restart heartbeat, and refresh the durable daily report periodically.
        self._sync_position_universe()
        self._paper_heartbeat()
        if time.time() - self._last_paper_report_at >= float(
            cfg_get(self.cfg, "paper.report_refresh_seconds", 300)
        ):
            self._refresh_paper_report(trading_day)

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
            # A background re-rank can tighten the new-entry allow-list while
            # this loop is fetching candles. Recheck membership before work;
            # position IDs remain present in the monitored union.
            if sec not in self.universe.monitored_ids():
                continue
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
                if sec not in self.universe.monitored_ids():
                    break
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
                signal = self.engine.on_candle(c)
                try:
                    self._record_evaluation(c, signal)
                except Exception:
                    # Evaluation auditing must not suppress an AVWAP signal or
                    # stop completed-candle processing if storage is degraded.
                    log.exception("AVWAP evaluation record failed for %s ts=%s", sec, c.ts)
                self._persist_candle(sec, c)
                self._touch_feed("candles", True)
                self._processed[sec] = win_ts

    def _maybe_refresh_universe(self, now: datetime) -> None:
        refresh_min = float(cfg_get(self.cfg, "market_data.universe_refresh_minutes", 60))
        if self.opportunity_enabled:
            scanner_min = float(cfg_get(self.cfg, "opportunity.refresh_minutes", 15))
            if scanner_min <= 0:
                scanner_min = 15.0
            refresh_min = min(refresh_min, scanner_min)
        if time.time() - self._last_universe_refresh < refresh_min * 60:
            return
        self._last_universe_refresh = time.time()
        if self.source == "mock":
            for u in self.feed.underlyings:
                self.universe.refresh_atm(u, self.feed.spot_for(u))
            return
        if self.source == "dhan":
            # Chain refresh and re-ranking run in the background so the loop
            # never blocks on Dhan's one-request-per-three-seconds chain limit.
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

            refresh_underlyings = (
                list(self._opportunity_symbols)
                if self.opportunity_enabled
                else list(self.universe.by_underlying.keys())
            )
            if self._opportunity_service is not None and self._opportunity_symbols:
                # Keep stock-volatility history point-in-time and request it
                # before the paced chain sweep so the quote/book snapshots are
                # as fresh as possible when the common scan timestamp is taken.
                self._scanner_history_by_symbol = self._opportunity_service.fetch_underlying_bars(
                    symbols=self._opportunity_symbols,
                    as_of=self.now(),
                )
            if self.opportunity_enabled:
                with self._chain_snapshot_lock:
                    for u in refresh_underlyings:
                        self._chain_snapshots.pop(u.upper(), None)

            for u in refresh_underlyings:
                try:
                    self._bootstrap_underlying_dhan(u, today, switch_day, master_index)
                except Exception as e:
                    log.warning("Universe refresh failed for %s: %s", u, e)
                # dynamic ATM from fresh spot
                uinfo = self.universe.get(u)
                if uinfo and uinfo.spot:
                    self.universe.refresh_atm(u, uinfo.spot)
            if self.opportunity_enabled:
                self._run_opportunity_scan()
            log.info("Universe refresh complete: %d underlyings",
                     len(self.universe.by_underlying))
        except Exception as exc:
            log.exception("Universe refresh crashed")
            if self.opportunity_enabled:
                self._fail_closed_opportunity_scan(exc)
                self._record_paper_scan_failure(exc, 0.0)

    def _run_opportunity_scan(self):
        """Rank current new-entry candidates and atomically update the allow-list."""
        if not self.opportunity_enabled:
            return None
        started = time.perf_counter()
        if self._opportunity_service is None:
            error = RuntimeError("opportunity scanner inputs were not initialized")
            self._fail_closed_opportunity_scan(error)
            self._record_paper_scan_failure(error, 0.0)
            return None

        with self._chain_snapshot_lock:
            chain_snapshots = dict(self._chain_snapshots)
        with self._opportunity_lock:
            underlying_bars = dict(self._scanner_history_by_symbol)
        positions = self.positions.get_open()
        try:
            result = self._opportunity_service.scan(
                chain_snapshots=chain_snapshots,
                underlying_bars_by_symbol=underlying_bars,
                existing_position_underlyings=tuple(
                    str(position.get("underlying") or "").strip().upper()
                    for position in positions
                    if str(position.get("underlying") or "").strip()
                ),
                existing_position_security_ids=tuple(
                    str(position.get("security_id") or "") for position in positions
                ),
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.exception("Opportunity scan failed; clearing new-entry eligibility")
            self._fail_closed_opportunity_scan(exc)
            self._record_paper_scan_failure(exc, elapsed_ms)
            return None

        self._apply_opportunity_result(result)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        try:
            self._record_paper_scan(result, elapsed_ms)
        except Exception as exc:
            log.exception("Scanner snapshot persistence failed; fail-closing new entries")
            self._fail_closed_opportunity_scan(exc)
            self._record_paper_error(
                component="scanner_persistence", code="SCAN_PERSISTENCE_FAILED",
                message=f"{exc.__class__.__name__}: {exc}",
                recovery_action="new-entry eligibility cleared; retry at next scanner refresh",
            )
            return None
        self._refresh_paper_report(result.timestamp.date().isoformat())
        return result

    def _record_paper_scan_failure(self, error: Exception, duration_ms: float) -> None:
        now = self.now()
        scan_id = f"{epoch(now)}-{uuid.uuid4().hex[:10]}"
        try:
            self.db.record_paper_scan({
                "scan_id": scan_id,
                "timestamp": epoch(now),
                "trading_date": now.date().isoformat(),
                "status": "failed",
                "stocks_scanned": len(self._opportunity_symbols),
                "contracts_scanned": 0,
                "qualified_contracts": 0,
                "rejected_contracts": 0,
                "selected_contracts": 0,
                "duration_ms": max(0.0, float(duration_ms)),
                "symbols": list(self._opportunity_symbols),
                "error": f"{error.__class__.__name__}: {error}",
            }, [])
            self._record_paper_error(
                component="scanner", code="OPPORTUNITY_SCAN_FAILED",
                message=f"{error.__class__.__name__}: {error}",
                recovery_action="new-entry eligibility remains cleared; retry at next scanner refresh",
                context={"scan_id": scan_id, "duration_ms": duration_ms},
            )
            self._refresh_paper_report(now.date().isoformat())
        except Exception:
            log.exception("failed to persist scanner failure snapshot")

    def _record_paper_scan(self, result, duration_ms: float) -> None:
        """Persist each discovered contract, including qualification/rejection state."""
        service = self._opportunity_service
        discovered = dict(getattr(service, "last_discovered_contracts", {}) or {})
        liquidity_report = getattr(service, "last_liquidity_report", None)
        liquidity_by_id = liquidity_report.by_security_id() if liquidity_report else {}
        volatility_by_symbol = getattr(service, "last_volatility_by_underlying", {}) or {}
        ranked = {str(row.security_id): row for row in (result.contracts or ())}
        top = {str(row.security_id): row for row in (result.top_opportunities or ())}
        excluded = {str(row.security_id): row for row in (result.excluded_contracts or ())}
        selected_ids = set(self.universe.scanner_ids())
        timestamp = epoch(result.timestamp)
        scan_id = f"{timestamp}-{uuid.uuid4().hex[:10]}"
        candidates = []
        qualified_count = 0
        for security_id, contract in sorted(discovered.items()):
            liquidity = liquidity_by_id.get(str(security_id))
            is_qualified = bool(liquidity and liquidity.qualified)
            qualified_count += int(is_qualified)
            rank_item = ranked.get(str(security_id)) or top.get(str(security_id))
            exclusion = excluded.get(str(security_id))
            volatility = volatility_by_symbol.get(str(contract.underlying).upper())
            reasons = list(getattr(liquidity, "rejection_reasons", ()) or ()) if liquidity else []
            if exclusion is not None:
                reasons = list(dict.fromkeys(reasons + list(exclusion.reasons or ())))
                status = "EXCLUDED_" + "_".join(str(v) for v in exclusion.reasons)
            elif not liquidity:
                reasons = ["MISSING_LIQUIDITY_RESULT"]
                status = "MISSING_LIQUIDITY_RESULT"
            elif not is_qualified:
                status = "REJECTED_LIQUIDITY"
            elif str(security_id) in top and str(security_id) in selected_ids:
                status = "TOP_OPPORTUNITY"
            elif str(security_id) in top:
                reasons = ["AVWAP_HISTORY_PRIME_FAILED_OR_NOT_REGISTERED"]
                status = "AVWAP_HISTORY_PRIME_FAILED"
            elif rank_item is not None:
                status = "RANKED_NOT_TOP"
            elif volatility is None:
                reasons = ["MISSING_VOLATILITY_RESULT"]
                status = "MISSING_VOLATILITY_RESULT"
            else:
                status = "NOT_SELECTED_BY_STOCK_LIMIT"
            candidates.append({
                "security_id": str(security_id),
                "symbol": str(contract.underlying).upper(),
                "contract_name": contract.name,
                "expiry": str(contract.expiry),
                "strike": float(contract.strike),
                "option_type": str(contract.option_type),
                "status": status,
                "qualified": is_qualified,
                "selected_for_avwap": str(security_id) in selected_ids,
                "rank": getattr(rank_item, "rank", None),
                "liquidity_score": getattr(rank_item, "liquidity_score", None)
                    if rank_item is not None else getattr(liquidity, "score", None),
                "volatility_score": getattr(rank_item, "volatility_score", None)
                    if rank_item is not None else getattr(volatility, "volatility_score", None),
                "opportunity_score": getattr(rank_item, "opportunity_score", None),
                "spread_percent": getattr(liquidity, "spread_percent", None),
                "quote_age_seconds": getattr(liquidity, "quote_age_seconds", None),
                "rejection_reasons": reasons,
            })
        now_date = result.timestamp.date().isoformat()
        selected_count = len(selected_ids)
        self.db.record_paper_scan({
            "scan_id": scan_id,
            "timestamp": timestamp,
            "trading_date": now_date,
            "status": "completed",
            "stocks_scanned": len(self._opportunity_symbols),
            "contracts_scanned": len(discovered),
            "qualified_contracts": qualified_count,
            "rejected_contracts": max(0, len(discovered) - qualified_count),
            "selected_contracts": selected_count,
            "duration_ms": max(0.0, float(duration_ms)),
            "symbols": list(self._opportunity_symbols),
            "error": None,
        }, candidates)

    def _apply_opportunity_result(self, result) -> None:
        """Apply top-ranked eligibility only; AVWAP remains the signal authority."""
        # The background scan can overlap an entry on the main thread. Re-read
        # open positions at apply time so a newly opened underlying cannot sneak
        # into the new-entry allow-list from a ranking started just before it.
        positions = self.positions.get_open()
        position_ids = {
            str(position.get("security_id") or "") for position in positions
            if position.get("security_id")
        }
        position_underlyings = set()
        for position in positions:
            underlying = str(position.get("underlying") or "").strip()
            if not underlying:
                contract = self.engine.contract_for(str(position.get("security_id") or ""))
                underlying = contract.underlying if contract is not None else ""
            if underlying:
                position_underlyings.add(underlying.strip().upper())
        # Phase 2 result objects carry the authoritative contract metadata used
        # by SignalEngine and order sizing; ranking rows themselves never create
        # a Signal or order.
        discovered = getattr(self._opportunity_service, "last_discovered_contracts", {})
        self.engine.register_contracts(list(discovered.values()))

        ranked = tuple(getattr(result, "top_opportunities", ()) or ())
        eligible_ids = {
            str(item.security_id) for item in ranked
            if item.security_id
            and str(item.symbol).strip().upper() not in position_underlyings
        }
        candidate_scores = {
            str(item.security_id): {
                "symbol": item.symbol,
                "option": item.contract_name,
                "liquidity_score": item.liquidity_score,
                "volatility_score": item.volatility_score,
                "opportunity_score": item.opportunity_score,
                "timestamp": result.timestamp,
            }
            for item in ranked
        }
        if self._bootstrap_complete:
            # Remove contracts that fell out of the new ranking immediately;
            # newly selected contracts are added only after their AVWAP history
            # has been primed below. Update scores for retained IDs atomically.
            retained = self.universe.scanner_ids() & eligible_ids
            with self._opportunity_lock:
                previous = self._opportunity_scores
                preserved = {security_id: previous[security_id]
                             for security_id in position_ids if security_id in previous}
                self._opportunity_scores = {
                    **preserved,
                    **{security_id: candidate_scores[security_id]
                       for security_id in retained if security_id in candidate_scores},
                }
            self.universe.set_eligible_scanner_ids(retained)
            eligible_ids = self._prime_opportunity_contracts(eligible_ids)

        known_ids = {
            security_id for security_id in eligible_ids
            if self.engine.contract_for(security_id) is not None
        }
        missing_contracts = eligible_ids - known_ids
        if missing_contracts:
            log.warning("Opportunity scan omitted %d contract(s) without registered metadata",
                        len(missing_contracts))
        eligible_ids = known_ids
        scores = {
            security_id: candidate_scores[security_id]
            for security_id in eligible_ids if security_id in candidate_scores
        }
        with self._opportunity_lock:
            # Keep the last selected scanner metrics for active positions, whose
            # underlyings are intentionally excluded from new-opportunity ranks.
            previous = self._opportunity_scores
            preserved = {security_id: previous[security_id]
                         for security_id in position_ids if security_id in previous}
            self._opportunity_scores = {**preserved, **scores}
            self.opportunity_result = result
            self.opportunity_error = None
            self.opportunity_status = "ready" if eligible_ids else "no_eligible_contracts"

        # This assignment is deliberately last for newly selected contracts:
        # their required AVWAP history was primed before they can be evaluated.
        self.universe.set_eligible_scanner_ids(eligible_ids)
        log.info(
            "Opportunity scan %s: %d eligible top opportunities (%d qualified ranked contracts)",
            result.timestamp.isoformat(), len(eligible_ids), len(result.contracts),
        )

    def _fail_closed_opportunity_scan(self, error: Exception) -> None:
        """On missing scanner inputs, stop new candidates but preserve positions."""
        self.universe.set_eligible_scanner_ids(set())
        positions = self.positions.get_open()
        position_ids = {str(p.get("security_id") or "") for p in positions}
        with self._opportunity_lock:
            self._opportunity_scores = {
                security_id: value for security_id, value in self._opportunity_scores.items()
                if security_id in position_ids
            }
            self.opportunity_status = "error"
            self.opportunity_error = f"{error.__class__.__name__}: {error}"

    def _prime_opportunity_contracts(self, security_ids: set[str]) -> set[str]:
        """Backfill newly selected contracts before exposing them to candle evaluation.

        A contract can leave and later re-enter the opportunity list. Its
        AVWAP state is merged from the last consumed candle, so skipped scanner
        intervals do not reset or re-anchor the existing AVWAP calculation.
        """
        current_ids = self.universe.scanner_ids()
        position_ids = {str(p["security_id"]) for p in self.positions.get_open()}
        new_ids = sorted(security_ids - current_ids - position_ids)
        if not new_ids:
            return set(security_ids)

        now = self.now()
        history_gap = float(cfg_get(self.cfg, "market_data.history_request_gap_seconds", 1.0))
        partial = set(self.db.kv_get("avwap_partial_history", []) or [])
        accepted = set(security_ids)
        latest_ended = self._last_ended_session_candle_ts(now)
        current_start = candle_start_for(now, self.interval)
        for index, security_id in enumerate(new_ids):
            contract = self.engine.contract_for(security_id)
            if contract is None:
                accepted.discard(security_id)
                continue
            if index and history_gap > 0:
                time.sleep(history_gap)
            state = self.db.get_avwap_state(security_id)
            last_ts = int(state["last_candle_ts"] or 0) if state else 0
            # Existing state needs the full gap since its last consumed bar;
            # only a brand-new contract uses the normal bounded anchor window.
            from_dt = from_epoch(last_ts) if last_ts else self._history_from_dt(now)
            try:
                history, full_range = self.feed.history_for(security_id, from_dt, to_dt=now)
                if current_start is not None:
                    history = [candle for candle in history
                               if candle.ts < epoch(current_start)]
                if history:
                    self.avwap.initialize_from_candles(
                        security_id, history, symbol=contract.name
                    )
                    if full_range:
                        partial.discard(security_id)
                    else:
                        partial.add(security_id)
                refreshed = self.db.get_avwap_state(security_id)
                refreshed_last = int(refreshed["last_candle_ts"] or 0) if refreshed else 0
                stale = bool(latest_ended and refreshed_last < latest_ended)
                if not refreshed_last or stale:
                    log.warning("Opportunity contract %s not activated: AVWAP history is unavailable/stale",
                                contract.name)
                    accepted.discard(security_id)
                    continue
                processed = refreshed_last
                if from_epoch(processed).date() != now.date():
                    processed = 0
                self._processed[security_id] = processed
            except Exception as exc:
                log.warning("Could not prime AVWAP history for opportunity %s: %s",
                            contract.name, exc)
                accepted.discard(security_id)

        self.db.kv_set("avwap_partial_history", sorted(partial))
        return accepted

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
                    if self.mode == "PAPER":
                        with self._quote_cache_lock:
                            self._option_quote_cache[str(sec)] = q
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
                        if q:
                            with self._quote_cache_lock:
                                self._underlying_quote_cache[str(u).upper()] = q
                        if q and u in self.universe.by_underlying:
                            self.universe.refresh_atm(u, q.price)
                except Exception as e:
                    log.warning("underlying spot poll failed: %s", e)
        if self.mode == "PAPER":
            self._save_paper_position_marks(now)

    def _paper_quote_snapshot(self, security_id: str):
        """Return the last observed book, even when stale, for truthful audit fields."""
        with self._quote_cache_lock:
            return self._option_quote_cache.get(str(security_id))

    def _fresh_quote_for(self, security_id: str, now: Optional[datetime] = None):
        """Return only a real, timestamped, still-fresh Dhan quote snapshot."""
        quote = self._paper_quote_snapshot(security_id)
        if quote is None:
            return None
        quote_ts = getattr(quote, "ts", None)
        try:
            quote_ts = int(quote_ts)
        except (TypeError, ValueError, OverflowError):
            return None
        if quote_ts <= 0:
            return None
        at = epoch(now or self.now())
        age = at - quote_ts
        max_age = float(cfg_get(self.cfg, "paper.max_quote_age_seconds", 60))
        if age < 0 or age > max_age:
            return None
        return quote

    def _fresh_underlying_spot(self, underlying: str, now: Optional[datetime] = None):
        with self._quote_cache_lock:
            quote = self._underlying_quote_cache.get(str(underlying).strip().upper())
        if quote is None:
            return None
        quote_ts = getattr(quote, "ts", None)
        price = getattr(quote, "price", None)
        try:
            quote_ts, price = int(quote_ts), float(price)
        except (TypeError, ValueError, OverflowError):
            return None
        age = epoch(now or self.now()) - quote_ts
        max_age = float(cfg_get(self.cfg, "paper.max_quote_age_seconds", 60))
        if quote_ts <= 0 or not math.isfinite(price) or price <= 0 or age < 0 or age > max_age:
            return None
        return price

    def _latest_completed_option_close(self, security_id: str, now: datetime):
        """Return today's last completed Dhan/mock option candle, never a guessed price."""
        state = self.db.get_avwap_state(str(security_id))
        if not state or state["last_candle_ts"] is None or state["last_close"] is None:
            return None
        candle_ts = int(state["last_candle_ts"])
        price = float(state["last_close"])
        now_ts = epoch(now)
        completed_at = candle_ts + self.interval * 60
        try:
            max_age = float(cfg_get(self.cfg, "paper.max_exit_candle_age_seconds", 1800))
        except (TypeError, ValueError, OverflowError):
            return None
        age = now_ts - completed_at
        if (not math.isfinite(max_age) or max_age < 0 or not math.isfinite(price)
                or price <= 0 or candle_ts <= 0 or age < 0 or age > max_age
                or from_epoch(candle_ts).date() != now.date()):
            return None
        return price, candle_ts

    def _save_paper_position_marks(self, now: datetime) -> None:
        """Persist marks only from timestamped marketfeed LTP observations."""
        try:
            for position in self.positions.get_open():
                quote = self._fresh_quote_for(position["security_id"], now)
                if quote is None:
                    continue
                price = float(getattr(quote, "price", 0) or 0)
                if not math.isfinite(price) or price <= 0:
                    continue
                bid = getattr(quote, "bid", None)
                ask = getattr(quote, "ask", None)
                try:
                    bid = float(bid) if bid is not None and float(bid) > 0 else None
                    ask = float(ask) if ask is not None and float(ask) > 0 else None
                except (TypeError, ValueError, OverflowError):
                    bid, ask = None, None
                spread = None
                if bid is not None and ask is not None and ask >= bid:
                    mid = (bid + ask) / 2.0
                    spread = ((ask - bid) / mid * 100.0) if mid > 0 else None
                quote_ts = int(getattr(quote, "ts", 0) or 0)
                entry_price = float(position.get("entry_price") or 0)
                quantity = int(position.get("quantity") or 0)
                state = self.db.get_avwap_state(position["security_id"])
                self.db.save_paper_position_mark({
                    "position_id": position["position_id"],
                    "security_id": position["security_id"],
                    "observed_at": quote_ts,
                    "last_price": price,
                    "bid": bid,
                    "ask": ask,
                    "spread_percent": spread,
                    "quote_age_seconds": max(0, epoch(now) - quote_ts),
                    "avwap": state["last_avwap"] if state else None,
                    "unrealized_pnl": (entry_price - price) * quantity,
                    "mark_source": ("DHAN_MARKETFEED_QUOTE_LTP" if self.source == "dhan"
                                    else "MOCK_SYNTHETIC_LTP"),
                })
        except Exception as exc:
            log.exception("paper position mark persistence failed")
            self._record_paper_error(
                component="position_mark", code="MARK_PERSISTENCE_FAILED",
                message=f"{exc.__class__.__name__}: {exc}",
                recovery_action="will retry on the next successful quote poll",
            )

    def ltp_getter(self, security_id: str) -> Optional[float]:
        """Return only a fresh, finite quote; cached display values are not risk marks."""
        quote = self._fresh_quote_for(str(security_id))
        if quote is None:
            return None
        value = getattr(quote, "price", None)
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return price if math.isfinite(price) and price > 0 else None

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

    # -------------------------------------------------------- evaluations
    def _record_evaluation(self, candle, signal) -> None:
        """Persist scanner context plus the unchanged AVWAP candle outcome."""
        contract = self.engine.contract_for(candle.security_id)
        state = self.db.get_avwap_state(candle.security_id)
        with self._opportunity_lock:
            scanner = dict(self._opportunity_scores.get(candle.security_id, {}))
        scanner_ts = scanner.get("timestamp")
        if isinstance(scanner_ts, datetime):
            scanner_ts = epoch(scanner_ts)
        elif scanner_ts is not None:
            try:
                scanner_ts = int(scanner_ts)
            except (TypeError, ValueError, OverflowError):
                scanner_ts = None

        symbol = contract.underlying if contract else str(candle.security_id)
        option = (
            f"{contract.strike:g} {contract.option_type} {contract.expiry}"
            if contract else str(candle.security_id)
        )
        self.db.save_evaluation({
            "timestamp": epoch(self.now()),
            "candle_ts": int(candle.ts),
            "security_id": str(candle.security_id),
            "symbol": str(symbol),
            "option": option,
            "option_type": contract.option_type if contract else None,
            "strike": contract.strike if contract else None,
            "expiry": contract.expiry if contract else None,
            "liquidity_score": scanner.get("liquidity_score"),
            "volatility_score": scanner.get("volatility_score"),
            "opportunity_score": scanner.get("opportunity_score"),
            "scanner_timestamp": scanner_ts,
            "avwap": state["last_avwap"] if state else None,
            "option_close": float(candle.close),
            "signal": signal.action if signal else None,
        })

    # ------------------------------------------------------------ signals
    def _on_signal(self, sig) -> None:
        log.info("SIGNAL %s %s @ %.2f (avwap %s) reason=%s",
                 sig.action, sig.symbol, sig.signal_price,
                 f"{sig.avwap:.2f}" if sig.avwap else "n/a", sig.reason)
        if sig.action == ENTRY_SELL:
            self._handle_entry(sig)
        elif sig.action == EXIT_BUY:
            self._handle_exit(sig)
        self._refresh_paper_report(self.now().date().isoformat())

    def _risk_gate(self) -> list[str]:
        open_pos = self.positions.get_open()
        day = self.now().strftime("%Y-%m-%d")
        trades_today = self.db.trades_today_count(day)
        marks = {
            str(position["security_id"]): self.ltp_getter(str(position["security_id"]))
            for position in open_pos
        }
        daily_pnl = self.risk.daily_pnl(
            day, open_pos, lambda security_id: marks.get(str(security_id))
        )
        reasons = self.risk.entry_block_reasons(len(open_pos), trades_today, daily_pnl)
        max_loss = float(cfg_get(self.cfg, "risk.max_daily_loss", 0) or 0)
        unpriced = sorted(security_id for security_id, mark in marks.items() if mark is None)
        if max_loss > 0 and unpriced:
            reasons = [reason for reason in reasons
                       if reason != "UNPRICED_OPEN_POSITION_DRAWDOWN_GATE"]
            reasons.append(f"UNPRICED_OPEN_POSITION({','.join(unpriced)})")
        return reasons

    def _handle_entry(self, sig) -> None:
        contract = self.engine.contract_for(sig.security_id)
        if contract is None:
            log.error("Entry signal for unknown contract %s; skipped", sig.security_id)
            self._record_paper_error(
                component="execution", code="UNKNOWN_ENTRY_CONTRACT",
                message=f"No registered contract for security_id={sig.security_id}",
                recovery_action="signal was not executed; wait for a valid scanner/bootstrap registration",
            )
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
        quantity = self.risk.quantity_for(contract)
        execution_time = self.now()
        result = self.broker.execute_entry(
            contract, quantity, sig,
            quote=self._paper_quote_snapshot(contract.security_id),
            execution_time=execution_time,
            underlying_spot=self._fresh_underlying_spot(contract.underlying, execution_time),
        )
        if not result.is_filled:
            self._record_paper_error(
                component="execution", code="PAPER_ENTRY_REJECTED",
                message=f"{contract.name}: {result.raw}",
                recovery_action="no position opened; inspect quote freshness and signal price before retry",
                context={"security_id": contract.security_id, "signal_key": sig.signal_key},
            )

    def _handle_exit(self, sig) -> None:
        pos = self.positions.find_by_security(sig.security_id)
        if pos is None:
            log.error("Exit signal with no open position: %s", sig.symbol)
            self._record_paper_error(
                component="execution", code="EXIT_WITHOUT_OPEN_POSITION",
                message=f"Exit signal has no open paper position: {sig.security_id}",
                severity="WARNING",
                recovery_action="position state retained; no synthetic close was created",
            )
            return
        execution_time = self.now()
        result = self.broker.execute_exit(
            pos, sig,
            quote=self._paper_quote_snapshot(sig.security_id),
            execution_time=execution_time,
            contract=self.engine.contract_for(sig.security_id),
        )
        if not result.is_filled:
            self._record_paper_error(
                component="execution", code="PAPER_EXIT_REJECTED",
                message=f"{pos.get('symbol')}: {result.raw}",
                recovery_action="position remains open; retry on next valid strategy or manual paper-exit action",
                context={"position_id": pos.get("position_id"), "signal_key": sig.signal_key},
            )

    # ---------------------------------------------------------- emergency
    def exit_all_positions(self, reason: str = "MANUAL_EXIT_ALL") -> int:
        log.warning("PAPER EXIT ALL POSITIONS requested (%s)", reason)
        now = self.now()
        self.journal.write("EXIT_ALL_REQUESTED", ts=epoch(now),
                           detail={"reason": reason, "mode": "PAPER"})
        closed = 0
        for position in self.positions.get_open():
            security_id = position["security_id"]
            quote = self._paper_quote_snapshot(security_id)
            fresh = self._fresh_quote_for(security_id, now)
            if fresh is not None:
                price_reference = getattr(fresh, "price", None)
                reference_method = "FRESH_LTP_FALLBACK_NO_VALID_TWO_SIDED_BOOK"
            else:
                candle_reference = self._latest_completed_option_close(security_id, now)
                price_reference = candle_reference[0] if candle_reference is not None else None
                reference_method = (
                    "LAST_COMPLETED_OPTION_CANDLE_CLOSE_FALLBACK"
                    if candle_reference is not None else None
                )
            result = self.broker.execute_exit(
                position, None, fill_price=price_reference,
                reason=reason, quote=quote, execution_time=now,
                contract=self.engine.contract_for(security_id),
                fill_price_method=reference_method,
            )
            if result.is_filled:
                closed += 1
            else:
                self._record_paper_error(
                    component="execution", code="PAPER_EXIT_ALL_POSITION_REJECTED",
                    message=f"{position.get('symbol')}: {result.raw}",
                    recovery_action="position remains open; refresh Dhan quote and retry the paper exit",
                    context={"position_id": position.get("position_id")},
                )
        self._refresh_paper_report(now.date().isoformat())
        return closed

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
        """Paper buy-to-close using a current book or explicitly labeled LTP."""
        pos = self.db.get_position(position_id)
        if pos is None:
            return "position not found"
        pos = dict(pos)
        if pos["status"] not in ("OPEN", "PENDING"):
            return f"position is not open (status: {pos['status']})"
        now = self.now()
        quote = self._paper_quote_snapshot(pos["security_id"])
        fresh = self._fresh_quote_for(pos["security_id"], now)
        ltp_reference = getattr(fresh, "price", None) if fresh is not None else None
        self.journal.write(
            "MANUAL_CLOSE_REQUESTED", ts=epoch(now),
            position_id=position_id, security_id=pos["security_id"],
            symbol=pos["symbol"],
            detail={"ltp_reference": ltp_reference, "fresh_quote_available": fresh is not None},
        )
        result = self.broker.execute_exit(
            pos, None, fill_price=ltp_reference,
            reason="MANUAL_CLOSE", quote=quote, execution_time=now,
            contract=self.engine.contract_for(pos["security_id"]),
        )
        if result.is_filled:
            self._refresh_paper_report(now.date().isoformat())
            method = (result.raw or {}).get("execution_method", "paper fill")
            return f"closed as paper fill at {result.avg_price:.4f} ({method})"
        self._record_paper_error(
            component="execution", code="MANUAL_PAPER_CLOSE_REJECTED",
            message=str(result.raw),
            recovery_action="position was not closed; obtain a fresh Dhan quote and retry",
            context={"position_id": position_id},
        )
        return f"paper exit not filled: {result.raw}"

    def paper_report_payload(self, trading_date: Optional[str] = None) -> dict:
        """Read a persisted day summary for the dashboard/API, without regenerating it."""
        day = trading_date or self.now().date().isoformat()
        date.fromisoformat(day)  # validate the requested date
        saved = next((row for row in self.db.get_paper_daily_reports(limit=10000)
                      if row.get("trading_date") == day), None)
        return {
            "status": "ready" if saved else "not_generated",
            "trading_date": day,
            "summary": (saved or {}).get("summary", {}),
            "report_dir": (saved or {}).get("report_dir", str(self.paper_report_dir)),
            "generated_at": (saved or {}).get("generated_at"),
            "paper_trading_only": True,
            "live_orders_sent": False,
        }

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
                "paper": {
                    "status": "BOOTSTRAPPING",
                    "scanner_status": self.opportunity_status,
                    "scanner_error": self.opportunity_error,
                    "report_dir": str(self.paper_report_dir),
                    "session_id": self.paper_session_id,
                    "live_orders_sent": False,
                },
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
        daily_report_rows = self.db.get_paper_daily_reports(limit=1)
        today_scans = self.db.get_paper_scanner_runs(day)
        runtime_state = self.db.kv_get("paper_runtime_state", {}) or {}
        paper_state = {
            "status": runtime_state.get("status", "READY"),
            "session_id": runtime_state.get("session_id", self.paper_session_id),
            "scanner_status": self.opportunity_status,
            "scanner_error": self.opportunity_error,
            "last_scan": today_scans[-1] if today_scans else None,
            "latest_daily_report": daily_report_rows[0] if daily_report_rows else None,
            "report_dir": str(self.paper_report_dir),
            "live_orders_sent": False,
        }

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
        unrealized_complete = all(row.get("unrealized_pnl") is not None for row in pos_rows)
        unrealized = (
            sum(row["unrealized_pnl"] for row in pos_rows)
            if unrealized_complete else None
        )
        summary = {
            "pnl_today": daily_pnl,
            "unrealized": unrealized,
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
            "storage": {"db_path": self.db.path},
        }

        return {
            "mode": self.mode,
            "source": self.source,
            "paper": paper_state,
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
