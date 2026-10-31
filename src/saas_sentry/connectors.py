"""Provider data imports and guarded remediation APIs."""

from __future__ import annotations

import csv
import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import gzip
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlparse
from urllib.request import Request, urlopen


class ConnectorError(RuntimeError):
    """Raised when provider authentication, permissions, or data is invalid."""


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


def _open_with_rate_limit(request, provider: str, *, timeout: int = 30, max_attempts: int = 4):
    """Retry provider rate limits using Retry-After when supplied, otherwise back off."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    for attempt in range(1, max_attempts + 1):
        try:
            return urlopen(request, timeout=timeout)
        except HTTPError as exc:
            remaining = (exc.headers.get("X-RateLimit-Remaining", "") if exc.headers else "")
            retryable = exc.code == 429 or (
                exc.code == 403 and bool(exc.headers) and
                (exc.headers.get("Retry-After") is not None or remaining == "0")
            )
            if exc.code == 403 and not retryable:
                try:
                    payload = json.loads(exc.read().decode("utf-8"))
                    reasons = [entry.get("reason", "") for entry in payload.get("error", {}).get("errors", [])
                               if isinstance(entry, dict)]
                    retryable = any(reason.casefold() in {"ratelimitexceeded", "userratelimitexceeded"}
                                    for reason in reasons)
                except (OSError, UnicodeDecodeError, ValueError, AttributeError):
                    retryable = False
            if not retryable:
                raise
            if attempt >= max_attempts:
                raise ConnectorError(f"{provider} API rate limit persisted after {attempt} attempts (HTTP {exc.code})") from exc
            from .provider_imports import note_rate_limit_retry
            note_rate_limit_retry()
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = float(2 ** (attempt - 1))
            time.sleep(min(delay, 120.0))


def _bearer_json(url: str, token: str, provider: str, allowed_hosts: set[str], *, headers: dict[str, str] | None = None) -> dict:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ConnectorError(f"{provider}: refused unexpected API URL")
    request_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    request_headers.update(headers or {})
    try:
        with _open_with_rate_limit(Request(url, headers=request_headers), provider, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ConnectorError(f"{provider} API returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ConnectorError(f"{provider} API request failed: {type(exc).__name__}") from exc
    if not isinstance(result, dict):
        raise ConnectorError(f"{provider} API returned an invalid JSON object")
    return result


def _bearer_bytes(url: str, token: str, provider: str, allowed_hosts: set[str]) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ConnectorError(f"{provider}: refused unexpected API URL")
    try:
        with _open_with_rate_limit(Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/gzip"}), provider, timeout=60) as response:
            return response.read()
    except HTTPError as exc:
        raise ConnectorError(f"{provider} API returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ConnectorError(f"{provider} API request failed: {type(exc).__name__}") from exc


def _bearer_mutation(
    url: str, token: str, provider: str, allowed_hosts: set[str], *, method: str,
    body: bytes | None = None, headers: dict[str, str] | None = None,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ConnectorError(f"{provider}: refused unexpected API URL")
    request_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    request_headers.update(headers or {})
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
    try:
        with urlopen(Request(url, data=body, headers=request_headers, method=method), timeout=30):
            pass
    except HTTPError as exc:
        raise ConnectorError(f"{provider} API returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ConnectorError(f"{provider} API request failed: {type(exc).__name__}") from exc


def _slack_admin_remove_user(token: str, team_id: str, user_id: str) -> None:
    body = urlencode({"team_id": team_id, "user_id": user_id}).encode("utf-8")
    request = Request(
        "https://slack.com/api/admin.users.remove", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ConnectorError(f"Slack API returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ConnectorError(f"Slack API request failed: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        error = payload.get("error", "unknown_error") if isinstance(payload, dict) else "invalid_response"
        raise ConnectorError(f"Slack admin.users.remove failed ({error})")


def _required_token(name: str, provider: str) -> str:
    token = os.environ.get(name, "")
    if not token:
        raise ConnectorError(f"Set required environment variable {name} for {provider}")
    return token


def _validate_prices(path: Path, required: set[str]) -> list[dict[str, str]]:
    rows = _read_csv(path)
    headers = set(rows[0]) if rows else set()
    missing = required - headers
    if missing:
        raise ConnectorError(f"{path}: pricing CSV missing columns: {', '.join(sorted(missing))}")
    if not rows:
        raise ConnectorError(f"{path}: pricing CSV must contain at least one price row")
    for line, row in enumerate(rows, start=2):
        try:
            amount = Decimal(row.get("annual_cost", ""))
        except InvalidOperation as exc:
            raise ConnectorError(f"{path}:{line}: annual_cost must be numeric") from exc
        if not amount.is_finite() or amount < 0:
            raise ConnectorError(f"{path}:{line}: annual_cost must be finite and non-negative")
    return rows


def _billing_export_row(identity: dict[str, str], price: dict[str, str], application: str) -> list[str]:
    return [
        identity.get("employee_id", ""), identity.get("email", ""), application, "active",
        price.get("annual_cost", ""), price.get("currency", ""),
        price.get("contract_id", "") or application, price.get("renewal_date", ""),
        price.get("seat_minimum", "0") or "0", price.get("commitment_end_date", ""),
        price.get("notice_days", "0") or "0", price.get("bundle_group", ""),
    ]


def _write_provider_exports(
    provider: str,
    output_dir: Path,
    usage_rows: list[list[str]],
    billing_rows: list[list[str]],
    usage_signal: str,
) -> tuple[int, int]:
    """Persist canonical CSV exports and import metadata after validation completes."""
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_csv(output_dir / "usage.csv", [
        "employee", "employee_id", "email", "application", "last_login", "usage_count",
        "active_days", "usage_window_days", "features_used",
    ], usage_rows)
    normalized_billing = [row + [provider, "", ""] if len(row) == 12 else row for row in billing_rows]
    _atomic_csv(output_dir / "billing.csv", [
        "employee_id", "email", "application", "license_status", "annual_cost", "currency",
        "contract_id", "renewal_date", "seat_minimum", "commitment_end_date", "notice_days",
        "bundle_group", "provider", "provider_user_id", "provider_license_id",
    ], normalized_billing)
    metadata = {
        "provider": provider, "imported_at": datetime.now(timezone.utc).isoformat(),
        "user_count": len({(row[2] or row[1]).casefold() for row in usage_rows}),
        "license_assignment_count": len(billing_rows), "usage_signal": usage_signal,
        "read_only": True,
    }
    (output_dir / "import.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata["user_count"], len(billing_rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ConnectorError(f"{path}: CSV has no header row")
            return [{(key or "").strip(): (value or "").strip() for key, value in row.items() if key}
                    for row in reader]
    except OSError as exc:
        raise ConnectorError(f"Cannot read pricing CSV {path}: {exc}") from exc


def _atomic_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        temporary_path.replace(path)
    except OSError as exc:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
        raise ConnectorError(f"Cannot write connector output {path}: {exc}") from exc


class Microsoft365Connector:
    """Read Microsoft Graph users, assigned SKUs, and last successful sign-in."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret

    @classmethod
    def from_environment(cls) -> "Microsoft365Connector":
        names = ("M365_TENANT_ID", "M365_CLIENT_ID", "M365_CLIENT_SECRET")
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise ConnectorError(f"Set required environment variables: {', '.join(missing)}")
        return cls(*(os.environ[name] for name in names))

    @staticmethod
    def _json_request(request: Request) -> dict:
        try:
            with _open_with_rate_limit(request, "Microsoft 365", timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            message = f"HTTP {exc.code} from Microsoft identity/Graph endpoint"
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                code = payload.get("error", {}).get("code")
                if code:
                    message += f" ({code})"
            except (ValueError, AttributeError):
                pass
            raise ConnectorError(message) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise ConnectorError(f"Microsoft Graph request failed: {type(exc).__name__}") from exc

    def _token(self) -> str:
        endpoint = f"https://login.microsoftonline.com/{quote(self.tenant_id, safe='')}/oauth2/v2.0/token"
        body = urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }).encode("utf-8")
        payload = self._json_request(Request(endpoint, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
        }, method="POST"))
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ConnectorError("Microsoft identity response did not include an access token")
        return token

    def remove_user_license(self, user_id: str, sku_id: str) -> str:
        """Remove one directly assigned M365 SKU after revalidating the imported target."""
        try:
            user_id = str(uuid.UUID(user_id))
            sku_id = str(uuid.UUID(sku_id))
        except ValueError as exc:
            raise ConnectorError("Microsoft 365 reclaim target IDs must be UUIDs") from exc
        token = self._token()
        encoded_user = quote(user_id, safe="")
        query = urlencode({"$select": "id,assignedLicenses,licenseAssignmentStates"})
        user_url = f"{GRAPH_ROOT}/users/{encoded_user}?{query}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        user = self._json_request(Request(user_url, headers=headers))
        if str(user.get("id", "")).casefold() != user_id.casefold():
            raise ConnectorError("Microsoft 365 returned a different user than the requested reclaim target")
        assigned = user.get("assignedLicenses")
        if not isinstance(assigned, list):
            raise ConnectorError("Microsoft 365 user response omitted assignedLicenses; refusing the reclaim")
        if not any(str(item.get("skuId", "")).casefold() == sku_id.casefold()
                   for item in assigned if isinstance(item, dict)):
            return "license_already_absent"
        states = user.get("licenseAssignmentStates")
        if not isinstance(states, list):
            raise ConnectorError("Microsoft 365 response omitted licenseAssignmentStates; refusing the reclaim")
        target_states = [item for item in states if isinstance(item, dict)
                         and str(item.get("skuId", "")).casefold() == sku_id.casefold()]
        if not target_states:
            raise ConnectorError("Microsoft 365 could not verify the target license assignment state")
        if any(item.get("assignedByGroup") for item in target_states):
            raise ConnectorError("The target license is group-assigned; remove it through the group workflow")
        if not any(str(item.get("state", "")).casefold() == "active" for item in target_states):
            raise ConnectorError("The target license assignment is not active; refusing the reclaim")
        url = f"{GRAPH_ROOT}/users/{encoded_user}/assignLicense"
        body = json.dumps({"addLicenses": [], "removeLicenses": [sku_id]}).encode("utf-8")
        request = Request(url, data=body, headers={**headers, "Content-Type": "application/json"}, method="POST")
        self._json_request(request)
        return "license_removed"

    def _get_pages(self, url: str, token: str) -> list[dict]:
        output: list[dict] = []
        next_url: str | None = url
        seen_urls: set[str] = set()
        while next_url:
            if next_url in seen_urls:
                raise ConnectorError("Microsoft Graph repeated a pagination URL")
            seen_urls.add(next_url)
            parsed = urlparse(next_url)
            if parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com":
                raise ConnectorError("Microsoft Graph returned an unexpected pagination URL; refusing to send credentials")
            payload = self._json_request(Request(next_url, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json",
            }))
            values = payload.get("value")
            if not isinstance(values, list):
                raise ConnectorError("Microsoft Graph response is missing its value collection")
            output.extend(item for item in values if isinstance(item, dict))
            next_url = payload.get("@odata.nextLink")
        return output

    def import_exports(self, pricing_path: Path, output_dir: Path) -> tuple[int, int]:
        """Write canonical usage and billing CSVs without changing tenant state."""
        price_rows = _read_csv(pricing_path)
        if not {"sku_part_number", "annual_cost"}.issubset(set(price_rows[0]) if price_rows else set()):
            raise ConnectorError(f"{pricing_path}: expected sku_part_number and annual_cost columns")
        prices: dict[str, dict[str, str]] = {}
        for line, row in enumerate(price_rows, start=2):
            sku = row.get("sku_part_number", "").casefold()
            if not sku:
                raise ConnectorError(f"{pricing_path}:{line}: sku_part_number cannot be blank")
            if sku in prices:
                raise ConnectorError(f"{pricing_path}:{line}: duplicate price mapping for {sku!r}")
            try:
                annual_cost = Decimal(row.get("annual_cost", ""))
            except InvalidOperation as exc:
                raise ConnectorError(f"{pricing_path}:{line}: annual_cost must be numeric") from exc
            if not annual_cost.is_finite() or annual_cost < 0:
                raise ConnectorError(f"{pricing_path}:{line}: annual_cost must be finite and non-negative")
            prices[sku] = row

        token = self._token()
        sku_url = f"{GRAPH_ROOT}/subscribedSkus?{urlencode({'$select': 'skuId,skuPartNumber'})}"
        subscribed = self._get_pages(sku_url, token)
        sku_names = {
            str(item["skuId"]).casefold(): str(item["skuPartNumber"])
            for item in subscribed if item.get("skuId") and item.get("skuPartNumber")
        }
        query = urlencode({
            "$select": "id,displayName,userPrincipalName,employeeId,assignedLicenses,signInActivity",
            "$top": "500",
        })
        users = self._get_pages(f"{GRAPH_ROOT}/users?{query}", token)
        if users and not all("signInActivity" in user for user in users):
            raise ConnectorError(
                "Graph omitted signInActivity. Check AuditLog.Read.All consent and the tenant's Entra ID P1/P2 requirements."
            )
        usage_rows: list[list[str]] = []
        billing_rows: list[list[str]] = []
        missing_prices: set[str] = set()
        for user in users:
            assignments = user.get("assignedLicenses")
            if not isinstance(assignments, list):
                raise ConnectorError("Graph user response omitted assignedLicenses; refusing an incomplete import")
            sign_in = user.get("signInActivity") or {}
            if not isinstance(sign_in, dict):
                raise ConnectorError("Graph returned an invalid signInActivity object")
            last_login = str(sign_in.get("lastSuccessfulSignInDateTime", ""))[:10]
            principal = str(user.get("userPrincipalName", ""))
            employee_id = str(user.get("employeeId", ""))
            employee = str(user.get("displayName", ""))
            for assignment in assignments:
                sku_part = sku_names.get(str(assignment.get("skuId", "")).casefold())
                if not sku_part:
                    raise ConnectorError("A user's assigned SKU was absent from subscribedSkus")
                price = prices.get(sku_part.casefold())
                if not price:
                    missing_prices.add(sku_part)
                    continue
                if not user.get("id"):
                    raise ConnectorError("Microsoft Graph user is missing its stable id; refusing an incomplete import")
                usage_rows.append([employee, employee_id, principal, sku_part, last_login, "", "", "", ""])
                billing_rows.append([
                    employee_id, principal, sku_part, "active", price.get("annual_cost", ""),
                    price.get("currency", ""), price.get("contract_id", "") or sku_part,
                    price.get("renewal_date", ""), price.get("seat_minimum", "0") or "0",
                    price.get("commitment_end_date", ""), price.get("notice_days", "0") or "0",
                    price.get("bundle_group", ""), "microsoft365", str(user.get("id", "")),
                    str(assignment.get("skuId", "")),
                ])
        if missing_prices:
            raise ConnectorError(
                "Add annual per-seat costs for these assigned SKUs to the pricing CSV: "
                + ", ".join(sorted(missing_prices))
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        usage_headers = [
            "employee", "employee_id", "email", "application", "last_login", "usage_count",
            "active_days", "usage_window_days", "features_used",
        ]
        billing_headers = [
            "employee_id", "email", "application", "license_status", "annual_cost", "currency",
            "contract_id", "renewal_date", "seat_minimum", "commitment_end_date", "notice_days",
            "bundle_group", "provider", "provider_user_id", "provider_license_id",
        ]
        _atomic_csv(output_dir / "usage.csv", usage_headers, usage_rows)
        _atomic_csv(output_dir / "billing.csv", billing_headers, billing_rows)
        metadata = {
            "provider": "microsoft365",
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "user_count": len(users),
            "license_assignment_count": len(billing_rows),
            "usage_signal": "lastSuccessfulSignInDateTime per user, repeated for each assigned SKU",
            "read_only": True,
        }
        (output_dir / "import.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        return len(users), len(billing_rows)


class GoogleWorkspaceConnector:
    """Import Google Workspace Directory users and assigned product SKUs."""

    HOSTS = {"admin.googleapis.com", "licensing.googleapis.com"}

    def __init__(self, access_token: str):
        self.access_token = access_token

    @classmethod
    def from_environment(cls) -> "GoogleWorkspaceConnector":
        return cls(_required_token("GOOGLE_WORKSPACE_ACCESS_TOKEN", "Google Workspace"))

    def _pages(self, url: str, collection: str) -> list[dict]:
        values: list[dict] = []
        next_url: str | None = url
        seen: set[str] = set()
        while next_url:
            if next_url in seen:
                raise ConnectorError("Google Workspace API repeated a pagination URL")
            seen.add(next_url)
            payload = _bearer_json(next_url, self.access_token, "Google Workspace", self.HOSTS)
            page = payload.get(collection, [])
            if not isinstance(page, list):
                raise ConnectorError(f"Google Workspace response has invalid {collection}")
            values.extend(item for item in page if isinstance(item, dict))
            token = payload.get("nextPageToken")
            if token:
                parts = urlparse(url)
                query = dict(parse_qsl(parts.query))
                query["pageToken"] = str(token)
                next_url = parts._replace(query=urlencode(query)).geturl()
            else:
                next_url = None
        return values

    def import_exports(self, pricing_path: Path, output_dir: Path, *, customer: str = "my_customer") -> tuple[int, int]:
        prices = _validate_prices(pricing_path, {"product_id", "sku_id", "application", "annual_cost"})
        price_map: dict[tuple[str, str], dict[str, str]] = {}
        for line, row in enumerate(prices, start=2):
            key = (row["product_id"].casefold(), row["sku_id"].casefold())
            if not all(key) or not row.get("application", "").strip():
                raise ConnectorError(f"{pricing_path}:{line}: product_id, sku_id, and application cannot be blank")
            if key in price_map:
                raise ConnectorError(f"{pricing_path}:{line}: duplicate Google SKU pricing mapping")
            price_map[key] = row

        users_query = urlencode({
            "customer": customer, "maxResults": "500", "orderBy": "email",
            "projection": "full", "fields": "nextPageToken,users(id,primaryEmail,name(fullName),lastLoginTime,suspended)",
        })
        users = self._pages(f"https://admin.googleapis.com/admin/directory/v1/users?{users_query}", "users")
        users_by_email = {
            str(user.get("primaryEmail", "")).casefold(): user
            for user in users if user.get("primaryEmail")
        }
        usage_by_pair: dict[tuple[str, str], list[str]] = {}
        billing_rows: list[list[str]] = []
        missing_skus: set[str] = set()
        for product_id in sorted({product for product, _ in price_map}):
            query = urlencode({"customerId": customer, "maxResults": "100"})
            url = (
                "https://licensing.googleapis.com/apps/licensing/v1/product/"
                f"{quote(product_id, safe='')}/users?{query}"
            )
            assignments = self._pages(url, "items")
            for assignment in assignments:
                assigned_product = str(assignment.get("productId", product_id)).casefold()
                assigned_sku = str(assignment.get("skuId", "")).casefold()
                price = price_map.get((assigned_product, assigned_sku))
                if not price:
                    missing_skus.add(f"{assigned_product}/{assigned_sku or '<unknown SKU>'}")
                    continue
                email = str(assignment.get("userId", ""))
                user = users_by_email.get(email.casefold())
                application = price["application"].strip()
                if not email or not user:
                    raise ConnectorError("A Google license assignment could not be matched to a Directory user")
                identity = {"email": email}
                full_name = str(user.get("name", {}).get("fullName", ""))
                last_login = str(user.get("lastLoginTime", ""))[:10]
                pair = (email.casefold(), application.casefold())
                if pair in usage_by_pair:
                    raise ConnectorError("Pricing maps multiple assigned Google SKUs to the same application/user")
                usage_by_pair[pair] = [full_name, "", email, application, last_login, "", "", "", ""]
                billing_rows.append(_billing_export_row(identity, price, application) + [
                    "google_workspace", email, json.dumps([price["product_id"], price["sku_id"]]),
                ])
        if missing_skus:
            raise ConnectorError(
                "Add pricing mappings for Google Workspace product/SKUs: "
                + ", ".join(sorted(missing_skus))
            )
        return _write_provider_exports(
            "google_workspace", output_dir, list(usage_by_pair.values()), billing_rows,
            "Google Directory lastLoginTime per user, repeated for each assigned SKU",
        )

    def remove_user_license(self, user_id: str, sku_id: str) -> str:
        """Revoke one Google Workspace product/SKU assignment for an exact user email."""
        try:
            product_id, sku = json.loads(sku_id)
        except (ValueError, TypeError) as exc:
            raise ConnectorError("Google Workspace reclaim target must contain a product and SKU") from exc
        if not all(isinstance(value, str) and value.strip() for value in (product_id, sku, user_id)):
            raise ConnectorError("Google Workspace reclaim target is incomplete")
        path = "/".join(quote(value, safe="") for value in (product_id, "sku", sku, "user", user_id))
        url = f"https://licensing.googleapis.com/apps/licensing/v1/product/{path}"
        current = _bearer_json(url, self.access_token, "Google Workspace", self.HOSTS)
        if (str(current.get("userId", "")).casefold() != user_id.casefold()
                or str(current.get("productId", "")).casefold() != product_id.casefold()
                or str(current.get("skuId", "")).casefold() != sku.casefold()):
            raise ConnectorError("Google Workspace returned a different license target; refusing the reclaim")
        _bearer_mutation(url, self.access_token, "Google Workspace", self.HOSTS, method="DELETE")
        return "license_revoked"


class SlackConnector:
    """Import Slack billable members and daily member analytics from Slack Grid."""

    HOSTS = {"slack.com"}
    ACTIVITY_KEYS = (
        "messages_posted_count", "reactions_added_count", "files_added_count",
        "total_calls_count", "search_count",
    )
    FEATURE_KEYS = (
        "messages_posted_count", "reactions_added_count", "files_added_count",
        "total_calls_count", "search_count", "is_active_apps", "is_active_workflows",
        "is_active_slack_connect",
    )

    def __init__(self, access_token: str):
        self.access_token = access_token

    @classmethod
    def from_environment(cls) -> "SlackConnector":
        return cls(_required_token("SLACK_ACCESS_TOKEN", "Slack"))

    def _members(self, team_id: str = "") -> list[dict]:
        members: list[dict] = []
        cursor = ""
        while True:
            query = {"limit": "200"}
            if team_id:
                query["team_id"] = team_id
            if cursor:
                query["cursor"] = cursor
            payload = _bearer_json(
                f"https://slack.com/api/users.list?{urlencode(query)}", self.access_token,
                "Slack", self.HOSTS,
            )
            if not payload.get("ok"):
                raise ConnectorError(f"Slack users.list failed ({payload.get('error', 'unknown_error')})")
            values = payload.get("members")
            if not isinstance(values, list):
                raise ConnectorError("Slack users.list response has no members array")
            members.extend(item for item in values if isinstance(item, dict))
            metadata = payload.get("response_metadata") or {}
            if not isinstance(metadata, dict):
                raise ConnectorError("Slack users.list response has invalid pagination metadata")
            cursor = str(metadata.get("next_cursor", ""))
            if not cursor:
                return members

    def _daily_analytics(self, day: date) -> list[dict]:
        url = "https://slack.com/api/admin.analytics.getFile?" + urlencode({
            "type": "member", "date": day.isoformat(),
        })
        data = _bearer_bytes(url, self.access_token, "Slack", self.HOSTS)
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            # Slack returns JSON errors uncompressed; surface the provider error without leaking the token.
            try:
                error = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ConnectorError("Slack member analytics response was not valid gzip/JSON") from exc
            raise ConnectorError(f"Slack member analytics failed ({error.get('error', 'unknown_error')})")
        rows: list[dict] = []
        try:
            for line in data.decode("utf-8").splitlines():
                if line.strip():
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ConnectorError("Slack member analytics returned invalid newline-delimited JSON") from exc
        return rows

    def import_exports(
        self,
        pricing_path: Path,
        output_dir: Path,
        *,
        lookback_days: int = 90,
        as_of: date | None = None,
        team_id: str = "",
    ) -> tuple[int, int]:
        if not 1 <= lookback_days <= 365:
            raise ConnectorError("Slack lookback_days must be between 1 and 365")
        prices = _validate_prices(pricing_path, {"annual_cost"})
        if len(prices) != 1:
            raise ConnectorError(f"{pricing_path}: Slack pricing CSV must have exactly one workspace seat price row")
        price = prices[0]
        application = price.get("application", "").strip() or "Slack"
        today = as_of or (date.today() - timedelta(days=1))
        start = today - timedelta(days=lookback_days - 1)
        members = self._members(team_id)
        member_by_id = {
            str(member.get("id")): member for member in members
            if member.get("id") and not member.get("deleted") and not member.get("is_bot")
            and not member.get("is_restricted") and not member.get("is_ultra_restricted")
        }
        activity: dict[str, dict[str, object]] = {}
        billable_ids: set[str] = set()
        for offset in range(lookback_days):
            day = start + timedelta(days=offset)
            daily_rows = self._daily_analytics(day)
            if offset == lookback_days - 1:
                billable_ids = {
                    str(row.get("user_id")) for row in daily_rows if row.get("is_billable_seat")
                }
            for row in daily_rows:
                user_id = str(row.get("user_id", ""))
                state = activity.setdefault(user_id, {
                    "latest": "", "usage_count": 0, "active_days": 0, "features": set(),
                })
                counts = [int(row.get(key, 0) or 0) for key in self.ACTIVITY_KEYS]
                features = state["features"]
                assert isinstance(features, set)
                row_features: set[str] = set()
                for key in self.FEATURE_KEYS:
                    value = row.get(key, 0)
                    if (isinstance(value, bool) and value) or (not isinstance(value, bool) and value and int(value) > 0):
                        features.add(key)
                        row_features.add(key)
                if row.get("is_active") or any(counts) or row_features:
                    state["latest"] = day.isoformat()
                    state["active_days"] = int(state["active_days"]) + 1
                state["usage_count"] = int(state["usage_count"]) + sum(counts)
        usage_rows: list[list[str]] = []
        billing_rows: list[list[str]] = []
        for user_id in sorted(member_by_id):
            if user_id not in billable_ids:
                continue
            member = member_by_id[user_id]
            email = str(member.get("profile", {}).get("email", ""))
            if not email:
                raise ConnectorError("Slack user is missing email; ensure users:read.email is granted")
            user_activity = activity.get(user_id, {"latest": "", "usage_count": 0, "active_days": 0, "features": set()})
            usage_rows.append([
                str(member.get("real_name", "")), "", email, application,
                str(user_activity["latest"]), str(user_activity["usage_count"]),
                str(user_activity["active_days"]), str(lookback_days),
                str(len(user_activity["features"])),
            ])
            workspace = str(member.get("team_id", "")) or team_id
            billing_rows.append(_billing_export_row({"email": email}, price, application) + [
                "slack", user_id, workspace,
            ])
        if not billing_rows:
            raise ConnectorError("Slack import found no billable members in the requested analytics window")
        return _write_provider_exports(
            "slack", output_dir, usage_rows, billing_rows,
            f"Daily Slack member analytics over {lookback_days} days; latest active date is a usage signal, not a login timestamp",
        )

    def remove_user_license(self, user_id: str, sku_id: str) -> str:
        """Remove a single Slack Grid member after checking the live user record."""
        team_id = sku_id.strip()
        if not user_id.strip() or not team_id:
            raise ConnectorError("Slack reclaim target requires a user ID and workspace team ID")
        payload = _bearer_json(
            f"https://slack.com/api/users.info?{urlencode({'user': user_id})}",
            self.access_token, "Slack", self.HOSTS,
        )
        if not payload.get("ok") or not isinstance(payload.get("user"), dict):
            raise ConnectorError(f"Slack users.info failed ({payload.get('error', 'invalid_response')})")
        user = payload["user"]
        if str(user.get("id", "")) != user_id:
            raise ConnectorError("Slack returned a different user than the requested reclaim target")
        if user.get("deleted"):
            return "user_already_removed"
        if user.get("is_bot") or user.get("is_restricted") or user.get("is_ultra_restricted"):
            raise ConnectorError("Slack target is not a standard billable member; refusing removal")
        _slack_admin_remove_user(self.access_token, team_id, user_id)
        return "user_removed_from_workspace"


class GitHubCopilotConnector:
    """Import GitHub Copilot seat assignments and the provider's latest activity signal."""

    HOSTS = {"api.github.com"}

    def __init__(self, access_token: str):
        self.access_token = access_token

    @classmethod
    def from_environment(cls) -> "GitHubCopilotConnector":
        return cls(_required_token("GITHUB_TOKEN", "GitHub Copilot"))

    def import_exports(
        self,
        organization: str,
        pricing_path: Path,
        identity_path: Path,
        output_dir: Path,
    ) -> tuple[int, int]:
        if not organization.strip():
            raise ConnectorError("GitHub organization cannot be blank")
        prices = _validate_prices(pricing_path, {"annual_cost"})
        if len(prices) != 1:
            raise ConnectorError(f"{pricing_path}: GitHub Copilot pricing CSV must have exactly one seat price row")
        price = prices[0]
        application = price.get("application", "").strip() or "GitHub Copilot"
        identity_rows = _read_csv(identity_path)
        if not identity_rows or not {"login", "email", "employee_id"}.issubset(identity_rows[0]):
            raise ConnectorError(f"{identity_path}: expected login,email,employee_id identity mapping columns")
        identities: dict[str, dict[str, str]] = {}
        for line, row in enumerate(identity_rows, start=2):
            login = row.get("login", "").casefold()
            if not login or (not row.get("email") and not row.get("employee_id")):
                raise ConnectorError(f"{identity_path}:{line}: provide login and at least email or employee_id")
            if login in identities:
                raise ConnectorError(f"{identity_path}:{line}: duplicate GitHub login {login!r}")
            identities[login] = row

        seats: list[dict] = []
        for page in range(1, 10001):
            query = urlencode({"per_page": "100", "page": str(page)})
            payload = _bearer_json(
                f"https://api.github.com/orgs/{quote(organization, safe='')}/copilot/billing/seats?{query}",
                self.access_token, "GitHub Copilot", self.HOSTS,
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            )
            values = payload.get("seats")
            if not isinstance(values, list):
                raise ConnectorError("GitHub Copilot response is missing its seats array")
            seats.extend(item for item in values if isinstance(item, dict))
            if len(values) < 100:
                break
        else:
            raise ConnectorError("GitHub Copilot pagination exceeded the safety limit")

        usage_rows: list[list[str]] = []
        billing_rows: list[list[str]] = []
        missing_logins: set[str] = set()
        for seat in seats:
            assignee = seat.get("assignee") or {}
            login = str(assignee.get("login", ""))
            mapping = identities.get(login.casefold())
            if not mapping:
                missing_logins.add(login or "<no login>")
                continue
            activity_raw = seat.get("last_activity_at") or ""
            last_activity = str(activity_raw)[:10]
            if last_activity:
                try:
                    date.fromisoformat(last_activity)
                except ValueError as exc:
                    raise ConnectorError(f"GitHub Copilot returned invalid last_activity_at for {login!r}") from exc
            employee = mapping.get("employee", "").strip() or login
            identity = {
                "employee_id": mapping.get("employee_id", ""),
                "email": mapping.get("email", ""),
            }
            usage_rows.append([employee, identity["employee_id"], identity["email"], application,
                               last_activity, "", "", "", ""])
            billing_rows.append(_billing_export_row(identity, price, application) + [
                "github_copilot", login, organization,
            ])
        if missing_logins:
            raise ConnectorError(
                "Add identity mappings for assigned Copilot users: " + ", ".join(sorted(missing_logins))
            )
        return _write_provider_exports(
            "github_copilot", output_dir, usage_rows, billing_rows,
            "GitHub Copilot last_activity_at; IDE telemetry must be enabled for IDE activity to appear",
        )

    def remove_user_license(self, user_id: str, sku_id: str) -> str:
        """Request cancellation of one organization's Copilot seat for a username."""
        organization, username = sku_id, user_id
        if not organization.strip() or not username.strip() or "/" in organization:
            raise ConnectorError("GitHub Copilot reclaim target must include organization and username")
        token = self.access_token
        seats: list[dict] = []
        for page in range(1, 10001):
            query = urlencode({"per_page": "100", "page": str(page)})
            current = _bearer_json(
                f"https://api.github.com/orgs/{quote(organization, safe='')}/copilot/billing/seats?{query}",
                token, "GitHub Copilot", self.HOSTS,
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            )
            values = current.get("seats")
            if not isinstance(values, list):
                raise ConnectorError("GitHub Copilot response is missing its seats array")
            seats.extend(item for item in values if isinstance(item, dict))
            if len(values) < 100:
                break
        else:
            raise ConnectorError("GitHub Copilot reclaim pagination exceeded the safety limit")
        matches = [seat for seat in seats if str((seat.get("assignee") or {}).get("login", "")).casefold()
                   == username.casefold()]
        if not matches:
            return "seat_already_absent"
        if len(matches) != 1:
            raise ConnectorError("GitHub Copilot username resolved to multiple seats; refusing cancellation")
        url = f"https://api.github.com/orgs/{quote(organization, safe='')}/copilot/billing/selected_users"
        payload = json.dumps({"selected_usernames": [username]}).encode("utf-8")
        _bearer_mutation(
            url, self.access_token, "GitHub Copilot", self.HOSTS, method="DELETE", body=payload,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        return "seat_cancellation_requested"


class ZoomConnector:
    """Import active Zoom licensed users and their last-login timestamps."""

    HOSTS = {"api.zoom.us"}

    def __init__(self, account_id: str, client_id: str, client_secret: str):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret

    @classmethod
    def from_environment(cls) -> "ZoomConnector":
        names = ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET")
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise ConnectorError(f"Set required environment variables: {', '.join(missing)}")
        return cls(*(os.environ[name] for name in names))

    def _token(self) -> str:
        url = "https://zoom.us/oauth/token?" + urlencode({
            "grant_type": "account_credentials", "account_id": self.account_id,
        })
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        try:
            with urlopen(Request(
                url, data=b"", headers={"Authorization": f"Basic {basic}"}, method="POST",
            ), timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ConnectorError(f"Zoom OAuth endpoint returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise ConnectorError(f"Zoom OAuth request failed: {type(exc).__name__}") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise ConnectorError("Zoom OAuth response did not include an access token")
        return token

    def import_exports(self, pricing_path: Path, output_dir: Path) -> tuple[int, int]:
        prices = _validate_prices(pricing_path, {"user_type", "application", "annual_cost"})
        price_map: dict[int, dict[str, str]] = {}
        for line, row in enumerate(prices, start=2):
            try:
                user_type = int(row["user_type"])
            except ValueError as exc:
                raise ConnectorError(f"{pricing_path}:{line}: user_type must be a whole number") from exc
            if user_type in price_map:
                raise ConnectorError(f"{pricing_path}:{line}: duplicate Zoom user_type price mapping")
            if not row.get("application", "").strip():
                raise ConnectorError(f"{pricing_path}:{line}: application cannot be blank")
            price_map[user_type] = row
        token = self._token()
        users: list[dict] = []
        page_token = ""
        while True:
            query = {"status": "active", "page_size": "300"}
            if page_token:
                query["next_page_token"] = page_token
            payload = _bearer_json(
                f"https://api.zoom.us/v2/users?{urlencode(query)}", token, "Zoom", self.HOSTS,
            )
            values = payload.get("users")
            if not isinstance(values, list):
                raise ConnectorError("Zoom users response is missing its users array")
            users.extend(item for item in values if isinstance(item, dict))
            page_token = str(payload.get("next_page_token", ""))
            if not page_token:
                break
        usage_rows: list[list[str]] = []
        billing_rows: list[list[str]] = []
        missing_types: set[int] = set()
        for user in users:
            try:
                user_type = int(user.get("type", 0))
            except (TypeError, ValueError) as exc:
                raise ConnectorError("Zoom returned an invalid user type") from exc
            # Type 2 is a licensed plan; basic and unassigned user types are not billed seats.
            if user_type != 2:
                continue
            price = price_map.get(user_type)
            if not price:
                missing_types.add(user_type)
                continue
            email = str(user.get("email", ""))
            if not email:
                raise ConnectorError("An active Zoom licensed user is missing email")
            application = price["application"]
            last_login = str(user.get("last_login_time", ""))[:10]
            if last_login:
                try:
                    date.fromisoformat(last_login)
                except ValueError as exc:
                    raise ConnectorError(f"Zoom returned invalid last_login_time for {email!r}") from exc
            employee = str(user.get("display_name", "")).strip() or " ".join(
                part for part in (str(user.get("first_name", "")), str(user.get("last_name", ""))) if part
            )
            identity = {"email": email, "employee_id": str(user.get("employee_unique_id", ""))}
            usage_rows.append([employee or email, identity["employee_id"], email, application,
                               last_login, "", "", "", ""])
            user_id = str(user.get("id", ""))
            if not user_id:
                raise ConnectorError("An active Zoom licensed user is missing its stable user ID")
            billing_rows.append(_billing_export_row(identity, price, application) + ["zoom", user_id, str(user_type)])
        if missing_types:
            raise ConnectorError("Add Zoom pricing mappings for licensed user types: " + ", ".join(map(str, sorted(missing_types))))
        return _write_provider_exports(
            "zoom", output_dir, usage_rows, billing_rows,
            "Zoom user last_login_time (provider reports this with a three-day buffer)",
        )

    def remove_user_license(self, user_id: str, sku_id: str) -> str:
        """Convert one verified Zoom Licensed user to Basic to release its paid user license."""
        if not user_id.strip() or sku_id != "2":
            raise ConnectorError("Zoom reclaim target must identify an imported Licensed user")
        token = self._token()
        url = f"https://api.zoom.us/v2/users/{quote(user_id, safe='')}"
        current = _bearer_json(url, token, "Zoom", self.HOSTS)
        if str(current.get("id", "")) != user_id:
            raise ConnectorError("Zoom returned a different user than the requested reclaim target")
        try:
            user_type = int(current.get("type", 0))
        except (TypeError, ValueError) as exc:
            raise ConnectorError("Zoom returned an invalid current user type") from exc
        if user_type == 1:
            return "user_already_basic"
        if user_type != 2:
            raise ConnectorError("Zoom user is no longer Licensed; refusing an unexpected user-type change")
        _bearer_mutation(
            url, token, "Zoom", self.HOSTS, method="PATCH",
            body=json.dumps({"type": 1}).encode("utf-8"),
        )
        updated = _bearer_json(url, token, "Zoom", self.HOSTS)
        if str(updated.get("id", "")) != user_id or updated.get("type") != 1:
            raise ConnectorError("Zoom did not confirm the user downgrade to Basic")
        return "license_changed_to_basic"


def transfer_provider_license(
    provider: str, source_user_id: str, target_email: str, license_id: str, *,
    target_user_id: str = "",
) -> str:
    """Move an approved paid seat between provider users and verify both live assignments.

    Target activation happens before source release where the provider supports it, so
    a failed activation leaves the original user licensed. A partial move raises an
    error and is never retried automatically; the transfer ledger records the outcome.
    """
    provider = provider.casefold()
    target_email = target_email.strip()
    if not source_user_id.strip() or not target_email:
        raise ConnectorError("Transfer requires the imported source user and target email")
    if provider == "microsoft365":
        connector = Microsoft365Connector.from_environment()
        try:
            source_id, sku_id = str(uuid.UUID(source_user_id)), str(uuid.UUID(license_id))
        except ValueError as exc:
            raise ConnectorError("Microsoft 365 transfer requires source and SKU UUIDs") from exc
        token = connector._token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        def graph_user(key: str) -> dict:
            url = f"{GRAPH_ROOT}/users/{quote(key, safe='')}?{urlencode({'$select':'id,mail,userPrincipalName,accountEnabled,assignedLicenses,licenseAssignmentStates'})}"
            return connector._json_request(Request(url, headers=headers))
        source, target = graph_user(source_id), graph_user(target_email)
        if str(source.get("id", "")).casefold() != source_id.casefold():
            raise ConnectorError("Microsoft 365 source identity changed")
        target_id = str(target.get("id", ""))
        if target.get("accountEnabled") is not True:
            raise ConnectorError("Microsoft 365 target account is disabled")
        if not target_id or str(target.get("userPrincipalName", target_email)).casefold() != target_email.casefold() and str(target.get("mail", "")).casefold() != target_email.casefold():
            raise ConnectorError("Microsoft 365 target email did not resolve to the requested user")
        def assigned(user: dict) -> bool:
            return any(str(item.get("skuId", "")).casefold() == sku_id.casefold()
                       for item in user.get("assignedLicenses", []) if isinstance(item, dict))
        if not assigned(source):
            raise ConnectorError("Microsoft 365 source no longer has the approved SKU")
        source_states = [state for state in source.get("licenseAssignmentStates", []) if isinstance(state, dict) and str(state.get("skuId", "")).casefold() == sku_id.casefold()]
        if not source_states or any(state.get("assignedByGroup") for state in source_states) or not any(str(state.get("state", "")).casefold() == "active" for state in source_states):
            raise ConnectorError("Microsoft 365 source SKU is missing, inactive, or group-assigned")
        if assigned(target):
            raise ConnectorError("Microsoft 365 target already has the SKU")
        connector._json_request(Request(f"{GRAPH_ROOT}/users/{quote(target_id, safe='')}/assignLicense", data=json.dumps({"addLicenses":[{"skuId":sku_id,"disabledPlans":[]}],"removeLicenses":[]}).encode(), headers={**headers,"Content-Type":"application/json"}, method="POST"))
        connector._json_request(Request(f"{GRAPH_ROOT}/users/{quote(source_id, safe='')}/assignLicense", data=json.dumps({"addLicenses":[],"removeLicenses":[sku_id]}).encode(), headers={**headers,"Content-Type":"application/json"}, method="POST"))
        if not assigned(graph_user(target_id)) or assigned(graph_user(source_id)):
            raise ConnectorError("Microsoft 365 live assignment verification failed")
        return target_id
    if provider == "google_workspace":
        connector = GoogleWorkspaceConnector.from_environment()
        try:
            product, sku = json.loads(license_id)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("Google Workspace transfer SKU must contain product and SKU IDs") from exc
        path = "/".join(quote(value, safe="") for value in (product, "sku", sku, "user"))
        target_dir = _bearer_json(f"https://admin.googleapis.com/admin/directory/v1/users/{quote(target_email, safe='')}", connector.access_token, "Google Workspace", connector.HOSTS)
        target_key = str(target_dir.get("primaryEmail", ""))
        if target_key.casefold() != target_email.casefold() or target_dir.get("suspended"):
            raise ConnectorError("Google Workspace target email did not resolve exactly")
        target_base = f"https://licensing.googleapis.com/apps/licensing/v1/product/{path}"
        target_url = f"{target_base}/{quote(target_key, safe='')}"
        source_url = f"https://licensing.googleapis.com/apps/licensing/v1/product/{path}/{quote(source_user_id, safe='')}"
        source_assignment = _bearer_json(source_url, connector.access_token, "Google Workspace", connector.HOSTS)
        if (str(source_assignment.get("userId", "")).casefold() != source_user_id.casefold()
                or str(source_assignment.get("productId", "")).casefold() != str(product).casefold()
                or str(source_assignment.get("skuId", "")).casefold() != str(sku).casefold()):
            raise ConnectorError("Google Workspace source assignment no longer matches the approved transfer")
        try:
            current = _bearer_json(target_url, connector.access_token, "Google Workspace", connector.HOSTS)
        except ConnectorError as exc:
            if "HTTP 404" not in str(exc):
                raise
        else:
            if current.get("userId"):
                raise ConnectorError("Google Workspace target already has this license")
        _bearer_mutation(target_base, connector.access_token, "Google Workspace", connector.HOSTS,
                         method="POST", body=json.dumps({"userId":target_key}).encode())
        _bearer_mutation(source_url, connector.access_token, "Google Workspace", connector.HOSTS, method="DELETE")
        verified = _bearer_json(target_url, connector.access_token, "Google Workspace", connector.HOSTS)
        if str(verified.get("userId", "")).casefold() != target_key.casefold():
            raise ConnectorError("Google Workspace target assignment verification failed")
        return target_key
    if provider == "github_copilot":
        if not target_user_id.strip():
            raise ConnectorError("GitHub Copilot transfer requires --identity-map to resolve the target email to a login")
        connector = GitHubCopilotConnector.from_environment()
        organization, source_login = license_id, source_user_id
        base = f"https://api.github.com/orgs/{quote(organization, safe='')}/copilot/billing/selected_users"
        headers = {"Accept":"application/vnd.github+json", "X-GitHub-Api-Version":"2022-11-28"}
        seats: list[dict] = []
        for page in range(1, 10001):
            seats_url = f"https://api.github.com/orgs/{quote(organization, safe='')}/copilot/billing/seats?{urlencode({'per_page':100,'page':page})}"
            values = _bearer_json(seats_url, connector.access_token, "GitHub Copilot", connector.HOSTS, headers=headers).get("seats", [])
            if not isinstance(values, list):
                raise ConnectorError("GitHub Copilot response is missing its seats list")
            seats.extend(value for value in values if isinstance(value, dict))
            if len(values) < 100:
                break
        else:
            raise ConnectorError("GitHub Copilot transfer pagination exceeded the safety limit")
        logins = [str((seat.get("assignee") or {}).get("login", "")).casefold() for seat in seats]
        if source_login.casefold() not in logins:
            raise ConnectorError("GitHub Copilot source seat no longer exists")
        if target_user_id.casefold() in logins:
            raise ConnectorError("GitHub Copilot target already has a seat")
        _bearer_mutation(base, connector.access_token, "GitHub Copilot", connector.HOSTS, method="POST",
                         body=json.dumps({"selected_usernames":[target_user_id]}).encode(), headers=headers)
        _bearer_mutation(base, connector.access_token, "GitHub Copilot", connector.HOSTS, method="DELETE",
                         body=json.dumps({"selected_usernames":[source_login]}).encode(), headers=headers)
        current: list[dict] = []
        for page in range(1, 10001):
            seats_url = f"https://api.github.com/orgs/{quote(organization, safe='')}/copilot/billing/seats?{urlencode({'per_page':100,'page':page})}"
            values = _bearer_json(seats_url, connector.access_token, "GitHub Copilot", connector.HOSTS, headers=headers).get("seats", [])
            if not isinstance(values, list):
                raise ConnectorError("GitHub Copilot response is missing its seats list")
            current.extend(value for value in values if isinstance(value, dict))
            if len(values) < 100:
                break
        else:
            raise ConnectorError("GitHub Copilot verification pagination exceeded the safety limit")
        final = [str((seat.get("assignee") or {}).get("login", "")).casefold() for seat in current]
        if target_user_id.casefold() not in final or source_login.casefold() in final:
            raise ConnectorError("GitHub Copilot live seat verification failed")
        return target_user_id
    if provider == "slack":
        connector = SlackConnector.from_environment()
        team_id = license_id
        source_check = _bearer_json(f"https://slack.com/api/users.info?{urlencode({'user':source_user_id})}", connector.access_token, "Slack", connector.HOSTS)
        source = source_check.get("user") or {}
        if not source_check.get("ok") or str(source.get("id", "")) != source_user_id or source.get("deleted"):
            raise ConnectorError("Slack source user is no longer an active member")
        target = _bearer_json(f"https://slack.com/api/users.lookupByEmail?{urlencode({'email':target_email})}", connector.access_token, "Slack", connector.HOSTS)
        user = target.get("user") or {}
        target_id = str(user.get("id", ""))
        if not target.get("ok") or not target_id or user.get("deleted") or user.get("is_bot"):
            raise ConnectorError("Slack target must be an active human account")
        if not (user.get("is_restricted") or user.get("is_ultra_restricted")):
            raise ConnectorError("Slack target already has a standard member seat")
        payload = urlencode({"team_id":team_id,"user_id":target_id}).encode()
        request = Request("https://slack.com/api/admin.users.setRegular", data=payload,
                          headers={"Authorization":f"Bearer {connector.access_token}","Content-Type":"application/x-www-form-urlencoded"},method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                promoted = json.loads(response.read().decode())
        except Exception as exc:
            raise ConnectorError("Slack regular-member promotion failed") from exc
        if not promoted.get("ok"):
            raise ConnectorError(f"Slack admin.users.setRegular failed ({promoted.get('error','unknown_error')})")
        _slack_admin_remove_user(connector.access_token, team_id, source_user_id)
        check = _bearer_json(f"https://slack.com/api/users.info?{urlencode({'user':target_id})}", connector.access_token, "Slack", connector.HOSTS)
        if not check.get("ok") or not isinstance(check.get("user"), dict) or check["user"].get("is_restricted"):
            raise ConnectorError("Slack target promotion verification failed")
        return target_id
    if provider == "zoom":
        connector = ZoomConnector.from_environment()
        token = connector._token()
        headers = {"Authorization":f"Bearer {token}","Accept":"application/json"}
        source_url = f"https://api.zoom.us/v2/users/{quote(source_user_id,safe='')}"
        source = _bearer_json(source_url, token, "Zoom", connector.HOSTS)
        target = _bearer_json(f"https://api.zoom.us/v2/users/{quote(target_email,safe='')}", token, "Zoom", connector.HOSTS)
        if str(source.get("id")) != source_user_id or str(target.get("email", "")).casefold() != target_email.casefold():
            raise ConnectorError("Zoom source or target identity changed")
        if str(source.get("status", "active")).casefold() != "active" or str(target.get("status", "active")).casefold() != "active":
            raise ConnectorError("Zoom source and target accounts must both be active")
        if int(source.get("type", 0)) != 2 or int(target.get("type", 0)) != 1:
            raise ConnectorError("Zoom transfer requires a Licensed source and Basic target")
        target_id = str(target.get("id", ""))
        _bearer_mutation(f"https://api.zoom.us/v2/users/{quote(target_id,safe='')}", token, "Zoom", connector.HOSTS, method="PATCH", body=b'{"type":2}')
        _bearer_mutation(source_url, token, "Zoom", connector.HOSTS, method="PATCH", body=b'{"type":1}')
        if int(_bearer_json(source_url, token, "Zoom", connector.HOSTS).get("type", 0)) != 1 or int(_bearer_json(f"https://api.zoom.us/v2/users/{quote(target_id,safe='')}", token, "Zoom", connector.HOSTS).get("type", 0)) != 2:
            raise ConnectorError("Zoom live user-type verification failed")
        return target_id
    raise ConnectorError(f"Unsupported transfer provider {provider!r}")
