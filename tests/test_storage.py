from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest

from saas_sentry.access_reviews import create_campaign, decide_campaign_item, list_campaign_items
from saas_sentry.actions import ActionError, list_actions, propose_action, transition_action
from saas_sentry.storage import migrate_csv_ledger


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


if __name__ == "__main__":
    unittest.main()
