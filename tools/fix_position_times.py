"""One-time repair of PAPER position timestamps (entry_time / exit_time).

Background
----------
Earlier builds stamped positions with the WALL-CLOCK moment the signal was
processed (signal.created_at) instead of the candle's close time
(signal.candle_ts). During startup catch-up, many historical candles are
processed within the same second, so trades ended up with identical or
misleading entry/exit times.

New code stores candle_ts, so only rows written BEFORE the fix need repair.
This tool remaps each PAPER position's times from the signals table: a paper
fill at slippage 0 equals the signal candle's close exactly, so
(entry_price == signal_price) identifies the originating candle.

Usage
-----
    python tools/fix_position_times.py            # dry run (prints only)
    python tools/fix_position_times.py --apply    # write the fix to the DB

Only mode='PAPER' rows are touched. Rows with no exact price match are
skipped (printed), never guessed.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import get as cfg_get  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402

TOL = 1e-6  # price match tolerance (paper fill == signal close at slippage 0)


def _fmt(ts):
    if not ts:
        return "-"
    import datetime
    # display in IST (user timezone)
    return (datetime.datetime.fromtimestamp(ts, datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
            .strftime("%Y-%m-%d %H:%M:%S"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.json"))
    ap.add_argument("--apply", action="store_true", help="actually write the changes")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    db_path = cfg_get(cfg, "storage.db_path", "data/trader.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), db_path)
    print(f"DB: {db_path}  (apply={args.apply})\n")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    signals = con.execute("SELECT security_id, action, candle_ts, signal_price FROM signals").fetchall()
    by_sec = defaultdict(lambda: {"ENTRY_SELL": [], "EXIT_BUY": []})
    for s in signals:
        if s["action"] in by_sec[s["security_id"]]:
            by_sec[s["security_id"]][s["action"]].append(s)

    positions = con.execute("SELECT * FROM positions WHERE mode='PAPER'").fetchall()

    fixed = 0
    print(f"{'position':<42} {'field':<8} {'old (IST)':<21} {'new (IST)':<21}")
    print("-" * 100)
    for p in positions:
        sec = p["security_id"]
        cands_e = by_sec[sec]["ENTRY_SELL"]
        cands_x = by_sec[sec]["EXIT_BUY"]

        # ---- entry
        if p["entry_time"]:
            matches = [s for s in cands_e if abs(s["signal_price"] - (p["entry_price"] or -1)) <= TOL]
            if len(matches) == 1:
                new_ts = matches[0]["candle_ts"]
            elif len(matches) > 1:
                before = [s for s in matches if s["candle_ts"] <= p["entry_time"]]
                pool = before or matches
                new_ts = max(pool, key=lambda s: s["candle_ts"])["candle_ts"]
            else:
                print(f"{p['symbol'][:40]:<42} {'entry':<8} {_fmt(p['entry_time']):<21} {'NO MATCH - skipped':<21}")
                new_ts = None
            if new_ts is not None and new_ts != p["entry_time"]:
                print(f"{p['symbol'][:40]:<42} {'entry':<8} {_fmt(p['entry_time']):<21} {_fmt(new_ts):<21}")
                if args.apply:
                    con.execute("UPDATE positions SET entry_time=? WHERE position_id=?", (new_ts, p["position_id"]))
                fixed += 1

        # ---- exit (signal-driven exits only; forced exits have no signal)
        if p["status"] == "CLOSED" and p["exit_time"] and p["exit_reason"] == "CLOSE_ABOVE_AVWAP":
            matches = [s for s in cands_x if abs(s["signal_price"] - (p["exit_price"] or -1)) <= TOL]
            if len(matches) == 1:
                new_ts = matches[0]["candle_ts"]
            elif len(matches) > 1:
                before = [s for s in matches if s["candle_ts"] <= p["exit_time"]]
                pool = before or matches
                new_ts = max(pool, key=lambda s: s["candle_ts"])["candle_ts"]
            else:
                print(f"{p['symbol'][:40]:<42} {'exit':<8} {_fmt(p['exit_time']):<21} {'NO MATCH - skipped':<21}")
                new_ts = None
            if new_ts is not None and new_ts != p["exit_time"]:
                print(f"{p['symbol'][:40]:<42} {'exit':<8} {_fmt(p['exit_time']):<21} {_fmt(new_ts):<21}")
                if args.apply:
                    con.execute("UPDATE positions SET exit_time=? WHERE position_id=?", (new_ts, p["position_id"]))
                fixed += 1

    if args.apply:
        con.commit()
        print(f"\nAPPLIED: {fixed} timestamp(s) updated.")
    else:
        print(f"\nDRY RUN: {fixed} timestamp(s) would be updated. Re-run with --apply to write.")
    con.close()


if __name__ == "__main__":
    main()
