from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from saas_sentry.contracts import record_contract_version
from saas_sentry.engine import InputError
from saas_sentry.renewals import (
    create_renewal_campaign,
    format_renewals,
    list_renewals,
    record_budget_approval,
    record_owner_decision,
    build_renewal_reminders,
    format_renewal_alerts,
)


class RenewalWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.contracts = self.root / "contracts.csv"
        self.ledger = self.root / "renewal-events.csv"
        record_contract_version(
            self.contracts, contract_id="slack-1", vendor="Slack", owner="owner@example.com",
            applications=["Slack"], effective_date=date(2026, 1, 1), renewal_date=date(2026, 12, 1),
            notice_days=90, annual_commitment_inr=Decimal("12000"), currency="INR",
            evidence_type="order_form", evidence_ref="evidence/order-form.pdf",
            recorded_by="procurement@example.com", recorded_on=date(2026, 1, 1),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create(self) -> int:
        return create_renewal_campaign(
            self.contracts, self.ledger, as_of=date(2026, 10, 1),
            horizon_days=60, created_by="finops@example.com",
        )

    def test_creates_overdue_campaign_and_completes_separate_approval(self) -> None:
        self.assertEqual(self.create(), 1)
        self.assertEqual(self.create(), 0)
        item, = list_renewals(self.ledger, as_of=date(2026, 10, 1))
        self.assertEqual(item.status, "overdue_owner_decision")
        self.assertEqual(item.notice_deadline, date(2026, 9, 2))
        record_owner_decision(
            self.ledger, "slack-1", 1, decision="renegotiate", decided_by="owner@example.com",
            evidence_ref="evidence/renewal-review.pdf", on=date(2026, 10, 2),
        )
        with self.assertRaisesRegex(InputError, "must differ"):
            record_budget_approval(
                self.ledger, "slack-1", 1, status="approved", approved_by="owner@example.com",
                evidence_ref="evidence/budget.pdf",
            )
        record_budget_approval(
            self.ledger, "slack-1", 1, status="approved", approved_by="finance@example.com",
            evidence_ref="evidence/budget.pdf", on=date(2026, 10, 3),
        )
        item, = list_renewals(self.ledger, as_of=date(2026, 10, 4))
        self.assertEqual((item.status, item.budget_approver), ("complete", "finance@example.com"))
        self.assertIn("evidence/budget.pdf", format_renewals([item]))

    def test_requires_assigned_owner_and_evidence_for_decision(self) -> None:
        self.create()
        with self.assertRaisesRegex(InputError, "only the assigned contract owner"):
            record_owner_decision(
                self.ledger, "slack-1", 1, decision="renew", decided_by="other@example.com",
                evidence_ref="evidence/decision.pdf",
            )
        with self.assertRaisesRegex(InputError, "evidence_ref are required"):
            record_owner_decision(
                self.ledger, "slack-1", 1, decision="renew", decided_by="owner@example.com",
                evidence_ref="",
            )

    def test_campaign_only_includes_contracts_whose_notice_deadline_is_in_horizon(self) -> None:
        record_contract_version(
            self.contracts, contract_id="slack-1", vendor="Slack", owner="owner@example.com",
            applications=["Slack"], effective_date=date(2026, 1, 1), renewal_date=date(2026, 9, 1),
            notice_days=30, annual_commitment_inr=Decimal("12000"), currency="INR",
            evidence_type="amendment", evidence_ref="evidence/amendment.pdf",
            recorded_by="procurement@example.com", recorded_on=date(2026, 1, 2),
        )
        self.assertEqual(create_renewal_campaign(
            self.contracts, self.ledger, as_of=date(2026, 10, 1), horizon_days=0,
            created_by="finops@example.com",
        ), 0)

    def test_deadline_reminder_is_owner_assigned_and_windowed(self) -> None:
        self.create()
        rows = build_renewal_reminders(
            self.ledger, as_of=date(2026, 9, 2), windows=(90, 30, 7),
            potential_savings={"slack-1": "4000.00"},
        )
        row, = rows
        self.assertEqual((row.owner, row.days_until_deadline, row.window_days),
                         ("owner@example.com", 0, 7))
        self.assertEqual(row.potential_savings_inr, "4000.00")
        self.assertIn("owner@example.com", format_renewal_alerts(rows))

    def test_alert_payload_is_stable_inside_a_window_for_delivery_deduplication(self) -> None:
        create_renewal_campaign(self.contracts, self.ledger, as_of=date(2026, 8, 1),
                                horizon_days=60, created_by="finops@example.com")
        first = build_renewal_reminders(self.ledger, as_of=date(2026, 8, 27))
        next_day = build_renewal_reminders(self.ledger, as_of=date(2026, 8, 28))
        self.assertEqual(format_renewal_alerts(first), format_renewal_alerts(next_day))


if __name__ == "__main__":
    unittest.main()
