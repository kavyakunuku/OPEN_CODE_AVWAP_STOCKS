# AVWAP F&O Option-Selling System (Stock Options Only)

A Python **Phase 8 paper-trading-only** system that implements **one strategy,
exactly**, against live Dhan market data:

> **15-minute, contract-specific, lifetime-anchored AVWAP option-selling
> for F&O stocks.**

No live order-placement API is called. LIVE mode is hard-blocked by both the
CLI and `TraderApp`; wait for Phase 9 and explicit approval before changing
that restriction.

---

## 1. The strategy (as specified — do not "improve" it)

For Dhan-backed Phase 6 operation, the configured Phase 1 stock universe is
validated against the instrument master and passed through the opportunity
scanner before contracts are monitored. With the scanner disabled, the legacy
F&O **stock** (not index options) candidate behavior is:

1. **Expiry** — the selected **monthly expiry**, identified from the actual
   exchange/Dhan expiry dates (the furthest expiry date of a calendar month):
   * 1st → 23rd of the month: **current month's** monthly expiry
   * 24th → end of month: **next month's** monthly expiry
   * the 24th is a *premium-preservation* rule, not an expiry rule
2. **ATM** — the available chain strike closest to the spot (ties → lower).
3. **Legacy candidates** — per underlying: `ATM + N ITM` calls **and**
   `ATM + N ITM` puts, from real chain strikes; `N` is configurable via
   `strategy.itm_strikes_per_side`. With the default Dhan scanner enabled,
   Phase 2–5 further filters/ranks new candidates before AVWAP monitoring.
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
├── config/universe.json  # Phase 1 validated 20-stock candidate universe
├── common/               # models, config loader, IST time/session math
├── dhan/
│   ├── client.py         # Dhan v2 REST client + JWT (stdlib-only)
│   ├── instruments.py    # instrument masters → F&O stock universe, lot sizes
│   ├── option_chain.py   # option chain + expiry list (paced: 1 req / 3 s)
│   ├── market_data.py    # intraday 15-min candles + batched quotes
│   ├── liquidity.py      # adapt captured Dhan/project data for liquidity scoring
│   ├── volatility.py     # adapt captured Dhan/project price/IV data
│   ├── orders.py         # dormant Dhan order adapter; not used in Phase 8
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
│   ├── paper.py          # quote-aware, persisted paper fills (no network access)
│   └── live.py           # dormant legacy code; never imported by the Phase 8 app
├── portfolio/
│   ├── positions.py      # position lifecycle (OPEN/CLOSED/PENDING/REJECTED)
│   ├── pnl.py            # short P&L = (entry − exit) × qty
│   └── risk.py           # risk controls (separate from the strategy)
├── storage/
│   ├── database.py       # SQLite: candles, avwap_state, signals, positions, orders, journal, kv
│   └── journal.py        # permanent audit log
├── dashboard/            # Flask dashboard (status, scanner, positions, controls)
└── tests/                # full suite covers strategy + Phases 2–8
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

## Phase 6 — scanner-to-AVWAP integration

For Dhan data, `opportunity.enabled` defaults to `true`. Phase 1 validates the
20 configured stocks in `config/universe.json` against the same Dhan instrument
master snapshot already loaded by the app. The runtime then uses the real option
master, paced option-chain snapshots, current NSE_FNO/NSE_EQ quotes, and
completed NSE_EQ 15-minute history to run the Phase 2 discovery, Phase 3 hard
liquidity gate, Phase 4 volatility analysis, and Phase 5 ranking pipeline.
Only top-ranked, metadata-registered contracts enter the scanner allow-list.
Scanner failure or an empty ranking fails closed for new candidates. The mock
feed has no genuine option-chain book/OI/IV source, so the scanner is disabled
for mock mode instead of inventing inputs.

```text
20 validated stocks
  → contract discovery + paced Dhan option-chain snapshots
  → liquidity qualification (hard prerequisite) + timestamped volatility inputs
  → deterministic opportunity ranking (default: top 10 contracts)
  → history-prime newly selected option contracts
  → existing completed-candle AVWAP evaluation
  → existing AVWAP entry/exit + risk + PAPER execution

open positions ────────────────────────────────────────┘ (always monitored)
```

