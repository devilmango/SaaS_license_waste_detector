from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from saas_sentry.dashboard import load_dashboard_users, route_request
from saas_sentry.dashboard_auth import Principal


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

    def test_reviewer_can_record_decision_but_viewer_cannot(self) -> None:
        report = json.loads(self.get("/api/report")[2])
        finding_id = report["findings"][0]["finding_id"]
        self.options["review_file"] = self.root / "reviews.csv"
        payload = json.dumps({"finding_id": finding_id, "status": "confirmed", "note": "Reviewed"}).encode()
        self.assertEqual(route_request("POST", "/api/reviews", self.options, role="viewer", actor="viewer", body=payload)[0], 403)
        status, _, body = route_request("POST", "/api/reviews", self.options, role="reviewer", actor="owner@example.com", body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["reviewed_by"], "owner@example.com")
        self.assertIn("confirmed", self.options["review_file"].read_text())

    def test_dashboard_users_are_loaded_from_environment(self) -> None:
        with patch.dict("os.environ", {"TEST_DASH_USERS": '[{"actor":"reviewer@example.com","role":"reviewer","token":"abcdefghijklmnopqrstuvwxyz123456","applications":["Slack"]}]'}):
            users = load_dashboard_users("TEST_DASH_USERS")
        self.assertEqual(users["abcdefghijklmnopqrstuvwxyz123456"]["role"], "reviewer")
        self.assertEqual(users["abcdefghijklmnopqrstuvwxyz123456"]["applications"], ["Slack"])

    def test_campaigns_can_be_created_listed_and_decided_in_dashboard_api(self) -> None:
        report = json.loads(self.get("/api/report")[2])
        finding_id = report["findings"][0]["finding_id"]
        self.options["review_file"] = self.root / "reviews.csv"
        self.options["campaign_ledger"] = self.root / "campaigns.csv"
        owner = Principal("owner@example.com", "reviewer")
        payload = json.dumps({"due_on": "2026-10-30", "campaign_id": "october", "assignments": {finding_id: "owner@example.com"}}).encode()
        status, _, created = route_request("POST", "/api/campaigns", self.options, principal=owner, body=payload)
        self.assertEqual(status, 200, created)
        self.assertEqual(json.loads(created)["assigned"], 1)
        status, _, listed = route_request("GET", "/api/campaigns", self.options, principal=owner)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(listed)["campaigns"][0]["owner"], "owner@example.com")
        decision = json.dumps({"campaign_id": "october", "finding_id": finding_id, "decision": "confirmed"}).encode()
        status, _, _ = route_request("POST", "/api/campaigns/decision", self.options, principal=owner, body=decision)
        self.assertEqual(status, 200)

    def test_scoped_principal_cannot_create_campaign_for_other_application(self) -> None:
        self.options["campaign_ledger"] = self.root / "campaigns.csv"
        scoped = Principal("owner@example.com", "reviewer", applications=("Notion",), restricted=True)
        status, _, _ = route_request("POST", "/api/campaigns", self.options, principal=scoped,
                                     body=b'{"due_on":"2026-10-30","default_owner":"owner@example.com"}')
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
