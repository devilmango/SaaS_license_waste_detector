"""Contract entitlement and billable-seat overage analysis."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .engine import InputError, _rows


@dataclass(frozen=True)
class EntitlementRow:
    contract_id: str
    application: str
    entitled_seats: int
    active_seats: int
    overage_seats: int
    unused_entitlement_seats: int
    estimated_annual_overage_inr: Decimal
    status: str


def analyze_entitlements(path: Path, billing_path: Path) -> tuple[EntitlementRow, ...]:
    """Compare contract entitlements with active license assignments and price overages."""
    contracts = _rows(path, {"contract_id", "application", "entitled_seats"})
    billing = _rows(billing_path, {"contract_id", "application", "license_status", "annual_cost"})
    assigned: dict[tuple[str, str], tuple[int, Decimal]] = {}
    for line, row in enumerate(billing, start=2):
        if row["license_status"].casefold() != "active":
            continue
        key = (_normalize(row["contract_id"]), _normalize(row["application"]))
        if not all(key):
            raise InputError(f"{billing_path}:{line}: active license must have contract_id and application")
        try:
            cost = Decimal(row["annual_cost"])
        except InvalidOperation as exc:
            raise InputError(f"{billing_path}:{line}: annual_cost must be numeric") from exc
        if not cost.is_finite() or cost < 0:
            raise InputError(f"{billing_path}:{line}: annual_cost must be finite and non-negative")
        seats, annual = assigned.get(key, (0, Decimal("0")))
        assigned[key] = (seats + 1, annual + cost)

    seen: set[tuple[str, str]] = set()
    result: list[EntitlementRow] = []
    for line, row in enumerate(contracts, start=2):
        contract, app = row["contract_id"].strip(), row["application"].strip()
        key = (_normalize(contract), _normalize(app))
        if not all(key):
            raise InputError(f"{path}:{line}: contract_id and application cannot be blank")
        if key in seen:
            raise InputError(f"{path}:{line}: duplicate contract/application entitlement")
        seen.add(key)
        try:
            entitlement = int(row["entitled_seats"])
        except ValueError as exc:
            raise InputError(f"{path}:{line}: entitled_seats must be a whole number") from exc
        if entitlement < 0:
            raise InputError(f"{path}:{line}: entitled_seats cannot be negative")
        active, annual_cost = assigned.get(key, (0, Decimal("0")))
        over = max(active - entitlement, 0)
        unused = max(entitlement - active, 0)
        per_seat = annual_cost / active if active else Decimal("0")
        estimated = (per_seat * over).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        status = "over_entitlement" if over else "unused_entitlement" if unused else "within_entitlement"
        result.append(EntitlementRow(contract, app, entitlement, active, over, unused, estimated, status))
    # Assignments without a matching entitlement are actionable instead of silently disappearing.
    for (contract_key, app_key), (active, annual_cost) in assigned.items():
        if (contract_key, app_key) in seen:
            continue
        result.append(EntitlementRow(
            contract_key, app_key, 0, active, active, 0,
            annual_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "missing_entitlement",
        ))
    return tuple(sorted(result, key=lambda item: (item.status, item.contract_id.casefold(), item.application.casefold())))


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def format_entitlements(rows: Iterable[EntitlementRow]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "contract_id", "application", "entitled_seats", "active_seats", "overage_seats",
        "unused_entitlement_seats", "estimated_annual_overage_inr", "status",
    ])
    for row in rows:
        writer.writerow([
            row.contract_id, row.application, row.entitled_seats, row.active_seats,
            row.overage_seats, row.unused_entitlement_seats,
            f"{row.estimated_annual_overage_inr:.2f}", row.status,
        ])
    return output.getvalue()