The scanner only controls eligibility for **new** opportunities. It does not
emit signals, change a position, switch a position to a higher-ranked contract,
or decide SELL/EXIT. Open positions remain in the monitoring set even when
rankings change or the scanner fails. Newly selected contracts are exposed only
after their available option history has been merged into the existing AVWAP
state; previously selected contracts that leave and re-enter the ranking keep
their anchor and accumulated state. Only completed 15-minute candles are
processed, using the existing candle boundaries and first-valid-signal timing.
The AVWAP formula, anchoring, option-chart basis, entry/exit conditions, and
mechanical exit logic are unchanged. Set `opportunity.enabled` to `false` to
restore the legacy ATM/ITM candidate universe for Dhan.

Each processed evaluation is idempotently stored in SQLite `evaluations` with
evaluation timestamp, candle timestamp, symbol, option, liquidity score,
volatility score, opportunity score, scanner timestamp, AVWAP, option close,
and signal. Scanner scores are associated with the ranking used for the
contract; active-position metrics retain their most recent scanner context.
Dhan's current chain/quote observations are not historical archives: absent
historical option quotes, depth, or IV are **not** fabricated. The runtime uses
only the current chain's actual spread/depth/OI/IV fields and actual
at-or-before-time option quotes; missing component history remains unavailable.
Because the live pipeline does not yet archive option candles or book snapshots,
history-based option-volume consistency, option movement, and historical IV
remain unavailable (their configured component weights are not redistributed).

Regression evidence:

```bash
.venv/bin/pytest -q tests/test_avwap.py tests/test_avwap_gap_backfill.py \
  tests/test_candles.py tests/test_engine.py tests/test_rules.py
.venv/bin/pytest -q
```

The integration tests compare Scanner OFF and ON for the same completed option
candles and assert identical AVWAP state and signals; another test primes a
newly selected contract and verifies the original first-valid completed-candle
signal timing. `strategy/avwap.py` and the AVWAP formula are not changed by this
integration. The complete regression suite passed 205 tests in the audited
workspace on 2026-10-01, including paper execution, restart, scanner audit,
reports, stale-mark handling, data validation, and live-order hard-block tests.
Live order placement is rejected by the CLI, app constructor, legacy broker
constructor, and legacy Dhan order adapter—not merely disabled in the example
config.

## Phase 8 — paper-only execution, audit, and recovery

Phase 8 runs the validated 20-stock universe through contract discovery,
liquidity qualification, volatility analysis, opportunity ranking, and the
existing option-candle AVWAP strategy. Dhan scanner snapshots refresh at the
configured `opportunity.refresh_minutes` cadence (15 minutes by default), and
each scan run plus each discovered contract's qualified/rejected/selected
status is persisted before the next re-rank can replace the in-memory ranking.
Re-ranking controls new entries only: it never replaces or closes an active
position. Active contracts remain monitored until the unchanged AVWAP/mechanical
exit or an explicit paper-only manual close.

Paper execution is quote-aware and deliberately avoids midpoint fills:

* A fresh two-sided Dhan option book (at most `paper.max_quote_age_seconds`
  old) is crossed: entry SELL at the captured best bid; exit BUY at the captured
  best ask.
* Configured adverse slippage is applied per side and rounded against the
  paper trader to the contract tick. Defaults are 5 basis points per side and
  a 60-second maximum quote age.
* If a fresh two-sided book is unavailable, strategy fills fall back to the
  AVWAP signal candle close plus adverse slippage. Each event records the
  method, quote timestamp/age, any observed bid/ask/spread, signal and fill
  timestamps/prices, quantity, lot size, slippage, configured costs, AVWAP,
  underlying spot, and indicative margin. Missing bid/ask is left missing; it
  is never synthesized. Manual exits may use a fresh timestamped LTP only when
  no valid two-sided book exists and are labeled as an explicit LTP fallback.
* Transaction-cost defaults are zero; only configured per-order and premium-
  turnover bps are reported, rather than silently inventing taxes/fees. The
  default margin estimate is 20% of underlying notional and is indicative,
  not a Dhan/SPAN requirement. Daily output distinguishes gross P&L from net
  P&L after configured costs.

SQLite stores immutable-ish paper fill events, scanner runs and per-contract
decisions, timestamped position marks, errors, and daily report summaries.
Open positions, closed positions, entry/exit details, P&L, margin estimate,
contract, quantity, and AVWAP survive restart. On startup, incomplete audit
rows are reconstructed from persisted positions with missing quote fields kept
NULL and explicitly labeled as recovered; a restart never fabricates market
data or replaces an active position. Runtime errors and recovery actions are
written to the paper error table and journal.

