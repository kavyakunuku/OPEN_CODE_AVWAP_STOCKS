"""Anchored VWAP (AVWAP) for a single option contract.

Per the strategy spec:
  * Every option contract has its OWN AVWAP state - states are never shared.
  * The anchor is the FIRST tradable 15-minute candle of that contract.
  * AVWAP is CUMULATIVE across trading days and is never reset daily.
  * AVWAP_t = sum(TP_i * V_i from anchor..t) / sum(V_i from anchor..t)
    where TP_i = (High_i + Low_i + Close_i) / 3.

State is persisted in SQLite (avwap_state) so it survives restarts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from common.models import Candle

log = logging.getLogger("avwap.strategy.avwap")


@dataclass
class AvwapState:
    security_id: str
    anchor_ts: Optional[int] = None
    cumulative_price_volume: float = 0.0
    cumulative_volume: float = 0.0
    last_candle_ts: Optional[int] = None
    last_close: Optional[float] = None
    last_avwap: Optional[float] = None
    symbol: Optional[str] = None

    @property
    def avwap(self) -> Optional[float]:
        return self.last_avwap

    def update(self, candle: Candle) -> Optional[float]:
        """Fold one completed candle into the cumulative state.

        Returns the AVWAP after this candle (None if cumulative volume is 0).
        Idempotent: re-feeding the same or an older candle is ignored.
        """
        if self.last_candle_ts is not None and candle.ts <= self.last_candle_ts:
            return self.last_avwap  # duplicate / out-of-order: no state change

        if self.anchor_ts is None:
            # First tradable candle of this contract = the anchor.
            self.anchor_ts = candle.ts

        tp = (candle.high + candle.low + candle.close) / 3.0
        self.cumulative_price_volume += tp * candle.volume
        self.cumulative_volume += candle.volume
        self.last_candle_ts = candle.ts
        self.last_close = candle.close
        self.last_avwap = (
            self.cumulative_price_volume / self.cumulative_volume
            if self.cumulative_volume > 0
            else None
        )
        return self.last_avwap

    def to_dict(self) -> dict:
        return {
            "security_id": self.security_id,
            "anchor_ts": self.anchor_ts,
            "cumulative_price_volume": self.cumulative_price_volume,
            "cumulative_volume": self.cumulative_volume,
            "last_candle_ts": self.last_candle_ts,
            "last_close": self.last_close,
            "last_avwap": self.last_avwap,
            "symbol": self.symbol,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AvwapState":
        return cls(
            security_id=d["security_id"],
            anchor_ts=d.get("anchor_ts"),
            cumulative_price_volume=float(d.get("cumulative_price_volume") or 0.0),
            cumulative_volume=float(d.get("cumulative_volume") or 0.0),
            last_candle_ts=d.get("last_candle_ts"),
            last_close=d.get("last_close"),
            last_avwap=d.get("last_avwap"),
            symbol=d.get("symbol"),
        )

    @classmethod
    def from_row(cls, row) -> "AvwapState":
        return cls(
            security_id=row["security_id"],
            anchor_ts=row["anchor_ts"],
            cumulative_price_volume=float(row["cumulative_price_volume"] or 0.0),
            cumulative_volume=float(row["cumulative_volume"] or 0.0),
            last_candle_ts=row["last_candle_ts"],
            last_close=row["last_close"],
            last_avwap=row["last_avwap"],
            symbol=row["symbol"] if "symbol" in row.keys() else None,
        )


class AvwapStore:
    """Loads / saves AvwapState per contract.

    AVWAP MUST NOT RESET DAILY: state is cumulative for the lifetime of the
    contract. On application restart we simply reload the persisted state.
    """

    def __init__(self, db):
        self.db = db

    def get(self, security_id: str) -> AvwapState:
        row = self.db.get_avwap_state(security_id)
        if row is not None:
            return AvwapState.from_row(row)
        return AvwapState(security_id=security_id)

    def save(self, state: AvwapState) -> None:
        self.db.save_avwap_state(state.to_dict())

    def delete(self, security_id: str) -> None:
        """Drop a state entirely (used before a full RE-ANCHOR rebuild - a
        late-anchored state cannot be fixed by merging older candles in,
        because update() ignores candles older than its last candle)."""
        self.db._exec("DELETE FROM avwap_state WHERE security_id=?", (security_id,))

    def initialize_from_candles(self, security_id: str, candles: list[Candle],
                                symbol: Optional[str] = None) -> AvwapState:
        """LIVE AVWAP INITIALIZATION (spec §15).

        Feed historical 15-min candles (oldest -> newest) into a fresh or
        existing state so the AVWAP is anchored at the contract's first
        tradable candle instead of at application start.

        If a state already exists, only candles NEWER than its last candle
        are accumulated (merge, never double count).
        """
        state = self.get(security_id)
        if symbol and not state.symbol:
            state.symbol = symbol
        candles = sorted(candles, key=lambda c: c.ts)
        added = 0
        for c in candles:
            before = state.last_candle_ts
            state.update(c)
            if state.last_candle_ts != before:
                added += 1
        self.save(state)
        if added:
            log.info(
                "AVWAP init %s: %d candles applied (anchor=%s, last_candle=%s, cum_v=%.0f, avwap=%s)",
                security_id, added, state.anchor_ts, state.last_candle_ts,
                state.cumulative_volume,
                f"{state.last_avwap:.4f}" if state.last_avwap is not None else "n/a",
            )
        return state
