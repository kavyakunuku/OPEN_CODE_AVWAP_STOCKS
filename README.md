# AVWAP F&O Option-Selling System (Stock Options Only)

A Python paper-trading (and later live-trading) system that implements **one
strategy, exactly**:

> **15-minute, contract-specific, lifetime-anchored AVWAP option-selling
> for F&O stocks.**

The strategy logic is **identical in PAPER and LIVE modes** — only the
execution layer (and optionally the data source) changes.

---

## 1. The strategy (as specified — do not "improve" it)

For every F&O **stock** (not index options):

1. **Expiry** — the selected **monthly expiry**, identified from the actual
   exchange/Dhan expiry dates (the furthest expiry date of a calendar month):
   * 1st → 23rd of the month: **current month's** monthly expiry
   * 24th → end of month: **next month's** monthly expiry
   * the 24th is a *premium-preservation* rule, not an expiry rule
2. **ATM** — the available chain strike closest to the spot (ties → lower).
3. **Universe** — for each underlying: `ATM + 4 ITM` calls **and** `ATM + 4
   ITM` puts (max 10 contracts), from real chain strikes. The width `N` is
   configurable via `strategy.itm_strikes_per_side` (default 4; deeper ITM
   strikes have thin liquidity on most names).
4. **Candles** — completed **15-minute candles only** (NSE session
   09:15–15:30 IST). No intracandle signals, ever. The first possible
   signal is after 09:30.
5. **AVWAP** — each option contract carries its **own** AVWAP, anchored at
   that contract's **first tradable candle**, cumulative across all days,
   never reset:
   `AVWAP = Σ(TPᵢ·Vᵢ) / Σ(Vᵢ)`, `TP = (H+L+C)/3`.
6. **Entry (SELL)** — a *true* cross on completed candles:
   `prev_close >= prev_avwap AND current_close < current_avwap`.
7. **Exit (BUY-TO-CLOSE)** — `current_close > current_avwap`.
   No fixed stop-loss. No other indicator. No other filter.
8. **Re-entry** — only after a fresh valid cross.
9. **Universe priority** — an open position is ALWAYS monitored for its exit,
   even when the underlying moves and the contract leaves the ATM±N scanner.

The system only ever: **SELLS** options (entry) and **BUYS** options
(exclusively to close an existing short).

## 2. Project layout

```
avwap_trader/
├── main.py               # entry point (PAPER by default)
├── app.py                # orchestrator: feed → candles → AVWAP → signals → broker
├── config/config.json    # your configuration (start from config.example.json)
├── common/               # models, config loader, IST time/session math
├── dhan/
│   ├── client.py         # Dhan v2 REST client + JWT (stdlib-only)
│   ├── instruments.py    # instrument masters → F&O stock universe, lot sizes
│   ├── option_chain.py   # option chain + expiry list (paced: 1 req / 3 s)
│   ├── market_data.py    # intraday 15-min candles + batched quotes
│   ├── orders.py         # place order / order status / positions
│   └── feed.py           # DhanCandlePollFeed (real data) + MockFeed (dev only)
├── market/
│   ├── candles.py        # 15-min candle engine (boundary/dedupe/no-intracandle)
│   └── universe.py       # expiry legs (monthly / N weeklies), per-leg ATM±N ITM (default 4), scanner ∪ positions
── strategy/
│   ├── avwap.py          # per-contract AVWAP state (persisted, never reset daily)
│   ├── rules.py          # the ONLY strategy rules (cross / close-above)
│   └── engine.py         # signal engine + duplicate-signal protection
├── execution/
│   ├── broker.py         # broker interface (SIGNAL → ORDER → FILL)
│   ├── paper.py          # PaperBroker (explicit fill conventions)
│   └── live.py           # DhanBroker (real orders, fills, reconciliation)
├── portfolio/
│   ├── positions.py      # position lifecycle (OPEN/CLOSED/PENDING/REJECTED)
│   ├── pnl.py            # short P&L = (entry − exit) × qty
│   └── risk.py           # risk controls (separate from the strategy)
├── storage/
│   ├── database.py       # SQLite: candles, avwap_state, signals, positions, orders, journal, kv
│   └── journal.py        # permanent audit log
├── dashboard/            # Flask dashboard (status, scanner, positions, controls)
└── tests/                # 65 tests covering the §48 testing requirements
```

## 3. Installation & running

```bash
cd avwap_trader
pip install -r requirements.txt

# 1) paper trading on REAL Dhan market data (no orders sent)
cp config/config.example.json config/config.json
# edit config/config.json → dhan.client_id, dhan.access_token
#    (or export DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN)
python main.py
# dashboard: http://localhost:9000

# 2) development run with synthetic data (no Dhan account needed)
python main.py --demo

# 3) tests
python -m pytest tests/ -v
```

