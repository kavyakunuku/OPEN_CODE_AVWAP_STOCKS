"""Canonical historical data contract for Phase 7 backtests.

All market observations keep their original capture times. The loader never
backdates end-of-day values, infers quotes, or repairs option-history anchors.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from functools import cached_property
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from common.models import Candle, OptionContract
from common.utils import IST, candle_start_for, epoch, from_epoch
from dhan.option_chain import ChainOption, OptionChainData
from market.contract_discovery import ContractCatalogSnapshot, OptionContractCatalog

REQUIRED_FILES = (
    "contract_master.csv",
    "scan_events.csv",
    "option_candles.csv",
    "underlying_candles.csv",
    "market_snapshots.csv",
)


class BacktestDataError(ValueError):
    """Raised when required historical data is absent or structurally unsafe."""


@dataclass(frozen=True)
class ScanEvent:
    scan_id: str
    as_of: datetime


@dataclass(frozen=True)
class MarketSnapshot:
    """One actual point-in-time chain/quote observation.

    ``source`` is ``chain``, ``option_quote`` or ``underlying_quote``. A chain
    observation represents one contract row from a captured option chain;
    rows sharing (observed_at, underlying, expiry) form one chain snapshot.
    """

    source: str
    observed_at: datetime
    symbol: str = ""
    underlying: str = ""
    expiry: str = ""
    security_id: str = ""
    strike: Optional[float] = None
    option_type: str = ""
    spot: Optional[float] = None
    last_price: Optional[float] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    oi_change: Optional[int] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_quantity: Optional[int] = None
    ask_quantity: Optional[int] = None
    bid_depth: tuple[dict, ...] = ()
    ask_depth: tuple[dict, ...] = ()
    implied_volatility: Optional[float] = None

    def as_chain_option(self) -> ChainOption:
        if self.source != "chain" or self.strike is None:
            raise ValueError("only chain rows can be converted to ChainOption")
        return ChainOption(
            strike=float(self.strike),
            option_type=self.option_type,
            security_id=self.security_id,
            ltp=self.last_price,
            oi=self.open_interest or 0,
            volume=self.volume or 0,
            bid=self.bid,
            ask=self.ask,
            iv=self.implied_volatility,
            oi_available=self.open_interest is not None,
            volume_available=self.volume is not None,
            oi_change=self.oi_change,
            bid_quantity=self.bid_quantity,
            ask_quantity=self.ask_quantity,
            bid_depth=list(self.bid_depth),
            ask_depth=list(self.ask_depth),
        )

    def to_quote(self):
        """Convert a captured option/equity quote to the project Quote model."""
        from common.models import Quote

        if self.source not in {"option_quote", "underlying_quote"}:
            raise ValueError("only quote rows can be converted to Quote")
        if self.last_price is None:
            return None
        return Quote(
            security_id=self.security_id,
            price=float(self.last_price),
            cum_volume=self.volume,
            ts=epoch(self.observed_at),
            bid=self.bid,
            ask=self.ask,
            bid_quantity=self.bid_quantity,
            ask_quantity=self.ask_quantity,
            open_interest=self.open_interest,
            bid_depth=list(self.bid_depth),
            ask_depth=list(self.ask_depth),
        )


@dataclass
class DataQuality:
    files: dict[str, dict] = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    dropped_future_snapshots: int = 0
    unverified_option_anchors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "status": "ready_with_warnings" if self.warnings and self.ready else
                      "ready" if self.ready else "blocked",
            "files": self.files,
            "warnings": self.warnings,
            "errors": self.errors,
            "dropped_future_snapshots": self.dropped_future_snapshots,
            "unverified_option_anchors": sorted(set(self.unverified_option_anchors)),
        }


@dataclass
class Phase7Dataset:
    root: Path
    manifest: dict
    catalog: OptionContractCatalog
    symbols: tuple[str, ...]
    scan_events: list[ScanEvent]
    option_candles: dict[str, list[Candle]]
    underlying_candles: dict[str, list[Candle]]
    snapshots: list[MarketSnapshot]
    anchor_confirmed: set[str]
    quality: DataQuality

    @property
    def start_at(self) -> Optional[datetime]:
        if self.manifest.get("period_start"):
            return _datetime(self.manifest["period_start"], "manifest.period_start")
        return min((event.as_of for event in self.scan_events), default=None)

    @property
    def end_at(self) -> Optional[datetime]:
        if self.manifest.get("period_end"):
            return _datetime(self.manifest["period_end"], "manifest.period_end")
        return max((event.as_of for event in self.scan_events), default=None)

    def contract_at(self, security_id: str, as_of: datetime) -> Optional[OptionContract]:
        """Return contract metadata from the latest master snapshot at or before T."""
        snapshot = self.catalog.snapshot_for(as_of)
        return next((contract for contract in snapshot.contracts
                     if str(contract.security_id) == str(security_id)), None)

    @cached_property
    def snapshots_by_security_id(self) -> dict[str, list[MarketSnapshot]]:
        out: dict[str, list[MarketSnapshot]] = {}
        for row in self.snapshots:
            out.setdefault(row.security_id, []).append(row)
        return out

    @cached_property
    def chain_snapshots_by_key(self) -> dict[tuple[str, str], list[MarketSnapshot]]:
        out: dict[tuple[str, str], list[MarketSnapshot]] = {}
        for row in self.snapshots:
            if row.source == "chain":
                out.setdefault((row.underlying.upper(), str(row.expiry)), []).append(row)
        return out

    @cached_property
    def snapshots_by_underlying(self) -> dict[str, list[MarketSnapshot]]:
        out: dict[str, list[MarketSnapshot]] = {}
        for row in self.snapshots:
            if row.source == "chain":
                out.setdefault(row.underlying.upper(), []).append(row)
        return out

    @cached_property
    def snapshots_by_symbol(self) -> dict[str, list[MarketSnapshot]]:
        out: dict[str, list[MarketSnapshot]] = {}
        for row in self.snapshots:
            if row.source == "underlying_quote":
                out.setdefault(row.symbol.upper(), []).append(row)
        return out

    def spot_snapshots_for_symbol(self, symbol: str) -> tuple[MarketSnapshot, ...]:
        """Return archived chain/underlying quote rows for one stock, already indexed."""
        normalized = symbol.strip().upper()
        return tuple(self.snapshots_by_underlying.get(normalized, ())) + tuple(
            self.snapshots_by_symbol.get(normalized, ())
        )

    def chain_at(
        self,
        symbol: str,
        expiry: str,
        as_of: datetime,
        max_age_seconds: float,
    ) -> Optional[OptionChainData]:
        """Return the latest complete captured chain no later than ``as_of``."""
        eligible = [
            row for row in self.chain_snapshots_by_key.get(
                (symbol.strip().upper(), str(expiry)), ()
            )
            if row.observed_at <= as_of
            and (as_of - row.observed_at).total_seconds() <= max_age_seconds
        ]
        if not eligible:
            return None
        observed_at = max(row.observed_at for row in eligible)
        rows = [row for row in eligible if row.observed_at == observed_at]
        spot_values = {row.spot for row in rows if row.spot is not None}
        if len(spot_values) > 1:
            raise BacktestDataError(
                f"chain rows disagree on spot for {symbol} {expiry} at {observed_at.isoformat()}"
            )
        chain = OptionChainData(
            spot=next(iter(spot_values), None),
            expiry=str(expiry),
            observed_at=observed_at,
        )
        for row in sorted(rows, key=lambda item: (item.strike or 0, item.option_type, item.security_id)):
            option = row.as_chain_option()
            output = chain.ce if row.option_type.upper() == "CE" else chain.pe
            if option.strike in output:
                raise BacktestDataError(
                    f"duplicate strike/side in chain snapshot: {symbol} {expiry} "
                    f"{option.strike} {option.option_type}"
                )
            output[option.strike] = option
        return chain

    def latest_snapshot(
        self,
        *,
        source: str,
        as_of: datetime,
        security_id: Optional[str] = None,
        symbol: Optional[str] = None,
        max_age_seconds: Optional[float] = None,
    ) -> Optional[MarketSnapshot]:
        """Pick one latest observation using strict at-or-before filtering."""
        if security_id is not None:
            pool = self.snapshots_by_security_id.get(str(security_id), ())
        elif symbol is not None and source == "underlying_quote":
            pool = self.snapshots_by_symbol.get(symbol.strip().upper(), ())
        elif source == "chain" and symbol is not None:
            pool = self.snapshots_by_underlying.get(symbol.strip().upper(), ())
        else:
            pool = self.snapshots
        rows = [
            row for row in pool
            if row.source == source and row.observed_at <= as_of
            and (symbol is None or row.symbol.strip().upper() == symbol.strip().upper()
                 or (source == "chain" and row.underlying.strip().upper() == symbol.strip().upper()))
            and (max_age_seconds is None or
                 (as_of - row.observed_at).total_seconds() <= max_age_seconds)
        ]
        return max(rows, key=lambda row: (row.observed_at, row.security_id)) if rows else None

    def option_quote_history(
        self, security_id: str, as_of: datetime
    ) -> list[MarketSnapshot]:
        return sorted(
            (row for row in self.snapshots_by_security_id.get(str(security_id), ())
             if row.source == "option_quote" and row.observed_at <= as_of),
            key=lambda row: row.observed_at,
        )

    def underlying_quote_history(self, symbol: str, as_of: datetime) -> list[MarketSnapshot]:
        return sorted(
            (row for row in self.snapshots_by_symbol.get(symbol.strip().upper(), ())
             if row.observed_at <= as_of),
            key=lambda row: row.observed_at,
        )

    def option_bars_at(self, security_id: str, as_of: datetime) -> list[Candle]:
        return [
            bar for bar in self.option_candles.get(str(security_id), ())
            if from_epoch(bar.ts) + timedelta(minutes=15) <= as_of
        ]

    def underlying_bars_at(self, symbol: str, as_of: datetime) -> list[Candle]:
        return [
            bar for bar in self.underlying_candles.get(symbol.strip().upper(), ())
            if from_epoch(bar.ts) + timedelta(minutes=15) <= as_of
        ]


def _datetime(value: str, where: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise BacktestDataError(f"{where}: timestamp is required")
    if text.replace(".", "", 1).isdigit():
        number = float(text)
        if not math.isfinite(number) or number <= 0:
            raise BacktestDataError(f"{where}: invalid epoch timestamp {text!r}")
        if number > 1e12:
            number /= 1000.0
        return from_epoch(int(number))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BacktestDataError(f"{where}: invalid ISO-8601 timestamp {text!r}") from exc
    if parsed.tzinfo is None:
        raise BacktestDataError(
            f"{where}: timestamp {text!r} has no timezone; use an explicit UTC offset"
        )
    return parsed.astimezone(IST)


def _optional_float(row: dict, key: str, where: str) -> Optional[float]:
    value = str(row.get(key, "")).strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise BacktestDataError(f"{where}: {key} must be numeric, got {value!r}") from exc
    if not math.isfinite(number):
        raise BacktestDataError(f"{where}: {key} must be finite")
    return number


def _optional_int(row: dict, key: str, where: str) -> Optional[int]:
    number = _optional_float(row, key, where)
    if number is None:
        return None
    if not number.is_integer():
        raise BacktestDataError(f"{where}: {key} must be an integer")
    return int(number)


def _json_depth(row: dict, key: str, where: str) -> tuple[dict, ...]:
    text = str(row.get(key, "")).strip()
    if not text:
        return ()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BacktestDataError(f"{where}: {key} must be JSON array") from exc
    if not isinstance(value, list):
        raise BacktestDataError(f"{where}: {key} must be a JSON array")
    out = []
    for index, level in enumerate(value):
        if not isinstance(level, dict):
            raise BacktestDataError(f"{where}: {key}[{index}] must be an object")
        price = level.get("price")
        quantity = level.get("quantity", level.get("qty"))
        if price in (None, "") or quantity in (None, ""):
            continue
        try:
            price_f, quantity_f = float(price), float(quantity)
        except (TypeError, ValueError) as exc:
            raise BacktestDataError(f"{where}: {key}[{index}] price/quantity must be numeric") from exc
        if not math.isfinite(price_f) or not math.isfinite(quantity_f):
            raise BacktestDataError(f"{where}: non-finite depth level")
        if price_f <= 0 or quantity_f < 0:
            # Dhan pads empty levels with (0, 0); ignore only that sentinel.
            if price_f == 0 and quantity_f == 0:
                continue
            raise BacktestDataError(f"{where}: invalid depth level {level!r}")
        out.append({"price": price_f, "quantity": quantity_f})
    return tuple(out)


def _bool(value: str, where: str) -> bool:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no", ""}:
        return False
    raise BacktestDataError(f"{where}: expected true/false, got {value!r}")


def _read_csv(path: Path) -> list[dict]:
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as stream:
            return list(csv.DictReader(stream))
    except OSError as exc:
        raise BacktestDataError(f"cannot read {path}: {exc}") from exc


def _candle_gap_summary(candles_by_instrument: dict[str, list[Candle]]) -> dict:
    """Count absent 15-minute starts only between observed same-day bars.

    These are coverage diagnostics, not claims that a contract necessarily
    traded in each absent interval. No bars are inserted or imputed.
    """
    sessions: dict[tuple[str, date], set[int]] = {}
    for instrument, rows in candles_by_instrument.items():
        for candle in rows:
            day = from_epoch(candle.ts).date()
            sessions.setdefault((instrument, day), set()).add(int(candle.ts))

    sessions_with_gaps = set()
    instruments_with_gaps = set()
    total_missing = 0
    max_consecutive = 0
    samples = []
    step = 15 * 60
    for (instrument, day), starts in sorted(sessions.items(), key=lambda item: (item[0][1], item[0][0])):
        timestamps = sorted(starts)
        for previous, current in zip(timestamps, timestamps[1:]):
            delta = current - previous
            missing = max(0, delta // step - 1)
            if missing <= 0:
                continue
            sessions_with_gaps.add((instrument, day))
            instruments_with_gaps.add(instrument)
            total_missing += missing
            max_consecutive = max(max_consecutive, missing)
            if len(samples) < 20:
                samples.append({
                    "instrument": instrument,
                    "session_date": day.isoformat(),
                    "after_candle_start": from_epoch(previous).isoformat(),
                    "before_candle_start": from_epoch(current).isoformat(),
                    "missing_grid_intervals": missing,
                })

    return {
        "sessions_observed": len(sessions),
        "sessions_with_internal_gaps": len(sessions_with_gaps),
        "instruments_with_internal_gaps": len(instruments_with_gaps),
        "missing_grid_intervals_within_observed_range": total_missing,
        "max_consecutive_missing_grid_intervals": max_consecutive,
        "sample_gaps": samples,
        "scope_note": (
            "Counts only 15-minute starts between first and last observed bars on a session date. "
            "It does not infer leading/trailing coverage or whether an absent interval had no trades "
            "versus a capture gap, and it never inserts candles."
        ),
    }


def inspect_data(root: str | Path) -> dict:
    """Return a non-fabricated readiness report, including missing inputs."""
    base = Path(root)
    missing = []
    manifest = base / "manifest.json"
    if not manifest.is_file():
        missing.append("manifest.json")
    for name in REQUIRED_FILES:
        if not (base / name).is_file():
            missing.append(name)
    return {
        "status": "blocked_missing_historical_data" if missing else "files_present_unvalidated",
        "data_root": str(base),
        "missing_files": missing,
        "empirical_results": None,
        "note": (
            "No performance metrics are calculated until the point-in-time archive "
            "passes schema and data-quality validation."
        ),
    }


def load_dataset(root: str | Path) -> Phase7Dataset:
    """Load and validate a Phase 7 CSV archive from a directory."""
    base = Path(root)
    readiness = inspect_data(base)
    if readiness["missing_files"]:
        raise BacktestDataError(
            "historical archive is incomplete; missing " + ", ".join(readiness["missing_files"])
        )
    try:
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestDataError(f"invalid manifest.json: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise BacktestDataError("manifest schema_version must be 1")
    if manifest.get("interval_minutes") != 15:
        raise BacktestDataError("Phase 7 input must use interval_minutes=15")
    if manifest.get("timezone") not in (None, "Asia/Kolkata", "IST"):
        raise BacktestDataError("manifest.timezone must be Asia/Kolkata or IST")
    manifest_start = _datetime(manifest["period_start"], "manifest.period_start") if manifest.get("period_start") else None
    manifest_end = _datetime(manifest["period_end"], "manifest.period_end") if manifest.get("period_end") else None
    if manifest_start and manifest_end and manifest_end < manifest_start:
        raise BacktestDataError("manifest.period_end must not be before period_start")
    symbols = tuple(dict.fromkeys(
        str(symbol).strip().upper() for symbol in manifest.get("symbols", [])
        if str(symbol).strip()
    ))
    if not symbols:
        raise BacktestDataError("manifest must list the validated Phase 1 symbols")

    quality = DataQuality()
    master_rows = _read_csv(base / "contract_master.csv")
    quality.files["contract_master.csv"] = {"rows": len(master_rows)}
    masters: dict[datetime, list[OptionContract]] = {}
    master_security_keys = set()
    master_contract_keys = set()
    for line, row in enumerate(master_rows, 2):
        where = f"contract_master.csv:{line}"
        captured_at = _datetime(row.get("captured_at"), where)
        try:
            contract = OptionContract(
                security_id=str(row.get("security_id", "")).strip(),
                symbol=str(row.get("symbol", "")).strip(),
                underlying=str(row.get("underlying", "")).strip().upper(),
                strike=float(row.get("strike", "")),
                option_type=str(row.get("option_type", "")).strip().upper(),
                expiry=str(row.get("expiry", "")).strip()[:10],
                lot_size=int(row.get("lot_size", "")),
                instrument=str(row.get("instrument", "OPTSTK") or "OPTSTK").strip(),
                exchange=str(row.get("exchange", "NSE") or "NSE").strip(),
                exchange_segment=str(row.get("exchange_segment", "NSE_FNO") or "NSE_FNO").strip(),
                tick_size=_optional_float(row, "tick_size", where),
                contract_birth_date=str(row.get("contract_birth_date", "")).strip() or None,
                expiry_flag=str(row.get("expiry_flag", "")).strip() or None,
                strike_interval=_optional_float(row, "strike_interval", where),
            )
        except (TypeError, ValueError) as exc:
            raise BacktestDataError(f"{where}: invalid contract metadata: {exc}") from exc
        if not contract.security_id or not contract.underlying or contract.option_type not in {"CE", "PE"}:
            raise BacktestDataError(f"{where}: security_id/underlying/CE-PE metadata is required")
        try:
            expiry_day = date.fromisoformat(contract.expiry)
        except ValueError as exc:
            raise BacktestDataError(f"{where}: expiry must be an ISO date") from exc
        if (not math.isfinite(contract.strike) or contract.strike <= 0
                or contract.lot_size <= 0 or len(contract.expiry) != 10):
            raise BacktestDataError(f"{where}: invalid strike/lot_size/expiry")
        if contract.underlying not in symbols:
            continue
        sid_key = (captured_at, contract.security_id)
        contract_key = (
            captured_at, contract.underlying, expiry_day.isoformat(),
            round(contract.strike, 8), contract.option_type,
        )
        if sid_key in master_security_keys or contract_key in master_contract_keys:
            raise BacktestDataError(f"{where}: duplicate contract in one captured master snapshot")
        master_security_keys.add(sid_key)
        master_contract_keys.add(contract_key)
        masters.setdefault(captured_at, []).append(contract)
    if not masters:
        raise BacktestDataError("contract_master.csv has no contracts for manifest.symbols")
    catalog = OptionContractCatalog()
    for captured_at, contracts in sorted(masters.items()):
        catalog.add_snapshot(captured_at, contracts, source="Phase 7 archived contract master")

    scan_rows = _read_csv(base / "scan_events.csv")
    quality.files["scan_events.csv"] = {"rows": len(scan_rows)}
    scans = []
    seen_scan_ids = set()
    for line, row in enumerate(scan_rows, 2):
        where = f"scan_events.csv:{line}"
        scan_id = str(row.get("scan_id", "")).strip()
        if not scan_id or scan_id in seen_scan_ids:
            raise BacktestDataError(f"{where}: scan_id is empty or duplicated")
        seen_scan_ids.add(scan_id)
        scans.append(ScanEvent(scan_id, _datetime(row.get("as_of"), where)))
    scans.sort(key=lambda event: (event.as_of, event.scan_id))
    if not scans:
        raise BacktestDataError("scan_events.csv has no archived selection timestamps")
    if len({event.as_of for event in scans}) != len(scans):
        raise BacktestDataError("scan_events.csv must contain one row per scanner replay timestamp")
    quality.files["scan_events.csv"]["unique_timestamps"] = len({e.as_of for e in scans})

    option_rows = _read_csv(base / "option_candles.csv")
    quality.files["option_candles.csv"] = {"rows": len(option_rows)}
    option_candles: dict[str, list[Candle]] = {}
    option_keys = set()
    anchor_flags: dict[str, list[tuple[int, bool]]] = {}
    for line, row in enumerate(option_rows, 2):
        where = f"option_candles.csv:{line}"
        sid = str(row.get("security_id", "")).strip()
        if not sid:
            raise BacktestDataError(f"{where}: security_id is required")
        start = _datetime(row.get("ts"), where)
        grid = candle_start_for(start, 15)
        if grid is None or grid != start:
            raise BacktestDataError(f"{where}: ts is not on the NSE 15-minute candle grid")
        try:
            candle = Candle(
                security_id=sid,
                ts=epoch(start),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BacktestDataError(f"{where}: invalid OHLCV row: {exc}") from exc
        if not all(math.isfinite(v) and v > 0 for v in (candle.open, candle.high, candle.low, candle.close)):
            raise BacktestDataError(f"{where}: OHLC values must be positive and finite")
        if candle.high < max(candle.open, candle.low, candle.close) or candle.low > min(candle.open, candle.high, candle.close):
            raise BacktestDataError(f"{where}: inconsistent OHLC range")
        if candle.volume < 0:
            raise BacktestDataError(f"{where}: volume cannot be negative")
        key = (sid, candle.ts)
        if key in option_keys:
            raise BacktestDataError(f"{where}: duplicate option candle {key}")
        option_keys.add(key)
        option_candles.setdefault(sid, []).append(candle)
        anchor_flags.setdefault(sid, []).append((candle.ts, _bool(row.get("is_first_tradable", ""), where)))
    for rows in option_candles.values():
        rows.sort(key=lambda candle: candle.ts)
    option_coverage = _candle_gap_summary(option_candles)
    quality.files["option_candles.csv"]["coverage"] = option_coverage
    if option_coverage["missing_grid_intervals_within_observed_range"]:
        quality.warnings.append({
            "code": "OPTION_CANDLE_INTERNAL_GAPS_OBSERVED",
            "sessions_with_gaps": option_coverage["sessions_with_internal_gaps"],
            "missing_grid_intervals": option_coverage["missing_grid_intervals_within_observed_range"],
            "message": option_coverage["scope_note"],
        })
    anchor_confirmed = set()
    for sid, rows in option_candles.items():
        first_ts = rows[0].ts
        flagged = [ts for ts, yes in anchor_flags[sid] if yes]
        if flagged == [first_ts]:
            anchor_confirmed.add(sid)
        else:
            quality.unverified_option_anchors.append(sid)
    if quality.unverified_option_anchors:
        quality.warnings.append({
            "code": "OPTION_AVWAP_ANCHORS_UNVERIFIED",
            "count": len(quality.unverified_option_anchors),
            "message": "Option history does not explicitly identify the first tradable candle; those contracts are excluded from trades by default.",
        })

    underlying_rows = _read_csv(base / "underlying_candles.csv")
    quality.files["underlying_candles.csv"] = {"rows": len(underlying_rows)}
    underlying_candles: dict[str, list[Candle]] = {}
    underlying_keys = set()
    for line, row in enumerate(underlying_rows, 2):
        where = f"underlying_candles.csv:{line}"
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol not in symbols:
            continue
        start = _datetime(row.get("ts"), where)
        grid = candle_start_for(start, 15)
        if grid is None or grid != start:
            raise BacktestDataError(f"{where}: ts is not on the NSE 15-minute candle grid")
        try:
            candle = Candle(
                security_id=str(row.get("security_id", "")).strip() or symbol,
                ts=epoch(start),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row.get("volume", 0) or 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BacktestDataError(f"{where}: invalid OHLCV row: {exc}") from exc
        if not all(math.isfinite(v) and v > 0 for v in (candle.open, candle.high, candle.low, candle.close)):
            raise BacktestDataError(f"{where}: OHLC values must be positive and finite")
        if candle.high < max(candle.open, candle.low, candle.close) or candle.low > min(candle.open, candle.high, candle.close):
            raise BacktestDataError(f"{where}: inconsistent OHLC range")
        if candle.volume < 0:
            raise BacktestDataError(f"{where}: volume cannot be negative")
        key = (symbol, candle.ts)
        if key in underlying_keys:
            raise BacktestDataError(f"{where}: duplicate underlying candle {key}")
        underlying_keys.add(key)
        underlying_candles.setdefault(symbol, []).append(candle)
    for rows in underlying_candles.values():
        rows.sort(key=lambda candle: candle.ts)
    underlying_coverage = _candle_gap_summary(underlying_candles)
    quality.files["underlying_candles.csv"]["coverage"] = underlying_coverage
    if underlying_coverage["missing_grid_intervals_within_observed_range"]:
        quality.warnings.append({
            "code": "UNDERLYING_CANDLE_INTERNAL_GAPS_OBSERVED",
            "sessions_with_gaps": underlying_coverage["sessions_with_internal_gaps"],
            "missing_grid_intervals": underlying_coverage["missing_grid_intervals_within_observed_range"],
            "message": underlying_coverage["scope_note"],
        })
    missing_underlyings = sorted(set(symbols) - set(underlying_candles))
    if missing_underlyings:
        quality.warnings.append({
            "code": "UNDERLYING_HISTORY_MISSING",
            "symbols": missing_underlyings,
        })

    snapshot_rows = _read_csv(base / "market_snapshots.csv")
    quality.files["market_snapshots.csv"] = {"rows": len(snapshot_rows)}
    snapshots = []
    snapshot_keys = set()
    for line, row in enumerate(snapshot_rows, 2):
        where = f"market_snapshots.csv:{line}"
        source = str(row.get("source", "")).strip().lower()
        if source not in {"chain", "option_quote", "underlying_quote"}:
            raise BacktestDataError(f"{where}: source must be chain, option_quote or underlying_quote")
        observed_at = _datetime(row.get("observed_at"), where)
        symbol = str(row.get("symbol", "")).strip().upper()
        underlying = str(row.get("underlying", "")).strip().upper()
        expiry = str(row.get("expiry", "")).strip()[:10]
        security_id = str(row.get("security_id", "")).strip()
        if not security_id:
            raise BacktestDataError(f"{where}: security_id is required")
        if source == "chain" and (underlying not in symbols or not expiry):
            raise BacktestDataError(f"{where}: chain rows require an in-universe underlying and expiry")
        if source == "underlying_quote" and symbol not in symbols:
            raise BacktestDataError(f"{where}: underlying_quote requires a manifest symbol")
        strike = _optional_float(row, "strike", where)
        option_type = str(row.get("option_type", "")).strip().upper()
        if source == "chain" and (strike is None or option_type not in {"CE", "PE"}):
            raise BacktestDataError(f"{where}: chain rows require strike and CE/PE")
        key = (source, observed_at, security_id)
        if key in snapshot_keys:
            raise BacktestDataError(f"{where}: duplicate market snapshot {key}")
        snapshot_keys.add(key)
        snapshots.append(MarketSnapshot(
            source=source,
            observed_at=observed_at,
            symbol=symbol,
            underlying=underlying,
            expiry=expiry,
            security_id=security_id,
            strike=strike,
            option_type=option_type,
            spot=_optional_float(row, "spot", where),
            last_price=_optional_float(row, "last_price", where),
            volume=_optional_int(row, "volume", where),
            open_interest=_optional_int(row, "open_interest", where),
            oi_change=_optional_int(row, "oi_change", where),
            bid=_optional_float(row, "bid", where),
            ask=_optional_float(row, "ask", where),
            bid_quantity=_optional_int(row, "bid_quantity", where),
            ask_quantity=_optional_int(row, "ask_quantity", where),
            bid_depth=_json_depth(row, "bid_depth_json", where),
            ask_depth=_json_depth(row, "ask_depth_json", where),
            implied_volatility=_optional_float(row, "implied_volatility", where),
        ))
    snapshots.sort(key=lambda row: (row.observed_at, row.source, row.security_id))
    if not any(row.source == "chain" for row in snapshots):
        quality.warnings.append({"code": "NO_HISTORICAL_OPTION_CHAIN", "message": "Scanner backtest cannot rank without point-in-time option-chain snapshots."})
    if not any(row.source == "option_quote" for row in snapshots):
        quality.warnings.append({"code": "NO_HISTORICAL_OPTION_QUOTES", "message": "Execution spread/depth cannot be measured without timestamped option quote snapshots."})
    if not any(row.source == "underlying_quote" for row in snapshots):
        quality.warnings.append({"code": "NO_HISTORICAL_UNDERLYING_QUOTES", "message": "Historical spot must come from chain snapshots or timestamped underlying quotes."})

    quality.files["option_candles.csv"].update({
        "contracts": len(option_candles),
        "anchor_confirmed_contracts": len(anchor_confirmed),
        "first_candle_timestamp": min((rows[0].ts for rows in option_candles.values()), default=None),
    })
    quality.files["underlying_candles.csv"].update({"symbols": len(underlying_candles)})
    quality.files["market_snapshots.csv"].update({
        "sources": {source: sum(1 for row in snapshots if row.source == source)
                    for source in ("chain", "option_quote", "underlying_quote")},
    })
    return Phase7Dataset(
        root=base,
        manifest=manifest,
        catalog=catalog,
        symbols=symbols,
        scan_events=scans,
        option_candles=option_candles,
        underlying_candles=underlying_candles,
        snapshots=snapshots,
        anchor_confirmed=anchor_confirmed,
        quality=quality,
    )