Reports are regenerated during operation (on scans/signals, periodically, and
at shutdown) and can be rebuilt without starting the engine:

```bash
python -m paper_trading report                  # all stored days plus today
python -m paper_trading report --date 2026-09-30 # one day
```

Each `reports/paper/YYYY-MM-DD/` contains `daily.json`, `daily.md`, scanner,
trade, position, execution, and error CSVs. Root-level `trade_log.csv`,
`position_log.csv`, `paper_error_log.csv`, and `daily_index.csv` provide
multi-day exports. The dashboard shows scanner/report status and serves a
read-only summary at `/api/paper-report?date=YYYY-MM-DD`.

**LIVE is hard-disabled for Phase 8.** `python main.py --mode LIVE`, a LIVE
config value, or `TRADING_MODE=LIVE` exits before a broker/app is constructed;
`TraderApp` also rejects non-PAPER mode. The `DhanOrders` and `DhanBroker`
legacy modules are not imported or instantiated by the app. No live order,
position reconciliation, or order-poll endpoint is called. `--i-understand-live`
is a legacy compatibility flag only and cannot enable orders. Phase 9 remains
out of scope until separately approved.

## 3. Installation & running

Create a virtual environment from the repository root, then install the
project requirements. Python 3.10+ is required (Python 3.11 is recommended).

Linux/macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp config/config.example.json config/config.json
```

Windows PowerShell:
```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item config/config.example.json config/config.json
```

`config/config.json` is a local, Git-ignored file. For Dhan-backed paper mode,
set `dhan.client_id` and `dhan.access_token` there, or use the environment
variables `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` instead. Keep credentials
private; do not commit them or paste them into an issue or chat. For a local-only
dashboard, set `dashboard.host` to `127.0.0.1` in your local config and use a
strong `dashboard.control_token` if remote control is enabled.

From the repository root, with the virtual environment active:
```bash
# Full regression suite
python -m pytest -q

# Dhan-backed PAPER mode: real market data, virtual fills only, NO live orders
python main.py
# dashboard (default port): http://127.0.0.1:9000

# Optional development smoke run: synthetic data; no Dhan credentials needed
python main.py --demo
```

The `--demo` run is not a market-data integration test or evidence of strategy
performance. Phase 8 is paper-only: the CLI rejects LIVE mode and the application
does not send live order-placement requests. The scanner cannot automatically
replace active positions. The Phase 7 historical archive is not bundled; until
the required archived market data is supplied, empirical backtest results are
blocked and must not be inferred from synthetic data.

### What happens on startup

```
load config + database
  → load open positions + AVWAP states               (restart recovery)
  → load Dhan instrument-master snapshot (cached 12 h)
  → if opportunity.enabled + Dhan: validate config/universe.json symbols
      against that same snapshot; fetch timestamped NSE_EQ 15-minute history
      and paced monthly option-chain snapshots for the validated universe
      → Phase 2 discovery → Phase 3 liquidity gate → Phase 4 volatility
      → Phase 5 ranking → new-entry eligibility allow-list (top opportunities)
    else: legacy F&O/ATM-ITM candidate selection
  → register selected option metadata; AVWAP-monitor eligible contracts ∪ positions
  → prime/restore completed option candles and AVWAP state
  → engine loop:
      candle window closes → fetch completed candle → AVWAP update
      → existing rules → signal → risk gate → PAPER fill (example config)
      → Dhan quote polling for fills/marks/dashboard
      → SQLite paper audit + daily/multi-day report refresh
  → dashboard (paper-only; no order endpoint calls)
