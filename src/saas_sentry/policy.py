"""Protected identities and audited, time-limited license exceptions."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import uuid
from typing import Iterable

from .engine import InputError, normalize_email


FIELDS = (
    "exception_id", "event", "employee_id", "email", "application", "reason",
    "requested_by", "approved_by", "revoked_by", "expires_on", "evidence_ref", "occurred_on", "note",
)


@dataclass(frozen=True)
class ExceptionRecord:
    exception_id: str
    employee_id: str
    email: str
    application: str
    reason: str
    requested_by: str
    approved_by: str
    revoked_by: str
    expires_on: date
    evidence_ref: str
    status: str
    note: str


def _app(value: str) -> str:
    return " ".join(value.casefold().split())


def _load(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: exception ledger is missing required columns")
            events = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read exception ledger {path}: {exc}") from exc
    states: dict[str, dict[str, str]] = {}
    for line, event in enumerate(events, start=2):
        exception_id, kind = event.get("exception_id", "").strip(), event.get("event", "")
        try:
            date.fromisoformat(event.get("expires_on", ""))
            date.fromisoformat(event.get("occurred_on", ""))
        except ValueError as exc:
            raise InputError(f"{path}:{line}: exception dates must use YYYY-MM-DD") from exc
        if not exception_id:
            raise InputError(f"{path}:{line}: exception_id cannot be blank")
        state = states.get(exception_id)
        if kind == "requested":
            if state:
                raise InputError(f"{path}:{line}: duplicate exception request {exception_id}")
            if not (event.get("employee_id", "").strip() or event.get("email", "").strip()):
                raise InputError(f"{path}:{line}: exception requires employee_id or email")
            if not all(event.get(field, "").strip() for field in (
                "application", "reason", "requested_by", "evidence_ref",
            )):
                raise InputError(f"{path}:{line}: application, reason, requester, and evidence_ref are required")
            if date.fromisoformat(event["expires_on"]) <= date.fromisoformat(event["occurred_on"]):
                raise InputError(f"{path}:{line}: expiration must be after the request date")
            state = {**event, "status": "pending", "approved_by": ""}
            states[exception_id] = state
        elif kind == "approved":
            if not state or state.get("status") != "pending":
                raise InputError(f"{path}:{line}: approval requires a pending exception")
            if (event.get("requested_by", "") != state["requested_by"]
                    or event.get("employee_id", "") != state["employee_id"]
                    or normalize_email(event.get("email", "")) != normalize_email(state["email"])
                    or _app(event.get("application", "")) != _app(state["application"])
                    or event.get("reason", "") != state["reason"]
                    or event.get("expires_on", "") != state["expires_on"]):
                raise InputError(f"{path}:{line}: approval changes the requested exception scope")
            approver = event.get("approved_by", "").strip()
            if not approver or approver.casefold() == state["requested_by"].casefold():
                raise InputError(f"{path}:{line}: exception approver must differ from the requester")
            if date.fromisoformat(event["expires_on"]) <= date.fromisoformat(event["occurred_on"]):
                raise InputError(f"{path}:{line}: cannot approve an exception that is already expired")
            if not event.get("evidence_ref", "").strip():
                raise InputError(f"{path}:{line}: approval evidence_ref is required")
            state.update({"status": "approved", "approved_by": approver, "note": event.get("note", ""),
                          "evidence_ref": event["evidence_ref"]})
        elif kind == "revoked":
            if not state or state.get("status") != "approved":
                raise InputError(f"{path}:{line}: revoke requires an approved exception")
            for field in ("employee_id", "application", "reason", "requested_by", "approved_by", "expires_on"):
                if event.get(field, "") != state.get(field, ""):
                    raise InputError(f"{path}:{line}: revoke changes immutable exception field {field}")
            if normalize_email(event.get("email", "")) != normalize_email(state["email"]):
                raise InputError(f"{path}:{line}: revoke changes the exception email")
            if not event.get("revoked_by", "").strip():
                raise InputError(f"{path}:{line}: revoked_by cannot be blank")
            state.update({"status": "revoked", "revoked_by": event["revoked_by"], "note": event.get("note", "")})
        else:
            raise InputError(f"{path}:{line}: unsupported exception event {kind!r}")
    return states


def _append(path: Path, event: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise InputError(f"Exception ledger is locked at {lock_path}") from exc
    try:
        states = _load(path)
        state = states.get(event["exception_id"])
        kind = event["event"]
        if kind == "requested" and state:
            raise InputError("exception ID already exists")
        if kind == "approved":
            if not state or state.get("status") != "pending":
                raise InputError("exception is missing or no longer pending approval")
            if event["approved_by"].casefold() == state["requested_by"].casefold():
                raise InputError("exception approver must differ from the requester")
            if date.fromisoformat(event["expires_on"]) <= date.fromisoformat(event["occurred_on"]):
                raise InputError("cannot approve an exception that is already expired")
        if kind == "revoked" and (not state or state.get("status") != "approved"):
            raise InputError("only an approved exception can be revoked")
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow({field: event.get(field, "") for field in FIELDS})
    except OSError as exc:
        raise InputError(f"Cannot append exception ledger {path}: {exc}") from exc
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


def request_exception(
    ledger: Path, *, employee_id: str = "", email: str = "", application: str,
    reason: str, requested_by: str, expires_on: date, evidence_ref: str,
    note: str = "", on: date | None = None,
) -> str:
    today = on or date.today()
    if not (employee_id.strip() or email.strip()) or not all((application.strip(), reason.strip(), requested_by.strip(), evidence_ref.strip())):
        raise InputError("provide employee_id or email, application, reason, requested_by, and evidence_ref")
    if expires_on <= today:
        raise InputError("exception expiration must be after the request date")
    exception_id = uuid.uuid4().hex[:12]
    _append(ledger, {
        "exception_id": exception_id, "event": "requested", "employee_id": employee_id.strip(),
        "email": normalize_email(email), "application": application.strip(), "reason": reason.strip(),
        "requested_by": requested_by.strip(), "expires_on": expires_on.isoformat(),
        "evidence_ref": evidence_ref.strip(), "occurred_on": today.isoformat(), "note": note,
    })
    return exception_id


def approve_exception(
    ledger: Path, exception_id: str, *, approved_by: str, evidence_ref: str,
    note: str = "", on: date | None = None,
) -> None:
    state = _load(ledger).get(exception_id)
    if not state or state.get("status") != "pending":
        raise InputError("exception was not found in pending state")
    today = on or date.today()
    if not approved_by.strip() or approved_by.casefold() == state["requested_by"].casefold():
        raise InputError("exception approver must differ from the requester")
    if not evidence_ref.strip():
        raise InputError("approval evidence_ref is required")
    if date.fromisoformat(state["expires_on"]) <= today:
        raise InputError("cannot approve an exception that is already expired")
    _append(ledger, {**state, "event": "approved", "approved_by": approved_by.strip(),
                     "evidence_ref": evidence_ref.strip(), "occurred_on": today.isoformat(), "note": note})


def revoke_exception(
    ledger: Path, exception_id: str, *, revoked_by: str, note: str = "", on: date | None = None,
) -> None:
    state = _load(ledger).get(exception_id)
    if not state or state.get("status") != "approved":
        raise InputError("only an approved exception can be revoked")
    if not revoked_by.strip():
        raise InputError("revoked_by cannot be blank")
    _append(ledger, {**state, "event": "revoked", "revoked_by": revoked_by.strip(),
                     "occurred_on": (on or date.today()).isoformat(), "note": note})


def active_exception_keys(ledger: Path | None, *, as_of: date) -> set[tuple[str, str, str]]:
    if ledger is None:
        return set()
    keys: set[tuple[str, str, str]] = set()
    for state in _load(ledger).values():
        if state["status"] != "approved" or date.fromisoformat(state["expires_on"]) < as_of:
            continue
        app = _app(state["application"])
        keys.add((state.get("employee_id", "").strip().casefold(), normalize_email(state.get("email", "")), app))
    return keys


def list_exceptions(ledger: Path, *, as_of: date | None = None) -> tuple[ExceptionRecord, ...]:
    today = as_of or date.today()
    records = []
    for exception_id, state in _load(ledger).items():
        status = state["status"]
        if status == "approved" and date.fromisoformat(state["expires_on"]) < today:
            status = "expired"
        records.append(ExceptionRecord(
            exception_id, state["employee_id"], state["email"], state["application"],
            state["reason"], state["requested_by"], state["approved_by"], state.get("revoked_by", ""),
            date.fromisoformat(state["expires_on"]), state["evidence_ref"], status, state.get("note", ""),
        ))
    return tuple(sorted(records, key=lambda row: (row.expires_on, row.exception_id)))


def format_exceptions(rows: Iterable[ExceptionRecord]) -> str:
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["exception_id", "employee_id", "email", "application", "reason", "requested_by",
                    "approved_by", "revoked_by", "expires_on", "evidence_ref", "status", "note"])
    for row in rows:
        writer.writerow([row.exception_id, row.employee_id, row.email, row.application, row.reason,
                         row.requested_by, row.approved_by, row.revoked_by, row.expires_on.isoformat(),
                         row.evidence_ref, row.status, row.note])
    return output.getvalue()
