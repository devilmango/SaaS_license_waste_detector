"""Join reclaim estimates with later invoice periods and approved renewal changes."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .actions import ActionError, list_actions
from .contracts import list_contract_versions
from .engine import InputError
from .renewals import list_renewals
from .verification import _read_focus


@dataclass(frozen=True)
class SavingsOutcome:
    application: str
    owner: str
    cost_center: str
    contract_id: str
    period: str
    baseline_period_start: date
    baseline_period_end: date
    current_period_start: date
    current_period_end: date
    estimated_annual_savings_inr: Decimal
    pending_estimated_savings_inr: Decimal
    realized_annual_savings_inr: Decimal
    renewal_commitment_change_inr: Decimal
    renewal_decision: str
    renewal_status: str
    status: str


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _findings(path: Path) -> dict[str, dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"finding_id", "application", "contract_id", "cost_center"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ActionError(f"{path}: findings CSV is missing columns: {', '.join(sorted(missing))}")
            rows = list(reader)
    except OSError as exc:
        raise ActionError(f"Cannot read findings CSV {path}: {exc}") from exc
    result: dict[str, dict[str, str]] = {}
    for line, row in enumerate(rows, start=2):
        finding_id = (row.get("finding_id") or "").strip()
        if not finding_id or finding_id in result:
            raise ActionError(f"{path}:{line}: finding_id is blank or duplicated")
        result[finding_id] = {key: (value or "").strip() for key, value in row.items() if key}
    return result


def _renewal_changes(contracts_path: Path | None, renewal_path: Path | None,
                     baseline_end: date, current_end: date
                     ) -> tuple[dict[str, tuple[Decimal, tuple[str, ...], int]],
                                dict[tuple[str, int], tuple[str, str]], dict[str, int]]:
    if contracts_path is None:
        return {}, {}, {}
    approved: dict[str, list] = {}
    latest_versions: dict[str, int] = {}
    for version in list_contract_versions(contracts_path):
        if version.event == "approved":
            approved.setdefault(_norm(version.contract_id), []).append(version)
    changes: dict[str, tuple[Decimal, tuple[str, ...], int]] = {}
    for key, versions in approved.items():
        versions.sort(key=lambda row: row.version)
        effective_versions = [version for version in versions if version.effective_date < current_end]
        if effective_versions:
            latest_versions[key] = effective_versions[-1].version
        current_candidates = [version for version in versions
                              if baseline_end <= version.effective_date < current_end]
        if not current_candidates:
            continue
        current = current_candidates[-1]
        previous_candidates = [version for version in versions if version.version < current.version]
        if previous_candidates:
            previous = previous_candidates[-1]
            changes[key] = (previous.annual_commitment_inr - current.annual_commitment_inr,
                            current.applications, current.version)
    statuses: dict[tuple[str, int], tuple[str, str]] = {}
    if renewal_path is not None:
        for item in list_renewals(renewal_path, as_of=current_end):
            statuses[(_norm(item.contract_id), item.version)] = (item.decision, item.status)
    return changes, statuses, latest_versions


def build_savings_outcomes(
    actions_path: Path,
    findings_path: Path,
    baseline_invoice_path: Path,
    current_invoice_path: Path,
    *,
    contracts_path: Path | None = None,
    renewals_path: Path | None = None,
) -> tuple[SavingsOutcome, ...]:
    """Reconcile active action estimates against a later invoice and contract renewal."""
    findings = _findings(findings_path)
    baseline, baseline_start, baseline_end = _read_focus(baseline_invoice_path)
    current, current_start, current_end = _read_focus(current_invoice_path)
    if baseline_end > current_start:
        raise InputError("baseline invoice period must end on or before the current invoice period starts")
    if renewals_path is not None and contracts_path is None:
        raise InputError("--renewals-ledger requires --contracts-ledger")
    renewal_changes, renewal_statuses, latest_contract_versions = _renewal_changes(
        contracts_path, renewals_path, baseline_end, current_end,
    )
    # Group by invoice dimensions first, then allocate an observed app/cost-center change
    # to owners in proportion to their reclaimed estimates so totals remain conserved.
    groups: dict[tuple[str, str], dict[str, object]] = {}
    for action in list_actions(actions_path):
        if action.status == "rejected":
            continue
        finding = findings.get(action.finding_id)
        if finding is None:
            raise ActionError(f"Action {action.action_id} refers to a finding missing from {findings_path}")
        application = finding.get("application", "").strip()
        if not application:
            raise ActionError(f"Finding {action.finding_id} has no application")
        center = finding.get("cost_center", "").strip() or "Unassigned"
        contract_id = finding.get("contract_id", "").strip() or application
        key = (_norm(application), _norm(center))
        group = groups.setdefault(key, {
            "application": application, "center": center, "actions": [],
        })
        owner = action.owner or finding.get("review_owner", "") or "Unassigned"
        group["actions"].append((action, owner, contract_id))

    period_days_before = (baseline_end - baseline_start).days
    period_days_current = (current_end - current_start).days
    output: list[SavingsOutcome] = []
    period_label = f"{current_start.isoformat()}/{current_end.isoformat()}"
    for key, group in groups.items():
        before = baseline.get(key, Decimal("0"))
        after = current.get(key, Decimal("0"))
        has_baseline_charge = key in baseline
        observed = ((before / period_days_before - after / period_days_current) * Decimal("365")
                    if has_baseline_charge else Decimal("0"))
        owner_values: dict[tuple[str, str], dict[str, Decimal | int | str]] = {}
        all_actions = group["actions"]
        for action, owner, contract_id in all_actions:
            entry = owner_values.setdefault((owner, _norm(contract_id)), {
                "owner": owner, "contract_id": contract_id,
                "pending": Decimal("0"), "reclaimed_estimate": Decimal("0"),
            })
            if action.status == "reclaimed":
                entry["reclaimed_estimate"] = Decimal(entry["reclaimed_estimate"]) + action.estimated_annual_savings_inr
            elif action.status in {"proposed", "approved"}:
                entry["pending"] = Decimal(entry["pending"]) + action.estimated_annual_savings_inr
        total_reclaimed_estimate = sum(
            (Decimal(value["reclaimed_estimate"]) for value in owner_values.values()), Decimal("0"),
        )
        if total_reclaimed_estimate:
            for value in owner_values.values():
                value["realized"] = observed * Decimal(value["reclaimed_estimate"]) / total_reclaimed_estimate
        else:
            for value in owner_values.values():
                value["realized"] = Decimal("0")
        # Attribute each contract's annual commitment movement across its application
        # owners using the same reclaimed estimate weights used for invoice outcomes.
        contract_groups: dict[str, list[dict[str, Decimal | int | str]]] = {}
        for value in owner_values.values():
            contract_groups.setdefault(_norm(str(value["contract_id"])), []).append(value)
        for contract_key, owners in contract_groups.items():
            change_record = renewal_changes.get(contract_key)
            contract_change, applications, changed_version = change_record or (Decimal("0"), (), 0)
            normalized_apps = {_norm(application) for application in applications}
            change = (contract_change / Decimal(len(normalized_apps))
                      if normalized_apps and _norm(str(group["application"])) in normalized_apps else Decimal("0"))
            contract_estimate = sum((Decimal(item["reclaimed_estimate"]) for item in owners), Decimal("0"))
            for item in owners:
                weight = (Decimal(item["reclaimed_estimate"]) / contract_estimate if contract_estimate else
                          Decimal("1") / Decimal(len(owners)))
                renewal_change = change * weight
                renewal_version = changed_version or latest_contract_versions.get(contract_key, 0)
                decision, renewal_status = renewal_statuses.get(
                    (contract_key, renewal_version),
                    ("", "not_provided" if renewals_path is None else "no_campaign"),
                )
                if not change and contracts_path is not None and renewal_status == "no_campaign" and not decision:
                    renewal_status = "no_change_recorded"
                reclaimed = Decimal(item["reclaimed_estimate"])
                pending = Decimal(item["pending"])
                status = "no_baseline_charge" if not has_baseline_charge else (
                    "pending_reclaim" if not reclaimed and pending else
                    "invoice_reconciled" if reclaimed else "no_completed_reclaim"
                )
                output.append(SavingsOutcome(
                    str(group["application"]), str(item["owner"]), str(group["center"]),
                    str(item["contract_id"]), period_label, baseline_start, baseline_end,
                    current_start, current_end, reclaimed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    pending.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    Decimal(item["realized"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    renewal_change.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), decision,
                    renewal_status, status,
                ))
    return tuple(sorted(output, key=lambda row: (row.application.casefold(), row.owner.casefold(), row.period)))


def format_savings_outcomes(rows: Iterable[SavingsOutcome]) -> str:
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "application", "owner", "cost_center", "contract_id", "period",
        "baseline_period_start", "baseline_period_end", "current_period_start", "current_period_end",
        "estimated_annual_savings_inr", "pending_estimated_savings_inr", "realized_annual_savings_inr",
        "renewal_commitment_change_inr", "renewal_decision", "renewal_status", "status",
    ])
    for row in rows:
        writer.writerow([
            row.application, row.owner, row.cost_center, row.contract_id, row.period,
            row.baseline_period_start.isoformat(), row.baseline_period_end.isoformat(),
            row.current_period_start.isoformat(), row.current_period_end.isoformat(),
            f"{row.estimated_annual_savings_inr:.2f}", f"{row.pending_estimated_savings_inr:.2f}",
            f"{row.realized_annual_savings_inr:.2f}", f"{row.renewal_commitment_change_inr:.2f}",
            row.renewal_decision, row.renewal_status, row.status,
        ])
    return output.getvalue()
