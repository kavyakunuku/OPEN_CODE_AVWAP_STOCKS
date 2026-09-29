"""AVWAP re-anchor: rebuild a contract's AVWAP state from the FULL
month-start -> now Dhan history.

Use this for contracts flagged PARTIAL (their month-start history fetch
failed during bootstrap and the AVWAP was silently anchored to a late 5-day
window, making it disagree with a chart's anchored VWAP).

A late-anchored state cannot be "merged" forward - the state only accumulates
candles NEWER than its last one - so the fix is: delete the state and
re-initialize from the complete history.

Usage (on the machine with working Dhan credentials; STOP the app first):
    python tools\\reanchor_avwap.py --all            # every PARTIAL-flagged contract
    python tools\\reanchor_avwap.py BAJFINANCE 970   # symbol match (like check_avwap)

Prints a before/after for each contract. Does not touch signals/positions.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from _common import (IST, db_path, dhan_creds, instrument_for, ist, load_config,
                     month_start, total_volume, vwap_of)

PARTIAL_KEY = "avwap_partial_history"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("match", nargs="*", help="symbol substring(s), e.g. BAJFINANCE 970")
    ap.add_argument("--all", action="store_true", help="re-anchor every PARTIAL-flagged contract")
    ap.add_argument("--dry", action="store_true", help="fetch + compute, but do not write")
    args = ap.parse_args()

    cfg = load_config()
    from storage.database import Database
    db = Database(db_path(cfg))
    from strategy.avwap import AvwapStore
    store = AvwapStore(db)

    partial = set(db.kv_get(PARTIAL_KEY, []) or [])
    rows = db._query("SELECT * FROM avwap_state")

    targets = []
    if args.all:
        for r in rows:
            if r["security_id"] in partial:
                targets.append((str(r["security_id"]), r["symbol"]))
        if not targets:
            print("No PARTIAL-flagged contracts in the DB - nothing to do.")
            return
    elif args.match:
        want = [w.upper() for w in args.match]
        for r in rows:
            sym = (r["symbol"] or "").upper()
            if all(w in sym for w in want):
                targets.append((str(r["security_id"]), r["symbol"]))
        if not targets:
            print(f"No stored AVWAP state matches {args.match}")
            return
    else:
        ap.print_help()
        sys.exit(1)

    client_id, token, base = dhan_creds(cfg)
    if not client_id or not token:
        print("ERROR: Dhan client_id/access_token not found (config.json or env).")
        sys.exit(1)

    from dhan.client import DhanREST
    from dhan.market_data import DhanMarketData
    rest = DhanREST(client_id, token, base)
    md = DhanMarketData(rest)
    now = datetime.now(IST)
    from_dt = month_start(now)
    gap = float(cfg.get("market_data", {}).get("history_request_gap_seconds", 1.0))

    print(f"Re-anchoring {len(targets)} contract(s) from {from_dt:%Y-%m-%d} -> now "
          f"({'DRY RUN' if args.dry else 'writing'})")
    ok = 0
    for i, (sec, sym) in enumerate(targets):
        if i > 0 and gap > 0:
            time.sleep(gap)
        instr = instrument_for(sym or "")
        old = db.get_avwap_state(sec)
        try:
            candles = md.intraday_candles(sec, from_dt, now, 15, instrument=instr)
        except Exception as e:
            print(f"  SKIP {sym or sec}: fetch failed ({e})")
            continue
        candles = sorted(candles, key=lambda c: c.ts)
        if not candles:
            print(f"  SKIP {sym or sec}: Dhan returned no history at all")
            continue
        fresh_typ = vwap_of(candles, "typical")
        print(f"\n=== {sym or sec} ({sec}) ===")
        if old is not None:
            print(f"  before : anchor {ist(old['anchor_ts'])}, cum_vol {float(old['cumulative_volume'] or 0):.0f}, avwap {old['last_avwap']}")
        print(f"  fresh  : {len(candles)} candles from {ist(candles[0].ts)}, vol {total_volume(candles):.0f}, avwap(typical) {fresh_typ:.4f}")
        if args.dry:
            print("  (dry run - not written)")
            continue
        store.delete(sec)
        store.initialize_from_candles(sec, candles, symbol=sym)
        new = db.get_avwap_state(sec)
        partial.discard(sec)
        db.kv_set(PARTIAL_KEY, sorted(partial))
        ok += 1
        print(f"  after  : anchor {ist(new['anchor_ts'])}, cum_vol {float(new['cumulative_volume'] or 0):.0f}, avwap {new['last_avwap']:.4f}")

    if args.dry:
        print("\nDRY RUN complete - re-run without --dry to apply.")
    else:
        print(f"\nDone: {ok} contract(s) re-anchored. {len(partial)} still flagged PARTIAL.")
        print("Start the app normally - the repaired states are persisted and will be")
        print("continued (not re-anchored) from here.")


if __name__ == "__main__":
    main()
