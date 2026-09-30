"""Deterministic, point-in-time NSE option opportunity ranking.

This is a scanner only. It combines Phase 2 discovered contracts, Phase 3
liquidity results, and Phase 4 underlying volatility results. It never creates
signals, changes positions, or places orders. Phase 3 qualification is a hard
gate: no amount of volatility can rescue a contract that failed liquidity.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Mapping, Optional, Sequence

from common.config import DEFAULTS
from common.models import OptionContract
from common.utils import IST
from market.contract_discovery import (
    DiscoveredContract,
    OptionContractDiscoveryResult,
)
from market.liquidity import (
    ContractLiquidityResult,
    OptionChainLiquidityReport,
)
from market.volatility import (
    OptionVolatilityResult,
    VolatilityAnalysisResult,
)

COMPONENTS = (
    "liquidity",
    "volatility",
    "chain_quality",
    "execution_quality",
)
EXECUTION_COMPONENTS = ("spread", "freshness")


class OpportunityConfigError(ValueError):
    """Raised when opportunity weights or selection settings are invalid."""


class OpportunityInputError(ValueError):
    """Raised when Phase 2–4 results are inconsistent or not point-in-time."""


@dataclass(frozen=True)
class OpportunityScannerInput:
    """Point-in-time Phase 2–4 results to rank.

    ``existing_position_underlyings`` is the safest position exclusion input.
    Candidate security IDs supplied in ``existing_position_security_ids`` are
    also resolved to their underlying and block that whole underlying, so a
    higher-ranked contract cannot prompt a contract switch.
    """

    as_of: datetime | str
    discoveries: Sequence[OptionContractDiscoveryResult]
    liquidity_report: OptionChainLiquidityReport
    volatility_by_underlying: Mapping[str, VolatilityAnalysisResult]
    existing_position_underlyings: Sequence[str] = field(default_factory=tuple)
    existing_position_security_ids: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class ExcludedOpportunity:
    symbol: str
    security_id: str
    contract_name: str
    expiry: str
    strike: float
    option_type: str
    reasons: tuple[str, ...]
    liquidity_rejection_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "contract_name": self.contract_name,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "reasons": list(self.reasons),
            "liquidity_rejection_reasons": list(self.liquidity_rejection_reasons),
        }


@dataclass(frozen=True)
class RankedContractOpportunity:
    rank: int
    symbol: str
    contract_name: str
    security_id: str
    instrument_token: str
    expiry: str
    strike: float
    option_type: str
    moneyness: str
    liquidity_score: float
    volatility_score: float
    chain_quality_score: Optional[float]
    execution_quality_score: float
    opportunity_score: float
    premium: Optional[float]
    spread_percent: Optional[float]
    quote_age_seconds: Optional[float]
    option_movement_percent: Optional[float]
    component_scores: Mapping[str, float]
    execution_components: Mapping[str, float]
    missing_components: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "contract_name": self.contract_name,
            "security_id": self.security_id,
            "instrument_token": self.instrument_token,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "moneyness": self.moneyness,
            "liquidity_score": self.liquidity_score,
            "volatility_score": self.volatility_score,
            "chain_quality_score": self.chain_quality_score,
            "execution_quality_score": self.execution_quality_score,
            "opportunity_score": self.opportunity_score,
            "premium": self.premium,
            "spread_percent": self.spread_percent,
            "quote_age_seconds": self.quote_age_seconds,
            "option_movement_percent": self.option_movement_percent,
            "component_scores": dict(self.component_scores),
            "execution_components": dict(self.execution_components),
            "missing_components": list(self.missing_components),
        }


@dataclass(frozen=True)
class RankedStockOpportunity:
    rank: int
    symbol: str
    liquidity_score: float
    volatility_score: float
    chain_quality_score: float
    execution_quality_score: float
    opportunity_score: float
    qualified_contracts: int
    missing_components: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "liquidity_score": self.liquidity_score,
            "volatility_score": self.volatility_score,
            "chain_quality_score": self.chain_quality_score,
            "execution_quality_score": self.execution_quality_score,
            "opportunity_score": self.opportunity_score,
            "qualified_contracts": self.qualified_contracts,
            "missing_components": list(self.missing_components),
        }


@dataclass(frozen=True)
class OpportunityRankingResult:
    timestamp: datetime
    stocks: tuple[RankedStockOpportunity, ...]
    contracts: tuple[RankedContractOpportunity, ...]
    top_opportunities: tuple[RankedContractOpportunity, ...]
    excluded_contracts: tuple[ExcludedOpportunity, ...]
    excluded_position_underlyings: tuple[str, ...]
    weights: Mapping[str, float]
    selection: Mapping[str, int]

    def to_dict(self) -> dict:
        """Return a JSON-ready scanner result with stable array ordering."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "stocks": [item.to_dict() for item in self.stocks],
            "contracts": [item.to_dict() for item in self.contracts],
            "top_opportunities": [item.to_dict() for item in self.top_opportunities],
            "excluded_contracts": [item.to_dict() for item in self.excluded_contracts],
            "excluded_position_underlyings": list(self.excluded_position_underlyings),
            "weights": dict(self.weights),
            "selection": dict(self.selection),
        }

    def to_json(self, *, indent: Optional[int] = 2) -> str:
        """Serialize deterministically; NaN/Infinity are never emitted as JSON."""
        return json.dumps(
            self.to_dict(), indent=indent, sort_keys=True, allow_nan=False
        )

    def to_csv(self, section: str = "contracts") -> str:
        """Return CSV for ``stocks``, ``contracts`` or ``top_opportunities``."""
        if section == "stocks":
            rows = [item.to_dict() for item in self.stocks]
            fields = (
                "rank", "symbol", "liquidity_score", "volatility_score",
                "chain_quality_score", "execution_quality_score", "opportunity_score",
                "qualified_contracts", "missing_components",
            )
        elif section in {"contracts", "top_opportunities"}:
            items = self.contracts if section == "contracts" else self.top_opportunities
            rows = [item.to_dict() for item in items]
            fields = (
                "rank", "symbol", "contract_name", "security_id", "expiry", "strike",
                "option_type", "moneyness", "liquidity_score", "volatility_score",
                "chain_quality_score", "execution_quality_score", "opportunity_score",
                "premium", "spread_percent", "quote_age_seconds",
                "option_movement_percent", "missing_components",
            )
        else:
            raise ValueError("section must be 'stocks', 'contracts', or 'top_opportunities'")

        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            clean = dict(row)
            if isinstance(clean.get("missing_components"), list):
                clean["missing_components"] = "|".join(clean["missing_components"])
            writer.writerow(clean)
        return stream.getvalue()

    def write_json(self, path: str | Path, *, indent: Optional[int] = 2) -> None:
        Path(path).write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")

    def write_csv(self, path: str | Path, section: str = "contracts") -> None:
        Path(path).write_text(self.to_csv(section), encoding="utf-8", newline="")


