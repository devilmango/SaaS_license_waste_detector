from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from saas_sentry.import_health import build_import_health, format_import_health


class ImportHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, name: str, headers: list[str], rows: list[list[str]]) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def test_reports_identity_coverage_metadata_mismatch_and_source_freshness(self) -> None:
        hr = self.write_csv("hr.csv", ["employee", "email", "status"], [
            ["Alice", "alice@example.com", "active"], ["Bob", "bob@example.com", "terminated"],
        ])
        usage = self.write_csv("usage.csv", ["email", "application", "last_login"], [
            ["alice@example.com", "Slack", "2026-10-08"],
            ["unknown@example.com", "Slack", ""], ["", "Notion", ""],
        ])
        billing = self.write_csv("billing.csv", [
            "email", "application", "license_status", "annual_cost",
        ], [["alice@example.com", "Slack", "active", "12000"],
            ["missing@example.com", "Notion", "active", "1000"]])
        metadata = self.root / "import.json"
        metadata.write_text(json.dumps({
            "provider": "slack", "imported_at": "2026-10-08T10:00:00Z",
            "license_assignment_count": 1,
        }), encoding="utf-8")
        rows = build_import_health(
            hr, usage, billing, metadata_path=metadata, as_of=date(2026, 10, 8), stale_after_days=2,
        )
        health = {row.source: row for row in rows}
        self.assertEqual(health["usage"].matched_records, 1)
        self.assertEqual(health["usage"].identity_coverage_percent, "33.33")
        self.assertEqual(health["usage"].status, "issues")
        self.assertEqual(health["billing"].status, "issues")
        self.assertEqual(health["import_metadata"].status, "issues")
        self.assertEqual(health["hr"].freshness, "fresh")
        self.assertIn("identity_coverage_percent", format_import_health(rows))

    def test_marks_stale_imports_and_rejects_negative_threshold(self) -> None:
        hr = self.write_csv("hr.csv", ["employee", "email", "status"], [["A", "a@example.com", "active"]])
        usage = self.write_csv("usage.csv", ["email", "application", "last_login"], [["a@example.com", "App", ""]])
        billing = self.write_csv("billing.csv", ["email", "application", "license_status", "annual_cost"],
                                 [["a@example.com", "App", "active", "1"]])
        metadata = self.root / "import.json"
        metadata.write_text(json.dumps({
            "imported_at": "2026-10-01T00:00:00Z", "license_assignment_count": 1,
        }), encoding="utf-8")
        rows = build_import_health(hr, usage, billing, metadata_path=metadata,
                                   as_of=date(2026, 10, 8), stale_after_days=3)
        self.assertEqual({row.source: row for row in rows}["usage"].status, "stale")
        with self.assertRaisesRegex(ValueError, "non-negative"):
            build_import_health(hr, usage, billing, stale_after_days=-1)


if __name__ == "__main__":
    unittest.main()