```

## 4. Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `trading_mode` | `PAPER` | Phase 8 accepts `PAPER` only. `LIVE` is rejected by CLI and app before broker construction. |
| `dhan.client_id` / `dhan.access_token` | — | Dhan API credentials (also `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` env vars) |
| `market_data.mode` | `candle_poll` | default data path: authoritative completed candles from Dhan's intraday API + batched LTP quotes |
| `market_data.source` | `dhan` | `dhan` (real) or `mock` (synthetic, development only) |
| `market_data.boundary_grace_seconds` | `8` | wait after a boundary before fetching the fresh candle |
| `market_data.ltp_poll_seconds` | `30` | dashboard LTP refresh cadence |
| `market_data.history_start` | `month_start` | AVWAP init window (fallback `history_lookback_days_fallback`) |
| `market_data.universe_refresh_minutes` | `60` | option-chain refresh cadence; with the scanner enabled, the effective cadence is the shorter of this and `opportunity.refresh_minutes` |
| `market_data.universe_stocks` | `[]` | optional additional allow-list matched against the live Dhan master. With the scanner enabled, the base universe is the validated symbol list in `config/universe.json`; an explicit non-empty value intersects that list. With the scanner disabled, `[]` retains the legacy full F&O universe |
| `strategy.itm_strikes_per_side` | `6` | ATM + N in-the-money strikes per side (CE below ATM, PE above ATM). Open positions are always monitored even when they fall outside this width |
| `strategy.expiry_switch_day` | `24` | premium-preservation switch day |
| `opportunity.enabled` | `true` (Dhan only) | Phase 2–5 scanner eligibility filter; `false` restores the legacy ATM/ITM candidate set; mock mode disables it because no real option-chain book is available |
| `opportunity.refresh_minutes` | `15` | scanner re-ranking cadence; active positions remain monitored regardless |
| `opportunity.underlying_history_days` | `90` | lookback requested for Phase 4 NSE_EQ 15-minute bars (only completed bars are scored) |
| `opportunity.weights` | `0.40 / 0.30 / 0.20 / 0.10` | liquidity / volatility / chain quality / execution quality scanner weights |
| `opportunity.selection` | `10 / 14 / 10` | max stocks / contracts per stock / top opportunities |
| `opportunity.execution` | spread `0.30–0.50%`, quote age `60s` | execution-quality spread and freshness normalization (not fill/slippage prediction) |
| `paper.fill_mode` | `marketable_quote` | Required by the persistent app: crosses fresh top of book (SELL at bid, BUY at ask). Non-marketable diagnostic modes are rejected by `TraderApp`. |
| `paper.slippage_bps` | `5` | adverse slippage per side, rounded against the trader to contract tick |
| `paper.max_quote_age_seconds` | `60` | maximum age for quote-based fills/marks; a missing/stale book is labeled and uses the documented signal-close fallback |
| `paper.quote_fallback` | `signal_candle_close` | explicit fallback for strategy fills when no fresh two-sided book exists |
| `paper.transaction_cost_per_order` / `transaction_cost_bps` | `0` / `0` | optional paper cost assumptions; no exchange/tax fees are invented |
| `paper.margin_estimate_pct_of_underlying_notional` | `20` | indicative estimate only, not official Dhan/SPAN margin |
| `paper.report_dir` | `reports/paper` | daily JSON/Markdown/CSV plus multi-day position/trade/error logs |
| `paper.report_refresh_seconds` | `300` | periodic report refresh in addition to scan/signal/shutdown updates |
| `paper.exit_retry_seconds` | `30` | retry interval for an unfilled 15:15 paper mechanical exit while the session remains open |
| `risk.quantity_per_trade` | `null` | `null` = use the contract's lot size from the instrument master |
| `risk.max_open_positions` | `8` | entry gate |
| `risk.max_trades_per_day` | `20` | entry gate |
| `risk.max_daily_loss` | `25000` | entry gate (realized today + unrealized at LTP) |
| `dashboard.*` | `:9000` | dashboard host/port; `control_token` guards the control buttons |

## 5. Paper fill convention and audit

The default app setting is `paper.fill_mode=marketable_quote`. For each entry
SELL, a fresh timestamped Dhan two-sided book is crossed at its best bid; for
each exit BUY, it is crossed at its best ask. No midpoint fill is assumed.
Configured adverse slippage (5 bps per side by default) is then applied and
tick-rounded against the trader. The quote freshness limit is 60 seconds by
default.

If a strategy fill has no valid fresh two-sided book, it uses the signal
candle's close plus adverse slippage and records
`SIGNAL_CANDLE_CLOSE_FALLBACK_NO_FRESH_BOOK`; stale observed bid/ask and quote
age remain recorded when available. Manual paper exits may use a fresh real LTP
when a two-sided book is absent, and are labeled as an explicit LTP fallback.
Missing quotes are never invented. Optional configured transaction costs are
reported separately: the underlying position P&L stays gross, while daily
reports also provide net P&L after configured costs. The margin field is an
indicative configurable percentage of underlying notional, not official margin.

Each `paper_execution_events` row stores signal/fill prices and timestamps,
bid, ask, spread, quote timestamp/age, execution method, slippage, quantity,
lot size, AVWAP, configured transaction cost, underlying spot, and indicative
margin. The journal, `orders` table (paper-virtual rows only), and position
records retain the execution lifecycle. `python -m paper_trading report` can
regenerate all daily reports and multi-day CSV logs after restart.

## 6. LIVE trading is disabled in Phase 8

There is no LIVE option in this phase. Setting `trading_mode=LIVE`, exporting
`TRADING_MODE=LIVE`, or passing `--mode LIVE` to `main.py` exits with an error
before broker construction. `TraderApp` independently raises on any
non-PAPER mode. The legacy `--i-understand-live` flag is ignored; it cannot
unlock orders. The app does not import or instantiate `DhanBroker`/`DhanOrders`,
does not call order placement, does not poll live order status, and does not
reconcile brokerage positions. Wait for Phase 9 and explicit authorization.

## 7. Data & storage

SQLite (`data/trader.db`, WAL mode). Tables:

* `candles` — completed 15-min candles + AVWAP after each candle
* `avwap_state` — **the** cumulative AVWAP state per contract (restart-proof)
* `signals` — every signal, unique key `(contract, candle, action)`
* `positions` — full trade records incl. entry/exit AVWAP, reasons, P&L
* `orders` — paper-virtual order lifecycle only; no Dhan order requests
* `paper_execution_events` — quote/fill audit and execution assumptions
* `paper_scanner_runs`, `paper_scanner_candidates` — timestamped scan decisions
* `paper_position_marks` — timestamped quote marks and unrealized P&L
* `paper_errors`, `paper_daily_reports` — recovery/error log and report index
* `journal` — permanent audit trail (SIGNAL, PAPER_ENTRY, PAPER_EXIT,
  ENTRY_BLOCKED, PAPER_ERROR, recovery events, …)
* `kv` — risk flags, startup state, and paper-session restart heartbeat

Restart recovery: load DB → restore open paper positions (adopt contracts that
left the scanner) → reconstruct incomplete fill-audit rows without inventing
quotes → load AVWAP states → resume. Re-ranking never replaces an open position.
Already-consumed candles are never re-evaluated (three layers: in-memory
candle dedupe, persisted signal keys, AVWAP-state idempotency).

## 8. Risk controls (separate from the strategy)

The strategy engine only sees AVWAP crosses. Before a signal becomes a paper
fill, the risk gate may block it: max open positions, max trades/day, max daily
loss, `disable_new_entries` (dashboard switch), or `emergency_stop` (dashboard
button — blocks new entries; **exits always work**). `PAPER EXIT ALL` is an
operational paper-only override recorded in the journal; it is not part of the
strategy logic and cannot send a live order.

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
python -m pytest -q
```

