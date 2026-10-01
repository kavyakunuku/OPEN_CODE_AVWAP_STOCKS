#!/usr/bin/env python3
"""Phase 8 entry point: paper trading only, using live Dhan data by default.

LIVE mode is deliberately hard-disabled in this phase. The CLI, environment,
configuration, and TraderApp all reject LIVE; the application has no live
order-placement route.

Examples:
    python main.py                    # paper trading with Dhan market data
    python main.py --demo             # paper + synthetic mock data (development)
    python -m paper_trading report    # regenerate reports without starting Dhan
"""
from __future__ import annotations

import argparse
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import get as cfg_get, load_config
from common.utils import setup_logging

LIVE_BLOCK_MESSAGE = (
    "LIVE trading is disabled for Phase 8. No live orders can be placed. "
    "Use trading_mode=PAPER; do not advance to Phase 9 without approval."
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 8 AVWAP option-selling paper trader (stock options only)"
    )
    parser.add_argument("--config", default="config/config.json", help="path to config JSON")
    parser.add_argument("--mode", choices=["PAPER", "LIVE"], default=None,
                        help="override mode; LIVE is rejected in Phase 8")
    parser.add_argument("--i-understand-live", action="store_true",
                        help="legacy compatibility flag; cannot enable LIVE in Phase 8")
    parser.add_argument("--demo", action="store_true",
                        help="development: use synthetic MOCK market data (no Dhan needed)")
    parser.add_argument("--stop-after", type=float, default=None,
                        help="stop the engine after N seconds (for testing)")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    cfg = load_config(args.config)
    if args.mode:
        cfg["trading_mode"] = args.mode

    # Refuse every LIVE route before credentials, dashboards, or app/broker
    # objects are constructed. The app constructor repeats this hard block.
    if str(cfg.get("trading_mode", "PAPER")).upper() != "PAPER":
        print(f"[phase 8] BLOCKED: {LIVE_BLOCK_MESSAGE}", file=sys.stderr)
        return 2
    cfg["trading_mode"] = "PAPER"

    if os.environ.get("DHAN_CLIENT_ID"):
        cfg.setdefault("dhan", {})["client_id"] = os.environ["DHAN_CLIENT_ID"]
    if os.environ.get("DHAN_ACCESS_TOKEN"):
        cfg.setdefault("dhan", {})["access_token"] = os.environ["DHAN_ACCESS_TOKEN"]

    if args.demo:
        cfg.setdefault("market_data", {})["source"] = "mock"
        print("[demo] using synthetic MOCK market data (development only)")

    setup_logging(
        cfg_get(cfg, "logging.file"),
        level=cfg_get(cfg, "logging.level", "INFO"),
    )
    print("=" * 68)
    print("PHASE 8 PAPER TRADING — no live orders are sent.")
    print(f"Market-data source: {cfg_get(cfg, 'market_data.source', 'dhan')}")
    if args.i_understand_live:
        print("[phase 8] --i-understand-live is ignored and cannot enable live orders.")
    if cfg_get(cfg, "market_data.source", "dhan") == "dhan":
        missing = [key for key in ("client_id", "access_token")
                   if not cfg_get(cfg, f"dhan.{key}")]
        if missing:
            print(f"ERROR: Dhan credentials not configured ({missing}).", file=sys.stderr)
            print("Provide them in config/config.json or via DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN.",
                  file=sys.stderr)
            print("Use --demo for a synthetic-data development run.", file=sys.stderr)
            return 2
    print("=" * 68)

    from app import TraderApp
    from dashboard.app import DashboardServer

    app = TraderApp(cfg)
    dashboard: DashboardServer | None = None
    if cfg_get(cfg, "dashboard.enabled", True):
        dashboard = DashboardServer(
            app,
            host=cfg_get(cfg, "dashboard.host", "0.0.0.0"),
            port=int(cfg_get(cfg, "dashboard.port", 9000)),
            control_token=cfg_get(cfg, "dashboard.control_token", ""),
        )
        dashboard.start()
        print(f"Dashboard: http://{cfg_get(cfg, 'dashboard.host', '0.0.0.0')}:{cfg_get(cfg, 'dashboard.port', 9000)}")

    def _signal_handler(signum, frame):
        print("\n[main] signal received, shutting down...")
        app.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        app.run(stop_after=args.stop_after)
    except KeyboardInterrupt:
        app.stop()
    finally:
        app.db.close()
    print("[main] shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
