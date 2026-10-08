"""Deduplicated JSON webhook delivery for monitor findings."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .engine import InputError


ALERT_FIELDS = ("severity", "kind", "scope", "baseline_value", "current_value", "change", "summary")
LEDGER_FIELDS = ("alert_id", "delivered_at", "destination")


def _load_alerts(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = set(ALERT_FIELDS) - set(reader.fieldnames or [])
            if missing:
                raise InputError(f"{path}: alert CSV missing columns: {', '.join(sorted(missing))}")
            return [{field: (row.get(field) or "").strip() for field in ALERT_FIELDS} for row in reader]
    except OSError as exc:
        raise InputError(f"Cannot read alert CSV {path}: {exc}") from exc


def _alert_id(row: dict[str, str], webhook_url: str) -> str:
    encoded = json.dumps(
        {"destination": webhook_url, "alert": row},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sent_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(LEDGER_FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: delivery ledger is missing required columns")
            return {row["alert_id"] for row in reader if row.get("alert_id")}
    except OSError as exc:
        raise InputError(f"Cannot read alert delivery ledger {path}: {exc}") from exc


def deliver_alerts(
    alerts_path: Path,
    ledger_path: Path,
    *,
    webhook_url: str,
    send: bool = False,
) -> tuple[int, int]:
    """Deliver new alerts once per ledger; preview mode makes no network request."""
    parsed = urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InputError("webhook URL must be HTTPS and cannot include embedded credentials")
    alerts = _load_alerts(alerts_path)
    sent = _sent_ids(ledger_path)
    pending = [(_alert_id(row, webhook_url), row) for row in alerts if _alert_id(row, webhook_url) not in sent]
    if not send or not pending:
        return len(pending), 0
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise InputError(f"Alert delivery ledger is locked at {lock_path}") from exc
    try:
        # Re-read under the lock so concurrent runs cannot duplicate delivery.
        sent = _sent_ids(ledger_path)
        pending = [(key, row) for key, row in pending if key not in sent]
        if not pending:
            return 0, 0
        payload = json.dumps({"source": "saas-sentry", "alerts": [row for _, row in pending]}).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "saas-sentry/0.1"}
        token = os.environ.get("SAAS_SENTRY_WEBHOOK_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urlopen(Request(webhook_url, data=payload, headers=headers, method="POST"), timeout=20) as response:
                if not 200 <= response.status < 300:
                    raise InputError(f"webhook returned HTTP {response.status}")
        except HTTPError as exc:
            raise InputError(f"webhook returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise InputError(f"webhook delivery failed: {type(exc).__name__}") from exc
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).isoformat()
        exists = ledger_path.exists() and ledger_path.stat().st_size > 0
        with ledger_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
            if not exists:
                writer.writeheader()
            for key, _row in pending:
                writer.writerow({"alert_id": key, "delivered_at": timestamp, "destination": parsed.hostname})
        return len(pending), len(pending)
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass
