"""Point-in-time volatility analytics for NSE stock and option price series.

The module is deliberately separate from AVWAP signals and order/risk logic.
It scores movement only; callers must preserve Phase 3 liquidity qualification
as an independent, primary gate.
"""
from __future__ import annotations

import copy
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Mapping, Optional, Sequence

from common.models import OptionContract
from common.config import DEFAULTS
from common.utils import (
    IST,
    SESSION_CLOSE,
    SESSION_OPEN,
    all_candle_starts,
    from_epoch,
    parse_epoch,
)

VOLATILITY_COMPONENTS = (
    "atr_15m_percent",
    "realized_volatility",
    "intraday_volatility",
    "option_movement",
)
BAR_INTERVAL_MINUTES = 15


class VolatilityConfigError(ValueError):
    """Raised when volatility weights, targets, or lookbacks are invalid."""


@dataclass(frozen=True)
class OHLCBar:
    """One OHLC bar whose ``ts`` is the bar start (epoch seconds or datetime)."""

    ts: object
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None


@dataclass(frozen=True)
class PriceObservation:
    """Timestamped last-traded-price observation; never implicitly backdated."""

    observed_at: datetime | date | str | int | float
    price: float


@dataclass(frozen=True)
class ImpliedVolatilityObservation:
    """An actual IV value reported by a source at a known capture timestamp."""

    observed_at: datetime | date | str | int | float
    value: float


@dataclass(frozen=True)
class OptionVolatilityInput:
    contract: OptionContract
    bars_15m: Sequence[OHLCBar] = field(default_factory=tuple)
    price_observations: Sequence[PriceObservation] = field(default_factory=tuple)
    implied_volatility_observations: Sequence[ImpliedVolatilityObservation] = field(
        default_factory=tuple
    )
    # Only options that already pass the Phase 3 liquidity gate can contribute
    # to the underlying's option-movement component.
    liquidity_qualified: bool = False


@dataclass(frozen=True)
class UnderlyingVolatilityInput:
    symbol: str
    daily_bars: Sequence[OHLCBar] = field(default_factory=tuple)
    bars_15m: Sequence[OHLCBar] = field(default_factory=tuple)
    price_observations: Sequence[PriceObservation] = field(default_factory=tuple)
    options: Sequence[OptionVolatilityInput] = field(default_factory=tuple)


@dataclass(frozen=True)
class OptionVolatilityResult:
    security_id: str
    option_type: str
    strike: float
    expiry: str
    current_price: Optional[float]
    price_movement: Optional[float]
    price_movement_percent: Optional[float]
    atr: Optional[float]
    atr_percent: Optional[float]
    realized_volatility: Optional[float]
    implied_volatility: Optional[float]
    liquidity_qualified: bool
    used_for_score: bool
    insufficient_data: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "security_id": self.security_id,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiry": self.expiry,
            "current_price": self.current_price,
            "price_movement": self.price_movement,
            "price_movement_percent": self.price_movement_percent,
            "atr": self.atr,
            "atr_percent": self.atr_percent,
            "realized_volatility": self.realized_volatility,
            "implied_volatility": self.implied_volatility,
            "liquidity_qualified": self.liquidity_qualified,
            "used_for_score": self.used_for_score,
            "insufficient_data": list(self.insufficient_data),
        }


