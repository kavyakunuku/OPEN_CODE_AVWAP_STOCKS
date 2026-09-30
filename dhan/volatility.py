"""Adapter from captured Dhan/project bars and quotes to volatility inputs.

The adapter performs no HTTP requests. Historical use requires the caller to
supply archived bars and observations with their original timestamps.
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping, Optional, Sequence

from common.models import Candle, OptionContract, Quote
from common.utils import from_epoch
from dhan.option_chain import ChainOption, OptionChainData
from market.volatility import (
    OHLCBar,
    ImpliedVolatilityObservation,
    OptionVolatilityInput,
    PriceObservation,
    UnderlyingVolatilityInput,
)


class DhanVolatilityAdapter:
    """Convert existing project candle/quote/option-chain models for Phase 4."""

    def build_input(
        self,
        symbol: str,
        *,
        daily_bars: Sequence = (),
        bars_15m: Sequence = (),
        underlying_quote: Optional[Quote | Mapping] = None,
        underlying_price_history: Sequence[PriceObservation] = (),
        option_contracts: Sequence[OptionContract] = (),
        option_bars_by_security_id: Optional[Mapping[str, Sequence]] = None,
        option_quotes_by_security_id: Optional[Mapping[str, Quote | Mapping]] = None,
        option_price_history_by_security_id: Optional[
            Mapping[str, Sequence[PriceObservation]]
        ] = None,
        option_chain: Optional[OptionChainData] = None,
        implied_volatility_history_by_security_id: Optional[
            Mapping[str, Sequence[ImpliedVolatilityObservation]]
        ] = None,
        liquidity_qualified_security_ids: Iterable[str] = (),
    ) -> UnderlyingVolatilityInput:
        """Build a timestamped input bundle without fabricating missing data.

        Options contribute to the underlying option-movement score only when
        their security IDs are explicitly passed from Phase 3's qualified set.
        Chain spot, IV, and LTP values inherit the chain's actual capture
        timestamp; an un-timestamped chain contributes none of those values.
        """
        option_bars = option_bars_by_security_id or {}
        option_quotes = option_quotes_by_security_id or {}
        price_history = option_price_history_by_security_id or {}
        iv_history = implied_volatility_history_by_security_id or {}
        qualified_ids = {str(security_id) for security_id in liquidity_qualified_security_ids}

        underlying_prices = list(underlying_price_history)
        if (
            option_chain is not None
            and option_chain.observed_at is not None
            and option_chain.spot is not None
        ):
            underlying_prices.append(
                PriceObservation(option_chain.observed_at, option_chain.spot)
            )
        quote_observation = _price_observation(underlying_quote)
        if quote_observation is not None:
            underlying_prices.append(quote_observation)

        options = []
        for contract in option_contracts:
            security_id = str(contract.security_id)
            option = _find_chain_option(option_chain, contract) if option_chain else None
            prices = list(price_history.get(security_id, ()))
            quote = _price_observation(option_quotes.get(security_id))
            if quote is not None:
                prices.append(quote)
            iv_values = list(iv_history.get(security_id, ()))
            chain_at = option_chain.observed_at if option_chain is not None else None
            if option is not None and chain_at is not None:
                if option.ltp is not None:
                    prices.append(PriceObservation(chain_at, option.ltp))
                if option.iv is not None:
                    iv_values.append(ImpliedVolatilityObservation(chain_at, option.iv))
            options.append(OptionVolatilityInput(
                contract=contract,
                bars_15m=tuple(_ohlc_bar(bar) for bar in option_bars.get(security_id, ())),
                price_observations=tuple(prices),
                implied_volatility_observations=tuple(iv_values),
                liquidity_qualified=security_id in qualified_ids,
            ))

        return UnderlyingVolatilityInput(
            symbol=str(symbol),
            daily_bars=tuple(_ohlc_bar(bar) for bar in daily_bars),
            bars_15m=tuple(_ohlc_bar(bar) for bar in bars_15m),
            price_observations=tuple(underlying_prices),
            options=tuple(options),
        )


def _ohlc_bar(bar) -> OHLCBar:
    if isinstance(bar, OHLCBar):
        return bar
    if isinstance(bar, Mapping):
        timestamp = bar.get("ts", bar.get("timestamp", bar.get("start")))
        return OHLCBar(
            ts=timestamp,
            open=bar.get("open"),
            high=bar.get("high"),
            low=bar.get("low"),
            close=bar.get("close"),
            volume=bar.get("volume"),
        )
    return OHLCBar(
        ts=getattr(bar, "ts", getattr(bar, "timestamp", None)),
        open=getattr(bar, "open", None),
        high=getattr(bar, "high", None),
        low=getattr(bar, "low", None),
        close=getattr(bar, "close", None),
        volume=getattr(bar, "volume", None),
    )


def _price_observation(quote) -> Optional[PriceObservation]:
    if quote is None:
        return None
    if isinstance(quote, Mapping):
        price = quote.get("price", quote.get("last_price", quote.get("lastPrice")))
        timestamp = quote.get("observed_at", quote.get("ts"))
    else:
        price = getattr(quote, "price", getattr(quote, "last_price", None))
        timestamp = getattr(quote, "observed_at", getattr(quote, "ts", None))
    if timestamp is None or price is None:
        return None
    if isinstance(timestamp, (int, float)):
        if not math.isfinite(float(timestamp)) or float(timestamp) <= 0:
            return None
        if float(timestamp) > 1e12:
            timestamp = float(timestamp) / 1000.0
        timestamp = from_epoch(int(timestamp))
    return PriceObservation(observed_at=timestamp, price=price)


def _find_chain_option(
    chain: OptionChainData,
    contract: OptionContract,
) -> Optional[ChainOption]:
    if chain.expiry and str(chain.expiry) != str(contract.expiry):
        return None
    option_type = str(contract.option_type).strip().upper()
    if option_type in {"CE", "CALL"}:
        side = chain.ce
    elif option_type in {"PE", "PUT"}:
        side = chain.pe
    else:
        return None
    strike = float(contract.strike)
    candidate = side.get(strike)
    if candidate is None:
        candidate = next((value for key, value in side.items()
                          if math.isclose(float(key), strike, rel_tol=0, abs_tol=1e-8)), None)
    if candidate is None or str(candidate.security_id) != str(contract.security_id):
        return None
    return candidate
