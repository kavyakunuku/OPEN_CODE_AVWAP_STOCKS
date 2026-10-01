"""Point-in-time, paper-only Phase 7 AVWAP backtest replay.

The replay imports the production AVWAP state/rules and the same Phase 2–5
selection components as the scanner. It has no broker, Dhan client, or order
submission integration.
"""
from __future__ import annotations

import copy
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence

from common.config import DEFAULTS
from common.models import Candle, ENTRY_SELL, OptionContract
from common.utils import IST, SESSION_CLOSE, SESSION_OPEN, from_epoch
from market.contract_discovery import MAX_ITM_STRIKES, OptionContractDiscovery
from market.liquidity import (
    REASON_CODES as LIQUIDITY_REASON_CODES,
    ContractLiquidityData,
    LiquidityObservation,
    OptionChainLiquidityReport,
    OptionLiquidityEngine,
)
from market.opportunity import OpportunityRankingEngine, OpportunityScannerInput
from market.universe import ce_universe_strikes, find_atm, pe_universe_strikes
from market.volatility import (
    ImpliedVolatilityObservation,
    OHLCBar,
    OptionVolatilityInput,
    PriceObservation,
    UnderlyingVolatilityInput,
    VolatilityAnalysisEngine,
)
from strategy import rules
from strategy.avwap import AvwapState

from backtest.data import (
    BacktestDataError,
    MarketSnapshot,
    Phase7Dataset,
    ScanEvent,
    load_dataset,
)

LIQUIDITY_SCORE_THRESHOLDS = (50, 60, 70, 80)
MAX_SPREAD_THRESHOLDS = (0.25, 0.50, 0.75, 1.00)
DEFAULT_BACKTEST_SETTINGS = {
    "fill_mode": "candle_close",
    "slippage_bps": 0.0,
    "transaction_cost_per_order": 0.0,
    "transaction_cost_bps": 0.0,
    "max_input_age_seconds": 60.0,
    "max_exit_candle_age_seconds": 1800.0,
}


class BacktestError(ValueError):
    """The replay cannot proceed without violating its data contract."""


@dataclass(frozen=True)
class ThresholdScenario:
    liquidity_score_min: int
    max_spread_percent: float

    @property
    def key(self) -> str:
        spread = f"{self.max_spread_percent:.2f}".replace(".", "")
        return f"liq_{self.liquidity_score_min}_spread_{spread}"

    def to_dict(self) -> dict:
        return {
            "scenario": self.key,
            "liquidity_score_min": self.liquidity_score_min,
            "max_spread_percent": self.max_spread_percent,
        }


def default_threshold_scenarios() -> tuple[ThresholdScenario, ...]:
    return tuple(
        ThresholdScenario(score, spread)
        for score in LIQUIDITY_SCORE_THRESHOLDS
        for spread in MAX_SPREAD_THRESHOLDS
    )


@dataclass(frozen=True)
class ReplayCandle:
    security_id: str
    candle: Candle
    available_at: datetime
    previous_close: Optional[float]
    previous_avwap: Optional[float]
    avwap: Optional[float]
    entry_cross: bool
    exit_close: bool
    anchor_verified: bool


@dataclass
class SimulatedPosition:
    contract: OptionContract
    quantity: int
    entry_signal_at: datetime
    entry_candle_ts: int
    entry_signal_price: float
    entry_fill_price: float
    entry_avwap: Optional[float]
    entry_quote: dict
    entry_spread_percent: Optional[float]
    entry_quote_age_seconds: Optional[float]
    entry_slippage_cost: float
    entry_transaction_cost: float
    entry_reason: str = rules.ENTRY_REASON


@dataclass
class ShadowPosition:
    contract: OptionContract
    quantity: int
    entry_signal_at: datetime
    entry_candle_ts: int
    entry_signal_price: float
    entry_fill_price: float
    entry_avwap: Optional[float]
    rejection_reason: str
    entry_spread_percent: Optional[float]
    entry_quote_age_seconds: Optional[float]
    entry_slippage_cost: float
    entry_transaction_cost: float


@dataclass
class ArmResult:
    name: str
    scenario: Optional[ThresholdScenario]
    scanner_rows: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    rejections: list[dict] = field(default_factory=list)
    missed_signals: list[dict] = field(default_factory=list)
    shadow_trades: list[dict] = field(default_factory=list)
    open_positions: dict[str, SimulatedPosition] = field(default_factory=dict)
    shadow_positions: dict[str, ShadowPosition] = field(default_factory=dict)
    active_contracts: dict[str, OptionContract] = field(default_factory=dict)
    latest_candidate_status: dict[str, dict] = field(default_factory=dict)
    latest_scan_at: Optional[datetime] = None
    entry_count_by_day: Counter = field(default_factory=Counter)
    realized_gross_by_day: Counter = field(default_factory=Counter)
    entry_signal_count: int = 0
    executed_entry_count: int = 0
    last_ranked: dict[str, dict] = field(default_factory=dict)
    _seen_signal_keys: set[str] = field(default_factory=set)

    @property
    def label(self) -> str:
        return self.scenario.key if self.scenario else "baseline_legacy_selector"


@dataclass
class ReplayAudit:
    decisions: int = 0
    contract_master_asof_checks: int = 0
    market_observation_asof_checks: int = 0
    option_candle_completion_checks: int = 0
    underlying_candle_completion_checks: int = 0
    future_contract_master_snapshots_excluded: int = 0
    future_market_observations_excluded: int = 0
    future_option_candles_excluded: int = 0
    future_underlying_candles_excluded: int = 0
    candle_evaluations: int = 0
    violations: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "status": "passed" if self.passed else "failed",
            "decisions": self.decisions,
            "contract_master_asof_checks": self.contract_master_asof_checks,
            "market_observation_asof_checks": self.market_observation_asof_checks,
            "option_candle_completion_checks": self.option_candle_completion_checks,
            "underlying_candle_completion_checks": self.underlying_candle_completion_checks,
            "candle_evaluations": self.candle_evaluations,
            "contract_master_snapshots_after_period_end_excluded": self.future_contract_master_snapshots_excluded,
            "market_observations_after_period_end_excluded": self.future_market_observations_excluded,
            "option_candles_completing_after_period_end_excluded": self.future_option_candles_excluded,
            "underlying_candles_completing_after_period_end_excluded": self.future_underlying_candles_excluded,
            "look_ahead_violations": list(self.violations),
            "validated_fields": [
                "contract-master capture timestamp",
                "chain and option/equity quote observation timestamp",
                "option and underlying 15-minute candle completion time",
                "liquidity volume, OI, spread, depth, consistency, and score inputs",
                "volatility price/IV inputs and derived features",
                "opportunity-ranking inputs",
            ],
        }


@dataclass
class Phase7Result:
    data_root: str
    output_dir: Optional[str]
    period_start: datetime
    period_end: datetime
    symbols: tuple[str, ...]
    assumptions: dict
    data_quality: dict
    lookahead_validation: dict
    baseline: ArmResult
    scenarios: list[ArmResult]
    comparison_rows: list[dict]
    threshold_rows: list[dict]
    missed_analysis_rows: list[dict]

    def to_dict(self) -> dict:
        return {
            "status": "completed",
            "data_root": self.data_root,
            "output_dir": self.output_dir,
            "period": {"start": self.period_start.isoformat(), "end": self.period_end.isoformat()},
            "symbols": list(self.symbols),
            "assumptions": self.assumptions,
            "data_quality": self.data_quality,
            "lookahead_validation": self.lookahead_validation,
            "baseline": summarize_arm(self.baseline),
            "threshold_sweep": [summarize_arm(arm) for arm in self.scenarios],
            "comparison": self.comparison_rows,
            "threshold_analysis": self.threshold_rows,
            "missed_opportunity_analysis": self.missed_analysis_rows,
            "automatic_winner_declared": False,
        }


