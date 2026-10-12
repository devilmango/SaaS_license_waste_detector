"""Owner-assigned access review campaigns backed by an append-only event ledger."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import uuid
from typing import Iterable

from .engine import InputError, _rows, update_review


EVENT_FIELDS = [
    "campaign_id", "finding_id", "application", "owner", "due_on", "event",
    "actor", "occurred_on", "decision", "note",
]
DECISIONS = {"confirmed", "dismissed", "deferred"}


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


@dataclass(frozen=True)
class CampaignItem:
    campaign_id: str
    finding_id: str
    application: str
    owner: str
    due_on: date
    status: str
    decision: str
    reminder_count: int
    last_reminder_on: date | None


def _load_events(path: Path) -> tuple[list[dict[str, str]], dict[tuple[str, str], dict[str, str]]]:
    if not path.exists():
        return [], {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not set(EVENT_FIELDS).issubset(reader.fieldnames):
                raise InputError(f"{path}: access-review ledger is missing required columns")
            events = list(reader)
    except OSError as exc:
        raise InputError(f"Cannot read access-review ledger {path}: {exc}") from exc
    states: dict[tuple[str, str], dict[str, str]] = {}
    for line, event in enumerate(events, start=2):
        campaign_id, finding_id = event.get("campaign_id", ""), event.get("finding_id", "")
        kind = event.get("event", "")
        if not campaign_id or not finding_id or kind not in {"assigned", "reminder", "decided"}:
            raise InputError(f"{path}:{line}: invalid access-review event")
        try:
            occurred_on = date.fromisoformat(event.get("occurred_on", ""))
            due_on = date.fromisoformat(event.get("due_on", ""))
        except ValueError as exc:
            raise InputError(f"{path}:{line}: event and due dates must use YYYY-MM-DD") from exc
        if not event.get("actor", "").strip():
            raise InputError(f"{path}:{line}: actor cannot be blank")
        key = (campaign_id, finding_id)
        state = states.get(key)
        if kind == "assigned":
            if state:
                raise InputError(f"{path}:{line}: finding is assigned more than once in a campaign")
            if not event.get("owner", "").strip() or not event.get("application", "").strip():
                raise InputError(f"{path}:{line}: assignment requires owner and application")
            state = {**event, "status": "pending", "reminder_count": "0", "last_reminder_on": ""}
            states[key] = state
            continue
        if not state:
            raise InputError(f"{path}:{line}: event references an unknown campaign assignment")
        if state["due_on"] != due_on.isoformat() or state["owner"].casefold() != event.get("owner", "").casefold():
            raise InputError(f"{path}:{line}: owner and due date must match the campaign assignment")
        if kind == "reminder":
            if state["status"] != "pending":
                raise InputError(f"{path}:{line}: completed campaign items cannot be reminded")
            state["reminder_count"] = str(int(state["reminder_count"]) + 1)
            state["last_reminder_on"] = occurred_on.isoformat()
        else:
            decision = event.get("decision", "")
            if state["status"] != "pending" or decision not in DECISIONS:
                raise InputError(f"{path}:{line}: invalid or duplicate campaign decision")
            if event.get("actor", "").strip().casefold() != state["owner"].casefold():
                raise InputError(f"{path}:{line}: only the assigned owner can decide this item")
            state.update({"status": "decided", "decision": decision, "decided_on": occurred_on.isoformat()})
    return events, states


def _append(path: Path, event: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        exists = path.exists()
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EVENT_FIELDS)
            if not exists or path.stat().st_size == 0:
                writer.writeheader()
            writer.writerow({field: event.get(field, "") for field in EVENT_FIELDS})
    except OSError as exc:
        raise InputError(f"Cannot write access-review ledger {path}: {exc}") from exc


def create_campaign(
    findings_path: Path,
    ledger_path: Path,
    *,
    due_on: date,
    created_by: str,
    default_owner: str = "",
    owner_map_path: Path | None = None,
    campaign_id: str | None = None,
    created_on: date | None = None,
) -> tuple[str, int]:
    """Assign unreviewed findings to their review owner and append assignment events."""
    if not created_by.strip():
        raise InputError("created_by cannot be blank")
    _, states = _load_events(ledger_path)
    try:
        rows = _rows(findings_path, {"finding_id", "application"})
    except InputError:
        raise
    owners_by_app: dict[str, str] = {}
    if owner_map_path:
        owner_rows = _rows(owner_map_path, {"application", "owner"})
        for line, row in enumerate(owner_rows, start=2):
            app_key, owner = _norm(row.get("application", "")), row.get("owner", "").strip()
            if not app_key or not owner or app_key in owners_by_app:
                raise InputError(f"{owner_map_path}:{line}: application and unique owner are required")
            owners_by_app[app_key] = owner
    seen: set[str] = set()
    candidates: list[dict[str, str]] = []
    for line, row in enumerate(rows, start=2):
        finding_id = row.get("finding_id", "").strip()
        if not finding_id or finding_id in seen:
            raise InputError(f"{findings_path}:{line}: finding_id is blank or duplicated")
        seen.add(finding_id)
        if row.get("review_status", "unreviewed").strip().casefold() not in {"", "unreviewed"}:
            continue
        if any(existing_id == finding_id and state.get("status") == "pending"
               for (_, existing_id), state in states.items()):
            continue
        owner = (
            owners_by_app.get(_norm(row.get("application", "")), "")
            or row.get("review_owner", "").strip()
            or default_owner.strip()
        )
        if not owner:
            raise InputError(f"{findings_path}:{line}: set review_owner or pass --default-owner")
        if not row.get("application", "").strip():
            raise InputError(f"{findings_path}:{line}: application cannot be blank")
        candidates.append({**row, "finding_id": finding_id, "owner": owner})
    if not candidates:
        raise InputError("No unreviewed findings are available to assign")
    campaign_id = (campaign_id or f"review-{(created_on or date.today()).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}").strip()
    if not campaign_id:
        raise InputError("campaign_id cannot be blank")
    if any(existing_campaign == campaign_id for existing_campaign, _ in states):
        raise InputError(f"Campaign {campaign_id!r} already exists")
    today = (created_on or date.today()).isoformat()
    for row in candidates:
        _append(ledger_path, {
            "campaign_id": campaign_id, "finding_id": row["finding_id"],
            "application": row["application"], "owner": row["owner"],
            "due_on": due_on.isoformat(), "event": "assigned", "actor": created_by.strip(),
            "occurred_on": today,
        })
    return campaign_id, len(candidates)


def list_campaign_items(path: Path, *, campaign_id: str = "", as_of: date | None = None) -> tuple[CampaignItem, ...]:
    _, states = _load_events(path)
    today = as_of or date.today()
    result = []
    for (campaign, finding), state in sorted(states.items()):
        if campaign_id and campaign != campaign_id:
            continue
        status = state["status"]
        due = date.fromisoformat(state["due_on"])
        if status == "pending" and due < today:
            status = "overdue"
        last_raw = state.get("last_reminder_on", "")
        result.append(CampaignItem(
            campaign, finding, state["application"], state["owner"], due, status,
            state.get("decision", ""), int(state["reminder_count"]),
            date.fromisoformat(last_raw) if last_raw else None,
        ))
    return tuple(sorted(result, key=lambda row: (row.due_on, row.owner.casefold(), row.finding_id)))


def generate_reminders(
    ledger_path: Path,
    output_path: Path,
    *,
    as_of: date | None = None,
    within_days: int = 7,
    minimum_gap_days: int = 7,
) -> tuple[CampaignItem, ...]:
    """Write a local reminder queue and record each reminder generation in the event ledger."""
    if within_days < 0 or minimum_gap_days < 0:
        raise InputError("reminder windows cannot be negative")
    if output_path.resolve() == ledger_path.resolve():
        raise InputError("reminder output cannot overwrite the campaign event ledger")
    today = as_of or date.today()
    pending = list_campaign_items(ledger_path, as_of=today)
    eligible = []
    for item in pending:
        if item.status not in {"pending", "overdue"} or (item.due_on - today).days > within_days:
            continue
        if item.last_reminder_on and (today - item.last_reminder_on).days < minimum_gap_days:
            continue
        eligible.append(item)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "campaign_id", "finding_id", "application", "owner", "due_on",
            "days_until_due", "reminder_count", "reminder_reason",
        ])
        for item in eligible:
            days = (item.due_on - today).days
            writer.writerow([
                item.campaign_id, item.finding_id, item.application, item.owner,
                item.due_on.isoformat(), days, item.reminder_count + 1,
                "overdue" if days < 0 else "due_soon",
            ])
    for item in eligible:
        _append(ledger_path, {
            "campaign_id": item.campaign_id, "finding_id": item.finding_id,
            "application": item.application, "owner": item.owner,
            "due_on": item.due_on.isoformat(), "event": "reminder",
            "actor": "saas-sentry", "occurred_on": today.isoformat(),
            "note": "Reminder queue generated locally; delivery is operator-managed.",
        })
    return tuple(eligible)


def decide_campaign_item(
    ledger_path: Path,
    reviews_path: Path,
    campaign_id: str,
    finding_id: str,
    decision: str,
    *,
    reviewed_by: str,
    note: str = "",
    decided_on: date | None = None,
) -> None:
    if decision not in DECISIONS:
        raise InputError(f"decision must be one of {', '.join(sorted(DECISIONS))}")
    _, states = _load_events(ledger_path)
    state = states.get((campaign_id, finding_id))
    if not state:
        raise InputError(f"Unknown finding {finding_id!r} in campaign {campaign_id!r}")
    if state["status"] != "pending":
        raise InputError("Only pending campaign items can be decided")
    if reviewed_by.strip().casefold() != state["owner"].casefold():
        raise InputError("decision must be recorded by the assigned owner")
    day = decided_on or date.today()
    update_review(
        reviews_path, finding_id, decision, owner=state["owner"], note=note, reviewed_on=day,
    )
    _append(ledger_path, {
        "campaign_id": campaign_id, "finding_id": finding_id,
        "application": state["application"], "owner": state["owner"],
        "due_on": state["due_on"], "event": "decided", "actor": reviewed_by.strip(),
        "occurred_on": day.isoformat(), "decision": decision, "note": note,
    })


def format_campaign_items(items: Iterable[CampaignItem]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "campaign_id", "finding_id", "application", "owner", "due_on", "status",
        "decision", "reminder_count", "last_reminder_on",
    ])
    for item in items:
        writer.writerow([
            item.campaign_id, item.finding_id, item.application, item.owner,
            item.due_on.isoformat(), item.status, item.decision, item.reminder_count,
            item.last_reminder_on.isoformat() if item.last_reminder_on else "",
        ])
    return output.getvalue()
