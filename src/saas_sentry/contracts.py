"""Versioned contract evidence, approval events, renewal tracking, and invoice checks."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
from typing import Iterable

from .engine import InputError


CONTRACT_FIELDS = (
    "contract_id", "version", "event", "vendor", "owner", "applications_json",
    "effective_date", "renewal_date", "notice_days", "annual_commitment_inr",
    "currency", "evidence_type", "evidence_ref", "actor", "occurred_on", "note",
)
EVIDENCE_TYPES = {"agreement", "order_form", "quote", "invoice", "amendment"}


@dataclass(frozen=True)
class ContractVersion:
    contract_id: str
    version: int
    event: str
    vendor: str
    owner: str
    applications: tuple[str, ...]
    effective_date: date
    renewal_date: date
    notice_days: int
    annual_commitment_inr: Decimal
    currency: str
    evidence_type: str
    evidence_ref: str
    recorded_by: str
    approved_by: str
    note: str


@dataclass(frozen=True)
class ContractInvoiceCheck:
    contract_id: str
    version: int
    vendor: str
    owner: str
    applications: str
    approval_status: str
    period_start: date
    period_end: date
    expected_period_cost_inr: Decimal
    actual_invoice_cost_inr: Decimal
    variance_inr: Decimal
    invoice_status: str
    renewal_date: date
    notice_deadline: date
    renewal_status: str
    evidence_type: str
    evidence_ref: str


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _load_events(path: Path) -> tuple[list[dict[str, str]], dict[tuple[str, int], dict[str, str]]]:
    if not path.exists():
        return [], {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(CONTRACT_FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: contract ledger is missing required columns")
            events = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read contract ledger {path}: {exc}") from exc
    states: dict[tuple[str, int], dict[str, str]] = {}
    latest: dict[str, int] = {}
    for line, event in enumerate(events, start=2):
        contract_id = event.get("contract_id", "").strip()
        event_type = event.get("event", "")
        try:
            version = int(event.get("version", ""))
            date.fromisoformat(event.get("occurred_on", ""))
        except ValueError as exc:
            raise InputError(f"{path}:{line}: version and event date are invalid") from exc
        if not contract_id or version < 1 or event_type not in {"versioned", "approved"}:
            raise InputError(f"{path}:{line}: invalid contract event")
        if not event.get("actor", "").strip():
            raise InputError(f"{path}:{line}: actor cannot be blank")
        key = (_norm(contract_id), version)
        state = states.get(key)
        if event_type == "versioned":
            if state or version != latest.get(key[0], 0) + 1:
                raise InputError(f"{path}:{line}: contract versions must be unique and sequential")
            try:
                apps = json.loads(event.get("applications_json", ""))
                effective = date.fromisoformat(event.get("effective_date", ""))
                renewal = date.fromisoformat(event.get("renewal_date", ""))
                notice = int(event.get("notice_days", ""))
                amount = Decimal(event.get("annual_commitment_inr", ""))
            except (ValueError, InvalidOperation, json.JSONDecodeError) as exc:
                raise InputError(f"{path}:{line}: invalid contract terms or applications JSON") from exc
            if not isinstance(apps, list) or not apps or any(not isinstance(app, str) or not app.strip() for app in apps):
                raise InputError(f"{path}:{line}: contract version requires named application(s)")
            if renewal < effective or notice < 0 or not amount.is_finite() or amount < 0:
                raise InputError(f"{path}:{line}: invalid dates, notice period, or annual commitment")
            if not all(event.get(field, "").strip() for field in (
                "vendor", "owner", "currency", "evidence_type", "evidence_ref",
            )):
                raise InputError(f"{path}:{line}: contract evidence and ownership fields cannot be blank")
            if event["evidence_type"] not in EVIDENCE_TYPES:
                raise InputError(f"{path}:{line}: unsupported contract evidence type")
            state = {**event, "approval_status": "pending", "recorded_by": event["actor"], "approved_by": ""}
            states[key] = state
            latest[key[0]] = version
            continue
        if not state or state.get("approval_status") != "pending":
            raise InputError(f"{path}:{line}: approval references a missing or already-approved version")
        if event.get("actor", "").casefold() == state.get("recorded_by", "").casefold():
            raise InputError(f"{path}:{line}: contract approver must differ from the person who recorded it")
        for field in CONTRACT_FIELDS:
            if field not in {"event", "actor", "occurred_on", "note"} and event.get(field, "") != state.get(field, ""):
                raise InputError(f"{path}:{line}: approval does not match the saved contract version")
        state.update({"approval_status": "approved", "approved_by": event["actor"]})
    return events, states


def list_contract_versions(path: Path) -> tuple[ContractVersion, ...]:
    _, states = _load_events(path)
    result = []
    for state in states.values():
        try:
            apps = tuple(json.loads(state["applications_json"]))
            amount = Decimal(state["annual_commitment_inr"])
            effective = date.fromisoformat(state["effective_date"])
            renewal = date.fromisoformat(state["renewal_date"])
            version = int(state["version"])
            notice = int(state["notice_days"])
        except (ValueError, InvalidOperation, json.JSONDecodeError) as exc:
            raise InputError(f"{path}: invalid saved contract version") from exc
        result.append(ContractVersion(
            state["contract_id"], version, state["approval_status"], state["vendor"], state["owner"],
            apps, effective, renewal, notice, amount, state["currency"], state["evidence_type"],
            state["evidence_ref"], state["recorded_by"], state["approved_by"], state.get("note", ""),
        ))
    return tuple(sorted(result, key=lambda item: (item.contract_id.casefold(), item.version)))


def _append_locked(path: Path, event: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise InputError(f"Contract ledger is locked at {lock_path}") from exc
    try:
        _, current = _load_events(path)
        key = (_norm(event["contract_id"]), int(event["version"]))
        current_versions = [version for (contract_key, version) in current if contract_key == key[0]]
        if event["event"] == "versioned":
            if key in current or key[1] != max(current_versions, default=0) + 1:
                raise InputError("contract version changed concurrently; retry with the latest version")
        else:
            state = current.get(key)
            if not state or state.get("approval_status") != "pending":
                raise InputError("contract version is no longer pending approval")
            if key[1] != max(current_versions, default=0):
                raise InputError("only the latest contract version can be approved")
            if event.get("actor", "").casefold() == state.get("recorded_by", "").casefold():
                raise InputError("approver must differ from the person who recorded the contract version")
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CONTRACT_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow({field: event.get(field, "") for field in CONTRACT_FIELDS})
    except OSError as exc:
        raise InputError(f"Cannot append contract ledger {path}: {exc}") from exc
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


def record_contract_version(
    ledger_path: Path,
    *,
    contract_id: str,
    vendor: str,
    owner: str,
    applications: Iterable[str],
    effective_date: date,
    renewal_date: date,
    notice_days: int,
    annual_commitment_inr: Decimal,
    currency: str,
    evidence_type: str,
    evidence_ref: str,
    recorded_by: str,
    note: str = "",
    recorded_on: date | None = None,
) -> int:
    values = [contract_id.strip(), vendor.strip(), owner.strip(), evidence_ref.strip(), recorded_by.strip()]
    if not all(values):
        raise InputError("contract_id, vendor, owner, evidence_ref, and recorded_by are required")
    apps = tuple(dict.fromkeys(app.strip() for app in applications if app.strip()))
    if not apps:
        raise InputError("at least one application is required")
    if renewal_date < effective_date or notice_days < 0:
        raise InputError("renewal date must not precede effective date and notice_days cannot be negative")
    if not annual_commitment_inr.is_finite() or annual_commitment_inr < 0:
        raise InputError("annual commitment must be finite and non-negative")
    if evidence_type not in EVIDENCE_TYPES:
        raise InputError(f"evidence_type must be one of {', '.join(sorted(EVIDENCE_TYPES))}")
    _, states = _load_events(ledger_path)
    version = max((number for (key, number) in states if key == _norm(contract_id)), default=0) + 1
    event = {
        "contract_id": contract_id.strip(), "version": str(version), "event": "versioned",
        "vendor": vendor.strip(), "owner": owner.strip(),
        "applications_json": json.dumps(apps, ensure_ascii=False),
        "effective_date": effective_date.isoformat(), "renewal_date": renewal_date.isoformat(),
        "notice_days": str(notice_days), "annual_commitment_inr": f"{annual_commitment_inr:.2f}",
        "currency": currency.strip().upper(), "evidence_type": evidence_type,
        "evidence_ref": evidence_ref.strip(), "actor": recorded_by.strip(),
        "occurred_on": (recorded_on or date.today()).isoformat(), "note": note.strip(),
    }
    _append_locked(ledger_path, event)
    # Reload after the append so malformed ledger state never goes unnoticed.
    _load_events(ledger_path)
    return version


def approve_contract_version(
    ledger_path: Path,
    contract_id: str,
    *,
    approved_by: str,
    note: str = "",
    approved_on: date | None = None,
    version: int | None = None,
) -> None:
    if not approved_by.strip():
        raise InputError("approved_by cannot be blank")
    _, states = _load_events(ledger_path)
    candidates = [
        (key, state) for key, state in states.items()
        if key[0] == _norm(contract_id) and (version is None or key[1] == version)
        and state.get("approval_status") == "pending"
    ]
    if len(candidates) != 1:
        raise InputError(f"Expected one pending contract version to approve; found {len(candidates)}")
    (key, state), = candidates
    latest_version = max(number for (contract_key, number) in states if contract_key == key[0])
    if key[1] != latest_version:
        raise InputError("only the latest contract version can be approved")
    if approved_by.strip().casefold() == state["recorded_by"].casefold():
        raise InputError("approver must differ from the person who recorded the contract version")
    event = {field: state.get(field, "") for field in CONTRACT_FIELDS}
    event.update({
        "event": "approved", "actor": approved_by.strip(),
        "occurred_on": (approved_on or date.today()).isoformat(), "note": note.strip(),
    })
    _append_locked(ledger_path, event)
    _load_events(ledger_path)


def reconcile_contract_invoices(
    ledger_path: Path,
    invoice_path: Path,
    *,
    as_of: date | None = None,
    tolerance_percent: Decimal = Decimal("5"),
) -> tuple[ContractInvoiceCheck, ...]:
    if not tolerance_percent.is_finite() or not Decimal("0") <= tolerance_percent <= Decimal("100"):
        raise InputError("invoice tolerance percent must be between 0 and 100")
    try:
        with invoice_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"application", "billing_period_start", "billing_period_end", "actual_billed_cost_inr"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise InputError(f"{invoice_path}: invoice CSV missing columns: {', '.join(sorted(missing))}")
            invoice_rows = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read invoice CSV {invoice_path}: {exc}") from exc
    periods: set[tuple[date, date]] = set()
    spend: dict[str, Decimal] = {}
    spend_by_contract: dict[str, Decimal] = {}
    has_contract_tags = False
    for line, row in enumerate(invoice_rows, start=2):
        try:
            start = date.fromisoformat((row.get("billing_period_start") or "")[:10])
            end = date.fromisoformat((row.get("billing_period_end") or "")[:10])
            amount = Decimal(row.get("actual_billed_cost_inr") or "")
        except (ValueError, InvalidOperation) as exc:
            raise InputError(f"{invoice_path}:{line}: invalid invoice period or cost") from exc
        app = (row.get("application") or "").strip()
        contract_id = (row.get("contract_id") or "").strip()
        if end <= start or not amount.is_finite() or amount < 0 or not app:
            raise InputError(f"{invoice_path}:{line}: invoice rows need an app, positive period, and non-negative cost")
        periods.add((start, end))
        app_key = _norm(app)
        spend[app_key] = spend.get(app_key, Decimal("0")) + amount
        if contract_id:
            has_contract_tags = True
            contract_key = _norm(contract_id)
            spend_by_contract[contract_key] = spend_by_contract.get(contract_key, Decimal("0")) + amount
    if len(periods) != 1:
        raise InputError(f"{invoice_path}: contract reconciliation requires one consistent billing period")
    period_start, period_end = next(iter(periods))
    period_days = (period_end - period_start).days
    today = as_of or date.today()
    results = []
    latest: dict[str, ContractVersion] = {}
    for contract in list_contract_versions(ledger_path):
        key = _norm(contract.contract_id)
        if key not in latest or contract.version > latest[key].version:
            latest[key] = contract
    application_owners: dict[str, str] = {}
    if not has_contract_tags:
        for contract in latest.values():
            for application in contract.applications:
                app_key = _norm(application)
                previous = application_owners.get(app_key)
                if previous and _norm(previous) != _norm(contract.contract_id):
                    raise InputError(
                        f"Application {application!r} maps to multiple contracts; add contract_id tags to the FOCUS import"
                    )
                application_owners[app_key] = contract.contract_id
    for contract in latest.values():
        if has_contract_tags:
            contract_key = _norm(contract.contract_id)
            actual = spend_by_contract.get(contract_key, Decimal("0"))
            invoice_found = contract_key in spend_by_contract
        else:
            actual = sum((spend.get(_norm(app), Decimal("0")) for app in contract.applications), Decimal("0"))
            invoice_found = any(_norm(app) in spend for app in contract.applications)
        expected = contract.annual_commitment_inr * Decimal(period_days) / Decimal("365")
        variance = actual - expected
        if contract.event != "approved":
            invoice_status = "pending_approval"
        elif not invoice_found:
            invoice_status = "invoice_missing"
        elif expected and abs(variance) * Decimal("100") / expected <= tolerance_percent:
            invoice_status = "within_tolerance"
        elif variance > 0:
            invoice_status = "invoice_over_commitment"
        else:
            invoice_status = "invoice_under_commitment"
        notice_deadline = contract.renewal_date - timedelta(days=contract.notice_days)
        renewal_status = "notice_due" if notice_deadline <= today else "upcoming" if (contract.renewal_date - today).days <= 90 else "scheduled"
        results.append(ContractInvoiceCheck(
            contract.contract_id, contract.version, contract.vendor, contract.owner,
            ";".join(contract.applications), contract.event, period_start, period_end,
            expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            actual.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            variance.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), invoice_status,
            contract.renewal_date, notice_deadline, renewal_status, contract.evidence_type, contract.evidence_ref,
        ))
    return tuple(sorted(results, key=lambda row: (row.renewal_date, row.contract_id.casefold())))


def format_contract_versions(rows: Iterable[ContractVersion]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "contract_id", "version", "approval_status", "vendor", "owner", "applications",
        "effective_date", "renewal_date", "notice_days", "annual_commitment_inr", "currency",
        "evidence_type", "evidence_ref", "recorded_by", "approved_by", "note",
    ])
    for row in rows:
        writer.writerow([
            row.contract_id, row.version, row.event, row.vendor, row.owner, ";".join(row.applications),
            row.effective_date, row.renewal_date, row.notice_days, f"{row.annual_commitment_inr:.2f}",
            row.currency, row.evidence_type, row.evidence_ref, row.recorded_by, row.approved_by, row.note,
        ])
    return output.getvalue()


def format_contract_invoice_checks(rows: Iterable[ContractInvoiceCheck]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "contract_id", "version", "vendor", "owner", "applications", "approval_status",
        "period_start", "period_end", "expected_period_cost_inr", "actual_invoice_cost_inr",
        "variance_inr", "invoice_status", "renewal_date", "notice_deadline", "renewal_status",
        "evidence_type", "evidence_ref",
    ])
    for row in rows:
        writer.writerow([
            row.contract_id, row.version, row.vendor, row.owner, row.applications, row.approval_status,
            row.period_start, row.period_end, f"{row.expected_period_cost_inr:.2f}",
            f"{row.actual_invoice_cost_inr:.2f}", f"{row.variance_inr:.2f}", row.invoice_status,
            row.renewal_date, row.notice_deadline, row.renewal_status, row.evidence_type, row.evidence_ref,
        ])
    return output.getvalue()
