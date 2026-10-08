from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from saas_sentry.connectors import (
    GitHubCopilotConnector,
    GoogleWorkspaceConnector,
    SlackConnector,
    ZoomConnector,
)


class ProviderConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, path: Path, headers: list[str], rows: list[list[str]]) -> Path:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def read_csv(self, path: Path) -> list[dict[str, str]]:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_google_workspace_imports_only_assigned_mapped_skus_with_gets(self) -> None:
        pricing = self.write_csv(
            self.root / "google-pricing.csv",
            ["product_id", "sku_id", "application", "annual_cost", "currency"],
            [["Google-Apps", "Google-Apps-For-Business", "Workspace Business", "18000", "INR"]],
        )
        connector = GoogleWorkspaceConnector("token")

        def response(url, *_args, **_kwargs):
            if "admin/directory" in url:
                return {"users": [{
                    "primaryEmail": "person@example.com", "name": {"fullName": "Person Example"},
                    "lastLoginTime": "2026-10-01T10:00:00Z",
                }]}
            if "licensing.googleapis.com" in url:
                return {"items": [{"userId": "person@example.com", "skuId": "Google-Apps-For-Business"}]}
            self.fail(f"Unexpected request {url}")

        output = self.root / "google-output"
        with patch("saas_sentry.connectors._bearer_json", side_effect=response) as mocked:
            result = connector.import_exports(pricing, output)
        self.assertEqual(result, (1, 1))
        self.assertTrue(all(call.args[0].startswith("https://") for call in mocked.call_args_list))
        self.assertEqual(self.read_csv(output / "usage.csv")[0]["last_login"], "2026-10-01")
        google_license = self.read_csv(output / "billing.csv")[0]
        self.assertEqual(google_license["provider"], "google_workspace")
        self.assertEqual(google_license["provider_user_id"], "person@example.com")
        self.assertEqual(json.loads(google_license["provider_license_id"]), [
            "Google-Apps", "Google-Apps-For-Business",
        ])
        self.assertTrue(json.loads((output / "import.json").read_text())["read_only"])

    def test_slack_import_uses_daily_member_analytics_without_presence_guessing(self) -> None:
        pricing = self.write_csv(self.root / "slack-pricing.csv", ["application", "annual_cost"], [
            ["Slack Business+", "24000"],
        ])
        connector = SlackConnector("token")
        connector._members = lambda _team="": [{
            "id": "U1", "real_name": "Person Example", "profile": {"email": "person@example.com"},
        }]
        rows_by_day = {
            date(2026, 10, 4): [{"user_id": "U1", "is_billable_seat": True, "is_active": True,
                                "messages_posted_count": 2}],
            date(2026, 10, 5): [{"user_id": "U1", "is_billable_seat": True, "is_active": False,
                                "messages_posted_count": 0}],
        }
        connector._daily_analytics = lambda day: rows_by_day[day]
        output = self.root / "slack-output"
        users, assignments = connector.import_exports(
            pricing, output, lookback_days=2, as_of=date(2026, 10, 5),
        )
        usage = self.read_csv(output / "usage.csv")[0]
        self.assertEqual((users, assignments), (1, 1))
        self.assertEqual(usage["last_login"], "2026-10-04")
        self.assertEqual(usage["active_days"], "1")
        self.assertEqual(usage["usage_count"], "2")

    def test_github_copilot_import_requires_identity_mapping(self) -> None:
        pricing = self.write_csv(self.root / "github-pricing.csv", ["application", "annual_cost"], [
            ["Copilot Business", "24000"],
        ])
        identities = self.write_csv(self.root / "github-identities.csv", ["login", "email", "employee_id"], [
            ["octocat", "octo@example.com", "E-1"],
        ])
        output = self.root / "github-output"
        with patch("saas_sentry.connectors._bearer_json", return_value={"seats": [{
            "assignee": {"login": "octocat"}, "last_activity_at": "2026-09-30T10:00:00Z",
        }]}) as mocked:
            result = GitHubCopilotConnector("token").import_exports("example-org", pricing, identities, output)
        self.assertEqual(result, (1, 1))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.read_csv(output / "usage.csv")[0]["email"], "octo@example.com")
        self.assertEqual(self.read_csv(output / "usage.csv")[0]["last_login"], "2026-09-30")
        github_license = self.read_csv(output / "billing.csv")[0]
        self.assertEqual(github_license["provider"], "github_copilot")
        self.assertEqual(github_license["provider_user_id"], "octocat")
        self.assertEqual(github_license["provider_license_id"], "example-org")

    def test_google_workspace_reclaim_verifies_target_before_delete(self) -> None:
        connector = GoogleWorkspaceConnector("token")
        url = "https://licensing.googleapis.com/apps/licensing/v1/product/Google-Apps/sku/Business/user/person%40example.com"
        with patch("saas_sentry.connectors._bearer_json", return_value={
            "productId": "Google-Apps", "skuId": "Business", "userId": "person@example.com",
        }) as verify, patch("saas_sentry.connectors._bearer_mutation") as delete:
            result = connector.remove_user_license("person@example.com", '["Google-Apps", "Business"]')
        self.assertEqual(result, "license_revoked")
        verify.assert_called_once_with(url, "token", "Google Workspace", connector.HOSTS)
        delete.assert_called_once_with(url, "token", "Google Workspace", connector.HOSTS, method="DELETE")

    def test_github_copilot_reclaim_targets_only_selected_username(self) -> None:
        connector = GitHubCopilotConnector("token")
        with patch("saas_sentry.connectors._bearer_json", return_value={"seats": [
            {"assignee": {"login": "octocat"}},
        ]}) as verify, patch("saas_sentry.connectors._bearer_mutation") as mutation:
            result = connector.remove_user_license("octocat", "example-org")
        self.assertEqual(result, "seat_cancellation_requested")
        self.assertIn("/copilot/billing/seats?", verify.call_args.args[0])
        request = mutation.call_args.args[0]
        self.assertIn("/orgs/example-org/copilot/billing/selected_users", request)
        self.assertEqual(mutation.call_args.kwargs["method"], "DELETE")
        self.assertEqual(json.loads(mutation.call_args.kwargs["body"]), {"selected_usernames": ["octocat"]})

    def test_github_copilot_does_not_cancel_missing_seat(self) -> None:
        connector = GitHubCopilotConnector("token")
        with patch("saas_sentry.connectors._bearer_json", return_value={"seats": []}) as verify, \
                patch("saas_sentry.connectors._bearer_mutation") as mutation:
            result = connector.remove_user_license("octocat", "example-org")
        self.assertEqual(result, "seat_already_absent")
        verify.assert_called_once()
        mutation.assert_not_called()

    def test_zoom_imports_licensed_users_only(self) -> None:
        pricing = self.write_csv(self.root / "zoom-pricing.csv", ["user_type", "application", "annual_cost"], [
            ["2", "Zoom Workplace", "18000"],
        ])
        connector = ZoomConnector("account", "client", "secret")
        with patch.object(connector, "_token", return_value="token"), patch(
            "saas_sentry.connectors._bearer_json", return_value={"users": [
                {"email": "licensed@example.com", "type": 2, "status": "active",
                 "last_login_time": "2026-10-01T10:00:00Z"},
                {"email": "basic@example.com", "type": 1, "status": "active"},
            ]},
        ):
            result = connector.import_exports(pricing, self.root / "zoom-output")
        self.assertEqual(result, (1, 1))
        self.assertEqual(self.read_csv(self.root / "zoom-output" / "billing.csv")[0]["email"], "licensed@example.com")

    def test_zoom_token_exchange_uses_post_without_leaking_credentials_in_url(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"access_token":"token"}'

        connector = ZoomConnector("account-id", "client-id", "client-secret")
        with patch("saas_sentry.connectors.urlopen", return_value=Response()) as mocked:
            self.assertEqual(connector._token(), "token")
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn("client-secret", request.full_url)


if __name__ == "__main__":
    unittest.main()
