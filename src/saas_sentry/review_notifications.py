"""Owner-grouped webhook delivery for access-review reminders."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .engine import InputError


REMINDER_FIELDS = (
    "campaign_id", "finding_id", "application", "owner", "due_on",
    "days_until_due", "reminder_count", "reminder_reason",
)
DELIVERY_FIELDS = (
    "delivery_id", "campaign_id", "finding_id", "owner", "status",
    "attempted_at", "destination", "detail",
)


def _read_reminders(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = set(REMINDER_FIELDS) - set(reader.fieldnames or [])
            if missing:
                raise InputError(f"{path}: reminder CSV missing columns: {', '.join(sorted(missing))}")
            rows = []
            for line, raw in enumerate(reader, start=2):
                row = {field: (raw.get(field) or "").strip() for field in REMINDER_FIELDS}
                if not row["campaign_id"] or not row["finding_id"] or not row["owner"]:
                    raise InputError(f"{path}:{line}: campaign, finding, and owner are required")
                try:
                    count = int(row["reminder_count"])
                    datetime.strptime(row["due_on"], "%Y-%m-%d")
                    int(row["days_until_due"])
                except ValueError as exc:
                    raise InputError(f"{path}:{line}: invalid reminder count or date") from exc
                if count < 1:
                    raise InputError(f"{path}:{line}: reminder_count must be positive")
                rows.append(row)
            return rows
    except OSError as exc:
        raise InputError(f"Cannot read reminder CSV {path}: {exc}") from exc


def _delivery_id(row: dict[str, str], destination: str) -> str:
    identity = "\0".join((destination, row["campaign_id"], row["finding_id"], row["reminder_count"]))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _delivered_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(DELIVERY_FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: reminder delivery ledger is missing required columns")
            return {
                row["delivery_id"] for row in reader
                if row.get("delivery_id") and row.get("status") == "delivered"
            }
    except OSError as exc:
        raise InputError(f"Cannot read reminder delivery ledger {path}: {exc}") from exc


def deliver_review_reminders(
    reminders_path: Path,
    ledger_path: Path,
    *,
    webhook_url: str,
    send: bool = False,
) -> tuple[int, int, int]:
    """Preview or deliver reminders grouped by owner, retrying undelivered entries."""
    parsed = urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InputError("webhook URL must be HTTPS and cannot include embedded credentials")
    rows = _read_reminders(reminders_path)
    delivered = _delivered_ids(ledger_path)
    pending = [row for row in rows if _delivery_id(row, webhook_url) not in delivered]
    if not send or not pending:
        return len(pending), 0, 0

    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise InputError(f"Reminder delivery ledger is locked at {lock_path}") from exc
    sent_count = failed_count = 0
    try:
        delivered = _delivered_ids(ledger_path)
        pending = [row for row in pending if _delivery_id(row, webhook_url) not in delivered]
        groups: dict[str, list[dict[str, str]]] = {}
        for row in pending:
            groups.setdefault(row["owner"], []).append(row)
        timestamp = datetime.now(timezone.utc).isoformat()
        token = os.environ.get("SAAS_SENTRY_REVIEW_WEBHOOK_TOKEN", "")
        for owner, owner_rows in groups.items():
            payload = json.dumps({
                "source": "saas-sentry-access-review",
                "owner": owner,
                "reminders": owner_rows,
            }).encode("utf-8")
            headers = {"Content-Type": "application/json", "User-Agent": "saas-sentry/0.1"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            status, detail = "delivered", ""
            try:
                with urlopen(Request(webhook_url, data=payload, headers=headers, method="POST"), timeout=20) as response:
                    if not 200 <= response.status < 300:
                        status, detail = "failed", f"HTTP {response.status}"
            except HTTPError as exc:
                status, detail = "failed", f"HTTP {exc.code}"
            except (URLError, TimeoutError, OSError) as exc:
                status, detail = "failed", f"delivery failed: {type(exc).__name__}"
            exists = ledger_path.exists() and ledger_path.stat().st_size > 0
            try:
                with ledger_path.open("a", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=DELIVERY_FIELDS)
                    if not exists:
                        writer.writeheader()
                    for row in owner_rows:
                        writer.writerow({
                            "delivery_id": _delivery_id(row, webhook_url),
                            "campaign_id": row["campaign_id"], "finding_id": row["finding_id"],
                            "owner": owner, "status": status, "attempted_at": timestamp,
                            "destination": parsed.hostname, "detail": detail,
                        })
            except OSError as exc:
                raise InputError(f"Cannot write reminder delivery ledger {ledger_path}: {exc}") from exc
            if status == "delivered":
                sent_count += len(owner_rows)
            else:
                failed_count += len(owner_rows)
        return len(pending), sent_count, failed_count
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass
