from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from saas_sentry.dashboard import route_request


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hr = self.root / "hr.csv"
        self.usage = self.root / "usage.csv"
        self.billing = self.root / "billing.csv"
        self.hr.write_text("employee,employee_id,email,status\nAlice,E1,alice@example.com,active\n", encoding="utf-8")
        self.usage.write_text(
            "employee,employee_id,email,application,last_login,usage_count\n"
            "Alice,E1,alice@example.com,Slack,2026-01-01,1\n", encoding="utf-8",
        )
        self.billing.write_text(
            "employee_id,email,application,license_status,annual_cost,currency,contract_id\n"
            "E1,alice@example.com,Slack,active,12000,INR,slack-1\n", encoding="utf-8",
        )
        self.options = {
            "hr": self.hr, "usage": self.usage, "billing": self.billing,
            "as_of": date(2026, 10, 4),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def get(self, path: str):
        return route_request("GET", path, self.options)

    def test_dashboard_and_read_only_json_api(self) -> None:
        status, content_type, html = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"SaaS Sentry", html)
        self.assertIn(b"/api/report", html)

        status, content_type, body = self.get("/api/report")
        report = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(report["as_of"], "2026-10-04")
        self.assertEqual(len(report["findings"]), 1)
        self.assertIn("application/json", content_type)

        _, _, page = self.get("/api/findings?limit=1&offset=0")
        self.assertEqual(json.loads(page)["total"], 1)
        _, _, health = self.get("/api/health")
        self.assertEqual(json.loads(health)["status"], "ok")

    def test_api_rejects_mutations_and_unknown_routes(self) -> None:
        self.assertEqual(route_request("POST", "/api/report", self.options)[0], 405)
        self.assertEqual(route_request("GET", "/api/not-a-route", self.options)[0], 404)


if __name__ == "__main__":
    unittest.main()
