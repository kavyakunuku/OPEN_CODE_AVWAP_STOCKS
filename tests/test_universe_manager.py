"""Tests for the NSE F&O Stock Universe Manager (Phase 1)."""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import Mock, patch, MagicMock

import pytest

from common.models import OptionContract
from market.universe_manager import (
    UniverseManager,
    SymbolValidationResult,
    UniverseValidationReport,
)


# All 20 symbols from the default universe.json
ALL_UNIVERSE_SYMBOLS = [
    "RELIANCE",
    "ICICIBANK",
    "HDFCBANK",
    "SBIN",
    "AXISBANK",
    "VEDL",
    "HINDZINC",
    "INFY",
    "ITC",
    "BHARTIARTL",
    "TATAMOTORS",
    "BAJFINANCE",
    "ADANIENT",
    "ADANIPORTS",
    "TATASTEEL",
    "HINDALCO",
    "SUNPHARMA",
    "BEL",
    "ETERNAL",
    "LT",
]


def _make_contract(symbol: str, security_id: str, strike: float, opt_type: str) -> OptionContract:
    """Create a test OptionContract."""
    return OptionContract(
        security_id=security_id,
        symbol=f"{symbol} {strike:g} {opt_type}",
        underlying=symbol,
        strike=strike,
        option_type=opt_type,
        expiry="2026-09-29",
        lot_size=25,
    )


def _create_mock_master(underlyings: list[str]) -> Mock:
    """Create a mock instrument master bundle with contracts for given underlyings."""
    bundle = Mock()
    contracts = []
    for sym in underlyings:
        # Add a few strikes per underlying
        for strike in [1400, 1450, 1500, 1550, 1600]:
            for opt_type in ["CE", "PE"]:
                contracts.append(_make_contract(sym, f"{sym}_{strike}_{opt_type}", strike, opt_type))
    bundle.contracts = contracts
    return bundle


class TestUniverseManagerConfig:
    """Tests for universe configuration loading."""

    def test_load_universe_config_from_file(self, tmp_path):
        """Test loading universe configuration from JSON file."""
        config_path = tmp_path / "universe.json"
        config_data = {
            "universe": {
                "enabled": True,
                "symbols": ["RELIANCE", "ICICIBANK", "HDFCBANK"]
            }
        }
        config_path.write_text(json.dumps(config_data))

        with patch("market.universe_manager.os.path.join", return_value=str(config_path)):
            with patch("market.universe_manager.os.path.dirname", return_value=str(tmp_path)):
                with patch("market.universe_manager.os.path.abspath", return_value=str(tmp_path / "universe_manager.py")):
                    from market.universe_manager import UniverseManager
                    mgr = UniverseManager()
                    # The actual loading happens in initialize, not __init__

    def test_universe_config_disabled(self, tmp_path):
        """Test that disabled universe returns empty valid list."""
        config_path = tmp_path / "universe.json"
        config_data = {
            "universe": {
                "enabled": False,
                "symbols": ["RELIANCE", "ICICIBANK"]
            }
        }
        config_path.write_text(json.dumps(config_data))

        # We'll test the full flow in integration tests


