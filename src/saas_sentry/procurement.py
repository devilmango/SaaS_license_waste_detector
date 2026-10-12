"""Normalize procurement and accounts-payable CSV exports for spend checks."""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re
from typing import Iterable

from .engine import CurrencyConfig, InputError


OUTPUT_FIELDS = (
    "vendor", "application", "contract_id", "invoice_id", "cost_center",
    "billing_period_start", "billing_period_end", "actual_billed_cost_inr",
    "source_currency", "owner", "renewal_date", "notice_days", "evidence_ref",
)

_ALIASES = {
    "vendor": ("vendor", "supplier", "supplier name", "vendor name", "payee", "name"),
    "application": ("application", "service", "service name", "item", "item description", "memo", "description"),
    "contract_id": ("contract_id", "contract id", "contract number", "contract #", "agreement id", "agreement number"),
    "invoice_id": ("invoice_id", "invoice id", "invoice number", "invoice #", "invoice no", "document number", "tranid"),
    "cost_center": ("cost_center", "cost center", "department", "account", "account name", "cost object"),
    "billing_period_start": ("billing_period_start", "period start", "service start date", "start date", "service period start"),
    "billing_period_end": ("billing_period_end", "period end", "service end date", "end date", "service period end"),
    "amount": ("amount", "line total", "total", "net amount", "billed cost", "amount (transaction currency)", "line amount"),
    "currency": ("currency", "billing currency", "transaction currency", "currency code"),
    "owner": ("owner", "contract owner", "requester", "business owner"),
    "renewal_date": ("renewal_date", "renewal date", "renewal", "expiration date", "expiry date"),
    "notice_days": ("notice_days", "notice days", "notice period days", "termination notice days"),
    "evidence_ref": ("evidence_ref", "evidence", "attachment url", "document url", "source document"),
}

_SYSTEM_FIELDS = {
    "canonical": {field: aliases[0] for field, aliases in _ALIASES.items()},
    # These are common export headings. Procurement administrators can export
    # custom headings; matching remains alias based and fails on missing fields.
    "coupa": {
        "vendor": "Supplier", "application": "Item", "contract_id": "Contract Number",
        "invoice_id": "Invoice #", "cost_center": "Account", "billing_period_start": "Service Start Date",
        "billing_period_end": "Service End Date", "amount": "Line Total", "currency": "Currency",
        "owner": "Contract Owner", "renewal_date": "Renewal Date", "notice_days": "Notice Period Days",
        "evidence_ref": "Attachment URL",
    },
    "netsuite": {
        "vendor": "Vendor", "application": "Memo", "contract_id": "Contract ID",
        "invoice_id": "TranID", "cost_center": "Department", "billing_period_start": "Start Date",
        "billing_period_end": "End Date", "amount": "Amount", "currency": "Currency",
        "owner": "Contract Owner", "renewal_date": "Renewal Date", "notice_days": "Notice Days",
        "evidence_ref": "Document URL",
    },
    "ariba": {
        "vendor": "Supplier Name", "application": "Description", "contract_id": "Contract Number",
        "invoice_id": "Invoice Number", "cost_center": "Cost Center", "billing_period_start": "Service Period Start",
        "billing_period_end": "Service Period End", "amount": "Net Amount", "currency": "Currency Code",
        "owner": "Business Owner", "renewal_date": "Expiration Date", "notice_days": "Termination Notice Days",
        "evidence_ref": "Source Document",
    },
}


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _date(value: str, path: Path, line: int, field: str, *, required: bool) -> str:
    if not value.strip():
        if required:
            raise InputError(f"{path}:{line}: {field} cannot be blank")
        return ""
    raw = value.strip()
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw).isoformat()
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except ValueError as exc:
        raise InputError(f"{path}:{line}: {field} must be an ISO date or timestamp") from exc


def normalize_procurement_csv(
    input_path: Path,
    *,
    system: str = "canonical",
    currency: CurrencyConfig | None = None,
) -> tuple[dict[str, str], ...]:
    """Convert common Coupa, NetSuite, Ariba, or canonical exports to one schema."""
    system = system.casefold()
    if system not in _SYSTEM_FIELDS:
        raise InputError(f"unsupported procurement system {system!r}")
    currency = currency or CurrencyConfig(rates_to_inr={"INR": Decimal("1")})
    try:
        with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []
            by_key = {_header_key(header): header for header in headers if header}
            if not headers:
                raise InputError(f"{input_path}: CSV requires a header row")
            selected: dict[str, str | None] = {}
            for field, preferred in _SYSTEM_FIELDS[system].items():
                choices = (preferred, *_ALIASES[field])
                selected[field] = next((by_key[_header_key(candidate)] for candidate in choices
                                        if _header_key(candidate) in by_key), None)
            required = {"vendor", "application", "contract_id", "invoice_id", "billing_period_start",
                        "billing_period_end", "amount", "currency"}
            missing = sorted(field for field in required if not selected[field])
            if missing:
                raise InputError(f"{input_path}: missing source columns for {', '.join(missing)}")
            normalized: list[dict[str, str]] = []
            for line, raw in enumerate(reader, start=2):
                value = {field: (raw.get(column) or "").strip() if column else ""
                         for field, column in selected.items()}
                for field in ("vendor", "application", "contract_id", "invoice_id", "currency"):
                    if not value[field]:
                        raise InputError(f"{input_path}:{line}: {field} cannot be blank")
                if not value["application"]:
                    raise InputError(f"{input_path}:{line}: application cannot be blank")
                try:
                    amount = Decimal(value["amount"].replace(",", ""))
                except InvalidOperation as exc:
                    raise InputError(f"{input_path}:{line}: amount must be numeric") from exc
                if not amount.is_finite():
                    raise InputError(f"{input_path}:{line}: amount must be finite")
                start = _date(value["billing_period_start"], input_path, line, "billing_period_start", required=True)
                end = _date(value["billing_period_end"], input_path, line, "billing_period_end", required=True)
                if date.fromisoformat(end) <= date.fromisoformat(start):
                    raise InputError(f"{input_path}:{line}: billing period end must be after start")
                code = value["currency"].upper()
                amount_inr = currency.convert(amount, code, input_path, line)
                notice = value["notice_days"]
                if notice:
                    try:
                        notice_days = int(notice)
                    except ValueError as exc:
                        raise InputError(f"{input_path}:{line}: notice_days must be a whole number") from exc
                    if notice_days < 0:
                        raise InputError(f"{input_path}:{line}: notice_days cannot be negative")
                    notice = str(notice_days)
                renewal = _date(value["renewal_date"], input_path, line, "renewal_date", required=False)
                normalized.append({
                    "vendor": value["vendor"], "application": value["application"],
                    "contract_id": value["contract_id"], "invoice_id": value["invoice_id"],
                    "cost_center": value["cost_center"], "billing_period_start": start,
                    "billing_period_end": end,
                    "actual_billed_cost_inr": f"{amount_inr.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}",
                    "source_currency": code, "owner": value["owner"], "renewal_date": renewal,
                    "notice_days": notice, "evidence_ref": value["evidence_ref"],
                })
            return tuple(normalized)
    except OSError as exc:
        raise InputError(f"Cannot read procurement export {input_path}: {exc}") from exc


def format_procurement(rows: Iterable[dict[str, str]]) -> str:
    from io import StringIO
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=OUTPUT_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()
