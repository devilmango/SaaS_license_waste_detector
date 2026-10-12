from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

from saas_sentry.engine import InputError
from saas_sentry.reassignment import format_reassignments, plan_reassignments


class ReassignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hr = self.root / "hr.csv"
        self.usage = self.root / "usage.csv"
        self.billing = self.root / "billing.csv"
        self.findings = self.root / "findings.csv"
        self.demand = self.root / "demand.csv"
        self.write(self.hr, ["employee", "employee_id", "email", "status"], [
            ["Source", "E1", "source@example.com", "active"],
            ["Target", "E2", "target@example.com", "active"],
        ])
        self.write(self.usage, ["employee", "employee_id", "email", "application", "last_login"], [])
        self.write(self.billing, ["employee_id", "email", "application", "license_status", "annual_cost", "currency", "contract_id"], [
            ["E1", "source@example.com", "Slack", "active", "12000", "INR", "slack-annual"],
        ])
        self.write(self.findings, [
            "finding_id", "employee", "employee_id", "email", "application", "contract_id",
            "review_status", "annualized_candidate_cost_inr",
        ], [["F1", "Source", "E1", "source@example.com", "Slack", "slack-annual", "confirmed", "12000"]])
        self.write(self.demand, ["employee_id", "email", "application", "contract_id", "priority", "requested_on", "reason"], [
            ["E2", "target@example.com", "Slack", "slack-annual", "high", "2026-09-30", "New hire"],
        ])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, path: Path, fields: list[str], rows: list[list[str]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            writer.writerows(rows)

    def plan(self):
        return plan_reassignments(
            hr_path=self.hr, usage_path=self.usage, billing_path=self.billing,
            findings_path=self.findings, demand_path=self.demand,
        )

    def test_matches_only_confirmed_same_contract_seats_to_active_unlicensed_requests(self) -> None:
        rows = self.plan()
        proposed, = rows
        self.assertEqual((proposed.status, proposed.to_employee, proposed.priority), ("proposed", "Target", "high"))
        self.assertIn("no accounts were changed", format_reassignments(rows))

    def test_unconfirmed_finding_does_not_supply_a_seat(self) -> None:
        self.write(self.findings, [
            "finding_id", "employee", "employee_id", "email", "application", "contract_id",
            "review_status", "annualized_candidate_cost_inr",
        ], [["F1", "Source", "E1", "source@example.com", "Slack", "slack-annual", "unreviewed", "12000"]])
        row, = self.plan()
        self.assertEqual(row.status, "unmatched_demand")

    def test_requires_contract_identity_in_demand(self) -> None:
        self.write(self.demand, ["employee_id", "email", "application", "contract_id", "priority", "requested_on", "reason"], [
            ["E2", "target@example.com", "Slack", "", "high", "2026-09-30", "New hire"],
        ])
        with self.assertRaisesRegex(InputError, "application and contract_id are required"):
            self.plan()


if __name__ == "__main__":
    unittest.main()
