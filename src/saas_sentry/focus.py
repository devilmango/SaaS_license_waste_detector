"""FOCUS billing CSV normalization for cost-center showback."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Iterable

from .engine import CurrencyConfig, InputError, _rows


@dataclass(frozen=True)
class FocusSpend:
    provider: str
    application: str
    cost_center: str
    billing_period_start: date
    billing_period_end: date
    billing_currency: str
    billed_cost_inr: Decimal


def _focus_date(value: str, path: Path, line: int, field: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise InputError(f"{path}:{line}: invalid FOCUS {field} {value!r}") from exc


def load_focus_spend(
    path: Path,
    *,
    currency: CurrencyConfig,
    cost_center_tag: str = "cost_center",
) -> tuple[FocusSpend, ...]:
    """Aggregate FOCUS charges by provider, service, cost center, period, and currency.

    This intentionally produces invoice-spend data, not per-user license assignments;
    FOCUS records do not guarantee a user identity for each charge.
    """
    required = {
        "ServiceName", "BilledCost", "BillingCurrency", "BillingPeriodStart", "BillingPeriodEnd",
    }
    rows = _rows(path, required)
    aggregated: dict[tuple[str, str, str, date, date, str], Decimal] = {}
    for line, row in enumerate(rows, start=2):
        service = row.get("ServiceName", "").strip()
        if not service:
            raise InputError(f"{path}:{line}: FOCUS ServiceName cannot be blank")
        billing_currency = row.get("BillingCurrency", "").strip().upper()
        if not billing_currency:
            raise InputError(f"{path}:{line}: FOCUS BillingCurrency cannot be blank")
        currency.convert(Decimal("0"), billing_currency, path, line)
        try:
            billed_cost = Decimal(row.get("BilledCost", ""))
        except InvalidOperation as exc:
            raise InputError(f"{path}:{line}: FOCUS BilledCost must be a number") from exc
        if not billed_cost.is_finite():
            raise InputError(f"{path}:{line}: FOCUS BilledCost must be finite")
        try:
            tags_value = json.loads(row.get("Tags", "") or "{}")
        except json.JSONDecodeError as exc:
            raise InputError(f"{path}:{line}: FOCUS Tags must contain a JSON object") from exc
        if not isinstance(tags_value, dict):
            raise InputError(f"{path}:{line}: FOCUS Tags must contain a JSON object")
        tag_value = tags_value.get(cost_center_tag, "")
        if isinstance(tag_value, (dict, list)):
            raise InputError(f"{path}:{line}: FOCUS tag {cost_center_tag!r} must be a scalar value")
        cost_center = str(tag_value).strip() if tag_value is not None else ""
        cost_center = cost_center or "Unassigned"
        period_start = _focus_date(row.get("BillingPeriodStart", ""), path, line, "BillingPeriodStart")
        period_end = _focus_date(row.get("BillingPeriodEnd", ""), path, line, "BillingPeriodEnd")
        if period_end <= period_start:
            raise InputError(f"{path}:{line}: FOCUS BillingPeriodEnd must be after BillingPeriodStart")
        provider = row.get("ProviderName", "").strip() or "Unknown provider"
        key = (provider, service, cost_center, period_start, period_end, billing_currency)
        aggregated[key] = aggregated.get(key, Decimal("0")) + billed_cost
    return tuple(
            FocusSpend(
            provider, service, cost_center, start, end, source_currency,
            currency.convert(amount, source_currency, path, 2),
        )
        for (provider, service, cost_center, start, end, source_currency), amount
        in sorted(aggregated.items(), key=lambda item: tuple(str(part).casefold() for part in item[0]))
    )


def format_focus_spend(rows: Iterable[FocusSpend], rate_date: date | None) -> str:
    """Write normalized, aggregated FOCUS actual-spend rows as UTF-8-compatible CSV text."""
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "provider", "application", "cost_center", "billing_period_start", "billing_period_end",
        "billing_currency", "actual_billed_cost_inr", "currency_rate_date",
    ])
    for row in rows:
        writer.writerow([
            row.provider, row.application, row.cost_center,
            row.billing_period_start.isoformat(), row.billing_period_end.isoformat(),
            row.billing_currency, f"{row.billed_cost_inr:.2f}",
            rate_date.isoformat() if rate_date else "",
        ])
    return output.getvalue()
