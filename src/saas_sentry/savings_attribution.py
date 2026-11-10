"""Match invoice line-item movements to completed reclaim actions."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .actions import ActionError, list_actions
from .engine import InputError
from .savings_outcomes import _findings, _norm, _renewal_changes
from .tier_optimization import list_tier_actions


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


@dataclass(frozen=True)
class TierSavingsAttribution:
    action_id: str
    email: str
    application: str
    contract_id: str
    current_tier: str
    recommended_tier: str
    period: str
    estimated_annual_savings_inr: Decimal
    observed_annualized_reduction_inr: Decimal
    baseline_invoice_ids: str
    current_invoice_ids: str
    confidence: str
    confidence_reason: str
    invoice_reduction_observed: bool
    savings_verified: bool
    renewal_commitment_change_inr: Decimal
    renewal_decision: str
    renewal_status: str
    status: str


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


def _tier_values(row: dict[str, str]) -> set[str]:
    return {_norm(row.get(field, "")) for field in (
        "tier", "license_tier", "sku", "sku_id", "provider_tier_id",
    ) if row.get(field, "").strip()}


def _select_tier_rows(rows: list[dict[str, str]], expected: set[str]) -> list[dict[str, str]]:
    if not expected:
        return rows
    matched = [row for row in rows if _tier_values(row) & expected]
    return matched or rows


def attribute_tier_savings(
    tier_actions_path: Path, baseline_invoice_path: Path, current_invoice_path: Path, *,
    contracts_path: Path | None = None, renewals_path: Path | None = None,
) -> tuple[TierSavingsAttribution, ...]:
    """Compare approved/executed tier changes with later invoices and approved renewals."""
    baseline, baseline_start, baseline_end = _rows(baseline_invoice_path)
    current, current_start, current_end = _rows(current_invoice_path)
    if baseline_end > current_start:
        raise InputError("baseline invoice period must end on or before current invoice period starts")
    if renewals_path is not None and contracts_path is None:
        raise InputError("--renewals-ledger requires --contracts-ledger")
    renewal_changes, renewal_statuses, latest_contract_versions = _renewal_changes(
        contracts_path, renewals_path, baseline_end, current_end,
    )
    period = f"{current_start.isoformat()}/{current_end.isoformat()}"
    old_days, new_days = Decimal((baseline_end - baseline_start).days), Decimal((current_end - current_start).days)
    output: list[TierSavingsAttribution] = []
    for action in list_tier_actions(tier_actions_path):
        status = action.get("status", "")
        if status not in {"approved", "verified"}:
            continue
        application = action.get("application", "")
        contract_id = action.get("contract_id", "")
        scoped = [row for row in baseline if _norm(row.get("application", "")) == _norm(application)]
        scoped_current = [row for row in current if _norm(row.get("application", "")) == _norm(application)]
        if contract_id:
            old_contract = [row for row in scoped if _norm(row.get("contract_id", "")) == _norm(contract_id)]
            new_contract = [row for row in scoped_current if _norm(row.get("contract_id", "")) == _norm(contract_id)]
            if old_contract or new_contract:
                scoped, scoped_current = old_contract, new_contract

        confidence, reason = "none", "no matching invoice lines for this application or contract"
        matched_old: list[dict[str, str]] = []
        matched_new: list[dict[str, str]] = []
        invoice_status = "awaiting_later_invoice"
        if status == "approved":
            invoice_status = "awaiting_execution"
        else:
            try:
                verified_at = datetime.fromisoformat(action.get("occurred_at", "").replace("Z", "+00:00"))
                verified_on = verified_at.date()
            except ValueError:
                verified_on = None
            if verified_on and current_end <= verified_on:
                invoice_status = "invoice_predates_downgrade"
                reason = "current invoice period ended before provider verification"
            else:
                email = _norm(action.get("email", ""))
                old_identity = [row for row in scoped if email and _norm(row.get("email", "")) == email]
                new_identity = [row for row in scoped_current if email and _norm(row.get("email", "")) == email]
                old_expected = {_norm(action.get("current_tier", "")),
                                _norm(action.get("current_provider_tier_id", "")),
                                _norm(action.get("provider_license_id", ""))} - {""}
                new_expected = {_norm(action.get("recommended_tier", "")),
                                _norm(action.get("recommended_provider_tier_id", ""))} - {""}
                if old_identity and new_identity:
                    matched_old = _select_tier_rows(old_identity, old_expected)
                    matched_new = _select_tier_rows(new_identity, new_expected)
                    confidence = "high"
                    reason = "invoice lines matched to the downgraded user identity"
                    if len(matched_old) != 1 or len(matched_new) != 1:
                        confidence, reason = "medium", "identity matched, but multiple invoice lines remain"
                else:
                    old_sku = [row for row in scoped if _tier_values(row) & old_expected]
                    new_sku = [row for row in scoped_current if _tier_values(row) & new_expected]
                    if len(old_sku) == 1 and len(new_sku) == 1:
                        matched_old, matched_new = old_sku, new_sku
                        confidence, reason = "medium", "unique old and replacement tiers matched within the application and contract"
                    else:
                        old_line_ids = {row.get("line_item_id", "") for row in scoped if row.get("line_item_id")}
                        new_line_ids = {row.get("line_item_id", "") for row in scoped_current if row.get("line_item_id")}
                        shared = old_line_ids & new_line_ids
                        if len(shared) == 1:
                            line_id = next(iter(shared))
                            matched_old = [row for row in scoped if row.get("line_item_id") == line_id]
                            matched_new = [row for row in scoped_current if row.get("line_item_id") == line_id]
                            confidence, reason = "high", "matched by one stable invoice line-item ID"
                        elif scoped and scoped_current:
                            matched_old, matched_new = scoped, scoped_current
                            confidence, reason = "low", "only application and contract scope matched; unrelated invoice changes may be included"

                if matched_old and matched_new:
                    before = _amount(matched_old) / old_days * Decimal("365")
                    after = _amount(matched_new) / new_days * Decimal("365")
                    observed = (before - after).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    invoice_status = "invoice_reconciled" if confidence in {"high", "medium"} else "low_confidence_match"
                else:
                    observed = Decimal("0.00")
                    invoice_status = "no_invoice_match"

        if status == "approved" or invoice_status in {"invoice_predates_downgrade", "awaiting_later_invoice", "no_invoice_match"}:
            observed = Decimal("0.00")
        reduction = observed > 0
        estimate = Decimal(action.get("annual_savings_inr", "0") or "0")
        verified = reduction and confidence == "high" and observed >= estimate * Decimal("0.8")
        renewal_change = Decimal("0.00")
        decision, renewal_status = "", "not_provided" if renewals_path is None else "no_campaign"
        contract_key = _norm(contract_id)
        renewal_record = renewal_changes.get(contract_key)
        if status == "verified" and renewal_record:
            change, applications, version = renewal_record
            if not applications or _norm(application) in {_norm(value) for value in applications}:
                renewal_change = change
            renewal_version = version or latest_contract_versions.get(contract_key, 0)
            decision, renewal_status = renewal_statuses.get(
                (contract_key, renewal_version), ("", "no_campaign" if renewals_path else "not_provided"),
            )
        elif contracts_path is not None:
            renewal_status = "no_change_recorded"
        output.append(TierSavingsAttribution(
            action["action_id"], action.get("email", ""), application, contract_id,
            action.get("current_tier", ""), action.get("recommended_tier", ""), period,
            estimate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), observed,
            ";".join(sorted({row.get("invoice_id", "") for row in matched_old if row.get("invoice_id")})),
            ";".join(sorted({row.get("invoice_id", "") for row in matched_new if row.get("invoice_id")})),
            confidence, reason, reduction, verified,
            renewal_change.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), decision,
            renewal_status, invoice_status,
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


def format_tier_attributions(rows: Iterable[TierSavingsAttribution]) -> str:
    from io import StringIO
    fields = tuple(TierSavingsAttribution.__dataclass_fields__)
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        values = {field: getattr(row, field) for field in fields}
        for field in ("estimated_annual_savings_inr", "observed_annualized_reduction_inr",
                      "renewal_commitment_change_inr"):
            values[field] = f"{values[field]:.2f}"
        for field in ("invoice_reduction_observed", "savings_verified"):
            values[field] = str(values[field]).lower()
        writer.writerow(values)
    return output.getvalue()
