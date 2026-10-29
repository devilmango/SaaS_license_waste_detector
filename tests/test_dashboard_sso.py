from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from saas_sentry.dashboard_auth import new_transaction
from saas_sentry.dashboard_sso import OIDCClient, SSOError, configured_mode, identity_principal


class DashboardSSOTests(unittest.TestCase):
    def test_oidc_authorization_uses_state_nonce_and_pkce(self) -> None:
        client = OIDCClient.__new__(OIDCClient)
        client.client_id = "client-id"
        client.redirect_uri = "https://sentry.example/auth/oidc/callback"
        client.discovery = {"authorization_endpoint": "https://idp.example/authorize"}
        tx = new_transaction()
        query = parse_qs(urlparse(client.authorization_url(tx)).query)
        self.assertEqual(query["state"], [tx["state"]])
        self.assertEqual(query["nonce"], [tx["nonce"]])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotEqual(query["code_challenge"], [tx["verifier"]])

    def test_oidc_requires_https_issuer(self) -> None:
        with self.assertRaises(SSOError):
            OIDCClient("http://idp.example", "client", "secret", "https://app.example/callback")

    def test_invalid_sso_mode_is_rejected(self) -> None:
        with patch.dict("os.environ", {"SAAS_SENTRY_SSO_MODE": "password"}):
            with self.assertRaises(SSOError):
                configured_mode()

    def test_email_identity_requires_verified_claim(self) -> None:
        with self.assertRaises(SSOError):
            identity_principal({"email": "user@example.com"}, ())


if __name__ == "__main__":
    unittest.main()
