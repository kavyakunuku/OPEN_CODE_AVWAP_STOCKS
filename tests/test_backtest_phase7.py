from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backtest import (
    BacktestDataError,
    ThresholdScenario,
    default_threshold_scenarios,
    inspect_data,
    load_dataset,
    run_phase7,
)
from backtest.data import MarketSnapshot
from backtest.engine import ArmResult, Phase7Backtester, SimulatedPosition, _quantity_for
from common.models import Candle
from common.utils import IST, from_epoch
from market.universe import UniverseManager

SYMBOL = "TESTA"
EXPIRY = "2026-09-29"
SCAN_AT = "2026-09-15T09:30:00+05:30"


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def make_archive(root: Path, *, future_observations: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "timezone": "Asia/Kolkata",
        "interval_minutes": 15,
        "symbols": [SYMBOL],
        "period_start": SCAN_AT,
        "period_end": "2026-09-15T10:30:00+05:30",
        "expiry_switch_day": 24,
        "source_description": "Synthetic unit-test rows only; never empirical market evidence",
    }), encoding="utf-8")
    master = []
    for side in ("CE", "PE"):
        master.append({
            "captured_at": "2026-09-15T08:45:00+05:30",
            "security_id": f"{SYMBOL}_{side}",
            "symbol": f"{SYMBOL} 100 {side}",
            "underlying": SYMBOL,
            "strike": 100,
            "option_type": side,
            "expiry": EXPIRY,
            "lot_size": 25,
            "instrument": "OPTSTK",
            "exchange": "NSE",
            "exchange_segment": "NSE_FNO",
            "tick_size": 0.05,
            "contract_birth_date": "2026-08-01",
            "expiry_flag": "M",
            "strike_interval": 10,
        })
    _write_csv(root / "contract_master.csv", master, [
        "captured_at", "security_id", "symbol", "underlying", "strike", "option_type",
        "expiry", "lot_size", "instrument", "exchange", "exchange_segment", "tick_size",
        "contract_birth_date", "expiry_flag", "strike_interval",
    ])
    _write_csv(root / "scan_events.csv", [{"scan_id": "scan-1", "as_of": SCAN_AT}], ["scan_id", "as_of"])

    starts = [
        (9, 15, 100, 100, 100, 100),
        (9, 30, 103, 105, 103, 104),
        (9, 45, 104, 104.5, 100.9, 101),
        (10, 0, 101, 101, 98.9, 99),
        (10, 15, 99, 106, 99, 106),
    ]
    option_bars = []
    for side in ("CE", "PE"):
        for index, (hour, minute, open_price, high, low, close) in enumerate(starts):
            at = datetime(2026, 9, 15, hour, minute, tzinfo=IST)
            option_bars.append({
                "security_id": f"{SYMBOL}_{side}",
                "ts": at.isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": 10,
                "is_first_tradable": "true" if index == 0 else "false",
            })
        if future_observations:
            option_bars.append({
                "security_id": f"{SYMBOL}_{side}",
                "ts": "2026-09-15T10:30:00+05:30",
                "open": 1000,
                "high": 1001,
                "low": 999,
                "close": 1000,
                "volume": 999999999,
                "is_first_tradable": "false",
            })
    _write_csv(root / "option_candles.csv", option_bars, [
        "security_id", "ts", "open", "high", "low", "close", "volume", "is_first_tradable",
    ])
    _write_csv(root / "underlying_candles.csv", [{
        "symbol": SYMBOL,
        "security_id": "EQ-TESTA",
        "ts": "2026-09-15T09:15:00+05:30",
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100,
        "volume": 10000,
    }], ["symbol", "security_id", "ts", "open", "high", "low", "close", "volume"])

    snapshots = []
    for side in ("CE", "PE"):
        if side == "CE":
            bid, ask = 99.75, 100.0  # 0.2503%: fails 0.25%, passes 0.50%
        else:
            bid, ask = 99.90, 100.0
        snapshots.append({
            "source": "chain",
            "observed_at": "2026-09-15T09:29:59+05:30",
            "symbol": SYMBOL,
            "underlying": SYMBOL,
            "expiry": EXPIRY,
            "security_id": f"{SYMBOL}_{side}",
            "strike": 100,
            "option_type": side,
            "spot": 100,
            "last_price": 100,
            "volume": 100000,
            "open_interest": 500000,
            "oi_change": 2000,
            "bid": bid,
            "ask": ask,
            "bid_quantity": 1000,
            "ask_quantity": 1000,
            "bid_depth_json": json.dumps([{"price": bid, "quantity": 1000}]),
            "ask_depth_json": json.dumps([{"price": ask, "quantity": 1000}]),
            "implied_volatility": "",
        })
    if future_observations:
        snapshots.append({
            "source": "underlying_quote",
            "observed_at": "2026-09-15T10:45:00+05:30",
            "symbol": SYMBOL,
            "underlying": "",
            "expiry": "",
            "security_id": "EQ-TESTA",
            "strike": "",
            "option_type": "",
            "spot": "",
            "last_price": 99999,
            "volume": 999999999,
            "open_interest": 999999999,
            "oi_change": 999999999,
            "bid": 99998,
            "ask": 100000,
            "bid_quantity": 999999,
            "ask_quantity": 999999,
            "bid_depth_json": "[]",
            "ask_depth_json": "[]",
            "implied_volatility": 999,
        })
    _write_csv(root / "market_snapshots.csv", snapshots, [
        "source", "observed_at", "symbol", "underlying", "expiry", "security_id", "strike",
        "option_type", "spot", "last_price", "volume", "open_interest", "oi_change",
        "bid", "ask", "bid_quantity", "ask_quantity", "bid_depth_json", "ask_depth_json",
        "implied_volatility",
    ])
    return root


