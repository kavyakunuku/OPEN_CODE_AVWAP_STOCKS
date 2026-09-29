"""V2 dashboard backend tests:

  * candle persistence (candles table + AVWAP-at-candle) - the substrate for
    contract charts / replay
  * REST telemetry (latency / 429 / errors) for System & Data Health
  * journal query filters (Journal + Alert pages)
  * manual position close (Positions page control)
  * AVWAP rebuild queue (Data Health control, applied at bootstrap)
  * /api/state V2 payload shape + /api/contract + /api/journal endpoints
    + token-gated control actions
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.models import Candle  # noqa: E402
from common.utils import IST, epoch  # noqa: E402
from conftest import C  # noqa: E402
from dhan.client import DhanAPIError, DhanREST  # noqa: E402


# --------------------------------------------------------------------- utils
class FrozenClock:
    def __init__(self, t: datetime):
        self.t = t

    def now(self) -> datetime:
        return self.t

    def advance_to(self, t: datetime) -> None:
        self.t = t


def make_app(tmp_path, at: datetime):
    """TraderApp on the MOCK feed with a frozen clock at `at` (IST)."""
    import copy
    from app import TraderApp

    cfg = {
        "trading_mode": "PAPER",
        "market_data": {
            "source": "mock",
            "loop_tick_seconds": 1,
            "boundary_grace_seconds": 5,
            "ltp_poll_seconds": 30,
            "universe_refresh_minutes": 60,
            "mock": {"underlyings": ["MOCKA"], "speed": 240},
        },
        "strategy": {
            "candle_interval_minutes": 15,
            "itm_strikes_per_side": 4,
            "expiry_switch_day": 24,
        },
        "risk": {
            "quantity_per_trade": 250, "max_open_positions": 20,
            "max_trades_per_day": 50, "max_daily_loss": 100000,
            "default_lot_size": 250,
        },
        "paper": {"fill_mode": "candle_close", "slippage_bps": 0},
        "storage": {"db_path": str(tmp_path / "v2.db"),
                     "instrument_cache_dir": str(tmp_path / "inst")},
        "logging": {"level": "ERROR"},
    }
    app = TraderApp(cfg)
    clock = FrozenClock(at)
    app.clock = clock
    app.feed.clock = clock
    return app, clock


# --------------------------------------------------------------- candle persist
def test_candle_persisted_with_avwap(tmp_path):
    at = datetime(2026, 9, 15, 11, 0, tzinfo=IST)  # Monday, mid-session
    app, clock = make_app(tmp_path, at)
    app.bootstrap()

    contracts = app.universe.all_scanner_contracts()
    assert contracts
    target = contracts[0].security_id
    hist = app.feed._history.get(target, [])
    assert hist, "mock feed should have history"
    last = hist[-1]

    # 1) direct persistence path (the normal engine path also calls this)
    app._persist_candle(target, last)
    rows = app.db.get_candles(target)
    assert any(r["ts"] == last.ts and abs(r["avwap"] - app.db.get_avwap_state(target)["last_avwap"]) < 1e-9
               for r in rows), "persisted candle must carry the AVWAP computed including it"
    assert app._last_vol.get(target) == int(last.volume)
    assert app._last_candle_ts >= last.ts

    # 2) full path: a NEW closed window flows through the engine and persists
    new_candle = C(target, 15, 11, 0, close=last.close * 1.01, vol=77,
                   open_=last.close, high=last.close * 1.02, low=last.close * 0.99)
    assert new_candle.ts > last.ts
    real_closed_candle = app.feed.closed_candle

    def fake_closed_candle(sec, win_ts):
        if sec == target and win_ts == new_candle.ts:
            return new_candle
        return None

    app.feed.closed_candle = fake_closed_candle
    try:
        app._processed[target] = last.ts  # pretend nothing newer was processed
        clock.advance_to(datetime(2026, 9, 15, 11, 16, tzinfo=IST))
        app._process_closed_windows(clock.t)
    finally:
        app.feed.closed_candle = real_closed_candle

    st = app.db.get_avwap_state(target)
    assert st["last_candle_ts"] == new_candle.ts, "engine consumed the new candle"
    rows = app.db.get_candles(target)
    match = [r for r in rows if r["ts"] == new_candle.ts]
    assert match, "new closed candle must be persisted"
    assert abs(match[0]["avwap"] - st["last_avwap"]) < 1e-9
    assert match[0]["volume"] == 77
    assert app._last_vol[target] == 77


# -------------------------------------------------------------------- REST stats
class FakeResp:
    def __init__(self, code=200, text='{"ok": 1}'):
        self.status_code = code
        self.text = text

    def json(self):
        return {"ok": 1}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, *a, **k):
        self.calls += 1
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def test_rest_stats_success_and_429(monkeypatch):
    monkeypatch.setattr("dhan.client.time.sleep", lambda s: None)
    rest = DhanREST("1000", "token")

    # success
    sess = FakeSession([FakeResp(200)])
    rest._session = sess
    out = rest.post("/v2/x")
    assert out == {"ok": 1}
    s = rest.stats()
    assert s["requests"] == 1 and s["errors"] == 0
    assert s["last_success_ts"] > 0 and s["avg_latency_ms"] >= 0

    # 429 then success (retry)
    sess = FakeSession([FakeResp(429, "slow down"), FakeResp(200)])
    rest._session = sess
    out = rest.post("/v2/y", retries=1)
    assert out == {"ok": 1}
    s = rest.stats()
    assert s["requests"] == 3 and s["h429"] == 1 and s["errors"] == 0
    assert s["by_path"]["/v2/y"]["h429"] == 1

    # hard 401 (no retry on 4xx) -> error counted + last_error set
    sess = FakeSession([FakeResp(401, '{"808":"Authentication Failed"}')])
    rest._session = sess
    with pytest.raises(DhanAPIError):
        rest.post("/v2/z", retries=1)
    s = rest.stats()
    assert s["errors"] == 1
    assert "401" in s["last_error"] or "Authentication" in s["last_error"]


# ------------------------------------------------------------- journal queries
def test_journal_query_filters(tmp_path):
    from storage.database import Database
    from storage.journal import Journal

    db = Database(str(tmp_path / "j.db"))
    j = Journal(db)
    for i in range(5):
        j.write("SIGNAL", ts=1000 + i, symbol=f"CON{i % 3}", security_id=f"S{i}",
                detail={"n": i})
    j.write("ORDER_SENT", ts=2000, symbol="CON0", security_id="S0", detail={})

    assert len(j.tail(limit=10)) == 6
    assert len(j.db.journal_query(limit=10, event="SIGNAL")) == 5
    assert len(j.db.journal_query(limit=10, event="ORDER_SENT")) == 1
    # symbol is a LIKE match: CON0 appears 3x (i=0, i=3 signals + the order)
    assert len(j.db.journal_query(limit=10, symbol="CON0")) == 3
    rows = j.db.journal_query(limit=10, security_id="S0")
    assert len(rows) == 2  # SIGNAL i=0 + ORDER_SENT
    found = j.db.journal_find("ORDER_SENT", "S0", 0, 9999)
    assert found is not None and found["event"] == "ORDER_SENT"


# ------------------------------------------------------------- manual close
def test_close_position_manual_paper(tmp_path):
    at = datetime(2026, 9, 15, 11, 0, tzinfo=IST)
    app, clock = make_app(tmp_path, at)
    app.bootstrap()
    contracts = app.universe.all_scanner_contracts()
    c = contracts[0]

    app.positions.open_position(
        contract=c, quantity=250, entry_price=100.0,
        ts=epoch(at), reason="TEST", avwap=101.0, mode="PAPER",
    )
    pos = app.positions.find_by_security(c.security_id)
    assert pos["status"] == "OPEN"

    msg = app.close_position_manual(pos["position_id"])
    assert "closed" in msg.lower()
    pos2 = app.db.get_position(pos["position_id"])
    assert pos2["status"] == "CLOSED"
    assert pos2["exit_reason"] == "MANUAL_CLOSE"
    evs = [r["event"] for r in app.db.journal_query(limit=50, security_id=c.security_id)]
    assert "MANUAL_CLOSE_REQUESTED" in evs

    # already-closed position is refused
    assert "not open" in app.close_position_manual(pos["position_id"])
    # unknown id
    assert "not found" in app.close_position_manual("nope")


# --------------------------------------------------------------- rebuild queue
def test_rebuild_queue_schedule_and_apply(tmp_path):
    at = datetime(2026, 9, 15, 11, 0, tzinfo=IST)
    app, clock = make_app(tmp_path, at)
    app.bootstrap()
    contracts = app.universe.all_scanner_contracts()
    c = contracts[0]
    sec = c.security_id
    assert app.db.get_avwap_state(sec) is not None, "bootstrap should have built state"

    # schedule (as the dashboard would)
    msg = app.schedule_rebuild(sec)
    assert "next" in msg
    assert app.db.kv_get("avwap_rebuild_queue") == [sec]
    # idempotent scheduling
    app.schedule_rebuild(sec)
    assert app.db.kv_get("avwap_rebuild_queue") == [sec]

    # apply (as bootstrap would)
    app._process_rebuild_queue()
    assert app.db.get_avwap_state(sec) is None, "state must be dropped for re-init"
    assert app.db.kv_get("avwap_rebuild_queue") == []
    evs = [r["event"] for r in app.db.journal_query(limit=50, security_id=sec)]
    assert "AVWAP_REBUILD_SCHEDULED" in evs and "AVWAP_REBUILD_APPLIED" in evs

    # a queued sec with no state is also handled cleanly
    app.db.kv_set("avwap_rebuild_queue", ["NEVER_SEEN"])
    app._process_rebuild_queue()
    assert app.db.kv_get("avwap_rebuild_queue") == []


# ------------------------------------------------------------ V2 state payload
def test_state_payload_v2_shape(tmp_path):
    at = datetime(2026, 9, 15, 11, 0, tzinfo=IST)
    app, clock = make_app(tmp_path, at)
    app.bootstrap()

    p = app.state_for_dashboard()
    for key in ("version", "uptime_s", "startup", "feeds", "summary",
                "system", "avwap_health", "orders", "alerts", "settings",
                "scanner", "positions", "signals"):
        assert key in p, f"missing V2 payload key: {key}"
    assert p["version"] and p["system"]["next_candle_close"] > 0
    assert isinstance(p["alerts"], list) and isinstance(p["orders"], list)
    assert p["startup"]["timeline"], "startup timeline must be populated"
    assert p["avwap_health"]["monitored"] > 0
    assert p["avwap_health"]["complete"] + len(p["avwap_health"]["partial"]) == \
        p["avwap_health"]["monitored"]
    assert p["settings"]["trading_mode"] == "PAPER"
    assert p["settings"]["strategy"]["itm_strikes_per_side"] == 4
    # scanner rows carry the id the UI needs for row-click navigation
    assert all("security_id" in r for r in p["scanner"])
    # signals carry their evidence chain
    for s in p["signals"]:
        assert "chain" in s and "order" in s["chain"]


def test_contract_payload_and_flask_endpoints(tmp_path):
    at = datetime(2026, 9, 15, 11, 0, tzinfo=IST)
    app, clock = make_app(tmp_path, at)
    app.bootstrap()
    contracts = app.universe.all_scanner_contracts()
    c = contracts[0]
    app._persist_candle(c.security_id, app.feed._history[c.security_id][-1])

    # contract payload (backs the detail page + chart)
    cp = app.contract_payload(c.security_id)
    assert cp["contract"]["symbol"] and cp["candles"], "candles feed the chart"
    assert cp["avwap"]["anchor_ts"] > 0

    from dashboard.app import create_dashboard
    flask = create_dashboard(app, control_token="tok")
    client = flask.test_client()

    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.get_json()
    assert body["version"] and "system" in body

    r = client.get(f"/api/contract/{c.security_id}")
    assert r.status_code == 200
    assert r.get_json()["contract"]["symbol"]

    r = client.get("/api/journal?limit=5")
    assert r.status_code == 200
    assert isinstance(r.get_json(), list)

    # control: bad token refused
    r = client.post("/api/control", json={"action": "rebuild_avwap",
                                           "security_id": c.security_id,
                                           "token": "wrong"})
    assert r.status_code == 403
    # control: rebuild with token ok
    r = client.post("/api/control", json={"action": "rebuild_avwap",
                                           "security_id": c.security_id,
                                           "token": "tok"})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert app.db.kv_get("avwap_rebuild_queue") == [c.security_id]

    # control: close_position missing id -> 400
    r = client.post("/api/control", json={"action": "close_position", "token": "tok"})
    assert r.status_code == 400
