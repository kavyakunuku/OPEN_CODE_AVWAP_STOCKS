"""Phase 2–5 opportunity selection is wired to, but does not alter, AVWAP."""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from common.models import Candle, OptionContract, Quote
from common.utils import epoch
from conftest import C, World
from dhan.option_chain import ChainOption, OptionChainData
from market.opportunity_runtime import OpportunityScanPipeline
from market.universe import UniverseManager
from paper_trading.reports import PaperReportWriter
from storage.database import Database
from app import TraderApp

IST = ZoneInfo("Asia/Kolkata")
AS_OF = datetime(2026, 9, 30, 10, 0, tzinfo=IST)
EXPIRY = date(2026, 10, 29)


def _contract(symbol: str, security_id: str, strike: float, side: str) -> OptionContract:
    return OptionContract(
        security_id=security_id,
        symbol=f"{symbol} {strike:g} {side}",
        underlying=symbol,
        strike=strike,
        option_type=side,
        expiry=EXPIRY.isoformat(),
        lot_size=25,
        tick_size=0.05,
    )


class _MarketData:
    def __init__(self, option_quotes, equity_quotes):
        self.option_quotes = option_quotes
        self.equity_quotes = equity_quotes
        self.intraday_requests = []
        self.quote_request_order = []

    def quotes(self, security_ids):
        self.quote_request_order.append("NSE_FNO")
        return {str(sid): self.option_quotes[str(sid)]
                for sid in security_ids if str(sid) in self.option_quotes}

    def quotes_segment(self, security_ids, exchange_segment):
        assert exchange_segment == "NSE_EQ"
        self.quote_request_order.append(exchange_segment)
        return {str(sid): self.equity_quotes[str(sid)]
                for sid in security_ids if str(sid) in self.equity_quotes}

    def intraday_candles(self, security_id, from_dt, to_dt, **kwargs):
        self.intraday_requests.append((str(security_id), from_dt, to_dt, kwargs))
        return []


def _chain(symbol: str, spot: float, strikes: list[float], contracts: list[OptionContract]):
    by_key = {(c.strike, c.option_type): c for c in contracts}
    ce, pe = {}, {}
    for strike in strikes:
        for side, output in (("CE", ce), ("PE", pe)):
            contract = by_key[(strike, side)]
            output[strike] = ChainOption(
                strike=strike,
                option_type=side,
                security_id=contract.security_id,
                ltp=10.0,
                oi=50_000,
                volume=100_000,
                bid=10.0,
                ask=10.02,
                bid_quantity=2_000,
                ask_quantity=2_000,
                bid_depth=[{"price": 10.0, "quantity": 2_000}],
                ask_depth=[{"price": 10.02, "quantity": 2_000}],
            )
    return OptionChainData(
        spot=spot,
        expiry=EXPIRY.isoformat(),
        ce=ce,
        pe=pe,
        observed_at=AS_OF - timedelta(seconds=5),
    )


def _pipeline_fixture():
    contracts = []
    chains = {}
    underlying_ids = {"TESTA": "1001", "TESTB": "1002"}
    for symbol, base, uid in (("TESTA", 100.0, "1001"), ("TESTB", 200.0, "1002")):
        strikes = [base - 10, base, base + 10]
        stock_contracts = [
            _contract(symbol, f"{symbol}_{int(strike)}_{side}", strike, side)
            for strike in strikes for side in ("CE", "PE")
        ]
        contracts.extend(stock_contracts)
        chains[symbol] = _chain(symbol, base, strikes, stock_contracts)

    option_quotes = {
        c.security_id: Quote(c.security_id, 10.01, cum_volume=110_000, ts=epoch(AS_OF))
        for c in contracts
    }
    equity_quotes = {
        uid: Quote(uid, 100.0 if symbol == "TESTA" else 200.0,
                   cum_volume=1_000_000, ts=epoch(AS_OF))
        for symbol, uid in underlying_ids.items()
    }
    market_data = _MarketData(option_quotes, equity_quotes)
    cfg = {
        "strategy": {"itm_strikes_per_side": 1, "expiry_switch_day": 24},
        "market_data": {"history_request_gap_seconds": 0},
        "liquidity": {"max_quote_age_seconds": 120},
        "volatility": {"windows": {
            "atr_period": 2,
            "atr_15m_period": 2,
            "realized_volatility_periods": 2,
            "intraday_volatility_periods": 2,
        }},
        "opportunity": {"enabled": True, "selection": {
            "max_stocks": 10,
            "max_contracts_per_stock": 14,
            "max_top_opportunities": 10,
        }},
    }
    pipeline = OpportunityScanPipeline(
        cfg,
        market_data=market_data,
        master_contracts=contracts,
        validated_symbols=["TESTA", "TESTB"],
        underlying_ids=underlying_ids,
        master_snapshot_at=AS_OF - timedelta(days=1),
        now_fn=lambda: AS_OF,
        sleep_fn=lambda _seconds: None,
    )
    bars_by_symbol = {}
    for symbol, uid, price in (("TESTA", "1001", 100.0), ("TESTB", "1002", 200.0)):
        bars_by_symbol[symbol] = [
            Candle(uid, epoch(AS_OF.replace(hour=9, minute=15) + timedelta(minutes=15 * i)),
                   price + i, price + i + 2, price + i - 1, price + i + 1, 10_000)
            for i in range(3)
        ]
    return pipeline, chains, bars_by_symbol, contracts, market_data