def _config():
    return {
        "strategy": {"itm_strikes_per_side": 0, "expiry_switch_day": 24},
        "liquidity": {"max_quote_age_seconds": 60},
        "opportunity": {"selection": {
            "max_stocks": 10,
            "max_contracts_per_stock": 14,
            "max_top_opportunities": 10,
        }},
        "risk": {"max_open_positions": 8, "max_trades_per_day": 20,
                  "max_daily_loss": 25000, "quantity_per_trade": None,
                  "default_lot_size": 250},
        "backtest": {"allow_non_20_symbol_universe_for_diagnostics": True},
    }


def test_missing_local_archive_is_reported_not_synthesized(tmp_path):
    report = inspect_data(tmp_path / "missing")
    assert report["status"] == "blocked_missing_historical_data"
    assert report["empirical_results"] is None
    assert "market_snapshots.csv" in report["missing_files"]


def test_replay_requires_the_configured_20_symbol_universe_by_default(tmp_path):
    dataset = load_dataset(make_archive(tmp_path / "archive"))
    with pytest.raises(ValueError, match="requires the configured 20-stock universe"):
        run_phase7(dataset, config={"strategy": {"itm_strikes_per_side": 0}})


def test_loader_reports_internal_candle_gaps_without_imputing_rows(tmp_path):
    root = make_archive(tmp_path / "archive")
    option_rows = list(csv.DictReader((root / "option_candles.csv").open(encoding="utf-8")))
    option_rows = [
        row for row in option_rows
        if not (row["security_id"] == f"{SYMBOL}_CE" and row["ts"].startswith("2026-09-15T09:45"))
    ]
    _write_csv(root / "option_candles.csv", option_rows, list(option_rows[0]))

    underlying_rows = list(csv.DictReader((root / "underlying_candles.csv").open(encoding="utf-8")))
    second_underlying = dict(underlying_rows[0])
    second_underlying["ts"] = "2026-09-15T09:45:00+05:30"
    underlying_rows.append(second_underlying)
    _write_csv(root / "underlying_candles.csv", underlying_rows, list(underlying_rows[0]))

    dataset = load_dataset(root)

    option_coverage = dataset.quality.files["option_candles.csv"]["coverage"]
    underlying_coverage = dataset.quality.files["underlying_candles.csv"]["coverage"]
    assert option_coverage["missing_grid_intervals_within_observed_range"] == 1
    assert underlying_coverage["missing_grid_intervals_within_observed_range"] == 1
    assert len(dataset.option_candles[f"{SYMBOL}_CE"]) == 4
    assert any(item["code"] == "OPTION_CANDLE_INTERNAL_GAPS_OBSERVED"
               for item in dataset.quality.warnings)
    assert any(item["code"] == "UNDERLYING_CANDLE_INTERNAL_GAPS_OBSERVED"
               for item in dataset.quality.warnings)


