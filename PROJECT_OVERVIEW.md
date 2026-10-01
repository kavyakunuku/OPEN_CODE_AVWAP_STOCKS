# AVWAP F&O Option-Selling System (Stock Options Only) — Project Overview

**Status:** Phase 8 paper-trading implementation · Python 3.10+ · default and only app mode is PAPER · live order placement is hard-disabled pending Phase 9 approval.

This overview documents the repository's current operating mode. It does not authorize live trading or claim that any paper results are empirical performance evidence. The app uses real Dhan market observations when configured for Dhan; it has not placed live orders.

---

## 1. What the system does

The system scans NSE F&O stock options using Dhan market data, then evaluates the existing AVWAP strategy on **completed 15-minute option candles**. It sells options only to open a paper short and buys only to close an existing paper short. There is no midpoint fill assumption and no live order-placement path available in Phase 8.

Core principles:

- Preserve the existing AVWAP formula, contract-specific anchor, 15-minute option-candle basis, entry/exit conditions, mechanical exit, and position logic.
- Use timestamped observations; missing quote, depth, IV, or history remains missing rather than being synthesized.
- Re-rank new-entry opportunities during the session without automatically replacing, switching, or closing an active position.
- Persist paper execution, scanner decisions, positions, marks, errors, and daily summaries so the audit trail survives restart.
- Keep Phase 8 paper-only. Live order placement is deferred to a separately approved Phase 9.

## 2. Strategy and universe

### Signals (unchanged)

Entry is a true cross below AVWAP, evaluated only after the option candle is complete:

```text
previous_close >= previous_avwap AND current_close < current_avwap  -> SELL to open
```

Exit is a completed option candle close above AVWAP:

```text
current_close > current_avwap  -> BUY to close
```

Already-below-AVWAP is not a new entry; a new entry requires a fresh cross. The strategy uses no additional indicators or inferred intrabar conditions. The existing 15:15 mechanical exit remains in place. Failed paper exits are retried while the market session remains open; a position is not marked closed without an audited fill price.

### Opportunity scanner

The default Dhan scanner starts from the validated **20-stock Phase 1 universe** and passes candidates through contract discovery, liquidity qualification, volatility analysis, and opportunity ranking. Current Dhan option-chain/book observations are timestamped snapshots, not historical archives. Underlying equity history is fetched as real NSE_EQ candles; option AVWAP remains based on option candles.

Scanner rankings refresh on the configured cadence (15 minutes by default). Each run and discovered contract's qualification/rejection/selection decision is persisted. A failed or incomplete scan fails closed for new entries. Active-position contracts remain in the monitoring universe even if their underlying or contract leaves the ranking. Re-ranking affects new-entry eligibility only.

The scanner creates no AVWAP signals and makes no position changes. The existing signal engine remains the sole source of strategy entries and exits. Mock mode remains available for demonstration/tests, but it has no authentic option-chain book and does not fabricate scanner observations.

### AVWAP integrity

AVWAP remains per contract, cumulative across days, anchored to that contract's existing first tradable candle, and calculated from the existing typical-price/volume convention. Completed candles are persisted and duplicate processing is prevented across restarts. Existing partial-history flags and repair tools remain in use; the Phase 8 work does not redefine the AVWAP formula or anchor.

## 3. Paper execution assumptions

The persistent `TraderApp` requires `paper.fill_mode=marketable_quote`:

- With a fresh, timestamped two-sided option quote (maximum age configured, 60 seconds by default), a paper SELL crosses at the observed best bid and a paper BUY crosses at the observed best ask. **No midpoint fill is used.**
- Configured adverse slippage is applied per side and rounded against the paper trader to the contract tick size (5 bps per side by default).
- If a strategy signal has no fresh two-sided book, the current configured fallback is signal-candle close plus adverse slippage. The method and quote age are recorded; absent bid/ask is left NULL. If there is no valid signal close, execution is rejected rather than guessed.
- Manual/mechanical exits may use a fresh timestamped LTP only as an explicitly labeled fallback when a valid two-sided book is unavailable. A forced exit may otherwise use only the explicitly labeled same-day last completed option-candle close. If neither observation is usable, the paper position remains open and the exit retries; no guessed fill is created.
- Configured per-order and premium-turnover costs default to zero. No unconfigured fee/tax is silently invented. Margin is an indicative configurable percentage of underlying notional (20% default), not an official Dhan/SPAN estimate.

