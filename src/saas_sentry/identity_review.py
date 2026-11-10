"""Durable human review and reusable provider-to-HR identity mapping rules."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import InputError, _rows


FIELDS = ("identity_source", "provider_user_id", "decision", "employee_id", "email",
          "reviewed_by", "reviewed_at", "note")
DECISIONS = {"map", "shared_account", "deactivate"}
MAPPING_DECISIONS = {"map", "shared_account"}


def _load_rules(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    rules: dict[tuple[str, str], dict[str, str]] = {}
    events, _ = _read_events(path)
    for row in events:
        key = (row["identity_source"].casefold(), row["provider_user_id"])
        if row["decision"] == "deactivate":
            if key not in rules:
                raise InputError(f"{path}: deactivation references an identity mapping that is not active")
            del rules[key]
        else:
            rules[key] = row
    return rules


def _read_events(path: Path) -> tuple[list[dict[str, str]], dict[tuple[str, str], dict[str, str]]]:
    if not path.exists():
        return [], {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: identity correction ledger is missing required columns")
            rows = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read identity correction ledger {path}: {exc}") from exc
    active: dict[tuple[str, str], dict[str, str]] = {}
    events: list[dict[str, str]] = []
    for line, raw in enumerate(rows, start=2):
        row = {field: raw.get(field, "") or "" for field in FIELDS}
        source = row["identity_source"].strip().casefold()
        provider_id = row["provider_user_id"].strip()
        decision = row["decision"]
        key = (source, provider_id)
        try:
            datetime.fromisoformat(row["reviewed_at"])
        except ValueError as exc:
            raise InputError(f"{path}:{line}: reviewed_at must be an ISO timestamp") from exc
        if not source or not provider_id or decision not in DECISIONS or not row["reviewed_by"].strip():
            raise InputError(f"{path}:{line}: source, provider user, decision, and reviewer are required")
        if decision == "map" and not (row["employee_id"].strip() or row["email"].strip()):
            raise InputError(f"{path}:{line}: mapped identity requires an employee ID or email")
        if decision == "deactivate":
            if key not in active:
                raise InputError(f"{path}:{line}: cannot deactivate a mapping that is not active")
            previous = active.pop(key)
            row["employee_id"], row["email"] = previous["employee_id"], previous["email"]
        else:
            active[key] = row
        events.append(row)
    return events, active


def apply_mapping_rules(
    identities: list[dict[str, str]], path: Path, *, conflicted: set[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Apply the latest reviewed rules before automated HR matching."""
    rules = _load_rules(path)
    output: list[dict[str, str]] = []
    for row in identities:
        copy = dict(row)
        key = (row.get("source", "").casefold(), row.get("provider_user_id", ""))
        rule = None if conflicted and key in conflicted else rules.get(key)
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
    rows, _ = _read_events(path)
    return list(reversed(rows[-100:]))