def test_loader_requires_explicit_verified_option_anchor(tmp_path):
    root = make_archive(tmp_path / "archive")
    rows = list(csv.DictReader((root / "option_candles.csv").open(encoding="utf-8")))
    rows[0]["is_first_tradable"] = "false"
    _write_csv(root / "option_candles.csv", rows, list(rows[0]))
    dataset = load_dataset(root)
    assert f"{SYMBOL}_CE" not in dataset.anchor_confirmed
    assert f"{SYMBOL}_PE" in dataset.anchor_confirmed
    assert dataset.quality.unverified_option_anchors
    assert any(item["code"] == "OPTION_AVWAP_ANCHORS_UNVERIFIED"
               for item in dataset.quality.warnings)


def test_asof_accessors_ignore_future_chain_quotes_and_uncompleted_candles(tmp_path):
    dataset = load_dataset(make_archive(tmp_path / "archive", future_observations=True))
    as_of = datetime.fromisoformat(SCAN_AT)
    chain = dataset.chain_at(SYMBOL, EXPIRY, as_of, 60)
    assert chain is not None
    assert chain.spot == 100
    assert chain.observed_at <= as_of
    bars = dataset.option_bars_at(f"{SYMBOL}_CE", as_of)
    assert len(bars) == 1
    assert from_epoch(bars[0].ts) + timedelta(minutes=15) == as_of
    quote = dataset.latest_snapshot(
        source="underlying_quote", symbol=SYMBOL, as_of=as_of,
    )
    assert quote is None


def test_baseline_selection_reuses_legacy_atm_itm_functions_and_avwap_rules(tmp_path):
    archive = make_archive(tmp_path / "archive")
    output = tmp_path / "reports"
    result = run_phase7(
        archive,
        output_dir=output,
        config=_config(),
        scenarios=[ThresholdScenario(50, 0.50)],
    )
    selected = {
        row["security_id"] for row in result.baseline.scanner_rows
        if row.get("selected_for_avwap")
    }
    # This fixture's point-in-time chain has a single real strike; the legacy
    # UniverseManager ATM + zero ITM rule therefore selects both actual sides.
    assert selected == {f"{SYMBOL}_CE", f"{SYMBOL}_PE"}
    legacy = UniverseManager(it_count=0)
    legacy.update(
        SYMBOL,
        datetime.fromisoformat(EXPIRY).date(),
        100,
        [100],
        {
            (100.0, "CE"): result.baseline.active_contracts.get(f"{SYMBOL}_CE")
                or load_dataset(archive).catalog.snapshots[0].contracts[0],
            (100.0, "PE"): result.baseline.active_contracts.get(f"{SYMBOL}_PE")
                or load_dataset(archive).catalog.snapshots[0].contracts[1],
        },
    )
    assert legacy.scanner_ids() == selected
    baseline_trades = [row for row in result.baseline.trades if row["status"] == "CLOSED"]
    # The fixture has no historical option quotes. Once the first position is
    # open, the configured max-daily-loss gate blocks another entry whose
    # current risk mark is unavailable.
    assert len(baseline_trades) == 1
    ce_trade = next(row for row in baseline_trades if row["security_id"] == f"{SYMBOL}_CE")
    assert any(
        row["security_id"] == f"{SYMBOL}_PE"
        and "UNPRICED_OPEN_POSITION_DRAWDOWN_GATE" in row["rejection_reasons"]
        for row in result.baseline.rejections
    )
    assert ce_trade["entry_reason"] == "CROSS_BELOW_AVWAP"
    assert ce_trade["exit_reason"] == "CLOSE_ABOVE_AVWAP"
    assert ce_trade["entry_signal_price"] == 101
    assert ce_trade["exit_signal_price"] == 106
    assert ce_trade["reference_pnl_before_slippage"] == -125
    assert ce_trade["net_pnl"] == -125
    assert result.lookahead_validation["status"] == "passed"
    assert result.to_dict()["automatic_winner_declared"] is False
    assert (output / "comparison.csv").is_file()
    assert (output / "threshold_analysis.csv").is_file()
    assert (output / "missed_opportunity_analysis.csv").is_file()
    assert (output / "daily_reports" / "2026-09-15" / "scanner.csv").is_file()
    assert (output / "daily_reports" / "2026-09-15" / "trades.csv").is_file()


