from __future__ import annotations

import csv
from contextlib import redirect_stdout
from io import StringIO
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from saas_sentry.actions import ActionError, propose_action, transition_action
from saas_sentry.cli import main
from saas_sentry.execution import build_execution_plan, execute_plan, resolve_execution


USER_ID = "11111111-1111-4111-8111-111111111111"
LICENSE_ID = "22222222-2222-4222-8222-222222222222"


class ProviderExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.findings = self.write_csv("findings.csv", [
            "finding_id", "review_status", "email", "application",
            "contract_adjusted_annual_opportunity_inr",
        ], [["finding-1", "confirmed", "person@example.com", "SKU_ONE", "12000"]])
        self.billing = self.write_csv("billing.csv", [
            "email", "application", "license_status", "provider", "provider_user_id", "provider_license_id",
        ], [["person@example.com", "SKU_ONE", "active", "microsoft365", USER_ID, LICENSE_ID]])
        self.actions = self.root / "actions.csv"
        self.action_id = propose_action(
            self.findings, self.actions, "finding-1", owner="FinOps", proposed_by="analyst@example.com",
            proposed_on=date(2026, 10, 1),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, name: str, headers: list[str], rows: list[list[str]]) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def approve(self) -> None:
        transition_action(self.actions, self.action_id, "approved", actor="manager@example.com")

    def test_preview_plan_requires_approval_and_exact_provider_target(self) -> None:
        with self.assertRaisesRegex(ActionError, "Only approved"):
            build_execution_plan(self.actions, self.findings, self.billing, self.action_id)
        self.approve()
        plan = build_execution_plan(self.actions, self.findings, self.billing, self.action_id)
        self.assertEqual(plan.provider_user_id, USER_ID)
        self.assertEqual(plan.provider_license_id, LICENSE_ID)
        self.assertEqual(len(plan.idempotency_key), 64)

    def test_cli_preview_is_non_mutating_and_does_not_require_provider_credentials(self) -> None:
        self.approve()
        execution_log = self.root / "execution.csv"
        args = [
            "saas-sentry", "actions", "execute", "--findings", str(self.findings),
            "--billing", str(self.billing), "--file", str(self.actions),
            "--execution-ledger", str(execution_log), "--action-id", self.action_id,
            "--executed-by", "operator@example.com",
        ]
        output = StringIO()
        with patch("sys.argv", args), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn("Preview only", output.getvalue())
        self.assertFalse(execution_log.exists())

    def test_execution_is_audited_and_idempotent(self) -> None:
        self.approve()
        plan = build_execution_plan(self.actions, self.findings, self.billing, self.action_id)
        execution_log = self.root / "execution.csv"
        operations = []
        self.assertEqual(execute_plan(
            plan, execution_log, actor="operator@example.com",
            operation=lambda target: operations.append(target.idempotency_key) or "license_removed",
        ), "license_removed")
        self.assertEqual(operations, [plan.idempotency_key])
        with execution_log.open(encoding="utf-8", newline="") as handle:
            events = list(csv.DictReader(handle))
        self.assertEqual([event["status"] for event in events], ["started", "succeeded"])
        self.assertEqual(events[-1]["actor"], "operator@example.com")
        with self.assertRaisesRegex(ActionError, "already has an execution record"):
            execute_plan(plan, execution_log, actor="operator@example.com", operation=lambda _target: self.fail("must not retry"))
        self.assertFalse(execution_log.with_name("execution.csv.lock").exists())

    def test_provider_failure_is_recorded_and_retry_is_blocked(self) -> None:
        self.approve()
        plan = build_execution_plan(self.actions, self.findings, self.billing, self.action_id)
        execution_log = self.root / "execution.csv"

        def failure(_target):
            raise RuntimeError("sanitized provider failure")

        with self.assertRaisesRegex(ActionError, "was recorded"):
            execute_plan(plan, execution_log, actor="operator", operation=failure)
        with execution_log.open(encoding="utf-8", newline="") as handle:
            events = list(csv.DictReader(handle))
        self.assertEqual(events[-1]["status"], "failed")
        with self.assertRaisesRegex(ActionError, "already has an execution record"):
            execute_plan(plan, execution_log, actor="operator", operation=failure)

    def test_independent_reconciliation_can_safely_enable_retry(self) -> None:
        self.approve()
        plan = build_execution_plan(self.actions, self.findings, self.billing, self.action_id)
        execution_log = self.root / "execution.csv"

        def failure(_target):
            raise RuntimeError("network timeout")

        with self.assertRaisesRegex(ActionError, "was recorded"):
            execute_plan(plan, execution_log, actor="operator", operation=failure)
        with self.assertRaisesRegex(ActionError, "different person"):
            resolve_execution(
                self.actions, execution_log, self.action_id, resolved_as="not_applied",
                actor="operator", note="Confirmed assignment remains active",
            )
        resolve_execution(
            self.actions, execution_log, self.action_id, resolved_as="not_applied",
            actor="reviewer", note="Confirmed assignment remains active in Microsoft 365",
        )
        self.assertEqual(execute_plan(
            plan, execution_log, actor="operator",
            operation=lambda _target: "license_removed",
        ), "license_removed")
        with execution_log.open(encoding="utf-8", newline="") as handle:
            events = list(csv.DictReader(handle))
        self.assertEqual(
            [event["status"] for event in events],
            ["started", "failed", "reconciled_not_applied", "started", "succeeded"],
        )

    def test_ambiguous_billing_target_is_rejected(self) -> None:
        self.approve()
        with self.billing.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([
                "person@example.com", "SKU_ONE", "active", "microsoft365", USER_ID, LICENSE_ID,
            ])
        with self.assertRaisesRegex(ActionError, "exactly one is required"):
            build_execution_plan(self.actions, self.findings, self.billing, self.action_id)

    def test_google_and_github_provider_targets_support_opaque_ids(self) -> None:
        self.approve()
        cases = [
            ("google_workspace", "person@example.com", '["Google-Apps", "Business"]'),
            ("github_copilot", "octocat", "example-org"),
        ]
        for provider, user_id, license_id in cases:
            with self.subTest(provider=provider):
                self.write_csv("billing.csv", [
                    "email", "application", "license_status", "provider", "provider_user_id", "provider_license_id",
                ], [["person@example.com", "SKU_ONE", "active", provider, user_id, license_id]])
                plan = build_execution_plan(self.actions, self.findings, self.billing, self.action_id, provider=provider)
                self.assertEqual((plan.provider_user_id, plan.provider_license_id), (user_id, license_id))


if __name__ == "__main__":
    unittest.main()
