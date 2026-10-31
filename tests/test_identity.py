from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from saas_sentry.connectors import ConnectorError
from saas_sentry.identity import import_entra_identities, import_scim_identities, reconcile_identities
from saas_sentry.identity_review import record_correction, review_queue


class IdentityDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_entra_import_normalizes_account_state_and_assigned_skus(self) -> None:
        connector = type("Connector", (), {
            "_token": lambda self: "token",
            "_get_pages": lambda self, url, token: [{
                "id": "entra-1", "displayName": "Alice Example", "userPrincipalName": "alice@example.com",
                "employeeId": "E1", "accountEnabled": True, "assignedLicenses": [{"skuId": "sku-123"}],
            }],
        })()
        output = self.root / "identities.csv"
        with patch("saas_sentry.identity.Microsoft365Connector.from_environment", return_value=connector):
            self.assertEqual(import_entra_identities(output), 1)
        with output.open(encoding="utf-8", newline="") as handle:
            row, = csv.DictReader(handle)
        self.assertEqual(row["source"], "entra_id")
        self.assertEqual(row["active"], "true")
        self.assertEqual(json.loads(row["app_access_json"]), ["sku-123"])

    def test_scim_import_follows_pages_and_exports_groups_as_access(self) -> None:
        pages = [
            {"totalResults": 2, "startIndex": 1, "Resources": [{
                "id": "scim-1", "externalId": "E1", "userName": "alice@example.com",
                "name": {"formatted": "Alice Example"}, "active": True,
                "groups": [{"display": "Slack"}, {"display": "Notion"}],
            }]},
            {"totalResults": 2, "startIndex": 2, "Resources": [{
                "id": "scim-2", "externalId": "E2", "userName": "bob@example.com", "active": False,
            }]},
        ]

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        output = self.root / "scim.csv"
        with patch("saas_sentry.identity.urlopen", side_effect=[Response(page) for page in pages]) as request:
            self.assertEqual(import_scim_identities("https://id.example.com/scim/v2", output, token="secret"), 2)
        self.assertEqual(request.call_count, 2)
        with output.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["provisioning_state"], "provisioned")
        self.assertEqual(json.loads(rows[0]["app_access_json"]), ["Notion", "Slack"])
        self.assertEqual(rows[1]["provisioning_state"], "deprovisioned")

    def test_reconciliation_flags_terminated_access_and_unmatched_directory_users(self) -> None:
        hr = self.root / "hr.csv"
        with hr.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["employee", "employee_id", "email", "email_aliases", "status"])
            writer.writeheader()
            writer.writerows([
                {"employee": "John", "employee_id": "E3", "email": "john@example.com", "status": "terminated"},
                {"employee": "Cara", "employee_id": "E4", "email": "cara@example.com", "status": "active"},
                {"employee": "Mina", "employee_id": "E5", "email": "mina@example.com", "status": "terminated"},
            ])
        identities = self.root / "identities.csv"
        with identities.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "employee_id", "email", "display_name", "source", "provider_user_id", "active",
                "app_access_json", "provisioning_state",
            ])
            writer.writeheader()
            writer.writerows([
                {"employee_id": "E3", "email": "john@example.com", "display_name": "John", "source": "scim",
                 "provider_user_id": "1", "active": "true", "app_access_json": '["Slack"]', "provisioning_state": "provisioned"},
                {"employee_id": "E4", "email": "cara@example.com", "display_name": "Cara", "source": "scim",
                 "provider_user_id": "2", "active": "false", "app_access_json": "[]", "provisioning_state": "deprovisioned"},
                {"employee_id": "E5", "email": "mina@example.com", "display_name": "Mina", "source": "scim",
                 "provider_user_id": "5", "active": "true", "app_access_json": "[]", "provisioning_state": "provisioned"},
                {"employee_id": "E9", "email": "unknown@example.com", "display_name": "Unknown", "source": "scim",
                 "provider_user_id": "9", "active": "true", "app_access_json": '["Zoom"]', "provisioning_state": "provisioned"},
            ])
        results = {row.employee_id: row for row in reconcile_identities(hr, identities)}
        self.assertEqual(results["E3"].match_status, "terminated_with_access")
        self.assertEqual(results["E4"].match_status, "active_hr_deprovisioned")
        self.assertEqual(results["E5"].match_status, "terminated_active_account")
        self.assertEqual(results["E9"].match_status, "identity_without_hr")

    def test_scim_requires_https_and_complete_pagination(self) -> None:
        with self.assertRaisesRegex(ConnectorError, "HTTPS"):
            import_scim_identities("http://id.example.com/scim/v2", self.root / "bad.csv", token="secret")
        response = {"totalResults": 2, "startIndex": 1, "Resources": [{
            "id": "one", "userName": "one@example.com", "active": True,
        }]}

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with patch("saas_sentry.identity.urlopen", side_effect=[
            FakeResponse(response),
            FakeResponse({"totalResults": 2, "startIndex": 2, "Resources": []}),
        ]):
            with self.assertRaisesRegex(ConnectorError, "ended before all"):
                import_scim_identities("https://id.example.com/scim/v2", self.root / "partial.csv", token="secret")

    def test_reviewed_identity_correction_is_a_reusable_mapping_rule(self) -> None:
        hr = self.root / "hr.csv"
        hr.write_text("employee,employee_id,email,status\nAlice,E1,alice@example.com,active\n", encoding="utf-8")
        identities = self.root / "identities.csv"
        identities.write_text(
            "employee_id,email,display_name,source,provider_user_id,active,app_access_json,provisioning_state\n"
            "OLD,alice.old@example.com,Alice,scim,scim-1,true,[\"Slack\"],provisioned\n", encoding="utf-8",
        )
        ledger = self.root / "identity-corrections.csv"
        queue = review_queue(hr, identities, ledger)
        self.assertTrue(any(item["status"] == "identity_without_hr" for item in queue))
        record_correction(hr, identities, ledger, identity_source="scim", provider_user_id="scim-1",
                          decision="map", employee_id="E1", reviewed_by="reviewer@example.com")
        result = reconcile_identities(hr, identities, mapping_rules_path=ledger)
        self.assertEqual([row.match_status for row in result], ["active_match"])
        self.assertEqual(review_queue(hr, identities, ledger), [])

    def test_shared_account_correction_is_preserved_and_not_person_matched(self) -> None:
        hr = self.root / "hr.csv"
        hr.write_text("employee,employee_id,email,status\nShared,E1,team@example.com,active\n", encoding="utf-8")
        identities = self.root / "identities.csv"
        identities.write_text(
            "employee_id,email,display_name,source,provider_user_id,active,app_access_json,provisioning_state\n"
            ",team@example.com,Team,scim,shared-1,true,[\"Slack\"],provisioned\n", encoding="utf-8",
        )
        ledger = self.root / "identity-corrections.csv"
        record_correction(hr, identities, ledger, identity_source="scim", provider_user_id="shared-1",
                          decision="shared_account", reviewed_by="reviewer@example.com")
        result = reconcile_identities(hr, identities, mapping_rules_path=ledger)
        self.assertIn("shared_account", [row.match_status for row in result])


if __name__ == "__main__":
    unittest.main()