The audited workspace's full suite passed 205 tests on 2026-10-01; run the
command above after installing dependencies to verify your local checkout.

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
* Phase 6 integration: Scanner OFF/ON AVWAP equivalence for identical candles,
  option-history priming and first-valid completed-candle timing, fail-closed
  eligibility/position union, Phase 1 master-snapshot reuse, Dhan NSE_EQ history
  payload, quote-book parsing/rate pacing, and evaluation-record persistence
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
  shared 1 request/s pacing; LTP, day volume, F&O OI, and supplied market depth
  are timestamped on receipt. Depth/book fields are used only as current data.
* Order-placement endpoints are not used or called by the Phase 8 app. The
  legacy Dhan order adapter exists only as dormant code for a separately
  approved later phase.
* Instrument master: `https://images.dhan.co/api-data/api-scrip-master-detailed.csv`
  (the old `…/api/instruments/v2/…/option-instrument-master.csv` URLs are
  dead — they 403 for everyone). Cached under `data/instruments/` (12 h TTL);
  the F&O stock universe is **derived** from it (index/ETF underlyings
  excluded), lot sizes and tick sizes included.
* With the default scanner, real-data bootstrap scans the 20 Phase 1 stocks
  in `config/universe.json`; starting before market open allows time for the
  3 s-paced expiry/chain requests and AVWAP history. Disabling the scanner
  restores the legacy full F&O universe and its longer bootstrap.
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
* Transaction costs default to zero and remain configurable paper assumptions;
  brokerage/taxes are not silently inferred. Compare the report's gross and
  configured net P&L fields accordingly.
* The dashboard is a development-grade Flask server (fine for a single
  trader's machine; put it behind a reverse proxy if exposed).

---

**Faithful implementation first. Observe in PAPER. Then — and only then —
talk about modifications.** (spec §49)
