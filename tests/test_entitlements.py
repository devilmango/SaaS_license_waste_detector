from __future__ import annotations

import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from saas_sentry.entitlements import analyze_entitlements, format_entitlements


class EntitlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, name: str, headers: list[str], rows: list[list[str]]) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def test_reports_overage_unused_and_unmapped_contract_cost(self) -> None:
        billing = self.write_csv("billing.csv", [
            "contract_id", "application", "license_status", "annual_cost",
        ], [
            ["design", "Figma", "active", "12000"],
            ["design", "Figma", "active", "12000"],
            ["design", "Figma", "active", "12000"],
            ["editor", "Editor", "active", "6000"],
        ])
        contracts = self.write_csv("contracts.csv", ["contract_id", "application", "entitled_seats"], [
            ["design", "Figma", "2"], ["editor", "Editor", "3"],
        ])
        rows = analyze_entitlements(contracts, billing)
        design = next(row for row in rows if row.application == "Figma")
        editor = next(row for row in rows if row.application == "Editor")
        self.assertEqual((design.overage_seats, design.unused_entitlement_seats), (1, 0))
        self.assertEqual(design.estimated_annual_overage_inr, Decimal("12000.00"))
        self.assertEqual((editor.overage_seats, editor.unused_entitlement_seats), (0, 2))
        self.assertIn("estimated_annual_overage_inr", format_entitlements(rows))

    def test_active_contract_without_entitlement_is_reported(self) -> None:
        billing = self.write_csv("billing.csv", ["contract_id", "application", "license_status", "annual_cost"], [
            ["unmapped", "App", "active", "500"],
        ])
        contracts = self.write_csv("contracts.csv", ["contract_id", "application", "entitled_seats"], [])
        row = analyze_entitlements(contracts, billing)[0]
        self.assertEqual(row.status, "missing_entitlement")
        self.assertEqual(row.estimated_annual_overage_inr, Decimal("500.00"))


if __name__ == "__main__":
    unittest.main()