class Phase7Backtester:
    """Replay the existing selector and Phase 2–5 scanner on one archive."""

    def __init__(
        self,
        dataset: Phase7Dataset,
        *,
        config: Optional[dict] = None,
        scenarios: Optional[Sequence[ThresholdScenario]] = None,
    ):
        if not isinstance(dataset, Phase7Dataset):
            raise TypeError("dataset must be a Phase7Dataset")
        if not dataset.quality.ready:
            raise BacktestError("dataset has blocking data-quality errors")
        self.dataset = dataset
        self.config = _deep_merge(copy.deepcopy(DEFAULTS), config or {})
        self.settings = _backtest_settings(self.config)
        self._validate_universe()
        self.scenarios = tuple(scenarios or default_threshold_scenarios())
        self._validate_scenarios(self.scenarios)
        self.itm_count = _int_setting(
            self.config.get("strategy", {}).get("itm_strikes_per_side", 6),
            "strategy.itm_strikes_per_side", minimum=0, maximum=MAX_ITM_STRIKES,
        )
        self.expiry_switch_day = _int_setting(
            self.config.get("strategy", {}).get("expiry_switch_day", 24),
            "strategy.expiry_switch_day", minimum=1, maximum=31,
        )
        self.risk_cfg = copy.deepcopy(self.config.get("risk", {}))
        self.discovery = OptionContractDiscovery(
            dataset.catalog,
            validated_symbols=dataset.symbols,
            expiry_switch_day=self.expiry_switch_day,
        )
        self.volatility_engine = VolatilityAnalysisEngine(self.config)
        self._scenario_rankers: dict[str, OpportunityRankingEngine] = {}
        self._spread_liquidity_engines: dict[float, OptionLiquidityEngine] = {}
        self._scenario_configs: dict[str, dict] = {}
        for scenario in self.scenarios:
            scenario_cfg = self._config_for_scenario(scenario)
            self._scenario_configs[scenario.key] = scenario_cfg
            self._scenario_rankers[scenario.key] = OpportunityRankingEngine(scenario_cfg)
            if scenario.max_spread_percent not in self._spread_liquidity_engines:
                self._spread_liquidity_engines[scenario.max_spread_percent] = OptionLiquidityEngine(
                    _liquidity_config_for_spread(self.config, scenario.max_spread_percent)
                )
        self.audit = ReplayAudit()

    def _validate_universe(self) -> None:
        settings = self.config.get("backtest", {})
        if settings.get("allow_non_20_symbol_universe_for_diagnostics", False):
            return
        if len(self.dataset.symbols) != 20:
            raise BacktestError(
                f"Phase 7 requires the configured 20-stock universe; archive has {len(self.dataset.symbols)} symbols. "
                "Use a complete archive or explicitly opt into a diagnostic-only subset."
            )
        expected = settings.get("expected_symbols") or _configured_phase1_symbols()
        if expected:
            expected = tuple(dict.fromkeys(str(s).strip().upper() for s in expected if str(s).strip()))
            if set(expected) != set(self.dataset.symbols):
                missing = sorted(set(expected) - set(self.dataset.symbols))
                unexpected = sorted(set(self.dataset.symbols) - set(expected))
                raise BacktestError(
                    "archive manifest.symbols does not match the Phase 1 universe "
                    f"(missing={missing}, unexpected={unexpected})"
                )

    @staticmethod
    def _validate_scenarios(scenarios: Sequence[ThresholdScenario]) -> None:
        if not scenarios:
            raise BacktestError("at least one new-system threshold scenario is required")
        keys = set()
        for item in scenarios:
            if not isinstance(item, ThresholdScenario):
                raise BacktestError("scenarios must be ThresholdScenario values")
            if isinstance(item.liquidity_score_min, bool) or not 0 <= int(item.liquidity_score_min) <= 100:
                raise BacktestError("liquidity score thresholds must be from 0 through 100")
            if not math.isfinite(float(item.max_spread_percent)) or item.max_spread_percent <= 0:
                raise BacktestError("maximum spread thresholds must be positive and finite")
            if item.key in keys:
                raise BacktestError(f"duplicate threshold scenario {item.key}")
            keys.add(item.key)

    def _config_for_scenario(self, scenario: ThresholdScenario) -> dict:
        cfg = copy.deepcopy(self.config)
        liquidity = cfg.setdefault("liquidity", {})
        liquidity["max_spread_percent"] = float(scenario.max_spread_percent)
        liquidity["preferred_spread_percent"] = min(
            float(liquidity.get("preferred_spread_percent", scenario.max_spread_percent)),
            float(scenario.max_spread_percent),
        )
        liquidity.setdefault("minimums", {})["liquidity_score"] = None
        execution = cfg.setdefault("opportunity", {}).setdefault("execution", {})
        execution["max_spread_percent"] = float(scenario.max_spread_percent)
        execution["preferred_spread_percent"] = min(
            float(execution.get("preferred_spread_percent", scenario.max_spread_percent)),
            float(scenario.max_spread_percent),
        )
        return cfg

    def run(self, output_dir: Optional[str | Path] = None) -> Phase7Result:
        scan_events = sorted(self.dataset.scan_events, key=lambda event: event.as_of)
        if not scan_events:
            raise BacktestError("no scan events are available for the replay period")
        first_scan, last_scan = scan_events[0].as_of, scan_events[-1].as_of
        period_start = _manifest_time(self.dataset.manifest, "period_start") or first_scan
        period_end = _manifest_time(self.dataset.manifest, "period_end") or last_scan
        if period_start > first_scan or period_end < last_scan or period_end < period_start:
            raise BacktestError("manifest period_start/period_end must contain every scan event and be ordered")
        self._validate_event_rows(scan_events, period_start, period_end)

        candle_events = self._build_candle_events(first_scan, period_end)
        timeline = self._timeline(scan_events, candle_events, first_scan, period_end)
        baseline = ArmResult(name="baseline", scenario=None)
        new_runs = [ArmResult(name="new_system", scenario=scenario) for scenario in self.scenarios]
        scan_map: dict[datetime, list[ScanEvent]] = defaultdict(list)
        for event in scan_events:
            scan_map[event.as_of].append(event)

        for at in sorted(timeline):
            # Match app._tick: mechanical exit, completed-candle processing,
            # then scanner/universe refresh. Entries at or after 15:15 are
            # separately blocked in _process_candle, as in the paper app.
            if at.time() == time(15, 15):
                self._mechanical_exit(baseline, at)
                for run in new_runs:
                    self._mechanical_exit(run, at)
            for item in candle_events.get(at, ()):
                self._process_candle(baseline, item)
                for run in new_runs:
                    self._process_candle(run, item)
            if at in scan_map:
                for scan in scan_map[at]:
                    self.audit.decisions += 1
                    self._assert_master_asof(at)
                    selection = self._baseline_scan(at, scan, baseline)
                    self._apply_baseline_selection(baseline, scan, selection)
                    for run in new_runs:
                        self._new_scan(run, scan, at)

        self._finalize_open_positions(baseline, period_end)
        for run in new_runs:
            self._finalize_open_positions(run, period_end)
        self.audit.future_contract_master_snapshots_excluded = sum(
            1 for snapshot in self.dataset.catalog.snapshots if snapshot.as_of > period_end
        )
        self.audit.future_market_observations_excluded = sum(
            1 for row in self.dataset.snapshots if row.observed_at > period_end
        )
        self.audit.future_option_candles_excluded = sum(
            1 for rows in self.dataset.option_candles.values() for bar in rows
            if from_epoch(bar.ts) + timedelta(minutes=15) > period_end
        )
        self.audit.future_underlying_candles_excluded = sum(
            1 for rows in self.dataset.underlying_candles.values() for bar in rows
            if from_epoch(bar.ts) + timedelta(minutes=15) > period_end
        )
        self.audit.candle_evaluations = sum(len(rows) for rows in candle_events.values())
        if not self.audit.passed:
            raise BacktestError("look-ahead validation failed; refusing to write comparison reports")

        comparison, threshold_rows, missed_rows = _analysis_tables(baseline, new_runs)
        result = Phase7Result(
            data_root=str(self.dataset.root),
            output_dir=str(output_dir) if output_dir is not None else None,
            period_start=period_start,
            period_end=period_end,
            symbols=self.dataset.symbols,
            assumptions=self._assumptions(),
            data_quality=self._final_data_quality(),
            lookahead_validation=self.audit.to_dict(),
            baseline=baseline,
            scenarios=new_runs,
            comparison_rows=comparison,
            threshold_rows=threshold_rows,
            missed_analysis_rows=missed_rows,
        )
        if output_dir is not None:
            from backtest.reports import write_reports
            write_reports(result, output_dir)
        return result

    def _validate_event_rows(self, events, period_start, period_end) -> None:
        timestamps = [event.as_of for event in events]
        if len(timestamps) != len(set(timestamps)):
            raise BacktestError("scan_events.csv must have one row per scanner replay timestamp")
        if any(not period_start <= event.as_of <= period_end for event in events):
            raise BacktestError("scan event falls outside manifest period_start/period_end")

    def _assert_master_asof(self, as_of: datetime) -> None:
        snapshot = self.dataset.catalog.snapshot_for(as_of)
        self.audit.contract_master_asof_checks += 1
        if snapshot.as_of > as_of:
            self.audit.violations.append({
                "code": "FUTURE_CONTRACT_MASTER",
                "as_of": as_of.isoformat(),
                "snapshot_at": snapshot.as_of.isoformat(),
            })

    def _build_candle_events(self, first_scan: datetime, period_end: datetime):
        """Build AVWAP sequentially from the production state object."""
        events: dict[datetime, list[ReplayCandle]] = defaultdict(list)
        for sid, rows in sorted(self.dataset.option_candles.items()):
            state = AvwapState(security_id=sid)
            anchor_verified = sid in self.dataset.anchor_confirmed
            for candle in sorted(rows, key=lambda bar: bar.ts):
                available_at = from_epoch(candle.ts) + timedelta(minutes=15)
                self.audit.option_candle_completion_checks += 1
                if available_at > period_end:
                    continue
                previous_close, previous_avwap = state.last_close, state.last_avwap
                avwap = state.update(candle)
                if available_at < first_scan:
                    continue
                events[available_at].append(ReplayCandle(
                    security_id=sid,
                    candle=candle,
                    available_at=available_at,
                    previous_close=previous_close,
                    previous_avwap=previous_avwap,
                    avwap=avwap,
                    entry_cross=anchor_verified and rules.is_entry_cross(
                        previous_close, previous_avwap, candle.close, avwap
                    ),
                    exit_close=anchor_verified and rules.is_exit_close(candle.close, avwap),
                    anchor_verified=anchor_verified,
                ))
        for rows in events.values():
            rows.sort(key=lambda item: item.security_id)
        return events

    @staticmethod
    def _timeline(scan_events, candle_events, first_scan, period_end) -> set[datetime]:
        values = {event.as_of for event in scan_events}
        values.update(at for at in candle_events if first_scan <= at <= period_end)
        session_days = {at.date() for at in values
                        if at.weekday() < 5 and SESSION_OPEN <= at.time() <= SESSION_CLOSE}
        for day in session_days:
            forced = datetime.combine(day, time(15, 15), tzinfo=IST)
            if first_scan <= forced <= period_end:
                values.add(forced)
        return values

    def _baseline_scan(self, as_of: datetime, scan: ScanEvent, run: ArmResult) -> dict:
        selected_by_symbol: dict[str, dict[str, OptionContract]] = {}
        row_meta: dict[str, dict] = {}
        errors: dict[str, str] = {}
        for symbol in self.dataset.symbols:
            spot = self._point_in_time_spot(symbol, as_of)
            if spot is None:
                errors[symbol] = "NO_ASOF_UNDERLYING_PRICE"
                continue
            try:
                discovery = self.discovery.discover_option_contracts(
                    symbol, as_of, max_itm=self.itm_count, underlying_price=spot[0]
                )
            except Exception as exc:
                errors[symbol] = _discovery_error_code(exc)
                continue
            chain = self.dataset.chain_at(
                symbol, discovery.expiry.isoformat(), as_of,
                self.settings["max_input_age_seconds"],
            )
            if chain is None:
                errors[symbol] = "NO_FRESH_ASOF_CHAIN_FOR_SELECTED_EXPIRY"
                continue
            if chain.observed_at and chain.observed_at > as_of:
                self.audit.violations.append({
                    "code": "FUTURE_CHAIN_USED", "symbol": symbol,
                    "as_of": as_of.isoformat(), "observed_at": chain.observed_at.isoformat(),
                })
                continue
            snapshot = self.dataset.catalog.snapshot_for(as_of)
            master = {
                (_strike_key(contract.strike), contract.option_type.upper()): contract
                for contract in snapshot.contracts
                if contract.underlying.upper() == symbol and contract.expiry == discovery.expiry.isoformat()
            }
            atm = find_atm(spot[0], chain.strikes)
            if atm is None:
                errors[symbol] = "NO_ASOF_CHAIN_STRIKES"
                continue
            selected: dict[str, OptionContract] = {}
            for side, strikes in (("CE", ce_universe_strikes(atm, chain.strikes, self.itm_count)),
                                  ("PE", pe_universe_strikes(atm, chain.strikes, self.itm_count))):
                for level, strike in enumerate(strikes):
                    contract = master.get((_strike_key(strike), side))
                    meta = {
                        "symbol": symbol, "strike": strike, "option_type": side,
                        "moneyness": "ATM" if level == 0 else f"ITM{level}",
                    }
                    if contract is None:
                        row_meta[f"{symbol}|{strike:g}|{side}"] = {
                            **meta,
                            "status": "REJECTED_MISSING_MASTER_CONTRACT",
                            "rejection_reasons": ["MISSING_ASOF_MASTER_CONTRACT"],
                        }
                        continue
                    selected[contract.security_id] = contract
                    row_meta[contract.security_id] = {
                        **meta,
                        "security_id": contract.security_id,
                        "expiry": contract.expiry,
                        "status": "SELECTED_BASELINE",
                        "rejection_reasons": [],
                        "spot": spot[0],
                        "spot_observed_at": spot[1].isoformat(),
                        "chain_observed_at": chain.observed_at.isoformat() if chain.observed_at else None,
                        "atm_strike": atm,
                    }
            selected_by_symbol[symbol] = selected

        rows = []
        for symbol in self.dataset.symbols:
            if symbol in errors:
                rows.append(self._scanner_base_row(
                    run, scan, as_of, symbol=symbol, status="NO_SELECTION",
                    rejection_reasons=[errors[symbol]],
                ))
            for key, meta in row_meta.items():
                if meta.get("symbol") != symbol:
                    continue
                sid = key if key in selected_by_symbol.get(symbol, {}) else None
                anchor = sid in self.dataset.anchor_confirmed if sid else None
                rows.append(self._scanner_base_row(
                    run, scan, as_of, **meta, anchor_verified=anchor,
                ))
        run.scanner_rows.extend(rows)
        return {"contracts": selected_by_symbol, "errors": errors}

    def _scanner_base_row(self, run, scan, as_of, **values) -> dict:
        return {
            "date": as_of.date().isoformat(),
            "scan_id": scan.scan_id,
            "scan_at": as_of.isoformat(),
            "arm": run.label,
            "symbol": values.get("symbol"),
            "security_id": values.get("security_id"),
            "expiry": values.get("expiry"),
            "strike": values.get("strike"),
            "option_type": values.get("option_type"),
            "moneyness": values.get("moneyness"),
            "rank": None,
            "liquidity_threshold": None,
            "max_spread_percent": None,
            "liquidity_score": None,
            "volatility_score": None,
            "chain_quality_score": None,
            "execution_quality_score": None,
            "opportunity_score": None,
            "spread_percent": None,
            "quote_age_seconds": None,
            "selected_for_avwap": False,
            "anchor_verified": values.get("anchor_verified"),
            "status": values.get("status", "SELECTED_BASELINE"),
            "rejection_reasons": values.get("rejection_reasons", []),
            "missing_components": [],
            "spot": values.get("spot"),
            "spot_observed_at": values.get("spot_observed_at"),
            "chain_observed_at": values.get("chain_observed_at"),
        }

    def _append_scanner_rejections(self, run: ArmResult, rows: Sequence[dict]) -> None:
        for row in rows:
            reasons = row.get("rejection_reasons") or [row.get("status", "NOT_SELECTED")]
            run.rejections.append({
                "date": row.get("date"), "arm": run.label,
                "event_at": row.get("scan_at"), "event_type": "SCANNER_SELECTION",
                "symbol": row.get("symbol"), "security_id": row.get("security_id"),
                "expiry": row.get("expiry"), "strike": row.get("strike"),
                "option_type": row.get("option_type"), "category": row.get("status", "NOT_SELECTED"),
                "rejection_reasons": list(reasons), "signal_price": None, "avwap": None,
                "latest_scan_at": row.get("scan_at"),
            })

    def _apply_baseline_selection(self, run: ArmResult, scan: ScanEvent, result: dict) -> None:
        run.active_contracts = {
            sid: contract for rows in result["contracts"].values()
            for sid, contract in rows.items()
        }
        run.latest_candidate_status = {
            sid: {"status": "SELECTED_BASELINE", "rejection_reasons": []}
            for sid in run.active_contracts
        }
        run.latest_scan_at = scan.as_of
        for row in reversed(run.scanner_rows):
            if row.get("scan_id") != scan.scan_id:
                break
            sid = row.get("security_id")
            if sid in run.active_contracts:
                row["selected_for_avwap"] = True
                row["status"] = "SELECTED_BASELINE" if sid in self.dataset.anchor_confirmed else "SELECTED_ANCHOR_UNVERIFIED"
                if sid not in self.dataset.anchor_confirmed:
                    row["rejection_reasons"] = ["OPTION_AVWAP_ANCHOR_UNVERIFIED"]
        self._append_scanner_rejections(run, [
            row for row in run.scanner_rows
            if row.get("scan_id") == scan.scan_id and not row.get("selected_for_avwap")
        ])

    def _new_scan(self, run: ArmResult, scan: ScanEvent, as_of: datetime) -> None:
        scenario = run.scenario
        if scenario is None:
            raise BacktestError("new-system run has no threshold scenario")
        cfg = self._scenario_configs[scenario.key]
        discoveries = []
        spot_by_symbol = {}
        errors = {}
        for symbol in self.dataset.symbols:
            spot = self._point_in_time_spot(symbol, as_of)
            if spot is None:
                errors[symbol] = "NO_ASOF_UNDERLYING_PRICE"
                continue
            spot_by_symbol[symbol] = spot
            try:
                discovery = self.discovery.discover_option_contracts(
                    symbol, as_of, max_itm=self.itm_count, underlying_price=spot[0]
                )
            except Exception as exc:
                errors[symbol] = _discovery_error_code(exc)
                continue
            if discovery.snapshot_as_of > as_of:
                self.audit.violations.append({
                    "code": "FUTURE_DISCOVERY_MASTER", "symbol": symbol,
                    "as_of": as_of.isoformat(), "snapshot_at": discovery.snapshot_as_of.isoformat(),
                })
                continue
            discoveries.append(discovery)

        liquidity_engine = self._spread_liquidity_engines[scenario.max_spread_percent]
        liquidity_inputs = []
        for discovery in discoveries:
            for contract in discovery.contracts:
                observations = self._liquidity_observations(contract.security_id, as_of)
                bars = self.dataset.option_bars_at(contract.security_id, as_of)
                self.audit.option_candle_completion_checks += len(bars)
                if any(from_epoch(bar.ts) + timedelta(minutes=15) > as_of for bar in bars):
                    self.audit.violations.append({
                        "code": "FUTURE_OPTION_BAR_IN_LIQUIDITY",
                        "security_id": contract.security_id, "as_of": as_of.isoformat(),
                    })
                liquidity_inputs.append(ContractLiquidityData(
                    contract=contract, observations=tuple(observations), candles=tuple(bars),
                ))
        raw_report = liquidity_engine.score_option_chain(liquidity_inputs, as_of)
        liquidity_report = _apply_score_threshold(raw_report, scenario.liquidity_score_min)
        liquidity_by_id = liquidity_report.by_security_id()
        actual_chain_keys = {
            (discovery.underlying.upper(), discovery.expiry.isoformat())
            for discovery in discoveries
            if self.dataset.chain_at(
                discovery.underlying, discovery.expiry.isoformat(), as_of,
                self.settings["max_input_age_seconds"],
            ) is not None
        }
        liquidity_report = OptionChainLiquidityReport(
            contracts=liquidity_report.contracts,
            chains=tuple(chain for chain in liquidity_report.chains
                         if (chain.underlying.upper(), chain.expiry) in actual_chain_keys),
        )
        qualified_ids = {
            item.security_id for item in liquidity_report.contracts if item.qualified
        }
        volatility_by_symbol = {}
        for discovery in discoveries:
            symbol = discovery.underlying
            underlying_bars = self.dataset.underlying_bars_at(symbol, as_of)
            self.audit.underlying_candle_completion_checks += len(underlying_bars)
            if not underlying_bars:
                continue
            option_inputs = []
            for contract in discovery.contracts:
                option_bars = self.dataset.option_bars_at(contract.security_id, as_of)
                snapshots = self._contract_snapshots(contract.security_id, as_of)
                option_inputs.append(OptionVolatilityInput(
                    contract=contract,
                    bars_15m=tuple(_to_ohlc(bar) for bar in option_bars),
                    price_observations=tuple(
                        PriceObservation(row.observed_at, row.last_price)
                        for row in snapshots if row.last_price is not None
                    ),
                    implied_volatility_observations=tuple(
                        ImpliedVolatilityObservation(row.observed_at, row.implied_volatility)
                        for row in snapshots if row.source == "chain" and row.implied_volatility is not None
                    ),
                    liquidity_qualified=contract.security_id in qualified_ids,
                ))
            volatility_by_symbol[symbol] = self.volatility_engine.analyze(
                UnderlyingVolatilityInput(
                    symbol=symbol,
                    daily_bars=(),
                    bars_15m=tuple(_to_ohlc(bar) for bar in underlying_bars),
                    price_observations=self._underlying_price_observations(symbol, as_of),
                    options=tuple(option_inputs),
                ),
                as_of,
            )

        open_underlyings = sorted({p.contract.underlying.upper() for p in run.open_positions.values()})
        ranking = self._scenario_rankers[scenario.key].rank(OpportunityScannerInput(
            as_of=as_of,
            discoveries=tuple(discoveries),
            liquidity_report=liquidity_report,
            volatility_by_underlying=volatility_by_symbol,
            existing_position_underlyings=tuple(open_underlyings),
            existing_position_security_ids=tuple(run.open_positions),
        ))
        ranked_by_id = {item.security_id: item for item in ranking.contracts}
        selected_by_id = {item.security_id: item for item in ranking.top_opportunities}
        excluded_by_id = {item.security_id: item for item in ranking.excluded_contracts}
        candidate_status = {}
        rows = []
        for discovery in discoveries:
            for contract in discovery.contracts:
                sid = contract.security_id
                liquidity = liquidity_by_id.get(sid)
                ranked = ranked_by_id.get(sid)
                excluded = excluded_by_id.get(sid)
                reasons = list(liquidity.rejection_reasons) if liquidity else ["MISSING_LIQUIDITY_RESULT"]
                if excluded:
                    reasons = list(dict.fromkeys(reasons + list(excluded.reasons)
                                                 + list(excluded.liquidity_rejection_reasons)))
                    status = "REJECTED_LIQUIDITY" if "LIQUIDITY_NOT_QUALIFIED" in excluded.reasons else "EXCLUDED_PHASE5"
                elif ranked and sid in selected_by_id:
                    status = "TOP_OPPORTUNITY"
                elif ranked:
                    status = "RANKED_NOT_TOP"
                elif liquidity and liquidity.qualified:
                    status = "RANKING_TRUNCATED"
                    reasons = ["NOT_IN_SELECTED_STOCKS_OR_CONTRACT_LIMIT"]
                else:
                    status = "EXCLUDED_PHASE5"
                    reasons = reasons or ["NOT_SELECTED_BY_PHASE5"]
                score_data = ranked or selected_by_id.get(sid)
                if score_data:
                    candidate_status[sid] = {
                        "status": "TOP_OPPORTUNITY" if sid in selected_by_id else "RANKED_NOT_TOP",
                        "rejection_reasons": [], "liquidity_score": score_data.liquidity_score,
                    }
                else:
                    candidate_status[sid] = {
                        "status": status, "rejection_reasons": reasons,
                        "liquidity_score": liquidity.score if liquidity else None,
                    }
                vol = volatility_by_symbol.get(contract.underlying)
                rows.append({
                    "date": as_of.date().isoformat(), "scan_id": scan.scan_id,
                    "scan_at": as_of.isoformat(), "arm": run.label,
                    "symbol": contract.underlying, "security_id": sid, "expiry": contract.expiry,
                    "strike": contract.strike, "option_type": contract.option_type,
                    "moneyness": _discovery_moneyness(discovery, sid),
                    "rank": ranked.rank if ranked else None,
                    "liquidity_threshold": scenario.liquidity_score_min,
                    "max_spread_percent": scenario.max_spread_percent,
                    "liquidity_score": liquidity.score if liquidity else None,
                    "volatility_score": vol.volatility_score if vol else None,
                    "chain_quality_score": score_data.chain_quality_score if score_data else None,
                    "execution_quality_score": score_data.execution_quality_score if score_data else None,
                    "opportunity_score": score_data.opportunity_score if score_data else None,
                    "spread_percent": liquidity.spread_percent if liquidity else None,
                    "quote_age_seconds": liquidity.quote_age_seconds if liquidity else None,
                    "selected_for_avwap": sid in selected_by_id,
                    "anchor_verified": sid in self.dataset.anchor_confirmed,
                    "status": status if sid in self.dataset.anchor_confirmed else f"{status}_ANCHOR_UNVERIFIED",
                    "rejection_reasons": reasons if score_data is None else [],
                    "missing_components": list(score_data.missing_components) if score_data else [],
                    "spot": spot_by_symbol.get(contract.underlying, (None, None))[0],
                    "spot_observed_at": (spot_by_symbol[contract.underlying][1].isoformat()
                                          if contract.underlying in spot_by_symbol else None),
                    "chain_observed_at": _chain_observed_at(
                        self.dataset, contract.underlying, contract.expiry, as_of,
                        self.settings["max_input_age_seconds"],
                    ),
                })
        for symbol, reason in errors.items():
            rows.append({
                "date": as_of.date().isoformat(), "scan_id": scan.scan_id,
                "scan_at": as_of.isoformat(), "arm": run.label, "symbol": symbol,
                "selected_for_avwap": False, "status": "NO_DISCOVERY",
                "rejection_reasons": [reason],
                "liquidity_threshold": scenario.liquidity_score_min,
                "max_spread_percent": scenario.max_spread_percent,
            })
        run.scanner_rows.extend(rows)
        self._append_scanner_rejections(run, [row for row in rows if not row.get("selected_for_avwap")])
        run.latest_scan_at = as_of
        run.latest_candidate_status = candidate_status
        contract_by_id = {
            contract.security_id: contract
            for discovery in discoveries for contract in discovery.contracts
        }
        run.active_contracts = {
            sid: contract_by_id[sid] for sid in selected_by_id if sid in contract_by_id
        }
        run.last_ranked = {
            item.security_id: {
                "liquidity_score": item.liquidity_score,
                "volatility_score": item.volatility_score,
                "chain_quality_score": item.chain_quality_score,
                "execution_quality_score": item.execution_quality_score,
                "opportunity_score": item.opportunity_score,
                "rank": item.rank,
                "spread_percent": item.spread_percent,
                "quote_age_seconds": item.quote_age_seconds,
                "missing_components": list(item.missing_components),
            }
            for item in ranking.top_opportunities
        }

    def _point_in_time_spot(self, symbol: str, as_of: datetime):
        observations = []
        for row in self.dataset.spot_snapshots_for_symbol(symbol):
            if row.observed_at > as_of:
                continue
            if row.source == "underlying_quote" and row.last_price is not None and row.last_price > 0:
                observations.append((row.observed_at, float(row.last_price)))
            elif row.source == "chain" and row.spot is not None and row.spot > 0:
                observations.append((row.observed_at, float(row.spot)))
        if not observations:
            return None
        observed_at, price = max(observations, key=lambda item: item[0])
        if (as_of - observed_at).total_seconds() > self.settings["max_input_age_seconds"]:
            return None
        if observed_at > as_of:
            self.audit.violations.append({
                "code": "FUTURE_SPOT_USED", "symbol": symbol,
                "as_of": as_of.isoformat(), "observed_at": observed_at.isoformat(),
            })
            return None
        self.audit.market_observation_asof_checks += 1
        return price, observed_at

    def _contract_snapshots(self, security_id: str, as_of: datetime) -> list[MarketSnapshot]:
        rows = [row for row in self.dataset.snapshots_by_security_id.get(str(security_id), ())
                if row.observed_at <= as_of]
        self.audit.market_observation_asof_checks += len(rows)
        return rows

    def _liquidity_observations(self, security_id: str, as_of: datetime) -> list[LiquidityObservation]:
        return [
            LiquidityObservation(
                observed_at=row.observed_at,
                last_price=row.last_price,
                volume=row.volume,
                open_interest=row.open_interest,
                oi_change=row.oi_change,
                bid=row.bid,
                ask=row.ask,
                bid_quantity=row.bid_quantity,
                ask_quantity=row.ask_quantity,
                bid_depth=row.bid_depth,
                ask_depth=row.ask_depth,
            )
            for row in self._contract_snapshots(security_id, as_of)
            if row.source in {"chain", "option_quote"}
        ]

    def _underlying_price_observations(self, symbol: str, as_of: datetime):
        out = []
        chain_groups = set()
        for row in self.dataset.spot_snapshots_for_symbol(symbol):
            if row.observed_at > as_of:
                continue
            if row.source == "underlying_quote" and row.last_price is not None:
                out.append(PriceObservation(row.observed_at, row.last_price))
            elif row.source == "chain" and row.spot is not None:
                key = (row.observed_at, row.expiry)
                if key not in chain_groups:
                    chain_groups.add(key)
                    out.append(PriceObservation(row.observed_at, row.spot))
        self.audit.market_observation_asof_checks += len(out)
        return tuple(out)

    def _process_candle(self, run: ArmResult, item: ReplayCandle) -> None:
        sid = item.security_id
        if sid in run.open_positions:
            if item.exit_close:
                self._close_position(
                    run, sid, item.available_at, item.candle.ts, item.candle.close,
                    item.avwap, "CLOSE_ABOVE_AVWAP",
                )
            self._process_shadow_candle(run, item)
            return
        shadow_was_open = sid in run.shadow_positions
        self._process_shadow_candle(run, item)
        if not item.entry_cross:
            return
        run.entry_signal_count += 1
        signal_key = f"{sid}:{item.candle.ts}:{ENTRY_SELL}"
        if signal_key in run._seen_signal_keys:
            return
        run._seen_signal_keys.add(signal_key)
        contract = run.active_contracts.get(sid)
        if contract is None:
            self._record_unselected_signal(run, item, allow_counterfactual=not shadow_was_open)
            return
        if not _contract_live_at(contract, item.available_at):
            self._record_rejection(run, item, "CONTRACT_EXPIRED", "CONTRACT")
            return
        if item.available_at.time() >= time(15, 15):
            self._record_rejection(run, item, "NEW_ENTRIES_DISABLED", "RISK_GATE")
            return
        risk_reasons = self._risk_reasons(run, contract, item.available_at)
        if risk_reasons:
            for reason in risk_reasons:
                self._record_rejection(run, item, reason, "RISK_GATE")
            return
        self._open_position(run, contract, item)

    def _record_unselected_signal(self, run, item, *, allow_counterfactual=True) -> None:
        sid = item.security_id
        state = run.latest_candidate_status.get(sid)
        if run.name == "baseline":
            category, reasons = "OUTSIDE_LEGACY_UNIVERSE", ["NOT_IN_LEGACY_ATM_ITM_SELECTION"]
        elif state is None:
            category, reasons = "OUTSIDE_DISCOVERY_UNIVERSE", ["NOT_IN_LATEST_PHASE2_DISCOVERY"]
        elif state.get("status") == "REJECTED_LIQUIDITY":
            category = "LIQUIDITY_REJECTED"
            reasons = list(state.get("rejection_reasons", [])) or ["LIQUIDITY_NOT_QUALIFIED"]
        elif state.get("status") in {"RANKED_NOT_TOP", "RANKING_TRUNCATED"}:
            category = "RANKING_NOT_SELECTED"
            reasons = list(state.get("rejection_reasons", [])) or ["NOT_IN_TOP_OPPORTUNITIES"]
        elif state.get("status") == "EXCLUDED_PHASE5":
            category = "PHASE5_EXCLUDED"
            reasons = list(state.get("rejection_reasons", [])) or ["EXCLUDED_BY_PHASE5"]
        else:
            category = "NOT_SELECTED"
            reasons = list(state.get("rejection_reasons", [])) or ["NOT_IN_AVWAP_ALLOWLIST"]
        self._record_rejection(run, item, reasons, category)
        if category in {"LIQUIDITY_REJECTED", "RANKING_NOT_SELECTED"}:
            self._record_missed_signal(run, item, category, reasons, state)
            if category == "LIQUIDITY_REJECTED":
                if allow_counterfactual:
                    self._open_shadow_position(run, item, ",".join(reasons))
                else:
                    for missed in reversed(run.missed_signals):
                        if missed["security_id"] == sid and missed["signal_at"] == item.available_at.isoformat():
                            missed["counterfactual_status"] = "SHADOW_POSITION_CLOSED_SAME_BAR"
                            break

    def _record_missed_signal(self, run, item, category, reasons, state):
        contract = _contract_at(self.dataset, item.security_id, item.available_at)
        run.missed_signals.append({
            "date": item.available_at.date().isoformat(),
            "arm": run.label,
            "signal_at": item.available_at.isoformat(),
            "candle_start": from_epoch(item.candle.ts).isoformat(),
            "security_id": item.security_id,
            "symbol": contract.underlying if contract else None,
            "expiry": contract.expiry if contract else None,
            "strike": contract.strike if contract else None,
            "option_type": contract.option_type if contract else None,
            "signal_price": item.candle.close,
            "avwap": item.avwap,
            "category": category,
            "rejection_reasons": list(reasons),
            "liquidity_score": state.get("liquidity_score") if state else None,
            "latest_scan_at": run.latest_scan_at.isoformat() if run.latest_scan_at else None,
            "scan_age_seconds": ((item.available_at - run.latest_scan_at).total_seconds()
                                 if run.latest_scan_at else None),
            "counterfactual_trade_opened": False,
            "counterfactual_trade_id": None,
            "counterfactual_net_pnl": None,
            "counterfactual_status": None,
        })

    def _record_rejection(self, run, item, reasons, category):
        if isinstance(reasons, str):
            reasons = [reasons]
        contract = run.active_contracts.get(item.security_id) or _contract_at(
            self.dataset, item.security_id, item.available_at
        )
        run.rejections.append({
            "date": item.available_at.date().isoformat(),
            "arm": run.label,
            "event_at": item.available_at.isoformat(),
            "event_type": "AVWAP_ENTRY_SIGNAL",
            "symbol": contract.underlying if contract else None,
            "security_id": item.security_id,
            "expiry": contract.expiry if contract else None,
            "strike": contract.strike if contract else None,
            "option_type": contract.option_type if contract else None,
            "category": category,
            "rejection_reasons": list(reasons),
            "signal_price": item.candle.close,
            "avwap": item.avwap,
            "latest_scan_at": run.latest_scan_at.isoformat() if run.latest_scan_at else None,
        })

    def _open_position(self, run, contract, item):
        quote = _execution_quote(self.dataset, contract.security_id, item.available_at)
        raw = float(item.candle.close)
        fill = _fill_price(raw, is_sell=True, slippage_bps=self.settings["slippage_bps"])
        quantity = _quantity_for(contract, self.risk_cfg)
        if quantity <= 0 or fill <= 0:
            self._record_rejection(run, item, "INVALID_QUANTITY_OR_FILL_PRICE", "EXECUTION")
            return
        position = SimulatedPosition(
            contract=contract,
            quantity=quantity,
            entry_signal_at=item.available_at,
            entry_candle_ts=item.candle.ts,
            entry_signal_price=raw,
            entry_fill_price=fill,
            entry_avwap=item.avwap,
            entry_quote=quote,
            entry_spread_percent=quote.get("spread_percent"),
            entry_quote_age_seconds=quote.get("quote_age_seconds"),
            entry_slippage_cost=max(0.0, raw - fill) * quantity,
            entry_transaction_cost=_order_cost(fill, quantity, self.settings),
        )
        run.open_positions[contract.security_id] = position
        run.executed_entry_count += 1
        run.entry_count_by_day[item.available_at.date().isoformat()] += 1
        run.trades.append(_open_trade_row(position, run.label))

    def _close_position(
        self, run, security_id, at, candle_ts, raw_exit, avwap, reason,
        quote_override=None,
    ):
        position = run.open_positions.pop(security_id, None)
        if position is None:
            return
        quote = quote_override or _execution_quote(self.dataset, security_id, at)
        exit_fill = _fill_price(raw_exit, is_sell=False, slippage_bps=self.settings["slippage_bps"])
        exit_slippage = max(0.0, exit_fill - raw_exit) * position.quantity
        exit_cost = _order_cost(exit_fill, position.quantity, self.settings)
        gross = (position.entry_fill_price - exit_fill) * position.quantity
        reference = (position.entry_signal_price - raw_exit) * position.quantity
        slippage = position.entry_slippage_cost + exit_slippage
        fees = position.entry_transaction_cost + exit_cost
        closed = _closed_trade_row(
            position, run.label, exit_at=at, exit_candle_ts=candle_ts,
            exit_signal_price=raw_exit, exit_fill_price=exit_fill,
            exit_avwap=avwap, exit_reason=reason, exit_quote=quote,
            exit_slippage_cost=exit_slippage, exit_transaction_cost=exit_cost,
            gross_pnl=gross, reference_pnl=reference,
            total_slippage_cost=slippage, transaction_costs=fees,
            net_pnl=gross - fees,
        )
        _replace_open_trade(run.trades, security_id, position.entry_signal_at, closed)
        run.realized_gross_by_day[at.date().isoformat()] += gross

    def _risk_reasons(self, run, contract, at):
        reasons = []
        max_open = int(self.risk_cfg.get("max_open_positions", 0) or 0)
        if max_open and len(run.open_positions) >= max_open:
            reasons.append(f"MAX_OPEN_POSITIONS({max_open})")
        max_trades = int(self.risk_cfg.get("max_trades_per_day", 0) or 0)
        if max_trades and run.entry_count_by_day[at.date().isoformat()] >= max_trades:
            reasons.append(f"MAX_TRADES_PER_DAY({max_trades})")
        max_loss = float(self.risk_cfg.get("max_daily_loss", 0) or 0)
        if max_loss:
            daily_pnl = float(run.realized_gross_by_day[at.date().isoformat()])
            unpriced_open_position = False
            for position in run.open_positions.values():
                mark = _latest_option_quote_price(
                    self.dataset, position.contract.security_id, at,
                    max_age_seconds=self.settings["max_input_age_seconds"],
                )
                if mark is None:
                    unpriced_open_position = True
                else:
                    daily_pnl += (position.entry_fill_price - mark) * position.quantity
            if daily_pnl <= -abs(max_loss):
                reasons.append(f"MAX_DAILY_LOSS({max_loss:g})")
            if unpriced_open_position:
                reasons.append("UNPRICED_OPEN_POSITION_DRAWDOWN_GATE")
        return reasons

    def _mechanical_exit(self, run, at):
        if at.time() != time(15, 15):
            return
        for sid in sorted(list(run.open_positions)):
            position = run.open_positions.get(sid)
            if position is None:
                continue
            mark = _mechanical_exit_mark(
                self.dataset, sid, at,
                max_quote_age_seconds=self.settings["max_input_age_seconds"],
                max_candle_age_seconds=self.settings["max_exit_candle_age_seconds"],
            )
            if mark is None:
                # Keep the position open/unpriced instead of manufacturing an
                # exit at its entry fill. Final reports will show it as open.
                continue
            raw, quote, source_candle_ts = mark
            self._close_position(
                run, sid, at, source_candle_ts, raw, None,
                "INTRADAY_EXIT_15_15", quote_override=quote,
            )
        for sid in sorted(list(run.shadow_positions)):
            mark = _mechanical_exit_mark(
                self.dataset, sid, at,
                max_quote_age_seconds=self.settings["max_input_age_seconds"],
                max_candle_age_seconds=self.settings["max_exit_candle_age_seconds"],
            )
            if mark is None:
                continue
            raw, quote, source_candle_ts = mark
            self._close_shadow(
                run, sid, at, reason="INTRADAY_EXIT_15_15",
                candle_ts=source_candle_ts, raw_exit=raw, quote_override=quote,
            )

    def _finalize_open_positions(self, run, period_end):
        for sid, position in sorted(run.open_positions.items()):
            mark = _mark_price(self.dataset, sid, period_end, with_source=True)
            row = _open_trade_row(position, run.label)
            gross_u = (position.entry_fill_price - mark[0]) * position.quantity if mark else None
            row.update({
                "status": "OPEN_AT_END", "exit_time": None, "exit_candle_start": None,
                "exit_signal_price": None, "exit_fill_price": None, "exit_reason": None,
                "mark_price": mark[0] if mark else None,
                "mark_source": mark[1] if mark else None,
                "gross_unrealized_pnl": gross_u,
                "estimated_net_unrealized_pnl": (
                    gross_u - position.entry_transaction_cost
                    - _order_cost(_fill_price(mark[0], is_sell=False,
                                              slippage_bps=self.settings["slippage_bps"]),
                                  position.quantity, self.settings)
                    if mark else None
                ),
                "open_at_period_end": True,
            })
            _replace_open_trade(run.trades, sid, position.entry_signal_at, row)
        run.open_positions.clear()
        for sid, position in sorted(run.shadow_positions.items()):
            mark = _mark_price(self.dataset, sid, period_end, with_source=True)
            gross_u = (position.entry_fill_price - mark[0]) * position.quantity if mark else None
            run.shadow_trades.append({
                "date": position.entry_signal_at.date().isoformat(), "arm": run.label,
                "security_id": sid, "symbol": position.contract.underlying,
                "expiry": position.contract.expiry, "strike": position.contract.strike,
                "option_type": position.contract.option_type,
                "entry_time": position.entry_signal_at.isoformat(),
                "entry_signal_price": position.entry_signal_price,
                "entry_fill_price": position.entry_fill_price,
                "quantity": position.quantity, "status": "OPEN_AT_END",
                "rejection_reason": position.rejection_reason,
                "mark_price": mark[0] if mark else None,
                "gross_unrealized_pnl": gross_u,
                "net_unrealized_pnl": gross_u - position.entry_transaction_cost if gross_u is not None else None,
            })
        run.shadow_positions.clear()

    def _process_shadow_candle(self, run, item):
        position = run.shadow_positions.get(item.security_id)
        if position and item.exit_close:
            self._close_shadow(
                run, item.security_id, item.available_at,
                reason="CLOSE_ABOVE_AVWAP", candle_ts=item.candle.ts,
                raw_exit=item.candle.close, avwap=item.avwap,
            )

    def _open_shadow_position(self, run, item, reason):
        sid = item.security_id
        if sid in run.shadow_positions:
            for missed in reversed(run.missed_signals):
                if missed["security_id"] == sid and missed["signal_at"] == item.available_at.isoformat():
                    missed["counterfactual_status"] = "SHADOW_POSITION_ALREADY_OPEN"
                    break
            return
        contract = _contract_at(self.dataset, sid, item.available_at)
        if contract is None:
            return
        quote = _execution_quote(self.dataset, sid, item.available_at)
        raw = float(item.candle.close)
        fill = _fill_price(raw, is_sell=True, slippage_bps=self.settings["slippage_bps"])
        quantity = _quantity_for(contract, self.risk_cfg)
        if quantity <= 0 or fill <= 0:
            return
        run.shadow_positions[sid] = ShadowPosition(
            contract=contract, quantity=quantity,
            entry_signal_at=item.available_at, entry_candle_ts=item.candle.ts,
            entry_signal_price=raw, entry_fill_price=fill, entry_avwap=item.avwap,
            rejection_reason=reason,
            entry_spread_percent=quote.get("spread_percent"),
            entry_quote_age_seconds=quote.get("quote_age_seconds"),
            entry_slippage_cost=max(0.0, raw - fill) * quantity,
            entry_transaction_cost=_order_cost(fill, quantity, self.settings),
        )
        for missed in reversed(run.missed_signals):
            if missed["security_id"] == sid and missed["signal_at"] == item.available_at.isoformat():
                missed["counterfactual_trade_opened"] = True
                missed["counterfactual_status"] = "OPEN"
                break

    def _close_shadow(
        self, run, security_id, at, *, reason, candle_ts=0, raw_exit=None,
        avwap=None, quote_override=None,
    ):
        position = run.shadow_positions.get(security_id)
        if position is None:
            return
        quote = quote_override or _execution_quote(self.dataset, security_id, at)
        if raw_exit is None:
            mark = _mechanical_exit_mark(
                self.dataset, security_id, at,
                max_quote_age_seconds=self.settings["max_input_age_seconds"],
                max_candle_age_seconds=self.settings["max_exit_candle_age_seconds"],
            )
            if mark is None:
                return
            raw_exit, quote, candle_ts = mark
        run.shadow_positions.pop(security_id, None)
        exit_fill = _fill_price(float(raw_exit), is_sell=False,
                                slippage_bps=self.settings["slippage_bps"])
        exit_cost = _order_cost(exit_fill, position.quantity, self.settings)
        exit_slippage = max(0.0, exit_fill - float(raw_exit)) * position.quantity
        gross = (position.entry_fill_price - exit_fill) * position.quantity
        reference = (position.entry_signal_price - float(raw_exit)) * position.quantity
        fees = position.entry_transaction_cost + exit_cost
        net = gross - fees
        trade_id = f"{security_id}:{position.entry_candle_ts}:{ENTRY_SELL}"
        if reason == "INTRADAY_EXIT_15_15":
            exit_mark_source = quote.get("mark_source")
            exit_mark_observed_at = quote.get("last_price_observed_at")
            exit_mark_age_seconds = quote.get("mark_age_seconds")
            if exit_mark_observed_at is None and candle_ts:
                exit_mark_observed_at = (
                    from_epoch(candle_ts) + timedelta(minutes=15)
                ).isoformat()
        else:
            exit_mark_source = "completed_option_candle_signal"
            exit_mark_age_seconds = None
            exit_mark_observed_at = (
                (from_epoch(candle_ts) + timedelta(minutes=15)).isoformat()
                if candle_ts else None
            )
        run.shadow_trades.append({
            "date": at.date().isoformat(), "arm": run.label,
            "security_id": security_id, "symbol": position.contract.underlying,
            "expiry": position.contract.expiry, "strike": position.contract.strike,
            "option_type": position.contract.option_type,
            "entry_time": position.entry_signal_at.isoformat(),
            "entry_candle_start": from_epoch(position.entry_candle_ts).isoformat(),
            "entry_signal_price": position.entry_signal_price,
            "entry_fill_price": position.entry_fill_price,
            "entry_avwap": position.entry_avwap,
            "exit_time": at.isoformat(),
            "exit_candle_start": from_epoch(candle_ts).isoformat() if candle_ts else None,
            "exit_signal_price": float(raw_exit), "exit_fill_price": exit_fill,
            "exit_avwap": avwap, "exit_reason": reason,
            "exit_mark_source": exit_mark_source,
            "exit_mark_observed_at": exit_mark_observed_at,
            "exit_mark_age_seconds": exit_mark_age_seconds,
            "exit_quote_age_seconds": quote.get("quote_age_seconds"),
            "quantity": position.quantity, "gross_pnl": gross,
            "reference_pnl_before_slippage": reference,
            "slippage_cost": position.entry_slippage_cost + exit_slippage,
            "transaction_costs": fees, "net_pnl": net,
            "rejection_reason": position.rejection_reason,
            "entry_spread_percent": position.entry_spread_percent,
            "entry_quote_age_seconds": position.entry_quote_age_seconds,
            "status": "COUNTERFACTUAL_CLOSED",
        })
        for missed in run.missed_signals:
            if (missed["security_id"] == security_id
                    and missed["signal_at"] == position.entry_signal_at.isoformat()
                    and missed.get("counterfactual_trade_opened")):
                missed["counterfactual_status"] = "COUNTERFACTUAL_CLOSED"
                missed["counterfactual_trade_id"] = trade_id
                missed["counterfactual_net_pnl"] = net
                break

    def _final_data_quality(self) -> dict:
        quality = self.dataset.quality.to_dict()
        warnings = list(quality.get("warnings", []))
        if len(self.dataset.symbols) != 20:
            warnings.append({
                "code": "UNIVERSE_SIZE_NOT_20",
                "configured_symbols": len(self.dataset.symbols),
                "message": "This diagnostic replay does not use the full configured 20-stock universe.",
            })
        if not self.dataset.anchor_confirmed:
            warnings.append({
                "code": "NO_VERIFIED_OPTION_AVWAP_ANCHORS",
                "message": "No contract has an explicitly flagged first-tradable option candle; no AVWAP trade can be considered valid.",
            })
        if not any(row.implied_volatility is not None for row in self.dataset.snapshots if row.source == "chain"):
            warnings.append({
                "code": "NO_ARCHIVED_IMPLIED_VOLATILITY",
                "message": "No actual IV observation is archived; IV remains missing and is not synthesized.",
            })
        if not self.dataset.underlying_candles:
            warnings.append({"code": "NO_UNDERLYING_15M_HISTORY"})
        if not any(row.source == "option_quote" for row in self.dataset.snapshots):
            warnings.append({
                "code": "NO_OPTION_QUOTE_ARCHIVE",
                "message": "No option quote-feed history is available; only actual archived chain books can contribute to execution metrics.",
            })
        quality["warnings"] = warnings
        quality["option_anchor_coverage"] = {
            "contracts_with_option_candles": len(self.dataset.option_candles),
            "contracts_with_confirmed_first_tradable_bar": len(self.dataset.anchor_confirmed),
            "contracts_unverified": len(self.dataset.quality.unverified_option_anchors),
            "trade_policy": "exclude unverified option AVWAP anchors; never infer an anchor from the first supplied bar",
        }
        quality["archive_counts"] = {
            "symbols": len(self.dataset.symbols),
            "scan_events": len(self.dataset.scan_events),
            "contract_master_snapshots": len(self.dataset.catalog.snapshots),
            "option_candle_contracts": len(self.dataset.option_candles),
            "option_candles": sum(map(len, self.dataset.option_candles.values())),
            "underlying_candles": sum(map(len, self.dataset.underlying_candles.values())),
            "market_snapshots": len(self.dataset.snapshots),
        }
        quality["no_synthetic_data"] = True
        return quality

    def _assumptions(self) -> dict:
        sample_scenario = self.scenarios[0]
        return {
            "baseline": {
                "selector": "market.universe legacy ATM plus configured ITM strikes",
                "expiry_rule": "same monthly expiry switch rule used by production discovery",
                "strike_source": "actual archived chain strikes; selected contracts must exist in the as-of contract master",
                "liquidity_filter": False,
            },
            "new_system": {
                "pipeline": "20-stock manifest -> point-in-time contract discovery -> Phase 3 liquidity hard gate -> Phase 4 volatility -> Phase 5 ranking -> unchanged AVWAP",
                "selector": "OpportunityRankingEngine.top_opportunities",
                "threshold_sweep": [item.to_dict() for item in self.scenarios],
                "component_weights": dict(self._scenario_rankers[sample_scenario.key].weights),
                "liquidity_weights": dict(self._spread_liquidity_engines[sample_scenario.max_spread_percent].weights),
            },
            "shared_trading_rules": {
                "timeframe_minutes": 15,
                "option_chart_basis": "option-contract OHLCV candles",
                "avwap_formula": "production AvwapState cumulative typical-price x volume from an explicitly confirmed first tradable option candle",
                "entry": "previous close >= previous AVWAP and current completed-candle close < current AVWAP",
                "exit": "current completed-candle close > current AVWAP",
                "mechanical_exit": "15:15 IST; fresh option quote within max_input_age_seconds, otherwise the last previously completed same-day option candle within max_exit_candle_age_seconds; if neither exists the position remains open/unpriced",
                "max_exit_candle_age_seconds": self.settings["max_exit_candle_age_seconds"],
                "position_rule": "one open position per option contract; production quantity and risk gates",
                "expiry_rule": "identical monthly expiry switch for baseline and discovery",
            },
            "execution": {
                "fill_mode": self.settings["fill_mode"],
                "slippage_bps_each_side": self.settings["slippage_bps"],
                "transaction_cost_per_order_rupees": self.settings["transaction_cost_per_order"],
                "transaction_cost_bps_of_premium_turnover": self.settings["transaction_cost_bps"],
                "default_cost_model_note": "Zero transaction fees unless explicitly configured; no approximate charge estimator is silently applied.",
                "max_quote_age_seconds_for_scan_inputs": self.settings["max_input_age_seconds"],
                "entry_exit_price_basis": "completed signal candle close with configured adverse slippage for AVWAP signal entries/exits; mechanical exit uses the separately stated fresh-quote/recent-candle policy",
            },
            "sizing_and_risk": {
                "quantity_per_trade": self.risk_cfg.get("quantity_per_trade"),
                "fallback_quantity": "contract lot size; then configured default_lot_size; explicit quantities must be positive integer lot multiples or are rejected",
                "max_open_positions": self.risk_cfg.get("max_open_positions"),
                "max_trades_per_day": self.risk_cfg.get("max_trades_per_day"),
                "max_daily_loss": self.risk_cfg.get("max_daily_loss"),
            },
            "safety": {
                "paper_trading_enabled": False, "live_trading_enabled": False,
                "orders_sent": False, "automatic_winner_declared": False,
            },
        }


