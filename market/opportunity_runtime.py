"""Runtime assembly for the Phase 2–5 opportunity scanner.

This layer connects timestamped Dhan snapshots and candles to the existing
contract-discovery, liquidity, volatility, and ranking engines. It only returns
ranked opportunities; it has no SignalEngine, position manager, or broker
reference and cannot create signals or orders.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import Callable, Mapping, Optional, Sequence

from common.config import get as cfg_get
from common.models import OptionContract
from common.utils import IST, from_epoch, now_ist
from dhan.liquidity import DhanLiquidityAdapter
from dhan.option_chain import OptionChainData
from dhan.volatility import DhanVolatilityAdapter
from market.contract_discovery import (
    MAX_ITM_STRIKES,
    OptionContractCatalog,
    OptionContractDiscovery,
)
from market.liquidity import OptionLiquidityEngine
from market.opportunity import (
    OpportunityRankingEngine,
    OpportunityRankingResult,
    OpportunityScannerInput,
)
from market.volatility import VolatilityAnalysisEngine

log = logging.getLogger("avwap.market.opportunity_runtime")


class OpportunityScanPipeline:
    """Build and rank one point-in-time Phase 2–5 scanner snapshot.

    The caller owns chain refresh pacing and supplies the chain snapshots from
    the app's existing ``PacedChainClient``. Underlying historical candles are
    fetched as NSE_EQ/EQUITY data; current option and underlying quotes are
    timestamped by ``DhanMarketData``. All component engines receive one exact
    evaluation timestamp and independently reject future/incomplete inputs.
    """

    def __init__(
        self,
        cfg: dict,
        *,
        market_data,
        master_contracts: Sequence[OptionContract],
        validated_symbols: Sequence[str],
        underlying_ids: Mapping[str, str],
        master_snapshot_at: datetime,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.market_data = market_data
        self.validated_symbols = tuple(dict.fromkeys(
            str(symbol).strip().upper() for symbol in validated_symbols
            if str(symbol).strip()
        ))
        self.underlying_ids = {
            str(symbol).strip().upper(): str(security_id)
            for symbol, security_id in underlying_ids.items()
        }
        self.now_fn = now_fn or now_ist
        self.sleep_fn = sleep_fn

        try:
            requested_itm = int(cfg_get(cfg, "strategy.itm_strikes_per_side", 4))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("strategy.itm_strikes_per_side must be an integer") from exc
        if requested_itm < 0 or requested_itm > MAX_ITM_STRIKES:
            raise ValueError(f"strategy.itm_strikes_per_side must be in 0..{MAX_ITM_STRIKES}")
        self.max_itm = requested_itm

        try:
            self.history_days = int(cfg_get(cfg, "opportunity.underlying_history_days", 90))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("opportunity.underlying_history_days must be a positive integer") from exc
        if self.history_days <= 0:
            raise ValueError("opportunity.underlying_history_days must be a positive integer")
        self.history_gap_seconds = float(cfg_get(
            cfg, "market_data.history_request_gap_seconds", 1.0
        ))
        if not math.isfinite(self.history_gap_seconds) or self.history_gap_seconds < 0:
            raise ValueError("market_data.history_request_gap_seconds must be finite and non-negative")

        self.catalog = OptionContractCatalog()
        self.catalog.add_snapshot(
            master_snapshot_at,
            master_contracts,
            source="Phase 1 validated Dhan instrument master",
        )
        self.discovery = OptionContractDiscovery(
            self.catalog,
            validated_symbols=self.validated_symbols,
            expiry_switch_day=int(cfg_get(cfg, "strategy.expiry_switch_day", 24)),
        )
        self.liquidity = OptionLiquidityEngine(cfg)
        self.volatility = VolatilityAnalysisEngine(cfg)
        self.ranker = OpportunityRankingEngine(cfg)
        self.liquidity_adapter = DhanLiquidityAdapter(market_data)
        self.volatility_adapter = DhanVolatilityAdapter()
        self.last_discovered_contracts: dict[str, OptionContract] = {}
        self.last_discoveries: tuple = ()
        self.last_liquidity_report = None
        self.last_volatility_by_underlying: dict = {}

    def fetch_underlying_bars(
        self,
        *,
        symbols: Optional[Sequence[str]] = None,
        as_of: Optional[datetime] = None,
    ) -> dict[str, list]:
        """Fetch real NSE_EQ 15-minute history for Phase 4.

        A failed symbol is omitted rather than filled with synthetic data. The
        volatility engine subsequently uses only bars whose 15-minute close is
        at or before its evaluation timestamp.
        """
        if self.market_data is None:
            return {}
        end = _as_ist_datetime(as_of or self.now_fn())
        start = end - timedelta(days=self.history_days)
        requested = self.validated_symbols if symbols is None else tuple(
            dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
        )
        out: dict[str, list] = {}
        request_count = 0
        for symbol in requested:
            security_id = self.underlying_ids.get(symbol)
            if not security_id:
                log.warning("Opportunity scan: no NSE_EQ security id for %s", symbol)
                continue
            if request_count and self.history_gap_seconds:
                self.sleep_fn(self.history_gap_seconds)
            request_count += 1
            try:
                bars = self.market_data.intraday_candles(
                    security_id,
                    start,
                    end,
                    interval_minutes=15,
                    instrument="EQUITY",
                    exchange_segment="NSE_EQ",
                )
            except Exception as exc:
                log.warning("Opportunity scan: underlying history failed for %s: %s", symbol, exc)
                continue
            if bars:
                out[symbol] = list(bars)
        return out

    def scan(
        self,
        *,
        chain_snapshots: Mapping[str, OptionChainData],
        underlying_bars_by_symbol: Mapping[str, Sequence],
        existing_position_underlyings: Sequence[str] = (),
        existing_position_security_ids: Sequence[str] = (),
        as_of: Optional[datetime] = None,
    ) -> OpportunityRankingResult:
        """Assemble Phase 2–4 results and return the deterministic Phase 5 rank."""
        chains = {
            str(symbol).strip().upper(): chain
            for symbol, chain in (chain_snapshots or {}).items()
            if str(symbol).strip() and chain is not None
        }
        bars_by_symbol = {
            str(symbol).strip().upper(): tuple(bars or ())
            for symbol, bars in (underlying_bars_by_symbol or {}).items()
            if str(symbol).strip()
        }

        option_quotes: dict = {}
        underlying_quotes: dict = {}
        equity_ids = [
            self.underlying_ids[symbol]
            for symbol in self.validated_symbols
            if symbol in self.underlying_ids
        ]
        if self.market_data is not None and equity_ids:
            try:
                underlying_quotes = self.market_data.quotes_segment(equity_ids, "NSE_EQ") or {}
            except Exception as exc:
                log.warning("Opportunity scan: underlying quote request failed: %s", exc)
                underlying_quotes = {}
        underlying_quote_by_symbol = {
            symbol: underlying_quotes.get(self.underlying_ids.get(symbol, ""))
            or underlying_quotes.get(str(self.underlying_ids.get(symbol, "")))
            for symbol in self.validated_symbols
        }

        # Resolve the option contracts from the freshest real, at-or-before-T
        # spot available. Pulling NSE_EQ first ensures the subsequent NSE_FNO
        # quote batch covers contracts around the current spot, not a stale
        # chain spot that may have shifted strikes.
        preliminary_time = _as_ist_datetime(as_of or self.now_fn())
        preliminary_discoveries = []
        for symbol in self.validated_symbols:
            chain = chains.get(symbol)
            spot = _latest_spot(
                chain, underlying_quote_by_symbol.get(symbol), preliminary_time
            )
            if spot is not None:
                try:
                    preliminary_discoveries.append(self.discovery.discover_option_contracts(
                        symbol,
                        preliminary_time,
                        max_itm=self.max_itm,
                        underlying_price=spot,
                    ))
                except Exception as exc:
                    log.debug("Opportunity scan: Phase 2 discovery unavailable for %s: %s", symbol, exc)

        # Marketfeed/quote returns current LTP, volume, OI and available book
        # depth. The timestamped option chain remains the source for any fields
        # the quote response omits; no historical book data is inferred.
        preliminary_contracts = [
            contract
            for discovery in preliminary_discoveries
            for contract in discovery.contracts
        ]
        if self.market_data is not None and preliminary_contracts:
            try:
                option_quotes = self.market_data.quotes([
                    str(contract.security_id) for contract in preliminary_contracts
                ]) or {}
            except Exception as exc:
                log.warning("Opportunity scan: option quote request failed: %s", exc)
                option_quotes = {}

        evaluation_time = _as_ist_datetime(as_of or self.now_fn())
        quote_by_id = {str(key): value for key, value in option_quotes.items()}

        discoveries = []
        for symbol in self.validated_symbols:
            chain = chains.get(symbol)
            quote = underlying_quote_by_symbol.get(symbol)
            spot = _latest_spot(chain, quote, evaluation_time)
            if spot is None:
                continue
            try:
                result = self.discovery.discover_option_contracts(
                    symbol,
                    evaluation_time,
                    max_itm=self.max_itm,
                    underlying_price=spot,
                )
            except Exception as exc:
                log.warning("Opportunity scan: Phase 2 discovery failed for %s: %s", symbol, exc)
                continue
            discoveries.append(result)

        self.last_discovered_contracts = {
            str(contract.security_id): contract
            for discovery in discoveries for contract in discovery.contracts
        }
        self.last_discoveries = tuple(discoveries)

        # Phase 3: one real chain observation plus any recent market quote per
        # discovered contract. No historical quote/depth/IV is synthesized.
        liquidity_inputs = []
        for discovery in discoveries:
            chain = chains.get(discovery.underlying)
            if chain is None:
                chain = OptionChainData(expiry=discovery.expiry.isoformat())
            try:
                liquidity_inputs.extend(self.liquidity_adapter.from_option_chain(
                    discovery.contracts,
                    chain,
                    quote_snapshots=quote_by_id,
                ))
            except Exception as exc:
                log.warning("Opportunity scan: liquidity input failed for %s: %s",
                            discovery.underlying, exc)
        liquidity_report = self.liquidity.score_option_chain(liquidity_inputs, evaluation_time)
        liquidity_by_id = liquidity_report.by_security_id()
        liquidity_qualified_ids = {
            security_id for security_id, result in liquidity_by_id.items() if result.qualified
        }

        # Phase 4: timestamped underlying 15-minute bars/quotes and only the
        # option-chain IV/LTP values actually reported by Dhan. Missing history
        # means no Phase 4 result, so Phase 5 will exclude that stock's options.
        volatility_by_underlying = {}
        for discovery in discoveries:
            symbol = discovery.underlying
            bars = bars_by_symbol.get(symbol, ())
            if not any(_bar_completed(bar, evaluation_time) for bar in bars):
                continue
            chain = chains.get(symbol)
            volatility_input = self.volatility_adapter.build_input(
                symbol,
                bars_15m=bars,
                underlying_quote=underlying_quote_by_symbol.get(symbol),
                option_contracts=discovery.contracts,
                option_chain=chain,
                liquidity_qualified_security_ids=liquidity_qualified_ids,
            )
            try:
                volatility_by_underlying[symbol] = self.volatility.analyze(
                    volatility_input, evaluation_time
                )
            except Exception as exc:
                log.warning("Opportunity scan: Phase 4 volatility failed for %s: %s", symbol, exc)

        self.last_liquidity_report = liquidity_report
        self.last_volatility_by_underlying = dict(volatility_by_underlying)
        return self.ranker.rank(OpportunityScannerInput(
            as_of=evaluation_time,
            discoveries=discoveries,
            liquidity_report=liquidity_report,
            volatility_by_underlying=volatility_by_underlying,
            existing_position_underlyings=tuple(existing_position_underlyings),
            existing_position_security_ids=tuple(existing_position_security_ids),
        ))


def _bar_completed(bar, as_of: datetime) -> bool:
    timestamp = getattr(bar, "ts", getattr(bar, "timestamp", None))
    start = _timestamp_datetime(timestamp)
    return bool(start is not None and start + timedelta(minutes=15) <= as_of)


def _latest_spot(chain: Optional[OptionChainData], quote, as_of: datetime) -> Optional[float]:
    candidates: list[tuple[datetime, float]] = []
    if chain is not None and chain.spot is not None and chain.observed_at is not None:
        observed_at = _timestamp_datetime(chain.observed_at)
        price = _positive(chain.spot)
        if observed_at is not None and observed_at <= as_of and price is not None:
            candidates.append((observed_at, price))
    observation = _quote_values(quote)
    if observation is not None:
        observed_at, price = observation
        if observed_at <= as_of:
            candidates.append((observed_at, price))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _quote_values(quote) -> Optional[tuple[datetime, float]]:
    if quote is None:
        return None
    if isinstance(quote, Mapping):
        price = quote.get("price", quote.get("last_price", quote.get("lastPrice")))
        timestamp = quote.get("ts", quote.get("observed_at"))
    else:
        price = getattr(quote, "price", getattr(quote, "last_price", None))
        timestamp = getattr(quote, "ts", getattr(quote, "observed_at", None))
    observed_at = _timestamp_datetime(timestamp)
    normalized_price = _positive(price)
    if observed_at is None or normalized_price is None:
        return None
    return observed_at, normalized_price


def _timestamp_datetime(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _as_ist_datetime(value)
    if isinstance(value, date):
        return _as_ist_datetime(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            timestamp = float(value)
            if not math.isfinite(timestamp):
                return None
            if timestamp > 1e12:
                timestamp /= 1000.0
            if timestamp <= 0:
                return None
            return from_epoch(int(timestamp))
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            return _as_ist_datetime(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _positive(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _as_ist_datetime(value: datetime | date) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)