class OpportunityRankingEngine:
    """Rank only new, Phase-3-qualified option opportunities; never trade."""

    def __init__(self, config: Optional[dict] = None):
        supplied = config or {}
        if isinstance(supplied, dict) and "opportunity" in supplied:
            supplied = supplied["opportunity"] or {}
        self.config = _merge(copy.deepcopy(DEFAULTS["opportunity"]), supplied)
        self._validate_config()
        weight_values = self.config["weights"]
        total = sum(weight_values[name] for name in COMPONENTS)
        self.weights = {
            name: weight_values[name] / total for name in COMPONENTS
        }
        execution = self.config["execution"]["weights"]
        execution_total = sum(execution[name] for name in EXECUTION_COMPONENTS)
        self.execution_weights = {
            name: execution[name] / execution_total for name in EXECUTION_COMPONENTS
        }

    def _validate_config(self) -> None:
        cfg = self.config
        weights = cfg.get("weights")
        if not isinstance(weights, dict) or set(weights) - set(COMPONENTS):
            raise OpportunityConfigError(
                "weights must contain only liquidity, volatility, chain_quality, and execution_quality"
            )
        _validate_weights(weights, COMPONENTS, "opportunity")
        if weights["liquidity"] <= weights["volatility"]:
            raise OpportunityConfigError(
                "liquidity weight must remain greater than volatility to preserve the primary filter"
            )

        selection = cfg.get("selection")
        selection_keys = {"max_stocks", "max_contracts_per_stock", "max_top_opportunities"}
        if not isinstance(selection, dict) or set(selection) - selection_keys:
            raise OpportunityConfigError(f"selection keys must be from {sorted(selection_keys)}")
        for key in selection_keys:
            value = selection.get(key)
            if isinstance(value, bool):
                raise OpportunityConfigError(f"selection.{key} must be a non-negative integer")
            try:
                number = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise OpportunityConfigError(f"selection.{key} must be a non-negative integer") from exc
            if number < 0 or float(value) != number:
                raise OpportunityConfigError(f"selection.{key} must be a non-negative integer")
            selection[key] = number

        execution = cfg.get("execution")
        if not isinstance(execution, dict) or set(execution) - {
            "preferred_spread_percent", "max_spread_percent", "max_quote_age_seconds", "weights"
        }:
            raise OpportunityConfigError("execution contains an unknown setting")
        for key in ("preferred_spread_percent", "max_spread_percent", "max_quote_age_seconds"):
            value = _finite_number(execution.get(key))
            if value is None or value <= 0:
                raise OpportunityConfigError(f"execution.{key} must be positive and finite")
            execution[key] = value
        if execution["preferred_spread_percent"] > execution["max_spread_percent"]:
            raise OpportunityConfigError("execution preferred spread cannot exceed max spread")
        execution_weights = execution.get("weights")
        if not isinstance(execution_weights, dict) or set(execution_weights) - set(EXECUTION_COMPONENTS):
            raise OpportunityConfigError("execution.weights must contain only spread and freshness")
        _validate_weights(execution_weights, EXECUTION_COMPONENTS, "execution")

    def rank(self, data: OpportunityScannerInput) -> OpportunityRankingResult:
        """Produce deterministic stock and contract rankings from Phase 2–4 results.

        Component results must be evaluated at exactly ``data.as_of``. Phase 2
        snapshots may be older but never newer. Missing chain-quality evidence
        is explicitly scored as zero; missing Phase 3 or Phase 4 results exclude
        the contract instead of manufacturing a complete opportunity score.
        """
        if not isinstance(data, OpportunityScannerInput):
            raise OpportunityInputError("rank expects an OpportunityScannerInput")
        as_of = _as_of_datetime(data.as_of)
        if any(not isinstance(item, OptionContractDiscoveryResult)
               for item in data.discoveries):
            raise OpportunityInputError("discoveries must contain Phase 2 discovery results")
        discoveries = sorted(
            data.discoveries,
            key=lambda item: (
                _normalise_symbol(item.underlying), _expiry_text(item.expiry),
                _number_sort(item.atm_strike),
            ),
        )
        _validate_discoveries(discoveries, as_of)
        liquidity_by_id, chains_by_key = _index_liquidity(data.liquidity_report, as_of)
        volatility_by_symbol = _index_volatility(data.volatility_by_underlying, as_of)

        discovered_rows: list[tuple[OptionContractDiscoveryResult, DiscoveredContract]] = []
        seen_security_ids: set[str] = set()
        for discovery in discoveries:
            for expected_side, side_items in (("CE", discovery.calls), ("PE", discovery.puts)):
                if any(_normalise_option_type(item.contract.option_type) != expected_side
                       for item in side_items):
                    raise OpportunityInputError(
                        f"Phase 2 {expected_side} list for {_normalise_symbol(discovery.underlying)} "
                        "contains a contract for the other option side"
                    )
            items = sorted(
                discovery.calls + discovery.puts,
                key=lambda item: _contract_sort_key(item.contract),
            )
            for item in items:
                contract = item.contract
                _validate_discovered_contract(discovery, contract)
                security_id = str(contract.security_id).strip()
                if security_id in seen_security_ids:
                    raise OpportunityInputError(f"duplicate discovered security_id {security_id!r}")
                seen_security_ids.add(security_id)
                discovered_rows.append((discovery, item))

        blocked_underlyings = {
            _normalise_symbol(value)
            for value in data.existing_position_underlyings
            if _normalise_symbol(value)
        }
        open_security_ids = {str(value).strip() for value in data.existing_position_security_ids if str(value).strip()}
        for discovery, item in discovered_rows:
            if str(item.contract.security_id).strip() in open_security_ids:
                blocked_underlyings.add(_normalise_symbol(discovery.underlying))

        excluded: list[ExcludedOpportunity] = []
        candidates: list[RankedContractOpportunity] = []
        for discovery, discovered in discovered_rows:
            contract = discovered.contract
            symbol = _normalise_symbol(discovery.underlying)
            security_id = str(contract.security_id).strip()
            expiry = _expiry_text(contract.expiry)
            contract_name = _contract_name(contract)
            if symbol in blocked_underlyings:
                excluded.append(_excluded(
                    contract, symbol, ("EXISTING_POSITION_UNDERLYING",),
                ))
                continue

            liquidity = liquidity_by_id.get(security_id)
            if liquidity is None:
                excluded.append(_excluded(contract, symbol, ("MISSING_LIQUIDITY_RESULT",)))
                continue
            _validate_liquidity_matches(liquidity, contract, as_of)
            if not liquidity.qualified:
                excluded.append(_excluded(
                    contract, symbol, ("LIQUIDITY_NOT_QUALIFIED",),
                    liquidity.rejection_reasons,
                ))
                continue

            volatility = volatility_by_symbol.get(symbol)
            if volatility is None:
                excluded.append(_excluded(contract, symbol, ("MISSING_VOLATILITY_RESULT",)))
                continue

            chain = chains_by_key.get((symbol, expiry))
            chain_score = chain.chain_score if chain is not None else None
            if chain_score is None:
                missing_chain = True
                chain_component = 0.0
            else:
                missing_chain = False
                chain_component = _score_value(chain_score, "chain_quality_score")

            execution_components, execution_missing = self._execution_components(liquidity)
            execution_score = _weighted(
                execution_components, self.execution_weights, EXECUTION_COMPONENTS
            )
            missing: list[str] = []
            if missing_chain:
                missing.append("chain_quality")
            missing.extend(execution_missing)
            component_scores = {
                "liquidity": _score_value(liquidity.score, "liquidity_score"),
                "volatility": _score_value(volatility.volatility_score, "volatility_score"),
                "chain_quality": chain_component,
                "execution_quality": execution_score,
            }
            opportunity_score = _weighted(component_scores, self.weights, COMPONENTS)
            option_metrics = _find_option_metrics(volatility.option_results, security_id)
            if option_metrics is not None:
                _validate_option_metrics(option_metrics, contract)

            candidates.append(RankedContractOpportunity(
                rank=0,
                symbol=symbol,
                contract_name=contract_name,
                security_id=security_id,
                instrument_token=str(contract.instrument_token),
                expiry=expiry,
                strike=_positive_number(contract.strike, f"strike[{security_id}]"),
                option_type=_normalise_option_type(contract.option_type),
                moneyness=str(discovered.moneyness),
                liquidity_score=component_scores["liquidity"],
                volatility_score=component_scores["volatility"],
                chain_quality_score=(chain_component if not missing_chain else None),
                execution_quality_score=execution_score,
                opportunity_score=opportunity_score,
                premium=_optional_nonnegative(liquidity.premium, "premium"),
                spread_percent=_optional_nonnegative(liquidity.spread_percent, "spread_percent"),
                quote_age_seconds=_optional_nonnegative(liquidity.quote_age_seconds, "quote_age_seconds"),
                option_movement_percent=(
                    _optional_nonnegative(option_metrics.price_movement_percent, "option_movement_percent")
                    if option_metrics is not None else None
                ),
                component_scores=component_scores,
                execution_components=execution_components,
                missing_components=tuple(dict.fromkeys(missing)),
            ))

        candidates.sort(key=_ranked_contract_sort_key)
        per_stock: dict[str, list[RankedContractOpportunity]] = {}
        limit_per_stock = self.config["selection"]["max_contracts_per_stock"]
        for candidate in candidates:
            bucket = per_stock.setdefault(candidate.symbol, [])
            if len(bucket) < limit_per_stock:
                bucket.append(candidate)

        stock_rows = [
            self._aggregate_stock(symbol, rows)
            for symbol, rows in sorted(per_stock.items()) if rows
        ]
        stock_rows.sort(key=_ranked_stock_sort_key)
        max_stocks = self.config["selection"]["max_stocks"]
        selected_stocks = [replace(item, rank=index + 1)
                           for index, item in enumerate(stock_rows[:max_stocks])]
        selected_symbols = {item.symbol for item in selected_stocks}

        selected_contracts = [
            candidate for symbol, rows in per_stock.items() if symbol in selected_symbols
            for candidate in rows
        ]
        selected_contracts.sort(key=_ranked_contract_sort_key)
        ranked_contracts = tuple(
            replace(item, rank=index + 1) for index, item in enumerate(selected_contracts)
        )
        top_count = self.config["selection"]["max_top_opportunities"]
        top_opportunities = ranked_contracts[:top_count]

        excluded.sort(key=_excluded_sort_key)
        return OpportunityRankingResult(
            timestamp=as_of,
            stocks=tuple(selected_stocks),
            contracts=ranked_contracts,
            top_opportunities=top_opportunities,
            excluded_contracts=tuple(excluded),
            excluded_position_underlyings=tuple(sorted(blocked_underlyings)),
            weights=dict(self.weights),
            selection=dict(self.config["selection"]),
        )

    def _execution_components(
        self, liquidity: ContractLiquidityResult
    ) -> tuple[dict[str, float], tuple[str, ...]]:
        cfg = self.config["execution"]
        spread = _finite_number(liquidity.spread_percent)
        age = _finite_number(liquidity.quote_age_seconds)
        missing: list[str] = []
        if spread is None or spread < 0:
            spread_score = 0.0
            missing.append("execution_spread")
        else:
            preferred = cfg["preferred_spread_percent"]
            maximum = cfg["max_spread_percent"]
            if spread <= preferred:
                spread_score = 100.0
            elif spread >= maximum:
                spread_score = 0.0
            else:
                spread_score = 100.0 * (maximum - spread) / (maximum - preferred)
        if age is None or age < 0:
            freshness_score = 0.0
            missing.append("execution_freshness")
        else:
            max_age = cfg["max_quote_age_seconds"]
            freshness_score = 100.0 * max(0.0, 1.0 - age / max_age)
        return {
            "spread": round(min(100.0, max(0.0, spread_score)), 4),
            "freshness": round(min(100.0, max(0.0, freshness_score)), 4),
        }, tuple(missing)

    def _aggregate_stock(
        self,
        symbol: str,
        contracts: Sequence[RankedContractOpportunity],
    ) -> RankedStockOpportunity:
        count = len(contracts)
        liquidity = _mean(item.liquidity_score for item in contracts)
        volatility = _mean(item.volatility_score for item in contracts)
        chain_quality = _mean(
            item.chain_quality_score if item.chain_quality_score is not None else 0.0
            for item in contracts
        )
        execution = _mean(item.execution_quality_score for item in contracts)
        components = {
            "liquidity": liquidity,
            "volatility": volatility,
            "chain_quality": chain_quality,
            "execution_quality": execution,
        }
        missing = tuple(sorted({
            value for item in contracts for value in item.missing_components
        }))
        return RankedStockOpportunity(
            rank=0,
            symbol=symbol,
            liquidity_score=round(liquidity, 4),
            volatility_score=round(volatility, 4),
            chain_quality_score=round(chain_quality, 4),
            execution_quality_score=round(execution, 4),
            opportunity_score=_weighted(components, self.weights, COMPONENTS),
            qualified_contracts=count,
            missing_components=missing,
        )


