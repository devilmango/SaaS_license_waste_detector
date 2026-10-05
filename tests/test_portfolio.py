from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from saas_sentry.engine import InputError, load_inputs
from saas_sentry.portfolio import (
    discover_portfolio,
    format_discovery,
    format_forecast,
    format_reconciliation,
    forecast_renewals,
    reconcile_focus,
)


class PortfolioFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hr = self.write_csv("hr.csv", ["employee", "email", "department", "cost_center", "status"], [
            ["Alice", "alice@example.com", "Engineering", "ENG", "active"],
        ])
        self.usage = self.write_csv("usage.csv", ["email", "application", "last_login"], [
            ["alice@example.com", "Design", "2026-10-01"],
            ["alice@example.com", "Chat", "2026-10-01"],
        ])
        self.billing = self.write_csv("billing.csv", [
            "email", "application", "license_status", "annual_cost", "contract_id", "renewal_date", "seat_minimum",
        ], [
            ["alice@example.com", "Design", "active", "3650", "suite", "2027-01-01", "2"],
            ["alice@example.com", "Chat", "active", "7300", "suite", "2027-01-01", "2"],
        ])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, name: str, headers: list[str], rows: list[list[str]]) -> Path:
        path = self.root / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def load(self):
        return load_inputs(self.hr, self.usage, self.billing)

    def test_reconciliation_marks_variances_unlicensed_spend_and_missing_bills(self) -> None:
        normalized = self.write_csv("focus-normalized.csv", [
            "application", "cost_center", "billing_period_start", "billing_period_end", "actual_billed_cost_inr",
        ], [
            ["Design", "ENG", "2026-09-01", "2026-10-01", "330"],
            ["Unknown Tool", "ENG", "2026-09-01", "2026-10-01", "50"],
        ])
        result = reconcile_focus(normalized, self.load(), tolerance_percent=Decimal("5"))
        by_app = {row.application: row for row in result}
        self.assertEqual(by_app["Design"].expected_cost_inr, Decimal("300.00"))
        self.assertEqual(by_app["Design"].status, "variance")
        self.assertEqual(by_app["Unknown Tool"].status, "unlicensed_spend")
        self.assertEqual(by_app["Chat"].status, "missing_invoice_spend")
        self.assertIn("variance_percent", format_reconciliation(result))

    def test_reconciliation_rejects_invalid_periods(self) -> None:
        normalized = self.write_csv("bad-period.csv", [
            "application", "cost_center", "billing_period_start", "billing_period_end", "actual_billed_cost_inr",
        ], [["Design", "ENG", "2026-10-01", "2026-09-01", "10"]])
        with self.assertRaisesRegex(InputError, "billing period end"):
            reconcile_focus(normalized, self.load())

    def test_discovery_flags_unmanaged_apps_and_category_overlap(self) -> None:
        inventory = self.write_csv("inventory.csv", [
            "application", "vendor", "category", "owner", "source",
        ], [
            ["Design", "Design Co", "Collaboration", "Engineering", "SSO"],
            ["Chat", "Chat Co", "Collaboration", "Engineering", "Expense"],
            ["MysteryApp", "Mystery Inc", "Collaboration", "", "Procurement"],
        ])
        result = discover_portfolio(inventory, self.load())
        by_app = {row.application: row for row in result}
        self.assertEqual(by_app["Design"].portfolio_status, "managed+overlap_candidate")
        self.assertEqual(by_app["MysteryApp"].portfolio_status, "shadow_saas+overlap_candidate")
        self.assertIn("SSO", by_app["Design"].sources)
        self.assertIn("Chat", by_app["Design"].related_applications)
        self.assertIn("portfolio_status", format_discovery(result))

    def test_forecast_applies_growth_inflation_and_contract_floor(self) -> None:
        result = forecast_renewals(
            self.load(), price_increase_percent=Decimal("10"),
            seat_growth_percent=Decimal("50"), seat_reduction_percent=Decimal("100"),
        )
        row = result[0]
        self.assertEqual(row.active_seats, 2)
        self.assertEqual(row.projected_seats_before_reduction, 3)
        self.assertEqual(row.projected_seats_after_reduction, 2)
        self.assertEqual(row.projected_annual_cost_inr, Decimal("18067.50"))
        self.assertEqual(row.reduced_scenario_annual_cost_inr, Decimal("12045.00"))
        self.assertIn("projected_annual_cost_inr", format_forecast(result))

    def test_forecast_rejects_reduction_over_one_hundred_percent(self) -> None:
        with self.assertRaisesRegex(InputError, "cannot exceed 100"):
            forecast_renewals(self.load(), seat_reduction_percent=Decimal("101"))

    def test_forecast_selects_the_tier_for_projected_seats(self) -> None:
        pricing = self.write_csv("tiers.csv", [
            "contract_id", "min_seats", "max_seats", "annual_cost_per_seat",
        ], [["suite", "0", "2", "5000"], ["suite", "3", "", "6000"]])
        data = load_inputs(self.hr, self.usage, self.billing, contract_pricing_path=pricing)
        result = forecast_renewals(data, price_increase_percent=Decimal("10"), seat_growth_percent=Decimal("50"))
        self.assertEqual(result[0].projected_seats_before_reduction, 3)
        self.assertEqual(result[0].projected_annual_cost_inr, Decimal("19800.00"))

    def test_forecast_fails_if_projected_seats_have_no_price_tier(self) -> None:
        pricing = self.write_csv("tier-gap.csv", [
            "contract_id", "min_seats", "max_seats", "annual_cost_per_seat",
        ], [["suite", "0", "2", "5000"]])
        data = load_inputs(self.hr, self.usage, self.billing, contract_pricing_path=pricing)
        with self.assertRaisesRegex(InputError, "No configured contract price tier covers 3 seats"):
            forecast_renewals(data, seat_growth_percent=Decimal("50"))


if __name__ == "__main__":
    unittest.main()
