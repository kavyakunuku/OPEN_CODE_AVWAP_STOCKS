#!/usr/bin/env python3
"""Export the trader's SQLite database to human-readable CSV files.

Usage:
    python tools/export_to_csv.py              # exports to data/exports/
    python tools/export_to_csv.py --db path    # explicit DB path

Writes (one CSV each, openable in Excel):
    candles.csv       - every completed 15-min candle fetched, with the
                        AVWAP value computed at that candle
    avwap_state.csv   - current AVWAP state per contract (anchor, cumulative
                        volume, last AVWAP/close)
    signals.csv       - every ENTRY_SELL / EXIT_BUY signal ever emitted
    positions.csv     - open + closed positions with P&L
    orders.csv        - order records (incl. raw Dhan responses)
    journal.csv       - full audit trail

All timestamps are rendered as IST strings (plus the raw epoch column).
This script is READ-ONLY - it never modifies the database.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.utils import IST  # noqa: E402


def ist(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), IST).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def export_table(cur, query: str, path: str, ts_cols: tuple = ()) -> int:
    cur.execute(query)
    cols = [d[0] for d in cur.description]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        header = list(cols)
        for c in ts_cols:
            header.append(f"{c}_ist")
        w.writerow(header)
        n = 0
        for row in cur:
            row = list(row)
            extra = [ist(row[cols.index(c)]) if c in ts_cols else "" for c in ts_cols]
            w.writerow(row + extra)
            n += 1
    return n


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/trader.db", help="path to trader.db")
    p.add_argument("--out", default=None, help="output dir (default: <db_dir>/exports)")
    args = p.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}")
        return 1
    out = args.out or os.path.join(os.path.dirname(args.db) or ".", "exports")
    os.makedirs(out, exist_ok=True)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = con.cursor()
    print(f"Exporting from {args.db} -> {out}/")

    counts = {
        "candles": export_table(
            cur,
            "SELECT security_id, ts, open, high, low, close, volume, avwap "
            "FROM candles ORDER BY ts, security_id",
            os.path.join(out, "candles.csv"), ("ts",),
        ),
        "avwap_state": export_table(
            cur,
            "SELECT security_id, anchor_ts, last_candle_ts, last_close, last_avwap, "
            "cumulative_volume, cumulative_price_volume FROM avwap_state ORDER BY security_id",
            os.path.join(out, "avwap_state.csv"), ("anchor_ts", "last_candle_ts"),
        ),
        "signals": export_table(
            cur,
            "SELECT signal_key, security_id, action, symbol, underlying, strike, "
            "option_type, expiry, candle_ts, signal_price, avwap, prev_close, "
            "prev_avwap, reason, created_at FROM signals ORDER BY candle_ts",
            os.path.join(out, "signals.csv"), ("candle_ts", "created_at"),
        ),
        "positions": export_table(
            cur,
            "SELECT position_id, security_id, symbol, underlying, strike, option_type, "
            "expiry, quantity, entry_price, entry_time, entry_avwap, entry_reason, "
            "exit_price, exit_time, exit_avwap, exit_reason, pnl, status, mode, source "
            "FROM positions ORDER BY entry_time",
            os.path.join(out, "positions.csv"), ("entry_time", "exit_time"),
        ),
        "orders": export_table(
            cur,
            "SELECT order_id, position_id, security_id, action, side, quantity, "
            "order_type, status, filled_qty, avg_price, placed_at, updated_at, raw "
            "FROM orders ORDER BY placed_at",
            os.path.join(out, "orders.csv"), ("placed_at", "updated_at"),
        ),
        "journal": export_table(
            cur,
            "SELECT id, ts, position_id, security_id, symbol, event, detail "
            "FROM journal ORDER BY ts",
            os.path.join(out, "journal.csv"), ("ts",),
        ),
    }
    con.close()

    for name, n in counts.items():
        print(f"  {name:<14s} {n:>8,} rows  -> {name}.csv")
    print(f"Total: {sum(counts.values()):,} rows exported. Open them in Excel "
          "(File > Open > select .csv).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
