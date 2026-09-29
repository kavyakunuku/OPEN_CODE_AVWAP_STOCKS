"""Point-in-time NSE stock-option contract discovery.

This module deliberately stops at identifying real contracts.  It does not
score liquidity/volatility, generate AVWAP signals, or interact with orders.

Inputs that must be supplied by the caller are explicit: a point-in-time
contract-master catalog, a validated Phase 1 stock universe (or its manager),
and either the underlying price for the requested timestamp or a price
provider.  Historical lookups never fall forward to a newer master snapshot.
"""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from math import gcd
from typing import Callable, Iterable, Optional

from common.models import OptionContract
from common.utils import IST, SESSION_CLOSE, from_epoch
from market.universe import find_atm, select_monthly_expiry

DEFAULT_EXPIRY_SWITCH_DAY = 24
MAX_ITM_STRIKES = 6


class ContractDiscoveryError(ValueError):
    """Base error for invalid or unavailable discovery inputs."""


class HistoricalContractSnapshotUnavailable(ContractDiscoveryError):
    """Raised rather than using a future/current master for a past query."""


class UnderlyingPriceUnavailable(ContractDiscoveryError):
    """The as-of underlying price was not supplied or could not be resolved."""


class UnvalidatedUnderlying(ContractDiscoveryError):
    """The requested symbol is not enabled by the Phase 1 stock universe."""


class NoEligibleExpiry(ContractDiscoveryError):
    """No unexpired expiry is available for the requested underlying."""


def _as_ist_datetime(value: datetime | date | str) -> datetime:
    """Normalize an as-of value; naive times are NSE local (Asia/Kolkata)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid timestamp {value!r}; use ISO-8601") from exc
    else:
        raise TypeError("timestamp must be a datetime, date, or ISO-8601 string")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _as_date(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _decimal(value) -> Optional[Decimal]:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not out.is_finite():
        return None
    return out


def _decimal_gcd(values: Iterable[Decimal]) -> Optional[Decimal]:
    """Exact decimal gcd, used to infer a strike grid without a fixed rupee step."""
    vals = [v.normalize() for v in values if v.is_finite()]
    if len(vals) < 2:
        return None
    scale = max(0, max(-v.as_tuple().exponent for v in vals))
    factor = 10 ** scale
    integers = [int(v * factor) for v in vals]
    step = 0
    for left, right in zip(integers, integers[1:]):
        step = gcd(step, abs(right - left))
    return Decimal(step).scaleb(-scale) if step > 0 else None


def detect_strike_interval(strikes: Iterable[float], atm: Optional[float] = None) -> Optional[float]:
    """Infer the actual grid interval from listed strike metadata.

    If ``atm`` is supplied, use the ATM and at most the seven closest strikes
    on either side.  That keeps a remote, finer grid from changing the local
    interval.  The decimal gcd tolerates omitted strikes (for example,
    100, 120, 130 still reveals a base step of 10) and avoids float rounding.
    This is a diagnostic/fallback helper for regular local grids. When the
    master has no explicit interval, discovery itself follows the actual
    ordered strike rows, which also supports nonuniform grids without
    fabricating an unobserved strike.
    """
    vals = sorted({d for v in strikes if (d := _decimal(v)) is not None})
    if len(vals) < 2:
        return None
    if atm is not None:
        center = _decimal(atm)
        if center is None:
            return None
        if center not in vals:
            center = min(vals, key=lambda v: (abs(v - center), v))
        below = [v for v in vals if v < center][-7:]
        above = [v for v in vals if v > center][:7]
        vals = sorted(set(below + [center] + above))
    interval = _decimal_gcd(vals)
    return float(interval) if interval is not None else None


def _explicit_interval(contracts: Iterable[OptionContract]) -> Optional[Decimal]:
    values = {
        d for c in contracts
        if (d := _decimal(getattr(c, "strike_interval", None))) is not None and d > 0
    }
    if len(values) == 1:
        return next(iter(values))
    # If a source supplies multiple interval values, do not guess which one is
    # authoritative; infer the local grid from actual listed strike prices.
    return None


def _side_first_interval(
    contracts: list[OptionContract], strikes: list[float], atm: float, option_type: str
) -> Optional[Decimal]:
    """Return explicit interval metadata, otherwise the first real grid gap."""
    explicit = _explicit_interval(contracts)
    if explicit is not None:
        return explicit
    center = _decimal(atm)
    if center is None:
        return None
    side_values = {
        d for s in strikes if (d := _decimal(s)) is not None
        and ((d < center) if option_type == "CE" else (d > center))
    }
    if not side_values:
        return None
    adjacent = max(side_values) if option_type == "CE" else min(side_values)
    return abs(center - adjacent)


def _side_targets(
    strikes: list[float],
    atm: float,
    option_type: str,
    max_itm: int,
    explicit_interval: Optional[Decimal],
) -> list[tuple[Optional[Decimal], Optional[Decimal]]]:
    """Build exact target strikes, using metadata steps or the actual grid.

    An explicit interval permits arithmetic targets (so a truly absent strike
    is reported). Without one, the master does not tell us whether a wider gap
    means a missing strike or a nonuniform grid; use the ordered real strikes
    and record each actual gap instead of inventing a step.
    """
    center = _decimal(atm)
    if center is None:
        return [(None, None)] * (max_itm + 1)
    targets: list[tuple[Optional[Decimal], Optional[Decimal]]] = [(center, None)]
    if explicit_interval is not None:
        direction = Decimal(-1) if option_type == "CE" else Decimal(1)
        targets.extend(
            (center + direction * explicit_interval * level, explicit_interval)
            for level in range(1, max_itm + 1)
        )
        return targets

    values = {
        d for strike in strikes if (d := _decimal(strike)) is not None
        and ((d < center) if option_type == "CE" else (d > center))
    }
    ordered = sorted(values, reverse=(option_type == "CE"))
    previous = center
    for level in range(max_itm):
        if level >= len(ordered):
            targets.append((None, None))
            continue
        strike = ordered[level]
        targets.append((strike, abs(previous - strike)))
        previous = strike
    return targets


def _is_unexpired(expiry: date, as_of: datetime) -> bool:
    """Keep expiry-day contracts through the NSE session; drop them at close."""
    if expiry > as_of.date():
        return True
    if expiry < as_of.date():
        return False
    return as_of.timetz().replace(tzinfo=None) < SESSION_CLOSE


def _born_by(contract: OptionContract, as_of: datetime) -> bool:
    """Use only the option's own birth/listing date, never the underlying date."""
    birth = _as_date(getattr(contract, "contract_birth_date", None))
    return birth is None or birth <= as_of.date()


