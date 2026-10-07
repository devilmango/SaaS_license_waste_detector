from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from saas_sentry.connectors import ConnectorError, Microsoft365Connector


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class Microsoft365ConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pricing = self.root / "pricing.csv"
        with self.pricing.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sku_part_number", "annual_cost", "currency", "contract_id", "renewal_date", "seat_minimum"])
            writer.writerow(["M365_BUSINESS_PREMIUM", "24000", "INR", "m365", "2026-12-31", "1"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_open(self, request, timeout=30):
        url = request.full_url
        if url.endswith("/oauth2/v2.0/token"):
            return FakeResponse({"access_token": "test-token"})
        if "/subscribedSkus" in url:
            return FakeResponse({"value": [{"skuId": "sku-guid", "skuPartNumber": "M365_BUSINESS_PREMIUM"}]})
        if "/users?" in url:
            return FakeResponse({"value": [{
                "id": "user-guid", "displayName": "Synthetic Person", "employeeId": "E-1",
                "userPrincipalName": "person@example.com", "assignedLicenses": [{"skuId": "sku-guid"}],
                "signInActivity": {"lastSuccessfulSignInDateTime": "2026-10-01T10:00:00Z"},
            }]})
        self.fail(f"Unexpected request: {url}")

    def test_import_writes_canonical_exports_without_write_requests(self) -> None:
        connector = Microsoft365Connector("tenant", "client", "secret")
        output = self.root / "out"
        with patch("saas_sentry.connectors.urlopen", side_effect=self.fake_open) as mocked:
            users, assignments = connector.import_exports(self.pricing, output)
        self.assertEqual((users, assignments), (1, 1))
        methods = [call.args[0].get_method() for call in mocked.call_args_list]
        self.assertEqual(methods, ["POST", "GET", "GET"])
        with (output / "usage.csv").open(encoding="utf-8", newline="") as handle:
            usage = list(csv.DictReader(handle))
        self.assertEqual(usage[0]["application"], "M365_BUSINESS_PREMIUM")
        self.assertEqual(usage[0]["last_login"], "2026-10-01")
        with (output / "billing.csv").open(encoding="utf-8", newline="") as handle:
            billing = list(csv.DictReader(handle))
        self.assertEqual(billing[0]["annual_cost"], "24000")
        self.assertEqual(billing[0]["provider"], "microsoft365")
        self.assertEqual(billing[0]["provider_user_id"], "user-guid")
        self.assertEqual(billing[0]["provider_license_id"], "sku-guid")
        metadata = json.loads((output / "import.json").read_text(encoding="utf-8"))
        self.assertTrue(metadata["read_only"])

    def test_reclaim_revalidates_assignment_and_removes_only_target_sku(self) -> None:
        connector = Microsoft365Connector("tenant", "client", "secret")
        user_id = "11111111-1111-4111-8111-111111111111"
        sku_id = "22222222-2222-4222-8222-222222222222"
        calls = []

        def fake_reclaim(request, timeout=30):
            calls.append(request)
            if request.full_url.endswith("/oauth2/v2.0/token"):
                return FakeResponse({"access_token": "test-token"})
            if "/users/" in request.full_url and request.get_method() == "GET":
                return FakeResponse({
                    "id": user_id, "assignedLicenses": [{"skuId": sku_id}],
                    "licenseAssignmentStates": [{"skuId": sku_id, "state": "Active", "assignedByGroup": None}],
                })
            if request.full_url.endswith("/assignLicense"):
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(json.loads(request.data), {"addLicenses": [], "removeLicenses": [sku_id]})
                return FakeResponse({"id": user_id})
            self.fail(f"Unexpected request: {request.full_url}")

        with patch("saas_sentry.connectors.urlopen", side_effect=fake_reclaim):
            self.assertEqual(connector.remove_user_license(user_id, sku_id), "license_removed")
        self.assertEqual([request.get_method() for request in calls], ["POST", "GET", "POST"])

    def test_reclaim_refuses_group_assigned_license(self) -> None:
        connector = Microsoft365Connector("tenant", "client", "secret")
        user_id = "11111111-1111-4111-8111-111111111111"
        sku_id = "22222222-2222-4222-8222-222222222222"

        def group_assignment(request, timeout=30):
            if request.full_url.endswith("/oauth2/v2.0/token"):
                return FakeResponse({"access_token": "test-token"})
            return FakeResponse({
                "id": user_id, "assignedLicenses": [{"skuId": sku_id}],
                "licenseAssignmentStates": [{
                    "skuId": sku_id, "state": "Active", "assignedByGroup": "group-guid",
                }],
            })

        with patch("saas_sentry.connectors.urlopen", side_effect=group_assignment) as mocked:
            with self.assertRaisesRegex(ConnectorError, "group-assigned"):
                connector.remove_user_license(user_id, sku_id)
        self.assertEqual([call.args[0].get_method() for call in mocked.call_args_list], ["POST", "GET"])

    def test_import_fails_closed_when_activity_permission_field_is_missing(self) -> None:
        connector = Microsoft365Connector("tenant", "client", "secret")

        def missing_activity(request, timeout=30):
            if request.full_url.endswith("/oauth2/v2.0/token"):
                return FakeResponse({"access_token": "test-token"})
            if "/subscribedSkus" in request.full_url:
                return FakeResponse({"value": []})
            return FakeResponse({"value": [{"id": "u", "assignedLicenses": []}]})

        output = self.root / "should-not-exist"
        with patch("saas_sentry.connectors.urlopen", side_effect=missing_activity):
            with self.assertRaisesRegex(ConnectorError, "signInActivity"):
                connector.import_exports(self.pricing, output)
        self.assertFalse(output.exists())

    def test_unmapped_assigned_sku_does_not_write_partial_exports(self) -> None:
        connector = Microsoft365Connector("tenant", "client", "secret")

        def unknown_sku(request, timeout=30):
            if request.full_url.endswith("/oauth2/v2.0/token"):
                return FakeResponse({"access_token": "test-token"})
            if "/subscribedSkus" in request.full_url:
                return FakeResponse({"value": [{"skuId": "new-sku", "skuPartNumber": "NEW_SKU"}]})
            return FakeResponse({"value": [{
                "employeeId": "E-1", "userPrincipalName": "person@example.com",
                "assignedLicenses": [{"skuId": "new-sku"}], "signInActivity": {},
            }]})

        output = self.root / "unmapped"
        with patch("saas_sentry.connectors.urlopen", side_effect=unknown_sku):
            with self.assertRaisesRegex(ConnectorError, "Add annual per-seat costs"):
                connector.import_exports(self.pricing, output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
