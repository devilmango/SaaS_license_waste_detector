"""Approval-gated, idempotent provider reclaim execution and audit logging."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import uuid
from pathlib import Path
from typing import Callable

from .actions import ActionError, list_actions


EXECUTION_FIELDS = [
    "idempotency_key", "action_id", "provider", "status", "actor", "occurred_at",
    "email", "application", "provider_user_id", "provider_license_id", "result",
]


@dataclass(frozen=True)
class ExecutionPlan:
    action_id: str
    finding_id: str
    provider: str
    email: str
    application: str
    provider_user_id: str
    provider_license_id: str
    idempotency_key: str


def _read_csv(path: Path, required: set[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = {header.strip() for header in (reader.fieldnames or []) if header}
            missing = required - headers
            if missing:
                raise ActionError(f"{path}: missing required columns: {', '.join(sorted(missing))}")
            return [
                {(key or "").strip(): (value or "").strip() for key, value in row.items() if key}
                for row in reader
            ]
    except OSError as exc:
        raise ActionError(f"Cannot read execution input {path}: {exc}") from exc


def _identity(value: str) -> str:
    return " ".join(value.casefold().split())


def _uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ActionError(f"Provider target {field} must be a UUID; re-import the provider data") from exc


def _idempotency_key(action_id: str, provider: str) -> str:
    raw_key = "\0".join((action_id, provider, "remove-license"))
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def build_execution_plan(
    action_ledger: Path,
    findings_path: Path,
    billing_path: Path,
    action_id: str,
    *,
    provider: str = "microsoft365",
) -> ExecutionPlan:
    """Resolve an approved action to exactly one imported M365 user/SKU target."""
    if provider != "microsoft365":
        raise ActionError("Only the microsoft365 reclaim adapter is currently supported")
    action = next((item for item in list_actions(action_ledger) if item.action_id == action_id), None)
    if not action:
        raise ActionError(f"Unknown action_id {action_id!r}")
    if action.status != "approved":
        raise ActionError("Only approved reclaim actions can be sent to a provider")
    finding_rows = _read_csv(findings_path, {"finding_id", "email", "application", "review_status"})
    findings = [row for row in finding_rows if row.get("finding_id") == action.finding_id]
    if len(findings) != 1:
        raise ActionError(f"Action finding matched {len(findings)} rows in {findings_path}")
    finding = findings[0]
    if finding.get("review_status") != "confirmed":
        raise ActionError("Finding is no longer confirmed; provider execution is blocked")
    email, application = finding.get("email", ""), finding.get("application", "")
    if not email or not application:
        raise ActionError("Finding must include an email and application to resolve a provider target")
    billing_rows = _read_csv(billing_path, {
        "email", "application", "license_status", "provider", "provider_user_id", "provider_license_id",
    })
    matches = [row for row in billing_rows if (
        _identity(row.get("email", "")) == _identity(email)
        and _identity(row.get("application", "")) == _identity(application)
        and row.get("license_status", "").casefold() == "active"
        and row.get("provider", "").casefold() == provider
    )]
    if len(matches) != 1:
        raise ActionError(
            f"Finding resolved to {len(matches)} active {provider} license targets; exactly one is required"
        )
    target = matches[0]
    user_id = _uuid(target.get("provider_user_id", ""), "provider_user_id")
    license_id = _uuid(target.get("provider_license_id", ""), "provider_license_id")
    return ExecutionPlan(
        action.action_id, action.finding_id, provider, email, application, user_id, license_id,
        _idempotency_key(action.action_id, provider),
    )


def _execution_states(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not set(EXECUTION_FIELDS).issubset(reader.fieldnames):
                raise ActionError(f"{path}: execution ledger is missing required columns")
            rows = list(reader)
    except OSError as exc:
        raise ActionError(f"Cannot read execution ledger {path}: {exc}") from exc
    states: dict[str, dict[str, str]] = {}
    for line, row in enumerate(rows, start=2):
        key, status = row.get("idempotency_key", ""), row.get("status", "")
        previous = states.get(key)
        valid_statuses = {
            "started", "succeeded", "failed", "reconciled_completed", "reconciled_not_applied",
        }
        if not key or status not in valid_statuses:
            raise ActionError(f"{path}:{line}: invalid provider execution event")
        if status == "started":
            if previous and previous.get("status") != "reconciled_not_applied":
                raise ActionError(f"{path}:{line}: duplicate idempotency key in execution ledger")
        elif status in {"succeeded", "failed"} and (not previous or previous.get("status") != "started"):
            raise ActionError(f"{path}:{line}: provider execution result has no started event")
        elif status.startswith("reconciled_") and (
            not previous or previous.get("status") not in {"started", "failed"}
        ):
            raise ActionError(f"{path}:{line}: reconciliation requires an ambiguous started/failed event")
        if previous and previous.get("action_id") != row.get("action_id"):
            raise ActionError(f"{path}:{line}: idempotency key is associated with multiple actions")
        states[key] = row
    return states


def ensure_plan_available(plan: ExecutionPlan, ledger_path: Path) -> None:
    """Fail if this approval already has a started or completed provider operation."""
    state = _execution_states(ledger_path).get(plan.idempotency_key)
    if state and state.get("status") != "reconciled_not_applied":
        raise ActionError(
            "This idempotent provider operation already has an execution record; "
            "reconcile provider state before taking further action"
        )


def _append(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXECUTION_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow({field: values.get(field, "") for field in EXECUTION_FIELDS})
    except OSError as exc:
        raise ActionError(f"Cannot write provider execution ledger {path}: {exc}") from exc


def execute_plan(
    plan: ExecutionPlan,
    ledger_path: Path,
    *,
    actor: str,
    operation: Callable[[ExecutionPlan], str],
) -> str:
    """Run one provider operation at most once and durably record its outcome."""
    if not actor.strip():
        raise ActionError("executed_by is required")
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = lock_path.open("x", encoding="utf-8")
        lock.write(f"{plan.idempotency_key}\n")
        lock.flush()
    except FileExistsError as exc:
        raise ActionError(f"Execution ledger is locked at {lock_path}; inspect and clear a stale lock manually") from exc
    except OSError as exc:
        raise ActionError(f"Cannot lock provider execution ledger {ledger_path}: {exc}") from exc

    def event(status: str, result: str = "") -> None:
        _append(ledger_path, {
            "idempotency_key": plan.idempotency_key, "action_id": plan.action_id,
            "provider": plan.provider, "status": status, "actor": actor.strip(),
            "occurred_at": datetime.now(timezone.utc).isoformat(), "email": plan.email,
            "application": plan.application, "provider_user_id": plan.provider_user_id,
            "provider_license_id": plan.provider_license_id, "result": result,
        })

    try:
        ensure_plan_available(plan, ledger_path)
        event("started", "Provider request started; retries are blocked until reconciled.")
        try:
            result = operation(plan)
        except Exception as exc:
            # Record an ambiguous failure before returning; credentials and request bodies are never logged.
            message = str(exc).replace("\n", " ")[:300]
            event("failed", message or type(exc).__name__)
            raise ActionError(f"Provider operation failed and was recorded: {message or type(exc).__name__}") from exc
        event("succeeded", result[:300])
        return result
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


def resolve_execution(
    action_ledger: Path,
    execution_ledger: Path,
    action_id: str,
    *,
    provider: str = "microsoft365",
    resolved_as: str,
    actor: str,
    note: str,
) -> None:
    """Record an independent human reconciliation of an ambiguous provider result."""
    if provider != "microsoft365":
        raise ActionError("Only the microsoft365 reclaim adapter is currently supported")
    if resolved_as not in {"completed", "not_applied"}:
        raise ActionError("resolved_as must be completed or not_applied")
    if not actor.strip() or not note.strip():
        raise ActionError("resolved_by and a reconciliation note are required")
    action = next((item for item in list_actions(action_ledger) if item.action_id == action_id), None)
    if not action or action.status != "approved":
        raise ActionError("Only an existing approved action can have its provider outcome reconciled")
    key = _idempotency_key(action_id, provider)
    lock_path = execution_ledger.with_name(execution_ledger.name + ".lock")
    execution_ledger.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = lock_path.open("x", encoding="utf-8")
        lock.write(f"{key}\n")
        lock.flush()
    except FileExistsError as exc:
        raise ActionError(f"Execution ledger is locked at {lock_path}; inspect and clear a stale lock manually") from exc
    except OSError as exc:
        raise ActionError(f"Cannot lock provider execution ledger {execution_ledger}: {exc}") from exc
    try:
        previous = _execution_states(execution_ledger).get(key)
        if not previous or previous.get("status") not in {"started", "failed"}:
            raise ActionError("No ambiguous provider execution is available for reconciliation")
        if previous.get("action_id") != action_id:
            raise ActionError("Execution ledger action does not match the requested action")
        if previous.get("actor", "").casefold() == actor.strip().casefold():
            raise ActionError("Execution reconciliation must be recorded by a different person")
        status = "reconciled_completed" if resolved_as == "completed" else "reconciled_not_applied"
        _append(execution_ledger, {
            **previous, "status": status, "actor": actor.strip(),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "result": f"{note.strip()} (manually reconciled as {resolved_as})"[:300],
        })
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass
