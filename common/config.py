"""Configuration loading: JSON file + environment overrides.

Priority (highest wins):
  1. command-line overrides applied by main.py
  2. environment variables (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, TRADING_MODE)
  3. config file (config/config.json)
  4. built-in defaults
"""
from __future__ import annotations

import copy
import json
import os

DEFAULTS: dict = {
    "trading_mode": "PAPER",
    "dhan": {
        "client_id": "",
        "access_token": "",
        "rest_base_url": "https://api.dhan.co",
        "http_timeout_seconds": 10,
        "instrument_master_ttl_hours": 12,
    },
    "market_data": {
        "source": "dhan",
        "mode": "candle_poll",
        "loop_tick_seconds": 1.0,
        "boundary_grace_seconds": 8,
        "ltp_poll_seconds": 30,
        "quote_batch_size": 1000,
        "history_start": "month_start",
        "history_lookback_days_fallback": 5,
        "universe_refresh_minutes": 60,
        "universe_stocks": [],
        "universe_indices": [],
        "weekly_expiries": {},
        "mock": {
            "underlyings": ["MOCKA", "MOCKB", "MOCKC"],
            "speed": 240,
        },
    },
    "strategy": {
        "candle_interval_minutes": 15,
        "itm_strikes_per_side": 6,
        "expiry_switch_day": 24,
    },
    "paper": {
        "fill_mode": "candle_close",
        "slippage_bps": 0,
        "next_quote_timeout_seconds": 45,
    },
    "live": {
        "product_type": "MARGIN",
        "order_type": "MARKET",
        "limit_price_offset_ticks": 1,
        "order_poll_seconds": 3,
        "confirm_phrase": "I_UNDERSTAND_LIVE_TRADING",
    },
    "risk": {
        "quantity_per_trade": None,
        "max_open_positions": 8,
        "max_trades_per_day": 20,
        "max_daily_loss": 25000,
        "default_lot_size": 250,
    },
    "storage": {
        "db_path": "data/trader.db",
        "instrument_cache_dir": "data/instruments",
    },
    "dashboard": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 9000,
        "control_token": "",
    },
    "logging": {
        "level": "INFO",
        "file": "logs/trader.log",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str = "config/config.json") -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg = _deep_merge(cfg, user_cfg)
    else:
        if path:
            print(f"[config] WARNING: {path} not found; using defaults.")

    # Environment overrides
    env = os.environ
    if env.get("DHAN_CLIENT_ID"):
        cfg["dhan"]["client_id"] = env["DHAN_CLIENT_ID"]
    if env.get("DHAN_ACCESS_TOKEN"):
        cfg["dhan"]["access_token"] = env["DHAN_ACCESS_TOKEN"]
    if env.get("TRADING_MODE"):
        cfg["trading_mode"] = env["TRADING_MODE"].upper()
    if env.get("AVWAP_DB_PATH"):
        cfg["storage"]["db_path"] = env["AVWAP_DB_PATH"]

    cfg["trading_mode"] = str(cfg.get("trading_mode", "PAPER")).upper()
    if cfg["trading_mode"] not in ("PAPER", "LIVE"):
        raise ValueError(
            f"trading_mode must be PAPER or LIVE, got {cfg['trading_mode']!r}. "
            "Refusing to start (never silently fall back)."
        )
    return cfg


def get(cfg: dict, dotted: str, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
