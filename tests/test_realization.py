from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from saas_sentry.actions import propose_action, transition_action
from saas_sentry.realization import build_realization_report, format_realization_report


class SavingsRealizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.findings = self.root / "findings.csv"
        with self.findings.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "finding_id", "review_status", "contract_adjusted_annual_opportunity_inr",
                "application", "department", "cost_center",
            ])
            writer.writeheader()
            writer.writerows([
                {"finding_id": "f-1", "review_status": "confirmed", "contract_adjusted_annual_opportunity_inr": "10000",
                 "application": "Slack", "department": "Engineering", "cost_center": "ENG"},
                {"finding_id": "f-2", "review_status": "confirmed", "contract_adjusted_annual_opportunity_inr": "5000",
                 "application": "Slack", "department": "Engineering", "cost_center": "ENG"},
                {"finding_id": "f-3", "review_status": "confirmed", "contract_adjusted_annual_opportunity_inr": "6000",
                 "application": "Notion", "department": "Product", "cost_center": "PROD"},
            ])
        self.actions = self.root / "actions.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def propose(self, finding_id: str) -> str:
        return propose_action(
            self.findings, self.actions, finding_id, owner="FinOps", proposed_by="analyst",
            proposed_on=date(2026, 10, 1),
        )

    def test_aggregates_realized_variance_pending_pipeline_and_month(self) -> None:
        reclaimed = self.propose("f-1")
        transition_action(self.actions, reclaimed, "approved", actor="manager")
        transition_action(self.actions, reclaimed, "reclaimed", actor="operator",
                          actual_annual_savings_inr=Decimal("8000"), occurred_on=date(2026, 11, 5))
        pending = self.propose("f-2")
        transition_action(self.actions, pending, "approved", actor="manager")
        rejected = self.propose("f-3")
        transition_action(self.actions, rejected, "rejected", actor="manager")

        rows = build_realization_report(self.actions, self.findings)
        overall = next(row for row in rows if row.scope_type == "overall")
        month = next(row for row in rows if row.scope_type == "completion_month")
        cost_center = next(row for row in rows if row.scope_type == "cost_center" and row.scope == "ENG")
        self.assertEqual(overall.action_count, 3)
        self.assertEqual((overall.reclaimed_count, overall.open_count, overall.rejected_count), (1, 1, 1))
        self.assertEqual(overall.estimated_reclaimed_annual_inr, Decimal("10000.00"))
        self.assertEqual(overall.actual_annual_inr, Decimal("8000.00"))
        self.assertEqual(overall.variance_inr, Decimal("-2000.00"))
        self.assertEqual(overall.realization_percent, Decimal("80.00"))
        self.assertEqual(overall.pending_estimated_annual_inr, Decimal("5000.00"))
        self.assertEqual(month.scope, "2026-11")
        self.assertEqual(cost_center.actual_annual_inr, Decimal("8000.00"))
        self.assertIn("realization_percent", format_realization_report(rows))


if __name__ == "__main__":
    unittest.main()
