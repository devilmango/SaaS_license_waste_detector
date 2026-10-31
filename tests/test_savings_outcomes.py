from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from saas_sentry.actions import propose_action, transition_action
from saas_sentry.contracts import approve_contract_version, record_contract_version
from saas_sentry.renewals import create_renewal_campaign, record_owner_decision
from saas_sentry.savings_outcomes import build_savings_outcomes, format_savings_outcomes


class SavingsOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.findings = self.root / "findings.csv"
        with self.findings.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "finding_id", "review_status", "contract_adjusted_annual_opportunity_inr",
                "application", "contract_id", "cost_center", "review_owner",
            ])
            writer.writeheader()
            writer.writerow({"finding_id": "f-1", "review_status": "confirmed",
                             "contract_adjusted_annual_opportunity_inr": "12000", "application": "Chat",
                             "contract_id": "chat-contract", "cost_center": "ENG", "review_owner": "Owner"})
        self.actions = self.root / "actions.csv"
        self.baseline = self.root / "baseline.csv"
        self.current = self.root / "current.csv"
        self.write_invoice(self.baseline, "2026-08-01", "2026-09-01", "30000")
        self.write_invoice(self.current, "2026-09-01", "2026-10-01", "20000")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def write_invoice(path: Path, start: str, end: str, amount: str) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "application", "cost_center", "billing_period_start", "billing_period_end", "actual_billed_cost_inr",
            ])
            writer.writeheader()
            writer.writerow({"application": "Chat", "cost_center": "ENG", "billing_period_start": start,
                             "billing_period_end": end, "actual_billed_cost_inr": amount})

    def completed_action(self) -> str:
        action = propose_action(self.findings, self.actions, "f-1", owner="Owner", proposed_by="analyst",
                                proposed_on=date(2026, 8, 1))
        transition_action(self.actions, action, "approved", actor="manager")
        transition_action(self.actions, action, "reclaimed", actor="operator",
                          actual_annual_savings_inr=Decimal("11000"), occurred_on=date(2026, 9, 5))
        return action

    def test_reconciles_invoice_savings_by_application_owner_and_period(self) -> None:
        self.completed_action()
        row, = build_savings_outcomes(self.actions, self.findings, self.baseline, self.current)
        self.assertEqual((row.application, row.owner, row.period), ("Chat", "Owner", "2026-09-01/2026-10-01"))
        self.assertEqual(row.estimated_annual_savings_inr, Decimal("12000.00"))
        self.assertEqual(row.realized_annual_savings_inr, Decimal("109892.47"))
        self.assertEqual(row.renewal_status, "not_provided")
        self.assertIn("realized_annual_savings_inr", format_savings_outcomes([row]))

    def test_matches_approved_renewal_change_and_owner_decision(self) -> None:
        self.completed_action()
        contracts = self.root / "contracts.csv"
        renewal = self.root / "renewals.csv"
        for effective, renewal_date, amount in ((date(2026, 1, 1), date(2026, 9, 1), "30000"),
                                                 (date(2026, 9, 1), date(2026, 9, 15), "24000")):
            version = record_contract_version(
                contracts, contract_id="chat-contract", vendor="Chat Inc", owner="Owner",
                applications=["Chat"], effective_date=effective, renewal_date=renewal_date,
                notice_days=30, annual_commitment_inr=Decimal(amount), currency="INR",
                evidence_type="order_form", evidence_ref=f"evidence/{effective}.pdf",
                recorded_by="procurement", recorded_on=effective,
            )
            approve_contract_version(contracts, "chat-contract", approved_by="finance", version=version,
                                     approved_on=effective + timedelta(days=1))
        create_renewal_campaign(contracts, renewal, as_of=date(2026, 8, 1), horizon_days=60,
                                created_by="finops")
        record_owner_decision(renewal, "chat-contract", 2, decision="renegotiate", decided_by="Owner",
                              evidence_ref="approval/renewal", on=date(2026, 8, 10))
        row, = build_savings_outcomes(self.actions, self.findings, self.baseline, self.current,
                                      contracts_path=contracts, renewals_path=renewal)
        self.assertEqual(row.renewal_decision, "renegotiate")
        self.assertEqual(row.renewal_status, "pending_budget_approval")
        self.assertEqual(row.renewal_commitment_change_inr, Decimal("6000.00"))


if __name__ == "__main__":
    unittest.main()
