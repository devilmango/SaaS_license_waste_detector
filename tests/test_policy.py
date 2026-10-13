from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from saas_sentry.engine import CurrencyConfig, InputError, Rules, analyze, load_config, load_inputs
from saas_sentry.policy import (
    active_exception_keys,
    approve_exception,
    format_exceptions,
    list_exceptions,
    request_exception,
    revoke_exception,
)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hr = self.root / "hr.csv"
        self.usage = self.root / "usage.csv"
        self.billing = self.root / "billing.csv"
        self.config = self.root / "policy.toml"
        self.hr.write_text(
            "employee,employee_id,email,status,terminated_on\n"
            "Alice,E1,alice@example.com,active,\n"
            "Bob,E2,bob@example.com,terminated,2026-10-01\n", encoding="utf-8",
        )
        self.usage.write_text(
            "employee,employee_id,email,application,last_login,usage_count\n"
            "Alice,E1,alice@example.com,Slack,2026-07-01,1\n"
            "Bob,E2,bob@example.com,Slack,,\n", encoding="utf-8",
        )
        self.billing.write_text(
            "employee_id,email,application,license_status,annual_cost,currency,contract_id\n"
            "E1,alice@example.com,Slack,active,12000,INR,slack-1\n"
            "E2,bob@example.com,Slack,active,12000,INR,slack-1\n", encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_configured_protected_users_and_grace_periods_suppress_findings(self) -> None:
        self.config.write_text(
            "[rules]\nstale_days=90\n"
            "[policy]\ninactivity_grace_days=30\ntermination_grace_days=7\n"
            'protected_emails=["alice@example.com"]\nprotected_employee_ids=[]\n',
            encoding="utf-8",
        )
        rules, currency = load_config(self.config)
        findings = analyze(
            load_inputs(self.hr, self.usage, self.billing, currency=currency),
            as_of=date(2026, 10, 4), rules=rules, currency=currency,
        )
        self.assertEqual(findings.findings, ())
        issue_kinds = {issue.issue for issue in findings.issues}
        self.assertEqual(issue_kinds, {"protected_user", "termination_grace_period"})

    def test_time_limited_exception_requires_separate_approval_and_can_be_revoked(self) -> None:
        ledger = self.root / "exceptions.csv"
        exception_id = request_exception(
            ledger, employee_id="E1", email="ALICE@example.com", application="Slack",
            reason="Temporary migration", requested_by="alice@example.com",
            expires_on=date(2026, 10, 20), evidence_ref="ticket/123",
            on=date(2026, 10, 1),
        )
        self.assertEqual(active_exception_keys(ledger, as_of=date(2026, 10, 4)), set())
        with self.assertRaisesRegex(InputError, "differ from the requester"):
            approve_exception(ledger, exception_id, approved_by="alice@example.com",
                              evidence_ref="approval/123", on=date(2026, 10, 2))
        approve_exception(ledger, exception_id, approved_by="security@example.com",
                          evidence_ref="approval/123", on=date(2026, 10, 2))
        keys = active_exception_keys(ledger, as_of=date(2026, 10, 20))
        self.assertIn(("e1", "alice@example.com", "slack"), keys)
        self.assertNotIn(("e1", "alice@example.com", "slack"), active_exception_keys(ledger, as_of=date(2026, 10, 21)))
        self.assertEqual(list_exceptions(ledger, as_of=date(2026, 10, 21))[0].status, "expired")
        revoke_exception(ledger, exception_id, revoked_by="security@example.com", note="Migration complete",
                         on=date(2026, 10, 5))
        row, = list_exceptions(ledger, as_of=date(2026, 10, 6))
        self.assertEqual(row.status, "revoked")
        self.assertEqual(row.revoked_by, "security@example.com")
        self.assertIn("revoked_by", format_exceptions([row]).splitlines()[0])

    def test_analysis_honors_only_matching_application_exception(self) -> None:
        data = load_inputs(self.hr, self.usage, self.billing)
        result = analyze(
            data, as_of=date(2026, 10, 4), rules=Rules(),
            currency=CurrencyConfig(rates_to_inr={"INR": 1}),
            active_exceptions={("e1", "alice@example.com", "slack")},
        )
        self.assertEqual({row.employee for row in result.findings}, {"Bob"})
        self.assertIn("active_exception", {issue.issue for issue in result.issues})

    def test_exception_with_conflicting_id_and_email_does_not_suppress_either_identity(self) -> None:
        result = analyze(
            load_inputs(self.hr, self.usage, self.billing), as_of=date(2026, 10, 4),
            active_exceptions={("e1", "bob@example.com", "slack")},
        )
        self.assertEqual({row.employee for row in result.findings}, {"Alice", "Bob"})

    def test_policy_lists_must_be_arrays_of_nonempty_strings(self) -> None:
        self.config.write_text('[policy]\nprotected_emails="admin@example.com"\n', encoding="utf-8")
        with self.assertRaisesRegex(InputError, "must be arrays"):
            load_config(self.config)


if __name__ == "__main__":
    unittest.main()
