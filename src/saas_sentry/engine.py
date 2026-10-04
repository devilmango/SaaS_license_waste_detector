"""CSV loading, identity resolution, and rule-based license analysis."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable


class InputError(ValueError):
    """Raised when an input CSV cannot be safely analyzed."""


@dataclass(frozen=True)
class Person:
    employee: str
    email: str
    status: str


@dataclass(frozen=True)
class Usage:
    employee: str
    email: str
    application: str
    last_login: date | None
    usage_count: int | None


@dataclass(frozen=True)
class License:
    email: str
    application: str
    status: str
    annual_cost: Decimal


@dataclass(frozen=True)
class Finding:
    employee: str
    email: str
    application: str
    category: str
    reason: str
    annual_savings: Decimal


@dataclass(frozen=True)
class Analysis:
    findings: tuple[Finding, ...]
    license_count: int
    unmatched_licenses: tuple[str, ...]


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def _rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise InputError(f"Cannot read {path}: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        headers = {header.strip() for header in (reader.fieldnames or []) if header}
        missing = required - headers
        if missing:
            raise InputError(f"{path}: missing required columns: {', '.join(sorted(missing))}")
        return [
            {key.strip(): (value or "").strip() for key, value in row.items() if key}
            for row in reader
        ]


def _parse_date(value: str, path: Path, line: int) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InputError(f"{path}:{line}: invalid last_login {value!r}; use YYYY-MM-DD") from exc


def _parse_money(value: str, path: Path, line: int) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise InputError(f"{path}:{line}: annual_cost must be a number, got {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise InputError(f"{path}:{line}: annual_cost must be a finite, non-negative number")
    return amount


def _unique_email(records: Iterable[Person], path: Path) -> dict[str, Person]:
    result: dict[str, Person] = {}
    for person in records:
        key = normalize_email(person.email)
        if key in result:
            raise InputError(f"{path}: duplicate email identity {person.email!r}")
        result[key] = person
    return result


def load_inputs(hr_path: Path, usage_path: Path, billing_path: Path):
    """Load the three CSV exports, validating columns and identities."""
    hr_rows = _rows(hr_path, {"employee", "email", "status"})
    people: list[Person] = []
    for line, row in enumerate(hr_rows, start=2):
        if not row.get("email"):
            raise InputError(f"{hr_path}:{line}: email cannot be blank")
        status = row.get("status", "").casefold()
        if status not in {"active", "terminated"}:
            raise InputError(f"{hr_path}:{line}: status must be active or terminated")
        people.append(Person(row.get("employee", "") or row["email"], row["email"], status))
    person_by_email = _unique_email(people, hr_path)

    usage_rows = _rows(usage_path, {"employee", "email", "application", "last_login"})
    usage: dict[tuple[str, str], Usage] = {}
    for line, row in enumerate(usage_rows, start=2):
        email, app = row.get("email", ""), row.get("application", "")
        if not email or not app:
            raise InputError(f"{usage_path}:{line}: email and application cannot be blank")
        key = (normalize_email(email), app.casefold())
        if key in usage:
            raise InputError(f"{usage_path}:{line}: duplicate usage record for {email} / {app}")
        count_value = row.get("usage_count", "")
        try:
            usage_count = int(count_value) if count_value else None
        except ValueError as exc:
            raise InputError(f"{usage_path}:{line}: usage_count must be a whole number") from exc
        if usage_count is not None and usage_count < 0:
            raise InputError(f"{usage_path}:{line}: usage_count cannot be negative")
        usage[key] = Usage(
            row.get("employee", ""), email, app,
            _parse_date(row.get("last_login", ""), usage_path, line), usage_count,
        )

    billing_rows = _rows(billing_path, {"email", "application", "license_status", "annual_cost"})
    licenses: list[License] = []
    seen: set[tuple[str, str]] = set()
    for line, row in enumerate(billing_rows, start=2):
        email, app = row.get("email", ""), row.get("application", "")
        status = row.get("license_status", "").casefold()
        if not email or not app:
            raise InputError(f"{billing_path}:{line}: email and application cannot be blank")
        if status not in {"active", "inactive"}:
            raise InputError(f"{billing_path}:{line}: license_status must be active or inactive")
        key = (normalize_email(email), app.casefold())
        if key in seen:
            raise InputError(f"{billing_path}:{line}: duplicate license for {email} / {app}")
        seen.add(key)
        licenses.append(License(email, app, status, _parse_money(row.get("annual_cost", ""), billing_path, line)))
    return person_by_email, usage, licenses


def analyze(
    people: dict[str, Person],
    usage: dict[tuple[str, str], Usage],
    licenses: list[License],
    *,
    as_of: date | None = None,
    stale_days: int = 90,
    cost_threshold: Decimal = Decimal("10000"),
    low_usage_threshold: int = 5,
) -> Analysis:
    """Apply reclaim, stale-login, and expensive/low-usage rules.

    A license is counted at most once. Terminated accounts take precedence over
    stale login; expensive low-usage is considered when login data is recent.
    Costs are annual INR amounts supplied by the billing export.
    """
    today = as_of or date.today()
    findings: list[Finding] = []
    unmatched: list[str] = []
    for license in licenses:
        email_key = normalize_email(license.email)
        person = people.get(email_key)
        if not person:
            unmatched.append(f"{license.application}: {license.email}")
            continue
        if license.status != "active":
            continue
        activity = usage.get((email_key, license.application.casefold()))
        if person.status == "terminated":
            category, reason = "terminated", "Employee is terminated but the license is active."
        elif activity is None or activity.last_login is None:
            category, reason = "inactive", "No login activity was recorded; investigate or reclaim."
        elif (today - activity.last_login).days > stale_days:
            category, reason = "inactive", f"Last login was more than {stale_days} days ago."
        elif (
            license.annual_cost > cost_threshold
            and activity.usage_count is not None
            and activity.usage_count < low_usage_threshold
        ):
            category, reason = "optimization", "High annual cost with low recorded usage."
        else:
            continue
        findings.append(Finding(
            employee=person.employee,
            email=person.email,
            application=license.application,
            category=category,
            reason=reason,
            annual_savings=license.annual_cost,
        ))
    findings.sort(key=lambda item: (item.application.casefold(), item.category, item.email.casefold()))
    active_license_count = sum(license.status == "active" for license in licenses)
    return Analysis(tuple(findings), active_license_count, tuple(unmatched))


def format_report(analysis: Analysis) -> str:
    """Render an at-a-glance INR savings report grouped by application."""
    if not analysis.findings:
        return "Potential savings\n\nNo reclaim or optimization candidates found."
    totals: dict[tuple[str, str], tuple[int, Decimal]] = {}
    for finding in analysis.findings:
        key = (finding.application, finding.category)
        count, amount = totals.get(key, (0, Decimal("0")))
        totals[key] = (count + 1, amount + finding.annual_savings)
    lines = ["Potential savings", ""]
    labels = {"inactive": "inactive users", "terminated": "terminated accounts", "optimization": "optimization candidates"}
    for (application, category), (count, amount) in sorted(totals.items(), key=lambda item: (item[0][0].casefold(), item[0][1])):
        lines.extend((application, f"{count} {labels[category]}", f"₹{amount:,.0f}/year", ""))
    total = sum((finding.annual_savings for finding in analysis.findings), Decimal("0"))
    lines.extend((f"Total potential annual savings: ₹{total:,.0f}", f"Active licenses analyzed: {analysis.license_count}"))
    if analysis.unmatched_licenses:
        lines.extend(("", f"Unmatched billing identities: {len(analysis.unmatched_licenses)}"))
    return "\n".join(lines)


def format_findings_csv(analysis: Analysis) -> str:
    """Serialize findings for follow-up workflows."""
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["employee", "email", "application", "category", "reason", "annual_savings_inr"])
    for finding in analysis.findings:
        writer.writerow([
            finding.employee, finding.email, finding.application, finding.category,
            finding.reason, f"{finding.annual_savings:.2f}",
        ])
    return output.getvalue()