def test_live_assembly_flows_from_discovery_through_hard_liquidity_gate_to_rank():
    pipeline, chains, bars, _contracts, market_data = _pipeline_fixture()

    result = pipeline.scan(
        chain_snapshots=chains,
        underlying_bars_by_symbol=bars,
        as_of=AS_OF,
    )

    assert result.timestamp == AS_OF
    assert len(result.contracts) > 0
    assert {item.symbol for item in result.contracts} == {"TESTA", "TESTB"}
    assert all(item.liquidity_score >= 0 for item in result.contracts)
    assert all(item.volatility_score >= 0 for item in result.contracts)
    assert all(item.opportunity_score >= 0 for item in result.contracts)
    assert {item.security_id for item in result.top_opportunities} <= {
        item.security_id for item in result.contracts
    }
    assert market_data.quote_request_order == ["NSE_EQ", "NSE_FNO"]
    assert len(market_data.intraday_requests) == 0  # supplied captured Phase 4 bars
    assert not hasattr(result, "signals")  # scanner ranks; it cannot emit AVWAP SELLs


def test_existing_position_underlying_is_excluded_from_new_opportunities():
    pipeline, chains, bars, _contracts, _market_data = _pipeline_fixture()

    result = pipeline.scan(
        chain_snapshots=chains,
        underlying_bars_by_symbol=bars,
        existing_position_underlyings=("TESTA",),
        existing_position_security_ids=("TESTA_100_CE",),
        as_of=AS_OF,
    )

    assert all(item.symbol != "TESTA" for item in result.contracts)
    assert "TESTA" in result.excluded_position_underlyings
    assert any(item.symbol == "TESTA" and "EXISTING_POSITION_UNDERLYING" in item.reasons
               for item in result.excluded_contracts)


def test_eligible_allowlist_does_not_remove_open_position_monitoring():
    pipeline, chains, bars, contracts, _market_data = _pipeline_fixture()
    result = pipeline.scan(chain_snapshots=chains, underlying_bars_by_symbol=bars, as_of=AS_OF)
    manager = UniverseManager(it_count=1)
    stock_contracts = [contract for contract in contracts if contract.underlying == "TESTA"]
    manager.update(
        "TESTA", EXPIRY, 100.0, [90.0, 100.0, 110.0],
        {(contract.strike, contract.option_type): contract for contract in stock_contracts},
    )
    open_position_id = "TESTA_OPEN_POSITION"
    manager.add_position_id(open_position_id)

    eligible = {item.security_id for item in result.top_opportunities}
    manager.set_eligible_scanner_ids(eligible)

    assert manager.scanner_ids() == eligible
    assert manager.monitored_ids() == eligible | {open_position_id}

    # Periodic ranking replaces only the new-opportunity allow-list; the old
    # active position remains monitored even when the new ranking has no rows.
    manager.set_eligible_scanner_ids(set())
    assert manager.monitored_ids() == {open_position_id}


