from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from saas_sentry.actions import ActionError, list_actions, propose_action, transition_action


class ReclaimActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.findings = self.root / "findings.csv"
        with self.findings.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "finding_id", "review_status", "contract_adjusted_annual_opportunity_inr",
            ])
            writer.writeheader()
            writer.writerow({
                "finding_id": "abc123", "review_status": "confirmed",
                "contract_adjusted_annual_opportunity_inr": "12000.00",
            })
            writer.writerow({
                "finding_id": "def456", "review_status": "unreviewed",
                "contract_adjusted_annual_opportunity_inr": "8000.00",
            })
        self.ledger = self.root / "actions.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_requires_confirmed_finding_independent_approval_and_records_variance(self) -> None:
        with self.assertRaisesRegex(ActionError, "review_status=confirmed"):
            propose_action(self.findings, self.ledger, "def456", owner="FinOps", proposed_by="Alex")
        action_id = propose_action(
            self.findings, self.ledger, "abc123", owner="FinOps", proposed_by="Alex",
            proposed_on=date(2026, 10, 5), note="Approved for renewal window",
        )
        with self.assertRaisesRegex(ActionError, "different from the proposer"):
            transition_action(self.ledger, action_id, "approved", actor="Alex")
        with self.assertRaisesRegex(ActionError, "Cannot transition action from proposed to reclaimed"):
            transition_action(
                self.ledger, action_id, "reclaimed", actor="Pat",
                actual_annual_savings_inr=Decimal("11000"),
            )
        transition_action(self.ledger, action_id, "approved", actor="Pat", note="Finance approved")
        transition_action(
            self.ledger, action_id, "reclaimed", actor="Morgan",
            actual_annual_savings_inr=Decimal("10500"), occurred_on=date(2026, 11, 1),
        )
        record = list_actions(self.ledger)[0]
        self.assertEqual(record.status, "reclaimed")
        self.assertEqual(record.approver, "Pat")
        self.assertEqual(record.actual_annual_savings_inr, Decimal("10500"))
        self.assertEqual(record.variance_inr, Decimal("-1500.00"))
        with self.assertRaisesRegex(ActionError, "already has"):
            propose_action(self.findings, self.ledger, "abc123", owner="FinOps", proposed_by="Alex")


if __name__ == "__main__":
    unittest.main()