def list_mapping_rules(
    hr_path: Path, identities_path: Path, ledger_path: Path,
) -> list[dict[str, Any]]:
    """Describe latest saved mappings against the current HR roster and directory snapshot."""
    from .engine import _rows

    events, active = _read_events(ledger_path)
    if not events:
        return []
    latest: dict[tuple[str, str], dict[str, str]] = {}
    prior_mapping: dict[tuple[str, str], dict[str, str]] = {}
    for event in events:
        key = (event["identity_source"].casefold(), event["provider_user_id"])
        latest[key] = event
        if event["decision"] != "deactivate":
            prior_mapping[key] = event
    hr = _rows(hr_path, {"employee_id", "email"})
    identities = _rows(identities_path, {"source", "provider_user_id", "email"})
    try:
        snapshot_time = datetime.fromtimestamp(identities_path.stat().st_mtime, timezone.utc)
    except OSError as exc:
        raise InputError(f"Cannot inspect directory import {identities_path}: {exc}") from exc

    def people_for(identity: dict[str, str]) -> list[dict[str, str]]:
        identity_id = identity.get("employee_id", "").strip().casefold()
        identity_email = identity.get("email", "").strip().casefold()
        found = []
        for person in hr:
            ids = {person.get("employee_id", "").strip().casefold()}
            emails = {person.get("email", "").strip().casefold()}
            emails.update(value.strip().casefold() for value in person.get("email_aliases", "").split(","))
            if (identity_id and identity_id in ids) or (identity_email and identity_email in emails):
                found.append(person)
        return found

    results = []
    for key, latest_event in latest.items():
        mapping = prior_mapping.get(key, latest_event)
        is_active = key in active
        source_rows = [row for row in identities if row.get("source", "").casefold() == key[0]
                       and row.get("provider_user_id", "") == key[1]]
        current = source_rows[0] if len(source_rows) == 1 else {}
        reviewed_at = datetime.fromisoformat(mapping["reviewed_at"])
        if reviewed_at.tzinfo is None:
            reviewed_at = reviewed_at.replace(tzinfo=timezone.utc)
        imported_at = snapshot_time
        for candidate in source_rows:
            raw_import = candidate.get("imported_at", "").strip()
            if raw_import:
                try:
                    imported_at = datetime.fromisoformat(raw_import.replace("Z", "+00:00"))
                    if imported_at.tzinfo is None:
                        imported_at = imported_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
                break
        target = next((person for person in hr if
                       (mapping.get("employee_id", "") and person.get("employee_id", "").casefold()
                        == mapping["employee_id"].casefold())
                       or (mapping.get("email", "") and person.get("email", "").casefold()
                           == mapping["email"].casefold())), None)
        resolved = people_for(current) if current else []
        newer = imported_at > reviewed_at
        conflicts = len(source_rows) > 1 or (is_active and mapping["decision"] == "map"
            and newer and (target is None or any(
                person.get("employee_id", "").casefold() != target.get("employee_id", "").casefold()
                for person in resolved
            )))
        if len(source_rows) > 1:
            reason = "multiple current directory rows use this provider identity"
        elif target is None and is_active and mapping["decision"] == "map":
            reason = "saved HR target no longer exists"
        elif conflicts:
            reason = "newer directory import now resolves this identity to a different HR person"
        elif mapping["decision"] == "shared_account" and is_active:
            reason = "intentionally excluded from person-level identity matching"
        elif not source_rows:
            reason = "provider identity is missing from the current directory import"
        else:
            reason = "directory identity is consistent with the saved rule"
        results.append({
            **mapping, "active": is_active, "status": "deactivated" if not is_active else
            "conflict" if conflicts else "shared_account" if mapping["decision"] == "shared_account" else
            "directory_missing" if not source_rows else "active",
            "conflict": bool(conflicts), "conflict_reason": reason,
            "directory_imported_at": imported_at.isoformat(),
            "directory_email": current.get("email", ""),
            "directory_employee_id": current.get("employee_id", ""),
            "directory_display_name": current.get("display_name", ""),
            "applications": _applications(current.get("app_access_json", "[]")),
        })
    return sorted(results, key=lambda row: (row["identity_source"].casefold(), row["provider_user_id"].casefold()))


def record_deactivation(ledger_path: Path, *, identity_source: str, provider_user_id: str,
                        reviewed_by: str, note: str = "") -> dict[str, str]:
    """Append a deactivation event while preserving the mapping's complete history."""
    identity_source, provider_user_id = identity_source.strip(), provider_user_id.strip()
    if not identity_source or not provider_user_id or not reviewed_by.strip():
        raise InputError("source, provider user, and reviewer are required to deactivate a mapping")
    if len(note) > 2000:
        raise InputError("identity review note cannot exceed 2000 characters")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise InputError(f"identity correction ledger is locked at {lock_path}") from exc
    try:
        active = _load_rules(ledger_path)
        key = (identity_source.casefold(), provider_user_id)
        previous = active.get(key)
        if previous is None:
            raise InputError("identity mapping is already inactive or was not found")
        event = {"identity_source": identity_source, "provider_user_id": provider_user_id,
                 "decision": "deactivate", "employee_id": previous.get("employee_id", ""),
                 "email": previous.get("email", ""), "reviewed_by": reviewed_by.strip(),
                 "reviewed_at": datetime.now(timezone.utc).isoformat(), "note": note.strip()}
        exists = ledger_path.exists() and ledger_path.stat().st_size > 0
        with ledger_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(event)
        return event
    except OSError as exc:
        raise InputError(f"Cannot append identity deactivation {ledger_path}: {exc}") from exc
    finally:
        lock.close()
        lock_path.unlink(missing_ok=True)


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
    if decision not in MAPPING_DECISIONS or not identity_source or not provider_user_id or not reviewed_by.strip():
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