@dataclass(frozen=True)
class VolatilityAnalysisResult:
    symbol: str
    evaluated_at: datetime
    current_price: Optional[float]
    atr: Optional[float]
    atr_percent: Optional[float]
    atr_15m: Optional[float]
    atr_15m_percent: Optional[float]
    realized_volatility: Optional[float]
    intraday_volatility: Optional[float]
    option_movement_percent: Optional[float]
    volatility_score: float
    component_scores: Mapping[str, float]
    component_values: Mapping[str, Optional[float]]
    daily_bars_used: int
    intraday_bars_used: int
    qualified_options_used: int
    insufficient_data: tuple[str, ...]
    data_quality: tuple[str, ...]
    option_results: tuple[OptionVolatilityResult, ...]

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation matching the public output contract."""
        return {
            "symbol": self.symbol,
            "evaluated_at": self.evaluated_at.isoformat(),
            "current_price": self.current_price,
            "atr": self.atr,
            "atr_percent": self.atr_percent,
            "atr_15m": self.atr_15m,
            "atr_15m_percent": self.atr_15m_percent,
            "realized_volatility": self.realized_volatility,
            "intraday_volatility": self.intraday_volatility,
            "option_movement_percent": self.option_movement_percent,
            "volatility_score": self.volatility_score,
            "component_scores": dict(self.component_scores),
            "component_values": dict(self.component_values),
            "daily_bars_used": self.daily_bars_used,
            "intraday_bars_used": self.intraday_bars_used,
            "qualified_options_used": self.qualified_options_used,
            "insufficient_data": list(self.insufficient_data),
            "data_quality": list(self.data_quality),
            "options": [option.to_dict() for option in self.option_results],
        }


@dataclass(frozen=True)
class _Bar:
    start: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float


class VolatilityAnalysisEngine:
    """Calculate ATR and annualized realized volatility without look-ahead."""

    def __init__(self, config: Optional[dict] = None):
        defaults = copy.deepcopy(DEFAULTS["volatility"])
        supplied = config or {}
        if isinstance(supplied, dict) and "volatility" in supplied:
            supplied = supplied["volatility"] or {}
        self.config = _merge(defaults, supplied)
        self._validate_config()
        total = sum(float(self.config["weights"][name]) for name in VOLATILITY_COMPONENTS)
        self.weights = {
            name: float(self.config["weights"][name]) / total
            for name in VOLATILITY_COMPONENTS
        }

    def _validate_config(self) -> None:
        cfg = self.config
        weights = cfg.get("weights")
        if not isinstance(weights, dict) or set(weights) - set(VOLATILITY_COMPONENTS):
            raise VolatilityConfigError(
                "weights must contain only atr_15m_percent, realized_volatility, "
                "intraday_volatility, and option_movement"
            )
        try:
            weight_values = [float(weights.get(name, 0.0)) for name in VOLATILITY_COMPONENTS]
        except (TypeError, ValueError) as exc:
            raise VolatilityConfigError("volatility weights must be numeric") from exc
        if any(not math.isfinite(value) or value < 0 for value in weight_values) or sum(weight_values) <= 0:
            raise VolatilityConfigError("volatility weights must be finite, non-negative, with a positive sum")

        targets = cfg.get("normalization_targets")
        target_keys = {
            "atr_15m_percent", "realized_volatility", "intraday_volatility",
            "option_movement_percent",
        }
        if not isinstance(targets, dict) or set(targets) - target_keys:
            raise VolatilityConfigError("normalization_targets contains an unknown metric")
        for key in target_keys:
            value = _finite_number(targets.get(key))
            if value is None or value <= 0:
                raise VolatilityConfigError(f"normalization_targets.{key} must be positive and finite")
            targets[key] = value

        windows = cfg.get("windows")
        window_keys = {
            "atr_period", "atr_15m_period", "realized_volatility_periods",
            "intraday_volatility_periods", "option_atr_period",
            "option_realized_volatility_periods", "option_movement_bars",
            "bars_per_session", "trading_days_per_year",
        }
        if not isinstance(windows, dict) or set(windows) - window_keys:
            raise VolatilityConfigError("windows contains an unknown lookback")
        for key in window_keys:
            try:
                value = int(windows[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise VolatilityConfigError(f"windows.{key} must be a positive integer") from exc
            if value <= 0 or float(windows[key]) != value:
                raise VolatilityConfigError(f"windows.{key} must be a positive integer")
            if key in {
                "realized_volatility_periods",
                "intraday_volatility_periods",
                "option_realized_volatility_periods",
            } and value < 2:
                raise VolatilityConfigError(f"windows.{key} must be at least 2")
            windows[key] = value

    def analyze(
        self,
        data: UnderlyingVolatilityInput,
        as_of: datetime | date | str,
    ) -> VolatilityAnalysisResult:
        """Compute one stock's metrics using only data available by ``as_of``."""
        evaluation_time = _as_ist_datetime(as_of)
        data_quality: list[str] = []
        insufficient: list[str] = []

        intraday_bars, invalid_intraday = _prepare_bars(
            data.bars_15m, evaluation_time, daily=False
        )
        if invalid_intraday:
            data_quality.append("INVALID_INTRADAY_OHLC")
        supplied_daily, invalid_daily = _prepare_bars(
            data.daily_bars, evaluation_time, daily=True
        )
        if invalid_daily:
            data_quality.append("INVALID_DAILY_OHLC")
        daily_bars = _merge_daily_bars(
            supplied_daily,
            _aggregate_full_sessions(intraday_bars, evaluation_time),
        )

        current_price, invalid_prices = _latest_price(
            data.price_observations,
            [*daily_bars, *intraday_bars],
            evaluation_time,
        )
        if invalid_prices:
            data_quality.append("INVALID_PRICE_OBSERVATION")

        windows = self.config["windows"]
        daily_atr = calculate_atr(daily_bars, windows["atr_period"])
        daily_atr_percent = _percent_of_price(daily_atr, current_price)
        atr_15m = calculate_atr(intraday_bars, windows["atr_15m_period"])
        atr_15m_percent = _percent_of_price(atr_15m, current_price)
        realized = calculate_realized_volatility(
            [bar.close for bar in daily_bars],
            windows["realized_volatility_periods"],
            windows["trading_days_per_year"],
        )
        intraday = calculate_intraday_volatility(
            intraday_bars,
            windows["intraday_volatility_periods"],
            windows["bars_per_session"],
            windows["trading_days_per_year"],
            BAR_INTERVAL_MINUTES,
        )

        option_results = tuple(
            self._analyze_option(option, evaluation_time, data_quality)
            for option in data.options
        )
        qualified_movements = [
            result.price_movement_percent
            for result in option_results
            if result.liquidity_qualified and result.price_movement_percent is not None
        ]
        option_movement = statistics.median(qualified_movements) if qualified_movements else None

        if daily_atr is None:
            insufficient.append("ATR")
        if daily_atr_percent is None:
            insufficient.append("ATR_PERCENT")
        if atr_15m is None:
            insufficient.append("ATR_15M")
        if atr_15m_percent is None:
            insufficient.append("ATR_15M_PERCENT")
        if realized is None:
            insufficient.append("REALIZED_VOLATILITY")
        if intraday is None:
            insufficient.append("INTRADAY_VOLATILITY")
        if option_movement is None:
            insufficient.append("OPTION_MOVEMENT")

        targets = self.config["normalization_targets"]
        component_values: dict[str, Optional[float]] = {
            "atr_15m_percent": atr_15m_percent,
            "realized_volatility": realized,
            "intraday_volatility": intraday,
            "option_movement_percent": option_movement,
        }
        component_scores = {
            "atr_15m_percent": _target_score(
                atr_15m_percent, targets["atr_15m_percent"]
            ),
            "realized_volatility": _target_score(
                realized, targets["realized_volatility"]
            ),
            "intraday_volatility": _target_score(
                intraday, targets["intraday_volatility"]
            ),
            "option_movement": _target_score(
                option_movement, targets["option_movement_percent"]
            ),
        }
        score = sum(
            self.weights[name] * component_scores[name]
            for name in VOLATILITY_COMPONENTS
        )

        return VolatilityAnalysisResult(
            symbol=str(data.symbol),
            evaluated_at=evaluation_time,
            current_price=current_price,
            atr=daily_atr,
            atr_percent=daily_atr_percent,
            atr_15m=atr_15m,
            atr_15m_percent=atr_15m_percent,
            realized_volatility=realized,
            intraday_volatility=intraday,
            option_movement_percent=option_movement,
            volatility_score=round(min(100.0, max(0.0, score)), 4),
            component_scores=component_scores,
            component_values=component_values,
            daily_bars_used=len(daily_bars),
            intraday_bars_used=len(intraday_bars),
            qualified_options_used=sum(1 for result in option_results if result.used_for_score),
            insufficient_data=tuple(dict.fromkeys(insufficient)),
            data_quality=tuple(dict.fromkeys(data_quality)),
            option_results=option_results,
        )

    def _analyze_option(
        self,
        option: OptionVolatilityInput,
        as_of: datetime,
        data_quality: list[str],
    ) -> OptionVolatilityResult:
        bars, invalid_bars = _prepare_bars(option.bars_15m, as_of, daily=False)
        if invalid_bars:
            data_quality.append(f"INVALID_OPTION_OHLC:{option.contract.security_id}")
        price, invalid_prices = _latest_price(
            option.price_observations, bars, as_of
        )
        if invalid_prices:
            data_quality.append(f"INVALID_OPTION_PRICE:{option.contract.security_id}")

        windows = self.config["windows"]
        atr_value = calculate_atr(bars, windows["option_atr_period"])
        atr_pct = _percent_of_price(atr_value, price)
        rv = calculate_intraday_volatility(
            bars,
            windows["option_realized_volatility_periods"],
            windows["bars_per_session"],
            windows["trading_days_per_year"],
            BAR_INTERVAL_MINUTES,
        )
        movement, movement_pct = _price_movement(
            bars, price, windows["option_movement_bars"]
        )
        iv = _latest_iv(option.implied_volatility_observations, as_of)

        missing = []
        if atr_value is None:
            missing.append("OPTION_ATR")
        if atr_pct is None:
            missing.append("OPTION_ATR_PERCENT")
        if rv is None:
            missing.append("OPTION_REALIZED_VOLATILITY")
        if movement_pct is None:
            missing.append("OPTION_PRICE_MOVEMENT")
        used = bool(option.liquidity_qualified and movement_pct is not None)
        contract = option.contract
        return OptionVolatilityResult(
            security_id=str(contract.security_id),
            option_type=str(contract.option_type).upper(),
            strike=float(contract.strike),
            expiry=str(contract.expiry),
            current_price=price,
            price_movement=movement,
            price_movement_percent=movement_pct,
            atr=atr_value,
            atr_percent=atr_pct,
            realized_volatility=rv,
            implied_volatility=iv,
            liquidity_qualified=bool(option.liquidity_qualified),
            used_for_score=used,
            insufficient_data=tuple(missing),
        )


