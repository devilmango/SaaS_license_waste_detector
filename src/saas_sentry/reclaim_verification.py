"""Compare completed provider reclaims with a later, complete provider import."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from .actions import ActionError
from .execution import _execution_states


VERIFICATION_FIELDS = (
    "verified_at", "snapshot_imported_at", "action_id", "finding_id", "provider", "email", "application",
    "provider_user_id", "provider_license_id", "status", "follow_up",
)
SUPPORTED_PROVIDERS = {"microsoft365", "google_workspace", "github_copilot", "slack", "zoom"}


@dataclass(frozen=True)
class ReclaimVerification:
    verified_at: str
    snapshot_imported_at: str
    action_id: str
    finding_id: str
    provider: str
    email: str
    application: str
    provider_user_id: str
    provider_license_id: str
    status: str
    follow_up: str


def _read_csv(path: Path, required: set[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ActionError(f"{path}: missing required columns: {', '.join(sorted(missing))}")
            return [{(key or "").strip(): (value or "").strip() for key, value in row.items() if key}
                    for row in reader]
    except OSError as exc:
        raise ActionError(f"Cannot read verification input {path}: {exc}") from exc


def _identity(value: str) -> str:
    return " ".join(value.casefold().split())


def _snapshot_metadata(billing_path: Path, provider: str, billing_rows: list[dict[str, str]]) -> str:
    metadata_path = billing_path.parent / "import.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ActionError(f"Cannot read provider import metadata {metadata_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ActionError(f"{metadata_path}: provider import metadata is invalid JSON") from exc
    if metadata.get("provider") != provider:
        raise ActionError(f"{metadata_path}: expected a {provider} import snapshot")
    try:
        declared_count = int(metadata["license_assignment_count"])
        imported_at = datetime.fromisoformat(metadata["imported_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ActionError(f"{metadata_path}: import timestamp and assignment count are required") from exc
    provider_rows = [row for row in billing_rows if row.get("provider", "").casefold() == provider]
    if len(provider_rows) != len(billing_rows) or declared_count != len(provider_rows):
        raise ActionError(f"{billing_path}: billing rows do not match the complete provider import metadata")
    return imported_at.isoformat()


def verify_provider_reclaims(
    action_ledger: Path,
    execution_ledger: Path,
    findings_path: Path | None,
    billing_path: Path,
    verification_ledger: Path,
    *,
    provider: str,
) -> tuple[ReclaimVerification, ...]:
    """Audit completed executions against a fresh provider snapshot and record follow-up states."""
    if provider not in SUPPORTED_PROVIDERS:
        raise ActionError(f"Unsupported reclaim provider {provider!r}")
    # Validate the action history using the domain ledger parser, including its transition rules.
    from .actions import list_actions

    valid_actions = {item.action_id: item for item in list_actions(action_ledger)}
    executions = _execution_states(execution_ledger)
    findings = _read_csv(findings_path, {"finding_id", "email", "application"}) if findings_path else []
    billing = _read_csv(billing_path, {
        "email", "application", "license_status", "provider", "provider_user_id", "provider_license_id",
    })
    imported_at = _snapshot_metadata(billing_path, provider, billing)
    verified_at = datetime.now(timezone.utc).isoformat()
    finding_by_id: dict[str, list[dict[str, str]]] = {}
    for finding in findings:
        finding_by_id.setdefault(finding.get("finding_id", ""), []).append(finding)
    completed = [
        row for row in executions.values()
        if row.get("provider") == provider and row.get("status") in {"succeeded", "reconciled_completed"}
    ]
    records: list[ReclaimVerification] = []
    for execution in completed:
        action_id = execution["action_id"]
        action = valid_actions.get(action_id)
        if not action or action.status not in {"approved", "reclaimed"}:
            continue
        if findings_path:
            matches = finding_by_id.get(action.finding_id, [])
            if len(matches) != 1:
                raise ActionError(f"Action {action_id} finding matched {len(matches)} rows in {findings_path}")
            finding = matches[0]
            if _identity(finding.get("email", "")) != _identity(execution.get("email", "")) or \
                    _identity(finding.get("application", "")) != _identity(execution.get("application", "")):
                raise ActionError(f"Action {action_id} execution target no longer matches its finding")
        assignments = [row for row in billing if (
            row.get("provider", "").casefold() == provider
            and row.get("license_status", "").casefold() == "active"
            and _identity(row.get("email", "")) == _identity(execution.get("email", ""))
            and _identity(row.get("application", "")) == _identity(execution.get("application", ""))
        )]
        exact = [row for row in assignments if (
            row.get("provider_user_id", "") == execution.get("provider_user_id", "")
            and row.get("provider_license_id", "") == execution.get("provider_license_id", "")
        )]
        if exact:
            status, follow_up = "still_assigned", "The exact license is still active; inspect the provider action and retry only after review."
        elif assignments:
            status, follow_up = "assignment_changed", "Active assignments changed; reconcile the new provider identity and license before closing this action."
        else:
            same_ids = [row for row in billing if (
                row.get("provider", "").casefold() == provider
                and row.get("license_status", "").casefold() == "active"
                and row.get("provider_user_id", "") == execution.get("provider_user_id", "")
                and row.get("provider_license_id", "") == execution.get("provider_license_id", "")
            )]
            if same_ids:
                status, follow_up = "identity_mismatch", "Provider assignment remains under a different identity; review before marking reclaimed."
            else:
                status, follow_up = "verified_reclaimed", "No follow-up required; record realized savings after billing verification."
        records.append(ReclaimVerification(
            verified_at, imported_at, action_id, action.finding_id, provider, execution.get("email", ""),
            execution.get("application", ""), execution.get("provider_user_id", ""),
            execution.get("provider_license_id", ""), status, follow_up,
        ))
    input_paths = {
        action_ledger.resolve(), execution_ledger.resolve(), billing_path.resolve(),
        (billing_path.parent / "import.json").resolve(),
    }
    if findings_path:
        input_paths.add(findings_path.resolve())
    if verification_ledger.resolve() in input_paths:
        raise ActionError("reclaim verification ledger cannot overwrite an input file")
    _append_verifications(verification_ledger, records)
    return tuple(records)


def _append_verifications(path: Path, records: Iterable[ReclaimVerification]) -> None:
    records = tuple(records)
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ActionError(f"Reclaim verification ledger is locked at {lock_path}") from exc
    try:
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=VERIFICATION_FIELDS)
            if not exists:
                writer.writeheader()
            for record in records:
                writer.writerow(record.__dict__)
    except OSError as exc:
        raise ActionError(f"Cannot write reclaim verification ledger {path}: {exc}") from exc
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


def format_verifications(records: Iterable[ReclaimVerification]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=VERIFICATION_FIELDS)
    writer.writeheader()
    for record in records:
        writer.writerow(record.__dict__)
    return output.getvalue()
