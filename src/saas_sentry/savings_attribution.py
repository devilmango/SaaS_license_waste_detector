"""Match invoice line-item movements to completed reclaim actions."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .actions import ActionError, list_actions
from .engine import InputError
from .savings_outcomes import _findings, _norm


@dataclass(frozen=True)
class SavingsAttribution:
    action_id: str
    finding_id: str
    application: str
    owner: str
    cost_center: str
    contract_id: str
    baseline_invoice_id: str
    current_invoice_id: str
    baseline_line_item_id: str
    current_line_item_id: str
    baseline_amount_inr: Decimal
    current_amount_inr: Decimal
    observed_annualized_reduction_inr: Decimal
    quantity_savings_inr: Decimal
    price_change_inr: Decimal
    estimated_annual_savings_inr: Decimal
    confidence: str
    confidence_reason: str
    invoice_reduction_observed: bool
    savings_verified: bool


def _rows(path: Path) -> tuple[list[dict[str, str]], date, date]:
    required = {"application", "cost_center", "billing_period_start", "billing_period_end", "actual_billed_cost_inr"}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise InputError(f"{path}: invoice CSV missing columns: {', '.join(sorted(missing))}")
            rows = [{key: (value or "").strip() for key, value in row.items() if key}
                    for row in reader]
    except OSError as exc:
        raise InputError(f"Cannot read invoice CSV {path}: {exc}") from exc
    periods: set[tuple[date, date]] = set()
    for line, row in enumerate(rows, start=2):
        try:
            start = date.fromisoformat(row["billing_period_start"][:10])
            end = date.fromisoformat(row["billing_period_end"][:10])
            amount = Decimal(row["actual_billed_cost_inr"])
        except (ValueError, InvalidOperation) as exc:
            raise InputError(f"{path}:{line}: invalid dates or billed amount") from exc
        if end <= start or not amount.is_finite() or amount < 0:
            raise InputError(f"{path}:{line}: billing period or amount is invalid")
        for field in ("quantity", "unit_price_inr"):
            if row.get(field):
                try:
                    metric = Decimal(row[field])
                except InvalidOperation as exc:
                    raise InputError(f"{path}:{line}: {field} must be numeric") from exc
                if not metric.is_finite() or metric < 0:
                    raise InputError(f"{path}:{line}: {field} must be finite and non-negative")
        periods.add((start, end))
    if len(periods) != 1:
        raise InputError(f"{path}: attribution requires one consistent invoice period per file")
    return rows, *next(iter(periods))


def _amount(rows: list[dict[str, str]]) -> Decimal:
    return sum((Decimal(row["actual_billed_cost_inr"]) for row in rows), Decimal("0"))


def _same_assignment(row: dict[str, str], finding: dict[str, str]) -> bool:
    return (_norm(row.get("application", "")) == _norm(finding.get("application", ""))
            and _norm(row.get("cost_center", "") or "Unassigned")
            == _norm(finding.get("cost_center", "") or "Unassigned"))


def attribute_savings(
    actions_path: Path, findings_path: Path, baseline_invoice_path: Path, current_invoice_path: Path,
) -> tuple[SavingsAttribution, ...]:
    """Attribute invoice movement to reclaimed actions with explicit evidence confidence."""
    findings = _findings(findings_path)
    baseline, baseline_start, baseline_end = _rows(baseline_invoice_path)
    current, current_start, current_end = _rows(current_invoice_path)
    if baseline_end > current_start:
        raise InputError("baseline invoice period must end on or before current invoice period starts")
    baseline_days = Decimal((baseline_end - baseline_start).days)
    current_days = Decimal((current_end - current_start).days)
    output: list[SavingsAttribution] = []
    reclaimed = [action for action in list_actions(actions_path) if action.status == "reclaimed"]
    for action in reclaimed:
        finding = findings.get(action.finding_id)
        if finding is None:
            raise ActionError(f"Action {action.action_id} refers to a missing finding")
        app_rows = [row for row in current if _same_assignment(row, finding)]
        old_app_rows = [row for row in baseline if _same_assignment(row, finding)]
        email = _norm(finding.get("email", ""))
        employee_id = _norm(finding.get("employee_id", ""))
        exact = lambda row: ((email and _norm(row.get("email", "")) == email)
                             or (employee_id and _norm(row.get("employee_id", "")) == employee_id))
        exact_current = [row for row in app_rows if exact(row)] if email or employee_id else []
        exact_baseline = [row for row in old_app_rows if exact(row)] if email or employee_id else []
        line_id = finding.get("invoice_line_item_id", "").strip()
        sku = finding.get("sku", "").strip()
        selector = "line_item_id" if line_id else "sku" if sku else ""
        selector_value = line_id or sku
        if not exact_current and selector:
            exact_current = [row for row in app_rows if _norm(row.get(selector, "")) == _norm(selector_value)]
            exact_baseline = [row for row in old_app_rows if _norm(row.get(selector, "")) == _norm(selector_value)]
        confidence, reason = "low", "only application and cost center matched; invoice movement may include unrelated changes"
        matched_current, matched_baseline = app_rows, old_app_rows
        if exact_current and exact_baseline:
            matched_current, matched_baseline = exact_current, exact_baseline
            confidence, reason = "high", "invoice line matched by employee identity" if email or employee_id else "invoice line matched by configured line-item key"
        else:
            current_line_ids = {row.get("line_item_id", "") for row in app_rows if row.get("line_item_id")}
            baseline_line_ids = {row.get("line_item_id", "") for row in old_app_rows if row.get("line_item_id")}
            shared_line_ids = current_line_ids & baseline_line_ids
            if shared_line_ids:
                matched_current = [row for row in app_rows if row.get("line_item_id") in shared_line_ids]
                matched_baseline = [row for row in old_app_rows if row.get("line_item_id") in shared_line_ids]
                confidence = "high" if len(shared_line_ids) == 1 else "medium"
                reason = "matched by stable invoice line-item ID" if confidence == "high" else "matched multiple invoice line-item IDs"
            else:
                current_skus = {row.get("sku", "") for row in app_rows if row.get("sku")}
                baseline_skus = {row.get("sku", "") for row in old_app_rows if row.get("sku")}
                shared_skus = current_skus & baseline_skus
                if len(shared_skus) == 1:
                    matched_current = [row for row in app_rows if row.get("sku") in shared_skus]
                    matched_baseline = [row for row in old_app_rows if row.get("sku") in shared_skus]
                    confidence, reason = "medium", "matched by unique SKU within application and cost center"
            contract_id = _norm(finding.get("contract_id", ""))
            contract_current = [row for row in app_rows if contract_id and _norm(row.get("contract_id", "")) == contract_id]
            contract_baseline = [row for row in old_app_rows if contract_id and _norm(row.get("contract_id", "")) == contract_id]
            if not shared_line_ids and not (len(current_skus) == 1 and len(baseline_skus) == 1 and current_skus == baseline_skus) and contract_current and contract_baseline:
                matched_current, matched_baseline = contract_current, contract_baseline
                confidence, reason = "medium", "matched by contract, application, and cost center"
        before, after = _amount(matched_baseline), _amount(matched_current)
        observed = before / baseline_days * Decimal("365") - after / current_days * Decimal("365")
        observed = observed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        quantity_savings = Decimal("0")
        price_change = Decimal("0")
        if len(matched_baseline) == len(matched_current) == 1:
            old, new = matched_baseline[0], matched_current[0]
            try:
                old_qty, new_qty = Decimal(old["quantity"]), Decimal(new["quantity"])
                old_price, new_price = Decimal(old["unit_price_inr"]), Decimal(new["unit_price_inr"])
                quantity_savings = (old_qty - new_qty) * old_price * Decimal("365") / baseline_days
                price_change = new_qty * (
                    old_price * Decimal("365") / baseline_days
                    - new_price * Decimal("365") / current_days
                )
            except (KeyError, InvalidOperation, ZeroDivisionError):
                pass
        reduction = observed > 0
        verified = (reduction and confidence == "high"
                    and quantity_savings >= action.estimated_annual_savings_inr * Decimal("0.8"))
        output.append(SavingsAttribution(
            action.action_id, action.finding_id, finding.get("application", ""), action.owner,
            finding.get("cost_center", "") or "Unassigned", finding.get("contract_id", ""),
            ";".join(sorted({row.get("invoice_id", "") for row in matched_baseline})),
            ";".join(sorted({row.get("invoice_id", "") for row in matched_current})),
            ";".join(sorted({row.get("line_item_id", "") for row in matched_baseline})),
            ";".join(sorted({row.get("line_item_id", "") for row in matched_current})),
            before.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            after.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), observed,
            quantity_savings.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            price_change.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            action.estimated_annual_savings_inr.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            confidence, reason, reduction, verified,
        ))
    return tuple(output)


def format_attributions(rows: Iterable[SavingsAttribution]) -> str:
    from io import StringIO
    fields = tuple(SavingsAttribution.__dataclass_fields__)
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        values = {field: getattr(row, field) for field in fields}
        for field in ("baseline_amount_inr", "current_amount_inr", "observed_annualized_reduction_inr",
                      "quantity_savings_inr", "price_change_inr", "estimated_annual_savings_inr"):
            values[field] = f"{values[field]:.2f}"
        for field in ("invoice_reduction_observed", "savings_verified"):
            values[field] = str(values[field]).lower()
        writer.writerow(values)
    return output.getvalue()
