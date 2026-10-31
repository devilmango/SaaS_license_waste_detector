"""Stage, validate, and publish provider exports as one recoverable import operation."""

from __future__ import annotations

import contextvars
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from typing import Callable

from .connectors import ConnectorError


_rate_limit_retries: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "sentry_rate_limit_retries", default=None,
)
EXPORTS = {
    "usage.csv": {"employee", "email", "application", "last_login"},
    "billing.csv": {"email", "application", "license_status", "annual_cost", "provider"},
}
HISTORY_FIELDS = (
    "import_id", "provider", "started_at", "finished_at", "status", "user_count",
    "assignment_count", "rate_limit_retries", "snapshot_version", "source_timestamp",
    "added_assignments", "removed_assignments", "changed_assignments", "validation_errors", "error",
)

CHANGE_FIELDS = ("change", "identity", "application", "before", "after")


def note_rate_limit_retry() -> None:
    """Count a retry in the current import operation, when one is active."""
    count = _rate_limit_retries.get()
    if count is not None:
        _rate_limit_retries.set(count + 1)


def current_rate_limit_retries() -> int:
    return _rate_limit_retries.get() or 0


def _validate_stage(stage: Path, provider: str, reported: tuple[int, int]) -> tuple[int, int, dict[str, object]]:
    records: dict[str, list[dict[str, str]]] = {}
    for filename, required in EXPORTS.items():
        path = stage / filename
        if not path.is_file():
            raise ConnectorError(f"Provider import did not produce staged {filename}")
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                missing = required - set(reader.fieldnames or [])
                if missing:
                    raise ConnectorError(f"staged {filename} is missing columns: {', '.join(sorted(missing))}")
                rows = list(reader)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise ConnectorError(f"cannot validate staged {filename}: {exc}") from exc
        if not rows:
            raise ConnectorError(f"staged {filename} is empty; refusing to publish a possibly partial import")
        records[filename] = rows
    usage_keys = set()
    for row in records["usage.csv"]:
        identity = (row.get("email") or row.get("employee_id") or "").strip().casefold()
        application = (row.get("application") or "").strip().casefold()
        if not identity or not application:
            raise ConnectorError("staged usage.csv contains a row without an identity or application")
        usage_keys.add((identity, application))
    billing_keys = set()
    for row in records["billing.csv"]:
        identity = (row.get("email") or row.get("employee_id") or "").strip().casefold()
        application = (row.get("application") or "").strip().casefold()
        if not identity or not application or row.get("provider") != provider:
            raise ConnectorError("staged billing.csv contains an incomplete or mismatched assignment")
        billing_keys.add((identity, application))
    if usage_keys != billing_keys:
        raise ConnectorError("staged usage and billing exports do not describe the same assignments")
    try:
        metadata = json.loads((stage / "import.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"provider import metadata is missing or invalid: {exc}") from exc
    if not isinstance(metadata, dict) or metadata.get("provider") != provider or metadata.get("read_only") is not True:
        raise ConnectorError("provider import metadata does not match the requested read-only provider import")
    users = len({(row.get("email") or row.get("employee_id") or "").casefold()
                 for row in records["usage.csv"]})
    assignments = len(records["billing.csv"])
    try:
        metadata_counts = (int(metadata.get("user_count", -1)),
                           int(metadata.get("license_assignment_count", -1)))
    except (TypeError, ValueError) as exc:
        raise ConnectorError("staged import metadata has invalid user or assignment counts") from exc
    if metadata_counts != (users, assignments):
        raise ConnectorError("staged import metadata counts do not match the validated CSV exports")
    if reported != (users, assignments):
        raise ConnectorError("provider import result counts do not match the staged exports")
    return users, assignments, metadata


def _assignment_rows(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            ((row.get("provider_user_id") or row.get("employee_id") or row.get("email") or "").strip().casefold(),
             (row.get("application") or "").strip().casefold()):
            {key: (value or "").strip() for key, value in row.items() if key}
            for row in csv.DictReader(handle)
        }


def _snapshot_changes(previous: Path | None, stage: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    before = _assignment_rows(previous / "billing.csv") if previous and (previous / "billing.csv").is_file() else {}
    after = _assignment_rows(stage / "billing.csv")
    changes: list[dict[str, str]] = []
    counts = {"added": 0, "removed": 0, "changed": 0}
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if old is None:
            kind = "added"
        elif new is None:
            kind = "removed"
        elif old != new:
            kind = "changed"
        else:
            continue
        counts[kind] += 1
        changes.append({
            "change": kind, "identity": key[0], "application": key[1],
            "before": json.dumps(old, sort_keys=True, ensure_ascii=False) if old is not None else "",
            "after": json.dumps(new, sort_keys=True, ensure_ascii=False) if new is not None else "",
        })
    return changes, counts


def _write_snapshot(output_dir: Path, provider: str, version: int, import_id: str,
                    source_timestamp: str, stage: Path, metadata: dict[str, object],
                    changes: list[dict[str, str]], counts: dict[str, int]) -> Path:
    snapshot = output_dir / "snapshots" / provider / f"{version:06d}"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=snapshot.parent))
    try:
        for filename in ("usage.csv", "billing.csv"):
            shutil.copy2(stage / filename, temporary / filename)
        snapshot_metadata = dict(metadata)
        snapshot_metadata.update({"import_id": import_id, "snapshot_version": version,
                                  "source_timestamp": source_timestamp})
        (temporary / "import.json").write_text(
            json.dumps(snapshot_metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        with (temporary / "changes.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CHANGE_FIELDS)
            writer.writeheader()
            writer.writerows(changes)
        (temporary / "validation.json").write_text(json.dumps({
            "valid": True, "published": False, "errors": [],
            "assignment_count": len(_assignment_rows(stage / "billing.csv")),
            "change_counts": counts,
        }, indent=2) + "\n", encoding="utf-8")
        if snapshot.exists():
            raise ConnectorError(f"provider snapshot {snapshot} already exists")
        os.replace(temporary, snapshot)
        return snapshot
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def list_import_snapshots(output_dir: Path, provider: str) -> tuple[dict[str, str], ...]:
    """List versioned provider snapshots with source timestamps and validation state."""
    root = output_dir / "snapshots" / provider
    rows = []
    if not root.exists():
        return ()
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and path.name.isdigit()):
        metadata = json.loads((folder / "import.json").read_text(encoding="utf-8"))
        validation = json.loads((folder / "validation.json").read_text(encoding="utf-8"))
        changes = validation.get("change_counts", {})
        rows.append({
            "provider": provider, "snapshot_version": str(metadata["snapshot_version"]),
            "import_id": str(metadata["import_id"]), "source_timestamp": str(metadata["source_timestamp"]),
            "valid": str(bool(validation.get("valid"))).lower(),
            "published": str(bool(validation.get("published"))).lower(),
            "validation_errors": "; ".join(validation.get("errors", [])),
            "added_assignments": str(changes.get("added", 0)),
            "removed_assignments": str(changes.get("removed", 0)),
            "changed_assignments": str(changes.get("changed", 0)),
        })
    return tuple(rows)


def _mark_snapshot_published(snapshot: Path) -> None:
    path = snapshot / "validation.json"
    validation = json.loads(path.read_text(encoding="utf-8"))
    validation["published"] = True
    path.write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _publish(stage: Path, output_dir: Path, import_id: str, metadata: dict[str, object]) -> None:
    metadata_path = stage / "import.json"
    metadata.update({"import_id": import_id, "rate_limit_retries": current_rate_limit_retries()})
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    backup_dir = stage / "previous"
    backup_dir.mkdir()
    published: list[Path] = []
    backed_up: list[tuple[Path, Path]] = []
    # Publish the import metadata last; its import_id identifies the committed CSV pair.
    filenames = ("usage.csv", "billing.csv", "import.json")
    try:
        for filename in filenames:
            target = output_dir / filename
            source = stage / filename
            if target.exists():
                backup = backup_dir / filename
                os.replace(target, backup)
                backed_up.append((target, backup))
            os.replace(source, target)
            published.append(target)
    except OSError as exc:
        for target in reversed(published):
            target.unlink(missing_ok=True)
        for target, backup in reversed(backed_up):
            if backup.exists():
                os.replace(backup, target)
        raise ConnectorError(f"could not publish provider exports; previous exports were restored: {exc}") from exc


def _record_history(output_dir: Path, row: dict[str, object]) -> None:
    path = output_dir / "import-history.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".import-history.lock"
    temporary = path.with_name(path.name + ".tmp")
    acquired = False
    try:
        lock = lock_path.open("x", encoding="utf-8")
        acquired = True
        with lock:
            existing_rows: list[dict[str, str]] = []
            if path.exists() and path.stat().st_size > 0:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    existing_rows = list(reader)
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
                writer.writeheader()
                for previous in existing_rows:
                    writer.writerow({field: previous.get(field, "") for field in HISTORY_FIELDS})
                writer.writerow({field: row.get(field, "") for field in HISTORY_FIELDS})
            os.replace(temporary, path)
    except FileExistsError:
        raise ConnectorError(f"provider import history is locked at {lock_path}")
    except (OSError, csv.Error, UnicodeError) as exc:
        raise ConnectorError(f"cannot record provider import history: {exc}") from exc
    finally:
        if acquired:
            temporary.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)


def run_staged_import(provider: str, output_dir: Path,
                      operation: Callable[[Path], tuple[int, int]]) -> tuple[int, int]:
    """Run provider export generation in isolation and publish only a validated complete set."""
    output_dir.mkdir(parents=True, exist_ok=True)
    import_id = uuid.uuid4().hex[:16]
    started_at = datetime.now(timezone.utc).isoformat()
    stage = Path(tempfile.mkdtemp(prefix=".provider-import-", dir=output_dir))
    token = _rate_limit_retries.set(0)
    outcome: tuple[int, int] | None = None
    failure = ""
    try:
        reported = operation(stage)
        users, assignments, metadata = _validate_stage(stage, provider, reported)
        snapshot_rows = list_import_snapshots(output_dir, provider)
        snapshot_version = max((int(row["snapshot_version"]) for row in snapshot_rows), default=0) + 1
        if snapshot_rows:
            previous_snapshot = output_dir / "snapshots" / provider / f"{int(snapshot_rows[-1]['snapshot_version']):06d}"
        else:
            try:
                previous_provider = json.loads((output_dir / "import.json").read_text(encoding="utf-8")).get("provider")
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                previous_provider = None
            previous_snapshot = output_dir if previous_provider == provider else None
        changes, change_counts = _snapshot_changes(previous_snapshot, stage)
        source_timestamp = str(metadata.get("source_timestamp") or metadata.get("exported_at")
                               or metadata.get("imported_at") or started_at)
        snapshot = _write_snapshot(output_dir, provider, snapshot_version, import_id, source_timestamp,
                                   stage, metadata, changes, change_counts)
        metadata.update({"snapshot_version": snapshot_version, "source_timestamp": source_timestamp,
                         "change_counts": change_counts, "validation_errors": []})
        _publish(stage, output_dir, import_id, metadata)
        try:
            _mark_snapshot_published(snapshot)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: provider exports were published, but snapshot status could not be updated: {exc}",
                  file=sys.stderr)
        outcome = (users, assignments)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {str(exc)}"[:1000]
    finally:
        retries = current_rate_limit_retries()
        _rate_limit_retries.reset(token)
        shutil.rmtree(stage, ignore_errors=True)
    finished_at = datetime.now(timezone.utc).isoformat()
    history = {
        "import_id": import_id, "provider": provider, "started_at": started_at,
        "finished_at": finished_at, "status": "succeeded" if outcome is not None else "failed",
        "user_count": outcome[0] if outcome else "", "assignment_count": outcome[1] if outcome else "",
        "rate_limit_retries": retries,
        "snapshot_version": snapshot_version if outcome is not None else "",
        "source_timestamp": source_timestamp if outcome is not None else "",
        "added_assignments": change_counts["added"] if outcome is not None else "",
        "removed_assignments": change_counts["removed"] if outcome is not None else "",
        "changed_assignments": change_counts["changed"] if outcome is not None else "",
        "validation_errors": failure if outcome is None else "", "error": failure,
    }
    try:
        _record_history(output_dir, history)
    except ConnectorError as exc:
        if outcome is None:
            raise ConnectorError(f"{failure}; import history could not be recorded: {exc}") from exc
        print(f"Warning: provider exports were published, but import history could not be recorded: {exc}", file=sys.stderr)
    if outcome is None:
        raise ConnectorError(failure or "provider import failed")
    return outcome