class TestUniverseManagerValidation:
    """Tests for symbol validation against instrument master."""

    @pytest.fixture
    def mock_rest(self):
        """Mock DhanREST client."""
        return Mock()

    @pytest.fixture
    def mock_master_bundle_all_valid(self):
        """Mock instrument master with ALL 20 universe symbols."""
        return _create_mock_master(ALL_UNIVERSE_SYMBOLS)

    @pytest.fixture
    def mock_master_bundle_partial(self):
        """Mock instrument master with only some symbols."""
        return _create_mock_master(["RELIANCE", "ICICIBANK", "HDFCBANK"])

    def test_validate_all_symbols_valid(self, mock_rest, mock_master_bundle_all_valid):
        """Test validation when all 20 configured symbols are in master."""
        cfg = {"storage": {"instrument_cache_dir": "data/instruments"},
               "dhan": {"client_id": "test", "access_token": "test"},
               "risk": {"default_lot_size": 25}}

        with patch("market.universe_manager.load_master_bundle", return_value=mock_master_bundle_all_valid):
            with patch("market.universe_manager.DhanREST", return_value=mock_rest):
                mgr = UniverseManager(cfg=cfg)
                report = mgr.initialize(rest=mock_rest)

        assert report.total_configured == 20
        assert report.enabled_count == 20
        assert len(report.valid_symbols) == 20
        assert len(report.invalid_symbols) == 0
        assert len(report.missing_contracts) == 0

    def test_validate_some_invalid(self, mock_rest, mock_master_bundle_partial):
        """Test validation when only some symbols are in master."""
        cfg = {"storage": {"instrument_cache_dir": "data/instruments"},
               "dhan": {"client_id": "test", "access_token": "test"},
               "risk": {"default_lot_size": 25}}

        with patch("market.universe_manager.load_master_bundle", return_value=mock_master_bundle_partial):
            with patch("market.universe_manager.DhanREST", return_value=mock_rest):
                mgr = UniverseManager(cfg=cfg)
                report = mgr.initialize(rest=mock_rest)

        # Only RELIANCE, ICICIBANK, HDFCBANK should be valid
        assert "RELIANCE" in report.valid_symbols
        assert "ICICIBANK" in report.valid_symbols
        assert "HDFCBANK" in report.valid_symbols
        assert report.enabled_count == 3
        assert len(report.invalid_symbols) == 17

    def test_validate_empty_string_filtered(self, mock_rest):
        """Test that empty symbol strings are handled."""
        master_bundle = _create_mock_master(["RELIANCE"])
        cfg = {"storage": {"instrument_cache_dir": "data/instruments"},
               "dhan": {"client_id": "test", "access_token": "test"},
               "risk": {"default_lot_size": 25}}

        with patch("market.universe_manager.load_master_bundle", return_value=master_bundle):
            with patch("market.universe_manager.DhanREST", return_value=mock_rest):
                mgr = UniverseManager(cfg=cfg)
                # Override the universe config to include empty string
                mgr._universe_cfg = {"enabled": True, "symbols": ["RELIANCE", "  ", ""]}
                report = mgr.initialize(rest=mock_rest)

        # Empty/whitespace strings should be marked invalid
        assert len(report.invalid_symbols) >= 1

    def test_contract_count_in_validation(self, mock_rest):
        """Test that contract count is correctly reported."""
        # Each underlying has 5 strikes * 2 types = 10 contracts
        master_bundle = _create_mock_master(["RELIANCE", "ICICIBANK"])
        cfg = {"storage": {"instrument_cache_dir": "data/instruments"},
               "dhan": {"client_id": "test", "access_token": "test"},
               "risk": {"default_lot_size": 25}}

        with patch("market.universe_manager.load_master_bundle", return_value=master_bundle):
            with patch("market.universe_manager.DhanREST", return_value=mock_rest):
                mgr = UniverseManager(cfg=cfg)
                report = mgr.initialize(rest=mock_rest)

        for detail in report.validation_details:
            if detail.is_valid:
                assert detail.contract_count == 10  # 5 strikes * 2 types


