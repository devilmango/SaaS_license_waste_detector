"""Identity provider imports and HR-to-directory reconciliation."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from .connectors import ConnectorError, GRAPH_ROOT, Microsoft365Connector
from .engine import InputError, _rows


IDENTITY_FIELDS = (
    "employee_id", "email", "display_name", "source", "provider_user_id", "active",
    "app_access_json", "provisioning_state",
)
RECONCILIATION_FIELDS = (
    "employee_id", "employee", "email", "hr_status", "identity_source",
    "provider_user_id", "identity_active", "app_access", "provisioning_state", "match_status",
)


@dataclass(frozen=True)
class IdentityMatch:
    employee_id: str
    employee: str
    email: str
    hr_status: str
    identity_source: str
    provider_user_id: str
    identity_active: str
    app_access: str
    provisioning_state: str
    match_status: str


def _atomic_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise InputError(f"Cannot write identity CSV {path}: {exc}") from exc


def _identity_row(
    *, employee_id: str, email: str, display_name: str, source: str,
    provider_user_id: str, active: bool, app_access: list[str],
) -> dict[str, str]:
    return {
        "employee_id": employee_id.strip(), "email": email.strip().casefold(),
        "display_name": display_name.strip(), "source": source,
        "provider_user_id": provider_user_id.strip(), "active": str(active).lower(),
        "app_access_json": json.dumps(sorted(set(app_access), key=str.casefold)),
        "provisioning_state": "provisioned" if active else "deprovisioned",
    }


def import_entra_identities(output_path: Path) -> int:
    """Import Entra users, active state, stable IDs, and assigned SKU access via Graph."""
    connector = Microsoft365Connector.from_environment()
    query = urlencode({"$select": "id,displayName,userPrincipalName,employeeId,accountEnabled,assignedLicenses", "$top": "500"})
    try:
        users = connector._get_pages(f"{GRAPH_ROOT}/users?{query}", connector._token())
    except ConnectorError:
        raise
    rows = []
    for line, user in enumerate(users, start=1):
        uid = str(user.get("id", "")).strip()
        email = str(user.get("userPrincipalName", "")).strip()
        licenses = user.get("assignedLicenses")
        if not uid or not email or not isinstance(licenses, list) or "accountEnabled" not in user:
            raise ConnectorError(f"Microsoft Graph user row {line} is missing identity or license state")
        if not isinstance(user["accountEnabled"], bool):
            raise ConnectorError(f"Microsoft Graph user row {line} has invalid accountEnabled state")
        apps = [str(item.get("skuId", "")).strip() for item in licenses if isinstance(item, dict)]
        if any(not app for app in apps) or len(apps) != len(licenses):
            raise ConnectorError(f"Microsoft Graph user row {line} has invalid assignedLicenses")
        rows.append(_identity_row(
            employee_id=str(user.get("employeeId", "")), email=email,
            display_name=str(user.get("displayName", "")), source="entra_id",
            provider_user_id=uid, active=user["accountEnabled"], app_access=apps,
        ))
    _atomic_csv(output_path, IDENTITY_FIELDS, rows)
    return len(rows)


def _scim_page(url: str, token: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConnectorError("SCIM base URL must use HTTPS and cannot contain embedded credentials")
    request = Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/scim+json, application/json",
    })
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ConnectorError(f"SCIM API returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ConnectorError(f"SCIM API request failed: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise ConnectorError("SCIM API returned an invalid JSON object")
    return payload


def import_scim_identities(base_url: str, output_path: Path, *, token: str | None = None) -> int:
    """Import SCIM Users resources and their group memberships from a paginated SCIM 2 endpoint."""
    secret = token or os.environ.get("SAAS_SENTRY_SCIM_TOKEN", "")
    if not secret:
        raise ConnectorError("Set SAAS_SENTRY_SCIM_TOKEN or pass a SCIM bearer token")
    parsed = urlparse(base_url.rstrip("/"))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConnectorError("SCIM base URL must be an HTTPS URL without credentials, query, or fragment")
    root = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
    start_index, count = 1, 100
    users: list[dict] = []
    seen: set[int] = set()
    total_results: int | None = None
    while True:
        if start_index in seen:
            raise ConnectorError("SCIM API repeated a pagination startIndex")
        seen.add(start_index)
        url = f"{root}/Users?{urlencode({'startIndex': start_index, 'count': count})}"
        page = _scim_page(url, secret)
        resources = page.get("Resources")
        if not isinstance(resources, list) or any(not isinstance(row, dict) for row in resources):
            raise ConnectorError("SCIM response must contain a Resources array")
        try:
            total_results = int(page["totalResults"])
            response_start = int(page["startIndex"])
        except (TypeError, ValueError) as exc:
            raise ConnectorError("SCIM totalResults and startIndex must be whole numbers") from exc
        except KeyError as exc:
            raise ConnectorError("SCIM response is missing totalResults or startIndex") from exc
        if response_start != start_index:
            raise ConnectorError("SCIM response startIndex does not match the requested page")
        if total_results < 0:
            raise ConnectorError("SCIM totalResults cannot be negative")
        users.extend(resources)
        if not resources or len(users) >= total_results:
            break
        next_index = response_start + len(resources)
        if next_index <= start_index:
            raise ConnectorError("SCIM pagination did not advance")
        start_index = next_index
    rows = []
    for line, user in enumerate(users, start=1):
        user_id = str(user.get("id", "")).strip()
        email = str(user.get("userName", "")).strip()
        name_obj = user.get("name") or {}
        display_name = str(name_obj.get("formatted", "")).strip() if isinstance(name_obj, dict) else ""
        emails = user.get("emails", [])
        if isinstance(emails, list):
            primary = next((item.get("value") for item in emails if isinstance(item, dict) and item.get("primary")), "")
            email = str(primary or email).strip()
        active = user.get("active")
        if not user_id or not email or not isinstance(active, bool):
            raise ConnectorError(f"SCIM User row {line} is missing id, email, or boolean active state")
        groups = user.get("groups", []) or []
        if not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups):
            raise ConnectorError(f"SCIM User row {line} has invalid group memberships")
        app_access = [str(group.get("display") or group.get("value") or "").strip() for group in groups]
        if any(not name for name in app_access):
            raise ConnectorError(f"SCIM User row {line} has a group membership without a display or value")
        rows.append(_identity_row(
            employee_id=str(user.get("externalId", "")), email=email,
            display_name=display_name, source="scim", provider_user_id=user_id,
            active=active, app_access=app_access,
        ))
    if total_results is not None and len(users) != total_results:
        raise ConnectorError("SCIM pagination ended before all directory users were imported")
    _atomic_csv(output_path, IDENTITY_FIELDS, rows)
    return len(rows)


def reconcile_identities(hr_path: Path, identities_path: Path) -> tuple[IdentityMatch, ...]:
    """Match directory records to HR by employee ID/email and flag access lifecycle gaps."""
    hr_rows = _rows(hr_path, {"employee_id", "email", "status"})
    identities = _rows(identities_path, {
        "employee_id", "email", "display_name", "source", "provider_user_id", "active",
        "app_access_json", "provisioning_state",
    })
    if not hr_rows:
        raise InputError(f"{hr_path}: HR roster is empty")
    identity_by_index: dict[int, dict[str, str]] = {}
    for line, identity in enumerate(identities, start=2):
        if identity.get("active", "").casefold() not in {"true", "false"}:
            raise InputError(f"{identities_path}:{line}: active must be true or false")
        try:
            apps = json.loads(identity.get("app_access_json", "[]"))
        except json.JSONDecodeError as exc:
            raise InputError(f"{identities_path}:{line}: app_access_json must be a JSON array") from exc
        if not isinstance(apps, list) or any(not isinstance(app, str) for app in apps):
            raise InputError(f"{identities_path}:{line}: app_access_json must be a JSON string array")
        identity_by_index[line - 2] = identity
    used: set[int] = set()
    result: list[IdentityMatch] = []
    hr_keys: dict[int, tuple[set[str], set[str]]] = {}
    for index, person in enumerate(hr_rows):
        employee_id = person.get("employee_id", "").strip()
        email = person.get("email", "").strip().casefold()
        aliases = {value.strip().casefold() for value in person.get("email_aliases", "").split(",") if value.strip()}
        emails = aliases | ({email} if email else set())
        ids = {employee_id.casefold()} if employee_id else set()
        hr_keys[index] = (ids, emails)
    identity_claims: dict[int, set[int]] = {}
    for hr_index, (ids, emails) in hr_keys.items():
        for identity_index, identity in identity_by_index.items():
            identity_id = identity.get("employee_id", "").strip().casefold()
            identity_email = identity.get("email", "").strip().casefold()
            if (ids and identity_id in ids) or (identity_email and identity_email in emails):
                identity_claims.setdefault(identity_index, set()).add(hr_index)
    for index, person in enumerate(hr_rows):
        ids, emails = hr_keys[index]
        candidates: set[int] = set()
        mismatch = False
        for identity_index, identity in identity_by_index.items():
            identity_id = identity.get("employee_id", "").strip().casefold()
            identity_emails = {identity.get("email", "").strip().casefold()}
            same_id, same_email = bool(ids and identity_id and identity_id in ids), bool(emails & identity_emails)
            if same_id or same_email:
                if same_id and identity_emails and not emails.intersection(identity_emails):
                    mismatch = True
                if same_email and ids and identity_id and identity_id not in ids:
                    mismatch = True
                candidates.add(identity_index)
        state = person.get("status", "").strip().casefold()
        contested = any(len(identity_claims.get(candidate, set())) > 1 for candidate in candidates)
        if len(candidates) > 1 or mismatch or contested:
            for candidate in candidates:
                used.add(candidate)
            result.append(IdentityMatch(
                person.get("employee_id", ""), person.get("employee", ""), person.get("email", ""),
                state, "", "", "", "", "", "identity_conflict",
            ))
            continue
        if not candidates:
            result.append(IdentityMatch(
                person.get("employee_id", ""), person.get("employee", ""), person.get("email", ""),
                state, "", "", "", "", "", "no_identity_record",
            ))
            continue
        identity_index = next(iter(candidates))
        used.add(identity_index)
        identity = identity_by_index[identity_index]
        active = identity["active"].casefold() == "true"
        apps = json.loads(identity["app_access_json"] or "[]")
        if state == "terminated":
            match_status = (
                "terminated_with_access" if active and apps
                else "terminated_active_account" if active
                else "deprovisioned"
            )
        elif state == "active" and not active:
            match_status = "active_hr_deprovisioned"
        elif state == "active":
            match_status = "active_match" if apps else "active_without_app_access"
        else:
            match_status = "matched"
        result.append(IdentityMatch(
            person.get("employee_id", ""), person.get("employee", ""), person.get("email", ""),
            state, identity.get("source", ""), identity.get("provider_user_id", ""),
            identity.get("active", ""), ";".join(apps), identity.get("provisioning_state", ""),
            match_status,
        ))
    for identity_index, identity in identity_by_index.items():
        if identity_index in used:
            continue
        apps = json.loads(identity["app_access_json"] or "[]")
        result.append(IdentityMatch(
            identity.get("employee_id", ""), identity.get("display_name", ""), identity.get("email", ""),
            "unknown", identity.get("source", ""), identity.get("provider_user_id", ""),
            identity.get("active", ""), ";".join(apps), identity.get("provisioning_state", ""),
            "identity_without_hr",
        ))
    return tuple(sorted(result, key=lambda item: (item.hr_status, item.employee.casefold(), item.email.casefold())))


def format_identity_matches(rows: tuple[IdentityMatch, ...]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=RECONCILIATION_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row.__dict__)
    return output.getvalue()