Every simulated fill records its signal and execution timestamps/prices, bid/ask/spread when observed, quote timestamp/age, fill method, slippage, configured costs, quantity, lot size, AVWAP, contract, and margin estimate. Open/closed positions, entry/exit details and gross P&L persist in SQLite; reports also calculate net P&L after configured costs.

## 4. Persistence, reports, and recovery

SQLite stores paper execution events, scanner runs and contract decisions, timestamped position marks, error/recovery events, and report summaries in addition to the existing positions, signals, candles, AVWAP state, and journal. Startup recovery reconstructs a missing audit row from a persisted position only with quote fields left NULL and the recovery method explicitly labeled. It never invents market data or replaces an active position.

Daily reports include stocks/contracts scanned, qualified and rejected contracts, signals, paper trades, open and closed positions, gross/net P&L, costs, slippage, indicative margin, marks and coverage, scanner decisions, assumptions, and errors. Daily JSON/Markdown/CSV reports are written under `reports/paper/YYYY-MM-DD/`. Multi-day exports include `trade_log.csv`, `position_log.csv`, `paper_error_log.csv`, and `daily_index.csv`. Stale position marks remain timestamped but are not reported as current unrealized P&L.

Build/rebuild reports without starting the trading loop:

```bash
python -m paper_trading report                  # stored days plus today
python -m paper_trading report --date 2026-09-30 # one day
```

The dashboard serves a read-only summary at `/api/paper-report?date=YYYY-MM-DD` and provides token-gated paper-only position controls. Dashboard controls do not change the strategy or scanner ranking.

## 5. Architecture (key modules)

| Module | Responsibility |
|---|---|
| `main.py` | CLI/config entry point; rejects LIVE before app construction. |
| `app.py` | Dhan market-data wiring, scanner refresh, unchanged AVWAP strategy orchestration, paper execution, restart/report hooks. |
| `market/opportunity_runtime.py` | Joins timestamped market inputs to discovery, liquidity, volatility, and ranking; cannot create signals or orders. |
| `execution/paper.py` | Quote-aware simulated fills, explicit fallbacks, transaction costs, indicative margin, and paper-audit recovery. |
| `execution/live.py` | Dormant legacy module; `DhanBroker` construction always raises in Phase 8. |
| `dhan/orders.py` | Legacy read adapter; `DhanOrders.place` raises before any order-placement REST request. |
| `storage/database.py` | SQLite migrations and persistence for scanner, paper events, marks, errors, and reports. |
| `paper_trading/reports.py` | Daily JSON/Markdown/CSV and multi-day trade/position/error exports. |
| `dashboard/app.py` | Paper-only UI and read-only report API. |

## 6. Safety boundary: no live orders

`TraderApp` rejects any non-PAPER mode before storage, feed, or broker construction. The CLI also rejects `--mode LIVE`, a LIVE config value, or `TRADING_MODE=LIVE` before it constructs the app. The legacy `--i-understand-live` flag cannot override this restriction. The app does not instantiate `DhanOrders` or `DhanBroker`; both legacy order-placement entry points are independently hard-disabled as defense in depth. Phase 8 does not submit, poll, or reconcile live orders/positions. Live order placement remains out of scope until Phase 9 is separately approved.

## 7. Run and validate

```bash
cp config/config.example.json config/config.json
# Set dhan.client_id and dhan.access_token (or DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN)
python main.py                  # PAPER on live Dhan market data; no orders sent
python main.py --demo           # mock demonstration; scanner has no real chain data
python main.py --mode LIVE      # rejected before app construction
```

Validation commands:

```bash
python -m py_compile app.py main.py dashboard/app.py
.venv/bin/pytest -q
```

The focused Phase 8 tests cover marketable quote fills, adverse slippage, explicit fallbacks, stale quote/mark handling, reports, restart recovery, scanner persistence, manual close, and hard blocks on LIVE/order placement. The full regression suite also covers existing AVWAP/candle/strategy behavior and scanner ranking/eligibility: **192 passed on 2026-09-30**.