def _normalise_price(value) -> Optional[float]:
    if value is None:
        return None
    if hasattr(value, "spot"):
        value = value.spot
    elif hasattr(value, "price"):
        value = value.price
    elif hasattr(value, "last_price"):
        value = value.last_price
    elif isinstance(value, dict):
        value = next((value.get(key) for key in (
            "spot", "last_price", "lastPrice", "underlying_ltp", "price"
        ) if value.get(key) is not None), None)
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def contract_birth_date_from_candles(candles: Iterable) -> Optional[str]:
    """Return the date of the first supplied candle for that option contract.

    Callers should pass a complete, contract-specific history window.  This
    helper intentionally accepts option candles only and has no underlying-date
    fallback.
    """
    timestamps = []
    for candle in candles or ():
        try:
            timestamps.append(int(candle.ts))
        except (AttributeError, TypeError, ValueError):
            continue
    if not timestamps:
        return None
    return from_epoch(min(timestamps)).date().isoformat()


@dataclass(frozen=True)
class ContractCatalogSnapshot:
    """Instrument-master contents captured at ``as_of`` (not an effective date)."""
    as_of: datetime
    contracts: tuple[OptionContract, ...]
    source: str = "unknown"


class OptionContractCatalog:
    """Time-versioned contract masters for live discovery and historical replay.

    Persist snapshots outside Git (for example under ``data/``).  A query only
    sees the newest snapshot captured at or before its timestamp; if there is
    no such snapshot, discovery fails instead of using present-day contracts.
    """

    def __init__(self, snapshots: Iterable[ContractCatalogSnapshot] = ()):
        self._snapshots: dict[datetime, ContractCatalogSnapshot] = {}
        for snapshot in snapshots:
            self.add_snapshot(snapshot.as_of, snapshot.contracts, snapshot.source)

    @property
    def snapshots(self) -> list[ContractCatalogSnapshot]:
        return [self._snapshots[k] for k in sorted(self._snapshots)]

    def add_snapshot(
        self,
        as_of: datetime | date | str,
        contracts: Iterable[OptionContract],
        source: str = "unknown",
    ) -> ContractCatalogSnapshot:
        captured_at = _as_ist_datetime(as_of)
        # Preserve real rows; discard only duplicate instrument IDs inside
        # one snapshot.  The first row wins deterministically.
        rows: list[OptionContract] = []
        seen_ids: set[str] = set()
        for contract in contracts:
            sid = str(getattr(contract, "security_id", "") or "").strip()
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            rows.append(contract)
        snapshot = ContractCatalogSnapshot(captured_at, tuple(rows), source)
        self._snapshots[captured_at] = snapshot
        return snapshot

    def snapshot_for(self, timestamp: datetime | date | str) -> ContractCatalogSnapshot:
        as_of = _as_ist_datetime(timestamp)
        eligible = [captured for captured in self._snapshots if captured <= as_of]
        if not eligible:
            first = min(self._snapshots) if self._snapshots else None
            raise HistoricalContractSnapshotUnavailable(
                "No contract-master snapshot was captured at or before "
                f"{as_of.isoformat()}; refusing to use a future/current master "
                f"(first available snapshot: {first.isoformat() if first else 'none'})."
            )
        return self._snapshots[max(eligible)]

    @classmethod
    def from_universe_manager(
        cls,
        universe_manager,
        *,
        captured_at: Optional[datetime | date | str] = None,
        source: str = "Phase 1 validated Dhan instrument master",
    ) -> "OptionContractCatalog":
        """Build a current snapshot from the validated Phase 1 stock universe."""
        symbols = universe_manager.get_enabled_symbols()
        if not symbols:
            raise ValueError("Phase 1 universe has no enabled/validated symbols")
        contracts = universe_manager.get_validated_contracts()
        at = captured_at or getattr(universe_manager, "master_loaded_at", None)
        if at is None:
            raise ValueError("Phase 1 manager has no master-loaded timestamp")
        catalog = cls()
        catalog.add_snapshot(at, contracts, source=source)
        return catalog

    def save(self, path: str) -> None:
        """Write snapshots as JSON (or gzip JSON for paths ending in .gz)."""
        path = os.fspath(path)
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        payload = {
            "schema_version": 1,
            "snapshots": [
                {
                    "as_of": snapshot.as_of.isoformat(),
                    "source": snapshot.source,
                    "contracts": [asdict(c) for c in snapshot.contracts],
                }
                for snapshot in self.snapshots
            ],
        }
        temporary = path + ".tmp"
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(temporary, "wt", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"))
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    @classmethod
    def load(cls, path: str) -> "OptionContractCatalog":
        path = os.fspath(path)
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        if payload.get("schema_version") != 1:
            raise ValueError(f"unsupported contract catalog schema in {path}")
        catalog = cls()
        for snapshot in payload.get("snapshots", []):
            contracts = [OptionContract(**row) for row in snapshot.get("contracts", [])]
            catalog.add_snapshot(
                snapshot["as_of"], contracts, source=snapshot.get("source", "unknown")
            )
        return catalog


@dataclass(frozen=True)
class DiscoveredContract:
    """A real option contract paired with its ATM/ITM rank."""
    contract: OptionContract
    moneyness: str
    strike_interval: Optional[float] = None

    def to_dict(self) -> dict:
        c = self.contract
        return {
            "underlying": c.underlying,
            "symbol": c.symbol,
            "expiry": c.expiry,
            "strike": c.strike,
            "option_type": c.option_type,
            # The repo's canonical Dhan field is security_id; expose the
            # instrument_token alias as well for consumers using that term.
            "security_id": c.security_id,
            "instrument_token": c.instrument_token,
            "exchange": c.exchange,
            "exchange_segment": c.exchange_segment,
            "lot_size": c.lot_size,
            "tick_size": c.tick_size,
            "contract_birth_date": c.contract_birth_date,
            "expiry_flag": c.expiry_flag,
            "moneyness": self.moneyness,
            "strike_interval": self.strike_interval,
        }


@dataclass
class OptionContractDiscoveryResult:
    underlying: str
    timestamp: datetime
    underlying_price: float
    expiry: date
    atm_strike: float
    call_strike_interval: Optional[float]
    put_strike_interval: Optional[float]
    calls: list[DiscoveredContract]
    puts: list[DiscoveredContract]
    missing_contracts: list[str]
    max_itm: int
    snapshot_as_of: datetime
    metadata_warnings: list[str]

    @property
    def requested_count(self) -> int:
        return 2 * (self.max_itm + 1)

    @property
    def available_count(self) -> int:
        return len(self.calls) + len(self.puts)

    @property
    def contracts(self) -> list[OptionContract]:
        return [item.contract for item in self.calls + self.puts]

    @property
    def strike_interval(self) -> Optional[float]:
        """Single interval when both sides agree; otherwise callers can inspect each side."""
        if self.call_strike_interval == self.put_strike_interval:
            return self.call_strike_interval
        if self.call_strike_interval is None:
            return self.put_strike_interval
        if self.put_strike_interval is None:
            return self.call_strike_interval
        return None

    def get_atm_contract(self, option_type: Optional[str] = None):
        """Return an ATM contract, or the CE/PE ATM pair when type is omitted."""
        if option_type is not None:
            kind = _normalise_option_type(option_type)
            side = self.calls if kind == "CE" else self.puts
            return next((item.contract for item in side if item.moneyness == "ATM"), None)
        return {
            kind: self.get_atm_contract(kind)
            for kind in ("CE", "PE")
        }

    def get_call_contracts(self) -> list[OptionContract]:
        return [item.contract for item in self.calls]

    def get_put_contracts(self) -> list[OptionContract]:
        return [item.contract for item in self.puts]

    def get_expiry(self) -> date:
        return self.expiry

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "timestamp": self.timestamp.isoformat(),
            "underlying_price": self.underlying_price,
            "expiry": self.expiry.isoformat(),
            "atm_strike": self.atm_strike,
            "strike_interval": self.strike_interval,
            "strike_intervals": {
                "CE": self.call_strike_interval,
                "PE": self.put_strike_interval,
            },
            "requested_count": self.requested_count,
            "available_count": self.available_count,
            "missing_contracts": list(self.missing_contracts),
            "snapshot_as_of": self.snapshot_as_of.isoformat(),
            "calls": [item.to_dict() for item in self.calls],
            "puts": [item.to_dict() for item in self.puts],
            "metadata_warnings": list(self.metadata_warnings),
        }


