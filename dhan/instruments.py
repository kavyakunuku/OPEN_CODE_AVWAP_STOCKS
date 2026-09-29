"""Instrument master handling.

PRIMARY source (works, verified 2026-09):
    https://images.dhan.co/api-data/api-scrip-master-detailed.csv

NSE options in that master are the rows with
    EXCH_ID = 'NSE'  AND  INSTRUMENT_TYPE = 'OP'
They carry Dhan security ids, underlying symbol + underlying security id,
strike, CE/PE, expiry (YYYY-MM-DD), lot size and tick size. The INSTRUMENT
column holds the intraday-candle-API code: 'OPTSTK' (stock options) -
each contract is tagged accordingly, because the candle API requires the
matching instrument code.

The F&O stock universe is DERIVED from this master (spec §3):
    every NSE option underlying = eligible F&O STOCKS

A fallback parser for the older "option-instrument-master" CSV format is
kept in case Dhan ships that file again.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from common.models import OptionContract
from common.utils import parse_epoch

log = logging.getLogger("avwap.dhan.instruments")

SCRIP_MASTER_DETAILED_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
OLD_OPTION_MASTER_URL = (
    "https://images.dhan.co/api-data/api/instruments/v2/segment/NSE_FNO/option-instrument-master.csv"
)

def is_stock_underlying(symbol: str) -> bool:
    """All NSE option underlyings are treated as stocks (no index options)."""
    s = (symbol or "").strip().upper()
    if not s:
        return False
    return True


# ---------------------------------------------------------------------------
# Detailed scrip master (primary)
# ---------------------------------------------------------------------------
@dataclass
class MasterBundle:
    contracts: list[OptionContract] = field(default_factory=list)
    underlying_ids: dict[str, str] = field(default_factory=dict)   # symbol -> security id
    tick_sizes: dict[str, float] = field(default_factory=dict)     # security id -> tick


def _parse_tick_size(value: str) -> float:
    """NSE option ticks are 0.05; the master may store ticks in paise
    (e.g. '5.0000'). Normalise defensively."""
    try:
        t = float(value)
    except (TypeError, ValueError):
        return 0.05
    if t <= 0:
        return 0.05
    if t > 1:  # stored in paise
        t /= 100.0
    return t


def _norm_expiry(value: str) -> Optional[str]:
    v = (value or "").strip()
    if not v:
        return None
    if v.isdigit():
        ts = parse_epoch(int(v))
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v[:len(fmt) + 8], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v).strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_scrip_master_detailed(text: str, default_lot_size: int = 250) -> MasterBundle:
    reader = csv.DictReader(io.StringIO(text))
    names = {(f or "").strip().upper() for f in (reader.fieldnames or [])}
    required = {
        "EXCH_ID", "INSTRUMENT_TYPE", "SECURITY_ID", "OPTION_TYPE",
        "STRIKE_PRICE", "SM_EXPIRY_DATE", "LOT_SIZE",
        "UNDERLYING_SYMBOL", "UNDERLYING_SECURITY_ID",
    }
    if not required.issubset(names):
        raise ValueError(
            f"scrip master format changed; missing columns "
            f"{sorted(required - names)}; header: {list(reader.fieldnames or [])[:20]}"
        )

    bundle = MasterBundle()
    seen = set()
    n_rows = 0
    for row in reader:
        try:
            if (row.get("EXCH_ID") or "").strip() != "NSE":
                continue
            # every NSE option has INSTRUMENT_TYPE='OP';
            # the candle-API instrument code lives in the INSTRUMENT column:
            # 'OPTSTK' = stock options
            if (row.get("INSTRUMENT_TYPE") or "").strip() != "OP":
                continue
            n_rows += 1
            opt = (row.get("OPTION_TYPE") or "").strip().upper()
            if opt not in ("CE", "PE"):
                continue
            instrument = (
                "OPTIDX"
                if (row.get("INSTRUMENT") or "").strip().upper() == "OPTIDX"
                else "OPTSTK"
            )
            try:
                strike = float((row.get("STRIKE_PRICE") or "0").strip())
            except ValueError:
                continue
            if strike <= 0:
                continue
            expiry = _norm_expiry(row.get("SM_EXPIRY_DATE") or "")
            if not expiry:
                continue
            sid = (row.get("SECURITY_ID") or "").strip()
            underlying = (row.get("UNDERLYING_SYMBOL") or "").strip().upper()
            if not sid or not underlying:
                continue
            lot = 0
            try:
                lot = int(float((row.get("LOT_SIZE") or "0").strip() or 0))
            except ValueError:
                lot = 0
            if lot <= 0:
                lot = int(default_lot_size)
            uid = (row.get("UNDERLYING_SECURITY_ID") or "").strip()
            if uid:
                bundle.underlying_ids[underlying] = uid

            symbol = (row.get("DISPLAY_NAME") or "").strip()
            if not symbol:
                symbol = (row.get("SYMBOL_NAME") or "").strip() or \
                    f"{underlying} {strike:g} {opt}"
            key = (sid, opt, strike, expiry)
            if key in seen:
                continue
            seen.add(key)
            bundle.contracts.append(
                OptionContract(
                    security_id=sid,
                    symbol=symbol,
                    underlying=underlying,
                    strike=strike,
                    option_type=opt,
                    expiry=expiry,
                    lot_size=lot,
                    instrument=instrument,
                )
            )
            tick = _parse_tick_size(row.get("TICK_SIZE") or "")
            bundle.tick_sizes[sid] = tick
        except Exception:
            continue
    if not bundle.contracts:
        raise ValueError(
            f"scrip master parsed 0 NSE option rows (raw option-like rows: {n_rows})"
        )
    log.info(
        "Scrip master (detailed): %d NSE option rows -> %d contracts, "
        "%d underlyings", len(seen), len(bundle.contracts), len(bundle.underlying_ids)
    )
    return bundle


# ---------------------------------------------------------------------------
# Legacy "option-instrument-master" format (fallback)
# ---------------------------------------------------------------------------
_ALIASES = {
    "security_id": ["securityid", "security_id", "id"],
    "symbol": ["symbol", "displayname", "symbolname"],
    "strike": ["strike", "strikeprice"],
    "expiry": ["expiry", "expirydate", "optionexpirydate", "expiry_date",
               "sm_expiry_date"],
    "option_type": ["type", "optiontype", "option_type", "optionkind"],
    "underlying": ["underlying", "underlyingsymbol", "base", "underlying_symbol"],
    "lot_size": ["lotsize", "lot_size"],
}


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "_")


def parse_legacy_option_master(text: str, default_lot_size: int = 250) -> MasterBundle:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("legacy option master has no header")
    fields = [_norm(f) for f in reader.fieldnames]
    mapping: dict[str, int] = {}
    for canon, aliases in _ALIASES.items():
        for i, f in enumerate(fields):
            if f in aliases:
                mapping[canon] = i
                break
    missing = [c for c in ("security_id", "symbol") if c not in mapping]
    if missing:
        raise ValueError(f"legacy master missing {missing}; header: {reader.fieldnames}")

    bundle = MasterBundle()
    seen = set()
    for row in reader:
        try:
            d = {c: (row.get(v) or "").strip() for c, v in mapping.items()}
            if not d.get("security_id"):
                continue
            opt_type = (d.get("option_type") or "").upper()
            opt_type = "CE" if "CALL" in opt_type or opt_type == "CE" else (
                "PE" if "PUT" in opt_type or opt_type == "PE" else "")
            if opt_type not in ("CE", "PE"):
                continue
            try:
                strike = float(d.get("strike") or 0)
            except ValueError:
                continue
            if strike <= 0:
                continue
            expiry = _norm_expiry(d.get("expiry") or "")
            if not expiry:
                continue
            lot = 0
            try:
                lot = int(float(d.get("lot_size") or 0))
            except ValueError:
                lot = 0
            if lot <= 0:
                lot = int(default_lot_size)
            key = (d["security_id"], opt_type, strike, expiry)
            if key in seen:
                continue
            seen.add(key)
            bundle.contracts.append(
                OptionContract(
                    security_id=d["security_id"],
                    symbol=d.get("symbol") or f"{d.get('underlying','?')} {strike:g} {opt_type}",
                    underlying=(d.get("underlying") or "").upper(),
                    strike=strike,
                    option_type=opt_type,
                    expiry=expiry,
                    lot_size=lot,
                )
            )
        except Exception:
            continue
    if not bundle.contracts:
        raise ValueError("legacy option master parsed to zero contracts")
    log.info("Legacy option master: %d contracts", len(bundle.contracts))
    return bundle


# ---------------------------------------------------------------------------
# Universe derivation
# ---------------------------------------------------------------------------
def fo_stock_universe(contracts: list[OptionContract]) -> list[str]:
    """Eligible F&O STOCK underlyings (option contracts only)."""
    return sorted(
        {c.underlying for c in contracts if is_stock_underlying(c.underlying)}
    )


def fo_index_universe(contracts: list[OptionContract]) -> list[str]:
    """INDEX underlyings - no longer used (stock options only)."""
    return []


# ---------------------------------------------------------------------------
# Download with cache
# ---------------------------------------------------------------------------
def _download_with_cache(rest, url: str, cache_dir: str, ttl_hours: float) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    name = url.split("/")[-1]
    path = os.path.join(cache_dir, name)
    try:
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl_hours * 3600:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
    except OSError:
        pass
    text = rest.get_text(url)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        log.warning("Could not cache instrument master to %s", path)
    return text


def load_master_bundle(rest, cache_dir: str, ttl_hours: float,
                       default_lot_size: int = 250) -> MasterBundle:
    """Download + parse the instrument master, trying sources in order."""
    sources = [
        (SCRIP_MASTER_DETAILED_URL, parse_scrip_master_detailed),
        (OLD_OPTION_MASTER_URL, parse_legacy_option_master),
    ]
    last_err: Optional[Exception] = None
    for url, parser in sources:
        try:
            text = _download_with_cache(rest, url, cache_dir, ttl_hours)
            bundle = parser(text, default_lot_size)
            return bundle
        except Exception as e:
            last_err = e
            log.warning("Instrument master source failed (%s): %s", url, e)
    raise RuntimeError(
        f"All instrument master sources failed; last error: {last_err}. "
        "Check network access to images.dhan.co."
    )
