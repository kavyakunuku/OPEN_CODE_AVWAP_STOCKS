"""Phase 8 paper execution, audit, restart, and LIVE hard-block tests."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import TraderApp
from common.models import Quote, Signal
from common.utils import IST, epoch
from conftest import World
from dhan.orders import DhanOrders
from execution.live import DhanBroker
from execution.paper import PaperBroker
from paper_trading.reports import PaperReportWriter
from portfolio.risk import RiskManager


def _signal(action="ENTRY_SELL", *, candle_minute=45, signal_price=101.0,
            avwap=102.0, created_minute=0):
    candle_hour = 9 if candle_minute < 60 else 10
    candle_minute %= 60
    candle_ts = epoch(datetime(2026, 9, 15, candle_hour, candle_minute, tzinfo=IST))
    created_at = epoch(datetime(2026, 9, 15, 10, created_minute, tzinfo=IST))
    exit_action = action == "EXIT_BUY"
    return Signal(
        security_id="SEC_CE_ATM",
        action=action,
        candle_ts=candle_ts,
        signal_price=signal_price,
        avwap=avwap,
        prev_close=104.0 if not exit_action else 80.0,
        prev_avwap=102.0 if not exit_action else 81.0,
        reason="CLOSE_ABOVE_AVWAP" if exit_action else "CROSS_BELOW_AVWAP",
        symbol="TESTA 1500 CE",
        underlying="TESTA",
        strike=1500.0,
        option_type="CE",
        expiry="2026-09-29",
        created_at=created_at,
    )


def test_marketable_quote_fills_cross_book_apply_adverse_slippage_and_audit(tmp_path):
    from dataclasses import replace
    world = World(tmp_path, with_broker_execution=False)
    contract = replace(world.ce, tick_size=0.05)
    broker = PaperBroker(
        world.db, world.positions, world.journal,
        fill_mode="marketable_quote", slippage_bps=10,
        max_quote_age_seconds=30,
        transaction_cost_per_order=1.0,
        transaction_cost_bps=10.0,
        margin_estimate_pct=20.0,
    )
    entry_signal = _signal()
    entry_at = entry_signal.created_at + 2
    entry_quote = Quote(
        security_id=world.ce.security_id, price=100.5, ts=entry_at,
        bid=100.0, ask=101.0,
    )

    entry = broker.execute_entry(
        contract, 25, entry_signal, quote=entry_quote,
        execution_time=entry_at, underlying_spot=1500.0,
    )
    assert entry.is_filled
    assert entry.avg_price == 99.9  # sell at bid 100, then 10 bps adverse and tick-down
    position = world.positions.find_by_security(world.ce.security_id)
    assert position is not None

    exit_signal = _signal(
        "EXIT_BUY", candle_minute=45, signal_price=90.0, avwap=89.0, created_minute=5
    )
    exit_at = entry_at + 300
    exit_quote = Quote(
        security_id=world.ce.security_id, price=90.5, ts=exit_at,
        bid=90.0, ask=91.0,
    )
    closed = broker.execute_exit(
        position, exit_signal, quote=exit_quote, execution_time=exit_at,
        contract=contract,
    )
    assert closed.is_filled
    assert closed.avg_price == 91.1  # buy at ask 91, then 10 bps adverse and tick-up

    events = world.db.get_paper_execution_for_position(position["position_id"])
    assert [event["event_type"] for event in events] == ["ENTRY_SELL", "EXIT_BUY"]
    entry_event, exit_event = events
    assert entry_event["timestamp"] == entry_at
    assert entry_event["signal_price"] == entry_signal.signal_price
    assert entry_event["bid"] == 100.0 and entry_event["ask"] == 101.0
    assert entry_event["spread_percent"] == pytest.approx(100 * 1 / 100.5)
    assert entry_event["quote_timestamp"] == entry_at
    assert entry_event["quote_age_seconds"] == 0
    assert entry_event["execution_method"] == "TOP_OF_BOOK_BID"
    assert entry_event["quantity"] == 25 and entry_event["lot_size"] == 25
    assert entry_event["avwap"] == entry_signal.avwap
    assert entry_event["slippage_amount"] == pytest.approx(2.5)
    assert entry_event["transaction_cost"] == pytest.approx(1 + 99.9 * 25 * 0.001)
    assert entry_event["margin_estimate"] == 7500.0
    assert "indicative only" in entry_event["margin_model"]
    assert exit_event["execution_method"] == "TOP_OF_BOOK_ASK"
    assert exit_event["ask"] == 91.0
    assert exit_event["slippage_amount"] == pytest.approx(2.5)
    assert world.db.get_position(position["position_id"])["status"] == "CLOSED"


def test_paper_entry_rejects_quantities_that_are_not_contract_lot_multiples(tmp_path):
    from dataclasses import replace

    world = World(tmp_path, with_broker_execution=False)
    broker = PaperBroker(world.db, world.positions, world.journal, fill_mode="candle_close")
    signal = _signal()

    invalid_multiple = broker.execute_entry(world.ce, 26, signal)
    assert invalid_multiple.status == "REJECTED"
    assert invalid_multiple.raw["reason"] == "quantity_not_lot_multiple"
    assert world.positions.get_open() == []

    unknown_lot = broker.execute_entry(replace(world.ce, lot_size=0), 25, signal)
    assert unknown_lot.status == "REJECTED"
    assert unknown_lot.raw["reason"] == "invalid_lot_size"
    assert world.positions.get_open() == []


def test_stale_quote_is_audited_but_strategy_fill_uses_explicit_candle_fallback(tmp_path):
    world = World(tmp_path, with_broker_execution=False)
    broker = PaperBroker(
        world.db, world.positions, world.journal,
        fill_mode="marketable_quote", slippage_bps=5,
        max_quote_age_seconds=60,
    )
    signal = _signal()
    execution_at = signal.created_at + 180
    stale_quote = Quote(
        security_id=world.ce.security_id, price=100.0,
        ts=signal.created_at, bid=99.5, ask=100.5,
    )

    result = broker.execute_entry(
        world.ce, 25, signal, quote=stale_quote, execution_time=execution_at,
    )
    assert result.is_filled
    assert result.avg_price == pytest.approx(signal.signal_price * (1 - 5 / 10_000))
    event = world.db.get_paper_execution_for_position(
        world.positions.find_by_security(world.ce.security_id)["position_id"]
    )[0]
    assert event["bid"] == 99.5 and event["ask"] == 100.5
    assert event["quote_age_seconds"] == 180
    assert event["execution_method"] == "SIGNAL_CANDLE_CLOSE_FALLBACK_NO_FRESH_BOOK"
    assert event["raw"]["quote_is_fresh"] is False
    assert event["raw"]["max_quote_age_seconds"] == 60


def test_report_writer_exports_daily_and_multi_day_logs(tmp_path):
    world = World(tmp_path, with_broker_execution=False)
    broker = PaperBroker(
        world.db, world.positions, world.journal,
        fill_mode="marketable_quote", slippage_bps=5,
    )
    signal = _signal()
    quote = Quote(
        security_id=world.ce.security_id, price=100.0,
        ts=signal.created_at, bid=99.0, ask=100.0,
    )
    opened = broker.execute_entry(
        world.ce, 25, signal, quote=quote,
        execution_time=signal.created_at, underlying_spot=1500.0,
    )
    assert opened.is_filled

    output = tmp_path / "reports"
    writer = PaperReportWriter(world.db, output)
    report = writer.write_day("2026-09-15")
    summary = report["summary"]
    assert summary["paper_trades"] == 1
    assert summary["open_positions_at_report"] == 1
    assert summary["estimated_margin_open"] == 7500.0
    assert report["assumptions"]["execution_methods_observed"] == ["TOP_OF_BOOK_BID"]
    assert report["executions"][0]["bid"] == 99.0
    assert report["executions"][0]["fill_price"] < report["executions"][0]["bid"]
    assert (output / "2026-09-15" / "daily.json").exists()
    assert (output / "2026-09-15" / "daily.md").exists()
    assert (output / "2026-09-15" / "paper_executions.csv").exists()
    assert (output / "trade_log.csv").exists()
    assert (output / "position_log.csv").exists()
    assert (output / "daily_index.csv").exists()
    serialized = json.loads((output / "2026-09-15" / "daily.json").read_text())
    assert serialized["summary"]["live_orders_sent"] is False
    assert world.db.get_paper_daily_reports(limit=1)[0]["summary"]["paper_trades"] == 1


def test_report_keeps_but_does_not_mislabel_stale_open_position_marks(tmp_path):
    world = World(tmp_path, with_broker_execution=False)
    broker = PaperBroker(
        world.db, world.positions, world.journal,
        fill_mode="marketable_quote", slippage_bps=5,
    )
    signal = _signal()
    quote = Quote(
        security_id=world.ce.security_id, price=100.0,
        ts=signal.created_at, bid=99.0, ask=100.0,
    )
    opened = broker.execute_entry(
        world.ce, 25, signal, quote=quote,
        execution_time=signal.created_at, underlying_spot=1500.0,
    )
    assert opened.is_filled
    position = world.positions.find_by_security(world.ce.security_id)
    observed_at = signal.created_at + 30
    world.db.save_paper_position_mark({
        "position_id": position["position_id"],
        "security_id": world.ce.security_id,
        "observed_at": observed_at,
        "last_price": 90.0,
        "bid": 89.5,
        "ask": 90.5,
        "spread_percent": 100 * 1 / 90.0,
        "quote_age_seconds": 0,
        "avwap": 102.0,
        "unrealized_pnl": (position["entry_price"] - 90.0) * 25,
        "mark_source": "DHAN_MARKETFEED_QUOTE_LTP",
    })

    writer = PaperReportWriter(world.db, tmp_path / "reports")
    fresh = writer.write_day(
        "2026-09-15",
        generated_at=datetime(2026, 9, 15, 10, 1, tzinfo=IST),
    )
    assert fresh["summary"]["unrealized_pnl"] is not None
    assert fresh["open_positions"][0]["mark_fresh"] is True

    stale = writer.write_day(
        "2026-09-15",
        generated_at=datetime(2026, 9, 15, 10, 5, tzinfo=IST),
    )
    assert stale["summary"]["unrealized_pnl"] is None
    assert stale["summary"]["unrealized_mark_coverage"] == "0/1"
    assert stale["open_positions"][0]["mark_price"] == 90.0
    assert stale["open_positions"][0]["mark_fresh"] is False
    assert stale["open_positions"][0]["unrealized_pnl"] is None


def test_recovery_reconstructs_missing_audit_without_fabricating_quotes(tmp_path):
    world = World(tmp_path, with_broker_execution=False)
    position = world.positions.open_position(
        contract=world.ce, quantity=25, entry_price=101.0,
        ts=epoch(datetime(2026, 9, 15, 10, 0, tzinfo=IST)),
        reason="RECOVERY_TEST", avwap=102.0, mode="PAPER",
        order_id="PAPER-ORIGINAL-ENTRY",
    )
    broker = PaperBroker(world.db, world.positions, world.journal)

    repaired = broker.recover_incomplete_records()
    assert repaired == [position["position_id"]]
    event = world.db.get_paper_execution_for_position(position["position_id"])[0]
    assert event["bid"] is None and event["ask"] is None
    assert event["execution_method"] == "RECOVERED_FROM_POSITION_NO_QUOTE"
    assert event["raw"]["reconstructed"] is True
    assert broker.recover_incomplete_records() == []
    assert len(world.db.get_paper_execution_for_position(position["position_id"])) == 1


def test_ltp_getter_does_not_treat_cached_display_price_as_a_fresh_mark():
    app = TraderApp.__new__(TraderApp)
    app.ltp = {"SEC_CE_ATM": 100.0}
    app._fresh_quote_for = lambda security_id: None

    assert app.ltp_getter("SEC_CE_ATM") is None

    app._fresh_quote_for = lambda security_id: SimpleNamespace(price=99.5)
    assert app.ltp_getter("SEC_CE_ATM") == 99.5
    app._fresh_quote_for = lambda security_id: SimpleNamespace(price=float("inf"))
    assert app.ltp_getter("SEC_CE_ATM") is None


def test_mechanical_candle_fallback_rejects_stale_same_day_marks():
    candle_start = datetime(2026, 9, 15, 9, 15, tzinfo=IST)
    candle_ts = epoch(candle_start)
    app = TraderApp.__new__(TraderApp)
    app.cfg = {"paper": {"max_exit_candle_age_seconds": 1800}}
    app.interval = 15
    app.db = SimpleNamespace(get_avwap_state=lambda security_id: {
        "last_candle_ts": candle_ts,
        "last_close": 100.0,
    })

    assert app._latest_completed_option_close(
        "SEC_CE_ATM", datetime(2026, 9, 15, 10, 0, tzinfo=IST)
    ) == (100.0, candle_ts)
    assert app._latest_completed_option_close(
        "SEC_CE_ATM", datetime(2026, 9, 15, 10, 0, 1, tzinfo=IST)
    ) is None


def test_daily_loss_gate_blocks_new_entries_when_an_open_position_is_unpriced():
    class FakeDB:
        @staticmethod
        def kv_get(key, default=None):
            return default

        @staticmethod
        def realized_pnl_today(day):
            return 0.0

        @staticmethod
        def trades_today_count(day):
            return 0

    db = FakeDB()
    app = TraderApp.__new__(TraderApp)
    app.cfg = {"risk": {"max_daily_loss": 25000}}
    app.positions = SimpleNamespace(get_open=lambda: [{
        "security_id": "SEC_CE_ATM", "entry_price": 100.0, "quantity": 25,
    }])
    app.db = db
    app.risk = RiskManager(db, app.cfg)
    app.now = lambda: datetime(2026, 9, 15, 10, 0, tzinfo=IST)
    app.ltp_getter = lambda security_id: None

    assert app._risk_gate() == ["UNPRICED_OPEN_POSITION(SEC_CE_ATM)"]


def test_trader_app_rejects_live_before_storage_or_broker_construction(tmp_path):
    db_path = tmp_path / "must_not_be_created.db"
    with pytest.raises(RuntimeError, match="hard-disabled in Phase 8"):
        TraderApp({
            "trading_mode": "LIVE",
            "storage": {"db_path": str(db_path)},
            "market_data": {"source": "dhan"},
        }, live_confirmed=True)
    assert not db_path.exists()


def test_cli_refuses_live_even_with_legacy_confirmation_flag(tmp_path, monkeypatch, capsys):
    import sys
    from main import main

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"trading_mode": "PAPER"}))
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "main.py", "--config", str(config), "--mode", "LIVE", "--i-understand-live",
    ])
    assert main() == 2
    captured = capsys.readouterr()
    assert "BLOCKED" in captured.err
    assert "disabled for Phase 8" in captured.err


def test_legacy_live_broker_and_order_adapter_are_also_hard_disabled():
    class NoOrderNetwork:
        def __init__(self):
            self.calls = []

        def post(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("live order endpoint must never be called")

    rest = NoOrderNetwork()
    orders = DhanOrders(rest)
    with pytest.raises(RuntimeError, match="hard-disabled in Phase 8"):
        orders.place("123", "SELL", 25)
    assert rest.calls == []
    with pytest.raises(RuntimeError, match="hard-disabled in Phase 8"):
        DhanBroker(rest, orders, None, None, None, live_confirmed=True)
