from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from common.models import OptionContract
from market.universe_manager import UniverseManager


def test_phase1_validation_can_reuse_app_loaded_master_snapshot():
    manager = UniverseManager(cfg={"dhan": {}, "storage": {}, "risk": {}})
    manager._universe_cfg = {"enabled": True, "symbols": ["RELIANCE", "MISSING"]}
    captured_at = datetime(2026, 9, 30, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    contracts = [
        OptionContract(
            security_id=f"R_{strike}_{side}",
            symbol=f"RELIANCE {strike} {side}",
            underlying="RELIANCE",
            strike=float(strike),
            option_type=side,
            expiry="2026-10-29",
            lot_size=250,
        )
        for strike in (2900, 3000) for side in ("CE", "PE")
    ]

    report = manager.initialize_from_master_bundle(
        SimpleNamespace(contracts=contracts),
        captured_at=captured_at,
    )

    assert manager.master_loaded_at == captured_at
    assert manager.get_enabled_symbols() == ["RELIANCE"]
    assert report.valid_symbols == ["RELIANCE"]
    assert report.invalid_symbols == ["MISSING"]