def run_phase7(
    dataset: Phase7Dataset | str | Path,
    *,
    output_dir: Optional[str | Path] = None,
    config: Optional[dict] = None,
    scenarios: Optional[Sequence[ThresholdScenario]] = None,
) -> Phase7Result:
    if not isinstance(dataset, Phase7Dataset):
        dataset = load_dataset(dataset)
    return Phase7Backtester(dataset, config=config, scenarios=scenarios).run(output_dir)


def summarize_arm(run: ArmResult) -> dict:
    closed = [trade for trade in run.trades if trade.get("status") == "CLOSED"]
    opened = [trade for trade in run.trades if trade.get("status") == "OPEN_AT_END"]
    gross = sum(float(t.get("gross_pnl") or 0.0) for t in closed)
    net = sum(float(t.get("net_pnl") or 0.0) for t in closed)
    reference = sum(float(t.get("reference_pnl_before_slippage") or 0.0) for t in closed)
    slippage = sum(float(t.get("slippage_cost") or 0.0) for t in closed)
    fees = sum(float(t.get("transaction_costs") or 0.0) for t in closed)
    wins = sum(float(t.get("net_pnl") or 0.0) > 0 for t in closed)
    losses = sum(float(t.get("net_pnl") or 0.0) < 0 for t in closed)
    profit = sum(float(t.get("net_pnl") or 0.0) for t in closed if (t.get("net_pnl") or 0) > 0)
    loss = abs(sum(float(t.get("net_pnl") or 0.0) for t in closed if (t.get("net_pnl") or 0) < 0))
    spreads = [float(t["entry_spread_percent"]) for t in closed if t.get("entry_spread_percent") is not None]
    exit_spreads = [float(t["exit_spread_percent"]) for t in closed if t.get("exit_spread_percent") is not None]
    quote_ages = [float(t["entry_quote_age_seconds"]) for t in closed if t.get("entry_quote_age_seconds") is not None]
    missed = Counter(row["category"] for row in run.missed_signals)
    rejection_counts = Counter(
        reason for row in run.rejections for reason in row.get("rejection_reasons", [])
    )
    daily = defaultdict(lambda: {"trades_closed": 0, "gross_pnl": 0.0, "net_pnl": 0.0})
    for trade in closed:
        day = trade.get("exit_date")
        if day:
            daily[day]["trades_closed"] += 1
            daily[day]["gross_pnl"] += float(trade.get("gross_pnl") or 0.0)
            daily[day]["net_pnl"] += float(trade.get("net_pnl") or 0.0)
    daily = {day: {key: _round_num(value) for key, value in stats.items()}
             for day, stats in sorted(daily.items())}
    counterfactual = [t for t in run.shadow_trades if t.get("status") == "COUNTERFACTUAL_CLOSED"]
    return {
        "arm": run.name,
        "scenario": run.label,
        "thresholds": run.scenario.to_dict() if run.scenario else None,
        "entry_signals_eligible_or_observed": run.entry_signal_count,
        "entries_executed": run.executed_entry_count,
        "trades_closed": len(closed),
        "open_at_period_end": len(opened),
        "wins": int(wins), "losses": int(losses),
        "breakeven": len(closed) - wins - losses,
        "win_rate_percent": _round_num(100 * wins / len(closed)) if closed else None,
        "profit_factor": _round_num(profit / loss) if loss > 0 else None,
        "gross_pnl_after_slippage": _round_num(gross),
        "signal_price_pnl_before_slippage": _round_num(reference),
        "net_pnl_after_transaction_costs": _round_num(net),
        "slippage_cost": _round_num(slippage),
        "transaction_costs": _round_num(fees),
        "average_net_pnl_per_closed_trade": _round_num(net / len(closed)) if closed else None,
        "max_drawdown_on_closed_net_pnl": _round_num(_max_drawdown([
            float(t.get("net_pnl") or 0) for t in sorted(
                closed, key=lambda row: (row.get("exit_time") or "", row.get("security_id") or "")
            )
        ])),
        "average_entry_spread_percent": _round_num(sum(spreads) / len(spreads)) if spreads else None,
        "average_exit_spread_percent": _round_num(sum(exit_spreads) / len(exit_spreads)) if exit_spreads else None,
        "average_entry_quote_age_seconds": _round_num(sum(quote_ages) / len(quote_ages)) if quote_ages else None,
        "entry_spread_observation_coverage_percent": _round_num(100 * len(spreads) / len(closed)) if closed else None,
        "exit_spread_observation_coverage_percent": _round_num(100 * len(exit_spreads) / len(closed)) if closed else None,
        "liquidity_rejected_avwap_signals": int(missed.get("LIQUIDITY_REJECTED", 0)),
        "ranking_not_selected_avwap_signals": int(missed.get("RANKING_NOT_SELECTED", 0)),
        "missed_avwap_signals_by_category": dict(sorted(missed.items())),
        "rejection_reason_counts": dict(sorted(rejection_counts.items())),
        "counterfactual_liquidity_rejected_trades_closed": len(counterfactual),
        "counterfactual_liquidity_rejected_net_pnl": _round_num(sum(float(t.get("net_pnl") or 0) for t in counterfactual)),
        "daily_closed_pnl": daily,
        "automatic_winner_declared": False,
    }


