"""Input identity coverage and import freshness reporting."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
from typing import Iterable

from .engine import InputError, _rows, normalize_email


@dataclass(frozen=True)
class ImportHealthRow:
    source: str
    records: int
    matched_records: int
    identity_coverage_percent: str
    issue_count: int
    freshness_days: int | None
    freshness: str
    status: str


def _read_metadata(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"Cannot read import metadata {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path}: import metadata must be a JSON object")
    return data


def _freshness(imported: date, as_of: date, stale_after_days: int) -> tuple[int, str]:
    age = (as_of - imported).days
    if age < 0:
        return age, "future_dated"
    return age, "stale" if age > stale_after_days else "fresh"


def _coverage(records: int, matched: int) -> str:
    if records == 0:
        return ""
    return f"{matched * 100 / records:.2f}"


def _health_status(records: int, matched: int, issues: int, freshness: str) -> str:
    if records == 0:
        return "empty"
    if freshness in {"stale", "future_dated"}:
        return freshness
    if matched < records or issues:
        return "issues"
    return "healthy"


def _identity(email: str, employee_id: str) -> tuple[str, str]:
    return normalize_email(email), employee_id.strip().casefold()


def build_import_health(
    hr_path: Path,
    usage_path: Path,
    billing_path: Path,
    *,
    metadata_path: Path | None = None,
    as_of: date | None = None,
    stale_after_days: int = 7,
) -> tuple[ImportHealthRow, ...]:
    """Measure HR identity completeness, usage/billing match coverage, and source freshness."""
    if stale_after_days < 0:
        raise InputError("stale-after-days must be non-negative")
    as_of = as_of or date.today()
    hr_rows = _rows(hr_path, {"employee", "status"})
    usage_rows = _rows(usage_path, {"application", "last_login"})
    billing_rows = _rows(billing_path, {"application", "license_status", "annual_cost"})
    email_index: dict[str, set[int]] = {}
    id_index: dict[str, set[int]] = {}
    hr_issues = 0
    for index, row in enumerate(hr_rows):
        email, employee_id = _identity(row.get("email", ""), row.get("employee_id", ""))
        if not email and not employee_id:
            hr_issues += 1
        if row.get("status", "").casefold() not in {"active", "terminated"}:
            hr_issues += 1
        addresses = {email} if email else set()
        addresses.update(
            normalize_email(value) for value in row.get("email_aliases", "").split(";") if value.strip()
        )
        for address in addresses:
            email_index.setdefault(address, set()).add(index)
        if employee_id:
            id_index.setdefault(employee_id, set()).add(index)
    for matches in email_index.values():
        if len(matches) > 1:
            hr_issues += 1
    for matches in id_index.values():
        if len(matches) > 1:
            hr_issues += 1

    def identity_coverage(source_rows: list[dict[str, str]], source: str) -> tuple[int, int]:
        matched = 0
        issues = 0
        seen_records: set[tuple[str, str, str]] = set()
        for row in source_rows:
            email, employee_id = _identity(row.get("email", ""), row.get("employee_id", ""))
            email_people = email_index.get(email, set()) if email else set()
            id_people = id_index.get(employee_id, set()) if employee_id else set()
            email_person = next(iter(email_people)) if len(email_people) == 1 else None
            id_person = next(iter(id_people)) if len(id_people) == 1 else None
            has_identity = bool(email or employee_id)
            conflict = len(email_people) > 1 or len(id_people) > 1
            conflict |= email_person is not None and id_person is not None and email_person != id_person
            is_matched = has_identity and not conflict and (email_person is not None or id_person is not None)
            matched += int(is_matched)
            issues += int(not is_matched)
            app = " ".join(row.get("application", "").casefold().split())
            resolved_person = email_person if email_person is not None else id_person
            identity_key = str(resolved_person) if resolved_person is not None else email or employee_id
            duplicate_key = (identity_key, app, "")
            if source in {"usage", "billing"} and duplicate_key in seen_records:
                issues += 1
            seen_records.add(duplicate_key)
            if not app:
                issues += 1
            if source == "usage" and row.get("last_login", ""):
                try:
                    date.fromisoformat(row["last_login"][:10])
                except ValueError:
                    issues += 1
            if source == "billing":
                try:
                    amount = float(row.get("annual_cost", ""))
                    if not math.isfinite(amount) or amount < 0:
                        issues += 1
                except ValueError:
                    issues += 1
                if row.get("license_status", "").casefold() not in {"active", "inactive"}:
                    issues += 1
        return matched, issues

    usage_matched, usage_issues = identity_coverage(usage_rows, "usage")
    billing_matched, billing_issues = identity_coverage(billing_rows, "billing")
    source_paths = {"hr": hr_path, "usage": usage_path, "billing": billing_path}
    metadata = _read_metadata(metadata_path) if metadata_path else None
    imported_day: date | None = None
    if metadata is not None:
        imported_at = metadata.get("imported_at")
        if not isinstance(imported_at, str) or not imported_at.strip():
            raise InputError(f"{metadata_path}: imported_at is required and must be an ISO timestamp")
        try:
            parsed = datetime.fromisoformat(imported_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InputError(f"{metadata_path}: imported_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        imported_day = parsed.astimezone(timezone.utc).date()

    issue_counts = {"hr": hr_issues, "usage": usage_issues, "billing": billing_issues}
    matched_counts = {
        "hr": sum(bool(row.get("email", "").strip() or row.get("employee_id", "").strip()) for row in hr_rows),
        "usage": usage_matched,
        "billing": billing_matched,
    }
    record_counts = {"hr": len(hr_rows), "usage": len(usage_rows), "billing": len(billing_rows)}
    result: list[ImportHealthRow] = []
    for source, path in source_paths.items():
        if imported_day is not None and source in {"usage", "billing"}:
            age, freshness = _freshness(imported_day, as_of, stale_after_days)
        else:
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()
            except OSError as exc:
                raise InputError(f"Cannot inspect source freshness for {path}: {exc}") from exc
            age, freshness = _freshness(modified, as_of, stale_after_days)
        records, matched = record_counts[source], matched_counts[source]
        result.append(ImportHealthRow(
            source, records, matched, _coverage(records, matched), issue_counts[source], age,
            freshness, _health_status(records, matched, issue_counts[source], freshness),
        ))

    if metadata is not None:
        expected = metadata.get("license_assignment_count")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise InputError(f"{metadata_path}: license_assignment_count must be a non-negative integer")
        actual = len(billing_rows)
        discrepancy = int(actual != expected)
        age, freshness = _freshness(imported_day, as_of, stale_after_days)  # type: ignore[arg-type]
        result.append(ImportHealthRow(
            "import_metadata", expected, actual if not discrepancy else 0,
            _coverage(expected, actual if not discrepancy else 0), discrepancy, age, freshness,
            _health_status(expected, actual if not discrepancy else 0, discrepancy, freshness),
        ))
    return tuple(result)


def format_import_health(rows: Iterable[ImportHealthRow]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "source", "records", "matched_records", "identity_coverage_percent", "issue_count",
        "freshness_days", "freshness", "status",
    ])
    for row in rows:
        writer.writerow([
            row.source, row.records, row.matched_records, row.identity_coverage_percent,
            row.issue_count, row.freshness_days if row.freshness_days is not None else "",
            row.freshness, row.status,
        ])
    return output.getvalue()
