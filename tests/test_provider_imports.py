from __future__ import annotations

import csv
from argparse import Namespace
from contextlib import redirect_stdout
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from saas_sentry.connectors import ConnectorError, _open_with_rate_limit
from saas_sentry.cli import _import_diff, _import_history, _import_snapshots
from saas_sentry.provider_imports import list_import_snapshots, run_staged_import


def write_exports(folder: Path, provider: str = "test") -> tuple[int, int]:
    with (folder / "usage.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["employee", "employee_id", "email", "application", "last_login", "usage_count", "active_days", "usage_window_days", "features_used"])
        writer.writerow(["Alice", "E1", "alice@example.com", "Chat", "2026-10-01", "1", "1", "30", "1"])
    with (folder / "billing.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["employee_id", "email", "application", "license_status", "annual_cost", "currency", "contract_id", "renewal_date", "seat_minimum", "commitment_end_date", "notice_days", "bundle_group", "provider", "provider_user_id", "provider_license_id"])
        writer.writerow(["E1", "alice@example.com", "Chat", "active", "12000", "INR", "chat", "", "0", "", "0", "", provider, "u1", "sku1"])
    (folder / "import.json").write_text(json.dumps({"provider": provider, "user_count": 1, "license_assignment_count": 1, "read_only": True}), encoding="utf-8")
    return 1, 1


class ProviderImportSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "exports"
        self.output.mkdir()
        (self.output / "usage.csv").write_text("old usage\n", encoding="utf-8")
        (self.output / "billing.csv").write_text("old billing\n", encoding="utf-8")
        (self.output / "import.json").write_text('{"provider":"old"}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_import_validates_staging_and_publishes_complete_export_set(self) -> None:
        self.assertEqual(run_staged_import("test", self.output, write_exports), (1, 1))
        self.assertIn("alice@example.com", (self.output / "usage.csv").read_text())
        metadata = json.loads((self.output / "import.json").read_text())
        self.assertEqual(metadata["provider"], "test")
        with (self.output / "import-history.csv").open(encoding="utf-8", newline="") as handle:
            history = list(csv.DictReader(handle))
        self.assertEqual(history[-1]["status"], "succeeded")
        self.assertEqual(history[-1]["assignment_count"], "1")
        self.assertEqual(history[-1]["snapshot_version"], "1")
        self.assertEqual(history[-1]["added_assignments"], "1")
        snapshots = list_import_snapshots(self.output, "test")
        self.assertEqual(snapshots[0]["valid"], "true")
        self.assertEqual(snapshots[0]["published"], "true")

    def test_second_snapshot_reports_changed_assignment(self) -> None:
        run_staged_import("test", self.output, write_exports)

        def changed(stage: Path):
            write_exports(stage)
            billing = stage / "billing.csv"
            billing.write_text(billing.read_text().replace(",12000,", ",15000,"), encoding="utf-8")
            return 1, 1

        run_staged_import("test", self.output, changed)
        snapshot = list_import_snapshots(self.output, "test")[-1]
        self.assertEqual(snapshot["snapshot_version"], "2")
        self.assertEqual(snapshot["changed_assignments"], "1")
        diff_output = io.StringIO()
        with redirect_stdout(diff_output):
            self.assertEqual(_import_diff(Namespace(output_dir=self.output, provider="test", version=2, output=None)), 0)
        self.assertIn("changed,u1,chat", diff_output.getvalue())
        summary_output = io.StringIO()
        with redirect_stdout(summary_output):
            self.assertEqual(_import_snapshots(Namespace(output_dir=self.output, provider="test", output=None)), 0)
        self.assertIn("changed_assignments", summary_output.getvalue())

    def test_history_upgrade_preserves_older_import_rows(self) -> None:
        (self.output / "import-history.csv").write_text(
            "import_id,provider,started_at,finished_at,status,user_count,assignment_count,rate_limit_retries,error\n"
            "legacy,test,2026-01-01T00:00:00Z,2026-01-01T00:00:01Z,succeeded,1,1,0,\n",
            encoding="utf-8",
        )
        run_staged_import("test", self.output, write_exports)
        with (self.output / "import-history.csv").open(encoding="utf-8", newline="") as handle:
            history = list(csv.DictReader(handle))
        self.assertEqual([row["import_id"] for row in history], ["legacy", history[1]["import_id"]])
        self.assertEqual(history[0]["snapshot_version"], "")

    def test_valid_snapshot_is_kept_with_unpublished_status_when_export_publish_fails(self) -> None:
        with patch("saas_sentry.provider_imports._publish", side_effect=ConnectorError("disk error")):
            with self.assertRaisesRegex(ConnectorError, "disk error"):
                run_staged_import("test", self.output, write_exports)
        snapshot, = list_import_snapshots(self.output, "test")
        self.assertEqual(snapshot["valid"], "true")
        self.assertEqual(snapshot["published"], "false")
        self.assertEqual((self.output / "billing.csv").read_text(), "old billing\n")

    def test_failed_or_partial_import_keeps_previous_exports_and_records_failure(self) -> None:
        def incomplete(stage: Path):
            write_exports(stage)
            (stage / "billing.csv").write_text("header only\n", encoding="utf-8")
            return 1, 0

        with self.assertRaisesRegex(ConnectorError, "staged billing.csv"):
            run_staged_import("test", self.output, incomplete)
        self.assertEqual((self.output / "usage.csv").read_text(), "old usage\n")
        self.assertEqual((self.output / "billing.csv").read_text(), "old billing\n")
        with (self.output / "import-history.csv").open(encoding="utf-8", newline="") as handle:
            history = list(csv.DictReader(handle))
        self.assertEqual(history[-1]["status"], "failed")

    def test_rate_limit_retries_and_honors_retry_after(self) -> None:
        headers = Message()
        headers["Retry-After"] = "3"
        rate_limited = HTTPError("https://api.example.test", 429, "limited", headers, None)
        with patch("saas_sentry.connectors.urlopen", side_effect=[rate_limited, object()]) as opened, \
                patch("saas_sentry.connectors.time.sleep") as sleep:
            result = _open_with_rate_limit("https://api.example.test", "Demo", timeout=1)
        self.assertIsNotNone(result)
        self.assertEqual(opened.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_rate_limit_exhaustion_has_clear_diagnostic(self) -> None:
        headers = Message()
        headers["Retry-After"] = "0"
        limited = HTTPError("https://api.example.test", 429, "limited", headers, None)
        with patch("saas_sentry.connectors.urlopen", side_effect=limited), \
                patch("saas_sentry.connectors.time.sleep"):
            with self.assertRaisesRegex(ConnectorError, "rate limit"):
                _open_with_rate_limit("https://api.example.test", "Demo", timeout=1, max_attempts=2)

    def test_history_records_rate_limit_retry_count(self) -> None:
        headers = Message()
        headers["Retry-After"] = "0"
        limited = HTTPError("https://api.example.test", 429, "limited", headers, None)

        def operation(stage: Path):
            with patch("saas_sentry.connectors.urlopen", side_effect=[limited, object()]), \
                    patch("saas_sentry.connectors.time.sleep"):
                _open_with_rate_limit("https://api.example.test", "Demo", timeout=1)
            return write_exports(stage)

        run_staged_import("test", self.output, operation)
        with (self.output / "import-history.csv").open(encoding="utf-8", newline="") as handle:
            history = list(csv.DictReader(handle))
        self.assertEqual(history[-1]["rate_limit_retries"], "1")

    def test_import_history_cli_prints_recent_rows(self) -> None:
        (self.output / "import-history.csv").write_text(
            "import_id,provider,started_at,finished_at,status,user_count,assignment_count,rate_limit_retries,error\n"
            "run-1,test,2026-10-30T00:00:00Z,2026-10-30T00:00:01Z,succeeded,1,1,0,\n",
            encoding="utf-8",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(_import_history(Namespace(output_dir=self.output, output=None)), 0)
        self.assertIn("import_id,provider,started_at,finished_at,status", output.getvalue())


if __name__ == "__main__":
    unittest.main()
