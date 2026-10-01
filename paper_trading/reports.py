"""Persistent daily paper-trading reports and CSV trade/position logs."""
from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

from common.config import DEFAULTS
from common.utils import IST


class PaperReportWriter:
    """Build reports from the SQLite audit tables; safe to run after restart."""

    def __init__(self, db, output_dir: str | Path = "reports/paper",
                 paper_config: Optional[dict] = None):
        self.db = db
        self.output_dir = Path(output_dir)
        self.paper_config = {**DEFAULTS["paper"], **(paper_config or {})}

    def write_day(self, trading_date: str | date, *, generated_at: Optional[datetime] = None) -> dict:
        day = trading_date.isoformat() if isinstance(trading_date, date) else str(trading_date)
        try:
            day_value = date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError("trading_date must be YYYY-MM-DD") from exc
        generated = _as_ist(generated_at or datetime.now(IST))
        start = int(datetime.combine(day_value, time.min, tzinfo=IST).timestamp())
        end = int((datetime.combine(day_value + timedelta(days=1), time.min, tzinfo=IST)
                   - timedelta(seconds=1)).timestamp())

        scans = self.db.get_paper_scanner_runs(day)
        candidates = self.db.get_paper_scanner_candidates(trading_date=day)
        executions = self.db.get_paper_execution_events(start_ts=start, end_ts=end)
        errors = self.db.get_paper_errors(day)
        signals = [dict(row) for row in self.db.get_signals(limit=100000, since_ts=start)
                   if int(row["candle_ts"] or 0) <= end]
        all_positions = [dict(row) for row in self.db.get_positions(limit=100000)
                         if str(row["mode"] or "").upper() == "PAPER"]
        opened_today = [row for row in all_positions if _in_day(row.get("entry_time"), start, end)]
        closed_today = [row for row in all_positions if row.get("status") == "CLOSED"
                        and _in_day(row.get("exit_time"), start, end)]
        open_now = [row for row in all_positions if row.get("status") in {"OPEN", "PENDING"}]

        execution_by_position: dict[str, list[dict]] = {}
        for event in self.db.get_paper_execution_events(limit=100000):
            execution_by_position.setdefault(str(event["position_id"]), []).append(event)
        marks = self.db.get_paper_position_marks()
        max_quote_age = float(self.paper_config.get("max_quote_age_seconds", 60))
        marks_for_day = {
            position_id: _mark_for_report(mark, day_value, generated, max_quote_age)
            for position_id, mark in marks.items()
        }

        candidate_symbols = {str(row["symbol"]).upper() for row in candidates if row.get("symbol")}
        scan_symbols = {
            str(symbol).strip().upper()
            for scan in scans for symbol in (scan.get("symbols") or ()) if str(symbol).strip()
        }
        discovered_ids = {str(row["security_id"]) for row in candidates if row.get("security_id")}
        qualified_ids = {str(row["security_id"]) for row in candidates
                         if row.get("qualified") and row.get("security_id")}
        rejected_ids = {str(row["security_id"]) for row in candidates
                        if not row.get("qualified") and row.get("security_id")}
        selected_ids = {str(row["security_id"]) for row in candidates
                        if row.get("selected_for_avwap") and row.get("security_id")}
        rejection_counts = Counter()
        for row in candidates:
            if not row.get("qualified"):
                rejection_counts.update(row.get("rejection_reasons") or [])

        closed_gross = sum(float(row.get("pnl") or 0.0) for row in closed_today)
        closed_ids = {str(row["position_id"]) for row in closed_today}
        close_trade_costs = sum(
            float(event.get("transaction_cost") or 0.0)
            for position_id in closed_ids
            for event in execution_by_position.get(position_id, ())
        )
        realized_net = closed_gross - close_trade_costs
        fill_costs_today = sum(float(event.get("transaction_cost") or 0.0) for event in executions)
        slippage_today = sum(float(event.get("slippage_amount") or 0.0) for event in executions)
        unrealized = 0.0
        unrealized_coverage = 0
        open_margin = 0.0
        margin_coverage = 0
        open_transaction_costs = 0.0
        open_position_rows = []
        position_by_id = {str(row["position_id"]): row for row in all_positions}
        for position in open_now:
            pid = str(position["position_id"])
            events = execution_by_position.get(pid, ())
            entry = next((event for event in events if event.get("event_type") == "ENTRY_SELL"), None)
            open_transaction_costs += float((entry or {}).get("transaction_cost") or 0.0)
            mark = marks_for_day.get(pid)
            current_price = (
                _finite(mark.get("last_price")) if mark and mark.get("mark_fresh") else None
            )
            if current_price is not None and position.get("entry_price") is not None:
                upnl = (float(position["entry_price"]) - current_price) * int(position.get("quantity") or 0)
                unrealized += upnl
                unrealized_coverage += 1
            else:
                upnl = None
            margin = _finite(entry.get("margin_estimate")) if entry else None
            if margin is not None:
                open_margin += margin
                margin_coverage += 1
            open_position_rows.append(_position_row(
                position, entry, None, mark=mark, unrealized=upnl,
                margin_estimate=margin,
            ))

        relevant_position_ids = {
            str(row["position_id"]) for row in opened_today + closed_today
        }
        relevant_position_ids.update(str(event["position_id"]) for event in executions)
        trade_rows = []
        for pid in sorted(relevant_position_ids):
            position = position_by_id.get(pid)
            if not position:
                continue
            events = execution_by_position.get(pid, ())
            entry = next((event for event in events if event.get("event_type") == "ENTRY_SELL"), None)
            exit_event = next((event for event in events if event.get("event_type") == "EXIT_BUY"), None)
            trade_rows.append(_trade_row(position, entry, exit_event, events))

        execution_raw = [event.get("raw") or {} for event in executions]

        def _unique_observed(values):
            clean = {json.dumps(value, sort_keys=True, default=str) for value in values if value is not None}
            return [json.loads(value) for value in sorted(clean)]

        summary = {
            "status": "ready" if scans else "no_scanner_snapshots",
            "trading_date": day,
            "generated_at": generated.isoformat(),
            "scan_cycles": len(scans),
            "stocks_scanned": len(scan_symbols or candidate_symbols),
            "stock_scan_evaluations": sum(int(scan.get("stocks_scanned") or 0) for scan in scans),
            "contracts_scanned": len(discovered_ids) if candidates else max(
                (int(scan.get("contracts_scanned") or 0) for scan in scans), default=0
            ),
            "contract_scan_evaluations": sum(int(scan.get("contracts_scanned") or 0) for scan in scans),
            "qualified_contracts": len(qualified_ids),
            "rejected_contracts": len(rejected_ids),
            "qualified_contract_evaluations": sum(int(scan.get("qualified_contracts") or 0) for scan in scans),
            "rejected_contract_evaluations": sum(int(scan.get("rejected_contracts") or 0) for scan in scans),
            "selected_contracts": len(selected_ids),
            "signals_generated": len(signals),
            "paper_trades": sum(1 for event in executions if event.get("event_type") == "ENTRY_SELL"),
            "open_positions_at_report": len(open_now),
            "closed_positions_today": len(closed_today),
            "realized_gross_pnl": _round(closed_gross),
            "transaction_costs_on_closed_trades": _round(close_trade_costs),
            "transaction_costs_on_open_positions": _round(open_transaction_costs),
            "realized_net_pnl": _round(realized_net),
            "fill_transaction_costs_today": _round(fill_costs_today),
            "slippage_cost_today": _round(slippage_today),
            "unrealized_pnl": _round(unrealized) if unrealized_coverage else None,
            "unrealized_mark_coverage": f"{unrealized_coverage}/{len(open_now)}",
            "estimated_margin_open": _round(open_margin) if margin_coverage else None,
            "margin_estimate_coverage": f"{margin_coverage}/{len(open_now)}",
            "combined_realized_plus_unrealized_pnl": (
                _round(realized_net + unrealized - open_transaction_costs)
                if unrealized_coverage else None
            ),
            "rejection_reasons": dict(sorted(rejection_counts.items())),
            "errors": len(errors),
            "recovered_errors": sum(bool(error.get("recovered")) for error in errors),
            "automatic_winner_declared": False,
            "paper_trading_only": True,
            "live_orders_sent": False,
        }
        report = {
            "summary": summary,
            "assumptions": {
                "configured_default_fill_mode": self.paper_config.get("fill_mode", "marketable_quote"),
                "configured_max_quote_age_seconds": self.paper_config.get("max_quote_age_seconds", 60),
                "configured_slippage_bps_per_side": self.paper_config.get("slippage_bps", 5),
                "configured_quote_fallback": self.paper_config.get("quote_fallback", "signal_candle_close"),
                "configured_transaction_cost_model": {
                    "per_order": self.paper_config.get("transaction_cost_per_order", 0),
                    "bps_of_premium_turnover": self.paper_config.get("transaction_cost_bps", 0),
                },
                "configured_margin_estimate_pct_of_underlying_notional": self.paper_config.get(
                    "margin_estimate_pct_of_underlying_notional", 20
                ),
                "entry_sell": "fresh two-sided option quote: sell at captured best bid; no midpoint fills",
                "exit_buy": "fresh two-sided option quote: buy at captured best ask; no midpoint fills",
                "quote_max_age_seconds_by_fill": _unique_observed(
                    raw.get("max_quote_age_seconds") for raw in execution_raw
                ),
                "fill_modes_observed": _unique_observed(
                    raw.get("fill_mode") for raw in execution_raw
                ),
                "fallbacks_configured": _unique_observed(
                    raw.get("quote_fallback") for raw in execution_raw
                ),
                "execution_methods_observed": _unique_observed(
                    event.get("execution_method") for event in executions
                ),
                "slippage_bps_per_side_observed": _unique_observed(
                    event.get("slippage_bps") for event in executions
                ),
                "transaction_cost_models_observed": _unique_observed(
                    raw.get("transaction_cost_model") for raw in execution_raw
                ),
                "margin_models_observed": _unique_observed(
                    event.get("margin_model") for event in executions
                ),
                "fallback": "when a strategy signal has no fresh two-sided book, signal candle close plus configured adverse slippage; method is recorded per fill",
                "slippage": "configured adverse basis points per side, rounded against the paper trader to contract tick size",
                "fees": "only configured per-order and premium-turnover basis-point costs; no exchange/tax estimate is silently invented",
                "margin": "configurable percentage of underlying notional; indicative only, not an official Dhan/SPAN margin requirement",
            },
            "scanner_runs": scans,
            "scanner_candidates": candidates,
            "signals": signals,
            "executions": executions,
            "trades": trade_rows,
            "open_positions": open_position_rows,
            "closed_positions_today": [
                _position_row(
                    position,
                    next((e for e in execution_by_position.get(str(position["position_id"]), ())
                          if e.get("event_type") == "ENTRY_SELL"), None),
                    next((e for e in execution_by_position.get(str(position["position_id"]), ())
                          if e.get("event_type") == "EXIT_BUY"), None),
                )
                for position in closed_today
            ],
            "errors": errors,
        }

        day_dir = self.output_dir / day
        day_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(day_dir / "daily.json", report)
        self._write_csv(day_dir / "scanner.csv", candidates)
        self._write_csv(day_dir / "trades.csv", trade_rows)
        self._write_csv(day_dir / "positions.csv", open_position_rows + report["closed_positions_today"])
        self._write_csv(day_dir / "paper_executions.csv", executions)
        self._write_csv(day_dir / "errors.csv", errors)
        (day_dir / "daily.md").write_text(_markdown(summary), encoding="utf-8")

        # Persistent all-days exports make the multi-session history easy to
        # analyze without depending on whichever day's dashboard is open.
        all_positions = [dict(row) for row in self.db.get_positions(limit=100000)
                         if str(row["mode"] or "").upper() == "PAPER"]
        all_events = self.db.get_paper_execution_events(limit=100000)
        all_by_position: dict[str, list[dict]] = {}
        for event in all_events:
            all_by_position.setdefault(str(event["position_id"]), []).append(event)
        all_trade_rows = []
        all_position_rows = []
        current_marks = {
            position_id: _mark_for_report(mark, generated.date(), generated, max_quote_age)
            for position_id, mark in marks.items()
        }
        for position in all_positions:
            pid = str(position["position_id"])
            events = all_by_position.get(pid, ())
            entry = next((e for e in events if e.get("event_type") == "ENTRY_SELL"), None)
            exit_event = next((e for e in events if e.get("event_type") == "EXIT_BUY"), None)
            all_position_rows.append(_position_row(
                position, entry, exit_event, mark=current_marks.get(pid),
            ))
            all_trade_rows.append(_trade_row(position, entry, exit_event, events))
        self._write_csv(self.output_dir / "trade_log.csv", all_trade_rows)
        self._write_csv(self.output_dir / "position_log.csv", all_position_rows)
        self._write_csv(self.output_dir / "paper_error_log.csv", self.db.get_paper_errors(limit=100000))
        self.db.save_paper_daily_report(day, int(generated.timestamp()), str(day_dir), summary)
        self._write_csv(self.output_dir / "daily_index.csv", self.db.get_paper_daily_reports(limit=100000))
        return report

    def write_existing_days(self, *, include_today: bool = False,
                            today: Optional[date] = None) -> list[dict]:
        days = set(self.db.get_paper_trading_dates())
        if include_today:
            days.add((today or datetime.now(IST).date()).isoformat())
        reports = []
        for day in sorted(days):
            reports.append(self.write_day(day))
        return reports

    @staticmethod
    def _write_json(path: Path, value) -> None:
        _atomic_write(path, json.dumps(
            _json_safe(value), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        ) + "\n")

    @staticmethod
    def _write_csv(path: Path, rows) -> None:
        rows = [dict(row) for row in rows]
        fields = list(dict.fromkeys(key for row in rows for key in row.keys()))
        if not fields:
            fields = ["status"]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: _csv_value(row.get(key)) for key in fields})
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def _in_day(value, start: int, end: int) -> bool:
    try:
        ts = int(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return start <= ts <= end


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _round(value):
    value = _finite(value)
    return round(value, 6) if value is not None else None


def _position_row(position, entry, exit_event, *, mark=None, unrealized=None, margin_estimate=None):
    return {
        "position_id": position.get("position_id"),
        "status": position.get("status"),
        "underlying": position.get("underlying"),
        "symbol": position.get("symbol"),
        "security_id": position.get("security_id"),
        "expiry": position.get("expiry"),
        "strike": position.get("strike"),
        "option_type": position.get("option_type"),
        "quantity": position.get("quantity"),
        "lot_size": (entry or {}).get("lot_size"),
        "entry_time": position.get("entry_time"),
        "entry_execution_timestamp": (entry or {}).get("timestamp"),
        "entry_signal_timestamp": (entry or {}).get("signal_timestamp"),
        "entry_quote_timestamp": (entry or {}).get("quote_timestamp"),
        "entry_price": position.get("entry_price"),
        "entry_avwap": position.get("entry_avwap"),
        "exit_time": position.get("exit_time"),
        "exit_execution_timestamp": (exit_event or {}).get("timestamp"),
        "exit_signal_timestamp": (exit_event or {}).get("signal_timestamp"),
        "exit_quote_timestamp": (exit_event or {}).get("quote_timestamp"),
        "exit_price": position.get("exit_price"),
        "exit_avwap": position.get("exit_avwap"),
        "exit_reason": position.get("exit_reason"),
        "gross_realized_pnl": position.get("pnl"),
        "entry_bid": (entry or {}).get("bid"),
        "entry_ask": (entry or {}).get("ask"),
        "entry_spread_percent": (entry or {}).get("spread_percent"),
        "entry_fill_price": (entry or {}).get("fill_price"),
        "entry_execution_method": (entry or {}).get("execution_method"),
        "entry_quote_age_seconds": (entry or {}).get("quote_age_seconds"),
        "exit_bid": (exit_event or {}).get("bid"),
        "exit_ask": (exit_event or {}).get("ask"),
        "exit_spread_percent": (exit_event or {}).get("spread_percent"),
        "exit_fill_price": (exit_event or {}).get("fill_price"),
        "exit_execution_method": (exit_event or {}).get("execution_method"),
        "exit_quote_age_seconds": (exit_event or {}).get("quote_age_seconds"),
        "margin_estimate": margin_estimate if margin_estimate is not None else (entry or {}).get("margin_estimate"),
        "margin_model": (entry or {}).get("margin_model"),
        "mark_price": (mark or {}).get("last_price"),
        "mark_time": (mark or {}).get("observed_at"),
        "mark_age_seconds": (mark or {}).get("mark_age_seconds"),
        "mark_fresh": (mark or {}).get("mark_fresh"),
        "current_avwap": (mark or {}).get("avwap"),
        "unrealized_pnl": unrealized if unrealized is not None else (mark or {}).get("unrealized_pnl"),
    }


def _trade_row(position, entry, exit_event, events):
    entry_cost = float((entry or {}).get("transaction_cost") or 0.0)
    exit_cost = float((exit_event or {}).get("transaction_cost") or 0.0)
    gross = _finite(position.get("pnl"))
    net = gross - entry_cost - exit_cost if gross is not None else None
    slippage = sum(float(event.get("slippage_amount") or 0.0) for event in events)
    return {
        **_position_row(position, entry, exit_event),
        "entry_signal_price": (entry or {}).get("signal_price"),
        "exit_signal_price": (exit_event or {}).get("signal_price"),
        "entry_simulated_fill_price": (entry or {}).get("fill_price"),
        "exit_simulated_fill_price": (exit_event or {}).get("fill_price"),
        "entry_slippage_amount": (entry or {}).get("slippage_amount"),
        "exit_slippage_amount": (exit_event or {}).get("slippage_amount"),
        "total_slippage_amount": _round(slippage),
        "entry_transaction_cost": _round(entry_cost),
        "exit_transaction_cost": _round(exit_cost),
        "total_transaction_cost": _round(entry_cost + exit_cost),
        "gross_pnl": _round(gross),
        "net_pnl_after_configured_costs": _round(net),
    }


def _csv_value(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_json_safe(value), sort_keys=True, ensure_ascii=False, allow_nan=False)
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _markdown(summary: dict) -> str:
    values = [
        ("Stocks scanned", summary.get("stocks_scanned")),
        ("Unique contracts scanned", summary.get("contracts_scanned")),
        ("Qualified contracts", summary.get("qualified_contracts")),
        ("Rejected contracts", summary.get("rejected_contracts")),
        ("AVWAP signals generated", summary.get("signals_generated")),
        ("Paper trades", summary.get("paper_trades")),
        ("Open positions at report", summary.get("open_positions_at_report")),
        ("Closed positions today", summary.get("closed_positions_today")),
        ("Realized gross P&L", summary.get("realized_gross_pnl")),
        ("Realized net P&L", summary.get("realized_net_pnl")),
        ("Unrealized P&L", summary.get("unrealized_pnl")),
        ("Estimated open margin", summary.get("estimated_margin_open")),
        ("Errors", summary.get("errors")),
    ]
    lines = [f"# Paper trading report — {summary.get('trading_date')}", "",
             "This report contains paper-only simulated execution; no live orders were sent.", "",
             "| Metric | Value |", "|---|---:|"]
    lines.extend(f"| {label} | {value if value is not None else 'unavailable'} |"
                 for label, value in values)
    lines.extend(["", "Execution assumptions: fresh two-sided entries cross at best bid;",
                  "fresh two-sided exits cross at best ask; no midpoint fill is assumed.",
                  "See `daily.json` for exact timestamps, fallbacks, slippage, costs,",
                  "quote coverage, margin model, scan decisions, positions, and errors.", ""])
    return "\n".join(lines)


def _mark_for_report(mark: dict, day_value: date, generated: datetime, max_quote_age: float):
    """Keep mark time/source visible, but never present an old quote as current P&L."""
    if not mark:
        return None
    out = dict(mark)
    try:
        observed = int(out.get("observed_at") or 0)
    except (TypeError, ValueError, OverflowError):
        observed = 0
    day_start = datetime.combine(day_value, time.min, tzinfo=IST)
    session_close = datetime.combine(day_value, time(15, 30), tzinfo=IST)
    cutoff = min(generated, session_close) if generated.date() == day_value else session_close
    age = int(cutoff.timestamp()) - observed if observed > 0 else None
    mark_date = datetime.fromtimestamp(observed, IST).date() if observed > 0 else None
    fresh = bool(
        mark_date == day_value and observed >= int(day_start.timestamp())
        and age is not None and 0 <= age <= max_quote_age
    )
    out["mark_age_seconds"] = age
    out["mark_fresh"] = fresh
    if not fresh:
        out["unrealized_pnl"] = None
    return out
