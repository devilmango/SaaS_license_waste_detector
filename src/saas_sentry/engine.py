"""CSV loading, identity resolution, and rule-based license analysis."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


class InputError(ValueError):
    """Raised when an input CSV or configuration cannot be safely analyzed."""


@dataclass(frozen=True)
class Person:
    key: str
    employee: str
    email: str
    status: str
    employee_id: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Usage:
    person_key: str
    employee: str
    email: str
    application: str
    last_login: date | None
    usage_count: int | None
    active_days: int | None
    window_days: int
    features_used: int | None


@dataclass(frozen=True)
class License:
    person_key: str | None
    email: str
    employee_id: str
    application: str
    status: str
    annual_cost_inr: Decimal
    contract_id: str
    renewal_date: date | None
    seat_minimum: int
    commitment_end_date: date | None
    notice_days: int
    bundle_group: str
    source_row: int


@dataclass(frozen=True)
class DataIssue:
    source: str
    row: int | str
    identity: str
    issue: str
    details: str


@dataclass(frozen=True)
class InputData:
    people: dict[str, Person]
    usage: dict[tuple[str, str], Usage]
    licenses: tuple[License, ...]
    issues: tuple[DataIssue, ...]
    price_tiers: dict[str, tuple["ContractTier", ...]]


@dataclass(frozen=True)
class ContractTier:
    min_seats: int
    max_seats: int | None
    annual_cost_per_seat_inr: Decimal


@dataclass(frozen=True)
class Rules:
    stale_days: int = 90
    cost_threshold_inr: Decimal = Decimal("10000")
    low_usage_threshold: int = 5
    low_active_day_ratio: Decimal = Decimal("0.10")
    low_features_used_threshold: int = 2
    default_usage_window_days: int = 30


@dataclass(frozen=True)
class CurrencyConfig:
    default_currency: str = "INR"
    rate_date: date | None = None
    rates_to_inr: dict[str, Decimal] | None = None

    def convert(self, amount: Decimal, currency: str, path: Path, line: int) -> Decimal:
        code = (currency or self.default_currency).upper()
        rates = self.rates_to_inr or {"INR": Decimal("1")}
        if code not in rates:
            raise InputError(f"{path}:{line}: no INR exchange rate configured for currency {code!r}")
        return (amount * rates[code]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Finding:
    finding_id: str
    employee: str
    email: str
    employee_id: str
    application: str
    contract_id: str
    category: str
    reason: str
    annualized_cost_inr: Decimal
    opportunity_savings_inr: Decimal = Decimal("0")
    realizable_12m_inr: Decimal = Decimal("0")
    renewal_date: date | None = None
    commitment_end_date: date | None = None
    notice_days: int = 0
    bundle_group: str = ""
    savings_effective_date: date | None = None
    evidence: str = ""


@dataclass(frozen=True)
class Analysis:
    findings: tuple[Finding, ...]
    active_license_count: int
    issues: tuple[DataIssue, ...]
    currency_rate_date: date | None


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def _normalize_id(value: str) -> str:
    return value.strip().casefold()


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


def _parse_date(value: str, path: Path, line: int, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InputError(f"{path}:{line}: invalid {field} {value!r}; use YYYY-MM-DD") from exc


def _parse_int(value: str, path: Path, line: int, field: str, *, default: int | None = None) -> int | None:
    if not value:
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise InputError(f"{path}:{line}: {field} must be a whole number") from exc
    if number < 0:
        raise InputError(f"{path}:{line}: {field} cannot be negative")
    return number


def _parse_money(value: str, path: Path, line: int) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise InputError(f"{path}:{line}: annual_cost must be a number, got {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise InputError(f"{path}:{line}: annual_cost must be a finite, non-negative number")
    return amount


def _resolve_identity(
    email: str,
    employee_id: str,
    email_index: dict[str, list[Person]],
    id_index: dict[str, list[Person]],
) -> tuple[Person | None, str | None]:
    by_email = email_index.get(normalize_email(email), []) if email else []
    by_id = id_index.get(_normalize_id(employee_id), []) if employee_id else []
    if len(by_email) > 1:
        return None, "email resolves to multiple HR people"
    if len(by_id) > 1:
        if len(by_email) == 1:
            return by_email[0], "employee_id collides; matched by email"
        return None, "employee_id resolves to multiple HR people"
    email_person = by_email[0] if by_email else None
    id_person = by_id[0] if by_id else None
    if email_person and id_person and email_person.key != id_person.key:
        return None, "email and employee_id resolve to different HR people"
    if id_person and email and not email_person:
        return id_person, "email is not a registered address; matched by employee_id"
    if employee_id and not id_person and email_person:
        return email_person, "employee_id is unknown; matched by email"
    return id_person or email_person, None


def load_inputs(
    hr_path: Path,
    usage_path: Path,
    billing_path: Path,
    *,
    currency: CurrencyConfig | None = None,
    default_usage_window_days: int = 30,
    contract_pricing_path: Path | None = None,
) -> InputData:
    """Load CSV exports and resolve identity by employee ID, email, and aliases."""
    currency = currency or CurrencyConfig()
    issues: list[DataIssue] = []
    hr_rows = _rows(hr_path, {"employee", "status"})
    people: dict[str, Person] = {}
    email_index: dict[str, list[Person]] = {}
    id_index: dict[str, list[Person]] = {}
    for line, row in enumerate(hr_rows, start=2):
        email = row.get("email", "")
        employee_id = row.get("employee_id", "")
        if not email and not employee_id:
            raise InputError(f"{hr_path}:{line}: provide email, employee_id, or both")
        status = row.get("status", "").casefold()
        if status not in {"active", "terminated"}:
            raise InputError(f"{hr_path}:{line}: status must be active or terminated")
        aliases = tuple(
            normalized for value in row.get("email_aliases", "").split(";")
            if (normalized := normalize_email(value))
        )
        normalized_email = normalize_email(email)
        key = f"id:{_normalize_id(employee_id)}" if employee_id else f"email:{normalized_email}"
        if key in people:
            issues.append(DataIssue("hr", line, email or employee_id, "duplicate_person_key", f"Identity key {key!r} is duplicated."))
            key = f"{key}:row:{line}"
        person = Person(key, row.get("employee", "") or email or employee_id, email,
                        status, employee_id, aliases)
        people[key] = person
        for address in sorted({normalized_email, *aliases}):
            if address:
                email_index.setdefault(address, []).append(person)
        if employee_id:
            id_index.setdefault(_normalize_id(employee_id), []).append(person)

    for address, matches in email_index.items():
        if len(matches) > 1:
            issues.append(DataIssue("hr", "multiple", address, "email_collision",
                                    "This primary email or alias belongs to multiple HR rows; records using it will be excluded."))
    for employee_id, matches in id_index.items():
        if len(matches) > 1:
            issues.append(DataIssue("hr", "multiple", employee_id, "employee_id_collision",
                                    "This employee_id appears on multiple HR rows; records using it will be excluded."))

    usage_rows = _rows(usage_path, {"application", "last_login"})
    usage: dict[tuple[str, str], Usage] = {}
    for line, row in enumerate(usage_rows, start=2):
        email, employee_id, app = row.get("email", ""), row.get("employee_id", ""), row.get("application", "")
        if not app:
            raise InputError(f"{usage_path}:{line}: application cannot be blank")
        if not email and not employee_id:
            raise InputError(f"{usage_path}:{line}: provide email, employee_id, or both")
        person, issue = _resolve_identity(email, employee_id, email_index, id_index)
        identity = f"employee_id={employee_id}; email={email}"
        if issue and not person:
            issues.append(DataIssue("usage", line, identity, "identity_conflict", issue))
            continue
        if issue:
            issues.append(DataIssue("usage", line, identity, "identity_warning", issue))
        if not person:
            issues.append(DataIssue("usage", line, identity, "unmatched_identity", "No HR person matched this usage record."))
            continue
        key = (person.key, app.casefold())
        if key in usage:
            raise InputError(f"{usage_path}:{line}: duplicate usage record for {identity} / {app}")
        if (not issue and email and person.email
                and normalize_email(email) not in {normalize_email(person.email), *person.aliases}):
            issues.append(DataIssue("usage", line, identity, "unrecognized_email_alias", "Identity matched by employee_id, but email is not in the HR primary email or alias list."))
        window = _parse_int(row.get("usage_window_days", ""), usage_path, line,
                            "usage_window_days", default=default_usage_window_days)
        if not window:
            raise InputError(f"{usage_path}:{line}: usage_window_days must be greater than zero")
        active_days = _parse_int(row.get("active_days", ""), usage_path, line, "active_days")
        if active_days is not None and active_days > window:
            raise InputError(f"{usage_path}:{line}: active_days cannot exceed usage_window_days")
        usage[key] = Usage(
            person.key, row.get("employee", "") or person.employee, email or person.email, app,
            _parse_date(row.get("last_login", ""), usage_path, line, "last_login"),
            _parse_int(row.get("usage_count", ""), usage_path, line, "usage_count"),
            active_days, window,
            _parse_int(row.get("features_used", ""), usage_path, line, "features_used"),
        )

    billing_rows = _rows(billing_path, {"application", "license_status", "annual_cost"})
    licenses: list[License] = []
    seen: set[tuple[str, str]] = set()
    contract_minimums: dict[str, int] = {}
    contract_terms: dict[str, tuple[int, date | None, date | None, int]] = {}
    for line, row in enumerate(billing_rows, start=2):
        email, employee_id, app = row.get("email", ""), row.get("employee_id", ""), row.get("application", "")
        if not app:
            raise InputError(f"{billing_path}:{line}: application cannot be blank")
        if not email and not employee_id:
            raise InputError(f"{billing_path}:{line}: provide email, employee_id, or both")
        status = row.get("license_status", "").casefold()
        if status not in {"active", "inactive"}:
            raise InputError(f"{billing_path}:{line}: license_status must be active or inactive")
        person, identity_issue = _resolve_identity(email, employee_id, email_index, id_index)
        identity = f"employee_id={employee_id}; email={email}"
        if identity_issue and not person:
            issues.append(DataIssue("billing", line, identity, "identity_conflict", identity_issue))
        elif not person:
            issues.append(DataIssue("billing", line, identity, "unmatched_identity", "No HR person matched this license."))
        elif (not identity_issue and email and person.email
              and normalize_email(email) not in {normalize_email(person.email), *person.aliases}):
            issues.append(DataIssue("billing", line, identity, "unrecognized_email_alias", "Identity matched by employee_id, but email is not in the HR primary email or alias list."))
        elif identity_issue:
            issues.append(DataIssue("billing", line, identity, "identity_warning", identity_issue))
        contract_id = row.get("contract_id", "") or app
        contract_key = contract_id.casefold()
        minimum = _parse_int(row.get("seat_minimum", ""), billing_path, line, "seat_minimum", default=0)
        assert minimum is not None
        if contract_key in contract_minimums and contract_minimums[contract_key] != minimum:
            raise InputError(f"{billing_path}:{line}: seat_minimum conflicts with other rows for contract {contract_id!r}")
        contract_minimums[contract_key] = minimum
        renewal_date = _parse_date(row.get("renewal_date", ""), billing_path, line, "renewal_date")
        commitment_end = _parse_date(row.get("commitment_end_date", ""), billing_path, line, "commitment_end_date")
        notice_days = _parse_int(row.get("notice_days", ""), billing_path, line, "notice_days", default=0)
        assert notice_days is not None
        terms = (minimum, renewal_date, commitment_end, notice_days)
        if contract_key in contract_terms and contract_terms[contract_key] != terms:
            raise InputError(f"{billing_path}:{line}: contract terms conflict across rows for {contract_id!r}")
        contract_terms[contract_key] = terms
        unique_identity = person.key if person else _normalize_id(employee_id) or normalize_email(email)
        pair_key = (unique_identity, app.casefold())
        if pair_key in seen:
            raise InputError(f"{billing_path}:{line}: duplicate license for {identity} / {app}")
        seen.add(pair_key)
        annual_cost = _parse_money(row.get("annual_cost", ""), billing_path, line)
        annual_cost_inr = currency.convert(annual_cost, row.get("currency", ""), billing_path, line)
        bundle_group = row.get("bundle_group", "")
        licenses.append(License(
            person.key if person else None,
            email or (person.email if person else ""), employee_id, app, status,
            annual_cost_inr, contract_id, renewal_date, minimum, commitment_end,
            notice_days, bundle_group, line,
        ))
    price_tiers: dict[str, tuple[ContractTier, ...]] = {}
    if contract_pricing_path:
        tier_rows = _rows(contract_pricing_path, {"contract_id", "min_seats", "annual_cost_per_seat"})
        collected: dict[str, list[ContractTier]] = {}
        for line, row in enumerate(tier_rows, start=2):
            contract_id = row.get("contract_id", "")
            if not contract_id:
                raise InputError(f"{contract_pricing_path}:{line}: contract_id cannot be blank")
            minimum = _parse_int(row.get("min_seats", ""), contract_pricing_path, line, "min_seats", default=0)
            maximum = _parse_int(row.get("max_seats", ""), contract_pricing_path, line, "max_seats")
            assert minimum is not None
            if maximum is not None and maximum < minimum:
                raise InputError(f"{contract_pricing_path}:{line}: max_seats must be at least min_seats")
            cost = _parse_money(row.get("annual_cost_per_seat", ""), contract_pricing_path, line)
            cost_inr = currency.convert(cost, row.get("currency", ""), contract_pricing_path, line)
            collected.setdefault(contract_id.casefold(), []).append(ContractTier(minimum, maximum, cost_inr))
        for contract_id, tiers in collected.items():
            ordered = sorted(tiers, key=lambda tier: tier.min_seats)
            for previous, current in zip(ordered, ordered[1:]):
                if previous.max_seats is None or current.min_seats <= previous.max_seats:
                    raise InputError(f"{contract_pricing_path}: overlapping price tiers for contract {contract_id!r}")
            price_tiers[contract_id] = tuple(ordered)
    return InputData(people, usage, tuple(licenses), tuple(issues), price_tiers)


def load_config(path: Path | None) -> tuple[Rules, CurrencyConfig]:
    """Read optional TOML rules and dated conversion rates."""
    if path is None:
        return Rules(), CurrencyConfig(rates_to_inr={"INR": Decimal("1")})
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"Cannot read TOML configuration {path}: {exc}") from exc
    rule_data = data.get("rules", {})
    usage_data = data.get("usage", {})
    currency_data = data.get("currency", {})
    try:
        rules = Rules(
            stale_days=int(rule_data.get("stale_days", 90)),
            cost_threshold_inr=Decimal(str(rule_data.get("cost_threshold_inr", "10000"))),
            low_usage_threshold=int(rule_data.get("low_usage_threshold", 5)),
            low_active_day_ratio=Decimal(str(rule_data.get("low_active_day_ratio", "0.10"))),
            low_features_used_threshold=int(rule_data.get("low_features_used_threshold", 2)),
            default_usage_window_days=int(usage_data.get("window_days", 30)),
        )
        default_currency = str(currency_data.get("default_currency", "INR")).upper()
        raw_rates = currency_data.get("rates_to_inr", {"INR": 1})
        rates = {str(code).upper(): Decimal(str(rate)) for code, rate in raw_rates.items()}
        rate_date_raw = currency_data.get("rate_date")
        rate_date = date.fromisoformat(str(rate_date_raw)) if rate_date_raw else None
    except (ValueError, InvalidOperation, AttributeError) as exc:
        raise InputError(f"Invalid rule or currency setting in {path}: {exc}") from exc
    if min(rules.stale_days, rules.low_usage_threshold, rules.low_features_used_threshold) < 0:
        raise InputError(f"{path}: day and usage thresholds must be non-negative")
    if not rules.low_active_day_ratio.is_finite() or not Decimal("0") <= rules.low_active_day_ratio <= Decimal("1"):
        raise InputError(f"{path}: low_active_day_ratio must be between zero and one")
    if rules.default_usage_window_days < 1 or not rules.cost_threshold_inr.is_finite() or rules.cost_threshold_inr < 0:
        raise InputError(f"{path}: usage window must be positive and cost threshold finite/non-negative")
    if not rates or any(not rate.is_finite() or rate <= 0 for rate in rates.values()):
        raise InputError(f"{path}: currency rates must be finite positive values")
    if "INR" not in rates:
        rates["INR"] = Decimal("1")
    if any(code != "INR" for code in rates) and rate_date is None:
        raise InputError(f"{path}: currency.rate_date is required when non-INR rates are configured")
    if default_currency not in rates:
        raise InputError(f"{path}: default currency {default_currency!r} has no configured rate")
    return rules, CurrencyConfig(default_currency, rate_date, rates)


def analyze(
    data: InputData,
    *,
    as_of: date | None = None,
    rules: Rules | None = None,
    currency: CurrencyConfig | None = None,
) -> Analysis:
    """Apply the configured reclaim, stale-activity, and low-use rules."""
    today = as_of or date.today()
    rules = rules or Rules()
    currency = currency or CurrencyConfig(rates_to_inr={"INR": Decimal("1")})
    candidates: list[tuple[Finding, License]] = []
    issues = list(data.issues)
    active_licenses = [license for license in data.licenses if license.status == "active"]
    for license in active_licenses:
        person = data.people.get(license.person_key or "")
        if not person:
            continue
        activity = data.usage.get((person.key, license.application.casefold()))
        if person.status == "terminated":
            category, reason = "terminated", "Employee is terminated but the license is active."
            evidence = f"HR status={person.status}; license status={license.status}"
        elif activity is None or activity.last_login is None:
            category, reason = "inactive", "No login activity was recorded; investigate or reclaim."
            evidence = "No matching usage record or last_login is blank."
        elif (today - activity.last_login).days > rules.stale_days:
            category = "inactive"
            reason = f"Last login was more than {rules.stale_days} days ago."
            evidence = f"Last login={activity.last_login.isoformat()}"
        elif (
            (activity.active_days is not None
             and Decimal(activity.active_days) / Decimal(activity.window_days) < rules.low_active_day_ratio)
            or (activity.features_used is not None and activity.features_used < rules.low_features_used_threshold)
        ):
            if license.annual_cost_inr <= rules.cost_threshold_inr:
                continue
            category = "optimization"
            reason = "High annual cost with few active days or product features used."
            evidence = f"Active days={activity.active_days}/{activity.window_days}; features used={activity.features_used if activity.features_used is not None else 'not provided'}"
        elif (
            license.annual_cost_inr > rules.cost_threshold_inr
            and activity.usage_count is not None
            and activity.usage_count < rules.low_usage_threshold
        ):
            category, reason = "optimization", "High annual cost with low recorded usage."
            evidence = f"Usage count={activity.usage_count}; window={activity.window_days} days"
        else:
            continue
        finding_id = hashlib.sha256(
            f"{person.key}\0{license.application.casefold()}".encode("utf-8")
        ).hexdigest()[:16]
        finding = Finding(
            finding_id, person.employee, person.email or license.email, person.employee_id,
            license.application, license.contract_id, category, reason,
            license.annual_cost_inr, renewal_date=license.renewal_date,
            commitment_end_date=license.commitment_end_date, notice_days=license.notice_days,
            bundle_group=license.bundle_group, evidence=evidence,
        )
        candidates.append((finding, license))

    by_contract: dict[str, list[License]] = {}
    candidates_by_contract: dict[str, list[tuple[Finding, License]]] = {}
    for license in active_licenses:
        by_contract.setdefault(license.contract_id.casefold(), []).append(license)
    for candidate in candidates:
        candidates_by_contract.setdefault(candidate[1].contract_id.casefold(), []).append(candidate)

    selected_by_contract: dict[str, set[str]] = {}
    for contract_key, contract_candidates in candidates_by_contract.items():
        contract_licenses = by_contract[contract_key]
        floor = max((license.seat_minimum for license in contract_licenses), default=0)
        removable_seats = max(0, len(contract_licenses) - floor)
        if len(contract_candidates) > removable_seats:
            issues.append(DataIssue(
                "billing", "contract", contract_candidates[0][1].contract_id, "seat_minimum_limit",
                f"{len(contract_candidates)} candidate seats but only {removable_seats} seats can be removed above the {floor}-seat minimum.",
            ))
        eligible = sorted(contract_candidates, key=lambda item: item[1].annual_cost_inr, reverse=True)
        selected_by_contract[contract_key] = {finding.finding_id for finding, _ in eligible[:removable_seats]}

    bundle_licenses: dict[tuple[str, str], list[License]] = {}
    for license in active_licenses:
        if license.person_key and license.bundle_group:
            bundle_licenses.setdefault((license.person_key, license.bundle_group.casefold()), []).append(license)
    candidate_license_by_id = {finding.finding_id: license for finding, license in candidates}
    id_by_license = {
        (license.person_key, license.application.casefold()): finding.finding_id
        for finding, license in candidates if license.person_key
    }
    for (person_key, bundle_key), bundled_licenses in bundle_licenses.items():
        candidate_ids = [
            id_by_license.get((person_key, license.application.casefold())) for license in bundled_licenses
        ]
        candidate_ids = [finding_id for finding_id in candidate_ids if finding_id]
        selected_ids = [
            finding_id for finding_id in candidate_ids
            if finding_id in selected_by_contract.get(candidate_license_by_id[finding_id].contract_id.casefold(), set())
        ]
        if candidate_ids and len(selected_ids) != len(bundled_licenses):
            for finding_id in selected_ids:
                license = candidate_license_by_id[finding_id]
                selected_by_contract[license.contract_id.casefold()].discard(finding_id)
            issues.append(DataIssue(
                "billing", "bundle", bundle_key, "bundle_constraint",
                "Savings are not counted unless all active licenses for this person in the bundle are reclaimable.",
            ))

    def tier_for_seats(contract_key: str, seats: int) -> ContractTier | None:
        return next((tier for tier in data.price_tiers.get(contract_key, ())
                     if tier.min_seats <= seats and (tier.max_seats is None or seats <= tier.max_seats)), None)

    updated: list[Finding] = []
    horizon = today + timedelta(days=365)
    for contract_key, contract_candidates in candidates_by_contract.items():
        contract_licenses = by_contract[contract_key]
        selected_ids = selected_by_contract[contract_key]
        selected_candidates = sorted(
            ((finding, license) for finding, license in contract_candidates if finding.finding_id in selected_ids),
            key=lambda item: item[1].annual_cost_inr, reverse=True,
        )
        remove_count = len(selected_candidates)
        baseline_total = sum((license.annual_cost_inr for license in contract_licenses), Decimal("0"))
        projected_total = max(Decimal("0"), baseline_total - sum((license.annual_cost_inr for _, license in selected_candidates), Decimal("0")))
        if contract_key in data.price_tiers:
            current_tier = tier_for_seats(contract_key, len(contract_licenses))
            remaining_tier = tier_for_seats(contract_key, len(contract_licenses) - remove_count)
            if not current_tier or (len(contract_licenses) > remove_count and not remaining_tier):
                raise InputError(f"No configured contract price tier covers the current or projected seat count for {contract_key!r}")
            baseline_total = current_tier.annual_cost_per_seat_inr * len(contract_licenses)
            remaining = len(contract_licenses) - remove_count
            projected_total = remaining_tier.annual_cost_per_seat_inr * remaining if remaining_tier and remaining else Decimal("0")
        total_opportunity = max(Decimal("0"), baseline_total - projected_total)
        weight_total = sum((license.annual_cost_inr for _, license in selected_candidates), Decimal("0"))
        allocations: dict[str, Decimal] = {}
        unallocated = total_opportunity
        for index, (finding, license) in enumerate(selected_candidates):
            if index == len(selected_candidates) - 1:
                amount = unallocated
            elif weight_total:
                amount = (total_opportunity * license.annual_cost_inr / weight_total).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            else:
                amount = Decimal("0")
            allocations[finding.finding_id] = amount
            unallocated -= amount
        for finding, license in contract_candidates:
            opportunity = allocations.get(finding.finding_id, Decimal("0"))
            effective = max((day for day in (license.renewal_date, license.commitment_end_date) if day), default=today)
            if license.notice_days and today > effective - timedelta(days=license.notice_days):
                effective += timedelta(days=365)
            effective = max(today, effective)
            remaining_days = max(0, (horizon - max(today, effective)).days)
            realizable = (opportunity * Decimal(remaining_days) / Decimal(365)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            updated.append(Finding(
                **{**finding.__dict__, "opportunity_savings_inr": opportunity,
                   "realizable_12m_inr": realizable,
                   "savings_effective_date": effective}
            ))
    updated.sort(key=lambda item: (item.application.casefold(), item.category, item.email.casefold()))
    active_count = len(active_licenses)
    return Analysis(tuple(updated), active_count, tuple(issues), currency.rate_date)


def format_report(analysis: Analysis, reviews: dict[str, dict[str, str]] | None = None) -> str:
    """Render annualized, contract-adjusted, and scheduled savings separately."""
    reviews = reviews or {}
    if not analysis.findings:
        lines = ["Potential savings", "", "No reclaim or optimization candidates found."]
    else:
        totals: dict[tuple[str, str], tuple[int, Decimal, Decimal, Decimal]] = {}
        for finding in analysis.findings:
            key = (finding.application, finding.category)
            count, face, opportunity, realizable = totals.get(
                key, (0, Decimal("0"), Decimal("0"), Decimal("0"))
            )
            totals[key] = (count + 1, face + finding.annualized_cost_inr,
                           opportunity + finding.opportunity_savings_inr,
                           realizable + finding.realizable_12m_inr)
        lines = ["Potential savings (INR)", ""]
        labels = {"inactive": "inactive users", "terminated": "terminated accounts", "optimization": "optimization candidates"}
        for (application, category), (count, face, opportunity, realizable) in sorted(
            totals.items(), key=lambda item: (item[0][0].casefold(), item[0][1])
        ):
            lines.extend((
                application, f"{count} {labels[category]}",
                f"Annualized candidate cost: ₹{face:,.0f}/year",
                f"Contract-adjusted annual opportunity: ₹{opportunity:,.0f}/year",
                f"Estimated realizable in next 12 months: ₹{realizable:,.0f}", "",
            ))
        totals_face = sum((item.annualized_cost_inr for item in analysis.findings), Decimal("0"))
        totals_opportunity = sum((item.opportunity_savings_inr for item in analysis.findings), Decimal("0"))
        totals_realizable = sum((item.realizable_12m_inr for item in analysis.findings), Decimal("0"))
        lines.extend((
            f"Annualized candidate cost: ₹{totals_face:,.0f}/year",
            f"Contract-adjusted annual opportunity: ₹{totals_opportunity:,.0f}/year",
            f"Estimated realizable in next 12 months: ₹{totals_realizable:,.0f}",
            f"Active licenses analyzed: {analysis.active_license_count}",
        ))
    if analysis.currency_rate_date:
        lines.append(f"Currency rates effective: {analysis.currency_rate_date.isoformat()}")
    if analysis.issues:
        lines.extend(("", f"Data quality / contract issues: {len(analysis.issues)} (see --quality-csv)"))
    if reviews and analysis.findings:
        reviewed = sum(finding.finding_id in reviews for finding in analysis.findings)
        lines.append(f"Reviewed candidates: {reviewed}/{len(analysis.findings)}")
    return "\n".join(lines)


def format_findings_csv(
    analysis: Analysis,
    reviews: dict[str, dict[str, str]] | None = None,
) -> str:
    """Serialize candidate evidence, savings scenarios, and review state."""
    from io import StringIO

    reviews = reviews or {}
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "finding_id", "employee", "employee_id", "email", "application", "contract_id",
        "category", "reason", "evidence", "annualized_candidate_cost_inr",
        "contract_adjusted_annual_opportunity_inr", "estimated_realizable_12m_inr",
        "renewal_date", "commitment_end_date", "notice_days", "bundle_group",
        "savings_effective_date", "currency_rate_date", "review_status", "review_owner",
        "review_note", "reviewed_on",
    ])
    for finding in analysis.findings:
        review = reviews.get(finding.finding_id, {})
        writer.writerow([
            finding.finding_id, finding.employee, finding.employee_id, finding.email,
            finding.application, finding.contract_id, finding.category, finding.reason,
            finding.evidence, f"{finding.annualized_cost_inr:.2f}",
            f"{finding.opportunity_savings_inr:.2f}", f"{finding.realizable_12m_inr:.2f}",
            finding.renewal_date.isoformat() if finding.renewal_date else "",
            finding.commitment_end_date.isoformat() if finding.commitment_end_date else "",
            finding.notice_days, finding.bundle_group,
            finding.savings_effective_date.isoformat() if finding.savings_effective_date else "",
            analysis.currency_rate_date.isoformat() if analysis.currency_rate_date else "",
            review.get("status", "unreviewed"), review.get("owner", ""),
            review.get("note", ""), review.get("reviewed_on", ""),
        ])
    return output.getvalue()


def format_quality_csv(analysis: Analysis) -> str:
    """Serialize data-quality and contract constraint issues for remediation."""
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["source", "row", "identity", "issue", "details"])
    for issue in analysis.issues:
        writer.writerow([issue.source, issue.row, issue.identity, issue.issue, issue.details])
    return output.getvalue()


def load_reviews(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    rows = _rows(path, {"finding_id", "status", "owner", "note", "reviewed_on"})
    records: dict[str, dict[str, str]] = {}
    for line, row in enumerate(rows, start=2):
        finding_id, status = row.get("finding_id", ""), row.get("status", "")
        if not finding_id:
            raise InputError(f"{path}:{line}: finding_id cannot be blank")
        if status not in {"confirmed", "dismissed", "deferred"}:
            raise InputError(f"{path}:{line}: status must be confirmed, dismissed, or deferred")
        if finding_id in records:
            raise InputError(f"{path}:{line}: duplicate finding_id {finding_id!r}")
        if not row.get("reviewed_on", ""):
            raise InputError(f"{path}:{line}: reviewed_on cannot be blank")
        _parse_date(row.get("reviewed_on", ""), path, line, "reviewed_on")
        records[finding_id] = row
    return records


def update_review(
    path: Path,
    finding_id: str,
    status: str,
    *,
    owner: str = "",
    note: str = "",
    reviewed_on: date | None = None,
) -> None:
    """Create or update one review record in the local CSV review ledger."""
    if status not in {"confirmed", "dismissed", "deferred"}:
        raise InputError("review status must be confirmed, dismissed, or deferred")
    if not finding_id.strip():
        raise InputError("finding_id cannot be blank")
    records = load_reviews(path)
    records[finding_id] = {
        "finding_id": finding_id, "status": status, "owner": owner,
        "note": note, "reviewed_on": (reviewed_on or date.today()).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["finding_id", "status", "owner", "note", "reviewed_on"])
        writer.writeheader()
        for key in sorted(records):
            writer.writerow(records[key])
