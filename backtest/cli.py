"""Command-line entry point for the Phase 7 historical replay."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backtest.data import BacktestDataError, inspect_data, load_dataset
from backtest.engine import BacktestError, run_phase7


ALLOWED_CONFIG_SECTIONS = {
    "strategy", "risk", "liquidity", "volatility", "opportunity", "paper", "backtest",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backtest",
        description="Run the read-only Phase 7 AVWAP historical comparison.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="validate an archive without running a backtest")
    inspect.add_argument("--data", default="data/phase7", help="point-in-time archive directory")
    run = subparsers.add_parser("run", help="replay baseline and threshold-sweep systems")
    run.add_argument("--data", default="data/phase7", help="point-in-time archive directory")
    run.add_argument("--out", default="reports/phase7", help="report output directory")
    run.add_argument(
        "--config", default=None,
        help="optional JSON config; only strategy/risk/scanner/backtest settings are read",
    )
    return parser


def _config(path: str | None) -> dict:
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestError(f"cannot read JSON config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BacktestError("config JSON must be an object")
    # In particular, never load trading credentials or instantiate the live app.
    return {key: item for key, item in value.items() if key in ALLOWED_CONFIG_SECTIONS}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "inspect":
        initial = inspect_data(args.data)
        if initial["missing_files"]:
            print(json.dumps(initial, indent=2, sort_keys=True))
            return 2
        try:
            dataset = load_dataset(args.data)
        except BacktestDataError as exc:
            print(json.dumps({
                "status": "blocked_invalid_historical_data",
                "data_root": str(args.data),
                "error": str(exc),
                "empirical_results": None,
            }, indent=2, sort_keys=True))
            return 2
        print(json.dumps({
            "status": "ready_with_warnings" if dataset.quality.warnings else "ready",
            "data_root": str(args.data),
            "symbols": list(dataset.symbols),
            "period_start": dataset.start_at.isoformat() if dataset.start_at else None,
            "period_end": dataset.end_at.isoformat() if dataset.end_at else None,
            "data_quality": dataset.quality.to_dict(),
            "empirical_results": None,
        }, indent=2, sort_keys=True))
        return 0

    try:
        settings = _config(args.config)
        result = run_phase7(args.data, output_dir=args.out, config=settings)
    except (BacktestDataError, BacktestError, OSError) as exc:
        print(json.dumps({
            "status": "blocked",
            "data_root": str(args.data),
            "error": str(exc),
            "reports_written": False,
            "paper_trading_enabled": False,
            "live_trading_enabled": False,
        }, indent=2, sort_keys=True))
        return 2
    print(json.dumps({
        "status": "completed",
        "output_dir": str(args.out),
        "period": {
            "start": result.period_start.isoformat(),
            "end": result.period_end.isoformat(),
        },
        "lookahead_validation": result.lookahead_validation.get("status"),
        "baseline": result.to_dict()["baseline"],
        "threshold_scenarios": len(result.scenarios),
        "automatic_winner_declared": False,
        "paper_trading_enabled": False,
        "live_trading_enabled": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
