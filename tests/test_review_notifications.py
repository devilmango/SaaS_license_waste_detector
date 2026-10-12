from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from saas_sentry.engine import InputError
from saas_sentry.review_notifications import deliver_review_reminders


class ReviewReminderDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reminders = self.root / "reminders.csv"
        with self.reminders.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "campaign_id", "finding_id", "application", "owner", "due_on",
                "days_until_due", "reminder_count", "reminder_reason",
            ])
            writer.writerows([
                ["october", "f-1", "Slack", "slack-owner@example.com", "2026-10-20", 5, 1, "due_soon"],
                ["october", "f-2", "Zoom", "slack-owner@example.com", "2026-10-20", 5, 1, "due_soon"],
                ["october", "f-3", "Notion", "notion-owner@example.com", "2026-10-18", 3, 2, "due_soon"],
            ])
        self.ledger = self.root / "deliveries.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def response():
        return type("Response", (), {
            "status": 204, "__enter__": lambda self: self,
            "__exit__": lambda *args: None,
        })()

    def test_preview_groups_by_owner_without_network_or_ledger_writes(self) -> None:
        with patch("saas_sentry.review_notifications.urlopen") as request:
            self.assertEqual(deliver_review_reminders(
                self.reminders, self.ledger, webhook_url="https://hooks.example.test", send=False,
            ), (3, 0, 0))
            request.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_delivery_groups_owners_deduplicates_success_and_retries_failures(self) -> None:
        responses = [self.response(), OSError("temporary")]
        captured = []

        def send(request, timeout):
            captured.append(json.loads(request.data))
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch("saas_sentry.review_notifications.urlopen", side_effect=send):
            self.assertEqual(deliver_review_reminders(
                self.reminders, self.ledger, webhook_url="https://hooks.example.test", send=True,
            ), (3, 2, 1))
        self.assertEqual(len(captured), 2)
        self.assertEqual({payload["owner"] for payload in captured}, {"slack-owner@example.com", "notion-owner@example.com"})
        self.assertEqual(len(next(payload for payload in captured if payload["owner"] == "slack-owner@example.com")["reminders"]), 2)
        with patch("saas_sentry.review_notifications.urlopen", return_value=self.response()) as request:
            self.assertEqual(deliver_review_reminders(
                self.reminders, self.ledger, webhook_url="https://hooks.example.test", send=True,
            ), (1, 1, 0))
            request.assert_called_once()
        with patch("saas_sentry.review_notifications.urlopen") as request:
            self.assertEqual(deliver_review_reminders(
                self.reminders, self.ledger, webhook_url="https://hooks.example.test", send=True,
            ), (0, 0, 0))
            request.assert_not_called()
        with self.ledger.open(encoding="utf-8", newline="") as handle:
            events = list(csv.DictReader(handle))
        self.assertEqual([row["status"] for row in events].count("failed"), 1)
        self.assertEqual([row["status"] for row in events].count("delivered"), 3)
        self.assertFalse(self.ledger.with_name("deliveries.csv.lock").exists())

    def test_rejects_insecure_webhook(self) -> None:
        with self.assertRaisesRegex(InputError, "HTTPS"):
            deliver_review_reminders(self.reminders, self.ledger, webhook_url="http://hooks.example.test", send=True)


if __name__ == "__main__":
    unittest.main()
