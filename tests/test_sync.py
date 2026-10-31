from __future__ import annotations

from argparse import Namespace
import csv
from pathlib import Path
import tempfile
import json
import unittest
from unittest.mock import patch

from saas_sentry.cli import _run_scheduled, _sync_once, build_parser


class ScheduledSyncTests(unittest.TestCase):
    def test_sync_cli_accepts_timezone_schedule_retries_and_health_path(self) -> None:
        args = build_parser().parse_args([
            "sync", "--provider", "zoom", "--hr", "hr.csv", "--pricing", "pricing.csv",
            "--output-dir", "out", "--schedule-time", "08:15", "--timezone", "Asia/Kolkata",
            "--retries", "4", "--retry-delay", "2", "--health-file", "health.json",
        ])
        self.assertEqual(args.schedule_time, "08:15")
        self.assertEqual(args.timezone, "Asia/Kolkata")
        self.assertEqual(args.retries, 4)
        self.assertEqual(args.health_file, Path("health.json"))

    def test_scheduled_run_persists_health_after_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = Namespace(
                retries=1, retry_delay=0, once=True, timezone="UTC", health_file=None,
            )
            attempts = []

            def operation():
                attempts.append(1)
                if len(attempts) == 1:
                    raise RuntimeError("temporary provider error")

            _run_scheduled(args, operation, job="test import", output_dir=root)
            health = json.loads((root / "schedule-health.json").read_text(encoding="utf-8"))
            self.assertEqual(health["status"], "healthy")
            self.assertEqual(health["attempts"], 2)
            self.assertEqual(health["error"], "")

    def test_failed_one_shot_persists_failure_and_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = Namespace(
                retries=0, retry_delay=0, once=True, timezone="UTC", health_file=None,
            )
            with self.assertRaisesRegex(Exception, "failed after 1 attempt"):
                _run_scheduled(args, lambda: (_ for _ in ()).throw(RuntimeError("offline")),
                               job="test import", output_dir=root)
            health = json.loads((root / "schedule-health.json").read_text(encoding="utf-8"))
            self.assertEqual(health["status"], "failed")
            self.assertIn("offline", health["error"])
            self.assertIn("offline", (root / "schedule-failures.csv").read_text(encoding="utf-8"))

    def test_sync_cli_accepts_zoom_and_one_shot_schedule(self) -> None:
        args = build_parser().parse_args([
            "sync", "--provider", "zoom", "--hr", "hr.csv", "--pricing", "pricing.csv",
            "--output-dir", "out", "--once",
        ])
        self.assertEqual(args.provider, "zoom")
        self.assertTrue(args.once)

    def test_sync_cli_accepts_optional_automatic_reclaim_verification(self) -> None:
        args = build_parser().parse_args([
            "sync", "--provider", "google-workspace", "--hr", "hr.csv", "--pricing", "pricing.csv",
            "--output-dir", "out", "--action-ledger", "actions.csv",
            "--execution-ledger", "execution.csv", "--verification-ledger", "verified.csv",
        ])
        self.assertEqual(args.action_ledger, Path("actions.csv"))
        self.assertEqual(args.execution_ledger, Path("execution.csv"))
        self.assertEqual(args.verification_ledger, Path("verified.csv"))

    def test_sync_once_imports_then_analyzes_timestamped_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = Namespace(
                provider="zoom", output_dir=root / "exports", pricing=root / "pricing.csv",
                hr=root / "hr.csv", contract_pricing=None, config=None, review_file=None,
                organization=None, identity_map=None, customer="my_customer", lookback_days=90,
                team_id="",
            )
            with patch("saas_sentry.cli.ZoomConnector.from_environment") as factory, patch(
                "saas_sentry.cli._analyze", return_value=0,
            ) as analyze:
                factory.return_value.import_exports.side_effect = lambda *args, **kwargs: self.write_staged_exports(
                    *args, provider="zoom", **kwargs,
                )
                self.assertEqual(_sync_once(args), (2, 2))
            report_args = analyze.call_args.args[0]
            self.assertEqual(report_args.billing, root / "exports" / "billing.csv")
            self.assertEqual(report_args.usage, root / "exports" / "usage.csv")
            self.assertEqual(report_args.output_json.parent.parent, root / "exports" / "reports")

    def test_sync_verifies_reclaims_against_each_fresh_provider_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = Namespace(
                provider="google-workspace", output_dir=root / "exports", pricing=root / "pricing.csv",
                hr=root / "hr.csv", contract_pricing=None, config=None, review_file=None,
                organization=None, identity_map=None, customer="my_customer", lookback_days=90,
                team_id="", action_ledger=root / "actions.csv", execution_ledger=root / "execution.csv",
                verification_ledger=None,
            )
            with patch("saas_sentry.cli.GoogleWorkspaceConnector.from_environment") as factory, patch(
                "saas_sentry.cli._analyze", return_value=0,
            ), patch("saas_sentry.cli.verify_provider_reclaims", return_value=()) as verify:
                factory.return_value.import_exports.side_effect = lambda *args, **kwargs: self.write_staged_exports(
                    *args, provider="google_workspace", **kwargs,
                )
                self.assertEqual(_sync_once(args), (2, 2))
            verify.assert_called_once()
            self.assertIsNone(verify.call_args.args[2])
            self.assertEqual(verify.call_args.kwargs["provider"], "google_workspace")
            self.assertEqual(verify.call_args.args[3], root / "exports" / "billing.csv")
            self.assertEqual(verify.call_args.args[4], root / "exports" / "reclaim-verification-events.csv")

    @staticmethod
    def write_staged_exports(*args, provider, **_kwargs):
        output = args[-1]
        with (output / "usage.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["employee", "employee_id", "email", "application", "last_login", "usage_count", "active_days", "usage_window_days", "features_used"])
            writer.writerows([[f"Person {i}", f"E{i}", f"person{i}@example.com", "Chat", "", "0", "0", "30", "0"] for i in range(2)])
        with (output / "billing.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["employee_id", "email", "application", "license_status", "annual_cost", "currency", "contract_id", "renewal_date", "seat_minimum", "commitment_end_date", "notice_days", "bundle_group", "provider", "provider_user_id", "provider_license_id"])
            writer.writerows([[f"E{i}", f"person{i}@example.com", "Chat", "active", "12000", "INR", "chat", "", "0", "", "0", "", provider, f"u{i}", "sku"] for i in range(2)])
        (output / "import.json").write_text(json.dumps({"provider": provider, "user_count": 2,
                                  "license_assignment_count": 2, "read_only": True}), encoding="utf-8")
        return 2, 2


if __name__ == "__main__":
    unittest.main()