# Friendly name for applications that treat the ranker as a scanner.
OptionOpportunityScanner = OpportunityRankingEngine


def _validate_weights(weights: dict, names: Sequence[str], label: str) -> None:
    try:
        numbers = {name: float(weights.get(name, 0.0)) for name in names}
    except (TypeError, ValueError, OverflowError) as exc:
        raise OpportunityConfigError(f"{label} weights must be numeric") from exc
    if any(not math.isfinite(value) or value < 0 for value in numbers.values()):
        raise OpportunityConfigError(f"{label} weights must be finite and non-negative")
    total = sum(numbers.values())
    if total <= 0:
        raise OpportunityConfigError(f"{label} weights must have a positive sum")
    weights.clear()
    weights.update(numbers)


def _merge(base: dict, override: dict) -> dict:
    if not isinstance(override, dict):
        raise OpportunityConfigError("opportunity config must be a mapping")
    out = copy.deepcopy(base)
    for key, value in override.items():
        if str(key).startswith("_"):
            continue
        if key not in out:
            raise OpportunityConfigError(f"unknown opportunity setting {key!r}")
        if isinstance(value, dict) and isinstance(out[key], dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _index_liquidity(
    report: OptionChainLiquidityReport,
    as_of: datetime,
) -> tuple[dict[str, ContractLiquidityResult], dict[tuple[str, str], object]]:
    if not isinstance(report, OptionChainLiquidityReport):
        raise OpportunityInputError("liquidity_report must be a Phase 3 OptionChainLiquidityReport")
    by_id: dict[str, ContractLiquidityResult] = {}
    for result in report.contracts:
        security_id = str(result.security_id).strip()
        if not security_id or security_id in by_id:
            raise OpportunityInputError(f"missing or duplicate liquidity security_id {security_id!r}")
        if _as_of_datetime(result.evaluated_at) != as_of:
            raise OpportunityInputError(
                f"liquidity result for {security_id} was not evaluated at requested timestamp"
            )
        by_id[security_id] = result

    chains: dict[tuple[str, str], object] = {}
    for chain in report.chains:
        key = (_normalise_symbol(chain.underlying), _expiry_text(chain.expiry))
        if key in chains:
            raise OpportunityInputError(f"duplicate Phase 3 chain quality result for {key}")
        _score_value(chain.chain_score, "chain_score")
        chains[key] = chain
    return by_id, chains


def _index_volatility(
    values: Mapping[str, VolatilityAnalysisResult],
    as_of: datetime,
) -> dict[str, VolatilityAnalysisResult]:
    if not isinstance(values, Mapping):
        raise OpportunityInputError("volatility_by_underlying must be a mapping")
    out: dict[str, VolatilityAnalysisResult] = {}
    for symbol, result in values.items():
        key = _normalise_symbol(symbol)
        if not key or key in out:
            raise OpportunityInputError(f"empty or duplicate volatility symbol {symbol!r}")
        if not isinstance(result, VolatilityAnalysisResult):
            raise OpportunityInputError(f"volatility result for {key} is not a Phase 4 result")
        if _normalise_symbol(result.symbol) != key:
            raise OpportunityInputError(f"volatility result symbol does not match key {key}")
        if _as_of_datetime(result.evaluated_at) != as_of:
            raise OpportunityInputError(
                f"volatility result for {key} was not evaluated at requested timestamp"
            )
        _score_value(result.volatility_score, f"volatility_score[{key}]")
        out[key] = result
    return out


def _validate_discoveries(
    discoveries: Sequence[OptionContractDiscoveryResult], as_of: datetime
) -> None:
    seen: set[tuple[str, str]] = set()
    for discovery in discoveries:
        if not isinstance(discovery, OptionContractDiscoveryResult):
            raise OpportunityInputError("discoveries must contain Phase 2 discovery results")
        symbol = _normalise_symbol(discovery.underlying)
        expiry = _expiry_text(discovery.expiry)
        key = (symbol, expiry)
        if not symbol or key in seen:
            raise OpportunityInputError(f"empty or duplicate Phase 2 discovery result {key}")
        seen.add(key)
        if _as_of_datetime(discovery.timestamp) != as_of:
            raise OpportunityInputError(
                f"Phase 2 discovery for {symbol} was not evaluated at requested timestamp"
            )
        if _as_of_datetime(discovery.snapshot_as_of) > as_of:
            raise OpportunityInputError(f"Phase 2 master snapshot for {symbol} is from the future")


def _validate_discovered_contract(
    discovery: OptionContractDiscoveryResult, contract: OptionContract
) -> None:
    symbol = _normalise_symbol(discovery.underlying)
    if not str(contract.security_id or "").strip():
        raise OpportunityInputError("Phase 2 discovery contains an empty security_id")
    if _normalise_symbol(contract.underlying) != symbol:
        raise OpportunityInputError(
            f"contract {contract.security_id} underlying does not match its discovery result"
        )
    if _expiry_text(contract.expiry) != _expiry_text(discovery.expiry):
        raise OpportunityInputError(
            f"contract {contract.security_id} expiry does not match its discovery result"
        )
    _normalise_option_type(contract.option_type)
    _positive_number(contract.strike, f"strike[{contract.security_id}]")


def _validate_liquidity_matches(
    result: ContractLiquidityResult, contract: OptionContract, as_of: datetime
) -> None:
    if _as_of_datetime(result.evaluated_at) != as_of:
        raise OpportunityInputError(
            f"liquidity result for {contract.security_id} was not evaluated at requested timestamp"
        )
    if _normalise_symbol(result.underlying) != _normalise_symbol(contract.underlying):
        raise OpportunityInputError(f"liquidity underlying mismatch for {contract.security_id}")
    if _normalise_option_type(result.option_type) != _normalise_option_type(contract.option_type):
        raise OpportunityInputError(f"liquidity option type mismatch for {contract.security_id}")
    if not math.isclose(_positive_number(result.strike, "liquidity strike"),
                        _positive_number(contract.strike, "contract strike"),
                        rel_tol=0.0, abs_tol=1e-8):
        raise OpportunityInputError(f"liquidity strike mismatch for {contract.security_id}")
    if _expiry_text(result.expiry) != _expiry_text(contract.expiry):
        raise OpportunityInputError(f"liquidity expiry mismatch for {contract.security_id}")


def _find_option_metrics(
    results: Sequence[OptionVolatilityResult], security_id: str
) -> Optional[OptionVolatilityResult]:
    matches = [item for item in results if str(item.security_id) == security_id]
    if len(matches) > 1:
        raise OpportunityInputError(f"duplicate Phase 4 option metrics for {security_id}")
    return matches[0] if matches else None


def _validate_option_metrics(
    result: OptionVolatilityResult, contract: OptionContract
) -> None:
    if _normalise_option_type(result.option_type) != _normalise_option_type(contract.option_type):
        raise OpportunityInputError(f"Phase 4 option type mismatch for {contract.security_id}")
    if not math.isclose(_positive_number(result.strike, "volatility strike"),
                        _positive_number(contract.strike, "contract strike"),
                        rel_tol=0.0, abs_tol=1e-8):
        raise OpportunityInputError(f"Phase 4 strike mismatch for {contract.security_id}")
    if _expiry_text(result.expiry) != _expiry_text(contract.expiry):
        raise OpportunityInputError(f"Phase 4 expiry mismatch for {contract.security_id}")


def _weighted(values: Mapping[str, float], weights: Mapping[str, float], names: Sequence[str]) -> float:
    score = sum(weights[name] * _score_value(values.get(name), name) for name in names)
    return round(min(100.0, max(0.0, score)), 4)


def _score_value(value, label: str) -> float:
    number = _finite_number(value)
    if number is None or not 0.0 <= number <= 100.0:
        raise OpportunityInputError(f"{label} must be a finite score between 0 and 100")
    return number


def _positive_number(value, label: str) -> float:
    number = _finite_number(value)
    if number is None or number <= 0:
        raise OpportunityInputError(f"{label} must be a finite positive number")
    return number


def _optional_nonnegative(value, label: str) -> Optional[float]:
    if value is None:
        return None
    number = _finite_number(value)
    if number is None or number < 0:
        raise OpportunityInputError(f"{label} must be a finite non-negative number or None")
    return number


def _finite_number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalise_symbol(value) -> str:
    return str(value or "").strip().upper()


def _normalise_option_type(value) -> str:
    text = str(value or "").strip().upper()
    if text in {"CE", "CALL"}:
        return "CE"
    if text in {"PE", "PUT"}:
        return "PE"
    raise OpportunityInputError(f"invalid option_type {value!r}; expected CE/CALL or PE/PUT")


def _expiry_text(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise OpportunityInputError(f"invalid expiry {value!r}") from exc


def _contract_name(contract: OptionContract) -> str:
    return str(contract.symbol or contract.name)


def _contract_sort_key(contract: OptionContract) -> tuple:
    return (
        _normalise_symbol(contract.underlying), _expiry_text(contract.expiry),
        _normalise_option_type(contract.option_type), _number_sort(contract.strike),
        str(contract.security_id),
    )


def _ranked_contract_sort_key(item: RankedContractOpportunity) -> tuple:
    return (
        -item.opportunity_score,
        -item.liquidity_score,
        -item.volatility_score,
        -(item.chain_quality_score or 0.0),
        -item.execution_quality_score,
        item.symbol,
        item.expiry,
        item.option_type,
        item.strike,
        item.security_id,
    )


def _ranked_stock_sort_key(item: RankedStockOpportunity) -> tuple:
    return (
        -item.opportunity_score,
        -item.liquidity_score,
        -item.volatility_score,
        -item.chain_quality_score,
        -item.execution_quality_score,
        item.symbol,
    )


def _excluded(
    contract: OptionContract,
    symbol: str,
    reasons: Sequence[str],
    liquidity_rejection_reasons: Sequence[str] = (),
) -> ExcludedOpportunity:
    return ExcludedOpportunity(
        symbol=symbol,
        security_id=str(contract.security_id),
        contract_name=_contract_name(contract),
        expiry=_expiry_text(contract.expiry),
        strike=_positive_number(contract.strike, "strike"),
        option_type=_normalise_option_type(contract.option_type),
        reasons=tuple(reasons),
        liquidity_rejection_reasons=tuple(liquidity_rejection_reasons),
    )


def _excluded_sort_key(item: ExcludedOpportunity) -> tuple:
    return (
        item.symbol, item.expiry, item.option_type, item.strike,
        item.security_id, item.reasons,
    )


def _number_sort(value) -> float:
    number = _finite_number(value)
    return number if number is not None else math.inf


def _mean(values) -> float:
    numbers = list(values)
    return sum(numbers) / len(numbers) if numbers else 0.0


def _as_of_datetime(value: datetime | str) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        raise OpportunityInputError("as_of must include a time; pass an exact datetime")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or ("T" not in text and " " not in text):
            raise OpportunityInputError("as_of must include a time; use an ISO-8601 datetime")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise OpportunityInputError(f"invalid as_of timestamp {value!r}") from exc
    else:
        raise OpportunityInputError("as_of must be a datetime or ISO-8601 datetime string")
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)
