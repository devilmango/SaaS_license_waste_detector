from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest

from saas_sentry.access_reviews import create_campaign, decide_campaign_item, list_campaign_items
from saas_sentry.actions import ActionError, list_actions, propose_action, transition_action
from saas_sentry.engine import load_reviews, update_review
from saas_sentry.policy import approve_exception, list_exceptions, request_exception
from saas_sentry.reassignment_workflow import list_states, stage_plan, transition
from saas_sentry.storage import backup_database, migrate_csv_ledger, restore_database


class SQLiteLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "saas-sentry.sqlite"
        self.findings = self.root / "findings.csv"
        with self.findings.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "finding_id", "application", "review_status", "review_owner", "email",
                "contract_adjusted_annual_opportunity_inr",
            ])
            writer.writeheader()
            writer.writerow({
                "finding_id": "f-1", "application": "Slack", "review_status": "confirmed",
                "review_owner": "owner@example.com", "email": "user@example.com",
                "contract_adjusted_annual_opportunity_inr": "12000",
            })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_action_and_campaign_ledgers_share_a_transactional_database(self) -> None:
        action_id = propose_action(
            self.findings, self.database, "f-1", owner="FinOps",
            proposed_by="analyst@example.com", proposed_on=date(2026, 10, 1),
        )
        transition_action(self.database, action_id, "approved", actor="manager@example.com")
        action, = list_actions(self.database)
        self.assertEqual(action.status, "approved")

        campaign_findings = self.root / "campaign-findings.csv"
        with campaign_findings.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "finding_id", "application", "review_status", "review_owner",
            ])
            writer.writeheader()
            writer.writerow({
                "finding_id": "f-2", "application": "Notion", "review_status": "unreviewed",
                "review_owner": "owner@example.com",
            })
        campaign, count = create_campaign(
            campaign_findings, self.database, due_on=date(2026, 10, 20),
            created_by="admin@example.com", campaign_id="october",
        )
        self.assertEqual((campaign, count), ("october", 1))
        decide_campaign_item(
            self.database, self.root / "reviews.csv", campaign, "f-2", "confirmed",
            reviewed_by="owner@example.com", decided_on=date(2026, 10, 12),
        )
        item, = list_campaign_items(self.database, campaign_id=campaign)
        self.assertEqual(item.status, "decided")

        with sqlite3.connect(self.database) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )}
        self.assertTrue({"action_events", "access_review_events"}.issubset(tables))

    def test_invalid_transition_is_rejected_without_corrupting_database(self) -> None:
        action_id = propose_action(
            self.findings, self.database, "f-1", owner="FinOps",
            proposed_by="analyst", proposed_on=date(2026, 10, 1),
        )
        transition_action(self.database, action_id, "approved", actor="manager")
        with self.assertRaisesRegex(ActionError, "Cannot transition"):
            transition_action(self.database, action_id, "approved", actor="reviewer")
        action, = list_actions(self.database)
        self.assertEqual(action.status, "approved")

    def test_migrates_existing_csv_action_history_and_rejects_duplicate_import(self) -> None:
        csv_ledger = self.root / "legacy-actions.csv"
        action_id = propose_action(
            self.findings, csv_ledger, "f-1", owner="FinOps",
            proposed_by="analyst", proposed_on=date(2026, 10, 1),
        )
        transition_action(csv_ledger, action_id, "approved", actor="manager")
        self.assertEqual(migrate_csv_ledger(csv_ledger, self.database, "actions"), 2)
        self.assertEqual(list_actions(self.database)[0].status, "approved")
        with self.assertRaisesRegex(ValueError, "already contains events"):
            migrate_csv_ledger(csv_ledger, self.database, "actions")

    def test_reviews_exceptions_and_reassignments_share_persistent_sqlite_storage(self) -> None:
        update_review(self.database, "f-1", "confirmed", owner="analyst", reviewed_on=date(2026, 10, 1))
        update_review(self.database, "f-1", "deferred", owner="analyst", reviewed_on=date(2026, 10, 2))
        self.assertEqual(load_reviews(self.database)["f-1"]["status"], "deferred")

        exception_id = request_exception(
            self.database, employee_id="e-1", email="user@example.com", application="Slack",
            reason="On-call account", requested_by="requester", expires_on=date(2026, 12, 1),
            evidence_ref="ticket-1", on=date(2026, 10, 1),
        )
        approve_exception(
            self.database, exception_id, approved_by="manager", evidence_ref="approval-1",
            on=date(2026, 10, 2),
        )
        self.assertEqual(list_exceptions(self.database, as_of=date(2026, 10, 3))[0].status, "approved")

        plan = self.root / "reassignment.csv"
        with plan.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "finding_id", "application", "contract_id", "from_email", "to_email", "status",
            ])
            writer.writeheader()
            writer.writerow({
                "finding_id": "f-transfer", "application": "Slack", "contract_id": "c-1",
                "from_email": "old@example.com", "to_email": "new@example.com", "status": "proposed",
            })
        self.assertEqual(stage_plan(plan, self.database, actor="analyst", on=date(2026, 10, 1)), 1)
        transition(self.database, "f-transfer", "approved", actor="manager", evidence_ref="approval")
        self.assertEqual(list_states(self.database)[0]["status"], "approved")

        with sqlite3.connect(self.database) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )}
        self.assertTrue({"review_events", "exception_events", "reassignment_events"}.issubset(tables))

    def test_sqlite_backup_and_restore_preserve_ledgers(self) -> None:
        update_review(self.database, "f-1", "confirmed", owner="analyst", reviewed_on=date(2026, 10, 1))
        backup = self.root / "backup.sqlite"
        restored = self.root / "restored.sqlite"
        backup_database(self.database, backup)
        restore_database(backup, restored)
        self.assertEqual(load_reviews(restored)["f-1"]["status"], "confirmed")

    def test_migrates_review_exception_and_reassignment_csv_ledgers(self) -> None:
        reviews = self.root / "reviews.csv"
        update_review(reviews, "f-1", "confirmed", owner="analyst", reviewed_on=date(2026, 10, 1))
        self.assertEqual(migrate_csv_ledger(reviews, self.database, "reviews"), 1)
        self.assertEqual(load_reviews(self.database)["f-1"]["status"], "confirmed")

        exceptions = self.root / "exceptions.csv"
        exception_id = request_exception(
            exceptions, email="user@example.com", application="Slack", reason="On-call",
            requested_by="requester", expires_on=date(2026, 12, 1), evidence_ref="ticket",
            on=date(2026, 10, 1),
        )
        approve_exception(exceptions, exception_id, approved_by="manager", evidence_ref="approval",
                          on=date(2026, 10, 2))
        self.assertEqual(migrate_csv_ledger(exceptions, self.database, "exceptions"), 2)
        self.assertEqual(list_exceptions(self.database, as_of=date(2026, 10, 3))[0].status, "approved")

        plan = self.root / "plan.csv"
        with plan.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "finding_id", "application", "contract_id", "from_email", "to_email", "status",
            ])
            writer.writeheader()
            writer.writerow({"finding_id":"f-2", "application":"Slack", "contract_id":"c",
                             "from_email":"old", "to_email":"new", "status":"proposed"})
        old_reassignments = self.root / "reassignments.csv"
        stage_plan(plan, old_reassignments, actor="analyst", on=date(2026, 10, 1))
        transition(old_reassignments, "f-2", "approved", actor="manager", evidence_ref="approval")
        self.assertEqual(migrate_csv_ledger(old_reassignments, self.database, "reassignments"), 2)
        self.assertEqual(list_states(self.database)[0]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
