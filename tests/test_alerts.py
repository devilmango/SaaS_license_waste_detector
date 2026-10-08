from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from saas_sentry.alerts import deliver_alerts
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


if __name__ == "__main__":
    unittest.main()
