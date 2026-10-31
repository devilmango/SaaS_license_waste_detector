from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal

from saas_sentry.dashboard import load_dashboard_users, route_request
from saas_sentry.dashboard_auth import Principal
from saas_sentry.actions import propose_action, transition_action


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
        self.assertIn(b"Action-level invoice attribution", html)
        self.assertIn(b"Identity match review", html)

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

    def test_admin_can_create_edit_pause_run_and_review_schedule_history(self) -> None:
        self.options["schedule_db"] = self.root / "dashboard.sqlite"
        admin = Principal("admin@example.com", "admin")
        status, _, body = route_request(
            "POST", "/api/schedules", self.options, principal=admin,
            body=b'{"name":"Daily portfolio","schedule_time":"08:30","timezone":"Asia/Kolkata"}',
        )
        self.assertEqual(status, 201, body)
        schedule = json.loads(body)["schedule"]
        schedule_id = schedule["schedule_id"]
        self.assertEqual(schedule["timezone"], "Asia/Kolkata")

        status, _, body = route_request(
            "POST", f"/api/schedules/{schedule_id}", self.options, principal=admin,
            body=b'{"name":"Morning portfolio","schedule_time":"09:00","timezone":"UTC"}',
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["schedule"]["name"], "Morning portfolio")

        status, _, body = route_request(
            "POST", f"/api/schedules/{schedule_id}/pause", self.options, principal=admin,
            body=b'{"paused":true}',
        )
        self.assertEqual(status, 200, body)
        self.assertFalse(json.loads(body)["schedule"]["enabled"])

        status, _, body = route_request(
            "POST", f"/api/schedules/{schedule_id}/run", self.options, principal=admin,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["run"]["status"], "succeeded")

        status, _, body = route_request("GET", "/api/schedules", self.options, principal=admin)
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result["schedules"][0]["name"], "Morning portfolio")
        self.assertEqual(result["schedules"][0]["last_result"], "succeeded")
        self.assertEqual(len(result["schedules"][0]["history"]), 1)

    def test_schedule_management_requires_admin_permission(self) -> None:
        self.options["schedule_db"] = self.root / "dashboard.sqlite"
        status, _, _ = route_request(
            "POST", "/api/schedules", self.options, principal=Principal("reviewer", "reviewer"),
            body=b'{"name":"Daily","schedule_time":"08:30","timezone":"UTC"}',
        )
        self.assertEqual(status, 403)

    def test_dashboard_exposes_scoped_period_savings_outcomes(self) -> None:
        findings = self.root / "outcome-findings.csv"
        findings.write_text(
            "finding_id,review_status,contract_adjusted_annual_opportunity_inr,application,contract_id,cost_center,review_owner\n"
            "outcome-1,confirmed,12000,Slack,slack,ENG,owner@example.com\n", encoding="utf-8",
        )
        actions = self.root / "outcome-actions.csv"
        action = propose_action(findings, actions, "outcome-1", owner="owner@example.com", proposed_by="analyst")
        transition_action(actions, action, "approved", actor="manager")
        transition_action(actions, action, "reclaimed", actor="operator",
                          actual_annual_savings_inr=Decimal("10000"))
        baseline = self.root / "outcome-baseline.csv"
        baseline.write_text(
            "application,cost_center,billing_period_start,billing_period_end,actual_billed_cost_inr\n"
            "Slack,ENG,2026-08-01,2026-09-01,30000\n", encoding="utf-8",
        )
        current = self.root / "outcome-current.csv"
        current.write_text(
            "application,cost_center,billing_period_start,billing_period_end,actual_billed_cost_inr\n"
            "Slack,ENG,2026-09-01,2026-10-01,20000\n", encoding="utf-8",
        )
        self.options.update({
            "outcomes_actions": actions, "outcomes_findings": findings,
            "invoice_baseline": baseline, "invoice_current": current,
        })
        status, _, body = route_request(
            "GET", "/api/savings-outcomes", self.options,
            principal=Principal("admin@example.com", "admin"),
        )
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(result["configured"])
        self.assertEqual(result["outcomes"][0]["owner"], "owner@example.com")
        self.assertEqual(result["outcomes"][0]["period"], "2026-09-01/2026-10-01")
        self.assertEqual(result["attributions"][0]["confidence"], "low")
        self.assertFalse(result["attributions"][0]["savings_verified"])

    def test_identity_match_review_queue_records_reusable_dashboard_correction(self) -> None:
        hr = self.root / "identity-hr.csv"
        hr.write_text("employee,employee_id,email,status\nAlice,E1,alice@example.com,active\n", encoding="utf-8")
        identities = self.root / "directory.csv"
        identities.write_text(
            "employee_id,email,display_name,source,provider_user_id,active,app_access_json,provisioning_state\n"
            "OLD,alice.old@example.com,Alice,scim,scim-1,true,[\"Slack\"],provisioned\n", encoding="utf-8",
        )
        self.options.update({"hr_identities": hr, "directory_identities": identities,
                             "identity_corrections": self.root / "identity-corrections.csv"})
        reviewer = Principal("reviewer@example.com", "reviewer")
        status, _, body = route_request("GET", "/api/identity-review", self.options, principal=reviewer)
        self.assertEqual(status, 200, body)
        queue = json.loads(body)["items"]
        candidate = next(c for row in queue for c in row["candidates"])
        out_of_scope = Principal("other@example.com", "reviewer", applications=("Notion",), restricted=True)
        status, _, body = route_request("GET", "/api/identity-review", self.options, principal=out_of_scope)
        self.assertEqual(json.loads(body)["items"], [])
        denied_payload = json.dumps(candidate | {"decision": "map", "employee_id": "E1"}).encode()
        self.assertEqual(route_request("POST", "/api/identity-review/correction", self.options,
                                       principal=out_of_scope, body=denied_payload)[0], 403)
        payload = json.dumps(candidate | {"decision": "map", "employee_id": "E1"}).encode()
        status, _, body = route_request("POST", "/api/identity-review/correction", self.options,
                                        principal=reviewer, body=payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["correction"]["employee_id"], "E1")
        status, _, body = route_request("GET", "/api/identity-review", self.options, principal=reviewer)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["items"], [])
        _, _, history = route_request("GET", "/api/identity-review", self.options, principal=reviewer)
        self.assertEqual(len(json.loads(history)["corrections"]), 1)


if __name__ == "__main__":
    unittest.main()