def _normalise_option_type(option_type: str) -> str:
    value = (option_type or "").strip().upper()
    if value in ("CE", "CALL"):
        return "CE"
    if value in ("PE", "PUT"):
        return "PE"
    raise ValueError("option_type must be CE/CALL or PE/PUT")


class OptionContractDiscovery:
    """Discovery engine configured with point-in-time market data sources."""

    def __init__(
        self,
        catalog: OptionContractCatalog,
        *,
        price_provider: Optional[Callable[[str, datetime], object]] = None,
        birth_date_provider: Optional[Callable[[OptionContract, datetime], object]] = None,
        validated_symbols: Optional[Iterable[str]] = None,
        universe_manager=None,
        expiry_switch_day: int = DEFAULT_EXPIRY_SWITCH_DAY,
    ):
        if not isinstance(catalog, OptionContractCatalog):
            raise TypeError("catalog must be an OptionContractCatalog")
        if validated_symbols is None and universe_manager is None:
            raise ValueError("pass validated_symbols or the initialized Phase 1 universe_manager")
        self.catalog = catalog
        self.price_provider = price_provider
        self.birth_date_provider = birth_date_provider
        self.universe_manager = universe_manager
        self.validated_symbols = {
            str(s).strip().upper() for s in (validated_symbols or []) if str(s).strip()
        }
        self.expiry_switch_day = int(expiry_switch_day)
        if self.expiry_switch_day < 1 or self.expiry_switch_day > 31:
            raise ValueError("expiry_switch_day must be in 1..31")

    @classmethod
    def from_universe_manager(
        cls,
        universe_manager,
        *,
        catalog: Optional[OptionContractCatalog] = None,
        price_provider: Optional[Callable[[str, datetime], object]] = None,
        birth_date_provider: Optional[Callable[[OptionContract, datetime], object]] = None,
        expiry_switch_day: int = DEFAULT_EXPIRY_SWITCH_DAY,
        captured_at: Optional[datetime | date | str] = None,
    ) -> "OptionContractDiscovery":
        catalog = catalog or OptionContractCatalog.from_universe_manager(
            universe_manager, captured_at=captured_at
        )
        return cls(
            catalog,
            price_provider=price_provider,
            birth_date_provider=birth_date_provider,
            universe_manager=universe_manager,
            expiry_switch_day=expiry_switch_day,
        )

    def _is_valid_symbol(self, symbol: str) -> bool:
        if self.universe_manager is not None:
            return bool(self.universe_manager.is_symbol_enabled(symbol))
        return symbol in self.validated_symbols

    def discover_option_contracts(
        self,
        symbol: str,
        timestamp: datetime | date | str,
        max_itm: int = 6,
        *,
        underlying_price: Optional[float] = None,
    ) -> OptionContractDiscoveryResult:
        """Discover the selected monthly expiry's ATM and up to six ITM CE/PEs.

        Explicit per-contract strike-interval metadata is applied as exact
        multiples. If the source has no interval field, the engine follows the
        real ordered strike grid (and records each local gap), preserving
        nonuniform grids. Missing option rows are reported; no contract is
        invented.
        """
        name = (symbol or "").strip().upper()
        if not name:
            raise ValueError("symbol must not be empty")
        if not self._is_valid_symbol(name):
            raise UnvalidatedUnderlying(f"{name} is not in the validated Phase 1 universe")
        count = int(max_itm)
        if count < 0 or count > MAX_ITM_STRIKES:
            raise ValueError(f"max_itm must be between 0 and {MAX_ITM_STRIKES}")

        as_of = _as_ist_datetime(timestamp)
        snapshot = self.catalog.snapshot_for(as_of)
        spot = _normalise_price(underlying_price)
        if spot is None and self.price_provider is not None:
            spot = _normalise_price(self.price_provider(name, as_of))
        if spot is None:
            raise UnderlyingPriceUnavailable(
                f"No positive underlying price was supplied for {name} at {as_of.isoformat()}"
            )

        eligible: list[OptionContract] = []
        for contract in snapshot.contracts:
            if (contract.underlying or "").strip().upper() != name:
                continue
            if contract.option_type not in ("CE", "PE"):
                continue
            if not str(contract.security_id or "").strip():
                continue
            strike = _decimal(contract.strike)
            expiry = _as_date(contract.expiry)
            if strike is None or strike <= 0 or expiry is None:
                continue
            if not _is_unexpired(expiry, as_of) or not _born_by(contract, as_of):
                continue
            eligible.append(contract)

        expiries = sorted({_as_date(c.expiry) for c in eligible if _as_date(c.expiry)})
        expiry = select_monthly_expiry(as_of.date(), expiries, self.expiry_switch_day)
        if expiry is None:
            raise NoEligibleExpiry(f"No unexpired option expiry is available for {name} at {as_of.isoformat()}")

        expiry_contracts = [c for c in eligible if _as_date(c.expiry) == expiry]
        strikes = sorted({float(c.strike) for c in expiry_contracts})
        atm = find_atm(spot, strikes)
        if atm is None:
            raise NoEligibleExpiry(f"Selected expiry {expiry} has no valid strikes for {name}")

        calls_for_interval = [c for c in expiry_contracts if c.option_type == "CE"]
        puts_for_interval = [c for c in expiry_contracts if c.option_type == "PE"]
        ce_explicit_interval = _explicit_interval(calls_for_interval)
        pe_explicit_interval = _explicit_interval(puts_for_interval)
        ce_interval = _side_first_interval(calls_for_interval, strikes, atm, "CE")
        pe_interval = _side_first_interval(puts_for_interval, strikes, atm, "PE")
        ce_targets = _side_targets(strikes, atm, "CE", count, ce_explicit_interval)
        pe_targets = _side_targets(strikes, atm, "PE", count, pe_explicit_interval)

        # Use Decimal keys to avoid a float equality miss (e.g. 0.3 vs 0.1*3).
        by_key: dict[tuple[Decimal, str], OptionContract] = {}
        for contract in expiry_contracts:
            key_strike = _decimal(contract.strike)
            if key_strike is not None:
                by_key.setdefault((key_strike, contract.option_type), contract)

        calls: list[DiscoveredContract] = []
        puts: list[DiscoveredContract] = []
        missing: list[str] = []
        resolution_warnings: list[str] = []
        for option_type, targets, output in (
            ("CE", ce_targets, calls), ("PE", pe_targets, puts)
        ):
            for level, (target, step) in enumerate(targets):
                label = "ATM" if level == 0 else f"ITM{level}"
                if target is None:
                    missing.append(f"{label} {option_type}")
                    continue
                contract = by_key.get((target, option_type))
                if contract is None:
                    missing.append(f"{label} {option_type}")
                    continue
                if not contract.contract_birth_date and self.birth_date_provider is not None:
                    try:
                        resolved_birth = _as_date(self.birth_date_provider(contract, as_of))
                    except Exception as exc:
                        resolved_birth = None
                        resolution_warnings.append(
                            f"could not resolve contract birth date for {contract.security_id}: {exc}"
                        )
                    if resolved_birth is not None:
                        contract = replace(
                            contract, contract_birth_date=resolved_birth.isoformat()
                        )
                        if resolved_birth > as_of.date():
                            missing.append(f"{label} {option_type}")
                            continue
                output.append(DiscoveredContract(
                    contract=contract,
                    moneyness=label,
                    strike_interval=(float(step) if level > 0 and step is not None else None),
                ))

        warnings = resolution_warnings + [
            f"contract birth/listing date unavailable for {item.contract.security_id}; "
            "no underlying listing date was substituted"
            for item in calls + puts
            if not item.contract.contract_birth_date
        ]
        return OptionContractDiscoveryResult(
            underlying=name,
            timestamp=as_of,
            underlying_price=spot,
            expiry=expiry,
            atm_strike=float(atm),
            call_strike_interval=float(ce_interval) if ce_interval is not None else None,
            put_strike_interval=float(pe_interval) if pe_interval is not None else None,
            calls=calls,
            puts=puts,
            missing_contracts=missing,
            max_itm=count,
            snapshot_as_of=snapshot.as_of,
            metadata_warnings=warnings,
        )


