from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from saas_sentry.engine import CurrencyConfig, InputError
from saas_sentry.focus import format_focus_spend, load_focus_spend


class FocusImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "focus.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_rows(self, rows: list[list[str]], headers: list[str] | None = None) -> None:
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers or [
                "ProviderName", "ServiceName", "BilledCost", "BillingCurrency",
                "BillingPeriodStart", "BillingPeriodEnd", "Tags",
            ])
            writer.writerows(rows)

    def test_aggregates_focus_billed_cost_by_cost_center_and_period(self) -> None:
        self.write_rows([
            ["Vendor", "Slack", "10", "USD", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", '{"cost_center":"ENG"}'],
            ["Vendor", "Slack", "2.50", "USD", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", '{"cost_center":"ENG"}'],
            ["Vendor", "Slack", "3", "USD", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", '{"cost_center":"FIN"}'],
        ])
        currency = CurrencyConfig("USD", date(2026, 10, 1), {"INR": Decimal("1"), "USD": Decimal("80")})
        result = load_focus_spend(self.path, currency=currency)
        self.assertEqual(len(result), 2)
        engineering = next(row for row in result if row.cost_center == "ENG")
        self.assertEqual(engineering.billed_cost_inr, Decimal("1000.00"))
        serialized = format_focus_spend(result, currency.rate_date)
        self.assertIn("actual_billed_cost_inr", serialized)
        self.assertIn("1000.00", serialized)

    def test_rejects_bad_tags_and_unknown_currency_rate(self) -> None:
        self.write_rows([
            ["Vendor", "Slack", "10", "USD", "2026-09-01", "2026-10-01", "[]"],
        ])
        with self.assertRaisesRegex(InputError, "Tags must contain a JSON object"):
            load_focus_spend(self.path, currency=CurrencyConfig(
                rates_to_inr={"INR": Decimal("1"), "USD": Decimal("80")},
            ))
        self.write_rows([
            ["Vendor", "Slack", "10", "USD", "2026-09-01", "2026-10-01", "{}"],
        ])
        with self.assertRaisesRegex(InputError, "no INR exchange rate"):
            load_focus_spend(self.path, currency=CurrencyConfig(rates_to_inr={"INR": Decimal("1")}))


if __name__ == "__main__":
    unittest.main()