def _apply_score_threshold(report, threshold):
    contracts = []
    for item in report.contracts:
        reasons = [reason for reason in item.rejection_reasons if reason != "LOW_LIQUIDITY_SCORE"]
        if item.score < threshold:
            reasons.append("LOW_LIQUIDITY_SCORE")
        ordered = tuple(code for code in LIQUIDITY_REASON_CODES if code in reasons)
        contracts.append(replace(item, qualified=not ordered, rejection_reasons=ordered))
    return OptionChainLiquidityReport(tuple(contracts), report.chains)


def _liquidity_config_for_spread(config, spread):
    out = copy.deepcopy(config)
    liquidity = out.setdefault("liquidity", {})
    liquidity["max_spread_percent"] = float(spread)
    liquidity["preferred_spread_percent"] = min(
        float(liquidity.get("preferred_spread_percent", spread)), float(spread)
    )
    liquidity.setdefault("minimums", {})["liquidity_score"] = None
    return out


def _backtest_settings(config):
    supplied = config.get("backtest", {}) if isinstance(config, dict) else {}
    settings = {**DEFAULT_BACKTEST_SETTINGS, **(supplied if isinstance(supplied, dict) else {})}
    if settings["fill_mode"] != "candle_close":
        raise BacktestError("Phase 7 currently supports only the production candle_close fill mode")
    for key in (
        "slippage_bps", "transaction_cost_per_order", "transaction_cost_bps",
        "max_input_age_seconds", "max_exit_candle_age_seconds",
    ):
        try:
            number = float(settings[key])
        except (TypeError, ValueError, OverflowError) as exc:
            raise BacktestError(f"backtest.{key} must be finite and non-negative") from exc
        if not math.isfinite(number) or number < 0:
            raise BacktestError(f"backtest.{key} must be finite and non-negative")
        settings[key] = number
    if settings["slippage_bps"] >= 10000:
        raise BacktestError("backtest.slippage_bps must be below 10000")
    if settings["max_input_age_seconds"] <= 0:
        raise BacktestError("backtest.max_input_age_seconds must be positive")
    return settings


