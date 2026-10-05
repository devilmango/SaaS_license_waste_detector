"""Portfolio reconciliation, discovery, and renewal forecasting reports."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .engine import InputData, InputError, _rows


@dataclass(frozen=True)
class ReconciliationRow:
    application: str
    cost_center: str
    period_start: date
    period_end: date
    active_seats: int
    expected_cost_inr: Decimal
    actual_cost_inr: Decimal
    variance_inr: Decimal
    variance_ratio: Decimal | None
    status: str


@dataclass(frozen=True)
class DiscoveryRow:
    application: str
    vendor: str
    category: str
    owner: str
    sources: str
    active_seats: int
    annualized_license_cost_inr: Decimal
    portfolio_status: str
    related_applications: str


@dataclass(frozen=True)
class ForecastRow:
    contract_id: str
    applications: str
    renewal_date: date | None
    active_seats: int
    seat_minimum: int
    projected_seats_before_reduction: int
    projected_seats_after_reduction: int
    current_annual_cost_inr: Decimal
    projected_annual_cost_inr: Decimal
    reduced_scenario_annual_cost_inr: Decimal
    price_increase_percent: Decimal
    seat_growth_percent: Decimal
    seat_reduction_percent: Decimal


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _decimal(value: str, path: Path, line: int, field: str, *, allow_negative: bool = False) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise InputError(f"{path}:{line}: {field} must be a number") from exc
    if not result.is_finite() or (result < 0 and not allow_negative):
        qualifier = "finite" if allow_negative else "finite and non-negative"
        raise InputError(f"{path}:{line}: {field} must be {qualifier}")
    return result


def _period_days(start: date, end: date) -> int:
    days = (end - start).days
    if days <= 0:
        raise InputError("billing period end must be after its start")
    return days


def reconcile_focus(
    path: Path,
    inputs: InputData,
    *,
    tolerance_percent: Decimal = Decimal("5"),
) -> tuple[ReconciliationRow, ...]:
    """Compare normalized FOCUS invoice spend with assigned-seat cost by cost center."""
    if not tolerance_percent.is_finite() or tolerance_percent < 0:
        raise InputError("variance tolerance must be finite and non-negative")
    rows = _rows(path, {
        "application", "cost_center", "billing_period_start", "billing_period_end",
        "actual_billed_cost_inr",
    })
    actual: dict[tuple[str, str, date, date], tuple[str, str, Decimal]] = {}
    for line, row in enumerate(rows, start=2):
        try:
            start = date.fromisoformat(row["billing_period_start"])
            end = date.fromisoformat(row["billing_period_end"])
        except ValueError as exc:
            raise InputError(f"{path}:{line}: billing period dates must use YYYY-MM-DD") from exc
        _period_days(start, end)
        app, center = row["application"].strip(), row["cost_center"].strip() or "Unassigned"
        if not app:
            raise InputError(f"{path}:{line}: application cannot be blank")
        key = (_normalized(app), _normalized(center), start, end)
        previous = actual.get(key)
        amount = _decimal(row["actual_billed_cost_inr"], path, line, "actual_billed_cost_inr", allow_negative=True)
        actual[key] = (app, center, amount + (previous[2] if previous else Decimal("0")))

    expected: dict[tuple[str, str, date, date], tuple[str, str, int, Decimal]] = {}
    # Derive expected period spend by scaling each active annual license cost to the invoice window.
    for license in inputs.licenses:
        if license.status.casefold() != "active":
            continue
        person = inputs.people.get(license.person_key or "")
        center = person.cost_center if person and person.cost_center else "Unassigned"
        key_base = (_normalized(license.application), _normalized(center))
        expected.setdefault((*key_base, date.min, date.min), (license.application, center, 0, Decimal("0")))
        key = (*key_base, date.min, date.min)
        app, display_center, seats, annual = expected[key]
        expected[key] = (app, display_center, seats + 1, annual + license.annual_cost_inr)

    combined: dict[tuple[str, str, date, date], ReconciliationRow] = {}
    keys = set(actual)
    actual_periods = {(key[2], key[3]) for key in actual}
    for app_center, (app, center, seats, annual) in expected.items():
        for start, end in actual_periods:
            keys.add((app_center[0], app_center[1], start, end))
    for key in sorted(keys):
        invoice = actual.get(key)
        model = expected.get((key[0], key[1], date.min, date.min))
        app = invoice[0] if invoice else model[0]
        center = invoice[1] if invoice else model[1]
        start, end = key[2], key[3]
        days = _period_days(start, end)
        seats, expected_cost = (model[2], model[3] * Decimal(days) / Decimal(365)) if model else (0, Decimal("0"))
        actual_cost = invoice[2] if invoice else Decimal("0")
        variance = actual_cost - expected_cost
        ratio = (variance / expected_cost * Decimal("100")) if expected_cost else None
        if invoice and not model:
            status = "unlicensed_spend"
        elif model and not invoice:
            status = "missing_invoice_spend"
        elif expected_cost == 0:
            status = "unlicensed_spend"
        elif abs(variance) > expected_cost * tolerance_percent / Decimal("100"):
            status = "variance"
        else:
            status = "matched"
        combined[key] = ReconciliationRow(
            app, center, start, end, seats, expected_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            actual_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            variance.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if ratio is not None else None,
            status,
        )
    return tuple(combined[key] for key in sorted(combined))


def format_reconciliation(rows: Iterable[ReconciliationRow]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "application", "cost_center", "billing_period_start", "billing_period_end", "active_seats",
        "expected_cost_inr", "actual_billed_cost_inr", "variance_inr", "variance_percent", "status",
    ])
    for row in rows:
        writer.writerow([
            row.application, row.cost_center, row.period_start.isoformat(), row.period_end.isoformat(),
            row.active_seats, f"{row.expected_cost_inr:.2f}", f"{row.actual_cost_inr:.2f}",
            f"{row.variance_inr:.2f}", f"{row.variance_ratio:.2f}" if row.variance_ratio is not None else "",
            row.status,
        ])
    return output.getvalue()


def discover_portfolio(path: Path, inputs: InputData) -> tuple[DiscoveryRow, ...]:
    """Compare an app inventory assembled from procurement/SSO/expense exports to assigned licenses."""
    rows = _rows(path, {"application", "vendor", "category", "source"})
    inventory: dict[str, dict[str, set[str] | str]] = {}
    for line, row in enumerate(rows, start=2):
        app = row["application"].strip()
        if not app:
            raise InputError(f"{path}:{line}: application cannot be blank")
        key = _normalized(app)
        record = inventory.setdefault(key, {
            "application": app, "vendor": set(), "category": set(), "owner": set(), "source": set(),
        })
        for field in ("vendor", "category", "owner", "source"):
            value = row.get(field, "").strip()
            if value:
                values = record[field]
                assert isinstance(values, set)
                values.add(value)

    licensed: dict[str, tuple[str, int, Decimal]] = {}
    for license in inputs.licenses:
        if license.status.casefold() != "active":
            continue
        key = _normalized(license.application)
        previous = licensed.get(key, (license.application, 0, Decimal("0")))
        licensed[key] = (previous[0], previous[1] + 1, previous[2] + license.annual_cost_inr)
    by_category: dict[str, list[str]] = {}
    for key, record in inventory.items():
        categories = record["category"]
        assert isinstance(categories, set)
        for category in categories:
            by_category.setdefault(_normalized(category), []).append(key)

    result: list[DiscoveryRow] = []
    for key, record in inventory.items():
        app = str(record["application"])
        categories = sorted(record["category"], key=str.casefold)  # type: ignore[arg-type]
        related_keys = sorted({other for category in categories for other in by_category[_normalized(category)] if other != key})
        related = [str(inventory[other]["application"]) for other in related_keys]
        license = licensed.get(key)
        status = "shadow_saas" if not license else "managed"
        if related:
            status += "+overlap_candidate"
        def joined(field: str) -> str:
            values = record[field]
            return "; ".join(sorted(values, key=str.casefold)) if isinstance(values, set) else ""
        result.append(DiscoveryRow(
            app, joined("vendor"), "; ".join(categories), joined("owner"), joined("source"),
            license[1] if license else 0,
            license[2].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if license else Decimal("0.00"),
            status, "; ".join(related),
        ))
    return tuple(sorted(result, key=lambda row: (row.portfolio_status, row.application.casefold())))


def format_discovery(rows: Iterable[DiscoveryRow]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "application", "vendor", "category", "owner", "sources", "active_seats",
        "annualized_license_cost_inr", "portfolio_status", "related_applications",
    ])
    for row in rows:
        writer.writerow([
            row.application, row.vendor, row.category, row.owner, row.sources, row.active_seats,
            f"{row.annualized_license_cost_inr:.2f}", row.portfolio_status, row.related_applications,
        ])
    return output.getvalue()


def forecast_renewals(
    inputs: InputData,
    *,
    price_increase_percent: Decimal = Decimal("0"),
    seat_growth_percent: Decimal = Decimal("0"),
    seat_reduction_percent: Decimal = Decimal("0"),
) -> tuple[ForecastRow, ...]:
    """Model renewal spend using seat growth, contract floors, tier prices, and price uplift."""
    assumptions = (price_increase_percent, seat_growth_percent, seat_reduction_percent)
    if any(not value.is_finite() or value < 0 for value in assumptions):
        raise InputError("forecast percentages must be finite and non-negative")
    if seat_reduction_percent > 100:
        raise InputError("seat reduction percent cannot exceed 100")
    contracts: dict[str, list] = {}
    for license in inputs.licenses:
        if license.status.casefold() == "active":
            contracts.setdefault(license.contract_id.casefold(), []).append(license)
    result: list[ForecastRow] = []
    uplift = Decimal("1") + price_increase_percent / Decimal("100")
    for key, licenses in contracts.items():
        first = licenses[0]
        active = len(licenses)
        minimum = max(license.seat_minimum for license in licenses)
        growth_seats = int((Decimal(active) * (Decimal("1") + seat_growth_percent / Decimal("100"))).to_integral_value(rounding=ROUND_CEILING))
        reduced_seats = int((Decimal(growth_seats) * (Decimal("1") - seat_reduction_percent / Decimal("100"))).to_integral_value(rounding=ROUND_CEILING))
        forecast_seats = max(minimum, growth_seats)
        reduced_seats = max(minimum, reduced_seats)
        current = sum((license.annual_cost_inr for license in licenses), Decimal("0"))
        tiers = inputs.price_tiers.get(key, ())
        if tiers:
            def tier_cost(seats: int) -> Decimal:
                tier = next((item for item in tiers if seats >= item.min_seats and (item.max_seats is None or seats <= item.max_seats)), None)
                if tier is None:
                    raise InputError(
                        f"No configured contract price tier covers {seats} seats for {first.contract_id!r}"
                    )
                return Decimal(seats) * tier.annual_cost_per_seat_inr
            projected = tier_cost(forecast_seats) * uplift
            reduced = tier_cost(reduced_seats) * uplift
        else:
            unit_cost = current / Decimal(active) if active else Decimal("0")
            projected = unit_cost * forecast_seats * uplift
            reduced = unit_cost * reduced_seats * uplift
        result.append(ForecastRow(
            first.contract_id,
            "; ".join(sorted({item.application for item in licenses}, key=str.casefold)),
            first.renewal_date or first.commitment_end_date, active, minimum, forecast_seats, reduced_seats,
            current.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            projected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            reduced.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            price_increase_percent, seat_growth_percent, seat_reduction_percent,
        ))
    return tuple(sorted(result, key=lambda row: (row.renewal_date or date.max, row.contract_id.casefold())))


def format_forecast(rows: Iterable[ForecastRow]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "contract_id", "applications", "renewal_date", "active_seats", "seat_minimum",
        "projected_seats_before_reduction", "projected_seats_after_reduction", "current_annual_cost_inr",
        "projected_annual_cost_inr", "reduced_scenario_annual_cost_inr", "price_increase_percent",
        "seat_growth_percent", "seat_reduction_percent",
    ])
    for row in rows:
        writer.writerow([
            row.contract_id, row.applications, row.renewal_date.isoformat() if row.renewal_date else "",
            row.active_seats, row.seat_minimum, row.projected_seats_before_reduction,
            row.projected_seats_after_reduction, f"{row.current_annual_cost_inr:.2f}",
            f"{row.projected_annual_cost_inr:.2f}", f"{row.reduced_scenario_annual_cost_inr:.2f}",
            f"{row.price_increase_percent:.2f}", f"{row.seat_growth_percent:.2f}",
            f"{row.seat_reduction_percent:.2f}",
        ])
    return output.getvalue()
