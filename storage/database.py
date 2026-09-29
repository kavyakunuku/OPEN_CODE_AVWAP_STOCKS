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