class TestUniverseManagerAPI:
    """Tests for the public API methods."""

    @pytest.fixture
    def initialized_manager(self, mock_rest):
        """Create an initialized manager with ALL 20 symbols in master."""
        master_bundle = _create_mock_master(ALL_UNIVERSE_SYMBOLS)
        cfg = {"storage": {"instrument_cache_dir": "data/instruments"},
               "dhan": {"client_id": "test", "access_token": "test"},
               "risk": {"default_lot_size": 25}}

        with patch("market.universe_manager.load_master_bundle", return_value=master_bundle):
            with patch("market.universe_manager.DhanREST", return_value=mock_rest):
                mgr = UniverseManager(cfg=cfg)
                mgr.initialize(rest=mock_rest)
                return mgr

    def test_get_all_symbols(self, initialized_manager):
        """Test get_all_symbols returns all 20 configured symbols."""
        all_symbols = initialized_manager.get_all_symbols()
        assert len(all_symbols) == 20
        assert "RELIANCE" in all_symbols
        assert "ICICIBANK" in all_symbols

    def test_get_enabled_symbols(self, initialized_manager):
        """Test get_enabled_symbols returns all 20 valid symbols."""
        enabled = initialized_manager.get_enabled_symbols()
        assert len(enabled) == 20
        assert all(s in ALL_UNIVERSE_SYMBOLS for s in enabled)

    def test_is_symbol_enabled_valid(self, initialized_manager):
        """Test is_symbol_enabled for valid symbols."""
        assert initialized_manager.is_symbol_enabled("RELIANCE") is True
        assert initialized_manager.is_symbol_enabled("reliance") is True  # case insensitive
        assert initialized_manager.is_symbol_enabled("  RELIANCE  ") is True  # whitespace

    def test_is_symbol_enabled_invalid(self, initialized_manager):
        """Test is_symbol_enabled for invalid symbols."""
        assert initialized_manager.is_symbol_enabled("INVALID") is False
        assert initialized_manager.is_symbol_enabled("NOTFOUND") is False

    def test_add_symbol_new_valid(self, initialized_manager):
        """Test adding a new valid symbol (already in config but testing method)."""
        # VEDL is already in the config, so this tests adding an already-present symbol
        result = initialized_manager.add_symbol("VEDL")
        assert result is True  # VEDL is in master and already in config
        assert "VEDL" in initialized_manager.get_all_symbols()

    def test_add_duplicate_symbol(self, initialized_manager):
        """Test adding a duplicate symbol doesn't increase count."""
        initial_count = len(initialized_manager.get_all_symbols())
        result = initialized_manager.add_symbol("RELIANCE")
        assert result is True
        assert len(initialized_manager.get_all_symbols()) == initial_count

    def test_add_symbol_not_in_master(self, initialized_manager):
        """Test adding a symbol that doesn't exist in master."""
        result = initialized_manager.add_symbol("NOTFOUND")
        assert result is False  # Not in master
        # The symbol IS added to config but marked invalid
        assert "NOTFOUND" in initialized_manager.get_all_symbols()
        assert initialized_manager.is_symbol_enabled("NOTFOUND") is False

    def test_remove_symbol(self, initialized_manager):
        """Test removing a symbol from the universe."""
        assert initialized_manager.remove_symbol("RELIANCE") is True
        assert "RELIANCE" not in initialized_manager.get_all_symbols()
        assert initialized_manager.is_symbol_enabled("RELIANCE") is False

    def test_remove_nonexistent_symbol(self, initialized_manager):
        """Test removing a symbol that doesn't exist."""
        result = initialized_manager.remove_symbol("NOTEXIST")
        assert result is True  # No error
        # Should not crash

    def test_validated_contract_accessors_and_master_timestamp(self, initialized_manager):
        """Discovery can consume only contracts belonging to Phase 1-valid symbols."""
        all_contracts = initialized_manager.get_validated_contracts()
        reliance = initialized_manager.get_contracts_for_symbol("reliance")
        assert all_contracts
        assert len(reliance) == 10
        assert all(c.underlying == "RELIANCE" for c in reliance)
        assert len(all_contracts) == 20 * 10
        assert initialized_manager.get_contracts_for_symbol("NOTFOUND") == []
        assert initialized_manager.master_loaded_at is not None

    def test_uninitialized_warning(self, mock_rest):
        """Test that methods warn when called before initialize."""
        cfg = {"storage": {"instrument_cache_dir": "data/instruments"},
               "dhan": {"client_id": "test", "access_token": "test"},
               "risk": {"default_lot_size": 25}}
        mgr = UniverseManager(cfg=cfg)

        # Should not crash but should log warning
        with patch("market.universe_manager.log.warning") as mock_log:
            mgr.get_enabled_symbols()
            mock_log.assert_called()

    def test_get_validation_report(self, initialized_manager):
        """Test getting the validation report."""
        report = initialized_manager.get_validation_report()
        assert report is not None
        assert isinstance(report, UniverseValidationReport)
        assert report.total_configured == 20
        assert report.enabled_count == 20