def _configured_phase1_symbols() -> tuple[str, ...]:
    import json
    path = Path(__file__).resolve().parent.parent / "config" / "universe.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    symbols = payload.get("universe", {}).get("symbols", [])
    if not isinstance(symbols, list):
        return ()
    return tuple(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _int_setting(value, name, *, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BacktestError(f"{name} must be an integer from {minimum} to {maximum}") from exc
    if isinstance(value, bool) or float(value) != parsed or not minimum <= parsed <= maximum:
        raise BacktestError(f"{name} must be an integer from {minimum} to {maximum}")
    return parsed


def _manifest_time(manifest, key):
    value = manifest.get(key)
    if not value:
        return None
    try:
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BacktestError(f"manifest.{key} must be ISO-8601") from exc
    if value.tzinfo is None:
        raise BacktestError(f"manifest.{key} must include an explicit timezone")
    return value.astimezone(IST)


def _strike_key(strike):
    return f"{float(strike):.8f}".rstrip("0").rstrip(".")


def _discovery_moneyness(discovery, security_id):
    for item in discovery.calls + discovery.puts:
        if item.contract.security_id == security_id:
            return item.moneyness
    return None


def _discovery_error_code(exc):
    name = type(exc).__name__.upper()
    return {
        "HISTORICALCONTRACTSNAPSHOTUNAVAILABLE": "NO_ASOF_CONTRACT_MASTER",
        "UNDERLYINGPRICEUNAVAILABLE": "NO_ASOF_UNDERLYING_PRICE",
        "NOVELIGIBLEEXPIRY": "NO_ASOF_EXPIRY",
        "UNVALIDATEDUNDERLYING": "SYMBOL_NOT_IN_VALIDATED_UNIVERSE",
    }.get(name, "DISCOVERY_ERROR:" + type(exc).__name__)


def _contract_at(dataset, security_id, as_of):
    try:
        snapshot = dataset.catalog.snapshot_for(as_of)
    except Exception:
        return None
    return next((c for c in snapshot.contracts if str(c.security_id) == str(security_id)), None)


def _to_ohlc(candle):
    return OHLCBar(
        ts=from_epoch(candle.ts), open=candle.open, high=candle.high,
        low=candle.low, close=candle.close, volume=candle.volume,
    )


def _chain_observed_at(dataset, symbol, expiry, as_of, max_age):
    chain = dataset.chain_at(symbol, expiry, as_of, max_age)
    return chain.observed_at.isoformat() if chain and chain.observed_at else None


def _execution_quote(dataset, security_id, as_of):
    rows = [row for row in dataset.snapshots_by_security_id.get(str(security_id), ())
            if row.source in {"chain", "option_quote"} and row.observed_at <= as_of]
    books = [row for row in rows if row.bid is not None and row.ask is not None
             and row.bid > 0 and row.ask > 0 and row.ask >= row.bid]
    book = max(books, key=lambda row: row.observed_at) if books else None
    prices = [row for row in rows if row.last_price is not None and row.last_price > 0]
    price_row = max(prices, key=lambda row: row.observed_at) if prices else None
    if book:
        midpoint = (book.bid + book.ask) / 2
        spread = 100 * (book.ask - book.bid) / midpoint if midpoint > 0 else None
        age = (as_of - book.observed_at).total_seconds()
    else:
        spread = None
        age = (as_of - price_row.observed_at).total_seconds() if price_row else None
    return {
        "bid": book.bid if book else None,
        "ask": book.ask if book else None,
        "spread_percent": spread,
        "quote_age_seconds": age,
        "quote_observed_at": book.observed_at.isoformat() if book else None,
        "last_price": price_row.last_price if price_row else None,
        "last_price_observed_at": price_row.observed_at.isoformat() if price_row else None,
        "mark_source": "captured_quote_or_chain" if price_row else None,
    }


def _latest_option_quote_price(dataset, security_id, as_of, max_age_seconds=None):
    rows = [row for row in dataset.snapshots_by_security_id.get(str(security_id), ())
            if row.source == "option_quote" and row.last_price is not None
            and math.isfinite(float(row.last_price)) and row.last_price > 0
            and row.observed_at <= as_of
            and (max_age_seconds is None
                 or (as_of - row.observed_at).total_seconds() <= max_age_seconds)]
    return float(max(rows, key=lambda row: row.observed_at).last_price) if rows else None


def _mechanical_exit_mark(
    dataset, security_id, as_of, *, max_quote_age_seconds, max_candle_age_seconds
):
    """Use a fresh captured quote, then an actual recent completed same-day bar.

    Candle fallback excludes bars completing exactly at ``as_of`` because the
    app attempts the 15:15 mechanical exit before processing that just-closed
    window. No entry-price substitution is permitted.
    """
    quote_rows = [
        row for row in dataset.snapshots_by_security_id.get(str(security_id), ())
        if row.source == "option_quote" and row.last_price is not None
        and math.isfinite(float(row.last_price)) and row.last_price > 0
        and row.observed_at <= as_of
        and (as_of - row.observed_at).total_seconds() <= max_quote_age_seconds
    ]
    if quote_rows:
        row = max(quote_rows, key=lambda item: item.observed_at)
        age = (as_of - row.observed_at).total_seconds()
        bid = row.bid if row.bid is not None and row.bid > 0 else None
        ask = row.ask if row.ask is not None and row.ask > 0 else None
        if bid is not None and ask is not None and ask < bid:
            bid = ask = None
        midpoint = (bid + ask) / 2 if bid is not None and ask is not None else None
        quote = {
            "bid": bid,
            "ask": ask,
            "spread_percent": (100 * (ask - bid) / midpoint
                               if midpoint and midpoint > 0 else None),
            "quote_age_seconds": age,
            "mark_age_seconds": age,
            "quote_observed_at": row.observed_at.isoformat(),
            "last_price": float(row.last_price),
            "last_price_observed_at": row.observed_at.isoformat(),
            "mark_source": "fresh_option_quote",
        }
        return float(row.last_price), quote, 0

    bars = []
    for bar in dataset.option_bars_at(str(security_id), as_of):
        start = from_epoch(bar.ts)
        completed_at = start + timedelta(minutes=15)
        age = (as_of - completed_at).total_seconds()
        if (completed_at >= as_of or start.date() != as_of.date()
                or age < 0 or age > max_candle_age_seconds):
            continue
        bars.append((bar, completed_at, age))
    if not bars:
        return None
    bar, completed_at, age = max(bars, key=lambda item: item[0].ts)
    quote = {
        "bid": None,
        "ask": None,
        "spread_percent": None,
        "quote_age_seconds": None,
        "mark_age_seconds": age,
        "quote_observed_at": None,
        "last_price": float(bar.close),
        "last_price_observed_at": completed_at.isoformat(),
        "mark_source": "completed_option_candle_fallback",
    }
    return float(bar.close), quote, int(bar.ts)


def _mark_price(dataset, security_id, as_of, with_source=False):
    quote_rows = [row for row in dataset.snapshots_by_security_id.get(str(security_id), ())
                  if row.source == "option_quote" and row.last_price is not None
                  and row.last_price > 0 and row.observed_at <= as_of]
    if quote_rows:
        row = max(quote_rows, key=lambda item: item.observed_at)
        return (float(row.last_price), "option_quote") if with_source else float(row.last_price)
    bars = dataset.option_bars_at(security_id, as_of)
    if bars:
        return (float(bars[-1].close), "completed_option_candle") if with_source else float(bars[-1].close)
    return None


def _contract_live_at(contract, at):
    try:
        expiry = date.fromisoformat(str(contract.expiry)[:10])
    except ValueError:
        return False
    if expiry > at.date():
        return True
    if expiry < at.date():
        return False
    return at.time() < SESSION_CLOSE


def _quantity_for(contract, risk):
    def positive_integer(value):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or number <= 0 or not number.is_integer():
            return None
        return int(number)

    lot = positive_integer(getattr(contract, "lot_size", None))
    if lot is None:
        lot = positive_integer(risk.get("default_lot_size", 250)) or 250
    quantity = risk.get("quantity_per_trade")
    if quantity is None or quantity == "":
        return lot
    configured = positive_integer(quantity)
    if configured is not None:
        return configured if configured % lot == 0 else 0
    try:
        numeric = float(quantity)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(numeric) or numeric > 0:
        return 0
    return lot


def _fill_price(raw, *, is_sell, slippage_bps):
    slip = float(slippage_bps) / 10000
    return float(raw) * (1 - slip if is_sell else 1 + slip)


def _order_cost(fill_price, quantity, settings):
    turnover = abs(float(fill_price) * int(quantity))
    return float(settings["transaction_cost_per_order"]) + turnover * float(settings["transaction_cost_bps"]) / 10000


def _open_trade_row(position, arm):
    return {
        "date": position.entry_signal_at.date().isoformat(),
        "arm": arm,
        "trade_id": f"{position.contract.security_id}:{position.entry_candle_ts}:{ENTRY_SELL}",
        "symbol": position.contract.underlying,
        "security_id": position.contract.security_id,
        "expiry": position.contract.expiry,
        "strike": position.contract.strike,
        "option_type": position.contract.option_type,
        "status": "OPEN",
        "entry_time": position.entry_signal_at.isoformat(),
        "entry_date": position.entry_signal_at.date().isoformat(),
        "entry_candle_start": from_epoch(position.entry_candle_ts).isoformat(),
        "entry_signal_price": position.entry_signal_price,
        "entry_fill_price": position.entry_fill_price,
        "entry_avwap": position.entry_avwap,
        "entry_reason": position.entry_reason,
        "entry_quantity": position.quantity,
        "quantity": position.quantity,
        "entry_spread_percent": position.entry_spread_percent,
        "entry_quote_age_seconds": position.entry_quote_age_seconds,
        "entry_quote_observed_at": position.entry_quote.get("quote_observed_at"),
        "entry_slippage_cost": position.entry_slippage_cost,
        "entry_transaction_cost": position.entry_transaction_cost,
        "exit_time": None, "exit_date": None, "exit_candle_start": None,
        "exit_signal_price": None, "exit_fill_price": None, "exit_avwap": None,
        "exit_reason": None, "exit_quantity": None,
        "exit_spread_percent": None, "exit_quote_age_seconds": None,
        "exit_quote_observed_at": None, "exit_mark_source": None,
        "exit_mark_observed_at": None, "exit_mark_age_seconds": None,
        "exit_slippage_cost": None,
        "exit_transaction_cost": None, "gross_pnl": None,
        "reference_pnl_before_slippage": None, "slippage_cost": None,
        "transaction_costs": None, "net_pnl": None, "open_at_period_end": False,
    }


def _closed_trade_row(
    position, arm, *, exit_at, exit_candle_ts, exit_signal_price,
    exit_fill_price, exit_avwap, exit_reason, exit_quote,
    exit_slippage_cost, exit_transaction_cost, gross_pnl,
    reference_pnl, total_slippage_cost, transaction_costs, net_pnl,
):
    row = _open_trade_row(position, arm)
    if exit_reason == "INTRADAY_EXIT_15_15":
        exit_mark_source = exit_quote.get("mark_source")
        exit_mark_observed_at = exit_quote.get("last_price_observed_at")
        exit_mark_age_seconds = exit_quote.get("mark_age_seconds")
        if exit_mark_observed_at is None and exit_candle_ts:
            exit_mark_observed_at = (
                from_epoch(exit_candle_ts) + timedelta(minutes=15)
            ).isoformat()
    else:
        exit_mark_source = "completed_option_candle_signal"
        exit_mark_age_seconds = None
        exit_mark_observed_at = (
            (from_epoch(exit_candle_ts) + timedelta(minutes=15)).isoformat()
            if exit_candle_ts else None
        )
    row.update({
        "status": "CLOSED",
        "exit_time": exit_at.isoformat(), "exit_date": exit_at.date().isoformat(),
        "exit_candle_start": from_epoch(exit_candle_ts).isoformat() if exit_candle_ts else None,
        "exit_signal_price": exit_signal_price, "exit_fill_price": exit_fill_price,
        "exit_avwap": exit_avwap, "exit_reason": exit_reason,
        "exit_quantity": position.quantity,
        "exit_spread_percent": exit_quote.get("spread_percent"),
        "exit_quote_age_seconds": exit_quote.get("quote_age_seconds"),
        "exit_quote_observed_at": exit_quote.get("quote_observed_at"),
        "exit_mark_source": exit_mark_source,
        "exit_mark_observed_at": exit_mark_observed_at,
        "exit_mark_age_seconds": exit_mark_age_seconds,
        "exit_slippage_cost": exit_slippage_cost,
        "exit_transaction_cost": exit_transaction_cost,
        "gross_pnl": gross_pnl,
        "reference_pnl_before_slippage": reference_pnl,
        "slippage_cost": total_slippage_cost,
        "transaction_costs": transaction_costs,
        "net_pnl": net_pnl,
        "open_at_period_end": False,
    })
    return row


def _replace_open_trade(trades, security_id, entry_at, replacement):
    for index in range(len(trades) - 1, -1, -1):
        row = trades[index]
        if row.get("security_id") == security_id and row.get("entry_time") == entry_at.isoformat():
            trades[index] = replacement
            return
    trades.append(replacement)


def _analysis_tables(baseline, scenarios):
    base = summarize_arm(baseline)
    comparisons, thresholds, missed_rows = [], [], []
    fields = (
        "entries_executed", "trades_closed", "win_rate_percent", "profit_factor",
        "gross_pnl_after_slippage", "net_pnl_after_transaction_costs", "slippage_cost",
        "transaction_costs", "average_entry_spread_percent", "average_entry_quote_age_seconds",
        "liquidity_rejected_avwap_signals", "counterfactual_liquidity_rejected_trades_closed",
        "counterfactual_liquidity_rejected_net_pnl",
    )
    for run in scenarios:
        metrics = summarize_arm(run)
        comp = {
            "scenario": run.label,
            "liquidity_score_min": run.scenario.liquidity_score_min,
            "max_spread_percent": run.scenario.max_spread_percent,
            "automatic_winner_declared": False,
        }
        for name in fields:
            before, after = base.get(name), metrics.get(name)
            comp[f"baseline_{name}"] = before
            comp[f"new_{name}"] = after
            comp[f"delta_{name}"] = _subtract(after, before)
        comparisons.append(comp)
        thresholds.append({
            "scenario": run.label,
            "liquidity_score_min": run.scenario.liquidity_score_min,
            "max_spread_percent": run.scenario.max_spread_percent,
            **{name: metrics.get(name) for name in fields},
            "baseline_trades_closed": base["trades_closed"],
            "baseline_net_pnl_after_transaction_costs": base["net_pnl_after_transaction_costs"],
            "delta_trades_closed": _subtract(metrics.get("trades_closed"), base["trades_closed"]),
            "delta_net_pnl_after_transaction_costs": _subtract(
                metrics.get("net_pnl_after_transaction_costs"), base["net_pnl_after_transaction_costs"]
            ),
            "automatic_winner_declared": False,
        })
        missed_rows.append({
            "scenario": run.label,
            "liquidity_score_min": run.scenario.liquidity_score_min,
            "max_spread_percent": run.scenario.max_spread_percent,
            "liquidity_rejected_avwap_signals": metrics["liquidity_rejected_avwap_signals"],
            "ranking_not_selected_avwap_signals": metrics["ranking_not_selected_avwap_signals"],
            "counterfactual_liquidity_rejected_trades_closed": metrics["counterfactual_liquidity_rejected_trades_closed"],
            "counterfactual_liquidity_rejected_net_pnl": metrics["counterfactual_liquidity_rejected_net_pnl"],
            "signals_rejected_by_reason": _missed_reason_counts(run),
            "automatic_winner_declared": False,
        })
    return comparisons, thresholds, missed_rows


def _missed_reason_counts(run):
    counts = Counter()
    for item in run.missed_signals:
        if item.get("category") == "LIQUIDITY_REJECTED":
            counts.update(item.get("rejection_reasons", []))
    return dict(sorted(counts.items()))


def _max_drawdown(pnls: Iterable[float]) -> float:
    cumulative = peak = maximum = 0.0
    for pnl in pnls:
        cumulative += float(pnl)
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _subtract(left, right):
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return _round_num(float(left) - float(right))
    return None


def _round_num(value):
    if value is None:
        return None
    number = float(value)
    return round(number, 6) if math.isfinite(number) else None
