"""Durable human review and reusable provider-to-HR identity mapping rules."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import InputError, _rows


FIELDS = ("identity_source", "provider_user_id", "decision", "employee_id", "email",
          "reviewed_by", "reviewed_at", "note")
DECISIONS = {"map", "shared_account"}


def _load_rules(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: identity correction ledger is missing required columns")
            rows = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read identity correction ledger {path}: {exc}") from exc
    rules: dict[tuple[str, str], dict[str, str]] = {}
    for line, row in enumerate(rows, start=2):
        source, provider_id = row.get("identity_source", "").strip().casefold(), row.get("provider_user_id", "").strip()
        decision = row.get("decision", "")
        try:
            datetime.fromisoformat(row.get("reviewed_at", ""))
        except ValueError as exc:
            raise InputError(f"{path}:{line}: reviewed_at must be an ISO timestamp") from exc
        if not source or not provider_id or decision not in DECISIONS or not row.get("reviewed_by", "").strip():
            raise InputError(f"{path}:{line}: source, provider user, decision, and reviewer are required")
        if decision == "map" and not (row.get("employee_id", "").strip() or row.get("email", "").strip()):
            raise InputError(f"{path}:{line}: mapped identity requires an employee ID or email")
        rules[(source, provider_id)] = row
    return rules


def apply_mapping_rules(identities: list[dict[str, str]], path: Path) -> list[dict[str, str]]:
    """Apply the latest reviewed rules before automated HR matching."""
    rules = _load_rules(path)
    output: list[dict[str, str]] = []
    for row in identities:
        copy = dict(row)
        rule = rules.get((row.get("source", "").casefold(), row.get("provider_user_id", "")))
        if rule:
            if rule["decision"] == "shared_account":
                copy["_shared_account"] = "true"
            else:
                if rule.get("employee_id", "").strip():
                    copy["employee_id"] = rule["employee_id"].strip()
                if rule.get("email", "").strip():
                    copy["email"] = rule["email"].strip().casefold()
        output.append(copy)
    return output


def review_queue(hr_path: Path, identities_path: Path, corrections_path: Path) -> list[dict[str, Any]]:
    """Return unresolved reconciliation rows with candidate provider identities."""
    from .identity import reconcile_identities

    hr = _rows(hr_path, {"employee_id", "email", "status"})
    identities = _rows(identities_path, {
        "employee_id", "email", "display_name", "source", "provider_user_id", "active",
        "app_access_json", "provisioning_state",
    })
    matches = reconcile_identities(hr_path, identities_path, mapping_rules_path=corrections_path)
    queue = []
    open_statuses = {"identity_conflict", "identity_without_hr"}
    for item in matches:
        if item.match_status not in open_statuses:
            continue
        email, employee_id = item.email.casefold(), item.employee_id.casefold()
        person = next((row for row in hr if row.get("employee_id", "").casefold() == employee_id
                       or row.get("email", "").casefold() == email), {})
        aliases = {value.strip().casefold() for value in person.get("email_aliases", "").split(",")
                   if value.strip()}
        emails = aliases | ({email} if email else set())
        candidates = [row for row in identities if (
            row.get("email", "").casefold() in emails
            or (employee_id and row.get("employee_id", "").casefold() == employee_id)
        )]
        queue.append({
            "employee": item.employee, "employee_id": item.employee_id, "email": item.email,
            "hr_status": item.hr_status, "status": item.match_status,
            "candidates": [{"identity_source": row.get("source", ""),
                            "provider_user_id": row.get("provider_user_id", ""),
                            "email": row.get("email", ""), "display_name": row.get("display_name", ""),
                            "applications": _applications(row.get("app_access_json", "[]"))}
                           for row in candidates],
        })
    return queue


def list_corrections(path: Path) -> list[dict[str, str]]:
    """Return the append-only review history for dashboard audit visibility."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: identity correction ledger is missing required columns")
            rows = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read identity correction ledger {path}: {exc}") from exc
    return list(reversed(rows[-100:]))


def _applications(raw: str) -> list[str]:
    import json
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in values if isinstance(item, str)] if isinstance(values, list) else []


def record_correction(
    hr_path: Path, identities_path: Path, ledger_path: Path, *, identity_source: str,
    provider_user_id: str, decision: str, employee_id: str = "", email: str = "",
    reviewed_by: str, note: str = "",
) -> dict[str, str]:
    """Append a correction after validating its source record and canonical HR target."""
    identity_source, provider_user_id = identity_source.strip(), provider_user_id.strip()
    employee_id, email = employee_id.strip(), email.strip().casefold()
    if decision not in DECISIONS or not identity_source or not provider_user_id or not reviewed_by.strip():
        raise InputError("source, provider user, supported decision, and reviewer are required")
    if len(note) > 2000:
        raise InputError("identity review note cannot exceed 2000 characters")
    identities = _rows(identities_path, {"source", "provider_user_id", "email"})
    source_matches = [row for row in identities if row.get("source", "").casefold() == identity_source.casefold()
                      and row.get("provider_user_id", "") == provider_user_id]
    if len(source_matches) != 1:
        raise InputError(f"provider identity matched {len(source_matches)} directory records; exactly one is required")
    if decision == "map":
        people = _rows(hr_path, {"employee_id", "email"})
        targets = [person for person in people if
                   (employee_id and person.get("employee_id", "").casefold() == employee_id.casefold())
                   or (email and person.get("email", "").casefold() == email)]
        if len(targets) != 1:
            raise InputError("identity correction must resolve to exactly one HR employee")
        target = targets[0]
        employee_id = target.get("employee_id", "").strip()
        email = target.get("email", "").strip().casefold()
    else:
        employee_id = email = ""
    event = {"identity_source": identity_source, "provider_user_id": provider_user_id,
             "decision": decision, "employee_id": employee_id, "email": email,
             "reviewed_by": reviewed_by.strip(), "reviewed_at": datetime.now(timezone.utc).isoformat(),
             "note": note.strip()}
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise InputError(f"identity correction ledger is locked at {lock_path}") from exc
    try:
        rules = _load_rules(ledger_path)
        key = (identity_source.casefold(), provider_user_id)
        if key in rules and rules[key].get("decision") == decision and all(
            rules[key].get(field, "") == event[field] for field in ("employee_id", "email")
        ):
            return rules[key]
        exists = ledger_path.exists() and ledger_path.stat().st_size > 0
        with ledger_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(event)
    except OSError as exc:
        raise InputError(f"Cannot append identity correction ledger {ledger_path}: {exc}") from exc
    finally:
        lock.close()
        lock_path.unlink(missing_ok=True)
    return event
