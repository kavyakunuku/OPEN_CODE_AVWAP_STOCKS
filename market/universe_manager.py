"""NSE F&O Stock Universe Manager.

Manages the candidate stock universe for the option-selling strategy.
Validates configured symbols against the Dhan instrument master.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from common.config import load_config
from common.utils import now_ist
from dhan.instruments import load_master_bundle, fo_stock_universe
from dhan.client import DhanREST

log = logging.getLogger("avwap.market.universe_manager")


@dataclass
class SymbolValidationResult:
    """Result of validating a single symbol against the instrument master."""
    symbol: str
    is_valid: bool
    has_contracts: bool
    contract_count: int = 0
    error: Optional[str] = None


@dataclass
class UniverseValidationReport:
    """Complete validation report for the universe."""
    timestamp: datetime
    total_configured: int
    enabled_count: int
    valid_symbols: list[str]
    invalid_symbols: list[str]
    missing_contracts: list[str]
    validation_details: list[SymbolValidationResult]

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "total_symbols": self.total_configured,
            "enabled_symbols": self.enabled_count,
            "valid_symbols": self.valid_symbols,
            "invalid_symbols": self.invalid_symbols,
            "missing_contracts": self.missing_contracts,
            "details": [
                {
                    "symbol": d.symbol,
                    "is_valid": d.is_valid,
                    "has_contracts": d.has_contracts,
                    "contract_count": d.contract_count,
                    "error": d.error,
                }
                for d in self.validation_details
            ],
        }


class UniverseManager:
    """Manages the NSE F&O stock candidate universe.

    Loads the configured universe from config/universe.json, validates each
    symbol against the Dhan instrument master, and provides access to the
    validated symbol list.
    """

    def __init__(self, cfg: Optional[dict] = None, rest: Optional[DhanREST] = None):
        self.cfg = cfg or load_config()
        self.rest = rest
        self._universe_cfg = self._load_universe_config()
        self._master_contracts: list = []
        self._master_loaded_at: Optional[datetime] = None
        self._valid_symbols: list[str] = []
        self._invalid_symbols: list[str] = []
        self._missing_contracts: list[str] = []
        self._validation_details: list[SymbolValidationResult] = []
        self._initialized = False

    def _load_universe_config(self) -> dict:
        """Load universe configuration from config/universe.json."""
        universe_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "universe.json"
        )
        if not os.path.exists(universe_path):
            log.warning("Universe config not found at %s, using defaults", universe_path)
            return {"enabled": True, "symbols": []}
        with open(universe_path, "r", encoding="utf-8") as f:
            return json.load(f).get("universe", {"enabled": True, "symbols": []})

    def initialize(self, rest: Optional[DhanREST] = None) -> UniverseValidationReport:
        """Initialize the universe manager by loading and validating symbols.

        Args:
            rest: Optional DhanREST client. If not provided, uses the one
                  passed to constructor or creates a new one from config.

        Returns:
            UniverseValidationReport with validation results.
        """
        if rest:
            self.rest = rest
        elif self.rest is None:
            self.rest = DhanREST(
                self.cfg.get("dhan", {}).get("client_id", ""),
                self.cfg.get("dhan", {}).get("access_token", ""),
                base_url=self.cfg.get("dhan", {}).get("rest_base_url", "https://api.dhan.co"),
                timeout=float(self.cfg.get("dhan", {}).get("http_timeout_seconds", 10)),
            )

        cache_dir = self.cfg.get("storage", {}).get("instrument_cache_dir", "data/instruments")
        ttl = float(self.cfg.get("dhan", {}).get("instrument_master_ttl_hours", 12))
        default_lot = int(self.cfg.get("risk", {}).get("default_lot_size", 250))

        log.info("Loading instrument master for universe validation...")
        bundle = load_master_bundle(self.rest, cache_dir, ttl, default_lot)
        return self.initialize_from_master_bundle(bundle, captured_at=now_ist())

    def initialize_from_master_bundle(
        self, bundle, *, captured_at: Optional[datetime] = None
    ) -> UniverseValidationReport:
        """Validate the configured 20-stock universe against an already-loaded master.

        TraderApp already loads the Dhan master for contract discovery. This
        entry point reuses that exact snapshot rather than making a duplicate
        download or validating against a different master file.
        """
        self._master_contracts = list(getattr(bundle, "contracts", ()) or ())
        self._master_loaded_at = captured_at or now_ist()

        # Get all valid F&O stock underlyings from the master
        master_underlyings = set(fo_stock_universe(self._master_contracts))
        log.info("Instrument master contains %d F&O stock underlyings", len(master_underlyings))

        # Validate configured symbols
        configured_symbols = self._universe_cfg.get("symbols", [])
        enabled = self._universe_cfg.get("enabled", True)

        if not enabled:
            log.info("Universe is disabled in configuration")
            self._initialized = True
            return UniverseValidationReport(
                timestamp=datetime.now(),
                total_configured=len(configured_symbols),
                enabled_count=0,
                valid_symbols=[],
                invalid_symbols=[],
                missing_contracts=[],
                validation_details=[],
            )

        self._valid_symbols = []
        self._invalid_symbols = []
        self._missing_contracts = []
        self._validation_details = []

        for symbol in configured_symbols:
            symbol_upper = symbol.strip().upper()
            result = self._validate_symbol(symbol_upper, master_underlyings)
            self._validation_details.append(result)

            if result.is_valid:
                self._valid_symbols.append(symbol_upper)
            else:
                self._invalid_symbols.append(symbol_upper)
                if not result.has_contracts:
                    self._missing_contracts.append(symbol_upper)

        self._initialized = True

        report = UniverseValidationReport(
            timestamp=datetime.now(),
            total_configured=len(configured_symbols),
            enabled_count=len(self._valid_symbols),
            valid_symbols=self._valid_symbols,
            invalid_symbols=self._invalid_symbols,
            missing_contracts=self._missing_contracts,
            validation_details=self._validation_details,
        )

        self._log_validation_report(report)
        return report

    def _validate_symbol(self, symbol: str, master_underlyings: set[str]) -> SymbolValidationResult:
        """Validate a single symbol against the instrument master."""
        if not symbol:
            return SymbolValidationResult(
                symbol=symbol,
                is_valid=False,
                has_contracts=False,
                error="Empty symbol",
            )

        if symbol not in master_underlyings:
            return SymbolValidationResult(
                symbol=symbol,
                is_valid=False,
                has_contracts=False,
                error="Not found in Dhan instrument master (not an F&O stock)",
            )

        # Count contracts for this underlying
        contract_count = sum(1 for c in self._master_contracts if c.underlying == symbol)

        if contract_count == 0:
            return SymbolValidationResult(
                symbol=symbol,
                is_valid=False,
                has_contracts=False,
                error="No option contracts found in master",
            )

        return SymbolValidationResult(
            symbol=symbol,
            is_valid=True,
            has_contracts=True,
            contract_count=contract_count,
        )

    def _log_validation_report(self, report: UniverseValidationReport) -> None:
        """Log the validation results."""
        log.info("=== Universe Validation Report ===")
        log.info("Total configured symbols: %d", report.total_configured)
        log.info("Enabled universe: %s", "Yes" if report.enabled_count > 0 else "No")
        log.info("Valid symbols (%d): %s", len(report.valid_symbols), ", ".join(report.valid_symbols) or "none")
        if report.invalid_symbols:
            log.warning("Invalid symbols (%d): %s", len(report.invalid_symbols), ", ".join(report.invalid_symbols))
        if report.missing_contracts:
            log.warning("Symbols with no contracts (%d): %s", len(report.missing_contracts), ", ".join(report.missing_contracts))
        log.info("=====================================")

    # ------------------------------------------------------------------ public API
    @property
    def master_loaded_at(self) -> Optional[datetime]:
        """IST timestamp when the current instrument-master snapshot was loaded."""
        return self._master_loaded_at

    def get_validated_contracts(self) -> list:
        """Return master contracts only for the validated Phase 1 universe."""
        if not self._initialized:
            raise RuntimeError("UniverseManager is not initialized")
        allowed = set(self._valid_symbols)
        return [c for c in self._master_contracts if c.underlying.upper() in allowed]

    def get_contracts_for_symbol(self, symbol: str) -> list:
        """Return actual master rows for one enabled Phase 1 symbol, or []."""
        if not self._initialized:
            raise RuntimeError("UniverseManager is not initialized")
        symbol = (symbol or "").strip().upper()
        if symbol not in set(self._valid_symbols):
            return []
        return [c for c in self._master_contracts if c.underlying.upper() == symbol]

    def get_all_symbols(self) -> list[str]:
        """Return all configured symbols (valid + invalid)."""
        return self._universe_cfg.get("symbols", [])

    def get_enabled_symbols(self) -> list[str]:
        """Return only valid, enabled symbols that have F&O contracts."""
        if not self._initialized:
            log.warning("UniverseManager not initialized; call initialize() first")
        return list(self._valid_symbols)

    def is_symbol_enabled(self, symbol: str) -> bool:
        """Check if a symbol is in the valid, enabled universe."""
        if not self._initialized:
            log.warning("UniverseManager not initialized; call initialize() first")
        return symbol.strip().upper() in self._valid_symbols

    def add_symbol(self, symbol: str) -> bool:
        """Add a symbol to the configured universe (in-memory only).

        Note: This does not persist to config file. For persistence,
        update config/universe.json directly.
        """
        symbol_upper = symbol.strip().upper()
        if not symbol_upper:
            return False
        configured = self._universe_cfg.get("symbols", [])
        if symbol_upper not in configured:
            configured.append(symbol_upper)
            self._universe_cfg["symbols"] = configured
            log.info("Added symbol to universe (in-memory): %s", symbol_upper)
        return symbol_upper in self._valid_symbols

    def remove_symbol(self, symbol: str) -> bool:
        """Remove a symbol from the configured universe (in-memory only)."""
        symbol_upper = symbol.strip().upper()
        configured = self._universe_cfg.get("symbols", [])
        if symbol_upper in configured:
            configured.remove(symbol_upper)
            self._universe_cfg["symbols"] = configured
            log.info("Removed symbol from universe (in-memory): %s", symbol_upper)
        if symbol_upper in self._valid_symbols:
            self._valid_symbols.remove(symbol_upper)
        return True

    def get_validation_report(self) -> Optional[UniverseValidationReport]:
        """Return the last validation report."""
        if not self._validation_details:
            return None
        return UniverseValidationReport(
            timestamp=self._validation_details[0].__dict__.get("timestamp", datetime.now())
            if hasattr(self._validation_details[0], '__dict__') else datetime.now(),
            total_configured=len(self.get_all_symbols()),
            enabled_count=len(self._valid_symbols),
            valid_symbols=self._valid_symbols,
            invalid_symbols=self._invalid_symbols,
            missing_contracts=self._missing_contracts,
            validation_details=self._validation_details,
        )

    def export_report_json(self, path: str) -> None:
        """Export validation report to JSON file."""
        report = self.get_validation_report()
        if report:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report.to_dict(), f, indent=2)
            log.info("Exported universe validation report to %s", path)