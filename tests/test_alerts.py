from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

from saas_sentry.alerts import deliver_alerts, list_delivery_records
from saas_sentry.engine import InputError


class AlertDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.alerts = self.root / "alerts.csv"
        with self.alerts.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["severity", "kind", "scope", "baseline_value", "current_value", "change", "summary"])
            writer.writerow(["high", "invoice_variance", "Slack / ENG", "model", "15000", "+5000", "Invoice differs"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preview_does_not_send_and_delivery_is_deduplicated(self) -> None:
        ledger = self.root / "deliveries.csv"
        with patch("saas_sentry.alerts.urlopen") as request:
            self.assertEqual(deliver_alerts(self.alerts, ledger, webhook_url="https://hooks.example.test", send=False), (1, 0))
            request.assert_not_called()
            request.return_value.__enter__.return_value.status = 204
            self.assertEqual(deliver_alerts(self.alerts, ledger, webhook_url="https://hooks.example.test", send=True), (1, 1))
            self.assertEqual(deliver_alerts(self.alerts, ledger, webhook_url="https://hooks.example.test", send=True), (0, 0))
            self.assertEqual(request.call_count, 1)
        self.assertFalse(ledger.with_name("deliveries.csv.lock").exists())

    def test_rejects_insecure_or_embedded_credential_urls(self) -> None:
        for url in ("http://hooks.example.test", "https://user:secret@hooks.example.test"):
            with self.subTest(url=url), self.assertRaisesRegex(InputError, "HTTPS"):
                deliver_alerts(self.alerts, self.root / "ledger.csv", webhook_url=url)

    def test_failed_alert_is_retried_and_attempts_are_visible(self) -> None:
        ledger = self.root / "deliveries.csv"
        with patch("saas_sentry.alerts.urlopen", side_effect=[URLError("busy"), _response()]), patch(
            "saas_sentry.alerts.time.sleep",
        ) as sleep:
            self.assertEqual(deliver_alerts(
                self.alerts, ledger, webhook_url="https://hooks.example.test", send=True,
                retries=1, base_delay=0.25,
            ), (1, 1))
        sleep.assert_called_once_with(0.25)
        record, = list_delivery_records(ledger)
        self.assertEqual((record["status"], record["attempts"], record["manual_follow_up"]),
                         ("delivered", 2, False))

    def test_exhausted_alert_delivery_is_marked_for_manual_follow_up(self) -> None:
        ledger = self.root / "deliveries.csv"
        with patch("saas_sentry.alerts.urlopen", side_effect=URLError("offline")):
            self.assertEqual(deliver_alerts(
                self.alerts, ledger, webhook_url="https://hooks.example.test", send=True,
                retries=0, max_attempts=2, base_delay=0,
            ), (1, 0))
            self.assertEqual(deliver_alerts(
                self.alerts, ledger, webhook_url="https://hooks.example.test", send=True,
                retries=0, max_attempts=2, base_delay=0,
            ), (1, 0))
            self.assertEqual(deliver_alerts(
                self.alerts, ledger, webhook_url="https://hooks.example.test", send=True,
                retries=0, max_attempts=2, base_delay=0,
            ), (0, 0))
        record, = list_delivery_records(ledger)
        self.assertEqual((record["status"], record["attempts"], record["manual_follow_up"]),
                         ("failed", 2, True))
        alert_id = record["alert_id"]
        from saas_sentry.alerts import retry_manual_alert
        retry_manual_alert(ledger, alert_id)
        reset, = list_delivery_records(ledger)
        self.assertEqual((reset["status"], reset["attempts"], reset["manual_follow_up"]),
                         ("pending", 0, False))

    def test_sqlite_delivery_state_is_persistent_and_lists_manual_follow_up(self) -> None:
        ledger = self.root / "deliveries.sqlite"
        with patch("saas_sentry.alerts.urlopen", side_effect=URLError("offline")):
            deliver_alerts(self.alerts, ledger, webhook_url="https://hooks.example.test", send=True,
                           retries=0, max_attempts=1, base_delay=0)
        record, = list_delivery_records(ledger)
        self.assertEqual(record["status"], "failed")
        self.assertTrue(record["manual_follow_up"])

    def test_legacy_delivery_csv_is_upgraded_without_losing_dedupe_state(self) -> None:
        ledger = self.root / "legacy.csv"
        ledger.write_text("alert_id,delivered_at,destination\nold-id,2026-10-29T00:00:00+00:00,hooks.example.test\n",
                          encoding="utf-8")
        with patch("saas_sentry.alerts.urlopen", return_value=_response()):
            self.assertEqual(deliver_alerts(self.alerts, ledger, webhook_url="https://hooks.example.test",
                                            send=True), (1, 1))
        self.assertEqual(len(list_delivery_records(ledger)), 2)
        self.assertIn("manual_follow_up", ledger.read_text(encoding="utf-8"))


def _response():
    from unittest.mock import MagicMock
    response = MagicMock()
    response.__enter__.return_value.status = 204
    return response


if __name__ == "__main__":
    unittest.main()
