# AVWAP F&O Option-Selling System (Stock Options Only) — Project Overview

**Status:** Phase 4 volatility analysis implemented · Python 3.10+ · ~14.3k lines of Python (≈11.1k application/ops, ≈3.3k tests) plus ~49 KB of hand-rolled dashboard JavaScript · 151 automated tests, all passing · trading app remains in **PAPER** mode (real Dhan market data, no live orders)

---

## 1. What this system is

An automated, rule-based **option-selling** trading system for NSE F&O markets, built on the **DhanHQ v2 API** with a real-time web dashboard. It sells short-dated option contracts (CE and PE) around ATM when a completed 15-minute candle crosses **below a per-contract Anchored VWAP (AVWAP)**, and buys them back when a candle closes **above** that AVWAP.

Core design principles:

- **Faithful, minimal strategy** — exactly the specified rules, no extra indicators, filters, or "improvements". The first objective is faithful implementation; the second is paper observation.
- **PAPER mode is the default.** Live trading is opt-in, requires explicit double confirmation, and is gated behind a 12-point checklist.
- **Real data, real semantics** — live Dhan quotes, candles, option chains and (in live mode) real order/fill data. Fills are never assumed; every state transition SIGNAL → ORDER SENT → ACCEPTED → FILLED/REJECTED is recorded.
- **Resilience by construction** — per-instrument error isolation (one bad stock can't stop the others), loud failures (the app aborts rather than run blind on empty data), and full persistence so restarts never lose positions or AVWAP state.

---

## 2. The strategy (exact rules)

### 2.1 Instruments
- **Sell-to-open only** (short options). The only buy is a **buy-to-close** of an existing short.
- Universe: a **curated list of ~67 highly liquid NSE F&O stocks**.
- **Expiry legs per underlying:**
  - All stocks: **monthly expiry**, with the standard premium-preservation switch on the **24th** of the month
  - Expiry dates always come from the exchange/Dhan (live expiry lists), never assumed.

### 2.2 Scanner universe (what is monitored)
- Per underlying, per expiry leg: **ATM + 4 ITM strikes on each side** (configurable via `strategy.itm_strikes_per_side`).
  - 9 unique strikes, **10 monitored contracts** (ATM contributes both a CE and a PE).
  - Cheaper stocks have finer strike grids, pricier stocks coarser — the window is always computed from that leg's **real chain strikes**, never an assumed step.
- **ATM tracking follows the underlying's live price**, never option LTPs:
  - Stocks: NSE equity LTP via the quote API, 30 s cadence
  - Indices: option-chain spot, 30 s cadence (shared 3 s pacing)
- **Existing open positions keep being monitored** even after they fall outside the ATM±4 window (position universe is a permanent union).

### 2.3 Entry (short)
A **true cross below AVWAP on completed 15-minute candles only** (no intracandle evaluation):

```
prev_close >= prev_avwap  AND  current_close < current_avwap   →  SELL (open short)
```

- Already-below-AVWAP is **not** an entry; re-entry after an exit requires a **fresh** cross.
- Duplicate protection: each signal is persisted with a unique key `(security_id, candle_ts, action)` — re-delivered candles (reconnect, retry, restart) can never produce a second signal.

### 2.4 Exit (buy-to-close)

```
current_close > current_avwap   →  BUY TO CLOSE
```

- **No fixed stop-loss, no other indicator, no time-of-day rules.** One rule in, one rule out.

### 2.5 Risk limits (configurable, enforced before any entry)
`risk.quantity_per_trade`, `risk.max_open_positions`, `risk.max_trades_per_day`, `risk.max_daily_loss` — the risk module vetoes entries that would breach any limit.

---

## 3. The AVWAP engine (the heart of the system)

- **Per-contract state** — every option contract has its own cumulative AVWAP; there is no shared cumulative state across strikes/expiries/underlyings.
- **Anchor** = the **first tradable 15-minute candle of that specific contract** (monthly series list at the start of the month; weeklies when listed).
- **Cumulative across days** — never reset at day, week, or month boundaries; the only reset event is a contract's own creation/expiry.
- **Price convention:** typical price `(High + Low + Close) / 3` on volume-weighted 15-min candles.
- **Anchor integrity is monitored end-to-end:**
  - History fetches are **paced (1.0 s)** with **4 retries** to survive Dhan rate-limit (429) storms.
  - If the month-start window can't be served, the app logs a loud ERROR, falls back to a shorter window, and raises a **permanent PARTIAL flag** (`avwap_partial_history`) shown in red on the dashboard ("AVWAP since" column).
  - Ops tools validate and repair late anchors: `tools/check_avwap.py` (read-only audit, compares stored vs freshly-fetched VWAP) and `tools/reanchor_avwap.py` (delete + full rebuild, `--all`/`--dry`).
- **Restart-safe:** anchor, cumulative value, and last-processed candle are persisted; already-consumed candles are never counted twice.

### Phase 4 — volatility analysis (standalone)

`market/volatility.py` reports daily ATR/ATR%, 15-minute ATR/ATR%, annualized
daily realized volatility, same-session 15-minute volatility, and per-option
movement/ATR/realized volatility where sufficient history is supplied. It uses
only completed bars at the requested timestamp; daily bars for the current
session are held back until 15:30, and daily bars synthesized from intraday
history require a full session. Missing history produces `None` and an explicit
insufficiency label.

The configurable score gives 40% to 15-minute ATR%, 30% to underlying realized
volatility, 20% to intraday volatility, and 10% to option movement. Component
scores saturate at configurable normalization targets. Unavailable components
contribute zero without redistributing their weights. Option series contribute
to the option-movement factor only when explicitly marked as Phase 3 liquidity
qualified. Volatility remains descriptive and cannot override the independent
Phase 3 liquidity gate; this engine adds no signals or trade approvals. IV is
reported only from timestamped source data and is never inferred.

---

## 4. Architecture

```
                         ┌────────────────────────────────────────────────┐
                         │                   main.py                      │
                         │  CLI, mode gating, env-var creds, signal wires │
                         └──────────────────────┬─────────────────────────┘
                                                ▼
┌──────────────┐   ┌────────────────────────────────────────────┐   ┌──────────────┐
│  dashboard/  │◄──┤                  app.py (TraderApp)        │──►│   storage/   │
│  Flask UI    │   │  bootstrap · main loop · wiring · payload  │   │ SQLite +     │
│  :9000       │   │  LTP/spot polling · position sync          │   │ journal      │
└──────────────┘   └───────┬──────────────┬──────────────┬──────┘   └──────────────┘
                           ▼              ▼              ▼
                    ┌────────────┐ ┌─────────────┐ ┌─────────────────────┐
                    │  market/   │ │  strategy/  │ │       execution/    │
                    │ universe · │ │ rules ·     │ │ broker (ABC) ·      │
                    │ candles    │ │ avwap ·     │ │ paper · live(Dhan)  │
                    └──────────── │ engine      │ │ reconcile (live)    │
                                   └─────────────┘ └─────────────────────┘
┌─────────────────────────────────────────────────────────────────────────────┐
│                        dhan/ — DhanHQ v2 API layer                          │
│  client (REST+auth+retries) · market_data (quotes/candles) · option_chain   │
│  (paced) · feed (candle-poll feed + mock feed) · instruments (master CSV)   │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Main loop (per tick, ~1 s):** process newly closed 15-min windows (each candle **persisted to the DB** as it is consumed) → engine evaluates rules → refresh universe/ATM (30 s cadence) → poll LTP + underlying spots (30 s cadence) → poll live order fills (3 s, live mode) → sync position universe. Every step is wrapped so one instrument's failure can't take down the loop; every outbound Dhan REST call updates the telemetry counters shown on the System Health page (requests, 429s, errors by path, last latency).

### Module map

| Path | Lines | Responsibility |
|---|---|---|
| `main.py` | 143 | Entry point. PAPER/LIVE gating (LIVE needs `trading_mode=LIVE` **and** `--i-understand-live`), `DHAN_CLIENT_ID`/`DHAN_ACCESS_TOKEN` env overrides, `--demo` mock mode. |
| `app.py` | 1590 | `TraderApp`: bootstrap (instrument master → per-underlying chains → AVWAP init), main loop, LTP/spot polling, universe refresh, position syncing, **per-candle persistence**, **REST telemetry**, dashboard payload builders (state / contract / journal), control actions (close-position, schedule-rebuild), fail-fast auth abort. |
| `strategy/rules.py` | 39 | **The only strategy rules** — entry cross + exit close. Nothing else may be added without an explicit strategy revision. |
| `strategy/avwap.py` | 156 | AVWAP accumulator: anchor, typical-price weighting, persistence, PARTIAL-history flags. |
| `strategy/engine.py` | 174 | `SignalEngine`: per-candle rule evaluation, signal dedup, dashboard status, last-signal tracking. |
| `market/universe.py` | 284 | Per-underlying, per-expiry-leg universe: ATM detection from real chain strikes, CE/PE ITM windows, scanner + position contract sets, ATM re-tracking. |
| `market/contract_discovery.py` | 758 | Phase 2 point-in-time contract catalog and NSE option discovery: reuses the monthly expiry selector, chooses ATM from the point-in-time strike grid, uses explicit strike-step metadata or actual local grid gaps, preserves instrument identity/birth date, and refuses future snapshots in historical mode. No scoring/signals/orders. |
| `market/liquidity.py` | 679 | Phase 3 per-option score and CE/PE-independent option-chain summary: comparable-contract percentiles, configurable weighted factors/floors, rejection codes, quote freshness, and as-of filtering of observations/completed bars. No AVWAP/signals/orders. |
| `market/volatility.py` | 755 | Phase 4 point-in-time daily/15-minute ATR, realized and intraday volatility, option movement/ATR/realized volatility, timestamped IV, and a capped configurable movement score. No strategy or trade approval. |
| `market/candles.py` | 172 | 15-min candle windowing (session-aware IST windows, boundary grace). |
| `dhan/client.py` | 141 | REST client: auth headers (`access-token`, `client-id`), JSON, retries with backoff on 429/5xx, typed `DhanAPIError`. |
| `dhan/market_data.py` | 220 | Quote API (batches ≤1000 ids, NSE_FNO / NSE_EQ) and intraday 15-min candles (5-yr depth) with pacing + retries; received quote timestamps use IST. |
| `dhan/option_chain.py` | 238 | Option chain + expiry list clients with a **shared 3 s pacer**; preserves capture time, OI change, quote sizes, and any supplied depth. |
| `dhan/liquidity.py` | 185 | Adapts Phase 2 contracts, captured chain snapshots, project candles/history, and optional timestamped Dhan quotes; does not backfill historical quote/depth data. |
| `dhan/volatility.py` | 167 | Adapts existing timestamped project candles, quotes, Phase 2 option identity, Phase 3-qualified IDs, and captured chain spot/LTP/IV; makes no network request or historical-data claim. |
| `dhan/feed.py` | 406 | `DhanCandlePollFeed` (production candle source: history fetch, closed-candle fetch, LTP poll) and `MockFeed` (deterministic synthetic market for `--demo` and tests). |
| `dhan/instruments.py` | 409 | Dhan scrip-master download/cache (12 h TTL): contract lookup, lot/tick sizes, optional contract birth date/strike-step metadata, underlying IDs, F&O universe extraction. |
| `dhan/orders.py` | 119 | Live order placement/status/positions (order polling, fill detection). |
| `execution/broker.py` | 44 | `Broker` ABC — the paper/live seam. Strategy logic is identical in both; only this layer differs. |
| `execution/paper.py` | 148 | Paper broker: fills against live LTP with configurable slippage (bps), order lifecycle, position book. |
| `execution/live.py` | 359 | Dhan live broker: limit orders with tick offset, order-status polling (SIGNAL→SENT→ACCEPTED→FILLED/REJECTED), startup **reconciliation** (DB ↔ Dhan positions, both directions). |
| `portfolio/positions.py` | 132 | Position lifecycle + persistence (open/close, status transitions, restart restore). |
| `portfolio/risk.py` | 89 | Pre-entry risk vetoes (max positions, trades/day, daily loss). |
| `portfolio/pnl.py` | 22 | Realized/unrealized P&L. |
| `storage/database.py` | 440 | SQLite (WAL): positions, signals (dedup keys), AVWAP state (+schema migrations), **persisted 15-min candles (system of record for the chart + AVWAP rebuilds)**, kv store, candle-processed markers. |
| `storage/journal.py` | 49 | Append-only audit journal (every signal, order, fill, reconcile, manual control action, structured startup timeline). |
| `common/models.py` | 146 | Dataclasses: `Candle`, contract-identity-rich `OptionContract`, `Quote`, `Signal`, action constants. |
| `common/utils.py` | 144 | IST time handling (ZoneInfo with fixed UTC+05:30 fallback for Windows), session state (PRE_OPEN/OPEN/CLOSED), candle-window math, logging setup. |
| `common/config.py` | 184 | Config loader + dotted-path access; includes configurable liquidity and volatility weights, targets, thresholds and lookback windows. |
| `dashboard/app.py` | 1281 | Flask dashboard (port 9000) + ~49 KB embedded vanilla JS. **12 pages** across two zones: *Live Trading* (Overview/Command Center, Scanner, Contract detail with canvas candlestick+AVWAP chart, Position Command Center, Signal Center with full explainability chain, Orders with raw Dhan responses) and *Data + Operations* (Data Health, System Health, Alert Center, Journal with filters + CSV/JSON export, Restart/Recovery, read-only Settings). REST: `/api/state`, `/api/contract/<sec>`, `/api/journal`, `/api/control` (gated by `dashboard.control_token`; actions: pause/resume, emergency stop/release, exit-all, **close-position**, **schedule-avwap-rebuild**). No CDN, no framework, no WebSocket. |
| `tools/check_avwap.py` | — | **Read-only AVWAP auditor**: lists anchor dates + PARTIAL flags; deep-dive per symbol (fresh full-history VWAP vs stored, 5-day-fallback simulation, diagnosis). |
| `tools/reanchor_avwap.py` | — | AVWAP repair: delete + full rebuild for late-anchored contracts (`--all`, symbol filter, `--dry`). |
| `tools/export_to_csv.py` / `fix_position_times.py` | — | Data export / maintenance utilities. |
| `tests/` | 3,278 | **151 tests**: existing strategy/AVWAP and recovery suites, Phase 2 contract discovery, Phase 3 liquidity scoring and history-integrity tests, plus Phase 4 ATR, realized/intraday volatility, option metrics/IV, insufficiency, as-of boundaries, and no-look-ahead coverage. |

---

## 5. Data & persistence

- **Database:** `data/trader.db` (SQLite, WAL mode) — the single source of truth:
  - `positions` — every order/position with full lifecycle
  - `signals` — deduplicated signal records (restart/reconnect safe)
  - `avwap_state` — per-contract anchor, cumulative AVWAP, last candle, PARTIAL flag
  - `candles` — **every processed 15-min candle, persisted at ingest** (the system of record for the contract chart and for AVWAP rebuilds — a chart is never drawn from in-memory state alone)
  - `journal` — append-only audit trail
  - `kv` — flags and markers (e.g. `avwap_partial_history`, the rebuild queue)
- **Instrument cache:** `data/instruments/` — Dhan scrip master (34.6 MB, 12 h TTL).
- **Restart semantics:** positions, AVWAP state, and journal all survive stop/start; the app re-attaches open positions to the monitoring universe on bootstrap ("restoring open positions" is step one) and resumes AVWAPs without double-counting candles.

## 6. Dhan API integration

| Endpoint | Use | Limits respected |
|---|---|---|
| `POST /v2/optionchain/expirylist` | Expiry legs per underlying | shared 3 s pacer |
| `POST /v2/optionchain` | Chain strikes + **index spot** + per-strike quotes | shared 3 s pacer |
| `POST /v2/marketfeed/quote` | LTP for ≤1000 option contracts (NSE_FNO) + stock spots (NSE_EQ) | 1 req/s, 1000 ids/req |
| Intraday candles (v2 historical) | 15-min history back **5 years** — AVWAP anchoring | 1.0 s pacing between history requests, 4 retries |
| Scrip master CSV | Contract/lot/tick/underlying reference | cached 12 h |
| Orders/positions endpoints | Live mode: place, poll, reconcile | 3 s fill polling |

- **Auth:** JWT access tokens are short-lived (days). The app **fails fast** on a 401 with an actionable message (generate new token → config or env var) instead of marching through the universe on failures, and **aborts bootstrap entirely** if every underlying fails (no running blind).
- **Credentials:** `config/config.json` (`dhan.client_id` / `dhan.access_token`) or `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` environment variables.

## 7. Safety model

1. **PAPER is the default** (`trading_mode: PAPER`). Strategy logic is byte-identical in paper and live — only the execution layer differs (`execution/paper.py` vs `execution/live.py`).
2. **LIVE requires double confirmation:** `trading_mode=LIVE` in config **and** `--i-understand-live` (or `AVWAP_LIVE_CONFIRM=<phrase>`), followed by a printed 12-point checklist.
3. **Live reconciliation on startup** (live mode): DB rows Dhan no longer holds → marked `CLOSED/RECONCILED`; shorts Dhan holds that the app doesn't know about → adopted.
4. **Error isolation per instrument** — one failing stock never stops the rest; the main loop logs and continues.
5. **Loud failures** — bootstrap aborts on total data failure; auth failures abort immediately; partial AVWAP history is permanently flagged and visible, never silent.
6. **Audit everything** — journal records signals, orders, fills, reconciliations, **manual dashboard control actions**, and the **structured startup timeline** (every bootstrap step, timestamped — the Restart/Recovery page renders it directly).
7. **Dashboard safety controls** — mode is always displayed (LIVE in red); pause/resume, emergency stop/release, and EXIT ALL (type-to-confirm) are the only runtime actions, all token-gated; strategy/risk/mode values can only be changed by editing the config file and restarting.

## 8. Dashboard (Flask, `http://localhost:9000`)

12 pages in two zones — **LIVE TRADING** and **DATA + OPERATIONS** — plus a persistent header (mode banner, market session, engine status, build). Single page, hash-free client-side navigation; state polled every 3 s; hand-rolled canvas charts, no CDN, no framework, no WebSocket.

**LIVE TRADING**
- **Overview / Command Center** — today's P&L, open positions x/max, signals today, orders today, wins/losses, exposure, unrealized; market-status bar with session timeline + underlying spots; system strip (last candle, next candle close, feed, DB, uptime).
- **Scanner** — every monitored contract: symbol/option, expiry + DTE, strike, CE/PE, LTP, last close, AVWAP, **AVWAP since** (red when history was partial), Δ vs AVWAP, trigger, volume, OI, last candle, status (WATCH/ENTRY/EXIT/OPEN/PENDING). Text filter + quick views (ALL / ACTIONABLE / OPEN / EXIT CANDIDATES / PARTIAL) + CE/PE + above/below-AVWAP filters; per-row ERROR state; row click → contract page.
- **Contract detail** — hand-rolled canvas candlestick chart with the AVWAP line, entry/exit markers and the AVWAP anchor marker (data = persisted candles); per-contract stats (AVWAP, typical price, Δ, volume, OI, anchor date, partial warning); the per-contract signal history with explainability; if a position is open → position card with **[CLOSE POSITION]**; if history was partial → **[SCHEDULE REBUILD]**.
- **Position Command Center** — portfolio totals (exposure, unrealized, realized today/all-time) + one card per open position (entry, current, P&L, duration, AVWAP, contract link, per-position **[CLOSE]**).
- **Signal Center** — signals as permanent events with the full **candle → signal → order → position** chain and explainability (which rule line fired, which risk checks passed/would veto).
- **Orders** — status table (SIGNAL → SENT → ACCEPTED → FILLED/REJECTED) with the **raw Dhan response** per order (expandable).

**DATA + OPERATIONS**
- **Data Health** — per-feed status (LTP / option chain / candles / master / REST) with last-ok, latency, error counts; AVWAP integrity summary (complete / partial / failed); the partial contracts with **[SCHEDULE REBUILD]** (queued in kv, applied at next start — never under the live engine).
- **System Health** — component status (engine, strategy, execution, database, dashboard), REST API stats (requests, 429s, errors by path), process CPU/RAM (optional `psutil`), heartbeats (engine, last candle, next close, polls, uptime).
- **Alert / Incident Center** — currently-computed alerts with severities + incident history derived from the journal.
- **Journal** — filterable by event / symbol / severity, full detail per row, **CSV and JSON export**.
- **Restart / Recovery** — structured, timestamped startup timeline (every bootstrap step with status), current recovery state.
- **Settings** — **read-only** display of the effective config; only a small safe subset is changeable at runtime (pause/resume, emergency stop, exit-all). Strategy/risk/execution/mode changes = edit `config/config.json` + restart (hard rule — the UI never edits strategy or risk values).

**Safety controls:** the header always shows the trading mode (LIVE in red); pause/resume entries, emergency stop/release, and EXIT ALL (type-to-confirm) are available from the header controls; every control action is token-gated (`dashboard.control_token`) and journaled.

## 9. Operations

```bash
pip install -r requirements.txt        # requests, Flask, psutil, tzdata(Windows); pytest for dev
# put Dhan client_id + access_token in config/config.json
# (or set DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN env vars)

python main.py                          # PAPER trading on live Dhan data
python main.py --demo                   # synthetic mock data (no Dhan needed)
python main.py --mode LIVE --i-understand-live   # live (after the checklist!)
```

| Task | Command |
|---|---|
| Audit AVWAP health (read-only) | `python tools\check_avwap.py` (or `… BAJFINANCE 970`, `--security <id>`) |
| Repair late-anchored AVWAPs | `python tools\reanchor_avwap.py --all` (or symbol filter, `--dry`) |
| Export data | `python tools\export_to_csv.py` |

**Runbook:** app may be stopped at any time (typical: post-market) and restarted pre-market — positions, AVWAP state, and journal persist in `data/`. Dhan tokens expire every few days: on a 401, generate a new token at dhan.co (Manage Access Token) and update config/env, then restart.

## 10. Testing & current status

- **151 automated tests**, all green: the existing AVWAP/candle/strategy, universe, risk, paper execution, recovery, and dashboard suites; Phase 2 point-in-time option discovery; Phase 3 Dhan liquidity adaptation, percentile/weight scoring, rejection codes, CE/PE independence and chain imbalance; and Phase 4 ATR/ATR%, rolling and intraday volatility, option movement/ATR/IV, insufficiency, completed-bar boundaries, and no-look-ahead checks.
- **Dashboard render-verified:** the served page is loaded in a headless DOM (jsdom) against payloads generated from a real mock `TraderApp`; all 12 pages render with zero JavaScript errors, and the scanner/quick-view/contract/close-button interactions are exercised.
- **Validated live:** full-universe validation against real Dhan data; AVWAP anchoring verified against chart-anchored VWAPs (root cause of one divergence — silent 429-storm fallback to a late anchor — fixed with pacing, retries, and the permanent PARTIAL flag + repair tools).
- **Known open item:** LTP column occasionally shows "–" in the scanner — diagnostic logging for the quote poll was added in v2026-09-16l to pinpoint the cause.

## 11. Key configuration (config/config.json)

| Key | Meaning | Default |
|---|---|---|
| `trading_mode` | `PAPER` / `LIVE` | `PAPER` |
| `market_data.source` | `dhan` / `mock` | `dhan` |
| `market_data.loop_tick_seconds` | main-loop cadence | 1 |
| `market_data.ltp_poll_seconds` | LTP/spot poll cadence | 30 |
| `market_data.universe_stocks` | curated liquid-stock list (~67) | set |
| `strategy.candle_interval_minutes` | signal timeframe | 15 |
| `strategy.itm_strikes_per_side` | scanner width (ITM per side) | 4 |
| `strategy.expiry_switch_day` | monthly switch day | 24 |
| `liquidity.weights` | volume / OI / spread / depth / consistency factor weights | 0.30 / 0.25 / 0.20 / 0.15 / 0.10 |
| `liquidity.max_spread_percent` / `preferred_spread_percent` | hard spread rejection / full-score spread | 0.50% / 0.30% |
| `liquidity.minimums.*` | optional qualification floors (score, volume/OI, percentiles, premium, depth) | null; calibrate before use |
| `volatility.weights` | 15-minute ATR%, daily realized vol, intraday vol, qualified option movement | 0.40 / 0.30 / 0.20 / 0.10 |
| `volatility.normalization_targets` | movement levels mapping component values to 100 points | illustrative; calibrate |
| `volatility.windows` | ATR/RV lookbacks, session annualization and option movement window | 14 / 20 / 50 / 4 bars |
| `risk.*` | quantity, max positions, max trades/day, max daily loss | set |
| `paper.slippage_bps` | paper-fill slippage | 10 |
| `dashboard.port` / `dashboard.control_token` | UI port / control gate | 9000 / — |
| `storage.db_path` | SQLite path | `data/trader.db` |

---

*One line to remember: the system does exactly one thing — sell options when a completed 15-min candle crosses below that contract's cumulative AVWAP and buy them back when it closes above it — and spends the rest of its engineering effort making sure that one thing is computed correctly, survives restarts, and can never trade live without an explicit, deliberate decision.*
