"""CSV/JSON/Markdown report writers for the Phase 7 paper-only replay."""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from backtest.engine import Phase7Result, summarize_arm


SCANNER_FIELDS = (
    "date", "scan_id", "scan_at", "arm", "symbol", "security_id", "expiry",
    "strike", "option_type", "moneyness", "rank", "liquidity_threshold",
    "max_spread_percent", "liquidity_score", "volatility_score",
    "chain_quality_score", "execution_quality_score", "opportunity_score",
    "spread_percent", "quote_age_seconds", "selected_for_avwap", "anchor_verified",
    "status", "rejection_reasons", "missing_components", "spot",
    "spot_observed_at", "chain_observed_at",
)
TRADE_FIELDS = (
    "date", "arm", "trade_id", "symbol", "security_id", "expiry", "strike",
    "option_type", "status", "entry_time", "entry_date", "entry_candle_start",
    "entry_signal_price", "entry_fill_price", "entry_avwap", "entry_reason",
    "entry_quantity", "quantity", "entry_spread_percent", "entry_quote_age_seconds",
    "entry_quote_observed_at", "entry_slippage_cost", "entry_transaction_cost",
    "exit_time", "exit_date", "exit_candle_start", "exit_signal_price", "exit_fill_price",
    "exit_avwap", "exit_reason", "exit_quantity", "exit_spread_percent",
    "exit_quote_age_seconds", "exit_quote_observed_at", "exit_mark_source",
    "exit_mark_observed_at", "exit_mark_age_seconds", "exit_slippage_cost",
    "exit_transaction_cost", "gross_pnl", "reference_pnl_before_slippage",
    "slippage_cost", "transaction_costs", "net_pnl", "open_at_period_end",
    "mark_price", "mark_source", "gross_unrealized_pnl", "estimated_net_unrealized_pnl",
)
REJECTION_FIELDS = (
    "date", "arm", "event_at", "event_type", "symbol", "security_id", "expiry",
    "strike", "option_type", "category", "rejection_reasons", "signal_price",
    "avwap", "latest_scan_at",
)
MISSED_FIELDS = (
    "date", "arm", "signal_at", "candle_start", "security_id", "symbol", "expiry",
    "strike", "option_type", "signal_price", "avwap", "category",
    "rejection_reasons", "liquidity_score", "latest_scan_at", "scan_age_seconds",
    "counterfactual_trade_opened", "counterfactual_trade_id", "counterfactual_net_pnl",
    "counterfactual_status",
)
SHADOW_FIELDS = (
    "date", "arm", "security_id", "symbol", "expiry", "strike", "option_type",
    "entry_time", "entry_candle_start", "entry_signal_price", "entry_fill_price",
    "entry_avwap", "exit_time", "exit_candle_start", "exit_signal_price",
    "exit_fill_price", "exit_avwap", "exit_reason", "exit_mark_source",
    "exit_mark_observed_at", "exit_mark_age_seconds", "exit_quote_age_seconds",
    "quantity", "gross_pnl",
    "reference_pnl_before_slippage", "slippage_cost", "transaction_costs", "net_pnl",
    "rejection_reason", "entry_spread_percent", "entry_quote_age_seconds", "status",
    "mark_price", "gross_unrealized_pnl", "net_unrealized_pnl",
)


def write_reports(result: Phase7Result, output_dir: str | Path) -> None:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    arms = [result.baseline, *result.scenarios]

    summary = result.to_dict()
    _write_json(root / "summary.json", summary)
    _write_json(root / "data_quality.json", result.data_quality)
    _write_json(root / "lookahead_validation.json", result.lookahead_validation)
    _write_json(root / "assumptions.json", result.assumptions)
    _write_rows(root / "comparison.csv", result.comparison_rows)
    _write_rows(root / "threshold_analysis.csv", result.threshold_rows)
    _write_rows(root / "missed_opportunity_analysis.csv", result.missed_analysis_rows)

    scanner_rows = [row for arm in arms for row in arm.scanner_rows]
    trade_rows = [row for arm in arms for row in arm.trades]
    rejection_rows = [row for arm in arms for row in arm.rejections]
    missed_rows = [row for arm in arms for row in arm.missed_signals]
    shadow_rows = [row for arm in arms for row in arm.shadow_trades]
    _write_rows(root / "scanner_report.csv", scanner_rows, SCANNER_FIELDS)
    _write_rows(root / "trades.csv", trade_rows, TRADE_FIELDS)
    _write_rows(root / "rejections.csv", rejection_rows, REJECTION_FIELDS)
    _write_rows(root / "missed_signals.csv", missed_rows, MISSED_FIELDS)
    _write_rows(root / "counterfactual_missed_trades.csv", shadow_rows, SHADOW_FIELDS)

    _write_daily_reports(root / "daily_reports", arms)
    (root / "recommended_experiments.md").write_text(
        _recommendations(result), encoding="utf-8"
    )
    (root / "README.txt").write_text(
        "Phase 7 backtest reports\n"
        "========================\n"
        "This is a historical paper-only replay; no orders were sent.\n"
        "See assumptions.json for fill, slippage, cost, sizing, expiry, AVWAP, and risk settings.\n"
        "See lookahead_validation.json and data_quality.json before interpreting metrics.\n"
        "comparison.csv compares the legacy selector with each threshold combination.\n"
        "Daily subreports are in daily_reports/YYYY-MM-DD/.\n"
        "Counterfactual missed-trade P&L is descriptive and is not included in actual P&L.\n",
        encoding="utf-8",
    )


