from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest
from datetime import date

from saas_sentry.actions import propose_action, transition_action
from saas_sentry.execution import build_execution_plan, execute_plan
from saas_sentry.reclaim_verification import verify_provider_reclaims


USER_ID = "11111111-1111-4111-8111-111111111111"
LICENSE_ID = "22222222-2222-4222-8222-222222222222"


class ReclaimVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.findings = self.write_csv("findings.csv", [
            "finding_id", "review_status", "email", "application", "contract_adjusted_annual_opportunity_inr",
        ], [["f-1", "confirmed", "person@example.com", "SKU_ONE", "12000"]])
        self.actions = self.root / "actions.csv"
        self.action_id = propose_action(
            self.findings, self.actions, "f-1", owner="FinOps", proposed_by="analyst",
            proposed_on=date(2026, 10, 1),
        )
        transition_action(self.actions, self.action_id, "approved", actor="manager")
        self.billing = self.root / "provider" / "billing.csv"
        self.write_billing([["person@example.com", "SKU_ONE", "active", "microsoft365", USER_ID, LICENSE_ID]])
        self.execution = self.root / "execution.csv"
        plan = build_execution_plan(self.actions, self.findings, self.billing, self.action_id)
        execute_plan(plan, self.execution, actor="operator", operation=lambda _plan: "license_removed")
        self.verification_ledger = self.root / "verification-events.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, name: str, headers: list[str], rows: list[list[str]]) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def write_billing(self, rows: list[list[str]]) -> None:
        self.write_csv("provider/billing.csv", [
            "email", "application", "license_status", "provider", "provider_user_id", "provider_license_id",
        ], rows)
        (self.billing.parent / "import.json").write_text(json.dumps({
            "provider": "microsoft365", "imported_at": "2026-10-12T10:00:00+00:00",
            "license_assignment_count": len(rows),
        }), encoding="utf-8")

    def verify(self):
        return verify_provider_reclaims(
            self.actions, self.execution, self.findings, self.billing,
            self.verification_ledger, provider="microsoft365",
        )

    def test_reports_exact_assignment_still_present_as_follow_up(self) -> None:
        record, = self.verify()
        self.assertEqual(record.status, "still_assigned")
        self.assertTrue(record.follow_up)
        with self.verification_ledger.open(encoding="utf-8", newline="") as handle:
            self.assertEqual(next(csv.DictReader(handle))["status"], "still_assigned")

    def test_verification_uses_execution_target_when_finding_disappears_from_new_analysis(self) -> None:
        records = verify_provider_reclaims(
            self.actions, self.execution, None, self.billing,
            self.verification_ledger, provider="microsoft365",
        )
        self.assertEqual(records[0].status, "still_assigned")

    def test_confirms_absent_assignment_only_from_complete_provider_snapshot(self) -> None:
        self.write_billing([])
        record, = self.verify()
        self.assertEqual(record.status, "verified_reclaimed")
        self.assertEqual(record.follow_up, "No follow-up required; record realized savings after billing verification.")

    def test_changed_or_moved_assignment_is_left_for_review(self) -> None:
        self.write_billing([["person@example.com", "SKU_ONE", "active", "microsoft365", USER_ID, "new-license"]])
        record, = self.verify()
        self.assertEqual(record.status, "assignment_changed")
        self.write_billing([["other@example.com", "SKU_ONE", "active", "microsoft365", USER_ID, LICENSE_ID]])
        record, = self.verify()
        self.assertEqual(record.status, "identity_mismatch")

    def test_rejects_incomplete_or_wrong_provider_snapshot(self) -> None:
        self.write_billing([])
        metadata = self.billing.parent / "import.json"
        metadata.write_text(json.dumps({
            "provider": "slack", "imported_at": "2026-10-12T10:00:00+00:00",
            "license_assignment_count": 0,
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "expected a microsoft365"):
            self.verify()
        metadata.write_text(json.dumps({
            "provider": "microsoft365", "imported_at": "2026-10-12T10:00:00+00:00",
            "license_assignment_count": 1,
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