### What happens on startup

```
load config + database
  → load open positions + AVWAP states            (restart recovery)
  → load Dhan instrument masters (cached 12 h)
  → F&O stock universe (derived, not hard-coded)
  → optionally restricted to market_data.universe_stocks (curated liquid list)
  → per underlying: expiry list → selected legs → option chain per leg
      (monthly single leg for all stocks; paced at 1 request / 3 s —
       ≈ 8 min for the 67-stock curated list, ≈ 25 min for the full ~210)
  → ATM + 4 ITM × (CE+PE) per expiry leg (default width; strategy.itm_strikes_per_side)
  → per contract: fetch month-to-date 15-min candles → AVWAP init
  → engine loop:
      candle window closes → fetch completed candle → AVWAP update
      → rules → signal → risk gate → PAPER fill | Dhan order
      → quote polling for LTP/dashboard
      → (LIVE) order-status polling + position reconciliation
  → dashboard
```

## 4. Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `trading_mode` | `PAPER` | `PAPER` or `LIVE`. Never silent-falls back. |
| `dhan.client_id` / `dhan.access_token` | — | Dhan API credentials (also `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` env vars) |
| `market_data.mode` | `candle_poll` | default data path: authoritative completed candles from Dhan's intraday API + batched LTP quotes |
| `market_data.source` | `dhan` | `dhan` (real) or `mock` (synthetic, development only) |
| `market_data.boundary_grace_seconds` | `8` | wait after a boundary before fetching the fresh candle |
| `market_data.ltp_poll_seconds` | `30` | dashboard LTP refresh cadence |
| `market_data.history_start` | `month_start` | AVWAP init window (fallback `history_lookback_days_fallback`) |
| `market_data.universe_refresh_minutes` | `60` | how often the option chains / ATM are refreshed |
| `market_data.universe_stocks` | `[]` (all F&O stocks) | optional curated list of liquid stocks to trade (e.g. 67 high-volume names, shipped in `config/config.json`). Symbols are matched against the live Dhan master case-insensitively; names not in the master are logged and skipped. `[]` = full universe |
| `strategy.itm_strikes_per_side` | `4` | ATM + N in-the-money strikes per side (CE below ATM, PE above ATM). Default 4 — deeper ITM strikes have thin liquidity/OI on most names. Open positions are always monitored even when they fall outside this width |
| `strategy.expiry_switch_day` | `24` | premium-preservation switch day |
| `paper.fill_mode` | `candle_close` | `candle_close` (fill = signal candle close) or `next_quote` (first LTP after the close, fallback candle close) |
| `paper.slippage_bps` | `0` | optional slippage applied against the trader |
| `live.product_type` | `MARGIN` | Dhan product type for orders |
| `live.order_type` | `MARKET` | `MARKET` or `LIMIT` (`limit_price_offset_ticks`) |
| `risk.quantity_per_trade` | `null` | `null` = use the contract's lot size from the instrument master |
| `risk.max_open_positions` | `8` | entry gate |
| `risk.max_trades_per_day` | `20` | entry gate |
| `risk.max_daily_loss` | `25000` | entry gate (realized today + unrealized at LTP) |
| `dashboard.*` | `:9000` | dashboard host/port; `control_token` guards the control buttons |

## 5. Paper fill convention (explicit, per spec)

| Mode | Entry SELL | Exit BUY |
|---|---|---|
| `fill_mode=candle_close` (default) | close of the completed signal candle | close of the completed signal candle |
| `fill_mode=next_quote` | first LTP quote after the close (≤ `next_quote_timeout_seconds`, else candle close) | same |

Signal price, fill price, AVWAP at entry/exit, and the reason are all
recorded separately in the journal and positions table.

## 6. Live trading (LIVE mode) — checklist

LIVE mode requires **two explicit confirmations**: `trading_mode: "LIVE"` in
the config **and** the `--i-understand-live` flag (or
`AVWAP_LIVE_CONFIRM=I_UNDERSTAND_LIVE_TRADING`). Otherwise the app aborts and
prints the checklist:

1. Dhan credentials confirmed (client_id, access token).
2. Static-IP / API-plan requirements satisfied for your account.
3. Instrument mapping verified (security IDs, lot sizes from the master).
4. Lot size per trade verified (`risk.quantity_per_trade` vs master).
5. Monthly expiry selection verified (24th switch rule, real expiry dates).
6. ATM calculation verified against the chain.
7. AVWAP calculation verified (`pytest` + several paper days).
8. Signal detection verified (one cross → one order; duplicates suppressed).
9. Paper execution observed for a sufficient number of days.
10. Position reconciliation verified (restart the app with open positions).
11. Order status handling verified (partial fills, rejections — exits that
    get rejected stay OPEN and raise a loud journal error).
