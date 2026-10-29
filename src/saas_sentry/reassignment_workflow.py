"""Auditable approval and completion tracking for planned seat transfers."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .engine import InputError


FIELDS = ("finding_id", "application", "contract_id", "from_email", "to_email", "status", "actor", "on", "evidence_ref", "note")


def _states(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: reassignment ledger is missing required columns")
            events = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read reassignment ledger {path}: {exc}") from exc
    states: dict[str, dict[str, str]] = {}
    for line, event in enumerate(events, start=2):
        key, status = event.get("finding_id", ""), event.get("status", "")
        previous = states.get(key)
        if not key or status not in {"staged", "approved", "rejected", "completed"}:
            raise InputError(f"{path}:{line}: invalid reassignment event")
        if not event.get("actor", "").strip() or not event.get("evidence_ref", "").strip():
            raise InputError(f"{path}:{line}: actor and evidence_ref are required")
        try:
            date.fromisoformat(event.get("on", ""))
        except ValueError as exc:
            raise InputError(f"{path}:{line}: event date must use YYYY-MM-DD") from exc
        if status == "staged":
            if previous:
                raise InputError(f"{path}:{line}: duplicate staged finding_id")
        elif not previous or previous["status"] not in ({"staged"} if status in {"approved", "rejected"} else {"approved"}):
            raise InputError(f"{path}:{line}: invalid reassignment state transition to {status}")
        elif status in {"approved", "rejected", "completed"} and previous["actor"].casefold() == event["actor"].casefold():
            raise InputError(f"{path}:{line}: approver/executor must be independent from the previous actor")
        for field in ("application", "contract_id", "from_email", "to_email"):
            if status != "staged" and event.get(field) != previous.get(field):
                raise InputError(f"{path}:{line}: reassignment identity fields cannot change")
        states[key] = event
    return states


def _append(path: Path, event: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({key: event.get(key, "") for key in FIELDS})


def stage_plan(plan_path: Path, ledger_path: Path, *, actor: str, on: date | None = None) -> int:
    if not actor.strip():
        raise InputError("staged_by cannot be blank")
    states = _states(ledger_path)
    try:
        with plan_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"finding_id", "application", "contract_id", "from_email", "to_email", "status"}
            if not required.issubset(reader.fieldnames or []):
                raise InputError(f"{plan_path}: reassignment plan is missing required columns")
            rows = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read reassignment plan {plan_path}: {exc}") from exc
    today = (on or date.today()).isoformat()
    count = 0
    for row in rows:
        key = row.get("finding_id", "").strip()
        if row.get("status") != "proposed":
            continue
        if not key or key in states:
            raise InputError(f"{plan_path}: proposed finding is blank or already staged")
        event = {**row, "finding_id": key, "status": "staged", "actor": actor.strip(), "on": today,
                 "evidence_ref": str(plan_path), "note": "Staged from an approved-candidate reassignment plan."}
        _append(ledger_path, event)
        states[key] = event
        count += 1
    if count == 0:
        raise InputError("reassignment plan contains no unstaged proposed transfers")
    return count


def transition(path: Path, finding_id: str, status: str, *, actor: str, evidence_ref: str,
               note: str = "", on: date | None = None) -> None:
    if status not in {"approved", "rejected", "completed"}:
        raise InputError("invalid reassignment transition")
    actor, evidence_ref = actor.strip(), evidence_ref.strip()
    if not actor or not evidence_ref:
        raise InputError("actor and evidence_ref are required")
    state = _states(path).get(finding_id)
    if not state or state["status"] not in ({"staged"} if status in {"approved", "rejected"} else {"approved"}):
        raise InputError(f"finding {finding_id!r} cannot transition to {status}")
    if actor.casefold() == state["actor"].casefold():
        raise InputError("approval and completion must be performed by someone other than the previous actor")
    _append(path, {**state, "status": status, "actor": actor, "on": (on or date.today()).isoformat(),
                   "evidence_ref": evidence_ref, "note": note})


def list_states(path: Path) -> tuple[dict[str, str], ...]:
    return tuple(_states(path)[key] for key in sorted(_states(path)))
