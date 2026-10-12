from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from saas_sentry.contract_model import model_contract_period
from saas_sentry.engine import InputError


class ContractModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.billing = self.write_csv("billing.csv", [
            "employee_id", "email", "application", "license_status", "contract_id",
            "seat_minimum", "seat_pool_id",
        ], [
            ["E1", "a@example.com", "Chat", "active", "collab", "0", "shared"],
            ["E1", "a@example.com", "Docs", "active", "collab", "0", "shared"],
            ["E2", "b@example.com", "Chat", "active", "collab", "0", "shared"],
            ["E2", "b@example.com", "Docs", "active", "collab", "0", "shared"],
        ])
        self.entitlements = self.write_csv("entitlements.csv", [
            "contract_id", "application", "entitled_seats", "pool_id",
        ], [
            ["collab", "Chat", "2", "shared"],
            ["collab", "Docs", "2", "shared"],
        ])
        self.pricing = self.write_csv("pricing.csv", [
            "contract_id", "min_seats", "max_seats", "annual_cost_per_seat", "currency",
        ], [
            ["collab", "1", "2", "3650", "INR"],
            ["collab", "3", "", "7300", "INR"],
        ])
        self.changes = self.write_csv("changes.csv", [
            "contract_id", "pool_id", "effective_date", "event_type", "seat_delta",
            "entitlement_delta", "annual_cost_per_seat", "currency",
        ], [
            ["collab", "shared", "2026-10-16", "seat_add", "1", "", "", ""],
            ["collab", "shared", "2026-10-16", "entitlement_change", "", "1", "", ""],
            ["collab", "", "2026-10-20", "price_change", "", "", "5000", "INR"],
        ])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, name: str, headers: list[str], rows: list[list[str]]) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def test_shared_pool_deduplicates_people_and_prorates_tier_and_midterm_changes(self) -> None:
        row, = model_contract_period(
            self.billing, self.entitlements, self.pricing, self.changes,
            date(2026, 10, 1), date(2026, 11, 1),
        )

        self.assertEqual(row.opening_active_seats, 2)
        self.assertEqual(row.ending_active_seats, 3)
        self.assertEqual(row.opening_entitled_seats, 2)
        self.assertEqual(row.ending_entitled_seats, 3)
        self.assertEqual(row.ending_overage_seats, 0)
        self.assertEqual(row.annualized_cost_at_start_inr, Decimal("7300.00"))
        self.assertEqual(row.annualized_cost_at_end_inr, Decimal("15000.00"))
        self.assertEqual(row.prorated_period_cost_inr, Decimal("1033.15"))
        self.assertEqual(row.applied_change_count, 3)

    def test_rejects_removal_that_would_make_pool_negative(self) -> None:
        self.write_csv("bad-change.csv", [
            "contract_id", "pool_id", "effective_date", "event_type", "seat_delta",
        ], [["collab", "shared", "2026-10-16", "seat_remove", "3"]])
        with self.assertRaisesRegex(InputError, "exceeds active seats"):
            model_contract_period(
                self.billing, self.entitlements, self.pricing, self.root / "bad-change.csv",
                date(2026, 10, 1), date(2026, 11, 1),
            )

    def test_entitlement_tiers_select_pool_capacity_from_seat_count(self) -> None:
        tiered = self.write_csv("tiered-entitlements.csv", [
            "contract_id", "application", "entitled_seats", "pool_id", "min_seats", "max_seats",
        ], [
            ["collab", "Chat", "2", "shared", "1", "2"],
            ["collab", "Chat", "3", "shared", "3", ""],
            ["collab", "Docs", "2", "shared", "1", "2"],
            ["collab", "Docs", "3", "shared", "3", ""],
        ])
        row, = model_contract_period(
            self.billing, tiered, self.pricing, self.changes,
            date(2026, 10, 1), date(2026, 11, 1),
        )
        self.assertEqual(row.opening_entitled_seats, 2)
        self.assertEqual(row.ending_entitled_seats, 4)
        self.assertEqual(row.ending_overage_seats, 0)


if __name__ == "__main__":
    unittest.main()