12. Emergency exit tested (`EXIT ALL POSITIONS` on the dashboard).

When LIVE is active the dashboard shows a pulsing red banner:
`⚠ LIVE TRADING ENABLED — REAL ORDERS MAY BE SENT TO DHAN`.

Live order lifecycle: `SIGNAL → ORDER SENT → (poll) COMPLETE / PARTIAL /
REJECTED`. Positions are `PENDING` until Dhan reports the fill; fills use
Dhan's `avgPrice`. On every start the system reconciles DB positions against
`GET /v2/positions` (foreign shorts are adopted into monitoring with a loud
warning; missing shorts are marked `CLOSED/RECONCILED_NOT_FOUND`).

## 7. Data & storage

SQLite (`data/trader.db`, WAL mode). Tables:

* `candles` — completed 15-min candles + AVWAP after each candle
* `avwap_state` — **the** cumulative AVWAP state per contract (restart-proof)
* `signals` — every signal, unique key `(contract, candle, action)`
* `positions` — full trade records incl. entry/exit AVWAP, reasons, P&L
* `orders` — every order (paper-virtual + live) with raw responses
* `journal` — permanent audit trail (SIGNAL, PAPER_ENTRY, ORDER_SENT, FILL,
  ORDER_REJECTED, ENTRY_BLOCKED, RECONCILE, …)
* `kv` — persisted risk flags (emergency stop, disable entries)

Restart recovery: load DB → load open positions (adopt contracts that are no
longer in the scanner) → load AVWAP states → reconcile (LIVE) → resume.
Already-consumed candles are never re-evaluated (three layers: in-memory
candle dedupe, persisted signal keys, AVWAP-state idempotency).

## 8. Risk controls (separate from the strategy)

The strategy engine only sees AVWAP crosses. Before a signal becomes an order,
the risk gate may block it: max open positions, max trades/day, max daily
loss, `disable_new_entries` (dashboard switch), `emergency_stop` (dashboard
button — blocks new entries; **exits always work**). `EXIT ALL POSITIONS` is
an operational override, recorded as `MANUAL_EXIT_ALL`, and is not part of
the strategy logic.

## 9. Dashboard (display + operational controls)

Flask, http://localhost:9000, auto-refresh every 4 s. **Everything shown is
display-only** — the strategy engine evaluates signals from completed 15-min
candles independently; the dashboard never influences trading.

* **Startup progress** — a one-time bootstrap builds the market universe and
  each contract's month-to-date AVWAP history (a fresh start takes several
  minutes on the full curated list; a restart with a warm DB is fast). While
  this runs the dashboard shows a live progress bar
  (`BOOTSTRAPPING — AVWAP history 450/980 · …`) instead of a blank
  "LOADING…", and `/api/state` stays fast (it returns a lightweight payload
  during bootstrap). Once done it switches to the normal live view.
* **Universe** — one row per underlying per expiry leg.
* **Scanner** — every monitored contract (scanner ∪ open positions) with:
  * **Δ vs AVWAP** — (last close − AVWAP) / AVWAP as a %: how far the last
    completed candle's close is from that contract's own AVWAP.
  * **Trigger** — how close the *next* completed candle is to each rule:
    `EXIT DUE` (open short, last close already above AVWAP),
    `EXIT NEAR x%` / `hold · exit on close above` (open short below AVWAP),
    `ENTRY NEAR x%` (close just above AVWAP — one red candle away from a
    valid entry), `armed @ +x%` (above AVWAP, further out),
    `below · re-arm above first` (a fresh cross is required before
    re-entry). The "near trigger" threshold is adjustable (default 1% of
    AVWAP, remembered in the browser).
  * **DTE** — days to expiry (`TODAY` / `1d` / `Nd`).
  * **Last close** — time + age of the last completed candle (red when >60 min).
  * The table **defaults to trigger-urgency order** (most about-to-trigger
    first); **click any column header to sort** (asc/desc); quick filters
    (Entry near / Exit near / Exit due / Open / Pending / Below) plus the
    free-text filter box; **scanner CSV export**.
* **Open positions** (with unrealized P&L), **recent signals**, **closed
  trade history**, **journal tail** — all sortable.
* **Operational controls** (guarded by `dashboard.control_token`): disable /
  enable entries, emergency stop (blocks entries, exits still work),
  EXIT ALL POSITIONS.

## 10. Testing

```
python -m pytest tests/ -v     # 65 tests
```

Coverage per the specification's testing requirements:

* AVWAP: first candle, multi-candle, multi-day continuity (no daily reset),
  zero/missing volume, contract independence, history initialization,
  persistence + reload