def test_spread_sweep_and_missed_liquidity_signals_are_measured(tmp_path):
    result = run_phase7(
        make_archive(tmp_path / "archive"),
        config=_config(),
        scenarios=[
            ThresholdScenario(50, 0.25),
            ThresholdScenario(60, 0.50),
        ],
    )
    strict, loose = result.scenarios
    strict_closed = [row for row in strict.trades if row["status"] == "CLOSED"]
    loose_closed = [row for row in loose.trades if row["status"] == "CLOSED"]
    assert {row["security_id"] for row in strict_closed} == {f"{SYMBOL}_PE"}
    assert {row["security_id"] for row in loose_closed} == {f"{SYMBOL}_CE"}
    assert any(
        row["security_id"] == f"{SYMBOL}_PE"
        and "UNPRICED_OPEN_POSITION_DRAWDOWN_GATE" in row["rejection_reasons"]
        for row in loose.rejections
    )
    strict_missed = [row for row in strict.missed_signals if row["category"] == "LIQUIDITY_REJECTED"]
    assert len(strict_missed) == 1
    assert "WIDE_SPREAD" in strict_missed[0]["rejection_reasons"]
    assert strict_missed[0]["counterfactual_status"] == "COUNTERFACTUAL_CLOSED"
    assert strict_missed[0]["counterfactual_net_pnl"] == -125
    assert len(result.comparison_rows) == 2
    assert all(not row["automatic_winner_declared"] for row in result.comparison_rows)


def test_future_snapshot_and_future_candle_do_not_change_earlier_results(tmp_path):
    base = run_phase7(
        make_archive(tmp_path / "base"),
        config=_config(),
        scenarios=[ThresholdScenario(50, 0.50)],
    )
    future = run_phase7(
        make_archive(tmp_path / "future", future_observations=True),
        config=_config(),
        scenarios=[ThresholdScenario(50, 0.50)],
    )
    def compact(run):
        return [
            (row["security_id"], row["entry_signal_price"], row["exit_signal_price"], row["net_pnl"])
            for row in run.trades
        ]
    assert compact(base.baseline) == compact(future.baseline)
    assert compact(base.scenarios[0]) == compact(future.scenarios[0])
    assert future.lookahead_validation["status"] == "passed"
    assert future.lookahead_validation["market_observations_after_period_end_excluded"] >= 1
    assert future.lookahead_validation["option_candles_completing_after_period_end_excluded"] >= 2


def test_backtest_explicit_quantity_must_be_a_lot_multiple(tmp_path):
    contract = load_dataset(make_archive(tmp_path / "archive")).catalog.snapshots[0].contracts[0]

    assert _quantity_for(contract, {"quantity_per_trade": None, "default_lot_size": 250}) == 25
    assert _quantity_for(contract, {"quantity_per_trade": 50}) == 50
    assert _quantity_for(contract, {"quantity_per_trade": 26}) == 0
    assert _quantity_for(contract, {"quantity_per_trade": 25.5}) == 0


def test_mechanical_exit_uses_fresh_quote_or_recent_real_candle_never_entry_price(tmp_path):
    dataset = load_dataset(make_archive(tmp_path / "archive"))
    backtester = Phase7Backtester(
        dataset, config=_config(), scenarios=[ThresholdScenario(50, 0.50)]
    )
    contract = next(
        item for item in dataset.catalog.snapshots[0].contracts
        if item.security_id == f"{SYMBOL}_CE"
    )
    candle_ts = int(datetime(2026, 9, 15, 14, 45, tzinfo=IST).timestamp())
    dataset.option_candles[contract.security_id].append(Candle(
        contract.security_id, candle_ts, 95.0, 98.0, 94.0, 96.0, 100,
    ))
    # This quote is point-in-time but too old for execution; it must not trump
    # the actual recent candle close.
    dataset.snapshots.append(MarketSnapshot(
        source="option_quote",
        observed_at=datetime(2026, 9, 15, 15, 10, tzinfo=IST),
        security_id=contract.security_id,
        last_price=70.0,
    ))
    at = datetime(2026, 9, 15, 15, 15, tzinfo=IST)
    position = SimulatedPosition(
        contract=contract, quantity=contract.lot_size,
        entry_signal_at=datetime(2026, 9, 15, 14, 0, tzinfo=IST),
        entry_candle_ts=int(datetime(2026, 9, 15, 13, 45, tzinfo=IST).timestamp()),
        entry_signal_price=100.0, entry_fill_price=100.0, entry_avwap=101.0,
        entry_quote={}, entry_spread_percent=None, entry_quote_age_seconds=None,
        entry_slippage_cost=0.0, entry_transaction_cost=0.0,
    )
    run = ArmResult(name="baseline", scenario=None)
    run.open_positions[contract.security_id] = position

    backtester._mechanical_exit(run, at)

    assert contract.security_id not in run.open_positions
    trade = run.trades[0]
    assert trade["exit_reason"] == "INTRADAY_EXIT_15_15"
    assert trade["exit_signal_price"] == 96.0
    assert trade["exit_mark_source"] == "completed_option_candle_fallback"
    assert trade["exit_mark_observed_at"] == "2026-09-15T15:00:00+05:30"
    assert trade["exit_quote_age_seconds"] is None
    assert trade["exit_mark_age_seconds"] == 900.0


