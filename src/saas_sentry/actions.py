"""Append-only, approval-gated local reclaim action ledger."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import uuid


class ActionError(ValueError):
    """Raised for invalid reclaim-action input or state transitions."""


FIELDS = [
    "action_id", "finding_id", "event", "owner", "actor", "occurred_on", "note",
    "estimated_annual_savings_inr", "actual_annual_savings_inr",
]
EVENT_STATUS = {"proposed": "proposed", "approved": "approved", "rejected": "rejected", "reclaimed": "reclaimed"}
TRANSITIONS = {"proposed": {"approved", "rejected"}, "approved": {"reclaimed"}}


@dataclass(frozen=True)
class ReclaimAction:
    action_id: str
    finding_id: str
    status: str
    owner: str
    proposed_by: str
    approver: str
    proposed_on: str
    updated_on: str
    note: str
    estimated_annual_savings_inr: Decimal
    actual_annual_savings_inr: Decimal | None

    @property
    def variance_inr(self) -> Decimal | None:
        if self.actual_annual_savings_inr is None:
            return None
        return self.actual_annual_savings_inr - self.estimated_annual_savings_inr


def _money(value: str, *, path: Path, line: int) -> Decimal:
    try:
        amount = Decimal(value or "0")
    except InvalidOperation as exc:
        raise ActionError(f"{path}:{line}: savings amount must be numeric") from exc
    if not amount.is_finite() or amount < 0:
        raise ActionError(f"{path}:{line}: savings amount must be finite and non-negative")
    return amount


def _load_events(path: Path) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    if not path.exists():
        return [], {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not set(FIELDS).issubset(reader.fieldnames):
                raise ActionError(f"{path}: action ledger is missing required columns")
            events = list(reader)
    except OSError as exc:
        raise ActionError(f"Cannot read action ledger {path}: {exc}") from exc
    states: dict[str, dict[str, str]] = {}
    for line, event in enumerate(events, start=2):
        action_id, finding_id, name = event.get("action_id", ""), event.get("finding_id", ""), event.get("event", "")
        if not action_id or not finding_id or name not in EVENT_STATUS:
            raise ActionError(f"{path}:{line}: invalid action event")
        try:
            date.fromisoformat(event.get("occurred_on", ""))
        except ValueError as exc:
            raise ActionError(f"{path}:{line}: occurred_on must use YYYY-MM-DD") from exc
        if not event.get("actor", "").strip():
            raise ActionError(f"{path}:{line}: actor cannot be blank")
        estimate = _money(event.get("estimated_annual_savings_inr", ""), path=path, line=line)
        actual_raw = event.get("actual_annual_savings_inr", "")
        if actual_raw:
            _money(actual_raw, path=path, line=line)
        state = states.get(action_id)
        if name == "proposed":
            if state:
                raise ActionError(f"{path}:{line}: an action can only be proposed once")
            if actual_raw:
                raise ActionError(f"{path}:{line}: a proposal cannot record actual savings")
            state = {
                **event, "status": "proposed", "proposed_by": event["actor"],
                "approver": "", "proposed_on": event["occurred_on"],
                "estimated_annual_savings_inr": str(estimate),
                "actual_annual_savings_inr": "",
            }
            states[action_id] = state
            continue
        if not state or state["finding_id"] != finding_id:
            raise ActionError(f"{path}:{line}: transition references an unknown action or mismatched finding")
        if name not in TRANSITIONS.get(state["status"], set()):
            raise ActionError(f"{path}:{line}: cannot transition action from {state['status']} to {name}")
        if estimate != Decimal(state["estimated_annual_savings_inr"]):
            raise ActionError(f"{path}:{line}: estimated savings cannot change after proposal")
        if name == "approved" and event["actor"].casefold() == state["proposed_by"].casefold():
            raise ActionError(f"{path}:{line}: approver must be different from the proposer")
        if name == "reclaimed" and not actual_raw:
            raise ActionError(f"{path}:{line}: reclaimed event must record actual annual savings")
        if name != "reclaimed" and actual_raw:
            raise ActionError(f"{path}:{line}: actual savings are only valid on a reclaimed event")
        state.update({
            "status": name, "owner": event.get("owner") or state.get("owner", ""),
            "actor": event["actor"], "updated_on": event["occurred_on"], "note": event.get("note", ""),
            "approver": event["actor"] if name == "approved" else state.get("approver", ""),
            "actual_annual_savings_inr": actual_raw or state.get("actual_annual_savings_inr", ""),
        })
    return events, states


def list_actions(path: Path) -> tuple[ReclaimAction, ...]:
    _, states = _load_events(path)
    actions = []
    for action_id, state in states.items():
        actual = state.get("actual_annual_savings_inr", "")
        actions.append(ReclaimAction(
            action_id, state["finding_id"], state["status"], state.get("owner", ""),
            state["proposed_by"], state.get("approver", ""), state["proposed_on"],
            state.get("updated_on", state["proposed_on"]), state.get("note", ""),
            Decimal(state["estimated_annual_savings_inr"]), Decimal(actual) if actual else None,
        ))
    return tuple(sorted(actions, key=lambda action: (action.proposed_on, action.action_id)))


def _append_event(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        exists = path.exists()
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if not exists or path.stat().st_size == 0:
                writer.writeheader()
            writer.writerow({field: values.get(field, "") for field in FIELDS})
    except OSError as exc:
        raise ActionError(f"Cannot write action ledger {path}: {exc}") from exc


def propose_action(
    findings_path: Path,
    ledger_path: Path,
    finding_id: str,
    *,
    owner: str,
    proposed_by: str,
    note: str = "",
    proposed_on: date | None = None,
) -> str:
    if not owner.strip() or not proposed_by.strip():
        raise ActionError("owner and proposed_by are required")
    try:
        with findings_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"finding_id", "review_status", "contract_adjusted_annual_opportunity_inr"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ActionError(f"{findings_path}: expected findings CSV with review and opportunity columns")
            matches = [row for row in reader if row.get("finding_id") == finding_id]
    except OSError as exc:
        raise ActionError(f"Cannot read findings CSV {findings_path}: {exc}") from exc
    if len(matches) != 1:
        raise ActionError(f"Finding {finding_id!r} matched {len(matches)} rows in {findings_path}")
    finding = matches[0]
    if finding.get("review_status") != "confirmed":
        raise ActionError("Only findings with review_status=confirmed can enter the reclaim workflow")
    estimate = _money(finding.get("contract_adjusted_annual_opportunity_inr", ""), path=findings_path, line=2)
    existing = list_actions(ledger_path)
    if any(action.finding_id == finding_id and action.status != "rejected" for action in existing):
        raise ActionError("This finding already has a proposed, approved, or completed reclaim action")
    action_id = uuid.uuid4().hex[:16]
    day = (proposed_on or date.today()).isoformat()
    _append_event(ledger_path, {
        "action_id": action_id, "finding_id": finding_id, "event": "proposed",
        "owner": owner.strip(), "actor": proposed_by.strip(), "occurred_on": day,
        "note": note, "estimated_annual_savings_inr": str(estimate),
    })
    return action_id


def transition_action(
    ledger_path: Path,
    action_id: str,
    event: str,
    *,
    actor: str,
    note: str = "",
    actual_annual_savings_inr: Decimal | None = None,
    occurred_on: date | None = None,
) -> None:
    if event not in {"approved", "rejected", "reclaimed"}:
        raise ActionError("event must be approved, rejected, or reclaimed")
    if not actor.strip():
        raise ActionError("actor is required")
    actions = {action.action_id: action for action in list_actions(ledger_path)}
    current = actions.get(action_id)
    if not current:
        raise ActionError(f"Unknown action_id {action_id!r}")
    if event not in TRANSITIONS.get(current.status, set()):
        raise ActionError(f"Cannot transition action from {current.status} to {event}")
    if event == "approved" and actor.strip().casefold() == current.proposed_by.casefold():
        raise ActionError("Approver must be different from the proposer")
    if event == "reclaimed" and actual_annual_savings_inr is None:
        raise ActionError("Record actual annual savings when marking a license reclaimed")
    if actual_annual_savings_inr is not None and (
        not actual_annual_savings_inr.is_finite() or actual_annual_savings_inr < 0
    ):
        raise ActionError("actual annual savings must be finite and non-negative")
    if event != "reclaimed" and actual_annual_savings_inr is not None:
        raise ActionError("actual annual savings can only be provided for a reclaimed action")
    _append_event(ledger_path, {
        "action_id": current.action_id, "finding_id": current.finding_id, "event": event,
        "owner": current.owner, "actor": actor.strip(),
        "occurred_on": (occurred_on or date.today()).isoformat(), "note": note,
        "estimated_annual_savings_inr": str(current.estimated_annual_savings_inr),
        "actual_annual_savings_inr": str(actual_annual_savings_inr) if actual_annual_savings_inr is not None else "",
    })
