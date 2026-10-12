from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
import tempfile
import unittest

from saas_sentry.access_reviews import (
    create_campaign,
    decide_campaign_item,
    generate_reminders,
    list_campaign_items,
)
from saas_sentry.engine import InputError, load_reviews


class AccessReviewCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.findings = self.root / "findings.csv"
        with self.findings.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "finding_id", "application", "review_status", "review_owner",
            ])
            writer.writeheader()
            writer.writerows([
                {"finding_id": "f-1", "application": "Slack", "review_status": "unreviewed", "review_owner": "owner@example.com"},
                {"finding_id": "f-2", "application": "Zoom", "review_status": "unreviewed", "review_owner": ""},
                {"finding_id": "f-3", "application": "Notion", "review_status": "confirmed", "review_owner": "owner@example.com"},
            ])
        self.ledger = self.root / "campaign-events.csv"
        self.reviews = self.root / "reviews.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_campaign_assigns_owners_generates_cooldown_reminders_and_records_decisions(self) -> None:
        campaign, assigned = create_campaign(
            self.findings, self.ledger, due_on=date(2026, 10, 20),
            created_by="admin@example.com", default_owner="owner@example.com",
            campaign_id="october-review", created_on=date(2026, 10, 10),
        )
        self.assertEqual((campaign, assigned), ("october-review", 2))
        reminder_file = self.root / "out" / "reminders.csv"
        reminders = generate_reminders(
            self.ledger, reminder_file, as_of=date(2026, 10, 15), within_days=7,
            minimum_gap_days=7,
        )
        self.assertEqual(len(reminders), 2)
        self.assertEqual(len(generate_reminders(
            self.ledger, reminder_file, as_of=date(2026, 10, 16), within_days=7,
            minimum_gap_days=7,
        )), 0)

        decide_campaign_item(
            self.ledger, self.reviews, campaign, "f-1", "confirmed",
            reviewed_by="owner@example.com", note="Still required", decided_on=date(2026, 10, 16),
        )

        rows = {row.finding_id: row for row in list_campaign_items(self.ledger, campaign_id=campaign)}
        self.assertEqual(rows["f-1"].status, "decided")
        self.assertEqual(rows["f-1"].decision, "confirmed")
        self.assertEqual(rows["f-2"].reminder_count, 1)
        self.assertEqual(load_reviews(self.reviews)["f-1"]["status"], "confirmed")

    def test_only_assigned_owner_can_decide_campaign_item(self) -> None:
        campaign, _ = create_campaign(
            self.findings, self.ledger, due_on=date(2026, 10, 20),
            created_by="admin", default_owner="owner@example.com", campaign_id="campaign",
        )
        with self.assertRaisesRegex(InputError, "assigned owner"):
            decide_campaign_item(
                self.ledger, self.reviews, campaign, "f-1", "dismissed", reviewed_by="other@example.com",
            )

    def test_application_owner_map_routes_findings_to_app_owners(self) -> None:
        owner_map = self.root / "owners.csv"
        with owner_map.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["application", "owner"])
            writer.writerows([["Slack", "slack-owner@example.com"], ["Zoom", "zoom-owner@example.com"]])
        campaign, _ = create_campaign(
            self.findings, self.ledger, due_on=date(2026, 10, 20), created_by="admin",
            owner_map_path=owner_map, campaign_id="mapped-campaign",
        )
        owners = {row.finding_id: row.owner for row in list_campaign_items(self.ledger, campaign_id=campaign)}
        self.assertEqual(owners, {"f-1": "slack-owner@example.com", "f-2": "zoom-owner@example.com"})


if __name__ == "__main__":
    unittest.main()
