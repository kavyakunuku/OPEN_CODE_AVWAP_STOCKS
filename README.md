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
│   ├── liquidity.py      # adapt captured Dhan/project data for liquidity scoring
│   ├── volatility.py     # adapt captured Dhan/project price/IV data
│   ├── orders.py         # place order / order status / positions
│   └── feed.py           # DhanCandlePollFeed (real data) + MockFeed (dev only)
├── market/
│   ├── candles.py        # 15-min candle engine (boundary/dedupe/no-intracandle)
│   ├── universe.py       # expiry legs (monthly / N weeklies), per-leg ATM±N ITM, scanner ∪ positions
│   ├── contract_discovery.py # point-in-time NSE options, ATM/ITM CE/PE, identity + birth date
│   ├── liquidity.py      # timestamp-safe option-contract and chain liquidity scoring
│   ├── volatility.py     # point-in-time stock/option ATR and realized-volatility metrics
│   └── opportunity.py    # deterministic ranking over Phase 2–4 results (scanner only)
├── strategy/
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
└── tests/                # 163 tests covering strategy + Phases 2–5
```

## Phase 2 — option contract discovery

`market/contract_discovery.py` is an isolated discovery layer. It consumes the
Phase 1 validated stock universe, a timestamped contract-master catalog, and an
as-of underlying price (or a `(symbol, timestamp) -> price` provider). It
reuses `market.universe.select_monthly_expiry` and `find_atm`; it does not
score liquidity/volatility, create AVWAP signals, or place orders.

```python
from market.contract_discovery import OptionContractCatalog, OptionContractDiscovery

# `stock_universe` is the initialized Phase 1 market.universe_manager.UniverseManager.
catalog = OptionContractCatalog.from_universe_manager(stock_universe)
engine = OptionContractDiscovery.from_universe_manager(
    stock_universe,
    catalog=catalog,
    price_provider=lambda symbol, at: historical_or_live_spot(symbol, at),
)
result = engine.discover_option_contracts(
    "RELIANCE", "2026-09-15 10:00", max_itm=6
)
print(result.to_dict())
```

For replay/backtests, save dated masters outside Git and load the catalog with
`OptionContractCatalog.load(path)`. The engine uses only the latest snapshot
captured at or before the query time; it raises
`HistoricalContractSnapshotUnavailable` instead of falling forward to a
present-day master. Pass a price provider that returns the underlying's price
at that same timestamp. CE ITM strikes are below ATM and PE ITM strikes are
above ATM. Explicit strike-interval metadata is used when available; otherwise
the engine follows the actual ordered strike grid and records each local gap,
so nonuniform grids do not assume a ₹50/₹100 step. Missing contracts are
reported, never synthesized.

The Dhan master parser preserves contract-specific birth-date metadata when a
source supplies it. It leaves `contract_birth_date=None` if unavailable—it
never substitutes the underlying listing date. Supply `birth_date_provider`
when a contract-specific source is available; the included
`contract_birth_date_from_candles()` helper derives the date from that option's
first supplied candle. The standard master may not carry the field, so exact
historical availability still requires archived point-in-time masters and
option-specific listing/history data.

## Phase 3 — option-contract liquidity engine

`market/liquidity.py` scores each Phase 2 `OptionContract` independently using
`ContractLiquidityData` (timestamped observations plus option candles). The
`dhan/liquidity.py` adapter combines captured `OptionChainData`, optional
`DhanMarketData` quotes, and project candle/history inputs. For example:

```python
from dhan.liquidity import DhanLiquidityAdapter
from market.liquidity import OptionLiquidityEngine

inputs = DhanLiquidityAdapter(market_data).capture_current(
    phase2_contracts, captured_chain, candles_by_security_id=completed_candles
)
report = OptionLiquidityEngine(config).score_option_chain(inputs, as_of=timestamp)
for result in report.contracts:
    print(result.option_type, result.score, result.qualified, result.rejection_reasons)