* Entry: cross triggers; `prev < AVWAP & cur < AVWAP` does NOT trigger
* Exit: only `close > AVWAP` (equal/below hold)
* Re-entry: requires a fresh cross
* CE/PE independence; one short per contract; duplicate-signal suppression
  (including across a full restart)
* Candle engine: session boundaries, no intracandle signals, duplicate /
  out-of-order rejection, tick-building volume deltas
* Universe: expiry switch rule (1st/23rd/24th, passed expiry), ATM ties,
  ATM movement never drops open positions
* Multi-expiry legs: N consecutive weekly selection (current+next week,
  rolling forward), per-leg ATM from each leg's own strikes, `drop_expiries`
  removes rolled-off legs while keeping positions monitored, one dashboard
  row per leg
* Risk gates; paper P&L (spec example: (100−80)×250 = 5,000)
* Restart recovery: positions + AVWAP survive; positions outside the scanner
  universe keep being monitored

## 11. Dhan API notes (v2) — request/response shapes verified LIVE on 2026-09-15

* Auth: the dashboard-generated access token is sent **as-is** in the
  `access-token` header (it is already a JWT — do NOT re-sign it locally),
  plus `client-id`; POST bodies carry `dhanClientId` (matches the official
  dhanhq SDK).
* Option chain: `POST /v2/optionchain` (body `UnderlyingScrip`, response
  `data.oc`, spot in `data.last_price`) + `POST /v2/optionchain/expirylist` —
  rate limit **1 request / 3 s** (a `PacedChainClient` enforces it).
* Intraday candles: `POST /v2/charts/intraday` with
  `{securityId, exchangeSegment:"NSE_FNO", instrument:"OPTSTK", interval:15,
  oi:false, fromDate:"YYYY-MM-DD", toDate:"YYYY-MM-DD"}` → columnar arrays at
  the top level (`timestamp/open/high/low/close/volume`). Two live-verified
  quirks are handled: the day's first candle is stamped at the first trade
  (e.g. 09:17) and is snapped onto the 09:15 grid; the 15:30-labeled
  closing-auction candle is outside the 09:15–15:30 session and is dropped.
* Quotes: Market Quote API `POST /v2/marketfeed/quote` (body
  `{"NSE_FNO":[ids]}` / `{"NSE_EQ":[ids]}`) — up to 1000 ids per request,
  1 request/s; LTP + day volume per instrument. 429s are retried.
* Orders: `POST /v2/oddl/requests/order`, status via
  `GET /v2/oddl/requests/order/get/{id}`, positions via `GET /v2/positions`
  (raw bodies are logged on every order response).
* Instrument master: `https://images.dhan.co/api-data/api-scrip-master-detailed.csv`
  (the old `…/api/instruments/v2/…/option-instrument-master.csv` URLs are
  dead — they 403 for everyone). Cached under `data/instruments/` (12 h TTL);
  the F&O stock universe is **derived** from it (index/ETF underlyings
  excluded), lot sizes and tick sizes included.
* A full real-data bootstrap walks ~210 stocks through the 3 s-paced chain
  API, so **start the app ~30 minutes before the 09:15 open**.
* All API errors are logged with raw response bodies and are isolated per
  instrument — one bad stock never stops the rest.

## 12. Mock mode (`--demo`) — development only

`market_data.source: "mock"` (or `--demo`) runs the **identical** strategy
pipeline on a synthetic market (3 fake stocks, monthly expiries, random-walk
premiums, month-anchored AVWAP history, accelerated IST clock). It exists so
the full system — bootstrap, AVWAP init, candles, signals, paper fills, risk
gates, dashboard, restart recovery — can be exercised without a Dhan account
or market hours. **Never use mock data to validate the strategy.**

## 13. Known limitations / notes

* NSE holidays are not modelled (a holiday simply produces no candles; the
  system idles and resumes on the next trading day).
* The candle-poll data path fetches the completed candle per contract right
  after each boundary (≈ 3 req/s for the full ~2,800-contract universe).
  This comfortably fits typical Dhan data-API limits; if you see 429s, raise
  `boundary_grace_seconds` / lower the monitored universe.
* The Dhan quotes/chain endpoints used here were verified against the v2
  docs in 2026-09; if Dhan changes a field name, the parsers log the raw
  response so it's easy to spot.
* Transaction costs (brokerage/STT/charges) are intentionally NOT part of
  P&L yet — add them as a separate configuration in a future revision.
* The dashboard is a development-grade Flask server (fine for a single
  trader's machine; put it behind a reverse proxy if exposed).

---

**Faithful implementation first. Observe in PAPER. Then — and only then —
talk about modifications.** (spec §49)
