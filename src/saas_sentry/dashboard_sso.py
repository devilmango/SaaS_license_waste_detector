"""OIDC and SAML login helpers for the dashboard."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .dashboard_auth import map_identity, new_transaction
from .engine import InputError


class SSOError(InputError):
    """An identity provider response could not be verified."""


def configured_mode() -> str:
    mode = os.environ.get("SAAS_SENTRY_SSO_MODE", "").strip().casefold()
    if mode not in {"", "oidc", "saml"}:
        raise SSOError("SAAS_SENTRY_SSO_MODE must be oidc or saml")
    if mode == "oidc":
        required = ("SAAS_SENTRY_OIDC_ISSUER", "SAAS_SENTRY_OIDC_CLIENT_ID", "SAAS_SENTRY_OIDC_CLIENT_SECRET", "SAAS_SENTRY_OIDC_REDIRECT_URI")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise SSOError("Missing OIDC settings: " + ", ".join(missing))
    if mode == "saml" and not os.environ.get("SAAS_SENTRY_SAML_SETTINGS_FILE"):
        raise SSOError("Set SAAS_SENTRY_SAML_SETTINGS_FILE for SAML login")
    return mode


class OIDCClient:
    def __init__(self, issuer: str, client_id: str, client_secret: str, redirect_uri: str):
        if not issuer.startswith("https://"):
            raise SSOError("OIDC issuer must use HTTPS")
        callback = urlparse(redirect_uri)
        if callback.scheme != "https" and not (callback.scheme == "http" and callback.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise SSOError("OIDC redirect URI must use HTTPS except for localhost development")
        self.issuer = issuer.rstrip("/")
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        request = Request(f"{self.issuer}/.well-known/openid-configuration", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=10) as response:
                self.discovery = json.loads(response.read().decode())
        except Exception as exc:
            raise SSOError("Unable to load OIDC discovery metadata") from exc
        if self.discovery.get("issuer", "").rstrip("/") != self.issuer:
            raise SSOError("OIDC discovery issuer does not match configured issuer")
        for endpoint in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            parsed = urlparse(str(self.discovery.get(endpoint, "")))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise SSOError(f"OIDC discovery {endpoint} must be an HTTPS URL")

    def authorization_url(self, transaction: dict[str, str]) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(transaction["verifier"].encode()).digest()).rstrip(b"=").decode()
        query = urlencode({
            "client_id": self.client_id, "response_type": "code", "scope": "openid profile email",
            "redirect_uri": self.redirect_uri, "state": transaction["state"], "nonce": transaction["nonce"],
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return f"{self.discovery['authorization_endpoint']}?{query}"

    def identity(self, code: str, transaction: dict[str, str]) -> dict:
        body = urlencode({
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret,
            "code_verifier": transaction["verifier"],
        }).encode()
        request = Request(self.discovery["token_endpoint"], data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=15) as response:
                token_response = json.loads(response.read().decode())
            token = token_response["id_token"]
            from authlib.jose import JsonWebKey, jwt
            jwks_request = Request(self.discovery["jwks_uri"], headers={"Accept": "application/json"})
            with urlopen(jwks_request, timeout=10) as response:
                jwks = JsonWebKey.import_key_set(json.loads(response.read().decode()))
            claims = jwt.decode(token, jwks, claims_options={"iss": {"essential": True, "value": self.issuer},
                                                            "aud": {"essential": True, "value": self.client_id}})
            claims.validate(leeway=60)
            result = dict(claims)
            if result.get("nonce") != transaction["nonce"]:
                raise SSOError("OIDC nonce validation failed")
            audiences = result.get("aud", [])
            if isinstance(audiences, str):
                audiences = [audiences]
            if len(audiences) > 1 and result.get("azp") != self.client_id:
                raise SSOError("OIDC authorized-party validation failed")
            return result
        except SSOError:
            raise
        except Exception as exc:
            raise SSOError("OIDC authorization response could not be verified") from exc


def oidc_client_from_environment() -> OIDCClient:
    configured_mode()
    return OIDCClient(os.environ["SAAS_SENTRY_OIDC_ISSUER"], os.environ["SAAS_SENTRY_OIDC_CLIENT_ID"],
                      os.environ["SAAS_SENTRY_OIDC_CLIENT_SECRET"], os.environ["SAAS_SENTRY_OIDC_REDIRECT_URI"])


def identity_principal(claims: dict, rules: tuple[dict, ...]):
    if claims.get("email_verified") is not True:
        raise SSOError("Identity provider must supply a verified email address")
    return map_identity(claims, rules)


def saml_auth(request: dict):
    settings_file = Path(os.environ["SAAS_SENTRY_SAML_SETTINGS_FILE"]).resolve()
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        if settings.get("strict") is not True:
            raise SSOError("SAML settings must set strict=true")
        security = settings.get("security", {})
        if not (security.get("wantMessagesSigned") or security.get("wantAssertionsSigned")):
            raise SSOError("SAML settings must require a signed response or assertion")
        prepared = {"https": "on" if request.get("https") else "off", "http_host": request.get("host", "localhost"),
                    "server_port": request.get("port", "443"), "script_name": request.get("path", "/auth/saml/acs"),
                    "get_data": request.get("query", {}), "post_data": request.get("form", {})}
        return OneLogin_Saml2_Auth(prepared, old_settings=settings)
    except SSOError:
        raise
    except Exception as exc:
        raise SSOError("Unable to initialize SAML service provider") from exc


def transaction() -> dict[str, str]:
    value = new_transaction()
    value["transaction"] = secrets.token_urlsafe(32)
    value["expires_at"] = str(time.time() + 600)
    return value