```

The default factor weights are volume 30%, OI 25%, spread 20%, depth 15%, and
trading consistency 10% (weights are configurable and normalized). Volume, OI,
and displayed depth are mid-rank percentiles among supplied contracts for the
same underlying, expiry, and option side; a single candidate or tie maps to 50.
Depth is the smaller of aggregate bid-side and ask-side displayed quantities
(top-level sizes are used when full depth is absent). Volume uses the latest
captured cumulative value, falling back to the configured window of completed
bar volume. Spread quality is measured as `(ask - bid) / midpoint × 100`, with
configurable preferred/max spread values. Consistency is the share of available
valid completed bars with nonzero volume (invalid OHLC marks data invalid).
The reported `premium` is the current bid—the executable sell-side price—while
`last_price` is retained separately. Missing factor evidence scores zero
rather than increasing the other weights.

The configurable starter settings in `config/config.example.json` use a 0.30%
preferred and 0.50% maximum spread, 60-second quote freshness, and nullable
qualification floors. Those are not empirically validated trading thresholds;
calibrate and set appropriate score, volume/OI, premium, and depth minimums
before relying on `qualified`. Every rejected contract includes reason codes
such as `INVALID_DATA`, `WIDE_SPREAD`, `LOW_VOLUME`, `LOW_OI`, `LOW_PREMIUM`,
`LOW_DEPTH`, or `LOW_LIQUIDITY_SCORE`. The chain summary reports separate CE
and PE averages and subtracts a configurable penalty (up to 25 points by
default) for one-sided chains; that aggregate never changes the individual CE
or PE result.

Scoring is point-in-time: observations after `as_of` are discarded, naive
input times are interpreted as IST, and a candle is used only when its full
configured interval ended by `as_of`. Live Dhan chain/quote calls are timestamped
when received. Dhan does not provide historical quote/depth replay through this
adapter; historical BBO, depth, and OI snapshots must have been captured and
archived with their original timestamps. Candle history alone cannot recreate
historical bid/ask or depth. Phase 3 is a standalone evaluation engine: it does
not calculate AVWAP, generate signals, place orders, or change strategy logic.

## Phase 4 — volatility analysis engine

`market/volatility.py` calculates daily ATR/ATR%, 15-minute ATR/ATR%, annualized
daily realized volatility, same-session intraday volatility, and (when supplied)
option price movement, option ATR, and option realized volatility. `dhan/volatility.py`
converts existing timestamped Dhan/project candles, quotes, and option-chain
spot/LTP/IV into engine inputs; it makes no network requests.

```python
from dhan.volatility import DhanVolatilityAdapter
from market.volatility import VolatilityAnalysisEngine

inputs = DhanVolatilityAdapter().build_input(
    "RELIANCE",
    daily_bars=stock_daily_bars,
    bars_15m=stock_15m_bars,
    option_contracts=phase2_contracts,
    option_bars_by_security_id=option_15m_bars,
    option_chain=captured_chain,
    liquidity_qualified_security_ids=phase3_qualified_ids,
)
result = VolatilityAnalysisEngine(config).analyze(inputs, as_of=timestamp)
print(result.to_dict())
```

ATR uses Wilder smoothing (14-bar starter periods). Daily realized volatility is
the annualized sample standard deviation of log returns; intraday volatility is
annualized from consecutive 15-minute, same-session returns and excludes
overnight gaps. Daily ATR can use supplied daily bars or fully observed 15-minute
sessions; incomplete sessions are not aggregated. Insufficient windows return
`None` and are listed in `insufficient_data`, rather than being fabricated.

The volatility score uses configurable starter weights of 40% 15-minute ATR%,
30% daily realized volatility, 20% intraday volatility, and 10% option movement.
Each component saturates at its configurable normalization target, so extreme
movement does not keep increasing the score. The targets in the example config
are illustrative and must be calibrated. Unavailable components contribute
zero; their weights are not redistributed, and missing windows are exposed in
`insufficient_data`. Only option series explicitly marked liquidity-qualified
by Phase 3 contribute to the option-movement factor; other
option metrics may still be reported for analysis. This is a movement score,
not a liquidity score or trade approval: Phase 3 liquidity qualification remains
an independent prerequisite and cannot be overridden by volatility.

All bars and price/IV observations are filtered at the requested timestamp.
15-minute bars are used only when their full interval ended by `as_of`; a daily
bar for the current session is unavailable until the 15:30 close. IV is copied
only from a timestamped source observation, reported in the source's raw units,
and never inferred. Historical analysis requires archived underlying/option
bars and timestamped quote/IV observations; a current Dhan chain snapshot is not
historical data. Phase 4 does not modify AVWAP, entry/exit rules, order or position
management, or generate signals.

## Phase 5 — opportunity ranking scanner

`market/opportunity.py` combines Phase 2 discovery results, a Phase 3
`OptionChainLiquidityReport`, and one Phase 4 `VolatilityAnalysisResult` per
underlying. It is standalone and deterministic: it consumes these results,
never fetches market data, never places orders, and never changes an open
position.

```python
from market.opportunity import OpportunityRankingEngine, OpportunityScannerInput