def calculate_atr(bars: Sequence, period: int = 14) -> Optional[float]:
    """Return Wilder-smoothed ATR from ordered bars; ``None`` if insufficient."""
    if period <= 0:
        raise ValueError("period must be a positive integer")
    ranges: list[float] = []
    previous_close: Optional[float] = None
    for bar in bars:
        values = _ohlc_values(bar)
        if values is None:
            return None
        _, high, low, close = values
        true_range = high - low if previous_close is None else max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
        ranges.append(true_range)
        previous_close = close
    if len(ranges) < period:
        return None
    current = sum(ranges[:period]) / period
    for true_range in ranges[period:]:
        current = ((current * (period - 1)) + true_range) / period
    return current


def calculate_atr_percent(bars: Sequence, period: int = 14) -> Optional[float]:
    """Return ATR as a percentage of the last supplied bar's close."""
    value = calculate_atr(bars, period)
    values = _ohlc_values(bars[-1]) if bars else None
    if value is None or values is None or values[3] <= 0:
        return None
    return 100.0 * value / values[3]


def calculate_realized_volatility(
    closes: Sequence[float],
    lookback_periods: int = 20,
    periods_per_year: int = 252,
) -> Optional[float]:
    """Annualized sample standard deviation of the last log-return window (%).

    ``lookback_periods`` is a count of returns, so one additional close is
    required. Inputs must already be sorted and filtered to the requested time.
    """
    if lookback_periods <= 1 or periods_per_year <= 0:
        raise ValueError("lookback_periods must exceed one and periods_per_year must be positive")
    values = [_finite_number(value) for value in closes]
    if any(value is None or value <= 0 for value in values):
        return None
    if len(values) < lookback_periods + 1:
        return None
    selected = values[-(lookback_periods + 1):]
    returns = [math.log(current) - math.log(previous)
               for previous, current in zip(selected, selected[1:])]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(periods_per_year) * 100.0


