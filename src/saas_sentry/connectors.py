"""Read-only provider data imports."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


class ConnectorError(RuntimeError):
    """Raised when provider authentication, permissions, or data is invalid."""


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


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
            with urlopen(request, timeout=30) as response:
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
                usage_rows.append([employee, employee_id, principal, sku_part, last_login, "", "", "", ""])
                billing_rows.append([
                    employee_id, principal, sku_part, "active", price.get("annual_cost", ""),
                    price.get("currency", ""), price.get("contract_id", "") or sku_part,
                    price.get("renewal_date", ""), price.get("seat_minimum", "0") or "0",
                    price.get("commitment_end_date", ""), price.get("notice_days", "0") or "0",
                    price.get("bundle_group", ""),
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
            "bundle_group",
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
