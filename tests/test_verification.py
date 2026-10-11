from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from saas_sentry.actions import propose_action, transition_action
from saas_sentry.engine import InputError
from saas_sentry.verification import verify_invoice_savings


class InvoiceVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.findings = self.root / "findings.csv"
        with self.findings.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "finding_id", "review_status", "contract_adjusted_annual_opportunity_inr",
                "application", "cost_center",
            ])
            writer.writeheader()
            writer.writerow({
                "finding_id": "f-1", "review_status": "confirmed",
                "contract_adjusted_annual_opportunity_inr": "100000",
                "application": "Slack", "cost_center": "ENG-100",
            })
        self.actions = self.root / "actions.csv"
        action_id = propose_action(
            self.findings, self.actions, "f-1", owner="FinOps", proposed_by="analyst",
            proposed_on=date(2026, 10, 2),
        )
        transition_action(self.actions, action_id, "approved", actor="manager")
        transition_action(
            self.actions, action_id, "reclaimed", actor="operator",
            actual_annual_savings_inr=Decimal("100000"), occurred_on=date(2026, 10, 5),
        )
        self.baseline = self.root / "baseline.csv"
        self.current = self.root / "current.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_invoice(self, path: Path, *, period: tuple[str, str], amount: str) -> Path:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "application", "cost_center", "billing_period_start", "billing_period_end",
                "actual_billed_cost_inr",
            ])
            writer.writeheader()
            writer.writerow({
                "application": "Slack", "cost_center": "ENG-100",
                "billing_period_start": period[0], "billing_period_end": period[1],
                "actual_billed_cost_inr": amount,
            })
        return path

    def verify(self):
        return verify_invoice_savings(self.actions, self.findings, self.baseline, self.current)

    def test_verifies_annualized_invoice_reduction_for_reclaimed_action(self) -> None:
        self.write_invoice(self.baseline, period=("2026-08-01", "2026-09-01"), amount="30000")
        self.write_invoice(self.current, period=("2026-09-01", "2026-10-01"), amount="20000")

        row, = self.verify()

        self.assertEqual(row.application, "Slack")
        self.assertEqual(row.reclaimed_actions, 1)
        self.assertEqual(row.observed_annualized_reduction_inr, Decimal("109892.47"))
        self.assertEqual(row.status, "verified")
        self.assertEqual(row.current_billed_inr, Decimal("20000.00"))

    def test_flags_increased_spend(self) -> None:
        self.write_invoice(self.baseline, period=("2026-08-01", "2026-09-01"), amount="10000")
        self.write_invoice(self.current, period=("2026-09-01", "2026-10-01"), amount="12000")

        row, = self.verify()

        self.assertEqual(row.status, "spend_increased")
        self.assertLess(row.observed_annualized_reduction_inr, Decimal("0"))

    def test_rejects_overlapping_periods(self) -> None:
        self.write_invoice(self.baseline, period=("2026-08-01", "2026-09-15"), amount="30000")
        self.write_invoice(self.current, period=("2026-09-01", "2026-10-01"), amount="20000")

        with self.assertRaisesRegex(InputError, "must end on or before"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
