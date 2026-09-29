#!/usr/bin/env python3
"""Entry point for the AVWAP F&O option-selling trading system (stock options only).

Default: PAPER mode (spec §36). LIVE mode is never the default and requires
explicit double confirmation:
    1. trading_mode = "LIVE" in config
    2. --i-understand-live flag (or env AVWAP_LIVE_CONFIRM=<phrase>)

Examples:
    python main.py                          # paper trading with Dhan data
    python main.py --demo                   # paper + synthetic mock data (dev)
    python main.py --mode LIVE --i-understand-live
"""
from __future__ import annotations

import argparse
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import get as cfg_get, load_config
from common.utils import setup_logging


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AVWAP F&O option-selling system (stock options)")
    p.add_argument("--config", default="config/config.json", help="path to config JSON")
    p.add_argument("--mode", choices=["PAPER", "LIVE"], default=None,
                   help="override trading_mode (LIVE requires --i-understand-live)")
    p.add_argument("--i-understand-live", action="store_true",
                   help="acknowledge that LIVE mode sends REAL orders to Dhan")
    p.add_argument("--demo", action="store_true",
                   help="development: use synthetic MOCK market data (no Dhan needed)")
    p.add_argument("--stop-after", type=float, default=None,
                   help="stop the engine after N seconds (for testing)")
    return p


def main() -> int:
    args = build_arg_parser().parse_args()
    cfg = load_config(args.config)

    # Environment overrides for Dhan credentials (kept as documented in the
    # NOTE message below; keeps secrets out of config.json and makes token
    # rotation a one-line env change).
    if os.environ.get("DHAN_CLIENT_ID"):
        cfg.setdefault("dhan", {})["client_id"] = os.environ["DHAN_CLIENT_ID"]
    if os.environ.get("DHAN_ACCESS_TOKEN"):
        cfg.setdefault("dhan", {})["access_token"] = os.environ["DHAN_ACCESS_TOKEN"]

    if args.mode:
        cfg["trading_mode"] = args.mode
    if args.demo:
        cfg["market_data"]["source"] = "mock"
        cfg.setdefault("logging", {})
        print("[demo] using synthetic MOCK market data (development only)")

    setup_logging(
        cfg_get(cfg, "logging.file"),
        level=cfg_get(cfg, "logging.level", "INFO"),
    )

    live = cfg["trading_mode"] == "LIVE"
    print("=" * 68)
    if live:
        print("!! LIVE TRADING ENABLED - REAL ORDERS MAY BE SENT TO DHAN !!")
        missing = [k for k in ("client_id", "access_token")
                   if not cfg_get(cfg, f"dhan.{k}")]
        if missing:
            print(f"!! ABORT: Dhan credentials missing: {missing}")
            return 2
        phrase = cfg_get(cfg, "live.confirm_phrase", "I_UNDERSTAND_LIVE_TRADING")
        confirmed = args.i_understand_live or os.environ.get("AVWAP_LIVE_CONFIRM") == phrase
        if not confirmed:
            print("!! ABORT: live mode requires explicit confirmation.")
            print(f"   Add --i-understand-live (or AVWAP_LIVE_CONFIRM={phrase})")
            print("   after verifying items 1-12 of the LIVE checklist in README.")
            return 2
        print("=" * 68)
        print("LIVE MODE CHECKLIST (verify before running):")
        print("  [ ] Dhan credentials correct (client_id, access_token)")
        print("  [ ] static-IP / API plan requirements satisfied")
        print("  [ ] instrument mapping verified (lot sizes from master)")
        print("  [ ] lot size per trade verified (risk.quantity_per_trade)")
        print("  [ ] monthly expiry selection verified (24th switch rule)")
        print("  [ ] ATM calculation verified against the chain")
        print("  [ ] AVWAP calculation verified (tests + paper runs)")
        print("  [ ] signal detection verified (one cross = one order)")
        print("  [ ] paper execution verified for N days")
        print("  [ ] position reconciliation verified (restart test)")
        print("  [ ] order status handling verified (partial/rejected)")
        print("  [ ] emergency exit tested")
        print("=" * 68)
    else:
        print("PAPER TRADING MODE - real market data, NO live orders.")
        if cfg.get("market_data", {}).get("source") == "dhan":
            missing = [k for k in ("client_id", "access_token")
                       if not cfg_get(cfg, f"dhan.{k}")]
            if missing:
                print(f"NOTE: Dhan credentials not configured ({missing}).")
                print("      Provide them in config/config.json or via")
                print("      DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN environment")
                print("      variables. Use --demo for a synthetic-data run.")
                return 2
        print("=" * 68)

    from app import TraderApp
    from dashboard.app import DashboardServer

    app = TraderApp(cfg, live_confirmed=(cfg["trading_mode"] == "LIVE"))

    dash: DashboardServer | None = None
    if cfg_get(cfg, "dashboard.enabled", True):
        dash = DashboardServer(
            app,
            host=cfg_get(cfg, "dashboard.host", "0.0.0.0"),
            port=int(cfg_get(cfg, "dashboard.port", 9000)),
            control_token=cfg_get(cfg, "dashboard.control_token", ""),
        )
        dash.start()
        print(f"Dashboard: http://{cfg_get(cfg, 'dashboard.host', '0.0.0.0')}:{cfg_get(cfg, 'dashboard.port', 9000)}")

    def _sig(signum, frame):
        print("\n[main] signal received, shutting down...")
        app.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

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
