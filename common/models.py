"""Core data models shared across layers."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

ENTRY_SELL = "ENTRY_SELL"
EXIT_BUY = "EXIT_BUY"

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"
STATUS_PENDING = "PENDING"
STATUS_REJECTED = "REJECTED"


@dataclass(frozen=True)
class OptionContract:
    """A single listed option contract (one CE or one PE, one strike, one expiry).

    ``security_id`` is Dhan's instrument token.  ``contract_birth_date`` is
    deliberately contract-specific; it must never be populated from the
    underlying's listing date.  Some masters do not provide that field, so it
    remains ``None`` until an authoritative source (for example, the first
    tradable candle for this option) is available.
    """
    security_id: str          # Dhan security id / instrument token (str form)
    symbol: str               # e.g. "RELIANCE 1500 CE"
    underlying: str           # e.g. "RELIANCE"
    strike: float
    option_type: str          # "CE" | "PE"
    expiry: str               # "YYYY-MM-DD"
    lot_size: int
    instrument: str = "OPTSTK"  # Dhan candle-API instrument: OPTSTK (stock options)
    exchange: str = "NSE"       # exchange name from the contract master
    exchange_segment: str = "NSE_FNO"  # Dhan API segment for NSE F&O contracts
    tick_size: Optional[float] = None
    contract_birth_date: Optional[str] = None  # ISO date, if the source supplies it
    expiry_flag: Optional[str] = None           # master metadata, e.g. M / W
    strike_interval: Optional[float] = None     # optional explicit master metadata

    @property
    def instrument_token(self) -> str:
        """Alias used by APIs that call Dhan's ``security_id`` a token."""
        return self.security_id

    @property
    def name(self) -> str:
        return f"{self.underlying} {self.strike:g} {self.option_type} ({self.expiry})"

    @property
    def uid(self) -> str:
        """Stable unique id used in dedupe keys."""
        return f"{self.security_id}"


@dataclass
class Candle:
    """A completed 15-minute candle for ONE option contract.

    ts = epoch seconds (IST) of the candle START.
    """
    security_id: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    def to_row(self, avwap: Optional[float] = None):
        return {
            "security_id": self.security_id,
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "avwap": avwap,
        }

    @classmethod
    def from_row(cls, row) -> "Candle":
        return cls(
            security_id=row["security_id"],
            ts=int(row["ts"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"] or 0),
        )


@dataclass
class Quote:
    security_id: str
    price: float
    cum_volume: int = 0
    ts: int = 0


@dataclass
class Signal:
    """A strategy signal. action is ENTRY_SELL or EXIT_BUY (the only two actions)."""
    security_id: str
    action: str
    candle_ts: int
    signal_price: float          # close of the completed candle that generated the signal
    avwap: Optional[float]
    prev_close: Optional[float]
    prev_avwap: Optional[float]
    reason: str                  # CROSS_BELOW_AVWAP | CLOSE_ABOVE_AVWAP
    symbol: str
    underlying: str
    strike: float
    option_type: str
    expiry: str
    created_at: int

    @property
    def signal_key(self) -> str:
        # One signal per contract per candle per action => duplicate protection.
        return f"{self.security_id}:{self.candle_ts}:{self.action}"

    def to_row(self):
        d = asdict(self)
        d["signal_key"] = self.signal_key
        return d


@dataclass
class FillResult:
    order_id: str
    status: str                  # COMPLETE | PENDING | REJECTED | PARTIAL
    filled_qty: int = 0
    avg_price: Optional[float] = None
    raw: dict = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status == "COMPLETE" and self.filled_qty > 0