def calculate_intraday_volatility(
    bars: Sequence,
    lookback_periods: int = 50,
    bars_per_session: int = 25,
    trading_days_per_year: int = 252,
    interval_minutes: int = BAR_INTERVAL_MINUTES,
) -> Optional[float]:
    """Annualized same-session 15-minute log-return stdev, in percent.

    Only adjacent bars in the same IST session contribute; overnight gaps and
    missing-interval jumps are not misclassified as intraday volatility. The
    bars passed here should already be point-in-time, completed candles.
    """
    if lookback_periods <= 1 or min(bars_per_session, trading_days_per_year, interval_minutes) <= 0:
        raise ValueError("intraday lookbacks and annualization periods must be positive")
    normalized = []
    for bar in bars:
        try:
            start = _as_ist_datetime(_bar_timestamp(bar))
            values = _ohlc_values(bar)
            if values is not None and SESSION_OPEN <= start.time() < SESSION_CLOSE:
                normalized.append((start, values[3]))
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    normalized.sort(key=lambda item: item[0])
    returns = []
    step = timedelta(minutes=interval_minutes)
    for (prior_time, prior_close), (current_time, current_close) in zip(
        normalized, normalized[1:]
    ):
        if current_time.date() != prior_time.date() or current_time - prior_time != step:
            continue
        returns.append(math.log(current_close) - math.log(prior_close))
    if len(returns) < lookback_periods:
        return None
    selected = returns[-lookback_periods:]
    periods_per_year = bars_per_session * trading_days_per_year
    return statistics.stdev(selected) * math.sqrt(periods_per_year) * 100.0


