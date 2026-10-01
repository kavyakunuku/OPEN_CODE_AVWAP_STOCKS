"""Regenerate persistent paper reports without starting the trading engine."""
from __future__ import annotations

import argparse
import json

from common.config import load_config, get as cfg_get
from storage.database import Database
from paper_trading.reports import PaperReportWriter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m paper_trading")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="regenerate daily CSV/JSON paper reports")
    report.add_argument("--db", default=None, help="SQLite DB path (defaults to config storage.db_path)")
    report.add_argument("--out", default=None, help="report directory (defaults to config paper.report_dir)")
    report.add_argument("--date", default=None, help="optional YYYY-MM-DD; defaults to all stored dates and today")
    args = parser.parse_args(argv)
    cfg = load_config()
    db = Database(args.db or cfg_get(cfg, "storage.db_path", "data/trader.db"))
    try:
        writer = PaperReportWriter(
            db, args.out or cfg_get(cfg, "paper.report_dir", "reports/paper"),
            cfg_get(cfg, "paper", {}),
        )
        if args.date:
            reports = [writer.write_day(args.date)]
        else:
            reports = writer.write_existing_days(include_today=True)
        print(json.dumps({
            "status": "completed",
            "reports_written": len(reports),
            "report_dates": [item["summary"]["trading_date"] for item in reports],
            "paper_trading_only": True,
            "live_orders_sent": False,
        }, indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
