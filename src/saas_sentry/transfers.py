"""Approval-bound, idempotent provider seat transfers and snapshot verification."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Callable

from .engine import InputError
from .execution import _identity
from .reassignment_workflow import list_states


TRANSFER_FIELDS = (
    "idempotency_key", "finding_id", "provider", "application", "contract_id",
    "source_email", "target_email", "source_user_id", "target_user_id", "license_id",
    "status", "actor", "occurred_at", "result",
)
SUPPORTED_TRANSFER_PROVIDERS = {"microsoft365", "google_workspace", "github_copilot", "slack", "zoom"}


@dataclass(frozen=True)
class TransferPlan:
    idempotency_key: str
    finding_id: str
    provider: str
    application: str
    contract_id: str
    source_email: str
    target_email: str
    source_user_id: str
    license_id: str


@dataclass(frozen=True)
class TransferVerification:
    finding_id: str
    provider: str
    source_email: str
    target_email: str
    source_user_id: str
    target_user_id: str
    status: str
    imported_at: str
    follow_up: str


def _read_rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise InputError(f"{path}: missing required columns: {', '.join(sorted(missing))}")
            return [{(key or "").strip(): (value or "").strip() for key, value in row.items() if key}
                    for row in reader]
    except OSError as exc:
        raise InputError(f"Cannot read {path}: {exc}") from exc


def build_transfer_plan(
    reassignment_ledger: Path, billing_path: Path, finding_id: str, *, provider: str,
) -> TransferPlan:
    if provider not in SUPPORTED_TRANSFER_PROVIDERS:
        raise InputError(f"Unsupported seat-transfer provider {provider!r}")
    state = next((item for item in list_states(reassignment_ledger) if item["finding_id"] == finding_id), None)
    if not state:
        raise InputError(f"Unknown reassignment finding {finding_id!r}")
    if state["status"] != "approved":
        raise InputError("Only approved seat transfers can be executed")
    billing = _read_rows(billing_path, {
        "email", "application", "license_status", "contract_id", "provider",
        "provider_user_id", "provider_license_id",
    })
    matches = [row for row in billing if (
        _identity(row["email"]) == _identity(state["from_email"])
        and _identity(row["application"]) == _identity(state["application"])
        and _identity(row["contract_id"]) == _identity(state["contract_id"])
        and row["license_status"].casefold() == "active"
        and row["provider"].casefold() == provider
    )]
    if len(matches) != 1:
        raise InputError(f"Expected exactly one current {provider} source assignment; found {len(matches)}")
    source = matches[0]
    if not source["provider_user_id"] or not source["provider_license_id"]:
        raise InputError("Provider source assignment is missing user or license identifiers; re-import provider data")
    material = "\0".join((finding_id, provider, source["provider_user_id"], state["to_email"], source["provider_license_id"]))
    key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return TransferPlan(
        key, finding_id, provider, state["application"], state["contract_id"], state["from_email"],
        state["to_email"], source["provider_user_id"], source["provider_license_id"],
    )


def _load_execution_states(path: Path) -> dict[str, dict[str, str]]:
    rows = _read_rows(path, set(TRANSFER_FIELDS)) if path.exists() else []
    states: dict[str, dict[str, str]] = {}
    for line, row in enumerate(rows, start=2):
        key, status = row["idempotency_key"], row["status"]
        previous = states.get(key)
        if not key or status not in {"started", "succeeded", "failed", "verified_transferred", "partial", "not_applied", "unverified"}:
            raise InputError(f"{path}:{line}: invalid transfer event")
        if status == "started":
            if previous:
                raise InputError(f"{path}:{line}: transfer idempotency key has already been used")
        elif status in {"succeeded", "failed"} and (not previous or previous["status"] != "started"):
            raise InputError(f"{path}:{line}: transfer result has no started event")
        elif status in {"verified_transferred", "partial", "not_applied", "unverified"} and (
            not previous or previous["status"] not in {"started", "succeeded", "failed"}
        ):
            raise InputError(f"{path}:{line}: transfer verification has no provider result")
        if previous and any(previous[field] != row[field] for field in (
            "finding_id", "provider", "source_email", "target_email", "source_user_id", "license_id",
        )):
            raise InputError(f"{path}:{line}: transfer identity cannot change within an idempotent operation")
        states[key] = row
    return states


def _append(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    has_header = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRANSFER_FIELDS)
        if not has_header:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in TRANSFER_FIELDS})


def _lock(path: Path, key: str):
    lock_path = path.with_name(path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock_path.open("x", encoding="utf-8")
        handle.write(key)
        handle.flush()
        return handle, lock_path
    except FileExistsError as exc:
        raise InputError(f"Transfer ledger is locked at {lock_path}") from exc
    except OSError as exc:
        raise InputError(f"Cannot lock transfer ledger {path}: {exc}") from exc


def execute_transfer(
    plan: TransferPlan, ledger_path: Path, *, actor: str,
    operation: Callable[[TransferPlan], str],
) -> str:
    actor = actor.strip()
    if not actor:
        raise InputError("executed_by cannot be blank")
    lock, lock_path = _lock(ledger_path, plan.idempotency_key)
    def event(status: str, result: str) -> None:
        _append(ledger_path, {
            **plan.__dict__, "target_user_id": "", "status": status, "actor": actor,
            "occurred_at": datetime.now(timezone.utc).isoformat(), "result": result[:300],
        })
    try:
        states = _load_execution_states(ledger_path)
        previous = states.get(plan.idempotency_key)
        if previous:
            raise InputError(f"Transfer already has a {previous['status']} record; verify provider state before any further action")
        event("started", "Provider transfer started; automatic retries are blocked.")
        try:
            result = operation(plan)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:240]}"
            event("failed", reason)
            raise InputError(f"Provider transfer outcome is recorded as ambiguous: {reason}") from exc
        event("succeeded", str(result))
        return str(result)
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


def verify_transfer_snapshot(
    ledger_path: Path, billing_path: Path, *, provider: str,
) -> tuple[TransferVerification, ...]:
    if provider not in SUPPORTED_TRANSFER_PROVIDERS:
        raise InputError(f"Unsupported seat-transfer provider {provider!r}")
    metadata_path = billing_path.parent / "import.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"Cannot read provider snapshot metadata {metadata_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{metadata_path}: provider import metadata is invalid JSON") from exc
    billing = _read_rows(billing_path, {
        "email", "application", "license_status", "contract_id", "provider",
        "provider_user_id", "provider_license_id",
    })
    if metadata.get("provider") != provider:
        raise InputError(f"{metadata_path}: expected complete {provider} provider import")
    try:
        imported_at = datetime.fromisoformat(metadata["imported_at"]).isoformat()
        imported_datetime = datetime.fromisoformat(metadata["imported_at"])
        count = int(metadata["license_assignment_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError(f"{metadata_path}: import timestamp and assignment count are required") from exc
    if count != len(billing) or any(row["provider"].casefold() != provider for row in billing):
        raise InputError("Billing rows do not match the complete provider import metadata")
    states = _load_execution_states(ledger_path)
    pending = [row for row in states.values() if row["provider"] == provider and row["status"] in {"started", "succeeded", "failed"}]
    for transfer in pending:
        try:
            operation_at = datetime.fromisoformat(transfer["occurred_at"])
            snapshot_time = imported_datetime
            if operation_at.tzinfo is None and snapshot_time.tzinfo is not None:
                operation_at = operation_at.replace(tzinfo=timezone.utc)
            if snapshot_time.tzinfo is None and operation_at.tzinfo is not None:
                snapshot_time = snapshot_time.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise InputError("Transfer event or provider import has an invalid timestamp") from exc
        if snapshot_time <= operation_at:
            raise InputError("Provider snapshot predates or matches the transfer attempt; import fresh provider state")
    records: list[TransferVerification] = []
    for transfer in pending:
        matching = [row for row in billing if (
            _identity(row["application"]) == _identity(transfer["application"])
            and _identity(row["contract_id"]) == _identity(transfer["contract_id"])
            and row["license_status"].casefold() == "active"
        )]
        source = [row for row in matching if (
            _identity(row["email"]) == _identity(transfer["source_email"])
            and row["provider_user_id"] == transfer["source_user_id"]
            and row["provider_license_id"] == transfer["license_id"]
        )]
        target = [row for row in matching if _identity(row["email"]) == _identity(transfer["target_email"])]
        if not source and len(target) == 1:
            status, follow_up = "verified_transferred", "Source assignment absent; target assignment is present."
        elif source and not target:
            status, follow_up = "not_applied", "Source assignment remains and target assignment is absent; reconcile before retrying."
        else:
            status, follow_up = "partial", "Source/target state is mixed or ambiguous; reconcile provider assignments manually."
        records.append(TransferVerification(
            transfer["finding_id"], provider, transfer["source_email"], transfer["target_email"],
            transfer["source_user_id"], target[0]["provider_user_id"] if len(target) == 1 else "",
            status, imported_at, follow_up,
        ))
    if not records:
        return ()
    lock, lock_path = _lock(ledger_path, "verify")
    try:
        for record in records:
            transfer = next(row for row in pending if row["finding_id"] == record.finding_id)
            _append(ledger_path, {
                **transfer, "target_user_id": record.target_user_id, "status": record.status,
                "actor": "saas-sentry-verification", "occurred_at": datetime.now(timezone.utc).isoformat(),
                "result": f"snapshot={record.imported_at}; {record.follow_up}",
            })
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass
    return tuple(records)