def _prepare_bars(
    source: Sequence,
    as_of: datetime,
    *,
    daily: bool,
) -> tuple[list[_Bar], int]:
    out: list[_Bar] = []
    invalid = 0
    for raw in source:
        try:
            start = _as_ist_datetime(_bar_timestamp(raw))
        except (TypeError, ValueError, OverflowError, OSError):
            invalid += 1
            continue
        available_at = _session_close(start.date()) if daily else start + timedelta(
            minutes=BAR_INTERVAL_MINUTES
        )
        if available_at > as_of:
            continue  # incomplete/future bar: it is not usable at T
        if not daily and (
            start.time() < SESSION_OPEN
            or start.time() >= SESSION_CLOSE
            or available_at > _session_close(start.date())
        ):
            continue
        values = _ohlc_values(raw)
        if values is None:
            invalid += 1
            continue
        open_price, high, low, close = values
        out.append(_Bar(start, available_at, open_price, high, low, close))
    # Last valid duplicate wins; duplicates never inflate rolling measures.
    unique = {bar.start: bar for bar in out}
    return sorted(unique.values(), key=lambda bar: bar.start), invalid


def _aggregate_full_sessions(bars: Sequence[_Bar], as_of: datetime) -> list[_Bar]:
    grouped: dict[date, dict[datetime, _Bar]] = {}
    for bar in bars:
        grouped.setdefault(bar.start.date(), {})[bar.start] = bar
    aggregated = []
    for session_day, session_bars in sorted(grouped.items()):
        close_time = _session_close(session_day)
        if close_time > as_of:
            continue
        day_start = datetime.combine(session_day, time.min, tzinfo=IST)
        expected = all_candle_starts(day_start, BAR_INTERVAL_MINUTES)
        expected_starts = set(expected)
        if not expected_starts or not expected_starts.issubset(session_bars):
            continue  # do not build a daily high/low from a partial session
        ordered = [session_bars[start] for start in expected]
        aggregated.append(_Bar(
            start=datetime.combine(session_day, SESSION_OPEN, tzinfo=IST),
            available_at=close_time,
            open=ordered[0].open,
            high=max(bar.high for bar in ordered),
            low=min(bar.low for bar in ordered),
            close=ordered[-1].close,
        ))
    return aggregated


