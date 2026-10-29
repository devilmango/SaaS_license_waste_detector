"""Dashboard principals, SSO configuration, and app/team-scoped authorization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from threading import RLock
from typing import Any
import uuid

from .engine import InputError


ROLE_LEVEL = {"viewer": 1, "reviewer": 2, "admin": 3}
ROLE_PERMISSIONS = {
    "viewer": {"report:read", "campaign:read"},
    "reviewer": {"report:read", "campaign:read", "review:write", "campaign:create", "campaign:decide"},
    "admin": {"report:read", "campaign:read", "review:write", "campaign:create", "campaign:decide", "transfer:execute", "policy:write"},
}


class AuthorizationError(InputError):
    """The authenticated principal does not have the requested capability or scope."""


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str
    applications: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    restricted: bool = False
    groups: tuple[str, ...] = ()


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def parse_authorization_rules(raw: str) -> tuple[dict[str, Any], ...]:
    if not raw.strip():
        return ()
    try:
        records = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError("SAAS_SENTRY_AUTHZ_RULES must contain a JSON array") from exc
    if not isinstance(records, list):
        raise InputError("SAAS_SENTRY_AUTHZ_RULES must contain a JSON array")
    rules = []
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise InputError(f"authorization rule {index + 1} must be an object")
        email, group = _norm(item.get("email")), _norm(item.get("group"))
        role = str(item.get("role", "")).strip().casefold()
        if not (email or group) or role not in ROLE_LEVEL:
            raise InputError(f"authorization rule {index + 1} needs email or group and a valid role")
        arrays = {}
        for field in ("applications", "teams", "permissions"):
            value = item.get(field, [])
            if not isinstance(value, list) or any(not isinstance(entry, str) or not entry.strip() for entry in value):
                raise InputError(f"authorization rule {index + 1} field {field} must be a list of non-empty strings")
            arrays[field] = tuple(entry.strip() for entry in value)
        rules.append({"email": email, "group": group, "role": role, **arrays})
    return tuple(rules)


def map_identity(claims: dict[str, Any], rules: tuple[dict[str, Any], ...]) -> Principal:
    email = _norm(claims.get("email") or claims.get("upn") or claims.get("preferred_username"))
    subject = str(claims.get("sub", "")).strip()
    raw_groups = claims.get("groups", []) or []
    if isinstance(raw_groups, str):
        raw_groups = [raw_groups]
    if not isinstance(raw_groups, (tuple, list)):
        raise AuthorizationError("Identity provider returned invalid groups")
    groups = tuple(str(group).strip() for group in raw_groups if str(group).strip())
    group_keys = {_norm(group) for group in groups}
    matched = [rule for rule in rules if (rule["email"] and rule["email"] == email)
               or (rule["group"] and rule["group"] in group_keys)]
    if not matched:
        raise AuthorizationError("Authenticated identity has no SaaS Sentry access mapping")
    actor = email or subject
    if not actor:
        raise AuthorizationError("Identity provider returned no stable user identity")
    role = max((item["role"] for item in matched), key=lambda name: ROLE_LEVEL[name])
    applications = tuple(sorted({value for item in matched for value in item["applications"]}, key=str.casefold))
    teams = tuple(sorted({value for item in matched for value in item["teams"]}, key=str.casefold))
    permissions = tuple(sorted({value for item in matched for value in item["permissions"]}))
    return Principal(actor, role, applications, teams, permissions, bool(applications or teams), groups)


def principal_from_user_record(actor: str, record: dict[str, Any]) -> Principal:
    role = str(record.get("role", "viewer")).casefold()
    if role not in ROLE_LEVEL:
        raise AuthorizationError("Dashboard user has an invalid role")
    applications = tuple(str(value).strip() for value in record.get("applications", []) if str(value).strip())
    teams = tuple(str(value).strip() for value in record.get("teams", []) if str(value).strip())
    permissions = tuple(str(value).strip() for value in record.get("permissions", []) if str(value).strip())
    return Principal(actor, role, applications, teams, permissions, bool(applications or teams))


def authorize(
    principal: Principal, permission: str, *, application: str = "", team: str = "",
    scope_required: bool = True,
) -> bool:
    if principal.role not in ROLE_LEVEL:
        raise AuthorizationError("Dashboard role is invalid")
    allowed_permissions = set(principal.permissions) if principal.permissions else ROLE_PERMISSIONS[principal.role]
    if permission not in allowed_permissions:
        raise AuthorizationError(f"Permission {permission!r} is required")
    if principal.restricted and scope_required:
        app_allowed = bool(application) and ("*" in principal.applications or any(
            _norm(application) == _norm(item) for item in principal.applications
        ))
        team_allowed = bool(team) and ("*" in principal.teams or any(
            _norm(team) == _norm(item) for item in principal.teams
        ))
        if not (app_allowed or team_allowed):
            raise AuthorizationError("The requested application or team is outside your access scope")
    return True


def _can_view(principal: Principal, row: dict[str, Any], *, permission: str = "report:read") -> bool:
    try:
        return authorize(
            principal, permission, application=str(row.get("application", "")),
            team=str(row.get("department") or row.get("team") or ""),
        )
    except AuthorizationError:
        return False


def scope_report(report: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """Return only report rows and aggregates the principal is authorized to see."""
    if not principal.restricted:
        result = dict(report)
        result["scope_limited"] = False
        return result
    result = dict(report)
    findings = [row for row in report.get("findings", []) if _can_view(principal, row)]
    result["findings"] = findings
    if principal.restricted:
        result["scope_limited"] = True
        # A finding is not a seat, so do not mislabel the scoped finding count as licenses.
        # A future report schema can add independently scoped seat totals.
        result["active_license_count"] = None
        def total(field: str) -> str:
            values = []
            for row in findings:
                try:
                    values.append(Decimal(str(row.get(field, 0) or 0)))
                except InvalidOperation as exc:
                    raise InputError(f"report contains an invalid amount in {field}") from exc
            return str(sum(values, Decimal(0)))
        result["totals_inr"] = {
            "annualized_candidate_cost": total("annualized_cost_inr"),
            "contract_adjusted_annual_opportunity": total("opportunity_savings_inr"),
            "estimated_realizable_12m": total("realizable_12m_inr"),
        }
        renewals = []
        for row in report.get("renewal_calendar", []):
            apps = [value.strip() for value in str(row.get("applications", "")).split(",") if value.strip()]
            if apps and all(_can_view(principal, {"application": app}) for app in apps):
                renewals.append(row)
        result["renewal_calendar"] = renewals
        if principal.teams:
            result["cost_center_showback"] = [row for row in report.get("cost_center_showback", [])
                                               if any(_norm(row.get(field)) == _norm(team)
                                                      for team in principal.teams for field in ("department", "cost_center"))]
        else:
            result["cost_center_showback"] = []
        result["data_quality_issues"] = [row for row in report.get("data_quality_issues", [])
                                          if row.get("application") and _can_view(principal, row)]
        result.pop("source_fingerprints", None)
        result.pop("config_fingerprint", None)
    else:
        result["scope_limited"] = False
    return result


@dataclass(frozen=True)
class Session:
    principal: Principal
    csrf_token: str
    expires_at: float


class SessionStore:
    """In-memory server-side sessions with random IDs, CSRF secrets, and expiry."""

    def __init__(self, *, lifetime_seconds: int = 8 * 60 * 60):
        if not 60 <= lifetime_seconds <= 24 * 60 * 60:
            raise InputError("dashboard session lifetime must be between 60 seconds and 24 hours")
        self.lifetime_seconds = lifetime_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()

    def create(self, principal: Principal) -> tuple[str, Session]:
        import secrets
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        session = Session(principal, csrf, datetime.now(timezone.utc).timestamp() + self.lifetime_seconds)
        with self._lock:
            self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session and session.expires_at <= datetime.now(timezone.utc).timestamp():
                del self._sessions[session_id]
                return None
            return session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


def new_transaction() -> dict[str, str]:
    import secrets
    return {key: secrets.token_urlsafe(size) for key, size in (
        ("transaction", 32), ("state", 32), ("nonce", 32), ("verifier", 48),
    )}
