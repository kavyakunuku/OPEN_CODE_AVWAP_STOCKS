"""Point-in-time option-contract and option-chain liquidity scoring.

The scorer never fetches data and never assumes that a live value is historical.
Callers provide timestamped observations and candles; the evaluator filters every
input against the requested as-of timestamp before computing any feature.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Mapping, Optional, Sequence

from common.config import DEFAULTS
from common.models import Candle, OptionContract
from common.utils import IST, from_epoch

COMPONENT_NAMES = ("volume", "oi", "spread", "depth", "consistency")
REASON_CODES = (
    "INVALID_DATA",
    "WIDE_SPREAD",
    "LOW_VOLUME",
    "LOW_OI",
    "LOW_PREMIUM",
    "LOW_DEPTH",
    "LOW_LIQUIDITY_SCORE",
)


class LiquidityConfigError(ValueError):
    """Raised when liquidity scoring settings are unsafe or inconsistent."""


@dataclass(frozen=True)
class DepthLevel:
    price: Optional[float]
    quantity: Optional[float]


@dataclass(frozen=True)
class LiquidityObservation:
    """A captured option quote/chain observation; all fields share its timestamp.

    Observations may be partial: quote-feed snapshots often have price/volume,
    while option-chain snapshots carry OI, bid/ask and depth. The scorer selects
    the latest available value per field, but requires a valid two-sided quote
    snapshot for qualification.
    """

    observed_at: datetime
    last_price: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    oi_change: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_quantity: Optional[float] = None
    ask_quantity: Optional[float] = None
    bid_depth: Sequence[DepthLevel | Mapping] = field(default_factory=tuple)
    ask_depth: Sequence[DepthLevel | Mapping] = field(default_factory=tuple)


@dataclass(frozen=True)
class ContractLiquidityData:
    """All point-in-time inputs for one listed option contract."""

    contract: OptionContract
    observations: Sequence[LiquidityObservation] = field(default_factory=tuple)
    candles: Sequence[Candle] = field(default_factory=tuple)


@dataclass(frozen=True)
class ContractLiquidityResult:
    security_id: str
    underlying: str
    strike: float
    option_type: str
    expiry: str
    evaluated_at: datetime
    observation_at: Optional[datetime]
    quote_age_seconds: Optional[float]
    last_price: Optional[float]
    premium: Optional[float]
    volume: Optional[float]
    open_interest: Optional[float]
    oi_change: Optional[float]
    depth: Optional[float]
    spread_percent: Optional[float]
    component_scores: Mapping[str, float]
    score: float
    qualified: bool
    rejection_reasons: tuple[str, ...]

    @property
    def overall_score(self) -> float:
        """Explicitly named alias for callers serializing result reports."""
        return self.score

    @property
    def rejection_codes(self) -> tuple[str, ...]:
        return self.rejection_reasons


@dataclass(frozen=True)
class OptionChainLiquidityResult:
    """Aggregated chain health; CE and PE scores remain independently visible."""

    underlying: str
    expiry: str
    call_score: Optional[float]
    put_score: Optional[float]
    chain_score: float
    imbalance_ratio: float
    penalty_points: float
    call_contract_count: int
    put_contract_count: int


@dataclass(frozen=True)
class OptionChainLiquidityReport:
    contracts: tuple[ContractLiquidityResult, ...]
    chains: tuple[OptionChainLiquidityResult, ...]

    def by_security_id(self) -> dict[str, ContractLiquidityResult]:
        return {result.security_id: result for result in self.contracts}


@dataclass
class _Prepared:
    source: ContractLiquidityData
    as_of: datetime
    observations: list[tuple[datetime, LiquidityObservation]]
    bars: list[Candle]
    latest_observation_at: Optional[datetime] = None
    quote_at: Optional[datetime] = None
    quote_age_seconds: Optional[float] = None
    last_price: Optional[float] = None
    premium: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    oi_change: Optional[float] = None
    depth: Optional[float] = None
    spread_percent: Optional[float] = None
    consistency_score: float = 0.0
    invalid_data: bool = False


class OptionLiquidityEngine:
    """Score individual CE/PE contracts and aggregate option-chain health.

    Percentiles are calculated within the supplied evaluation batch, grouped by
    underlying, expiry and option side. For each contract, the target is ranked
    against the *other* available comparable contracts using mid-rank percentile;
    a lone contract or tied peers score 50. Weights are normalized if they do
    not sum to one. Missing component evidence contributes zero rather than
    silently redistributing its weight.
    """

    def __init__(self, config: Optional[dict] = None):
        defaults = copy.deepcopy(DEFAULTS["liquidity"])
        supplied = config or {}
        if isinstance(supplied, dict) and "liquidity" in supplied:
            supplied = supplied["liquidity"] or {}
        self.config = _merge(defaults, supplied)
        self._validate_config()
        self.weights = {
            name: float(self.config["weights"].get(name, 0.0))
            for name in COMPONENT_NAMES
        }
        total = sum(self.weights.values())
        self.weights = {name: weight / total for name, weight in self.weights.items()}

    def _validate_config(self) -> None:
        cfg = self.config
        weights = cfg.get("weights")
        if not isinstance(weights, dict) or set(weights) - set(COMPONENT_NAMES):
            raise LiquidityConfigError("weights must be a mapping of volume, oi, spread, depth and consistency")
        try:
            values = [float(weights.get(name, 0.0)) for name in COMPONENT_NAMES]
        except (TypeError, ValueError) as exc:
            raise LiquidityConfigError("liquidity weights must be numeric") from exc
        if any(not math.isfinite(value) or value < 0 for value in values) or sum(values) <= 0:
            raise LiquidityConfigError("liquidity weights must be finite, non-negative, and have a positive sum")

        for key in ("max_spread_percent", "preferred_spread_percent"):
            value = _finite_number(cfg.get(key))
            if value is None or value < 0:
                raise LiquidityConfigError(f"{key} must be a finite non-negative percentage")
            cfg[key] = value
        if cfg["max_spread_percent"] <= 0:
            raise LiquidityConfigError("max_spread_percent must be greater than zero")
        if cfg["preferred_spread_percent"] > cfg["max_spread_percent"]:
            raise LiquidityConfigError("preferred_spread_percent cannot exceed max_spread_percent")

        penalty = _finite_number(cfg.get("side_imbalance_penalty"))
        if penalty is None or not 0 <= penalty <= 1:
            raise LiquidityConfigError("side_imbalance_penalty must be between 0 and 1")
        cfg["side_imbalance_penalty"] = penalty

        max_age = cfg.get("max_quote_age_seconds")
        if max_age is not None:
            max_age_value = _finite_number(max_age)
            if max_age_value is None or max_age_value <= 0:
                raise LiquidityConfigError("max_quote_age_seconds must be null or a positive number")
            cfg["max_quote_age_seconds"] = max_age_value

        minimums = cfg.get("minimums")
        minimum_keys = {
            "liquidity_score", "volume", "volume_percentile", "oi", "oi_percentile",
            "premium", "depth",
        }
        if not isinstance(minimums, dict) or set(minimums) - minimum_keys:
            raise LiquidityConfigError(f"minimums keys must be from {sorted(minimum_keys)}")
        for key, value in minimums.items():
            if value is None:
                continue
            number = _finite_number(value)
            if number is None or number < 0:
                raise LiquidityConfigError(f"minimums.{key} must be null or finite and non-negative")
            if key in ("liquidity_score", "volume_percentile", "oi_percentile") and number > 100:
                raise LiquidityConfigError(f"minimums.{key} cannot exceed 100")
            minimums[key] = number

        windows = cfg.get("windows")
        window_keys = {
            "bar_interval_minutes", "recent_bars", "consistency_lookback_bars",
            "oi_change_lookback_minutes",
        }
        if not isinstance(windows, dict) or set(windows) - window_keys:
            raise LiquidityConfigError(f"windows keys must be from {sorted(window_keys)}")
        for key in window_keys:
            try:
                number = int(windows[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise LiquidityConfigError(f"windows.{key} must be a positive integer") from exc
            if number <= 0 or float(windows[key]) != number:
                raise LiquidityConfigError(f"windows.{key} must be a positive integer")
            windows[key] = number

    def score_contract(
        self,
        data: ContractLiquidityData,
        as_of: datetime | date | str,
        comparable_contracts: Sequence[ContractLiquidityData] = (),
    ) -> ContractLiquidityResult:
        """Score one contract against same-underlying/expiry/side peers."""
        inputs = [data] + [
            peer for peer in comparable_contracts
            if peer.contract.security_id != data.contract.security_id
        ]
        return self.score_contracts(inputs, as_of)[0]

    def score_contracts(
        self,
        contracts: Sequence[ContractLiquidityData],
        as_of: datetime | date | str,
    ) -> list[ContractLiquidityResult]:
        """Score a batch using only observations and completed bars available by T."""
        evaluation_time = _as_ist_datetime(as_of)
        ids = [item.contract.security_id for item in contracts]
        if len(ids) != len(set(ids)):
            raise ValueError("score_contracts requires unique contract security_ids")
        prepared = [self._prepare(item, evaluation_time) for item in contracts]
        groups: dict[tuple[str, str, str], list[int]] = {}
        for index, item in enumerate(prepared):
            contract = item.source.contract
            side = _normalise_option_type(contract.option_type)
            groups.setdefault(
                (contract.underlying.strip().upper(), str(contract.expiry), side), []
            ).append(index)

        results: list[ContractLiquidityResult] = []
        for index, item in enumerate(prepared):
            contract = item.source.contract
            group = groups[(contract.underlying.strip().upper(), str(contract.expiry),
                            _normalise_option_type(contract.option_type))]
            peers = [prepared[i] for i in group if i != index]
            results.append(self._score_prepared(item, peers))
        return results

    def score_option_chain(
        self,
        contracts: Sequence[ContractLiquidityData],
        as_of: datetime | date | str,
    ) -> OptionChainLiquidityReport:
        """Score contracts independently, then summarize CE/PE balance per chain."""
        results = self.score_contracts(contracts, as_of)
        groups: dict[tuple[str, str], list[ContractLiquidityResult]] = {}
        for result in results:
            groups.setdefault((result.underlying.strip().upper(), result.expiry), []).append(result)
        chains = tuple(
            self._aggregate_chain(underlying, expiry, items)
            for (underlying, expiry), items in sorted(groups.items())
        )
        return OptionChainLiquidityReport(tuple(results), chains)

    def _prepare(self, source: ContractLiquidityData, as_of: datetime) -> _Prepared:
        item = _Prepared(source=source, as_of=as_of, observations=[], bars=[])
        contract = source.contract
        if (
            not str(contract.security_id or "").strip()
            or not str(contract.underlying or "").strip()
            or not str(contract.expiry or "").strip()
            or _normalise_option_type(contract.option_type) not in {"CE", "PE"}
            or (_finite_number(contract.strike) or 0) <= 0
        ):
            item.invalid_data = True
        interval_minutes = self.config["windows"]["bar_interval_minutes"]
        for observation in source.observations:
            try:
                observed_at = _as_ist_datetime(observation.observed_at)
            except (TypeError, ValueError):
                item.invalid_data = True
                continue
            if observed_at <= as_of:
                item.observations.append((observed_at, observation))
        item.observations.sort(key=lambda pair: pair[0])
        if item.observations:
            item.latest_observation_at = item.observations[-1][0]
        else:
            item.invalid_data = True

        for candle in source.candles:
            if str(candle.security_id) != str(source.contract.security_id):
                continue
            try:
                start = from_epoch(int(candle.ts))
                if start + timedelta(minutes=interval_minutes) > as_of:
                    continue  # incomplete at T (also excludes any future candle)
                volume = _finite_number(candle.volume)
                if volume is None or volume < 0 or not _valid_ohlc(candle):
                    item.invalid_data = True
                    continue
                item.bars.append(candle)
            except (AttributeError, TypeError, ValueError, OverflowError, OSError):
                item.invalid_data = True
        # Duplicated bars are not allowed to inflate rolling volume or consistency.
        item.bars = list({int(bar.ts): bar for bar in item.bars}.values())
        item.bars.sort(key=lambda bar: int(bar.ts))
        consistency_window = self.config["windows"]["consistency_lookback_bars"]
        recent_consistency_bars = item.bars[-consistency_window:]
        if recent_consistency_bars:
            active = sum(1 for bar in recent_consistency_bars if float(bar.volume) > 0)
            item.consistency_score = 100.0 * active / len(recent_consistency_bars)

        self._prepare_quote_and_metrics(item)
        return item

    def _prepare_quote_and_metrics(self, item: _Prepared) -> None:
        observations = item.observations

        def latest(field_name: str):
            for observed_at, observation in reversed(observations):
                value = getattr(observation, field_name)
                if value is not None:
                    return value, observed_at, observation
            return None, None, None

        item.last_price, _, _ = latest("last_price")
        item.volume, _, _ = latest("volume")
        item.open_interest, _, oi_observation = latest("open_interest")
        explicit_oi_change, _, _ = latest("oi_change")
        if explicit_oi_change is not None:
            item.oi_change = _finite_number(explicit_oi_change)
            if item.oi_change is None:
                item.invalid_data = True

        if item.volume is not None:
            item.volume = _finite_number(item.volume)
            if item.volume is None or item.volume < 0:
                item.invalid_data = True
                item.volume = None
        elif item.bars:
            recent_count = self.config["windows"]["recent_bars"]
            item.volume = float(sum(max(0, int(bar.volume)) for bar in item.bars[-recent_count:]))
        else:
            item.invalid_data = True

        if item.open_interest is not None:
            item.open_interest = _finite_number(item.open_interest)
            if item.open_interest is None or item.open_interest < 0:
                item.invalid_data = True
                item.open_interest = None
        else:
            item.invalid_data = True

        # Use an explicit chain OI-change field when available; otherwise infer
        # only from archived OI observations bracketing the configured lookback.
        if item.oi_change is None and oi_observation is not None:
            lookback = timedelta(minutes=self.config["windows"]["oi_change_lookback_minutes"])
            cutoff = item.as_of - lookback
            current_oi = _finite_number(item.open_interest)
            prior_values = [
                (at, _finite_number(observation.open_interest))
                for at, observation in observations
                if at <= cutoff and observation.open_interest is not None
            ]
            prior_values = [(at, value) for at, value in prior_values if value is not None]
            if current_oi is not None and prior_values:
                item.oi_change = current_oi - max(prior_values, key=lambda pair: pair[0])[1]

        # A quote snapshot is a coherent two-sided book, not a bid from one time
        # and an ask from another. A newer price-only quote does not erase it.
        book_observations = [
            (at, observation) for at, observation in observations
            if observation.bid is not None or observation.ask is not None
        ]
        if book_observations:
            item.quote_at, book = book_observations[-1]
            bid = _finite_number(book.bid)
            ask = _finite_number(book.ask)
            if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
                item.invalid_data = True
            else:
                midpoint = (bid + ask) / 2.0
                item.spread_percent = 100.0 * (ask - bid) / midpoint
                item.premium = bid  # executable sell-side premium, not a stale LTP
                item.quote_age_seconds = max(0.0, (item.as_of - item.quote_at).total_seconds())
                max_age = self.config.get("max_quote_age_seconds")
                if max_age is not None and item.quote_age_seconds > max_age:
                    item.invalid_data = True
        else:
            item.invalid_data = True

        if item.last_price is not None:
            item.last_price = _finite_number(item.last_price)
            if item.last_price is None or item.last_price <= 0:
                item.invalid_data = True
        elif item.bars:
            item.last_price = _finite_number(item.bars[-1].close)

        depth_observations = [
            (at, observation) for at, observation in observations
            if _has_depth(observation)
        ]
        if depth_observations:
            _, depth_observation = depth_observations[-1]
            bid_depth = _side_quantity(depth_observation.bid_depth, depth_observation.bid_quantity)
            ask_depth = _side_quantity(depth_observation.ask_depth, depth_observation.ask_quantity)
            if bid_depth is not None and ask_depth is not None:
                item.depth = min(bid_depth, ask_depth)
            elif _invalid_depth(depth_observation):
                item.invalid_data = True

        # Validate any supplied depth quantity, including snapshots not selected
        # as the latest depth; silently accepting negative size would be unsafe.
        if any(_invalid_depth(observation) for _, observation in observations):
            item.invalid_data = True

    def _score_prepared(
        self, item: _Prepared, peers: Sequence[_Prepared]
    ) -> ContractLiquidityResult:
        peer_volumes = [peer.volume for peer in peers if peer.volume is not None]
        peer_oi = [peer.open_interest for peer in peers if peer.open_interest is not None]
        peer_depth = [peer.depth for peer in peers if peer.depth is not None]

        components = {
            "volume": _percentile(item.volume, peer_volumes),
            "oi": _percentile(item.open_interest, peer_oi),
            "spread": _spread_score(
                item.spread_percent,
                self.config["preferred_spread_percent"],
                self.config["max_spread_percent"],
            ),
            "depth": _percentile(item.depth, peer_depth),
            "consistency": item.consistency_score,
        }
        score = sum(self.weights[name] * components[name] for name in COMPONENT_NAMES)
        score = round(min(100.0, max(0.0, score)), 4)

        minimums = self.config["minimums"]
        reasons: list[str] = []
        if item.invalid_data:
            reasons.append("INVALID_DATA")
        if item.spread_percent is not None and item.spread_percent > self.config["max_spread_percent"]:
            reasons.append("WIDE_SPREAD")
        if _below(item.volume, minimums.get("volume")) or _below(
            components["volume"], minimums.get("volume_percentile")
        ):
            reasons.append("LOW_VOLUME")
        if _below(item.open_interest, minimums.get("oi")) or _below(
            components["oi"], minimums.get("oi_percentile")
        ):
            reasons.append("LOW_OI")
        if _below(item.premium, minimums.get("premium")):
            reasons.append("LOW_PREMIUM")
        if _below(item.depth, minimums.get("depth")):
            reasons.append("LOW_DEPTH")
        if minimums.get("liquidity_score") is not None and score < minimums["liquidity_score"]:
            reasons.append("LOW_LIQUIDITY_SCORE")

        contract = item.source.contract
        # Stable order and de-duplication make rejection output easy to audit.
        reason_codes = tuple(code for code in REASON_CODES if code in reasons)
        return ContractLiquidityResult(
            security_id=str(contract.security_id),
            underlying=str(contract.underlying),
            strike=float(contract.strike),
            option_type=_normalise_option_type(contract.option_type),
            expiry=str(contract.expiry),
            evaluated_at=item.as_of,
            observation_at=item.latest_observation_at,
            quote_age_seconds=item.quote_age_seconds,
            last_price=item.last_price,
            premium=item.premium,
            volume=item.volume,
            open_interest=item.open_interest,
            oi_change=item.oi_change,
            depth=item.depth,
            spread_percent=item.spread_percent,
            component_scores=components,
            score=score,
            qualified=not reason_codes,
            rejection_reasons=reason_codes,
        )

    def _aggregate_chain(
        self,
        underlying: str,
        expiry: str,
        results: Sequence[ContractLiquidityResult],
    ) -> OptionChainLiquidityResult:
        calls = [result.score for result in results if result.option_type == "CE"]
        puts = [result.score for result in results if result.option_type == "PE"]
        call_score = sum(calls) / len(calls) if calls else None
        put_score = sum(puts) / len(puts) if puts else None
        call_value = call_score or 0.0
        put_value = put_score or 0.0
        if not calls and not puts:
            base = 0.0
            imbalance = 0.0
        else:
            base = (call_value + put_value) / 2.0
            total = call_value + put_value
            imbalance = abs(call_value - put_value) / total if total > 0 else 0.0
            if not calls or not puts:
                imbalance = 1.0
        penalty_points = 100.0 * self.config["side_imbalance_penalty"] * imbalance
        chain_score = round(max(0.0, base - penalty_points), 4)
        return OptionChainLiquidityResult(
            underlying=underlying,
            expiry=expiry,
            call_score=round(call_score, 4) if call_score is not None else None,
            put_score=round(put_score, 4) if put_score is not None else None,
            chain_score=chain_score,
            imbalance_ratio=round(imbalance, 6),
            penalty_points=round(penalty_points, 4),
            call_contract_count=len(calls),
            put_contract_count=len(puts),
        )


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        raise LiquidityConfigError("liquidity config must be a mapping")
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _finite_number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _as_ist_datetime(value: datetime | date | str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid timestamp {value!r}; use ISO-8601") from exc
    else:
        raise TypeError("as_of and observation timestamps must be datetime/date/ISO-8601")
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _normalise_option_type(value: str) -> str:
    text = str(value or "").strip().upper()
    if text in {"CE", "CALL"}:
        return "CE"
    if text in {"PE", "PUT"}:
        return "PE"
    return text


def _percentile(value: Optional[float], peers: Sequence[float]) -> float:
    """Mid-rank percentile against peers; absent peer population maps to 50."""
    if value is None:
        return 0.0
    valid = [number for peer in peers if (number := _finite_number(peer)) is not None]
    if not valid:
        return 50.0
    less = sum(peer < value for peer in valid)
    equal = sum(math.isclose(peer, value, rel_tol=1e-12, abs_tol=1e-12) for peer in valid)
    return min(100.0, max(0.0, 100.0 * (less + 0.5 * equal) / len(valid)))


def _spread_score(spread: Optional[float], preferred: float, maximum: float) -> float:
    if spread is None or spread < 0:
        return 0.0
    if spread <= preferred:
        return 100.0
    if spread >= maximum:
        return 0.0
    return 100.0 * (maximum - spread) / (maximum - preferred)


def _below(value: Optional[float], minimum: Optional[float]) -> bool:
    if minimum is None:
        return False
    return value is None or value < minimum


def _valid_ohlc(candle: Candle) -> bool:
    values = [_finite_number(getattr(candle, name, None)) for name in
              ("open", "high", "low", "close")]
    if any(value is None or value <= 0 for value in values):
        return False
    open_price, high, low, close = values
    return high >= max(open_price, low, close) and low <= min(open_price, high, close)


def _has_depth(observation: LiquidityObservation) -> bool:
    return bool(observation.bid_depth or observation.ask_depth) or any(
        value is not None for value in (observation.bid_quantity, observation.ask_quantity)
    )


def _level_quantity(level) -> Optional[float]:
    if isinstance(level, Mapping):
        quantity = level.get("quantity", level.get("qty"))
    else:
        quantity = getattr(level, "quantity", None)
    number = _finite_number(quantity)
    return number


def _side_quantity(levels, top_quantity) -> Optional[float]:
    if levels:
        quantities = [_level_quantity(level) for level in levels]
        if any(value is None or value < 0 for value in quantities):
            return None
        return float(sum(quantities))
    quantity = _finite_number(top_quantity)
    return quantity if quantity is not None and quantity >= 0 else None


def _invalid_depth(observation: LiquidityObservation) -> bool:
    for value in (observation.bid_quantity, observation.ask_quantity):
        number = _finite_number(value) if value is not None else None
        if value is not None and (number is None or number < 0):
            return True
    for level in (*(observation.bid_depth or ()), *(observation.ask_depth or ())):
        quantity = _level_quantity(level)
        if quantity is None or quantity < 0:
            return True
        price = level.get("price") if isinstance(level, Mapping) else getattr(level, "price", None)
        if price is not None:
            price_number = _finite_number(price)
            if price_number is None or price_number <= 0:
                return True
    return False