def _write_daily_reports(root: Path, arms) -> None:
    by_day: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {"scanner": [], "trades": [], "rejections": [], "missed_signals": [], "counterfactual": []}
    )
    for arm in arms:
        for row in arm.scanner_rows:
            if row.get("date"):
                by_day[str(row["date"])]["scanner"].append(row)
        for row in arm.trades:
            day = row.get("exit_date") or row.get("entry_date") or row.get("date")
            if day:
                by_day[str(day)]["trades"].append(row)
        for row in arm.rejections:
            if row.get("date"):
                by_day[str(row["date"])]["rejections"].append(row)
        for row in arm.missed_signals:
            if row.get("date"):
                by_day[str(row["date"])]["missed_signals"].append(row)
        for row in arm.shadow_trades:
            if row.get("date"):
                by_day[str(row["date"])]["counterfactual"].append(row)

    for day, reports in sorted(by_day.items()):
        folder = root / day
        folder.mkdir(parents=True, exist_ok=True)
        _write_rows(folder / "scanner.csv", reports["scanner"], SCANNER_FIELDS)
        _write_rows(folder / "trades.csv", reports["trades"], TRADE_FIELDS)
        _write_rows(folder / "rejections.csv", reports["rejections"], REJECTION_FIELDS)
        _write_rows(folder / "missed_signals.csv", reports["missed_signals"], MISSED_FIELDS)
        _write_rows(folder / "counterfactual_missed_trades.csv", reports["counterfactual"], SHADOW_FIELDS)


def _write_rows(path: Path, rows: Iterable[dict], fields: Iterable[str] | None = None) -> None:
    rows = list(rows)
    fieldnames = list(fields or _field_union(rows))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _field_union(rows: list[dict]) -> list[str]:
    return list(dict.fromkeys(key for row in rows for key in row.keys()))


def _csv_value(value):
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(value), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _recommendations(result: Phase7Result) -> str:
    warnings = result.data_quality.get("warnings", [])
    blocking_quality = result.data_quality.get("errors", [])
    lines = [
        "# Phase 7: recommended experiments and interpretation",
        "",
        "## Interpretation",
        "",
        "No automatic winner is declared. Read the comparison together with the data-quality and look-ahead reports. "
        "Counterfactual missed-trade P&L is not realized P&L and is not included in the system comparison.",
        "",
        f"Replay period: {result.period_start.isoformat()} to {result.period_end.isoformat()}.",
        f"Symbols in the archived manifest: {len(result.symbols)}.",
        f"Look-ahead validation: {result.lookahead_validation.get('status', 'unknown')}.",
        "",
        "## Data readiness",
        "",
    ]
    if blocking_quality:
        lines.append("- Blocking issues remain; performance metrics should not be interpreted.")
        for item in blocking_quality:
            lines.append(f"- **{item.get('code', 'DATA_ERROR')}**: {item.get('message', item)}")
    elif warnings:
        lines.append("- Resolve or quantify these warnings before comparing strategies:")
        for item in warnings:
            lines.append(f"- **{item.get('code', 'DATA_WARNING')}**: {item.get('message', item)}")
    else:
        lines.append("- No loader warnings were recorded; still verify source provenance and archive completeness.")
    lines.extend([
        "",
        "## Suggested follow-up experiments",
        "",
        "1. Repeat the identical paired comparison on a separately held-out historical period; do not tune and report on the same dates.",
        "2. Re-run with documented nonzero transaction fees and adverse slippage assumptions, changing one execution assumption at a time.",
        "3. Compare the full liquidity-score/spread sweep by date, symbol, expiry, and option side; investigate instability rather than selecting a single best cell.",
        "4. Inspect the missed-signal report and counterfactual paths for liquidity-rejected contracts, including anchor coverage and quote age.",
        "5. Add archived quote/depth/chain captures and complete option-candle histories where data-quality coverage is absent; never backfill with current snapshots.",
        "6. Verify that contract-master captures represent complete point-in-time snapshots and that the 20-symbol manifest matches the intended universe.",
        "",
        "Paper and live trading remain disabled. This report writer has no execution integration.",
        "",
    ])
    return "\n".join(lines)