def discover_option_contracts(
    symbol: str,
    timestamp: datetime | date | str,
    max_itm: int = 6,
    *,
    catalog: Optional[OptionContractCatalog] = None,
    underlying_price: Optional[float] = None,
    price_provider: Optional[Callable[[str, datetime], object]] = None,
    birth_date_provider: Optional[Callable[[OptionContract, datetime], object]] = None,
    validated_symbols: Optional[Iterable[str]] = None,
    universe_manager=None,
    expiry_switch_day: int = DEFAULT_EXPIRY_SWITCH_DAY,
    snapshot_as_of: Optional[datetime | date | str] = None,
) -> OptionContractDiscoveryResult:
    """Convenience API for one discovery request.

    Use ``catalog`` for historical/versioned masters.  If omitted, an
    initialized Phase 1 ``universe_manager`` is required and its current
    validated master is captured at ``snapshot_as_of`` (or the manager's
    master-load time); that one current snapshot cannot answer older queries.
    """
    if catalog is None:
        if universe_manager is None:
            raise ValueError("catalog or initialized Phase 1 universe_manager is required")
        catalog = OptionContractCatalog.from_universe_manager(
            universe_manager, captured_at=snapshot_as_of
        )
    engine = OptionContractDiscovery(
        catalog,
        price_provider=price_provider,
        birth_date_provider=birth_date_provider,
        validated_symbols=validated_symbols,
        universe_manager=universe_manager,
        expiry_switch_day=expiry_switch_day,
    )
    return engine.discover_option_contracts(
        symbol, timestamp, max_itm, underlying_price=underlying_price
    )


def get_atm_contract(result: OptionContractDiscoveryResult, option_type: Optional[str] = None):
    return result.get_atm_contract(option_type)


def get_call_contracts(result: OptionContractDiscoveryResult) -> list[OptionContract]:
    return result.get_call_contracts()


def get_put_contracts(result: OptionContractDiscoveryResult) -> list[OptionContract]:
    return result.get_put_contracts()


def get_expiry(result: OptionContractDiscoveryResult) -> date:
    return result.get_expiry()