def _merge_daily_bars(primary: Sequence[_Bar], fallback: Sequence[_Bar]) -> list[_Bar]:
    by_day = {bar.start.date(): bar for bar in fallback}
    by_day.update({bar.start.date(): bar for bar in primary})
    return sorted(by_day.values(), key=lambda bar: bar.start)


def _latest_price(
    observations: Sequence[PriceObservation],
    bars: Sequence[_Bar],
    as_of: datetime,
) -> tuple[Optional[float], bool]:
    candidates: list[tuple[datetime, object]] = [
        (bar.available_at, bar.close) for bar in bars if bar.available_at <= as_of
    ]
    invalid = False
    for observation in observations:
        try:
            observed_at = _as_ist_datetime(observation.observed_at)
        except (TypeError, ValueError, OverflowError, OSError):
            invalid = True
            continue
        if observed_at <= as_of:
            candidates.append((observed_at, observation.price))
    if not candidates:
        return None, invalid
    _, (_, raw_price) = max(
        enumerate(candidates), key=lambda indexed: (indexed[1][0], indexed[0])
    )
    price = _finite_number(raw_price)
    if price is None or price <= 0:
        return None, True
    return price, invalid


def _latest_iv(
    observations: Sequence[ImpliedVolatilityObservation],
    as_of: datetime,
) -> Optional[float]:
    eligible = []
    for observation in observations:
        try:
            observed_at = _as_ist_datetime(observation.observed_at)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if observed_at <= as_of:
            eligible.append((observed_at, observation.value))
    if not eligible:
        return None
    _, raw_value = max(eligible, key=lambda item: item[0])
    value = _finite_number(raw_value)
    return value if value is not None and value > 0 else None


def _price_movement(
    bars: Sequence[_Bar],
    current_price: Optional[float],
    lookback_bars: int,
) -> tuple[Optional[float], Optional[float]]:
    if len(bars) < lookback_bars:
        return None, None
    selected = list(bars[-lookback_bars:])
    prices = [value for bar in selected for value in (bar.high, bar.low)]
    if current_price is not None and current_price > 0:
        prices.append(current_price)
    movement = max(prices) - min(prices)
    denominator = current_price or selected[-1].close
    if denominator <= 0:
        return None, None
    return movement, 100.0 * movement / denominator


def _ohlc_values(bar) -> Optional[tuple[float, float, float, float]]:
    if isinstance(bar, Mapping):
        raw = tuple(bar.get(name) for name in ("open", "high", "low", "close"))
    else:
        raw = tuple(getattr(bar, name, None) for name in ("open", "high", "low", "close"))
    values = tuple(_finite_number(value) for value in raw)
    if any(value is None or value <= 0 for value in values):
        return None
    open_price, high, low, close = values
    if high < max(open_price, low, close) or low > min(open_price, high, close):
        return None
    return open_price, high, low, close


def _bar_timestamp(bar):
    if isinstance(bar, Mapping):
        return bar.get("ts", bar.get("timestamp", bar.get("start")))
    return getattr(bar, "ts", getattr(bar, "timestamp", getattr(bar, "start", None)))


def _as_ist_datetime(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, SESSION_OPEN)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ValueError("timestamp must be finite")
        if timestamp > 1e12:
            timestamp /= 1000.0
        dt = from_epoch(int(timestamp))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        if text.isdigit():
            parsed = parse_epoch(text)
            if parsed is None:
                raise ValueError(f"invalid epoch timestamp {value!r}")
            dt = from_epoch(parsed)
        else:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
    else:
        raise TypeError("timestamp must be datetime/date/epoch/ISO-8601")
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _session_close(day: date) -> datetime:
    return datetime.combine(day, SESSION_CLOSE, tzinfo=IST)


def _percent_of_price(value: Optional[float], price: Optional[float]) -> Optional[float]:
    if value is None or price is None or price <= 0:
        return None
    return 100.0 * value / price


def _target_score(value: Optional[float], target: float) -> float:
    if value is None or value < 0:
        return 0.0
    return min(100.0, 100.0 * value / target)


def _finite_number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        raise VolatilityConfigError("volatility config must be a mapping")
    for key, value in override.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out
