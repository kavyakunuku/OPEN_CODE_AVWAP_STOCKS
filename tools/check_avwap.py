"""AVWAP validator (READ-ONLY).

Compares a contract's STORED AVWAP state (what the dashboard shows) against a
FRESH Dhan fetch of month-start -> now history, and prints both price
conventions:
  * typical = (H+L+C)/3  <- what THIS SYSTEM uses (standard VWAP)
  * close   = C          <- what many charting tools' "Anchored VWAP" use

If your chart's anchored VWAP differs a lot from the stored value, this tool
shows why: it prints the fresh anchor date, candle count, total volume, the
full-history VWAP, and the 5-day-window VWAP (what a failed/fallback history
fetch would produce).

Usage (on the machine with working Dhan credentials):
    python tools\\check_avwap.py                  # list all stored AVWAP states
    python tools\\check_avwap.py BAJFINANCE 970   # deep-dive (symbol match)
    python tools\\check_avwap.py --security 123   # deep-dive by security id
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from _common import (IST, db_path, dhan_creds, instrument_for, ist, load_config,
                     month_start, total_volume, vwap_of)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("match", nargs="*", help="symbol substring(s) to deep-dive, e.g. BAJFINANCE 970")
    ap.add_argument("--security", help="security id to deep-dive directly")
    ap.add_argument("--no-fetch", action="store_true", help="list mode only (no Dhan calls)")
    args = ap.parse_args()

    cfg = load_config()
    from storage.database import Database
    db = Database(db_path(cfg))

    partial = set(db.kv_get("avwap_partial_history", []) or [])
    rows = db._query("SELECT * FROM avwap_state ORDER BY anchor_ts")

    print(f"DB: {db_path(cfg)}")
    print(f"{'symbol':<32} {'security':>10} {'anchor (IST)':<20} {'cum vol':>12} {'last candle (IST)':<20} {'AVWAP':>9}  flag")
    print("-" * 118)
    for r in rows:
        sym = r["symbol"] or "-"
        print(f"{str(sym)[:32]:<32} {r['security_id']:>10} {ist(r['anchor_ts']):<20} "
              f"{float(r['cumulative_volume'] or 0):>12.0f} {ist(r['last_candle_ts']):<20} "
              f"{(r['last_avwap'] or 0):>9.4f}  {'PARTIAL!' if r['security_id'] in partial else ''}")

    if args.no_fetch or (not args.match and not args.security):
        print("\n(list mode only - pass a symbol match or --security to run the Dhan comparison)")
        return

    # ---------------- deep dive ----------------
    client_id, token, base = dhan_creds(cfg)
    if not client_id or not token:
        print("\nERROR: Dhan client_id/access_token not found (config.json or DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN env).")
        sys.exit(1)

    from dhan.client import DhanREST
    from dhan.market_data import DhanMarketData
    rest = DhanREST(client_id, token, base)
    md = DhanMarketData(rest)

    targets = []
    if args.security:
        sym = next((r["symbol"] for r in rows if str(r["security_id"]) == str(args.security)), "unknown")
        targets.append((str(args.security), sym))
    else:
        want = [w.upper() for w in args.match]
        for r in rows:
            sym = (r["symbol"] or "").upper()
            if all(w in sym for w in want):
                targets.append((str(r["security_id"]), r["symbol"]))
    if not targets:
        print(f"\nNo stored AVWAP state matches {args.match or args.security}")
        return

    now = datetime.now(IST)
    from_dt = month_start(now)
    print(f"\nFetching month-start ({from_dt:%Y-%m-%d}) -> now from Dhan...")
    for sec, sym in targets:
        instr = instrument_for(sym)
        try:
            candles = md.intraday_candles(sec, from_dt, now, 15, instrument=instr)
        except Exception as e:
            print(f"  FETCH FAILED for {sym} ({sec}): {e}")
            continue
        candles = sorted(candles, key=lambda c: c.ts)
        stored = db.get_avwap_state(sec)
        five_day = candles[-5 * 25:]  # ~5 trading days of 25 candles (approximate window)

        print(f"\n=== {sym}  (security {sec}, {instr}) ===")
        print(f"  fresh fetch : {len(candles)} candles, first {ist(candles[0].ts) if candles else '-'}, last {ist(candles[-1].ts) if candles else '-'}")
        print(f"  fresh volume: {total_volume(candles):.0f} total")
        print(f"  fresh VWAP (typical, this system's formula): {vwap_of(candles, 'typical')}")
        print(f"  fresh VWAP (close price, chart style)      : {vwap_of(candles, 'close')}")
        print(f"  5-day-window VWAP (typical) [fallback sim]: {vwap_of(five_day, 'typical')}")
        if stored is not None:
            print(f"  STORED      : anchor {ist(stored['anchor_ts'])}, cum_vol {float(stored['cumulative_volume'] or 0):.0f}, "
                  f"last_candle {ist(stored['last_candle_ts'])}, AVWAP {stored['last_avwap']}")
            if r0 := stored:
                gap_days = (int(candles[0].ts) - int(r0["anchor_ts"] or 0)) / 86400 if r0["anchor_ts"] and candles else None
                if gap_days is not None and gap_days > 2:
                    print(f"  >>> stored anchor is {gap_days:.1f} days LATER than the fresh history's first candle")
                    print(f"  >>> likely cause: the month-start fetch failed during bootstrap (429) and the")
                    print(f"      silent 5-day fallback anchored the AVWAP late. Repair with:")
                    print(f"          python tools\\reanchor_avwap.py {sym}")
                elif abs(float(stored["last_avwap"] or 0) - (vwap_of(candles, 'typical') or 0)) > 0.02 * abs(vwap_of(candles, 'typical') or 1):
                    print("  >>> stored AVWAP differs from the fresh full-history computation - investigate volume data")
                else:
                    print("  >>> stored state is consistent with a full month-start anchor (good)")
        else:
            print("  STORED      : (no state - contract was never initialized)")


if __name__ == "__main__":
    main()
