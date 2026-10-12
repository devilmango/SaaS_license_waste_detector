from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from saas_sentry.contracts import (
    approve_contract_version,
    list_contract_versions,
    reconcile_contract_invoices,
    record_contract_version,
)
from saas_sentry.engine import InputError


class ContractEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = self.root / "contract-events.csv"
        self.invoice = self.root / "invoice.csv"
        with self.invoice.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["application", "billing_period_start", "billing_period_end", "actual_billed_cost_inr", "contract_id"])
            writer.writerow(["Slack", "2026-09-01", "2026-10-01", "1000", "slack-enterprise"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def record(self, amount: str = "12000") -> int:
        return record_contract_version(
            self.ledger, contract_id="slack-enterprise", vendor="Slack Inc", owner="FinOps",
            applications=["Slack"], effective_date=date(2026, 1, 1), renewal_date=date(2026, 11, 15),
            notice_days=30, annual_commitment_inr=Decimal(amount), currency="USD",
            evidence_type="order_form", evidence_ref="evidence/slack-order-form.pdf",
            recorded_by="procurement@example.com", recorded_on=date(2026, 10, 1),
        )

    def test_separate_approval_and_invoice_reconciliation(self) -> None:
        self.assertEqual(self.record(), 1)
        row, = list_contract_versions(self.ledger)
        self.assertEqual(row.event, "pending")
        self.assertEqual(row.evidence_ref, "evidence/slack-order-form.pdf")
        pending, = reconcile_contract_invoices(
            self.ledger, self.invoice, as_of=date(2026, 10, 1),
        )
        self.assertEqual(pending.invoice_status, "pending_approval")
        with self.assertRaisesRegex(InputError, "differ from the person"):
            approve_contract_version(self.ledger, "slack-enterprise", approved_by="procurement@example.com")
        approve_contract_version(
            self.ledger, "slack-enterprise", approved_by="finance@example.com", note="Terms reviewed",
            approved_on=date(2026, 10, 2),
        )
        approved, = list_contract_versions(self.ledger)
        self.assertEqual((approved.event, approved.approved_by), ("approved", "finance@example.com"))
        report, = reconcile_contract_invoices(
            self.ledger, self.invoice, as_of=date(2026, 10, 20), tolerance_percent=Decimal("5"),
        )
        self.assertEqual(report.invoice_status, "within_tolerance")
        self.assertEqual(report.renewal_status, "notice_due")
        self.assertEqual(report.evidence_ref, "evidence/slack-order-form.pdf")

    def test_versions_are_sequential_and_only_latest_can_be_approved(self) -> None:
        self.record()
        self.assertEqual(self.record("15000"), 2)
        with self.assertRaisesRegex(InputError, "latest contract version"):
            approve_contract_version(
                self.ledger, "slack-enterprise", version=1, approved_by="finance@example.com",
            )
        approve_contract_version(
            self.ledger, "slack-enterprise", version=2, approved_by="finance@example.com",
        )
        versions = list_contract_versions(self.ledger)
        self.assertEqual([row.event for row in versions], ["pending", "approved"])

    def test_invoice_periods_must_be_consistent(self) -> None:
        self.record()
        with self.invoice.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(["Notion", "2026-10-01", "2026-11-01", "200"])
        with self.assertRaisesRegex(InputError, "one consistent billing period"):
            reconcile_contract_invoices(self.ledger, self.invoice)

    def test_focus_contract_tags_separate_spend_for_shared_application_names(self) -> None:
        self.record()
        record_contract_version(
            self.ledger, contract_id="slack-enterprise-addon", vendor="Slack Inc", owner="FinOps",
            applications=["Slack"], effective_date=date(2026, 1, 1), renewal_date=date(2027, 1, 1),
            notice_days=30, annual_commitment_inr=Decimal("6000"), currency="INR",
            evidence_type="amendment", evidence_ref="evidence/slack-addon.pdf", recorded_by="procurement",
            recorded_on=date(2026, 10, 1),
        )
        approve_contract_version(self.ledger, "slack-enterprise", approved_by="finance")
        approve_contract_version(self.ledger, "slack-enterprise-addon", approved_by="finance")
        with self.invoice.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(["Slack", "2026-09-01", "2026-10-01", "500", "slack-enterprise-addon"])
        checks = {row.contract_id: row for row in reconcile_contract_invoices(self.ledger, self.invoice)}
        self.assertEqual(checks["slack-enterprise"].actual_invoice_cost_inr, Decimal("1000.00"))
        self.assertEqual(checks["slack-enterprise-addon"].actual_invoice_cost_inr, Decimal("500.00"))


if __name__ == "__main__":
    unittest.main()
