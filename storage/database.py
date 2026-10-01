"""SQLite persistence layer.

Tables: candles, avwap_state, signals, positions, orders, journal, kv.
All timestamps stored as epoch seconds (IST wall time). Thread-safe via a lock;
WAL mode so the dashboard can read while the engine writes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    security_id TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      INTEGER NOT NULL DEFAULT 0,
    avwap       REAL,
    PRIMARY KEY (security_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles (ts);

CREATE TABLE IF NOT EXISTS avwap_state (
    security_id             TEXT PRIMARY KEY,
    anchor_ts               INTEGER,
    cumulative_price_volume REAL NOT NULL DEFAULT 0,
    cumulative_volume       REAL NOT NULL DEFAULT 0,
    last_candle_ts          INTEGER,
    last_close              REAL,
    last_avwap              REAL,
    symbol                  TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key   TEXT NOT NULL UNIQUE,
    security_id  TEXT NOT NULL,
    action       TEXT NOT NULL,
    candle_ts    INTEGER NOT NULL,
    signal_price REAL NOT NULL,
    avwap        REAL,
    prev_close   REAL,
    prev_avwap   REAL,
    reason       TEXT,
    symbol       TEXT,
    underlying   TEXT,
    strike       REAL,
    option_type  TEXT,
    expiry       TEXT,
    created_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (candle_ts);

CREATE TABLE IF NOT EXISTS evaluations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp          INTEGER NOT NULL,
    candle_ts          INTEGER NOT NULL,
    security_id        TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    option             TEXT NOT NULL,
    option_type        TEXT,
    strike             REAL,
    expiry             TEXT,
    liquidity_score    REAL,
    volatility_score   REAL,
    opportunity_score  REAL,
    scanner_timestamp  INTEGER,
    avwap              REAL,
    option_close       REAL NOT NULL,
    signal             TEXT,
    UNIQUE (security_id, candle_ts)
);
CREATE INDEX IF NOT EXISTS idx_evaluations_ts ON evaluations (timestamp);

CREATE TABLE IF NOT EXISTS positions (
    position_id   TEXT PRIMARY KEY,
    security_id   TEXT NOT NULL,
    symbol        TEXT,
    underlying    TEXT,
    strike        REAL,
    option_type   TEXT,
    expiry        TEXT,
    quantity      INTEGER NOT NULL,
    entry_price   REAL,
    entry_time    INTEGER,
    entry_avwap   REAL,
    entry_reason  TEXT,
    entry_order_id TEXT,
    exit_price    REAL,
    exit_time     INTEGER,
    exit_avwap    REAL,
    exit_reason   TEXT,
    exit_order_id TEXT,
    pnl           REAL,
    status        TEXT NOT NULL,
    mode          TEXT NOT NULL,
    source        TEXT DEFAULT 'system'
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);
CREATE INDEX IF NOT EXISTS idx_positions_sec ON positions (security_id);

CREATE TABLE IF NOT EXISTS orders (
    order_id      TEXT PRIMARY KEY,
    position_id   TEXT,
    security_id   TEXT,
    action        TEXT,
    side          TEXT,
    quantity      INTEGER,
    order_type    TEXT,
    status        TEXT,
    filled_qty    INTEGER DEFAULT 0,
    avg_price     REAL,
    placed_at     INTEGER,
    updated_at    INTEGER,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    position_id TEXT,
    security_id TEXT,
    symbol      TEXT,
    event       TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal (ts);
CREATE INDEX IF NOT EXISTS idx_journal_event_sec ON journal(event, security_id, ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Phase 8 paper-only execution audit: one immutable-ish row per simulated fill.
CREATE TABLE IF NOT EXISTS paper_execution_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id           TEXT NOT NULL UNIQUE,
    position_id        TEXT NOT NULL,
    security_id        TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    timestamp          INTEGER NOT NULL,
    signal_timestamp   INTEGER,
    signal_price       REAL,
    fill_price         REAL,
    bid                REAL,
    ask                REAL,
    spread_percent     REAL,
    slippage_bps       REAL,
    slippage_amount    REAL,
    transaction_cost   REAL,
    quantity           INTEGER NOT NULL,
    lot_size           INTEGER,
    avwap              REAL,
    quote_timestamp    INTEGER,
    quote_age_seconds  REAL,
    execution_method   TEXT,
    underlying_spot    REAL,
    margin_estimate    REAL,
    margin_model       TEXT,
    raw                TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_paper_exec_ts ON paper_execution_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_paper_exec_position ON paper_execution_events(position_id, timestamp);

-- Scanner decisions are persisted before the in-memory ranking can be replaced.
CREATE TABLE IF NOT EXISTS paper_scanner_runs (
    scan_id               TEXT PRIMARY KEY,
    timestamp             INTEGER NOT NULL,
    trading_date          TEXT NOT NULL,
    status                TEXT NOT NULL,
    stocks_scanned        INTEGER NOT NULL DEFAULT 0,
    contracts_scanned     INTEGER NOT NULL DEFAULT 0,
    qualified_contracts  INTEGER NOT NULL DEFAULT 0,
    rejected_contracts   INTEGER NOT NULL DEFAULT 0,
    selected_contracts   INTEGER NOT NULL DEFAULT 0,
    duration_ms           REAL,
    symbols_json          TEXT NOT NULL DEFAULT '[]',
    error                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_scan_ts ON paper_scanner_runs(timestamp);
CREATE INDEX IF NOT EXISTS idx_paper_scan_date ON paper_scanner_runs(trading_date, timestamp);

CREATE TABLE IF NOT EXISTS paper_scanner_candidates (
    scan_id             TEXT NOT NULL,
    security_id         TEXT NOT NULL,
    symbol              TEXT,
    contract_name       TEXT,
    expiry              TEXT,
    strike              REAL,
    option_type         TEXT,
    status              TEXT NOT NULL,
    qualified           INTEGER NOT NULL DEFAULT 0,
    selected_for_avwap  INTEGER NOT NULL DEFAULT 0,
    rank                INTEGER,
    liquidity_score     REAL,
    volatility_score    REAL,
    opportunity_score   REAL,
    spread_percent      REAL,
    quote_age_seconds   REAL,
    rejection_reasons   TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (scan_id, security_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_scan_candidates_status ON paper_scanner_candidates(status);

CREATE TABLE IF NOT EXISTS paper_errors (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         INTEGER NOT NULL,
    trading_date      TEXT NOT NULL,
    session_id        TEXT,
    component         TEXT NOT NULL,
    severity          TEXT NOT NULL DEFAULT 'ERROR',
    code              TEXT NOT NULL,
    message           TEXT NOT NULL,
    recovery_action   TEXT,
    recovered         INTEGER NOT NULL DEFAULT 0,
    context_json      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_paper_errors_ts ON paper_errors(timestamp);
CREATE INDEX IF NOT EXISTS idx_paper_errors_date ON paper_errors(trading_date, timestamp);

CREATE TABLE IF NOT EXISTS paper_position_marks (
    position_id       TEXT PRIMARY KEY,
    security_id       TEXT NOT NULL,
    observed_at       INTEGER NOT NULL,
    last_price        REAL,
    bid               REAL,
    ask               REAL,
    spread_percent    REAL,
    quote_age_seconds REAL,
    avwap             REAL,
    unrealized_pnl    REAL,
    mark_source       TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_marks_observed ON paper_position_marks(observed_at);

CREATE TABLE IF NOT EXISTS paper_daily_reports (
    trading_date TEXT PRIMARY KEY,
    generated_at INTEGER NOT NULL,
    report_dir   TEXT NOT NULL,
    summary_json TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            # migration for DBs created before the `symbol` column existed
            try:
                self._conn.execute("ALTER TABLE avwap_state ADD COLUMN symbol TEXT")
            except sqlite3.OperationalError:
                pass  # column already present
            self._conn.commit()

    # ------------------------------------------------------------------ low
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --------------------------------------------------------------- candles
    def candle_exists(self, security_id: str, ts: int) -> bool:
        rows = self._query(
            "SELECT 1 FROM candles WHERE security_id=? AND ts=?", (security_id, ts)
        )
        return bool(rows)

    def save_candle(self, candle_row: dict) -> None:
        self._exec(
            """INSERT INTO candles (security_id, ts, open, high, low, close, volume, avwap)
               VALUES (:security_id, :ts, :open, :high, :low, :close, :volume, :avwap)
               ON CONFLICT(security_id, ts) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume, avwap=excluded.avwap""",
            candle_row,
        )

    def get_candles(self, security_id: str, after_ts: int = 0, limit: int | None = None):
        sql = (
            "SELECT * FROM candles WHERE security_id=? AND ts>?"
            " ORDER BY ts ASC"
        )
        rows = self._query(sql, (security_id, after_ts))
        if limit:
            rows = rows[:limit]
        return rows

    # ---------------------------------------------------------------- avwap
    def get_avwap_state(self, security_id: str):
        rows = self._query(
            "SELECT * FROM avwap_state WHERE security_id=?", (security_id,)
        )
        return rows[0] if rows else None

    def save_avwap_state(self, state: dict) -> None:
        self._exec(
            """INSERT INTO avwap_state
               (security_id, anchor_ts, cumulative_price_volume, cumulative_volume,
                last_candle_ts, last_close, last_avwap, symbol)
               VALUES (:security_id, :anchor_ts, :cumulative_price_volume,
                       :cumulative_volume, :last_candle_ts, :last_close, :last_avwap,
                       :symbol)
               ON CONFLICT(security_id) DO UPDATE SET
                 anchor_ts=excluded.anchor_ts,
                 cumulative_price_volume=excluded.cumulative_price_volume,
                 cumulative_volume=excluded.cumulative_volume,
                 last_candle_ts=excluded.last_candle_ts,
                 last_close=excluded.last_close,
                 last_avwap=excluded.last_avwap,
                 symbol=excluded.symbol""",
            state,
        )

    # -------------------------------------------------------------- signals
    def signal_exists(self, signal_key: str) -> bool:
        rows = self._query("SELECT 1 FROM signals WHERE signal_key=?", (signal_key,))
        return bool(rows)

    def save_signal(self, sig: dict) -> None:
        try:
            self._exec(
                """INSERT INTO signals (signal_key, security_id, action, candle_ts,
                        signal_price, avwap, prev_close, prev_avwap, reason,
                        symbol, underlying, strike, option_type, expiry, created_at)
                   VALUES (:signal_key, :security_id, :action, :candle_ts,
                           :signal_price, :avwap, :prev_close, :prev_avwap, :reason,
                           :symbol, :underlying, :strike, :option_type, :expiry,
                           :created_at)""",
                sig,
            )
        except sqlite3.IntegrityError:
            pass  # duplicate signal_key -> already handled

    def get_signals(self, limit: int = 100, since_ts: int = 0):
        return self._query(
            "SELECT * FROM signals WHERE candle_ts>=? ORDER BY candle_ts DESC, id DESC LIMIT ?",
            (since_ts, limit),
        )

    def get_signals_for(self, security_id: str, limit: int = 50):
        return self._query(
            "SELECT * FROM signals WHERE security_id=? ORDER BY candle_ts DESC, id DESC LIMIT ?",
            (security_id, limit),
        )

    # ----------------------------------------------------------- evaluations
    def save_evaluation(self, row: dict) -> None:
        """Persist the audit record for one completed-candle AVWAP evaluation.

        The contract/candle key makes retries idempotent; a duplicate delivery
        cannot manufacture an extra evaluation row for the same 15-minute bar.
        """
        self._exec(
            """INSERT INTO evaluations (
                    timestamp, candle_ts, security_id, symbol, option, option_type,
                    strike, expiry, liquidity_score, volatility_score,
                    opportunity_score, scanner_timestamp, avwap, option_close, signal
                ) VALUES (
                    :timestamp, :candle_ts, :security_id, :symbol, :option,
                    :option_type, :strike, :expiry, :liquidity_score,
                    :volatility_score, :opportunity_score, :scanner_timestamp,
                    :avwap, :option_close, :signal
                )
                ON CONFLICT(security_id, candle_ts) DO UPDATE SET
                    timestamp=excluded.timestamp,
                    symbol=excluded.symbol,
                    option=excluded.option,
                    option_type=excluded.option_type,
                    strike=excluded.strike,
                    expiry=excluded.expiry,
                    liquidity_score=excluded.liquidity_score,
                    volatility_score=excluded.volatility_score,
                    opportunity_score=excluded.opportunity_score,
                    scanner_timestamp=excluded.scanner_timestamp,
                    avwap=excluded.avwap,
                    option_close=excluded.option_close,
                    signal=excluded.signal""",
            row,
        )

    def get_evaluations(self, limit: int = 100, since_ts: int = 0):
        return self._query(
            "SELECT * FROM evaluations WHERE timestamp>=? "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (since_ts, limit),
        )

    # ------------------------------------------------------------ positions
    def save_position(self, p: dict) -> None:
        self._exec(
            """INSERT INTO positions (position_id, security_id, symbol, underlying,
                    strike, option_type, expiry, quantity, entry_price, entry_time,
                    entry_avwap, entry_reason, entry_order_id, exit_price, exit_time,
                    exit_avwap, exit_reason, exit_order_id, pnl, status, mode, source)
               VALUES (:position_id, :security_id, :symbol, :underlying, :strike,
                       :option_type, :expiry, :quantity, :entry_price, :entry_time,
                       :entry_avwap, :entry_reason, :entry_order_id, :exit_price,
                       :exit_time, :exit_avwap, :exit_reason, :exit_order_id, :pnl,
                       :status, :mode, :source)
               ON CONFLICT(position_id) DO UPDATE SET
                 exit_price=excluded.exit_price, exit_time=excluded.exit_time,
                 exit_avwap=excluded.exit_avwap, exit_reason=excluded.exit_reason,
                 exit_order_id=excluded.exit_order_id, pnl=excluded.pnl,
                 status=excluded.status, entry_price=excluded.entry_price,
                 entry_time=excluded.entry_time, entry_order_id=excluded.entry_order_id,
                 source=excluded.source""",
            p,
        )

    def get_position(self, position_id: str):
        rows = self._query("SELECT * FROM positions WHERE position_id=?", (position_id,))
        return rows[0] if rows else None

    def get_positions(self, status: str | tuple | list | None = None, limit: int = 500):
        if status:
            if isinstance(status, (tuple, list)):
                marks = ",".join("?" for _ in status)
                rows = self._query(
                    f"SELECT * FROM positions WHERE status IN ({marks}) "
                    "ORDER BY entry_time DESC LIMIT ?",
                    (*status, limit),
                )
            else:
                rows = self._query(
                    "SELECT * FROM positions WHERE status=? ORDER BY entry_time DESC LIMIT ?",
                    (status, limit),
                )
        else:
            rows = self._query(
                "SELECT * FROM positions ORDER BY entry_time DESC LIMIT ?", (limit,)
            )
        return rows

    def find_position_by_security(self, security_id: str, statuses=("OPEN", "PENDING")):
        marks = ",".join("?" for _ in statuses)
        rows = self._query(
            f"SELECT * FROM positions WHERE security_id=? AND status IN ({marks}) "
            "ORDER BY entry_time DESC LIMIT 1",
            (security_id, *statuses),
        )
        return rows[0] if rows else None

    def update_position(self, position_id: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(
            f"UPDATE positions SET {sets} WHERE position_id=?",
            (*fields.values(), position_id),
        )

    # --------------------------------------------------------------- orders
    def save_order(self, o: dict) -> None:
        self._exec(
            """INSERT INTO orders (order_id, position_id, security_id, action, side,
                    quantity, order_type, status, filled_qty, avg_price, placed_at,
                    updated_at, raw)
               VALUES (:order_id, :position_id, :security_id, :action, :side, :quantity,
                       :order_type, :status, :filled_qty, :avg_price, :placed_at,
                       :updated_at, :raw)
               ON CONFLICT(order_id) DO UPDATE SET
                 status=excluded.status, filled_qty=excluded.filled_qty,
                 avg_price=excluded.avg_price, updated_at=excluded.updated_at,
                 raw=excluded.raw""",
            {
                "order_id": o["order_id"],
                "position_id": o.get("position_id"),
                "security_id": o.get("security_id"),
                "action": o.get("action"),
                "side": o.get("side"),
                "quantity": o.get("quantity"),
                "order_type": o.get("order_type"),
                "status": o.get("status"),
                "filled_qty": o.get("filled_qty", 0),
                "avg_price": o.get("avg_price"),
                "placed_at": o.get("placed_at"),
                "updated_at": o.get("updated_at"),
                "raw": json.dumps(o.get("raw", {}), default=str),
            },
        )

    def get_order(self, order_id: str):
        rows = self._query("SELECT * FROM orders WHERE order_id=?", (order_id,))
        return rows[0] if rows else None

    def update_order(self, order_id: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(
            f"UPDATE orders SET {sets} WHERE order_id=?",
            (*fields.values(), order_id),
        )

    def get_orders(self, limit: int = 200, statuses=None):
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = self._query(
                f"SELECT * FROM orders WHERE status IN ({marks}) "
                "ORDER BY placed_at DESC LIMIT ?",
                (*statuses, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM orders ORDER BY placed_at DESC LIMIT ?", (limit,)
            )
        return rows

    def find_order_for_security(self, security_id: str, since_ts: int):
        """Newest order for a contract placed at/after since_ts (V2 signal
        chain: SIGNAL -> ORDER)."""
        rows = self._query(
            "SELECT * FROM orders WHERE security_id=? AND placed_at>=? "
            "ORDER BY placed_at DESC LIMIT 1",
            (security_id, since_ts),
        )
        return rows[0] if rows else None

    def orders_count_since(self, ts: int) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM orders WHERE placed_at>=?", (ts,))
        return int(rows[0]["n"]) if rows else 0

    # --------------------------------------------------------------- journal
    def journal_query(self, limit: int = 200, event: str = None, symbol: str = None,
                      security_id: str = None, since_ts: int = 0, until_ts: int = None):
        sql = "SELECT * FROM journal WHERE ts>=?"
        params: list = [since_ts]
        if until_ts is not None:
            sql += " AND ts<=?"
            params.append(until_ts)
        if event:
            sql += " AND event=?"
            params.append(event)
        if security_id:
            sql += " AND security_id=?"
            params.append(security_id)
        if symbol:
            sql += " AND symbol LIKE ?"
            params.append(f"%{symbol}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self._query(sql, tuple(params))

    def journal_find(self, event: str, security_id: str, ts_from: int, ts_to: int):
        rows = self._query(
            "SELECT * FROM journal WHERE event=? AND security_id=? "
            "AND ts BETWEEN ? AND ? ORDER BY id DESC LIMIT 1",
            (event, security_id, ts_from, ts_to),
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------- kv
    def kv_set(self, key: str, value) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def kv_get(self, key: str, default=None):
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            return default

    # ---------------------------------------------------------- Phase 8 paper
    def record_paper_execution(self, event: dict) -> None:
        """Persist one simulated fill with its actual quote/fallback assumptions."""
        raw = event.get("raw", {})
        self._exec(
            """INSERT INTO paper_execution_events (
                    order_id, position_id, security_id, event_type, timestamp,
                    signal_timestamp, signal_price, fill_price, bid, ask,
                    spread_percent, slippage_bps, slippage_amount,
                    transaction_cost, quantity, lot_size, avwap, quote_timestamp,
                    quote_age_seconds, execution_method, underlying_spot,
                    margin_estimate, margin_model, raw
                ) VALUES (
                    :order_id, :position_id, :security_id, :event_type, :timestamp,
                    :signal_timestamp, :signal_price, :fill_price, :bid, :ask,
                    :spread_percent, :slippage_bps, :slippage_amount,
                    :transaction_cost, :quantity, :lot_size, :avwap, :quote_timestamp,
                    :quote_age_seconds, :execution_method, :underlying_spot,
                    :margin_estimate, :margin_model, :raw
                ) ON CONFLICT(order_id) DO UPDATE SET
                    position_id=excluded.position_id,
                    security_id=excluded.security_id,
                    event_type=excluded.event_type,
                    timestamp=excluded.timestamp,
                    signal_timestamp=excluded.signal_timestamp,
                    signal_price=excluded.signal_price,
                    fill_price=excluded.fill_price,
                    bid=excluded.bid, ask=excluded.ask,
                    spread_percent=excluded.spread_percent,
                    slippage_bps=excluded.slippage_bps,
                    slippage_amount=excluded.slippage_amount,
                    transaction_cost=excluded.transaction_cost,
                    quantity=excluded.quantity, lot_size=excluded.lot_size,
                    avwap=excluded.avwap, quote_timestamp=excluded.quote_timestamp,
                    quote_age_seconds=excluded.quote_age_seconds,
                    execution_method=excluded.execution_method,
                    underlying_spot=excluded.underlying_spot,
                    margin_estimate=excluded.margin_estimate,
                    margin_model=excluded.margin_model, raw=excluded.raw""",
            {
                **{key: event.get(key) for key in (
                    "order_id", "position_id", "security_id", "event_type", "timestamp",
                    "signal_timestamp", "signal_price", "fill_price", "bid", "ask",
                    "spread_percent", "slippage_bps", "slippage_amount",
                    "transaction_cost", "quantity", "lot_size", "avwap",
                    "quote_timestamp", "quote_age_seconds", "execution_method",
                    "underlying_spot", "margin_estimate", "margin_model",
                )},
                "raw": json.dumps(raw, default=str, sort_keys=True),
            },
        )

    def get_paper_execution_events(
        self, *, position_id: str | None = None, start_ts: int = 0,
        end_ts: int | None = None, event_type: str | None = None, limit: int = 100000,
    ) -> list[dict]:
        sql = "SELECT * FROM paper_execution_events WHERE timestamp>=?"
        params: list = [int(start_ts)]
        if end_ts is not None:
            sql += " AND timestamp<=?"
            params.append(int(end_ts))
        if position_id is not None:
            sql += " AND position_id=?"
            params.append(str(position_id))
        if event_type is not None:
            sql += " AND event_type=?"
            params.append(str(event_type))
        sql += " ORDER BY timestamp, id LIMIT ?"
        params.append(int(limit))
        out = []
        for row in self._query(sql, tuple(params)):
            item = dict(row)
            try:
                item["raw"] = json.loads(item.get("raw") or "{}")
            except (TypeError, ValueError):
                item["raw"] = {}
            out.append(item)
        return out

    def get_paper_execution_for_position(self, position_id: str) -> list[dict]:
        return self.get_paper_execution_events(position_id=position_id)

    def record_paper_scan(self, scan: dict, candidates: list[dict] | None = None) -> None:
        """Atomically persist one complete ranking snapshot and its contract rows."""
        candidates = candidates or []
        now = int(scan["timestamp"])
        values = (
            str(scan["scan_id"]), now, str(scan["trading_date"]),
            str(scan.get("status", "completed")), int(scan.get("stocks_scanned", 0)),
            int(scan.get("contracts_scanned", 0)), int(scan.get("qualified_contracts", 0)),
            int(scan.get("rejected_contracts", 0)), int(scan.get("selected_contracts", 0)),
            scan.get("duration_ms"),
            json.dumps(scan.get("symbols", []), default=str, sort_keys=True),
            scan.get("error"),
        )
        candidate_values = []
        for row in candidates:
            candidate_values.append((
                str(scan["scan_id"]), str(row.get("security_id", "")),
                row.get("symbol"), row.get("contract_name"), row.get("expiry"),
                row.get("strike"), row.get("option_type"),
                str(row.get("status", "UNKNOWN")), int(bool(row.get("qualified"))),
                int(bool(row.get("selected_for_avwap"))), row.get("rank"),
                row.get("liquidity_score"), row.get("volatility_score"),
                row.get("opportunity_score"), row.get("spread_percent"),
                row.get("quote_age_seconds"),
                json.dumps(row.get("rejection_reasons", []), default=str),
            ))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """INSERT OR REPLACE INTO paper_scanner_runs (
                        scan_id, timestamp, trading_date, status, stocks_scanned,
                        contracts_scanned, qualified_contracts, rejected_contracts,
                        selected_contracts, duration_ms, symbols_json, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                self._conn.execute(
                    "DELETE FROM paper_scanner_candidates WHERE scan_id=?",
                    (str(scan["scan_id"]),),
                )
                if candidate_values:
                    self._conn.executemany(
                        """INSERT INTO paper_scanner_candidates (
                            scan_id, security_id, symbol, contract_name, expiry,
                            strike, option_type, status, qualified,
                            selected_for_avwap, rank, liquidity_score,
                            volatility_score, opportunity_score, spread_percent,
                            quote_age_seconds, rejection_reasons
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        candidate_values,
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def get_paper_scanner_runs(self, trading_date: str | None = None) -> list[dict]:
        if trading_date is None:
            rows = self._query("SELECT * FROM paper_scanner_runs ORDER BY timestamp, scan_id")
        else:
            rows = self._query(
                "SELECT * FROM paper_scanner_runs WHERE trading_date=? ORDER BY timestamp, scan_id",
                (str(trading_date),),
            )
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["symbols"] = json.loads(item.pop("symbols_json") or "[]")
            except (TypeError, ValueError):
                item["symbols"] = []
            out.append(item)
        return out

    def get_paper_scanner_candidates(
        self, *, scan_id: str | None = None, trading_date: str | None = None,
        limit: int = 100000,
    ) -> list[dict]:
        sql = "SELECT c.* FROM paper_scanner_candidates c"
        params: list = []
        if trading_date is not None:
            sql += " JOIN paper_scanner_runs r ON r.scan_id=c.scan_id WHERE r.trading_date=?"
            params.append(str(trading_date))
        elif scan_id is not None:
            sql += " WHERE c.scan_id=?"
            params.append(str(scan_id))
        sql += " ORDER BY c.scan_id, c.symbol, c.security_id LIMIT ?"
        params.append(int(limit))
        out = []
        for row in self._query(sql, tuple(params)):
            item = dict(row)
            for key in ("qualified", "selected_for_avwap"):
                item[key] = bool(item.get(key))
            try:
                item["rejection_reasons"] = json.loads(item.get("rejection_reasons") or "[]")
            except (TypeError, ValueError):
                item["rejection_reasons"] = []
            out.append(item)
        return out

    def record_paper_error(self, event: dict) -> None:
        self._exec(
            """INSERT INTO paper_errors (
                timestamp, trading_date, session_id, component, severity,
                code, message, recovery_action, recovered, context_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(event["timestamp"]), str(event["trading_date"]),
                event.get("session_id"), str(event.get("component", "runtime")),
                str(event.get("severity", "ERROR")), str(event.get("code", "PAPER_ERROR")),
                str(event.get("message", ""))[:2000], event.get("recovery_action"),
                int(bool(event.get("recovered", False))),
                json.dumps(event.get("context", {}), default=str, sort_keys=True),
            ),
        )

    def get_paper_errors(self, trading_date: str | None = None, limit: int = 10000) -> list[dict]:
        if trading_date is None:
            rows = self._query(
                "SELECT * FROM paper_errors ORDER BY timestamp, id LIMIT ?", (int(limit),)
            )
        else:
            rows = self._query(
                "SELECT * FROM paper_errors WHERE trading_date=? ORDER BY timestamp, id LIMIT ?",
                (str(trading_date), int(limit)),
            )
        out = []
        for row in rows:
            item = dict(row)
            item["recovered"] = bool(item.get("recovered"))
            try:
                item["context"] = json.loads(item.pop("context_json") or "{}")
            except (TypeError, ValueError):
                item["context"] = {}
            out.append(item)
        return out

    def save_paper_position_mark(self, mark: dict) -> None:
        self._exec(
            """INSERT INTO paper_position_marks (
                position_id, security_id, observed_at, last_price, bid, ask,
                spread_percent, quote_age_seconds, avwap, unrealized_pnl, mark_source
            ) VALUES (
                :position_id, :security_id, :observed_at, :last_price, :bid, :ask,
                :spread_percent, :quote_age_seconds, :avwap, :unrealized_pnl, :mark_source
            ) ON CONFLICT(position_id) DO UPDATE SET
                security_id=excluded.security_id, observed_at=excluded.observed_at,
                last_price=excluded.last_price, bid=excluded.bid, ask=excluded.ask,
                spread_percent=excluded.spread_percent,
                quote_age_seconds=excluded.quote_age_seconds, avwap=excluded.avwap,
                unrealized_pnl=excluded.unrealized_pnl, mark_source=excluded.mark_source""",
            mark,
        )

    def get_paper_position_marks(self) -> dict[str, dict]:
        return {str(row["position_id"]): dict(row)
                for row in self._query("SELECT * FROM paper_position_marks")}

    def save_paper_daily_report(self, trading_date: str, generated_at: int,
                                report_dir: str, summary: dict) -> None:
        self._exec(
            """INSERT INTO paper_daily_reports (trading_date, generated_at, report_dir, summary_json)
               VALUES (?, ?, ?, ?) ON CONFLICT(trading_date) DO UPDATE SET
                 generated_at=excluded.generated_at, report_dir=excluded.report_dir,
                 summary_json=excluded.summary_json""",
            (str(trading_date), int(generated_at), str(report_dir),
             json.dumps(summary, default=str, sort_keys=True)),
        )

    def get_paper_daily_reports(self, limit: int = 1000) -> list[dict]:
        rows = self._query(
            "SELECT * FROM paper_daily_reports ORDER BY trading_date DESC LIMIT ?",
            (int(limit),),
        )
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["summary"] = json.loads(item.pop("summary_json") or "{}")
            except (TypeError, ValueError):
                item["summary"] = {}
            out.append(item)
        return out

    def get_paper_trading_dates(self) -> list[str]:
        """Return all IST dates represented by scanner, execution, position, error, or report data."""
        rows = self._query(
            """SELECT trading_date FROM paper_scanner_runs
               UNION SELECT trading_date FROM paper_errors
               UNION SELECT date(timestamp, 'unixepoch', '+5 hours', '+30 minutes')
                     FROM paper_execution_events
               UNION SELECT date(entry_time, 'unixepoch', '+5 hours', '+30 minutes')
                     FROM positions WHERE mode='PAPER' AND entry_time IS NOT NULL
               UNION SELECT date(exit_time, 'unixepoch', '+5 hours', '+30 minutes')
                     FROM positions WHERE mode='PAPER' AND exit_time IS NOT NULL
               UNION SELECT trading_date FROM paper_daily_reports
               ORDER BY 1"""
        )
        return [str(row["trading_date"]) for row in rows if row["trading_date"]]

    # ----------------------------------------------------------- analytics
    def trades_today_count(self, ist_day_str: str) -> int:
        """Number of entries placed on the given IST day (YYYY-MM-DD)."""
        rows = self._query(
            "SELECT COUNT(*) AS c FROM positions WHERE status != 'REJECTED' "
            "AND date(entry_time, 'unixepoch', '+5 hours', '+30 minutes') = ?",
            (ist_day_str,),
        )
        return int(rows[0]["c"]) if rows else 0

    def realized_pnl_today(self, ist_day_str: str) -> float:
        rows = self._query(
            "SELECT COALESCE(SUM(pnl), 0) AS s FROM positions "
            "WHERE status='CLOSED' AND exit_time IS NOT NULL "
            "AND date(exit_time, 'unixepoch', '+5 hours', '+30 minutes') = ?",
            (ist_day_str,),
        )
        return float(rows[0]["s"]) if rows else 0.0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