def test_phase8_persists_full_scanner_snapshot_and_daily_report(tmp_path):
    pipeline, chains, bars, _contracts, _market_data = _pipeline_fixture()
    result = pipeline.scan(
        chain_snapshots=chains,
        underlying_bars_by_symbol=bars,
        as_of=AS_OF,
    )
    db = Database(str(tmp_path / "phase8_scanner.db"))
    app = object.__new__(TraderApp)
    app._opportunity_service = pipeline
    app._opportunity_symbols = ["TESTA", "TESTB"]
    app.universe = UniverseManager(it_count=1)
    selected_ids = {item.security_id for item in result.top_opportunities}
    app.universe.set_eligible_scanner_ids(selected_ids)
    app.db = db

    app._record_paper_scan(result, 12.5)

    runs = db.get_paper_scanner_runs(AS_OF.date().isoformat())
    candidates = db.get_paper_scanner_candidates(trading_date=AS_OF.date().isoformat())
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["stocks_scanned"] == 2
    assert runs[0]["contracts_scanned"] == len(pipeline.last_discovered_contracts)
    assert len(candidates) == runs[0]["contracts_scanned"]
    assert sum(row["qualified"] for row in candidates) == runs[0]["qualified_contracts"]
    assert {row["security_id"] for row in candidates if row["selected_for_avwap"]} == selected_ids
    assert all(row["status"] for row in candidates)
    assert any(row["selected_for_avwap"] for row in candidates)

    writer = PaperReportWriter(db, tmp_path / "paper_reports")
    report = writer.write_day(AS_OF.date())
    assert report["summary"]["scan_cycles"] == 1
    assert report["summary"]["stocks_scanned"] == 2
    assert report["summary"]["contracts_scanned"] == len(candidates)
    assert report["summary"]["qualified_contracts"] == runs[0]["qualified_contracts"]
    assert (tmp_path / "paper_reports" / AS_OF.date().isoformat() / "daily.json").exists()
    assert db.get_paper_daily_reports(limit=1)[0]["summary"]["scan_cycles"] == 1
    db.close()