def test_mechanical_exit_remains_open_when_no_fresh_reference_exists(tmp_path):
    dataset = load_dataset(make_archive(tmp_path / "archive"))
    backtester = Phase7Backtester(
        dataset, config=_config(), scenarios=[ThresholdScenario(50, 0.50)]
    )
    contract = next(
        item for item in dataset.catalog.snapshots[0].contracts
        if item.security_id == f"{SYMBOL}_CE"
    )
    position = SimulatedPosition(
        contract=contract, quantity=contract.lot_size,
        entry_signal_at=datetime(2026, 9, 15, 9, 30, tzinfo=IST),
        entry_candle_ts=int(datetime(2026, 9, 15, 9, 15, tzinfo=IST).timestamp()),
        entry_signal_price=100.0, entry_fill_price=101.0, entry_avwap=101.0,
        entry_quote={}, entry_spread_percent=None, entry_quote_age_seconds=None,
        entry_slippage_cost=0.0, entry_transaction_cost=0.0,
    )
    run = ArmResult(name="baseline", scenario=None)
    run.open_positions[contract.security_id] = position

    backtester._mechanical_exit(run, datetime(2026, 9, 15, 15, 15, tzinfo=IST))

    assert run.open_positions[contract.security_id] is position
    assert run.trades == []


def test_replay_matches_app_same_timestamp_order_candle_before_scanner(tmp_path, monkeypatch):
    dataset = load_dataset(make_archive(tmp_path / "archive"))
    backtester = Phase7Backtester(
        dataset, config=_config(), scenarios=[ThresholdScenario(50, 0.50)]
    )
    events = []
    monkeypatch.setattr(
        backtester, "_process_candle",
        lambda run, item: events.append(("candle", run.name, item.available_at)),
    )
    monkeypatch.setattr(
        backtester, "_baseline_scan",
        lambda at, scan, run: events.append(("baseline_scan", run.name, at)) or {},
    )
    monkeypatch.setattr(backtester, "_apply_baseline_selection", lambda *args: None)
    monkeypatch.setattr(
        backtester, "_new_scan",
        lambda run, scan, at: events.append(("new_scan", run.name, at)),
    )
    monkeypatch.setattr(backtester, "_finalize_open_positions", lambda *args: None)

    backtester.run()

    at = datetime.fromisoformat(SCAN_AT)
    same_timestamp = [event[0] for event in events if event[2] == at]
    assert same_timestamp[:4] == ["candle"] * 4
    assert same_timestamp[4:] == ["baseline_scan", "new_scan"]


def test_spread_sweep_defaults_are_exact_16_combinations():
    scenarios = default_threshold_scenarios()
    assert len(scenarios) == 16
    assert {item.liquidity_score_min for item in scenarios} == {50, 60, 70, 80}
    assert {item.max_spread_percent for item in scenarios} == {0.25, 0.50, 0.75, 1.00}


def test_loader_rejects_timezone_naive_market_timestamps(tmp_path):
    root = make_archive(tmp_path / "archive")
    rows = list(csv.DictReader((root / "scan_events.csv").open(encoding="utf-8")))
    rows[0]["as_of"] = "2026-09-15T09:30:00"
    _write_csv(root / "scan_events.csv", rows, ["scan_id", "as_of"])
    with pytest.raises(BacktestDataError, match="no timezone"):
        load_dataset(root)
