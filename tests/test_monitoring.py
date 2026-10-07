from __future__ import annotations

import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from saas_sentry.monitoring import detect_anomalies, format_anomalies


class MonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_detects_material_changes_contract_floor_renewal_and_invoice_issues(self) -> None:
        baseline = {
            "as_of": "2026-09-01",
            "cost_center_showback": [{
                "department": "Engineering", "cost_center": "ENG", "active_license_count": 5,
                "annualized_spend_inr": "10000.00",
            }],
            "renewal_calendar": [{
                "contract_id": "editor-suite", "active_seats": 5, "seat_minimum": 4,
                "annualized_spend_inr": "10000.00", "days_until_notice_deadline": 120,
                "status": "scheduled", "notice_deadline": "2027-01-01",
            }],
        }
        current = {
            "as_of": "2026-10-01",
            "cost_center_showback": [{
                "department": "Engineering", "cost_center": "ENG", "active_license_count": 7,
                "annualized_spend_inr": "13000.00",
            }],
            "renewal_calendar": [{
                "contract_id": "editor-suite", "active_seats": 7, "seat_minimum": 10,
                "annualized_spend_inr": "13000.00", "days_until_notice_deadline": 21,
                "status": "action_due_soon", "notice_deadline": "2026-10-22",
            }],
            "data_quality_issues": [{
                "issue": "seat_minimum_limit", "identity": "editor-suite",
                "details": "Only 7 seats can be removed without violating the floor.",
            }],
        }
        invoice = self.root / "reconciliation.csv"
        with invoice.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "application", "cost_center", "billing_period_start", "billing_period_end",
                "actual_billed_cost_inr", "variance_inr", "status",
            ])
            writer.writeheader()
            writer.writerow({
                "application": "Editor", "cost_center": "ENG", "billing_period_start": "2026-09-01",
                "billing_period_end": "2026-10-01", "actual_billed_cost_inr": "5000",
                "variance_inr": "5000", "status": "unlicensed_spend",
            })
        anomalies = detect_anomalies(
            current, baseline=baseline, reconciliation_path=invoice,
            spend_change_threshold_percent=Decimal("20"), seat_change_threshold=2,
            renewal_window_days=60,
        )
        kinds = {item.kind for item in anomalies}
        self.assertIn("cost_center_spend_change", kinds)
        self.assertIn("cost_center_seat_change", kinds)
        self.assertIn("contract_spend_change", kinds)
        self.assertIn("contract_seat_change", kinds)
        self.assertIn("underutilized_commitment", kinds)
        self.assertIn("renewal_notice_due", kinds)
        self.assertIn("reclaim_blocked_by_contract_floor", kinds)
        self.assertIn("invoice_unlicensed_spend", kinds)
        self.assertEqual(anomalies[0].severity, "high")
        self.assertIn("severity,kind,scope", format_anomalies(anomalies))

    def test_rejects_invalid_monitor_thresholds(self) -> None:
        with self.assertRaisesRegex(ValueError, "seat and renewal thresholds"):
            detect_anomalies({"as_of": "2026-10-01"}, seat_change_threshold=-1)


if __name__ == "__main__":
    unittest.main()