def test_scanner_off_and_on_produce_identical_avwap_for_same_contract(tmp_path):
    off = World(tmp_path / "scanner-off", with_broker_execution=False)
    on = World(tmp_path / "scanner-on", with_broker_execution=False)
    for world in (off, on):
        world.universe.update(
            "TESTA", EXPIRY, 1500.0, [1500.0, 1520.0],
            {
                (1500.0, "CE"): world.ce,
                (1500.0, "PE"): world.pe,
            },
        )
    on.universe.set_eligible_scanner_ids({on.ce.security_id})
    assert off.ce.security_id in off.universe.monitored_ids()
    assert off.pe.security_id in off.universe.monitored_ids()
    assert on.universe.monitored_ids() == {on.ce.security_id}

    candles = [
        C("SEC_CE_ATM", 15, 9, 15, close=100, vol=10),
        C("SEC_CE_ATM", 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C("SEC_CE_ATM", 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),
        C("SEC_CE_ATM", 15, 10, 0, close=99, vol=10, open_=101, high=101, low=98.9),
    ]
    for candle in candles:
        from common.utils import candle_end_for, from_epoch
        for world in (off, on):
            world.now.t = candle_end_for(from_epoch(candle.ts)) + timedelta(seconds=5)
        if candle.security_id in off.universe.monitored_ids():
            off.engine.on_candle(candle)
        if candle.security_id in on.universe.monitored_ids():
            on.engine.on_candle(candle)

    off_state = off.db.get_avwap_state(off.ce.security_id)
    on_state = on.db.get_avwap_state(on.ce.security_id)
    for field in (
        "anchor_ts", "cumulative_price_volume", "cumulative_volume",
        "last_candle_ts", "last_close", "last_avwap",
    ):
        assert off_state[field] == on_state[field]
    assert [signal.action for signal in off.signals] == [signal.action for signal in on.signals]


def test_newly_selected_contract_is_history_primed_before_first_completed_signal(tmp_path):
    world = World(tmp_path, with_broker_execution=False)
    app = object.__new__(TraderApp)
    app.db = world.db
    app.avwap = world.avwap
    app.engine = world.engine
    app.positions = world.positions
    app.universe = world.universe
    app.now = world.now
    app.now.t = datetime(2026, 9, 15, 10, 0, 5, tzinfo=IST)
    app.interval = 15
    app.source = "mock"
    app.cfg = {"market_data": {"history_request_gap_seconds": 0}}
    app._processed = {}
    app._history_from_dt = lambda _now: datetime(2026, 9, 15, 9, 0, tzinfo=IST)
    first = C(world.ce.security_id, 15, 9, 15, close=100, vol=10)
    second = C(world.ce.security_id, 15, 9, 30, close=104, vol=10,
               open_=103, high=105, low=103)

    class _HistoryFeed:
        def history_for(self, security_id, from_dt, to_dt=None):
            assert security_id == world.ce.security_id
            return [first, second], True

    app.feed = _HistoryFeed()
    primed = TraderApp._prime_opportunity_contracts(app, {world.ce.security_id})

    assert primed == {world.ce.security_id}
    state = world.db.get_avwap_state(world.ce.security_id)
    assert state["anchor_ts"] == first.ts
    assert state["last_candle_ts"] == second.ts
    assert app._processed[world.ce.security_id] == second.ts

    # The next completed candle, not a partial/intermediate scanner event,
    # remains the first signal opportunity and uses the original AVWAP path.
    world.universe.set_eligible_scanner_ids(primed)
    third = C(world.ce.security_id, 15, 9, 45, close=101, vol=10,
              open_=104, high=104.5, low=100.9)
    from common.utils import candle_end_for, from_epoch
    world.now.t = candle_end_for(from_epoch(third.ts)) + timedelta(seconds=5)
    signal = world.engine.on_candle(third)
    assert signal is not None and signal.action == "ENTRY_SELL"
    assert abs(signal.avwap - 102.0444) < 1e-3


def test_apply_rerank_rechecks_open_underlyings_and_keeps_their_position_metrics(tmp_path):
    world = World(tmp_path, with_broker_execution=False)
    world.universe.update(
        "TESTA", EXPIRY, 1500.0, [1500.0, 1520.0],
        {(1500.0, "CE"): world.ce, (1500.0, "PE"): world.pe,
         (1520.0, "CE"): world.ce2},
    )
    position = world.positions.open_position(
        world.ce, 25, 10.0, epoch(AS_OF), "test", 9.0, "PAPER"
    )
    world.universe.add_position_id(world.ce.security_id)
    world.universe.set_eligible_scanner_ids({world.ce2.security_id, world.pe.security_id})

    app = object.__new__(TraderApp)
    app.db = world.db
    app.engine = world.engine
    app.positions = world.positions
    app.universe = world.universe
    app._opportunity_lock = threading.RLock()
    app._bootstrap_complete = True
    app._opportunity_scores = {
        world.ce.security_id: {
            "liquidity_score": 80.0,
            "volatility_score": 60.0,
            "opportunity_score": 70.0,
            "timestamp": AS_OF,
        }
    }
    app._opportunity_service = SimpleNamespace(
        last_discovered_contracts={world.ce2.security_id: world.ce2}
    )
    stale_rank = SimpleNamespace(
        timestamp=AS_OF,
        top_opportunities=[SimpleNamespace(
            security_id=world.ce2.security_id,
            symbol="TESTA",
            contract_name=world.ce2.symbol,
            liquidity_score=90.0,
            volatility_score=75.0,
            opportunity_score=85.0,
        )],
        contracts=(),
    )

    TraderApp._apply_opportunity_result(app, stale_rank)

    assert world.universe.scanner_ids() == set()
    assert world.universe.monitored_ids() == {world.ce.security_id}
    assert app._opportunity_scores[world.ce.security_id]["opportunity_score"] == 70.0
    assert world.positions.has_open(position["security_id"])


def test_app_persists_scanner_scores_with_completed_candle_avwap_evaluation(tmp_path):
    world = World(tmp_path, with_broker_execution=False)
    app = object.__new__(TraderApp)
    app.db = world.db
    app.engine = world.engine
    app.now = world.now
    app._opportunity_lock = threading.RLock()
    scanner_time = datetime(2026, 9, 15, 9, 30, tzinfo=IST)
    app._opportunity_scores = {
        world.ce.security_id: {
            "liquidity_score": 82.5,
            "volatility_score": 61.0,
            "opportunity_score": 70.25,
            "timestamp": scanner_time,
        }
    }
    candles = [
        C(world.ce.security_id, 15, 9, 15, close=100, vol=10),
        C(world.ce.security_id, 15, 9, 30, close=104, vol=10, open_=103, high=105, low=103),
        C(world.ce.security_id, 15, 9, 45, close=101, vol=10, open_=104, high=104.5, low=100.9),
    ]
    signal = None
    for candle in candles:
        from common.utils import candle_end_for, from_epoch
        world.now.t = candle_end_for(from_epoch(candle.ts)) + timedelta(seconds=5)
        signal = world.engine.on_candle(candle)
        TraderApp._record_evaluation(app, candle, signal)

    rows = world.db.get_evaluations(limit=10)
    assert len(rows) == len(candles)
    assert {row["candle_ts"] for row in rows} == {candle.ts for candle in candles}
    # Retrying the same completed-candle evaluation updates its audit row
    # instead of duplicating it.
    TraderApp._record_evaluation(app, candles[-1], signal)
    assert len(world.db.get_evaluations(limit=10)) == len(candles)

    row = rows[0]
    assert row["timestamp"] == epoch(world.now())
    assert row["symbol"] == "TESTA"
    assert row["option"] == "1500 CE 2026-09-29"
    assert row["liquidity_score"] == 82.5
    assert row["volatility_score"] == 61.0
    assert row["opportunity_score"] == 70.25
    assert row["scanner_timestamp"] == epoch(scanner_time)
    assert row["avwap"] == world.db.get_avwap_state(world.ce.security_id)["last_avwap"]
    assert row["option_close"] == 101
    assert row["signal"] == "ENTRY_SELL"
