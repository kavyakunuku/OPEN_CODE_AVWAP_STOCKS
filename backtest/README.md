# Phase 7 historical replay

The Phase 7 runner compares the production legacy AVWAP selector with the
Phase 2–5 point-in-time discovery, liquidity, volatility, and opportunity-ranking
pipeline. It is strictly read-only: it does not instantiate the app, a broker,
a Dhan client, paper execution, or live execution.

## Data availability

No suitable historical point-in-time option archive is checked into this
repository. `data/` is ignored, and current Dhan chain/quote endpoints are not
historical quote or depth archives. Historical candles alone are insufficient
to measure past bid/ask spread, market depth, IV, OI, or chain activity.

Supply an externally archived dataset using the schema below. The loader fails
closed when required files, timestamps, or fields are missing or structurally
unsafe. Empty quote fields stay missing; the runner does not create quotes,
IV, OI, depth, or option-candle history.

## Archive layout

Place these files together, for example under the ignored directory
`data/phase7/`:

### `manifest.json`

```json
{
  "schema_version": 1,
  "timezone": "Asia/Kolkata",
  "interval_minutes": 15,
  "symbols": ["RELIANCE", "ICICIBANK"],
  "period_start": "2025-01-01T09:30:00+05:30",
  "period_end": "2025-01-31T15:15:00+05:30",
  "expiry_switch_day": 24,
  "source_description": "Document the actual capture source and coverage here"
}
```

For a study run, `symbols` must be the same validated 20-stock universe for
both arms. Small symbol sets are useful for tests only. `period_start` and
`period_end` are optional; when omitted, the first and last `scan_events.csv`
timestamps define the evaluation window. If supplied, both must use ISO-8601
with an explicit timezone offset and contain all scan events.

### `contract_master.csv`

Required columns:

```text
captured_at,security_id,symbol,underlying,strike,option_type,expiry,lot_size,instrument,exchange,exchange_segment,tick_size,contract_birth_date,expiry_flag,strike_interval
```

`captured_at` is when the master snapshot was actually observed, not an
inferred effective date. Store a complete snapshot at each capture time. A
replay can use only the latest snapshot captured at or before its timestamp;
a later/current master is never substituted. `contract_birth_date` must be the
option contract's own date if known—never the underlying's listing date.

### `scan_events.csv`

```text
scan_id,as_of
scan-0001,2025-01-02T09:30:00+05:30
```

Use one row for each scanner decision timestamp. The same event evaluates all
symbols from the manifest in both arms. Timestamps must be unique and
strictly comparable; ISO-8601 offsets are mandatory (epoch seconds/milliseconds
are also accepted by CSV inputs).

### `option_candles.csv`

```text
security_id,ts,open,high,low,close,volume,is_first_tradable
```

These are 15-minute **option-contract** candles; `ts` is the candle start and
must lie on the NSE 09:15–15:30 grid. A candle is available only at its close.
For each contract with a complete archive, set `is_first_tradable=true` on
exactly its first tradable option candle and `false` on every other row. The
runner excludes any contract without this explicit confirmation from AVWAP
trades; it does not silently anchor to the first candle supplied in a partial
history window. Include earlier bars as warm-up history when available.

### `underlying_candles.csv`

```text
symbol,security_id,ts,open,high,low,close,volume
```

Underlying 15-minute candles for Phase 4 volatility. `security_id` is optional.
Only completed candles are passed to the volatility engine. Full prior sessions
are needed for reliable daily aggregates; partial sessions are not turned into
daily bars.

### `market_snapshots.csv`

```text
source,observed_at,symbol,underlying,expiry,security_id,strike,option_type,spot,last_price,volume,open_interest,oi_change,bid,ask,bid_quantity,ask_quantity,bid_depth_json,ask_depth_json,implied_volatility
```

`source` is one of `chain`, `option_quote`, or `underlying_quote`. Keep each
capture's real timestamp. For a chain capture, write one row per actual option
contract and include the observed underlying `spot`; rows for a given
(underlying, expiry, capture time) make up that captured chain. Option/equity
quote observations are separate rows. Quote and depth history must be genuinely
archived; do not copy a present-day chain into historical timestamps.

Depth columns are optional JSON arrays such as
`[{"price": 12.3, "quantity": 500}]`. Missing or padded zero-price levels
remain empty. Spread thresholds are percentages: `0.50` means 0.50%, not 50%.

The archive should preserve actual volume/OI semantics from its source
(e.g. cumulative day volume versus a candle's traded volume) in its
`source_description`; the loader does not reinterpret or backfill them. It
reports missing 15-minute starts only between the first and last observed bar
for each instrument/session. Such gaps may represent no trades or a capture
gap; the loader does not infer which, fill them, or count leading/trailing
coverage as complete.

## Commands

```bash
# Confirm the current local archive is missing or validate a supplied archive.
python -m backtest inspect --data data/phase7

# Run the baseline and all 16 liquidity/spread combinations.
python -m backtest run --data data/phase7 --out reports/phase7

# Optionally use only strategy, risk, scanner, and backtest settings from JSON.
python -m backtest run --data data/phase7 --out reports/phase7 --config config/config.example.json
```

The default new-system sweep is liquidity-score minimums 50/60/70/80 crossed
with maximum spread thresholds 0.25%/0.50%/0.75%/1.00%. The primary Phase 3
liquidity gate is applied before Phase 4 volatility and Phase 5 ranking.
Phase 5 weights stay configurable (default 40% liquidity, 30% volatility,
20% chain quality, 10% execution quality).

## Execution assumptions and reports

Both arms share the production 15-minute option-chart AVWAP formula and
entry/exit rules, monthly expiry switch, contract lot sizing/fixed quantity,
risk caps, 15:15 IST mechanical exit, fill mode, transaction costs, and
slippage. Strategy-signal fills use the completed signal candle close with 0 bps
slippage and zero configured transaction fees by default; this is optimistic
because a signal is available only after that candle closes, and the replay has
no post-signal quote/depth execution simulation. These assumptions are printed
in `assumptions.json`. Fees are not silently populated from the repository's
approximate historical-charge utility. Set explicit `backtest` settings in
JSON to model nonzero costs/slippage. Only `candle_close` is accepted in this
Phase 7 implementation.

The 15:15 mechanical exit uses the latest actual option quote captured at or
before the exit only when it is within `backtest.max_input_age_seconds`
(default 60 seconds). Otherwise it may use the last previously completed,
same-day option candle within `backtest.max_exit_candle_age_seconds` (default
1800 seconds), matching the app's pre-window-processing fallback. If neither
reference exists, the simulated position remains open/unpriced; entry price is
never substituted as an exit. Same-timestamp replay ordering is mechanical
exit, completed-candle processing, then scanner refresh, matching `app._tick`;
new entries at or after 15:15 are blocked.

Output includes comparison and threshold tables; daily scanner, trade,
rejection, and missed-signal CSVs; data-quality and look-ahead validation;
plus separate counterfactual trades for AVWAP signals rejected by liquidity.
Counterfactual P&L is not included in actual strategy results. No system is
selected automatically as a winner. Paper and live trading remain disabled.
