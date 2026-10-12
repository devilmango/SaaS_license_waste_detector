from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from saas_sentry.engine import CurrencyConfig, InputError
from saas_sentry.procurement import format_procurement, normalize_procurement_csv
from saas_sentry.contracts import approve_contract_version, reconcile_contract_invoices, record_contract_version


class ProcurementImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "export.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, headers: list[str], row: list[str]) -> None:
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerow(row)

    def test_normalizes_coupa_export_and_preserves_renewal_metadata(self) -> None:
        self.write(
            ["Supplier", "Item", "Contract Number", "Invoice #", "Account", "Service Start Date",
             "Service End Date", "Line Total", "Currency", "Contract Owner", "Renewal Date",
             "Notice Period Days", "Attachment URL"],
            ["Slack Inc", "Slack", "slack-1", "INV-10", "Engineering", "2026-09-01T00:00:00Z",
             "2026-10-01T00:00:00Z", "1,250.50", "INR", "owner@example.com", "2027-01-01", "60", "quote.pdf"],
        )
        rows = normalize_procurement_csv(self.path, system="coupa")
        self.assertEqual(rows[0]["actual_billed_cost_inr"], "1250.50")
        self.assertEqual(rows[0]["renewal_date"], "2027-01-01")
        self.assertIn("contract_id", format_procurement(rows).splitlines()[0])

    def test_converts_invoice_currency_using_dated_config(self) -> None:
        self.write(
            ["vendor", "application", "contract_id", "invoice_id", "billing_period_start",
             "billing_period_end", "amount", "currency"],
            ["Vendor", "Zoom", "zoom-1", "INV-1", "2026-09-01", "2026-10-01", "10", "USD"],
        )
        rows = normalize_procurement_csv(self.path, currency=CurrencyConfig(
            "USD", date(2026, 10, 1), {"INR": Decimal("1"), "USD": Decimal("80")},
        ))
        self.assertEqual(rows[0]["actual_billed_cost_inr"], "800.00")

    def test_rejects_unknown_currency_or_reversed_invoice_period(self) -> None:
        self.write(
            ["vendor", "application", "contract_id", "invoice_id", "billing_period_start",
             "billing_period_end", "amount", "currency"],
            ["Vendor", "Zoom", "zoom-1", "INV-1", "2026-10-01", "2026-09-01", "10", "USD"],
        )
        with self.assertRaisesRegex(InputError, "end must be after start"):
            normalize_procurement_csv(self.path, currency=CurrencyConfig(rates_to_inr={"INR": Decimal("1"), "USD": Decimal("80")}))
        self.write(
            ["vendor", "application", "contract_id", "invoice_id", "billing_period_start",
             "billing_period_end", "amount", "currency"],
            ["Vendor", "Zoom", "zoom-1", "INV-1", "2026-09-01", "2026-10-01", "10", "USD"],
        )
        with self.assertRaisesRegex(InputError, "no INR exchange rate"):
            normalize_procurement_csv(self.path)

    def test_normalized_procurement_invoice_feeds_contract_reconciliation(self) -> None:
        self.write(
            ["vendor", "application", "contract_id", "invoice_id", "billing_period_start",
             "billing_period_end", "amount", "currency"],
            ["Slack Inc", "Slack", "slack-1", "INV-1", "2026-09-01", "2026-10-01", "1000", "INR"],
        )
        invoice_path = self.root_path("normalized.csv")
        invoice_path.write_text(format_procurement(normalize_procurement_csv(self.path)), encoding="utf-8")
        ledger = self.root_path("contracts.csv")
        record_contract_version(
            ledger, contract_id="slack-1", vendor="Slack Inc", owner="finops@example.com",
            applications=["Slack"], effective_date=date(2026, 1, 1), renewal_date=date(2027, 1, 1),
            notice_days=30, annual_commitment_inr=Decimal("12000"), currency="INR",
            evidence_type="order_form", evidence_ref="order-form.pdf", recorded_by="procurement@example.com",
        )
        approve_contract_version(ledger, "slack-1", approved_by="finance@example.com")
        row, = reconcile_contract_invoices(ledger, invoice_path, as_of=date(2026, 10, 1))
        self.assertEqual(row.invoice_status, "within_tolerance")

    def root_path(self, name: str) -> Path:
        return self.path.parent / name


if __name__ == "__main__":
    unittest.main()
