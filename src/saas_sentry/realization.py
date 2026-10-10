"""Modeled-versus-realized savings reporting for reclaim actions."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .actions import ActionError, list_actions


@dataclass(frozen=True)
class RealizationRow:
    scope_type: str
    scope: str
    action_count: int
    reclaimed_count: int
    open_count: int
    rejected_count: int
    estimated_reclaimed_annual_inr: Decimal
    actual_annual_inr: Decimal
    variance_inr: Decimal
    realization_percent: Decimal | None
    pending_estimated_annual_inr: Decimal


def _findings(path: Path) -> dict[str, dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "finding_id" not in reader.fieldnames:
                raise ActionError(f"{path}: expected a findings CSV with a finding_id column")
            rows = list(reader)
    except OSError as exc:
        raise ActionError(f"Cannot read findings CSV {path}: {exc}") from exc
    result: dict[str, dict[str, str]] = {}
    for line, row in enumerate(rows, start=2):
        finding_id = (row.get("finding_id") or "").strip()
        if not finding_id:
            raise ActionError(f"{path}:{line}: finding_id cannot be blank")
        if finding_id in result:
            raise ActionError(f"{path}:{line}: duplicate finding_id {finding_id!r}")
        result[finding_id] = {key: (value or "").strip() for key, value in row.items() if key}
    return result


def build_realization_report(actions_path: Path, findings_path: Path) -> tuple[RealizationRow, ...]:
    """Aggregate actual annual savings, variance, and pending estimates by useful dimensions."""
    findings = _findings(findings_path)
    actions = list_actions(actions_path)
    # Each accumulator tracks count fields and annualized savings amounts.
    buckets: dict[tuple[str, str], dict[str, Decimal | int]] = {}

    def add(scope_type: str, scope: str, action, status: str) -> None:
        bucket = buckets.setdefault((scope_type, scope), {
            "action_count": 0, "reclaimed_count": 0, "open_count": 0, "rejected_count": 0,
            "estimated": Decimal("0"), "actual": Decimal("0"), "pending": Decimal("0"),
        })
        bucket["action_count"] = int(bucket["action_count"]) + 1
        if status == "reclaimed":
            bucket["reclaimed_count"] = int(bucket["reclaimed_count"]) + 1
            bucket["estimated"] = Decimal(bucket["estimated"]) + action.estimated_annual_savings_inr
            bucket["actual"] = Decimal(bucket["actual"]) + (action.actual_annual_savings_inr or Decimal("0"))
        elif status in {"proposed", "approved"}:
            bucket["open_count"] = int(bucket["open_count"]) + 1
            bucket["pending"] = Decimal(bucket["pending"]) + action.estimated_annual_savings_inr
        elif status == "rejected":
            bucket["rejected_count"] = int(bucket["rejected_count"]) + 1

    for action in actions:
        finding = findings.get(action.finding_id, {})
        add("overall", "All actions", action, action.status)
        dimensions = {
            "owner": action.owner or "Unassigned",
            "application": finding.get("application", "Unknown finding") or "Unknown finding",
            "department": finding.get("department", "Unassigned") or "Unassigned",
            "cost_center": finding.get("cost_center", "Unassigned") or "Unassigned",
        }
        for scope_type, value in dimensions.items():
            add(scope_type, value, action, action.status)
        if action.status == "reclaimed":
            month = action.updated_on[:7]
            add("completion_month", month, action, action.status)

    result: list[RealizationRow] = []
    for (scope_type, scope), values in sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1].casefold())):
        estimated = Decimal(values["estimated"])
        actual = Decimal(values["actual"])
        ratio = (actual * Decimal("100") / estimated).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if estimated else None
        result.append(RealizationRow(
            scope_type, scope, int(values["action_count"]), int(values["reclaimed_count"]),
            int(values["open_count"]), int(values["rejected_count"]),
            estimated.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            actual.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            (actual - estimated).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ratio,
            Decimal(values["pending"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        ))
    return tuple(result)


def format_realization_report(rows: Iterable[RealizationRow]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "scope_type", "scope", "action_count", "reclaimed_count", "open_count", "rejected_count",
        "estimated_reclaimed_annual_inr", "actual_annual_inr", "variance_inr", "realization_percent",
        "pending_estimated_annual_inr",
    ])
    for row in rows:
        writer.writerow([
            row.scope_type, row.scope, row.action_count, row.reclaimed_count, row.open_count,
            row.rejected_count, f"{row.estimated_reclaimed_annual_inr:.2f}",
            f"{row.actual_annual_inr:.2f}", f"{row.variance_inr:.2f}",
            f"{row.realization_percent:.2f}" if row.realization_percent is not None else "",
            f"{row.pending_estimated_annual_inr:.2f}",
        ])
    return output.getvalue()
