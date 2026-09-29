#!/usr/bin/env python3
"""Calculate Day-to-Day P&L History including estimated charges."""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

# Typical Indian options charges (approximate map for Dhan/Upstox)
BROKERAGE_PER_ORDER = 20.0
STT_RATE_OPTIONS_SELL = 0.001  # 0.1% on options sell premium turnover (Oct 2024 revision)
EXCHANGE_TXN_CHARGE = 0.00035  # ~0.035% on premium turnover
GS_RATE = 0.18 # 18% GST on (Brokerage + Exchange)
STAMP_DUTY_RATE = 0.00003 # 0.003% on buy side premium
SEBI_TURNOVER = 0.000001 # 0.0001% on premium turnover

def estimate_charges(entry_price: float, exit_price: float, qty: int) -> float:
    # Option selling: Entry is Sell, Exit is Buy
    sell_premium = entry_price * qty
    buy_premium = exit_price * qty
    
    brokerage = BROKERAGE_PER_ORDER * 2 # Entry and Exit
    stt = sell_premium * STT_RATE_OPTIONS_SELL
    exc_txn = (sell_premium + buy_premium) * EXCHANGE_TXN_CHARGE
    sebi = (sell_premium + buy_premium) * SEBI_TURNOVER
    stamp = buy_premium * STAMP_DUTY_RATE
    gst = (brokerage + exc_txn + sebi) * GS_RATE
    
    total_charges = brokerage + stt + exc_txn + sebi + stamp + gst
    return total_charges

def ist_date(ts) -> str:
    from datetime import timezone, timedelta
    if not ts:
        return ""
    # IST is UTC+5:30
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.fromtimestamp(int(ts), ist).strftime("%Y-%m-%d")

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/trader.db", help="path to trader.db")
    p.add_argument("--out", default="data/exports/daily_pnl.csv", help="output csv path")
    args = p.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}")
        return 1
        
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = con.cursor()
    
    cur.execute("SELECT entry_price, exit_price, quantity, pnl, exit_time FROM positions WHERE status='CLOSED' ORDER BY exit_time")
    
    # Store aggregated data
    daily_stats = defaultdict(lambda: {"trades": 0, "gross_pnl": 0.0, "charges": 0.0, "net_pnl": 0.0})
    
    for row in cur:
        entry_price, exit_price, quantity, pnl, exit_time = row
        if exit_time is None:
            continue
        day = ist_date(exit_time)
        
        gross_pnl = pnl if pnl is not None else 0.0
        
        if entry_price and exit_price and quantity:
            charges = estimate_charges(entry_price, exit_price, quantity)
        else:
            charges = 0.0
            
        net_pnl = gross_pnl - charges
        
        daily_stats[day]["trades"] += 1
        daily_stats[day]["gross_pnl"] += gross_pnl
        daily_stats[day]["charges"] += charges
        daily_stats[day]["net_pnl"] += net_pnl
    
    con.close()
    
    total_gross = 0.0
    total_charges = 0.0
    total_net = 0.0
    
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Trades", "Gross_PnL", "Charges", "Net_PnL"])
        for day in sorted(daily_stats.keys()):
            stats = daily_stats[day]
            w.writerow([day, stats["trades"], round(stats["gross_pnl"], 2), round(stats["charges"], 2), round(stats["net_pnl"], 2)])
            
            total_gross += stats["gross_pnl"]
            total_charges += stats["charges"]
            total_net += stats["net_pnl"]
            
        # Add a total row
        w.writerow(["TOTAL", "", round(total_gross, 2), round(total_charges, 2), round(total_net, 2)])
        
    print(f"Exported Daily P&L History to {args.out}")
    print(f"Total Gross: {total_gross:.2f} | Total Charges: {total_charges:.2f} | Total Net: {total_net:.2f}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
