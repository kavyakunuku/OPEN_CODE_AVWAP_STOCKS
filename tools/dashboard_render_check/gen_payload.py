"""Generate realistic V2 dashboard payloads from a mock TraderApp, plus the
rendered HTML - inputs for the jsdom render harness."""
"""Usage: python tools/dashboard_render_check/gen_payload.py [out_dir]
       (default out_dir: a fresh temp dir; then run render_check.js on it)
Requires: repo dependencies + `import tests.conftest` working (i.e. run from
anywhere; paths are resolved relative to this file)."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
os.chdir(REPO)

from common.utils import IST, epoch  # noqa: E402
from app import TraderApp  # noqa: E402
from dashboard.app import PAGE_HTML  # noqa: E402

TMP = Path(sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "v2h"))
TMP.mkdir(parents=True, exist_ok=True)

cfg = {
    "trading_mode": "PAPER",
    "market_data": {
        "source": "mock",
        "loop_tick_seconds": 1,
        "boundary_grace_seconds": 5,
        "ltp_poll_seconds": 30,
        "universe_refresh_minutes": 60,
        "universe_indices": ["MOCKIDX"],
        "weekly_expiries": {},
        "mock": {"underlyings": ["MOCKA", "MOCKB"], "speed": 240},
    },
    "strategy": {"candle_interval_minutes": 15, "itm_strikes_per_side": 4,
                  "expiry_switch_day": 24},
    "risk": {"quantity_per_trade": 250, "max_open_positions": 20,
              "max_trades_per_day": 50, "max_daily_loss": 100000,
              "default_lot_size": 250},
    "paper": {"fill_mode": "candle_close", "slippage_bps": 0},
    "live": {"product_type": "MARGIN", "order_type": "MARKET",
              "limit_price_offset_ticks": 1, "order_poll_seconds": 3,
              "confirm_phrase": "X"},
    "storage": {"db_path": f"{TMP}/h.db", "instrument_cache_dir": f"{TMP}/inst"},
    "logging": {"level": "ERROR"},
    "dashboard": {"port": 8000},
}
app = TraderApp(cfg)
clock_t = datetime(2026, 9, 15, 11, 0, tzinfo=IST)
from tests.test_v2_dashboard import FrozenClock  # noqa: E402
clock = FrozenClock(clock_t)
app.clock = clock
app.feed.clock = clock
app.bootstrap()

contracts = app.universe.all_scanner_contracts()
c1 = contracts[0]
c2 = contracts[1]

# persist a few candles for c1 (feeds the chart)
for cd in app.feed._history[c1.security_id][-40:]:
    app._persist_candle(c1.security_id, cd)

# a signal + order + open position (richer Signal/Orders/Positions pages)
last = app.feed._history[c1.security_id][-1]
st = app.db.get_avwap_state(c1.security_id)
app.db.save_signal({
    "signal_key": "harness-1", "security_id": c1.security_id,
    "action": "ENTRY_SELL", "candle_ts": last.ts, "signal_price": last.close,
    "avwap": st["last_avwap"], "prev_close": st["last_close"],
    "prev_avwap": st["last_avwap"] * 0.999, "reason": "CROSS_BELOW_AVWAP",
    "symbol": c1.name, "underlying": c1.underlying, "strike": c1.strike,
    "option_type": c1.option_type, "expiry": str(c1.expiry),
    "created_at": epoch(clock_t),
})
app.db.save_order({
    "order_id": "HORD1", "position_id": None, "security_id": c1.security_id,
    "action": "ENTRY_SELL", "side": "SELL", "quantity": 250,
    "order_type": "MARKET", "status": "FILLED", "filled_qty": 250,
    "avg_price": last.close, "placed_at": epoch(clock_t),
    "updated_at": epoch(clock_t), "raw": json.dumps({"note": "paper virtual fill"}),
})
app.positions.open_position(
    contract=c2, quantity=250, entry_price=50.0, ts=epoch(clock_t) - 3600,
    reason="CROSS_BELOW_AVWAP", avwap=51.0, mode="PAPER",
)
app.journal.write("DATA_ERROR", ts=epoch(clock_t) - 60, symbol=c2.name,
                  security_id=c2.security_id, detail={"err": "harness sample"})
app.journal.write("ENTRY_BLOCKED", ts=epoch(clock_t) - 30, symbol=c1.name,
                  security_id=c1.security_id, detail={"reasons": ["daily_loss_limit (sample)"]})

# a PARTIAL flag for one contract (Data Health page)
app.db.kv_set("avwap_partial_history", [contracts[3].security_id])

# populate the LTP cache the way a real tick would (market is OPEN at 11:00 IST)
app._poll_ltp(clock_t)

state = app.state_for_dashboard()
contract = app.contract_payload(c1.security_id)
journal = app.journal_payload(limit=100)

open(f"{TMP}/page.html", "w").write(PAGE_HTML)
open(f"{TMP}/state.json", "w").write(json.dumps(state, default=str))
open(f"{TMP}/contract.json", "w").write(json.dumps(contract, default=str))
open(f"{TMP}/journal.json", "w").write(json.dumps(journal, default=str))
open(f"{TMP}/contract_id.txt", "w").write(c1.security_id)
print("OK: scanner rows =", len(state["scanner"]),
      "| candles for chart =", len(contract["candles"]),
      "| positions =", len(state["positions"]),
      "| signals =", len(state["signals"]),
      "| orders =", len(state["orders"]),
      "| journal =", len(journal),
      "| alerts =", len(state["alerts"]),
      "| partial =", len(state["avwap_health"]["partial"]))
