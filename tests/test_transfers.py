from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from saas_sentry.engine import InputError
from saas_sentry.reassignment_workflow import stage_plan, transition
from saas_sentry.transfers import TRANSFER_FIELDS, build_transfer_plan, execute_transfer, verify_transfer_snapshot


class ProviderTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan_csv = self.root / "plan.csv"
        self.plan_csv.write_text(
            "finding_id,application,contract_id,from_email,to_email,status\n"
            "f-1,Slack,c-1,former@example.com,new@example.com,proposed\n",
            encoding="utf-8",
        )
        self.events = self.root / "reassignments.csv"
        stage_plan(self.plan_csv, self.events, actor="planner", on=date(2026, 10, 1))
        transition(self.events, "f-1", "approved", actor="approver", evidence_ref="approval/1", on=date(2026, 10, 2))
        self.billing = self.root / "billing.csv"
        self.billing.write_text(
            "email,application,license_status,contract_id,provider,provider_user_id,provider_license_id\n"
            "former@example.com,Slack,active,c-1,slack,U1,T1\n",
            encoding="utf-8",
        )
        self.ledger = self.root / "transfers.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plan_resolves_an_approved_source_assignment_without_mutating(self) -> None:
        plan = build_transfer_plan(self.events, self.billing, "f-1", provider="slack")
        self.assertEqual(plan.source_user_id, "U1")
        self.assertEqual(plan.target_email, "new@example.com")
        self.assertEqual(plan.license_id, "T1")
        self.assertFalse(self.ledger.exists())

    def test_execution_is_idempotent_and_requires_approved_plan(self) -> None:
        plan = build_transfer_plan(self.events, self.billing, "f-1", provider="slack")
        calls: list[str] = []
        result = execute_transfer(plan, self.ledger, actor="executor", operation=lambda p: calls.append(p.target_email) or "verified")
        self.assertEqual(result, "verified")
        self.assertEqual(calls, ["new@example.com"])
        with self.assertRaises(InputError):
            execute_transfer(plan, self.ledger, actor="executor", operation=lambda p: calls.append("duplicate") or "ok")
        self.assertEqual(calls, ["new@example.com"])

    def test_failed_provider_operation_is_not_blindly_retried(self) -> None:
        plan = build_transfer_plan(self.events, self.billing, "f-1", provider="slack")

        def ambiguous(_plan):
            raise RuntimeError("timed out after provider accepted request")

        with self.assertRaises(InputError):
            execute_transfer(plan, self.ledger, actor="executor", operation=ambiguous)
        with self.assertRaises(InputError):
            execute_transfer(plan, self.ledger, actor="executor", operation=lambda _plan: "retry")

    def test_verification_requires_complete_import_and_confirms_both_assignment_sides(self) -> None:
        plan = build_transfer_plan(self.events, self.billing, "f-1", provider="slack")
        execute_transfer(plan, self.ledger, actor="executor", operation=lambda _plan: "provider request accepted")
        fresh = self.root / "fresh"
        fresh.mkdir()
        (fresh / "import.json").write_text(
            json.dumps({"provider":"slack","imported_at":(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),"license_assignment_count":1}),
            encoding="utf-8",
        )
        (fresh / "billing.csv").write_text(
            "email,application,license_status,contract_id,provider,provider_user_id,provider_license_id\n"
            "new@example.com,Slack,active,c-1,slack,U2,T1\n",
            encoding="utf-8",
        )
        result = verify_transfer_snapshot(self.ledger, fresh / "billing.csv", provider="slack")
        self.assertEqual(result[0].status, "verified_transferred")
        self.assertEqual(result[0].target_user_id, "U2")

    def test_verification_rejects_incomplete_provider_snapshot(self) -> None:
        plan = build_transfer_plan(self.events, self.billing, "f-1", provider="slack")
        execute_transfer(plan, self.ledger, actor="executor", operation=lambda _plan: "accepted")
        with self.assertRaises(InputError):
            verify_transfer_snapshot(self.ledger, self.billing, provider="slack")

    def test_verification_rejects_snapshot_that_predates_transfer(self) -> None:
        plan = build_transfer_plan(self.events, self.billing, "f-1", provider="slack")
        execute_transfer(plan, self.ledger, actor="executor", operation=lambda _plan: "accepted")
        imported = self.root / "stale"
        imported.mkdir()
        (imported / "import.json").write_text(
            json.dumps({"provider":"slack","imported_at":"2020-01-01T00:00:00+00:00","license_assignment_count":1}),
            encoding="utf-8",
        )
        (imported / "billing.csv").write_text(self.billing.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(InputError, "predates"):
            verify_transfer_snapshot(self.ledger, imported / "billing.csv", provider="slack")

    def test_fresh_snapshot_can_reconcile_an_interrupted_started_attempt(self) -> None:
        plan = build_transfer_plan(self.events, self.billing, "f-1", provider="slack")
        with self.ledger.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRANSFER_FIELDS)
            writer.writeheader()
            writer.writerow({**plan.__dict__, "target_user_id":"", "status":"started", "actor":"executor",
                             "occurred_at":datetime.now(timezone.utc).isoformat(), "result":"in progress"})
        fresh = self.root / "interrupted-fresh"
        fresh.mkdir()
        (fresh / "import.json").write_text(
            json.dumps({"provider":"slack","imported_at":(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),"license_assignment_count":1}),
            encoding="utf-8",
        )
        (fresh / "billing.csv").write_text(
            "email,application,license_status,contract_id,provider,provider_user_id,provider_license_id\n"
            "new@example.com,Slack,active,c-1,slack,U2,T1\n", encoding="utf-8",
        )
        result = verify_transfer_snapshot(self.ledger, fresh / "billing.csv", provider="slack")
        self.assertEqual(result[0].status, "verified_transferred")


if __name__ == "__main__":
    unittest.main()
