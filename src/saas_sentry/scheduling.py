"""Timezone-aware scheduling helpers and durable run-health records."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .engine import InputError

T = TypeVar("T")


def next_run(now: datetime, local_time: str, timezone_name: str) -> datetime:
    """Return the next local wall-clock occurrence as an aware UTC datetime."""
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone {timezone_name!r}") from exc
    try:
        hour, minute = (int(part) for part in local_time.split(":"))
        target_time = time(hour, minute)
    except (ValueError, TypeError) as exc:
        raise ValueError("schedule time must use HH:MM (24-hour clock)") from exc
    if len(local_time.split(":")) != 2:
        raise ValueError("schedule time must use HH:MM (24-hour clock)")
    if now.tzinfo is None:
        raise ValueError("current time must include a timezone")
    local_now = now.astimezone(zone)
    candidate = datetime.combine(local_now.date(), target_time, tzinfo=zone)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), target_time, tzinfo=zone)
    # For a wall time skipped by a spring-forward transition, run at the first
    # valid minute after the gap. Ambiguous fall-back times use the first fold.
    for _ in range(181):
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip == candidate.replace(tzinfo=None):
            break
        candidate += timedelta(minutes=1)
    return candidate.astimezone(timezone.utc)


def run_with_retry(
    operation: Callable[[], T], *, retries: int, base_delay: float,
    sleep: Callable[[float], None] = __import__("time").sleep,
) -> T:
    """Retry an idempotent scheduled cycle with capped exponential backoff."""
    if retries < 0 or base_delay < 0:
        raise ValueError("retries and base_delay must be non-negative")
    for attempt in range(retries + 1):
        try:
            return operation()
        except Exception:
            if attempt >= retries:
                raise
            sleep(min(base_delay * (2 ** attempt), 300.0))
    raise RuntimeError("unreachable retry state")


def write_health(path: Path, *, status: str, attempts: int, timezone_name: str,
                 last_started_at: datetime, next_scheduled_at: datetime | None,
                 error: str = "") -> None:
    """Atomically persist the latest scheduled run outcome for operators."""
    record = {
        "status": status,
        "attempts": attempts,
        "timezone": timezone_name,
        "last_started_at": last_started_at.astimezone(timezone.utc).isoformat(),
        "last_finished_at": datetime.now(timezone.utc).isoformat(),
        "next_scheduled_at": next_scheduled_at.astimezone(timezone.utc).isoformat() if next_scheduled_at else "",
        "error": error[:500],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise InputError(f"Cannot write schedule health {path}: {exc}") from exc
