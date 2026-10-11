"""Compare post-reclaim invoice spend with the prior billing period."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .actions import ActionError, list_actions
from .engine import InputError


@dataclass(frozen=True)
class InvoiceVerificationRow:
    application: str
    cost_center: str
    reclaimed_actions: int
    estimated_annual_savings_inr: Decimal
    baseline_period_start: date
    baseline_period_end: date
    baseline_billed_inr: Decimal
    current_period_start: date
    current_period_end: date
    current_billed_inr: Decimal
    observed_annualized_reduction_inr: Decimal
    variance_inr: Decimal
    realization_percent: Decimal | None
    status: str


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _read_focus(path: Path) -> tuple[dict[tuple[str, str], Decimal], date, date]:
    required = {
        "application", "cost_center", "billing_period_start", "billing_period_end",
        "actual_billed_cost_inr",
    }
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise InputError(f"{path}: normalized invoice CSV missing columns: {', '.join(sorted(missing))}")
            rows = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read normalized invoice CSV {path}: {exc}") from exc
    periods: set[tuple[date, date]] = set()
    spend: dict[tuple[str, str], Decimal] = {}
    for line, row in enumerate(rows, start=2):
        try:
            start = date.fromisoformat((row.get("billing_period_start") or "")[:10])
            end = date.fromisoformat((row.get("billing_period_end") or "")[:10])
            amount = Decimal(row.get("actual_billed_cost_inr") or "")
        except (ValueError, InvalidOperation) as exc:
            raise InputError(f"{path}:{line}: invalid invoice period or INR amount") from exc
        if end <= start or not amount.is_finite() or amount < 0:
            raise InputError(f"{path}:{line}: invoice period must be positive and billed cost finite/non-negative")
        application = (row.get("application") or "").strip()
        center = (row.get("cost_center") or "").strip() or "Unassigned"
        if not application:
            raise InputError(f"{path}:{line}: application cannot be blank")
        periods.add((start, end))
        key = (_norm(application), _norm(center))
        spend[key] = spend.get(key, Decimal("0")) + amount
    if len(periods) != 1:
        raise InputError(f"{path}: invoice verification requires one consistent billing period per file")
    start, end = next(iter(periods))
    return spend, start, end


def verify_invoice_savings(
    actions_path: Path,
    findings_path: Path,
    baseline_invoice_path: Path,
    current_invoice_path: Path,
    *,
    minimum_realization_percent: Decimal = Decimal("80"),
) -> tuple[InvoiceVerificationRow, ...]:
    """Measure app/cost-center invoice change after reclaimed actions, annualized by period length."""
    if not minimum_realization_percent.is_finite() or not Decimal("0") <= minimum_realization_percent <= Decimal("100"):
        raise InputError("minimum realization percent must be between 0 and 100")
    try:
        with findings_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not {"finding_id", "application"}.issubset(reader.fieldnames):
                raise ActionError(f"{findings_path}: expected findings CSV with finding_id and application columns")
            finding_rows = list(reader)
    except OSError as exc:
        raise ActionError(f"Cannot read findings CSV {findings_path}: {exc}") from exc
    findings: dict[str, dict[str, str]] = {}
    for line, row in enumerate(finding_rows, start=2):
        finding_id = (row.get("finding_id") or "").strip()
        if not finding_id or finding_id in findings:
            raise ActionError(f"{findings_path}:{line}: finding_id is blank or duplicated")
        findings[finding_id] = {key: (value or "").strip() for key, value in row.items() if key}
    baseline, baseline_start, baseline_end = _read_focus(baseline_invoice_path)
    current, current_start, current_end = _read_focus(current_invoice_path)
    if baseline_end > current_start:
        raise InputError("baseline invoice period must end on or before the current invoice period starts")
    baseline_days = (baseline_end - baseline_start).days
    current_days = (current_end - current_start).days
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for action in list_actions(actions_path):
        if action.status != "reclaimed":
            continue
        finding = findings.get(action.finding_id)
        if finding is None:
            raise ActionError(f"Action {action.action_id} refers to a finding missing from {findings_path}")
        application = finding.get("application", "").strip()
        if not application:
            raise ActionError(f"Finding {action.finding_id} has no application to match invoice spend")
        center = finding.get("cost_center", "").strip() or "Unassigned"
        key = (_norm(application), _norm(center))
        row = grouped.setdefault(key, {
            "application": application, "cost_center": center, "actions": 0,
            "estimated": Decimal("0"),
        })
        row["actions"] = int(row["actions"]) + 1
        row["estimated"] = Decimal(row["estimated"]) + action.estimated_annual_savings_inr

    result: list[InvoiceVerificationRow] = []
    for key, item in sorted(grouped.items()):
        before = baseline.get(key, Decimal("0"))
        after = current.get(key, Decimal("0"))
        estimated = Decimal(item["estimated"])
        observed = (before / baseline_days - after / current_days) * Decimal("365")
        observed = observed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        variance = observed - estimated
        ratio = (observed * Decimal("100") / estimated).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if estimated else None
        if before == 0:
            status = "no_baseline_charge"
        elif observed < 0:
            status = "spend_increased"
        elif observed == 0:
            status = "no_reduction_observed"
        elif ratio is None:
            status = "reduction_observed_unscored"
        elif ratio >= minimum_realization_percent:
            status = "verified"
        else:
            status = "partial"
        result.append(InvoiceVerificationRow(
            str(item["application"]), str(item["cost_center"]), int(item["actions"]),
            estimated.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            baseline_start, baseline_end, before.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            current_start, current_end, after.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            observed, variance.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ratio, status,
        ))
    return tuple(result)


def format_invoice_verification(rows: Iterable[InvoiceVerificationRow]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "application", "cost_center", "reclaimed_actions", "estimated_annual_savings_inr",
        "baseline_period_start", "baseline_period_end", "baseline_billed_inr",
        "current_period_start", "current_period_end", "current_billed_inr",
        "observed_annualized_reduction_inr", "variance_inr", "realization_percent", "status",
    ])
    for row in rows:
        writer.writerow([
            row.application, row.cost_center, row.reclaimed_actions,
            f"{row.estimated_annual_savings_inr:.2f}", row.baseline_period_start.isoformat(),
            row.baseline_period_end.isoformat(), f"{row.baseline_billed_inr:.2f}",
            row.current_period_start.isoformat(), row.current_period_end.isoformat(),
            f"{row.current_billed_inr:.2f}", f"{row.observed_annualized_reduction_inr:.2f}",
            f"{row.variance_inr:.2f}",
            f"{row.realization_percent:.2f}" if row.realization_percent is not None else "",
            row.status,
        ])
    return output.getvalue()
