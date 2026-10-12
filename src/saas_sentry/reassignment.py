"""Match confirmed reclaim candidates to approved requests for the same seat."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from .engine import CurrencyConfig, InputError, _rows, load_inputs


DEMAND_FIELDS = {
    "employee_id", "email", "application", "contract_id", "priority",
    "requested_on", "reason",
}


@dataclass(frozen=True)
class Reassignment:
    finding_id: str
    from_employee: str
    from_email: str
    to_employee: str
    to_email: str
    application: str
    contract_id: str
    annual_cost_inr: Decimal
    priority: str
    status: str
    note: str


def _key(value: str) -> str:
    return " ".join(value.casefold().split())


def plan_reassignments(
    *, hr_path: Path, usage_path: Path, billing_path: Path, findings_path: Path,
    demand_path: Path, currency: CurrencyConfig | None = None,
) -> tuple[Reassignment, ...]:
    """Create a conservative, non-mutating seat reassignment proposal.

    Both supply and demand must identify the same application and contract. Only
    confirmed findings and active HR requesters are eligible.
    """
    inputs = load_inputs(hr_path, usage_path, billing_path, currency=currency)
    demands = _rows(demand_path, DEMAND_FIELDS)
    try:
        findings_handle = findings_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise InputError(f"Cannot read findings {findings_path}: {exc}") from exc
    with findings_handle:
        reader = csv.DictReader(findings_handle)
        required = {"finding_id", "employee", "email", "employee_id", "application", "contract_id", "review_status", "annualized_candidate_cost_inr"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise InputError(f"{findings_path}: missing required columns: {', '.join(sorted(missing))}")
        supplies = list(reader)

    all_people = list(inputs.people.values())
    def resolve_person(email: str, employee_id: str, *, active_only: bool = False):
        email_key, id_key = email.strip().casefold(), employee_id.strip().casefold()
        matches = [
            person for person in all_people
            if (not active_only or person.status == "active")
            and ((bool(id_key) and person.employee_id.casefold() == id_key)
                 or (bool(email_key) and email_key in {person.email.casefold(), *person.aliases}))
        ]
        unique = {person.key: person for person in matches}
        return next(iter(unique.values())) if len(unique) == 1 else None

    assigned_applications = {
        (license.person_key, _key(license.application))
        for license in inputs.licenses if license.status == "active"
    }
    demand_by_seat: dict[tuple[str, str], list[dict[str, str]]] = {}
    seen_demands: set[tuple[str, str, str]] = set()
    for line, row in enumerate(demands, start=2):
        app, contract = row["application"].strip(), row["contract_id"].strip()
        if not app or not contract:
            raise InputError(f"{demand_path}:{line}: application and contract_id are required")
        if not row["email"].strip() and not row["employee_id"].strip():
            raise InputError(f"{demand_path}:{line}: provide employee_id or email")
        try:
            requested_on = date.fromisoformat(row["requested_on"])
        except ValueError as exc:
            raise InputError(f"{demand_path}:{line}: requested_on must use YYYY-MM-DD") from exc
        priority = (row["priority"] or "normal").casefold()
        if priority not in {"high", "normal", "low"}:
            raise InputError(f"{demand_path}:{line}: priority must be high, normal, or low")
        person = resolve_person(row["email"], row["employee_id"], active_only=True)
        if not person:
            raise InputError(f"{demand_path}:{line}: request identity is unmatched, ambiguous, or not active in HR")
        if (person.key, _key(app)) in assigned_applications:
            raise InputError(f"{demand_path}:{line}: requester already has an active {app} license")
        demand_key = (person.key, _key(app), _key(contract))
        if demand_key in seen_demands:
            raise InputError(f"{demand_path}:{line}: duplicate request for the same employee, application, and contract")
        seen_demands.add(demand_key)
        row["_employee"] = person.employee
        row["_email"] = person.email or row["email"].strip()
        row["_person_key"] = person.key
        row["_requested_on"] = requested_on.isoformat()
        row["_priority"] = priority
        demand_by_seat.setdefault((_key(app), _key(contract)), []).append(row)

    priority_order = {"high": 0, "normal": 1, "low": 2}
    for requests in demand_by_seat.values():
        requests.sort(key=lambda row: (priority_order[row["_priority"]], row["_requested_on"], row["_email"].casefold()))

    available: dict[tuple[str, str], list[dict[str, str]]] = {}
    seen_findings: set[str] = set()
    for line, row in enumerate(supplies, start=2):
        if row.get("review_status", "").casefold() != "confirmed":
            continue
        app, contract, finding_id = row.get("application", "").strip(), row.get("contract_id", "").strip(), row.get("finding_id", "").strip()
        if not app or not contract or not finding_id:
            raise InputError(f"{findings_path}:{line}: confirmed finding requires finding_id, application, and contract_id")
        if finding_id in seen_findings:
            raise InputError(f"{findings_path}:{line}: duplicate confirmed finding_id {finding_id!r}")
        seen_findings.add(finding_id)
        try:
            amount = Decimal(row["annualized_candidate_cost_inr"])
        except InvalidOperation as exc:
            raise InputError(f"{findings_path}:{line}: annualized_candidate_cost_inr must be numeric") from exc
        if not amount.is_finite() or amount < 0:
            raise InputError(f"{findings_path}:{line}: annualized_candidate_cost_inr must be finite and non-negative")
        source_person = resolve_person(row.get("email", ""), row.get("employee_id", ""))
        source_key = source_person.key if source_person else None
        if not source_key or not any(
            license.person_key == source_key and license.status == "active"
            and _key(license.application) == _key(app) and _key(license.contract_id) == _key(contract)
            for license in inputs.licenses
        ):
            raise InputError(f"{findings_path}:{line}: confirmed finding does not match an active billed seat for its contract")
        available.setdefault((_key(app), _key(contract)), []).append(row)

    result: list[Reassignment] = []
    for seat_key, sources in available.items():
        requests = demand_by_seat.get(seat_key, [])
        sources.sort(key=lambda row: (row["application"].casefold(), row["email"].casefold(), row["finding_id"]))
        for source in sources:
            if requests:
                target = requests.pop(0)
                result.append(Reassignment(
                    source["finding_id"], source["employee"], source["email"], target["_employee"],
                    target["_email"], source["application"], source["contract_id"],
                    Decimal(source["annualized_candidate_cost_inr"]), target["_priority"], "proposed",
                    "Requires human approval and provider-specific seat transfer; no accounts were changed.",
                ))
            else:
                result.append(Reassignment(
                    source["finding_id"], source["employee"], source["email"], "", "",
                    source["application"], source["contract_id"],
                    Decimal(source["annualized_candidate_cost_inr"]), "", "unmatched_supply",
                    "No active, unlicensed request matched this application and contract.",
                ))
    for requests in demand_by_seat.values():
        result.extend(Reassignment(
            "", "", "", row["_employee"], row["_email"], row["application"], row["contract_id"],
            Decimal("0"), row["_priority"], "unmatched_demand",
            "No confirmed reclaim candidate matched this application and contract.",
        ) for row in requests)
    return tuple(result)


def format_reassignments(rows: Iterable[Reassignment]) -> str:
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "finding_id", "from_employee", "from_email", "to_employee", "to_email", "application",
        "contract_id", "annual_cost_inr", "priority", "status", "note",
    ])
    for row in rows:
        writer.writerow([
            row.finding_id, row.from_employee, row.from_email, row.to_employee, row.to_email,
            row.application, row.contract_id, f"{row.annual_cost_inr:.2f}", row.priority, row.status, row.note,
        ])
    return output.getvalue()
