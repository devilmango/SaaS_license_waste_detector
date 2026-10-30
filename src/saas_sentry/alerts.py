"""Deduplicated JSON webhook delivery for monitor findings."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .engine import InputError
from .storage import _connect, is_sqlite_path


ALERT_FIELDS = ("severity", "kind", "scope", "baseline_value", "current_value", "change", "summary")
LEDGER_FIELDS = (
    "alert_id", "status", "attempts", "last_attempt_at", "delivered_at",
    "destination", "last_error", "manual_follow_up", "payload",
)
LEGACY_LEDGER_FIELDS = ("alert_id", "delivered_at", "destination")


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


def _delivery_states(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    if is_sqlite_path(path):
        connection = _connect(path)
        try:
            rows = connection.execute(
                "SELECT alert_id,status,attempts,last_attempt_at,delivered_at,destination,last_error,manual_follow_up,payload "
                "FROM alert_deliveries ORDER BY alert_id"
            ).fetchall()
        finally:
            connection.close()
        return {row[0]: {
            "alert_id": row[0], "status": row[1], "attempts": row[2],
            "last_attempt_at": row[3], "delivered_at": row[4], "destination": row[5],
            "last_error": row[6], "manual_follow_up": bool(row[7]), "payload": json.loads(row[8]),
        } for row in rows}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(LEGACY_LEDGER_FIELDS).issubset(reader.fieldnames or []):
                raise InputError(f"{path}: delivery ledger is missing required columns")
            states: dict[str, dict[str, object]] = {}
            for row in reader:
                if not row.get("alert_id"):
                    continue
                states[row["alert_id"]] = {
                    "alert_id": row["alert_id"],
                    "status": row.get("status") or ("delivered" if row.get("delivered_at") else "pending"),
                    "attempts": int(row.get("attempts") or (1 if row.get("delivered_at") else 0)),
                    "last_attempt_at": row.get("last_attempt_at") or row.get("delivered_at", ""),
                    "delivered_at": row.get("delivered_at", ""), "destination": row.get("destination", ""),
                    "last_error": row.get("last_error", ""),
                    "manual_follow_up": str(row.get("manual_follow_up", "")).casefold() in {"true", "1", "yes"},
                    "payload": json.loads(row["payload"]) if row.get("payload") else {},
                }
            return states
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise InputError(f"Cannot read alert delivery ledger {path}: {exc}") from exc


def list_delivery_records(path: Path) -> tuple[dict[str, object], ...]:
    """Return current per-alert delivery states, including manual follow-up items."""
    states = _delivery_states(path)
    return tuple(states[key] for key in sorted(states))


def retry_manual_alert(path: Path, alert_id: str) -> None:
    """Clear an exhausted alert for one explicit operator-requested retry."""
    states = _delivery_states(path)
    state = states.get(alert_id)
    if not state or not state["manual_follow_up"]:
        raise InputError("alert is not waiting for manual follow-up")
    state.update({"status": "pending", "attempts": 0, "last_error": "Manual retry requested",
                  "manual_follow_up": False})
    _write_delivery_state(path, state)


def _write_delivery_state(path: Path, state: dict[str, object]) -> None:
    if is_sqlite_path(path):
        connection = _connect(path)
        try:
            connection.execute(
                "INSERT INTO alert_deliveries(alert_id,payload,status,attempts,last_attempt_at,delivered_at,"
                "destination,last_error,manual_follow_up) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(alert_id) DO UPDATE SET payload=excluded.payload,status=excluded.status,"
                "attempts=excluded.attempts,last_attempt_at=excluded.last_attempt_at,"
                "delivered_at=excluded.delivered_at,destination=excluded.destination,"
                "last_error=excluded.last_error,manual_follow_up=excluded.manual_follow_up",
                (state["alert_id"], json.dumps(state["payload"], ensure_ascii=False), state["status"],
                 state["attempts"], state["last_attempt_at"], state["delivered_at"], state["destination"],
                 state["last_error"], int(bool(state["manual_follow_up"]))),
            )
        except sqlite3.Error as exc:
            raise InputError(f"Cannot update SQLite alert delivery ledger {path}: {exc}") from exc
        finally:
            connection.close()
        return
    _upgrade_csv_ledger(path)
    exists = path.exists() and path.stat().st_size > 0
    try:
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
            if not exists:
                writer.writeheader()
            row = {field: state.get(field, "") for field in LEDGER_FIELDS}
            row["payload"] = json.dumps(state.get("payload", {}), ensure_ascii=False, separators=(",", ":"))
            row["manual_follow_up"] = str(bool(state.get("manual_follow_up", False))).lower()
            writer.writerow(row)
    except OSError as exc:
        raise InputError(f"Cannot write alert delivery ledger {path}: {exc}") from exc


def _upgrade_csv_ledger(path: Path) -> None:
    if not path.exists() or not path.stat().st_size:
        return
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if set(LEDGER_FIELDS).issubset(reader.fieldnames or []):
                return
            legacy = list(reader)
        temporary = path.with_name(path.name + ".upgrade-tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
            writer.writeheader()
            for row in legacy:
                writer.writerow({
                    "alert_id": row.get("alert_id", ""),
                    "status": "delivered" if row.get("delivered_at") else "pending",
                    "attempts": 1 if row.get("delivered_at") else 0,
                    "last_attempt_at": row.get("delivered_at", ""),
                    "delivered_at": row.get("delivered_at", ""),
                    "destination": row.get("destination", ""), "payload": "{}",
                })
        temporary.replace(path)
    except OSError as exc:
        raise InputError(f"Cannot upgrade alert delivery ledger {path}: {exc}") from exc


def deliver_alerts(
    alerts_path: Path,
    ledger_path: Path,
    *,
    webhook_url: str,
    send: bool = False,
    retries: int = 2,
    max_attempts: int = 5,
    base_delay: float = 1.0,
) -> tuple[int, int]:
    """Retry new alerts and retain delivery state; preview mode makes no network request."""
    parsed = urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InputError("webhook URL must be HTTPS and cannot include embedded credentials")
    alerts = _load_alerts(alerts_path)
    if retries < 0 or max_attempts < 1 or base_delay < 0:
        raise InputError("retries must be non-negative, max_attempts positive, and base_delay non-negative")
    states = _delivery_states(ledger_path)
    pending = [(_alert_id(row, webhook_url), row) for row in alerts
               if (state := states.get(_alert_id(row, webhook_url))) is None
               or (state["status"] != "delivered" and not state["manual_follow_up"])]
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
        states = _delivery_states(ledger_path)
        pending = [(key, row) for key, row in pending if (state := states.get(key)) is None
                   or (state["status"] != "delivered" and not state["manual_follow_up"])]
        if not pending:
            return 0, 0
        headers = {"Content-Type": "application/json", "User-Agent": "saas-sentry/0.1"}
        token = os.environ.get("SAAS_SENTRY_WEBHOOK_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        delivered = 0
        for key, row in pending:
            previous = states.get(key, {})
            attempts = int(previous.get("attempts", 0))
            for attempt_index in range(retries + 1):
                if attempts >= max_attempts:
                    break
                attempts += 1
                from datetime import datetime, timezone
                timestamp = datetime.now(timezone.utc).isoformat()
                error = ""
                try:
                    payload = json.dumps({"source": "saas-sentry", "alerts": [row]}).encode("utf-8")
                    with urlopen(Request(webhook_url, data=payload, headers=headers, method="POST"), timeout=20) as response:
                        if not 200 <= response.status < 300:
                            raise InputError(f"webhook returned HTTP {response.status}")
                    delivered_at = timestamp
                    status = "delivered"
                    delivered += 1
                except HTTPError as exc:
                    error = f"webhook returned HTTP {exc.code}"
                    status, delivered_at = "failed", ""
                except (URLError, TimeoutError, OSError, InputError) as exc:
                    error = f"webhook delivery failed: {type(exc).__name__}: {str(exc)[:240]}"
                    status, delivered_at = "failed", ""
                manual = status != "delivered" and attempts >= max_attempts
                state = {
                    "alert_id": key, "status": status if status == "delivered" or manual else "pending",
                    "attempts": attempts, "last_attempt_at": timestamp, "delivered_at": delivered_at,
                    "destination": parsed.hostname, "last_error": error,
                    "manual_follow_up": manual, "payload": row,
                }
                _write_delivery_state(ledger_path, state)
                states[key] = state
                if status == "delivered" or manual:
                    break
                if attempt_index < retries:
                    time.sleep(min(base_delay * (2 ** (attempts - 1)), 300.0))
        return len(pending), delivered
    finally:
        lock.close()
        try:
            lock_path.unlink()
        except OSError:
            pass
