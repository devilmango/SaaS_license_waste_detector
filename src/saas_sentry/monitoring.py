"""Snapshot and invoice anomaly detection for recurring SaaS monitoring."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .engine import InputError, _rows


@dataclass(frozen=True)
class Anomaly:
    severity: str
    kind: str
    scope: str
    baseline_value: str
    current_value: str
    change: str
    summary: str


def _amount(value: Any, *, label: str) -> Decimal:
    try:
        amount = Decimal(str(value or "0"))
    except InvalidOperation as exc:
        raise InputError(f"monitor report has invalid {label}: {value!r}") from exc
    if not amount.is_finite():
        raise InputError(f"monitor report has non-finite {label}")
    return amount


def _key(*values: Any) -> str:
    return "\0".join(" ".join(str(value or "").casefold().split()) for value in values)


def _snapshot_rows(snapshot: dict[str, Any], field: str) -> list[dict[str, Any]]:
    rows = snapshot.get(field, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise InputError(f"monitor report field {field!r} must be a list of objects")
    return rows


def detect_anomalies(
    current: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    reconciliation_path: Path | None = None,
    spend_change_threshold_percent: Decimal = Decimal("20"),
    seat_change_threshold: int = 2,
    renewal_window_days: int = 60,
) -> tuple[Anomaly, ...]:
    """Detect material spend/seat changes, contract-floor and renewal risks, and invoice issues."""
    if not spend_change_threshold_percent.is_finite() or spend_change_threshold_percent < 0:
        raise InputError("spend change threshold must be finite and non-negative")
    if seat_change_threshold < 0 or renewal_window_days < 0:
        raise InputError("seat and renewal thresholds must be non-negative")
    if not isinstance(current.get("as_of"), str):
        raise InputError("current report must include an as_of date")
    try:
        current_day = date.fromisoformat(current["as_of"])
        baseline_day = date.fromisoformat(baseline["as_of"]) if baseline else None
    except (TypeError, ValueError) as exc:
        raise InputError("monitor report as_of dates must use YYYY-MM-DD") from exc
    if baseline_day and current_day < baseline_day:
        raise InputError("current report as_of date cannot precede the baseline")

    alerts: list[Anomaly] = []
    compare_fields = (
        ("cost_center_showback", "cost_center", "annualized_spend_inr", "active_license_count"),
        ("renewal_calendar", "contract", "annualized_spend_inr", "active_seats"),
    )
    for field, scope_type, spend_field, seat_field in compare_fields:
        current_rows = _snapshot_rows(current, field)
        baseline_rows = _snapshot_rows(baseline, field) if baseline else []
        if field == "cost_center_showback":
            get_key = lambda row: _key(row.get("department"), row.get("cost_center"))
            display = lambda row: f"{row.get('department', 'Unassigned')} / {row.get('cost_center', 'Unassigned')}"
        else:
            get_key = lambda row: _key(row.get("contract_id"))
            display = lambda row: str(row.get("contract_id", "Unknown contract"))
        old = {get_key(row): row for row in baseline_rows}
        new = {get_key(row): row for row in current_rows}
        if len(new) != len(current_rows) or len(old) != len(baseline_rows):
            raise InputError(f"monitor report contains duplicate {scope_type} keys")
        if baseline:
            for item_key in sorted(set(old) | set(new)):
                before_row, after_row = old.get(item_key, {}), new.get(item_key, {})
                label = display(after_row or before_row)
                for measure, field_name, threshold in (
                    ("spend", spend_field, spend_change_threshold_percent),
                    ("seats", seat_field, Decimal(seat_change_threshold)),
                ):
                    before = _amount(before_row.get(field_name), label=f"{field}.{field_name}")
                    after = _amount(after_row.get(field_name), label=f"{field}.{field_name}")
                    delta = after - before
                    if measure == "spend":
                        changed = (
                            (before == 0 and after != 0)
                            or (delta != 0 and before != 0 and abs(delta) * Decimal("100") / abs(before) >= threshold)
                        )
                        delta_label = f"{delta:+.2f} INR"
                        baseline_label, current_label = f"{before:.2f} INR", f"{after:.2f} INR"
                        kind = f"{scope_type}_spend_change"
                    else:
                        changed = abs(delta) >= threshold and delta != 0
                        delta_label = f"{delta:+.0f} seats"
                        baseline_label, current_label = f"{before:.0f} seats", f"{after:.0f} seats"
                        kind = f"{scope_type}_seat_change"
                    if changed:
                        direction = "increased" if delta > 0 else "decreased"
                        alerts.append(Anomaly(
                            "medium" if delta > 0 else "low", kind, f"{scope_type}: {label}",
                            baseline_label, current_label, delta_label,
                            f"{measure.capitalize()} {direction} beyond the configured monitoring threshold.",
                        ))

    for row in _snapshot_rows(current, "renewal_calendar"):
        contract_id = str(row.get("contract_id", "Unknown contract"))
        active = int(_amount(row.get("active_seats"), label="renewal_calendar.active_seats"))
        minimum = int(_amount(row.get("seat_minimum"), label="renewal_calendar.seat_minimum"))
        if minimum > active:
            idle = minimum - active
            alerts.append(Anomaly(
                "high", "underutilized_commitment", f"contract: {contract_id}",
                f"{minimum} committed seats", f"{active} active seats", f"{idle} unused seats",
                "Active assignments are below the contractual seat minimum; review the renewal commitment.",
            ))
        days = row.get("days_until_notice_deadline")
        if days not in (None, ""):
            days_left = int(_amount(days, label="renewal_calendar.days_until_notice_deadline"))
            status = str(row.get("status", ""))
            if status in {"notice_overdue", "missed_notice_next_cycle"}:
                alerts.append(Anomaly(
                    "high", "renewal_notice_overdue", f"contract: {contract_id}",
                    "notice deadline", str(row.get("notice_deadline", "")), f"{days_left} days",
                    "The contract notice deadline has passed or the next renewal cycle is assumed.",
                ))
            elif 0 <= days_left <= renewal_window_days:
                alerts.append(Anomaly(
                    "high" if days_left <= 14 else "medium", "renewal_notice_due",
                    f"contract: {contract_id}", f">{renewal_window_days} days", str(days_left),
                    f"{days_left} days", "A contract notice deadline is approaching.",
                ))

    for issue in _snapshot_rows(current, "data_quality_issues"):
        if issue.get("issue") == "seat_minimum_limit":
            alerts.append(Anomaly(
                "medium", "reclaim_blocked_by_contract_floor",
                f"contract: {issue.get('identity', 'Unknown contract')}", "configured minimum",
                "reclaim candidate exceeds removable seats", "review required",
                str(issue.get("details", "A candidate reclaim is limited by the contract seat minimum.")),
            ))

    if reconciliation_path:
        invoice_rows = _rows(reconciliation_path, {
            "application", "cost_center", "billing_period_start", "billing_period_end",
            "actual_billed_cost_inr", "variance_inr", "status",
        })
        for line, row in enumerate(invoice_rows, start=2):
            status = row.get("status", "")
            if status == "matched":
                continue
            if status not in {"variance", "unlicensed_spend", "missing_invoice_spend"}:
                raise InputError(f"{reconciliation_path}:{line}: unknown reconciliation status {status!r}")
            actual = _amount(row.get("actual_billed_cost_inr"), label=f"{reconciliation_path}:{line}:actual spend")
            variance = _amount(row.get("variance_inr"), label=f"{reconciliation_path}:{line}:variance")
            severity = "high" if status == "unlicensed_spend" else "medium"
            alerts.append(Anomaly(
                severity, f"invoice_{status}",
                f"{row['application']} / {row['cost_center']} ({row['billing_period_start']} to {row['billing_period_end']})",
                "assigned-license model", f"{actual:.2f} INR actual", f"{variance:+.2f} INR variance",
                f"FOCUS reconciliation reported {status.replace('_', ' ')}.",
            ))
    order = {"high": 0, "medium": 1, "low": 2}
    return tuple(sorted(alerts, key=lambda item: (order.get(item.severity, 3), item.kind, item.scope.casefold())))


def format_anomalies(rows: Iterable[Anomaly]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["severity", "kind", "scope", "baseline_value", "current_value", "change", "summary"])
    for row in rows:
        writer.writerow([
            row.severity, row.kind, row.scope, row.baseline_value, row.current_value,
            row.change, row.summary,
        ])
    return output.getvalue()