class TestUniverseManagerIntegration:
    """Integration tests with mocked DhanREST and instrument master."""

    def test_full_initialization_flow(self, tmp_path, mock_rest):
        """Test the complete initialization flow with custom config."""
        config_path = tmp_path / "universe.json"
        config_data = {
            "universe": {
                "enabled": True,
                "symbols": ["RELIANCE", "ICICIBANK", "INVALID_SYM"]
            }
        }
        config_path.write_text(json.dumps(config_data))

        master_bundle = _create_mock_master(["RELIANCE", "ICICIBANK"])
        cfg = {"storage": {"instrument_cache_dir": "data/instruments"},
               "dhan": {"client_id": "test", "access_token": "test"},
               "risk": {"default_lot_size": 25}}

        with patch("market.universe_manager.load_master_bundle", return_value=master_bundle):
            with patch("market.universe_manager.DhanREST", return_value=mock_rest):
                mgr = UniverseManager(cfg=cfg)
                # Override the config path to use our temp file
                mgr._universe_cfg = config_data["universe"]
                report = mgr.initialize(rest=mock_rest)

        assert report.total_configured == 3
        assert report.enabled_count == 2
        assert set(report.valid_symbols) == {"RELIANCE", "ICICIBANK"}
        assert "INVALID_SYM" in report.invalid_symbols

    def test_disabled_universe(self, mock_rest):
        """Test that disabled universe returns empty valid list."""
        master_bundle = _create_mock_master(["RELIANCE"])
        cfg = {"storage": {"instrument_cache_dir": "data/instruments"},
               "dhan": {"client_id": "test", "access_token": "test"},
               "risk": {"default_lot_size": 25}}

        with patch("market.universe_manager.load_master_bundle", return_value=master_bundle):
            with patch("market.universe_manager.DhanREST", return_value=mock_rest):
                mgr = UniverseManager(cfg=cfg)
                mgr._universe_cfg = {"enabled": False, "symbols": ["RELIANCE"]}
                report = mgr.initialize(rest=mock_rest)

        assert report.enabled_count == 0
        assert report.valid_symbols == []
        assert report.invalid_symbols == []


class TestSymbolValidationResult:
    """Tests for SymbolValidationResult dataclass."""

    def test_valid_result(self):
        """Test creating a valid validation result."""
        result = SymbolValidationResult(
            symbol="RELIANCE",
            is_valid=True,
            has_contracts=True,
            contract_count=10
        )
        assert result.symbol == "RELIANCE"
        assert result.is_valid is True
        assert result.has_contracts is True
        assert result.contract_count == 10
        assert result.error is None

    def test_invalid_result_with_error(self):
        """Test creating an invalid validation result with error."""
        result = SymbolValidationResult(
            symbol="INVALID",
            is_valid=False,
            has_contracts=False,
            error="Not found in master"
        )
        assert result.symbol == "INVALID"
        assert result.is_valid is False
        assert result.has_contracts is False
        assert result.error == "Not found in master"


class TestUniverseValidationReport:
    """Tests for UniverseValidationReport dataclass."""

    def test_to_dict(self):
        """Test serialization to dictionary."""
        report = UniverseValidationReport(
            timestamp=date(2026, 9, 15),
            total_configured=3,
            enabled_count=2,
            valid_symbols=["RELIANCE", "ICICIBANK"],
            invalid_symbols=["INVALID"],
            missing_contracts=["INVALID"],
            validation_details=[
                SymbolValidationResult("RELIANCE", True, True, 10),
                SymbolValidationResult("ICICIBANK", True, True, 10),
                SymbolValidationResult("INVALID", False, False, 0, "Not found"),
            ]
        )
        d = report.to_dict()
        assert d["total_symbols"] == 3
        assert d["enabled_symbols"] == 2
        assert d["valid_symbols"] == ["RELIANCE", "ICICIBANK"]
        assert d["invalid_symbols"] == ["INVALID"]
        assert len(d["details"]) == 3


class TestUniverseManagerExport:
    """Tests for report export functionality."""

    @pytest.fixture
    def initialized_manager_full(self, mock_rest):
        """Create an initialized manager with all symbols in master."""
        master_bundle = _create_mock_master(ALL_UNIVERSE_SYMBOLS)
        cfg = {"storage": {"instrument_cache_dir": "data/instruments"},
               "dhan": {"client_id": "test", "access_token": "test"},
               "risk": {"default_lot_size": 25}}

        with patch("market.universe_manager.load_master_bundle", return_value=master_bundle):
            with patch("market.universe_manager.DhanREST", return_value=mock_rest):
                mgr = UniverseManager(cfg=cfg)
                mgr.initialize(rest=mock_rest)
                return mgr

    def test_export_report_json(self, initialized_manager_full, tmp_path):
        """Test exporting validation report to JSON file."""
        output_path = tmp_path / "universe_report.json"
        initialized_manager_full.export_report_json(str(output_path))

        assert output_path.exists()
        with open(output_path) as f:
            data = json.load(f)
        assert data["total_symbols"] == 20
        assert data["enabled_symbols"] == 20
        assert len(data["valid_symbols"]) == 20


# Helper fixtures
@pytest.fixture
def mock_rest():
    """Mock DhanREST client."""
    return Mock()


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v"])