scan_input = OpportunityScannerInput(
    as_of=timestamp,
    discoveries=phase2_discoveries,
    liquidity_report=phase3_report,
    volatility_by_underlying={"VEDL": vedl_phase4_result},
    existing_position_underlyings=open_position_underlyings,
)
ranked = OpportunityRankingEngine(config).rank(scan_input)
print(ranked.to_json())
print(ranked.to_csv("stocks"))
print(ranked.to_csv("contracts"))
```

Only contracts explicitly **qualified by Phase 3** can enter a new-opportunity
ranking; high volatility cannot rescue a liquidity rejection. The default
opportunity weights are 40% liquidity, 30% underlying volatility, 20% Phase 3
chain quality, and 10% execution quality. All four are configurable, but
configuration rejects a volatility weight equal to or greater than liquidity.
Execution quality uses only the Phase 3 quote spread and quote age (default 70/30 internal
spread/freshness weights); it does not estimate fills or slippage. Missing chain
or execution evidence contributes zero without redistributing the other weights
and is listed in `missing_components`. Missing Phase 3 or Phase 4 results are
excluded rather than invented.

Both CE and PE contracts qualify independently. An existing position blocks
new opportunities for its underlying, even if a different strike/side scores
higher; pass active underlyings (or a candidate security ID, which is resolved
to its underlying) through the position-exclusion inputs. This prevents
scanner ranks from becoming position-switch instructions. Phase 2 timestamp
and Phase 3/4 evaluation timestamps must exactly match the requested `as_of`;
its archived contract-master snapshot may be older but never later.

Default selection is the top 10 stocks, at most 14 qualified contracts per
stock, and 10 top opportunities. These limits and weights are configurable in
`opportunity.*`. Contract ranks are global among the returned stock selection.
Stock component scores are arithmetic means over that stock's selected
qualified contracts; stock volatility uses its Phase 4 underlying score. Stable
tie-breaks use symbol, expiry, side, strike, and security ID. `to_dict()` returns
`timestamp`, `stocks`, `contracts`, and `top_opportunities` (plus exclusions and
configuration); `to_json()`, `to_csv(section)`, `write_json()`, and `write_csv()`
provide structured file/export paths. Phase 5 does not connect to order
placement, AVWAP, entry/exit rules, or position management.

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
| `opportunity.weights` | `0.40 / 0.30 / 0.20 / 0.10` | liquidity / volatility / chain quality / execution quality scanner weights |
| `opportunity.selection` | `10 / 14 / 10` | max stocks / contracts per stock / top opportunities |
| `opportunity.execution` | spread `0.30–0.50%`, quote age `60s` | execution-quality spread and freshness normalization (not fill/slippage prediction) |
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
python -m pytest tests/ -v     # 163 tests
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
* Contract discovery: point-in-time master selection (no future snapshots),
  monthly expiry/close-of-expiry handling, ATM and CE/PE ITM directions,
  dynamic strike-step inference, missing contracts, birth dates, and token
  identity
* Liquidity engine: comparable-contract percentiles and configurable weights,
  spread/invalid quote checks, multiple rejection reasons, independent CE/PE
  scoring, one-sided chain penalty, Dhan field adaptation, and as-of filtering
  of future observations and incomplete candles
* Volatility engine: Wilder ATR/ATR%, 15-minute ATR, rolling annualized and
  intraday volatility, option movement/ATR/realized volatility and timestamped
  IV, data-insufficiency reporting, complete-session daily aggregation, target
  saturation/weight behavior, liquidity-qualified option gating, and no-look-ahead
  boundaries
* Opportunity scanner: 40/30/20/10 weighted scoring, Phase 3 liquidity hard gate,
  execution spread/freshness scoring, deterministic ranks/tie-breaks, top-N caps,
  independent CE/PE qualification, existing-position underlying exclusion,
  historical as-of integrity, end-to-end Phase 2/3/4 inputs, and JSON/CSV output
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
