from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from saas_sentry.engine import (
    CurrencyConfig,
    InputError,
    Rules,
    analyze,
    format_findings_csv,
    format_renewals_csv,
    format_quality_csv,
    format_showback_csv,
    load_config,
    load_inputs,
    load_reviews,
    update_review,
)
from saas_sentry.reports import compare_snapshots, list_snapshots, render_html, report_payload, write_snapshot


class SaaSSentryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hr = self.root / "hr.csv"
        self.usage = self.root / "usage.csv"
        self.billing = self.root / "billing.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, path: Path, headers: list[str], rows: list[list[str]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)

    def basic_inputs(self) -> None:
        self.write_csv(self.hr, ["employee", "employee_id", "email", "email_aliases", "status"], [
            ["Alice", "A-1", "alice@example.com", "ally@example.com", "active"],
            ["Bob", "B-2", "bob@example.com", "", "terminated"],
            ["Cara", "C-3", "cara@example.com", "", "active"],
        ])
        self.write_csv(self.usage, [
            "employee_id", "email", "application", "last_login", "usage_count",
            "active_days", "usage_window_days", "features_used",
        ], [
            ["A-1", "ally@example.com", "Editor", "2026-10-01", "20", "20", "30", "4"],
            ["C-3", "cara@example.com", "Editor", "2026-10-01", "1", "1", "30", "1"],
        ])
        self.write_csv(self.billing, [
            "employee_id", "email", "application", "license_status", "annual_cost",
            "currency", "contract_id", "renewal_date", "seat_minimum",
        ], [
            ["A-1", "alice@example.com", "Editor", "active", "200", "USD", "editors", "2026-11-15", "2"],
            ["B-2", "bob@example.com", "Editor", "active", "100", "USD", "editors", "2026-11-15", "2"],
            ["C-3", "cara@example.com", "Editor", "active", "300", "USD", "editors", "2026-11-15", "2"],
        ])

    def test_identity_alias_terminated_and_low_activity(self) -> None:
        self.basic_inputs()
        data = load_inputs(
            self.hr, self.usage, self.billing,
            currency=CurrencyConfig("USD", date(2026, 10, 1), {"INR": Decimal("1"), "USD": Decimal("80")}),
        )
        result = analyze(
            data, as_of=date(2026, 10, 4),
            rules=Rules(cost_threshold_inr=Decimal("10000"), low_active_day_ratio=Decimal("0.10")),
            currency=CurrencyConfig("USD", date(2026, 10, 1), {"INR": Decimal("1"), "USD": Decimal("80")}),
        )
        by_email = {finding.email: finding for finding in result.findings}
        self.assertEqual(set(by_email), {"bob@example.com", "cara@example.com"})
        self.assertEqual(by_email["bob@example.com"].category, "terminated")
        self.assertEqual(by_email["cara@example.com"].category, "optimization")
        self.assertEqual(by_email["cara@example.com"].annualized_cost_inr, Decimal("24000.00"))
        self.assertEqual(by_email["cara@example.com"].evidence, "Active days=1/30; features used=1")

    def test_cost_center_showback_and_renewal_calendar(self) -> None:
        self.basic_inputs()
        self.write_csv(self.hr, [
            "employee", "employee_id", "email", "email_aliases", "department", "cost_center", "status",
        ], [
            ["Alice", "A-1", "alice@example.com", "ally@example.com", "Engineering", "ENG-100", "active"],
            ["Bob", "B-2", "bob@example.com", "", "Finance", "FIN-200", "terminated"],
            ["Cara", "C-3", "cara@example.com", "", "Engineering", "ENG-100", "active"],
        ])
        currency = CurrencyConfig("USD", date(2026, 10, 1), {"INR": Decimal("1"), "USD": Decimal("80")})
        data = load_inputs(self.hr, self.usage, self.billing, currency=currency)
        result = analyze(data, as_of=date(2026, 10, 4), currency=currency)
        showback = {(row.department, row.cost_center): row for row in result.showback}
        engineering = showback[("Engineering", "ENG-100")]
        finance = showback[("Finance", "FIN-200")]
        self.assertEqual(engineering.active_license_count, 2)
        self.assertEqual(engineering.annualized_spend_inr, Decimal("40000.00"))
        self.assertEqual(engineering.annual_opportunity_inr, Decimal("24000.00"))
        self.assertEqual(finance.annualized_spend_inr, Decimal("8000.00"))
        self.assertIn("ENG-100", format_showback_csv(result))
        renewal = result.renewals[0]
        self.assertEqual(renewal.notice_deadline, date(2026, 11, 15))
        self.assertEqual(renewal.days_until_notice_deadline, 42)
        self.assertEqual(renewal.status, "action_due_soon")
        self.assertIn("notice_deadline", format_renewals_csv(result))

    def test_contract_floor_caps_savings_and_renewal_prorates(self) -> None:
        self.basic_inputs()
        data = load_inputs(
            self.hr, self.usage, self.billing,
            currency=CurrencyConfig("USD", date(2026, 10, 1), {"INR": Decimal("1"), "USD": Decimal("80")}),
        )
        result = analyze(data, as_of=date(2026, 10, 4), currency=CurrencyConfig(
            "USD", date(2026, 10, 1), {"INR": Decimal("1"), "USD": Decimal("80")},
        ))
        self.assertEqual(len(result.findings), 2)
        self.assertEqual(sum((f.opportunity_savings_inr for f in result.findings), Decimal("0")), Decimal("24000.00"))
        self.assertEqual(sum((f.realizable_12m_inr for f in result.findings), Decimal("0")), Decimal("21238.36"))
        self.assertTrue(any(issue.issue == "seat_minimum_limit" for issue in result.issues))

    def test_contract_tiers_notice_period_and_bundle_constraints(self) -> None:
        self.write_csv(self.hr, ["employee", "employee_id", "email", "status"], [
            ["Alice", "A-1", "alice@example.com", "active"],
            ["Bob", "B-2", "bob@example.com", "active"],
        ])
        self.write_csv(self.usage, ["employee_id", "email", "application", "last_login"], [
            ["A-1", "alice@example.com", "Editor", "2026-10-01"],
            ["B-2", "bob@example.com", "Editor", "2025-10-01"],
        ])
        self.write_csv(self.billing, [
            "employee_id", "email", "application", "license_status", "annual_cost", "contract_id",
            "renewal_date", "seat_minimum", "commitment_end_date", "notice_days",
        ], [
            ["A-1", "alice@example.com", "Editor", "active", "100", "editors", "2026-11-15", "0", "2027-03-31", "90"],
            ["B-2", "bob@example.com", "Editor", "active", "100", "editors", "2026-11-15", "0", "2027-03-31", "90"],
        ])
        pricing = self.root / "tiers.csv"
        self.write_csv(pricing, ["contract_id", "min_seats", "max_seats", "annual_cost_per_seat"], [
            ["editors", "0", "1", "80"], ["editors", "2", "", "100"],
        ])
        data = load_inputs(self.hr, self.usage, self.billing, contract_pricing_path=pricing)
        result = analyze(data, as_of=date(2026, 10, 4))
        finding = result.findings[0]
        self.assertEqual(finding.opportunity_savings_inr, Decimal("120.00"))
        self.assertEqual(finding.savings_effective_date, date(2027, 3, 31))
        self.assertEqual(finding.realizable_12m_inr, Decimal("61.48"))

    def test_bundle_requires_all_assigned_products_to_be_reclaimable(self) -> None:
        self.write_csv(self.hr, ["employee", "employee_id", "email", "status"], [
            ["Alice", "A-1", "alice@example.com", "active"],
        ])
        self.write_csv(self.usage, ["employee_id", "email", "application", "last_login"], [
            ["A-1", "alice@example.com", "Product B", "2026-10-01"],
        ])
        self.write_csv(self.billing, ["employee_id", "email", "application", "license_status", "annual_cost", "bundle_group"], [
            ["A-1", "alice@example.com", "Product A", "active", "100", "suite"],
            ["A-1", "alice@example.com", "Product B", "active", "100", "suite"],
        ])
        data = load_inputs(self.hr, self.usage, self.billing)
        result = analyze(data, as_of=date(2026, 10, 4))
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].opportunity_savings_inr, Decimal("0"))
        self.assertTrue(any(issue.issue == "bundle_constraint" for issue in result.issues))

    def test_conflicting_identity_is_reported_and_excluded(self) -> None:
        self.basic_inputs()
        self.write_csv(self.billing, ["employee_id", "email", "application", "license_status", "annual_cost"], [
            ["A-1", "bob@example.com", "Other", "active", "100"],
            ["UNKNOWN", "nobody@example.com", "Other", "active", "100"],
        ])
        data = load_inputs(self.hr, self.usage, self.billing)
        result = analyze(data, as_of=date(2026, 10, 4))
        self.assertEqual(result.findings, ())
        self.assertEqual(result.showback[0].cost_center, "Unassigned")
        self.assertEqual(result.showback[0].active_license_count, 2)
        self.assertEqual(sum(issue.issue == "identity_conflict" for issue in result.issues), 1)
        self.assertEqual(sum(issue.issue == "unmatched_identity" for issue in result.issues), 1)
        self.assertIn("identity_conflict", format_quality_csv(result))

    def test_stale_active_license_and_id_with_email_mismatch(self) -> None:
        self.basic_inputs()
        self.write_csv(self.hr, ["employee", "employee_id", "email", "status"], [
            ["Alice", "A-1", "alice@example.com", "active"],
        ])
        self.write_csv(self.usage, ["employee_id", "email", "application", "last_login"], [
            ["A-1", "old-alias@example.com", "Editor", "2026-04-01"],
        ])
        self.write_csv(self.billing, ["employee_id", "email", "application", "license_status", "annual_cost"], [
            ["A-1", "alice@example.com", "Editor", "active", "12000"],
        ])
        data = load_inputs(self.hr, self.usage, self.billing)
        result = analyze(data, as_of=date(2026, 10, 4))
        self.assertEqual(result.findings[0].category, "inactive")
        self.assertTrue(any(issue.issue == "identity_warning" for issue in result.issues))

    def test_review_ledger_upserts_and_joins_to_finding_export(self) -> None:
        self.basic_inputs()
        currency = CurrencyConfig("USD", date(2026, 10, 1), {"INR": Decimal("1"), "USD": Decimal("80")})
        data = load_inputs(self.hr, self.usage, self.billing, currency=currency)
        result = analyze(data, as_of=date(2026, 10, 4), currency=currency)
        finding_id = result.findings[0].finding_id
        ledger = self.root / "nested" / "reviews.csv"
        update_review(ledger, finding_id, "confirmed", owner="FinOps", note="Remove at renewal", reviewed_on=date(2026, 10, 4))
        update_review(ledger, finding_id, "deferred", owner="FinOps", note="Need owner sign-off", reviewed_on=date(2026, 10, 5))
        reviews = load_reviews(ledger)
        self.assertEqual(reviews[finding_id]["status"], "deferred")
        self.assertIn("Need owner sign-off", format_findings_csv(result, reviews))

    def test_toml_configuration_requires_rate_date_for_foreign_exchange(self) -> None:
        config = self.root / "config.toml"
        config.write_text(
            '[rules]\nstale_days=45\nlow_active_day_ratio=0.15\n'
            '[usage]\nwindow_days=14\n'
            '[currency]\ndefault_currency="USD"\nrates_to_inr={INR=1, USD=83.5}\n',
            encoding="utf-8",
        )
        with self.assertRaises(InputError):
            load_config(config)
        config.write_text(
            '[rules]\nstale_days=45\nlow_active_day_ratio=0.15\n'
            '[usage]\nwindow_days=14\n'
            '[currency]\ndefault_currency="USD"\nrate_date="2026-10-01"\nrates_to_inr={INR=1, USD=83.5}\n',
            encoding="utf-8",
        )
        rules, currency = load_config(config)
        self.assertEqual(rules.stale_days, 45)
        self.assertEqual(rules.low_active_day_ratio, Decimal("0.15"))
        self.assertEqual(rules.default_usage_window_days, 14)
        self.assertEqual(currency.rate_date, date(2026, 10, 1))

    def test_snapshots_are_listed_and_comparable_and_html_is_escaped(self) -> None:
        self.basic_inputs()
        currency = CurrencyConfig("USD", date(2026, 10, 1), {"INR": Decimal("1"), "USD": Decimal("80")})
        result = analyze(load_inputs(self.hr, self.usage, self.billing, currency=currency),
                         as_of=date(2026, 10, 4), currency=currency)
        payload = report_payload(result, as_of=date(2026, 10, 4), source_paths=[self.hr, self.usage, self.billing])
        self.assertTrue(payload["cost_center_showback"])
        self.assertTrue(payload["renewal_calendar"])
        self.assertIn("Renewal calendar", render_html(payload))
        snapshots = self.root / "snapshots"
        write_snapshot(snapshots, payload)
        write_snapshot(snapshots, payload)
        self.assertEqual(len(list_snapshots(snapshots)), 2)
        comparison = compare_snapshots(payload, payload)
        self.assertEqual(comparison["added_finding_ids"], [])
        self.assertEqual(comparison["contract_adjusted_annual_opportunity_delta_inr"], "0.00")
        payload["findings"][0]["employee"] = "<script>alert(1)</script>"
        self.assertIn("&lt;script&gt;", render_html(payload))


if __name__ == "__main__":
    unittest.main()
