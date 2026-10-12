"""Owner and budget approval workflow for upcoming SaaS contract renewals."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from .contracts import list_contract_versions
from .engine import InputError


FIELDS = (
    "contract_id", "version", "event", "vendor", "applications", "renewal_date", "notice_deadline", "owner",
    "decision", "decision_by", "budget_status", "budget_approver", "evidence_ref",
    "note", "occurred_on",
)
DECISIONS = {"renew", "renegotiate", "cancel"}


@dataclass(frozen=True)
class RenewalItem:
    contract_id: str
    version: int
    vendor: str
    applications: str
    renewal_date: date
    notice_deadline: date
    owner: str
    decision: str
    decision_by: str
    budget_status: str
    budget_approver: str
    evidence_ref: str
    status: str
    note: str


def _load(path: Path) -> dict[tuple[str, int], dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: renewal ledger is missing required columns")
            events = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read renewal ledger {path}: {exc}") from exc
    states: dict[tuple[str, int], dict[str, str]] = {}
    for line, event in enumerate(events, start=2):
        try:
            version = int(event.get("version", ""))
            date.fromisoformat(event.get("occurred_on", ""))
            date.fromisoformat(event.get("renewal_date", ""))
            date.fromisoformat(event.get("notice_deadline", ""))
        except ValueError as exc:
            raise InputError(f"{path}:{line}: renewal version or dates are invalid") from exc
        key = (" ".join(event.get("contract_id", "").casefold().split()), version)
        if not key[0] or version < 1 or not event.get("owner", "").strip():
            raise InputError(f"{path}:{line}: contract ID, positive version, and owner are required")
        state = states.get(key)
        kind = event.get("event", "")
        if kind == "campaign_created":
            if state:
                raise InputError(f"{path}:{line}: duplicate renewal campaign")
            state = {**event, "decision": "", "decision_by": "", "budget_status": "pending", "budget_approver": ""}
            states[key] = state
            continue
        elif kind in {"owner_decision", "budget_approval"}:
            if not state:
                raise InputError(f"{path}:{line}: renewal event references a missing campaign")
            for field in ("contract_id", "version", "vendor", "applications", "renewal_date", "notice_deadline", "owner"):
                if event.get(field, "") != state.get(field, ""):
                    raise InputError(f"{path}:{line}: renewal event changes immutable campaign field {field}")
        if kind == "owner_decision":
            if not state or state.get("decision"):
                raise InputError(f"{path}:{line}: owner decision requires a pending campaign")
            if event.get("decision") not in DECISIONS or not event.get("decision_by", "").strip() or not event.get("evidence_ref", "").strip():
                raise InputError(f"{path}:{line}: decision, decision owner, and evidence reference are required")
            if event["decision_by"].casefold() != state["owner"].casefold():
                raise InputError(f"{path}:{line}: only the assigned contract owner can record this decision")
            state.update({"decision": event["decision"], "decision_by": event["decision_by"],
                          "evidence_ref": event["evidence_ref"], "note": event.get("note", "")})
        elif kind == "budget_approval":
            if not state or not state.get("decision") or state.get("budget_status") != "pending":
                raise InputError(f"{path}:{line}: budget approval requires an owner decision awaiting approval")
            if event.get("budget_status") not in {"approved", "rejected"}:
                raise InputError(f"{path}:{line}: budget status must be approved or rejected")
            approver = event.get("budget_approver", "").strip()
            if not approver or approver.casefold() == state["decision_by"].casefold():
                raise InputError(f"{path}:{line}: budget approver must differ from the contract owner decision maker")
            if not event.get("evidence_ref", "").strip():
                raise InputError(f"{path}:{line}: budget approval requires an evidence reference")
            state.update({"budget_status": event["budget_status"], "budget_approver": approver,
                          "evidence_ref": event["evidence_ref"], "note": event.get("note", "")})
        else:
            raise InputError(f"{path}:{line}: unsupported renewal event {kind!r}")
    return states


def _append(path: Path, event: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise InputError(f"Renewal ledger is locked at {lock_path}") from exc
    try:
        states = _load(path)
        key = (" ".join(event["contract_id"].casefold().split()), int(event["version"]))
        state = states.get(key)
        kind = event["event"]
        if kind == "campaign_created" and state:
            raise InputError("renewal campaign already exists for this contract version")
        if kind == "owner_decision" and (not state or state.get("decision")):
            raise InputError("renewal campaign is missing or already has an owner decision")
        if kind == "owner_decision":
            if event.get("decision") not in DECISIONS or not event.get("evidence_ref", "").strip():
                raise InputError("a supported owner decision and evidence reference are required")
            if event.get("decision_by", "").casefold() != state["owner"].casefold():
                raise InputError("only the assigned contract owner can record this decision")
        if kind == "budget_approval" and (not state or not state.get("decision") or state.get("budget_status") != "pending"):
            raise InputError("renewal campaign is not awaiting budget approval")
        if kind == "budget_approval":
            if event.get("budget_status") not in {"approved", "rejected"}:
                raise InputError("budget status must be approved or rejected")
            if (not event.get("budget_approver", "").strip()
                    or event["budget_approver"].casefold() == state["decision_by"].casefold()):
                raise InputError("budget approver must differ from the contract owner decision maker")
            if not event.get("evidence_ref", "").strip():
                raise InputError("budget approval requires an evidence reference")
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow({field: event.get(field, "") for field in FIELDS})
    except OSError as exc:
        raise InputError(f"Cannot append renewal ledger {path}: {exc}") from exc
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


def create_renewal_campaign(
    contracts_path: Path, ledger_path: Path, *, as_of: date, horizon_days: int,
    created_by: str,
) -> int:
    if horizon_days < 0 or not created_by.strip():
        raise InputError("horizon_days must be non-negative and created_by is required")
    latest: dict[str, object] = {}
    for version in list_contract_versions(contracts_path):
        key = " ".join(version.contract_id.casefold().split())
        previous = latest.get(key)
        if previous is None or version.version > previous.version:
            latest[key] = version
    cutoff = as_of + timedelta(days=horizon_days)
    states = _load(ledger_path)
    created = 0
    for version in latest.values():
        deadline = version.renewal_date - timedelta(days=version.notice_days)
        if version.renewal_date < as_of or deadline > cutoff:
            continue
        if (" ".join(version.contract_id.casefold().split()), version.version) in states:
            continue
        _append(ledger_path, {
            "contract_id": version.contract_id, "version": str(version.version), "event": "campaign_created",
            "vendor": version.vendor, "applications": "; ".join(version.applications),
            "renewal_date": version.renewal_date.isoformat(), "notice_deadline": deadline.isoformat(),
            "owner": version.owner, "budget_status": "pending", "evidence_ref": version.evidence_ref,
            "note": f"Created by {created_by.strip()}", "occurred_on": as_of.isoformat(),
        })
        created += 1
        states = _load(ledger_path)
    return created


def record_owner_decision(
    ledger_path: Path, contract_id: str, version: int, *, decision: str,
    decided_by: str, evidence_ref: str, note: str = "", on: date | None = None,
) -> None:
    if decision not in DECISIONS or not decided_by.strip() or not evidence_ref.strip():
        raise InputError("decision must be renew, renegotiate, or cancel; decision maker and evidence_ref are required")
    state = _load(ledger_path).get((" ".join(contract_id.casefold().split()), version))
    if not state:
        raise InputError("renewal campaign was not found")
    _append(ledger_path, {**state, "event": "owner_decision", "decision": decision,
                          "decision_by": decided_by.strip(), "evidence_ref": evidence_ref.strip(),
                          "note": note, "occurred_on": (on or date.today()).isoformat()})


def record_budget_approval(
    ledger_path: Path, contract_id: str, version: int, *, status: str,
    approved_by: str, evidence_ref: str, note: str = "", on: date | None = None,
) -> None:
    if status not in {"approved", "rejected"} or not approved_by.strip() or not evidence_ref.strip():
        raise InputError("budget status must be approved or rejected; approver and evidence_ref are required")
    state = _load(ledger_path).get((" ".join(contract_id.casefold().split()), version))
    if not state:
        raise InputError("renewal campaign was not found")
    _append(ledger_path, {**state, "event": "budget_approval", "budget_status": status,
                          "budget_approver": approved_by.strip(), "evidence_ref": evidence_ref.strip(),
                          "note": note, "occurred_on": (on or date.today()).isoformat()})


def list_renewals(ledger_path: Path, *, as_of: date | None = None) -> tuple[RenewalItem, ...]:
    today = as_of or date.today()
    states = _load(ledger_path)
    result = []
    for (key, version), state in states.items():
        renewal, deadline = date.fromisoformat(state["renewal_date"]), date.fromisoformat(state["notice_deadline"])
        decision, budget = state.get("decision", ""), state.get("budget_status", "pending")
        status = ("complete" if budget == "approved" else "budget_rejected" if budget == "rejected"
                  else "pending_budget_approval" if decision
                  else "overdue_owner_decision" if today > deadline else "pending_owner_decision")
        result.append(RenewalItem(
            state["contract_id"], version, state.get("vendor", ""), state.get("applications", ""),
            renewal, deadline, state["owner"], decision, state.get("decision_by", ""), budget,
            state.get("budget_approver", ""), state.get("evidence_ref", ""), status, state.get("note", ""),
        ))
    return tuple(sorted(result, key=lambda row: (row.notice_deadline, row.contract_id.casefold(), row.version)))


def format_renewals(rows: Iterable[RenewalItem]) -> str:
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "contract_id", "version", "vendor", "applications", "renewal_date", "notice_deadline",
        "owner", "decision", "decision_by", "budget_status", "budget_approver", "evidence_ref", "status", "note",
    ])
    for row in rows:
        writer.writerow([
            row.contract_id, row.version, row.vendor, row.applications, row.renewal_date.isoformat(),
            row.notice_deadline.isoformat(), row.owner, row.decision, row.decision_by,
            row.budget_status, row.budget_approver, row.evidence_ref, row.status, row.note,
        ])
    return output.getvalue()
