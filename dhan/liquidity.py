"""Adapters from Dhan/project market-data models into liquidity inputs.

Dhan's option-chain endpoint is a live snapshot. This adapter timestamps that
snapshot using its captured ``observed_at`` value and never treats it as past
data. Historical scoring is available only when the caller supplies archived,
timestamped observations and candles that were captured at the time.
"""
from __future__ import annotations

from datetime import datetime
import math
from typing import Mapping, Optional, Sequence

from common.models import Candle, OptionContract, Quote
from common.utils import from_epoch
from dhan.option_chain import ChainOption, OptionChainData
from market.liquidity import (
    ContractLiquidityData,
    DepthLevel,
    LiquidityObservation,
)


class DhanLiquidityAdapter:
    """Translate option-chain, quote and candle adapter results for the scorer."""

    def __init__(self, market_data=None):
        self.market_data = market_data

    def from_option_chain(
        self,
        contracts: Sequence[OptionContract],
        chain: OptionChainData,
        *,
        quote_snapshots: Optional[Mapping[str, Quote | Mapping]] = None,
        candles_by_security_id: Optional[Mapping[str, Sequence[Candle]]] = None,
        observation_history_by_security_id: Optional[
            Mapping[str, Sequence[LiquidityObservation]]
        ] = None,
        chain_observed_at: Optional[datetime] = None,
    ) -> list[ContractLiquidityData]:
        """Build scorer inputs without making any network requests.

        ``chain_observed_at`` is an explicit fallback only for a caller that
        captured a raw chain outside ``parse_chain_response``. It is never
        inferred from an evaluation timestamp. ``quote_snapshots`` should come
        from ``DhanMarketData.quotes`` or an equivalent timestamped project
        adapter.
        """
        quotes = quote_snapshots or {}
        candles = candles_by_security_id or {}
        history = observation_history_by_security_id or {}
        chain_time = chain.observed_at or chain_observed_at
        out: list[ContractLiquidityData] = []
        for contract in contracts:
            option = _find_chain_option(chain, contract)
            observations = list(history.get(str(contract.security_id), ()))
            if option is not None and chain_time is not None:
                observations.append(_chain_observation(option, chain_time))
            quote = quotes.get(str(contract.security_id))
            quote_observation = _quote_observation(quote)
            if quote_observation is not None:
                observations.append(quote_observation)
            out.append(ContractLiquidityData(
                contract=contract,
                observations=tuple(observations),
                candles=tuple(candles.get(str(contract.security_id), ())),
            ))
        return out

    def capture_current(
        self,
        contracts: Sequence[OptionContract],
        chain: OptionChainData,
        *,
        candles_by_security_id: Optional[Mapping[str, Sequence[Candle]]] = None,
        observation_history_by_security_id: Optional[
            Mapping[str, Sequence[LiquidityObservation]]
        ] = None,
    ) -> list[ContractLiquidityData]:
        """Fetch current Dhan quote snapshots and combine them with a chain.

        This is a live capture helper; it must not be called to reconstruct a
        historical timestamp. The chain endpoint remains caller-managed because
        it requires the underlying ID/expiry and is paced by Dhan's API limit.
        """
        if self.market_data is None:
            raise ValueError("capture_current requires a market_data adapter")
        quotes = self.market_data.quotes([str(contract.security_id) for contract in contracts])
        return self.from_option_chain(
            contracts,
            chain,
            quote_snapshots=quotes,
            candles_by_security_id=candles_by_security_id,
            observation_history_by_security_id=observation_history_by_security_id,
        )


def _find_chain_option(chain: OptionChainData, contract: OptionContract) -> Optional[ChainOption]:
    if chain.expiry and str(chain.expiry) != str(contract.expiry):
        return None
    option_type = str(contract.option_type).strip().upper()
    if option_type in ("CE", "CALL"):
        side = chain.ce
    elif option_type in ("PE", "PUT"):
        side = chain.pe
    else:
        return None
    strike = float(contract.strike)
    option = side.get(strike)
    if option is None:
        option = next((candidate for candidate_strike, candidate in side.items()
                       if math.isclose(float(candidate_strike), strike,
                                       rel_tol=0.0, abs_tol=1e-8)), None)
    if option is None or str(option.security_id) != str(contract.security_id):
        return None
    return option


def _chain_observation(option: ChainOption, observed_at: datetime) -> LiquidityObservation:
    oi_available = option.oi_available is not False
    volume_available = option.volume_available is not False
    return LiquidityObservation(
        observed_at=observed_at,
        last_price=option.ltp,
        volume=float(option.volume) if volume_available and option.volume is not None else None,
        open_interest=float(option.oi) if oi_available and option.oi is not None else None,
        oi_change=float(option.oi_change) if option.oi_change is not None else None,
        bid=option.bid,
        ask=option.ask,
        bid_quantity=_as_float(option.bid_quantity),
        ask_quantity=_as_float(option.ask_quantity),
        bid_depth=tuple(_depth_level(level) for level in (option.bid_depth or ())),
        ask_depth=tuple(_depth_level(level) for level in (option.ask_depth or ())),
    )


def _quote_observation(quote) -> Optional[LiquidityObservation]:
    if quote is None:
        return None
    if isinstance(quote, Mapping):
        price = quote.get("price", quote.get("last_price"))
        volume = quote.get("cum_volume", quote.get("volume"))
        open_interest = quote.get("open_interest", quote.get("oi"))
        bid = quote.get("bid")
        ask = quote.get("ask")
        bid_quantity = quote.get("bid_quantity")
        ask_quantity = quote.get("ask_quantity")
        bid_depth = quote.get("bid_depth", ())
        ask_depth = quote.get("ask_depth", ())
        timestamp = quote.get("ts", quote.get("observed_at"))
    else:
        price = getattr(quote, "price", getattr(quote, "last_price", None))
        volume = getattr(quote, "cum_volume", getattr(quote, "volume", None))
        open_interest = getattr(quote, "open_interest", getattr(quote, "oi", None))
        bid = getattr(quote, "bid", None)
        ask = getattr(quote, "ask", None)
        bid_quantity = getattr(quote, "bid_quantity", None)
        ask_quantity = getattr(quote, "ask_quantity", None)
        bid_depth = getattr(quote, "bid_depth", ())
        ask_depth = getattr(quote, "ask_depth", ())
        timestamp = getattr(quote, "ts", getattr(quote, "observed_at", None))
    if isinstance(timestamp, datetime):
        observed_at = timestamp
    else:
        try:
            timestamp = int(timestamp)
            if timestamp <= 0:
                return None
            observed_at = from_epoch(timestamp)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return LiquidityObservation(
        observed_at=observed_at,
        last_price=_as_float(price),
        volume=_as_float(volume),
        open_interest=_as_float(open_interest),
        bid=_as_float(bid),
        ask=_as_float(ask),
        bid_quantity=_as_float(bid_quantity),
        ask_quantity=_as_float(ask_quantity),
        bid_depth=tuple(_depth_level(level) for level in (bid_depth or ())),
        ask_depth=tuple(_depth_level(level) for level in (ask_depth or ())),
    )


def _depth_level(level) -> DepthLevel:
    if isinstance(level, Mapping):
        return DepthLevel(
            price=_as_float(level.get("price")),
            quantity=_as_float(level.get("quantity", level.get("qty"))),
        )
    return DepthLevel(
        price=_as_float(getattr(level, "price", None)),
        quantity=_as_float(getattr(level, "quantity", None)),
    )


def _as_float(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None
