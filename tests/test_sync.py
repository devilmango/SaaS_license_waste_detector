from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from saas_sentry.cli import _sync_once, build_parser


class ScheduledSyncTests(unittest.TestCase):
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
                factory.return_value.import_exports.return_value = (3, 2)
                self.assertEqual(_sync_once(args), (3, 2))
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
                factory.return_value.import_exports.return_value = (3, 2)
                self.assertEqual(_sync_once(args), (3, 2))
            verify.assert_called_once()
            self.assertIsNone(verify.call_args.args[2])
            self.assertEqual(verify.call_args.kwargs["provider"], "google_workspace")
            self.assertEqual(verify.call_args.args[3], root / "exports" / "billing.csv")
            self.assertEqual(verify.call_args.args[4], root / "exports" / "reclaim-verification-events.csv")


if __name__ == "__main__":
    unittest.main()
