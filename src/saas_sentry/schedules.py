"""Persistent dashboard schedules and execution history."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .engine import InputError
from .scheduling import next_run, run_with_retry
from .storage import _connect


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate(name: str, schedule_time: str, timezone_name: str) -> tuple[str, str, str]:
    name = " ".join(name.split())
    if not name or len(name) > 100:
        raise InputError("schedule name must contain 1 to 100 characters")
    try:
        ZoneInfo(timezone_name)
        next_run(_now(), schedule_time, timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise InputError(f"invalid schedule time or IANA timezone: {exc}") from exc
    return name, schedule_time, timezone_name


def _row(connection: sqlite3.Connection, schedule_id: str) -> dict[str, object] | None:
    result = connection.execute(
        "SELECT schedule_id,name,schedule_time,timezone,enabled,next_run_at,created_by,created_at,"
        "updated_at,last_status,last_error FROM dashboard_schedules WHERE schedule_id=?",
        (schedule_id,),
    ).fetchone()
    if not result:
        return None
    fields = ("schedule_id", "name", "schedule_time", "timezone", "enabled", "next_run_at",
              "created_by", "created_at", "updated_at", "last_status", "last_error")
    schedule = dict(zip(fields, result))
    schedule["enabled"] = bool(schedule["enabled"])
    schedule["history"] = [dict(zip(
        ("run_id", "scheduled_for", "started_at", "finished_at", "status", "attempts", "error", "report_path"), run,
    )) for run in connection.execute(
        "SELECT run_id,scheduled_for,started_at,finished_at,status,attempts,error,report_path "
        "FROM dashboard_schedule_runs WHERE schedule_id=? ORDER BY started_at DESC LIMIT 20",
        (schedule_id,),
    )]
    schedule["last_result"] = schedule["last_status"] or "not_run"
    schedule["next_run"] = schedule["next_run_at"] if schedule["enabled"] else None
    return schedule


def create_schedule(database: Path, *, name: str, schedule_time: str, timezone_name: str,
                    actor: str, now: datetime | None = None) -> dict[str, object]:
    name, schedule_time, timezone_name = _validate(name, schedule_time, timezone_name)
    actor = actor.strip()
    if not actor:
        raise InputError("schedule creator cannot be blank")
    current = (now or _now()).astimezone(timezone.utc)
    schedule_id = uuid.uuid4().hex[:16]
    timestamp = current.isoformat()
    next_at = next_run(current, schedule_time, timezone_name).isoformat()
    connection = _connect(database)
    try:
        connection.execute(
            "INSERT INTO dashboard_schedules(schedule_id,name,schedule_time,timezone,enabled,next_run_at,"
            "created_by,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?,?)",
            (schedule_id, name, schedule_time, timezone_name, next_at, actor, timestamp, timestamp),
        )
        return _row(connection, schedule_id) or {}
    except sqlite3.Error as exc:
        raise InputError(f"Cannot create dashboard schedule: {exc}") from exc
    finally:
        connection.close()


def list_schedules(database: Path) -> tuple[dict[str, object], ...]:
    connection = _connect(database)
    try:
        ids = [row[0] for row in connection.execute(
            "SELECT schedule_id FROM dashboard_schedules ORDER BY name COLLATE NOCASE, schedule_id"
        )]
        schedules = []
        for schedule_id in ids:
            schedule = _row(connection, schedule_id)
            if schedule:
                schedules.append(schedule)
        return tuple(schedules)
    finally:
        connection.close()


def update_schedule(database: Path, schedule_id: str, *, name: str, schedule_time: str,
                    timezone_name: str) -> dict[str, object]:
    name, schedule_time, timezone_name = _validate(name, schedule_time, timezone_name)
    connection = _connect(database)
    try:
        current = connection.execute(
            "SELECT enabled FROM dashboard_schedules WHERE schedule_id=?", (schedule_id,),
        ).fetchone()
        if not current:
            raise InputError("dashboard schedule was not found")
        now = _now()
        next_at = next_run(now, schedule_time, timezone_name).isoformat()
        connection.execute(
            "UPDATE dashboard_schedules SET name=?,schedule_time=?,timezone=?,next_run_at=?,updated_at=? "
            "WHERE schedule_id=?",
            (name, schedule_time, timezone_name, next_at, now.isoformat(), schedule_id),
        )
        return _row(connection, schedule_id) or {}
    except sqlite3.Error as exc:
        raise InputError(f"Cannot update dashboard schedule: {exc}") from exc
    finally:
        connection.close()


def set_schedule_enabled(database: Path, schedule_id: str, *, enabled: bool) -> dict[str, object]:
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT schedule_time,timezone FROM dashboard_schedules WHERE schedule_id=?", (schedule_id,),
        ).fetchone()
        if not row:
            raise InputError("dashboard schedule was not found")
        now = _now()
        next_at = next_run(now, row[0], row[1]).isoformat()
        connection.execute(
            "UPDATE dashboard_schedules SET enabled=?,next_run_at=?,updated_at=? WHERE schedule_id=?",
            (int(enabled), next_at, now.isoformat(), schedule_id),
        )
        return _row(connection, schedule_id) or {}
    except sqlite3.Error as exc:
        raise InputError(f"Cannot update dashboard schedule state: {exc}") from exc
    finally:
        connection.close()


def _claim_run(database: Path, schedule_id: str, scheduled_for: str, *, require_due: bool) -> tuple[str, dict[str, object]] | None:
    connection = _connect(database)
    run_id = uuid.uuid4().hex
    started = _now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT schedule_id,name,schedule_time,timezone,enabled,next_run_at FROM dashboard_schedules WHERE schedule_id=?",
            (schedule_id,),
        ).fetchone()
        if not job:
            raise InputError("dashboard schedule was not found")
        if require_due and (not job[4] or job[5] > scheduled_for or job[5] > started.isoformat()):
            connection.rollback()
            return None
        connection.execute(
            "INSERT INTO dashboard_schedule_runs(run_id,schedule_id,scheduled_for,started_at,status) "
            "VALUES(?,?,?,?, 'running')",
            (run_id, schedule_id, scheduled_for, started.isoformat()),
        )
        connection.commit()
        return run_id, {"schedule_id": job[0], "name": job[1], "schedule_time": job[2], "timezone": job[3],
                        "enabled": bool(job[4]), "scheduled_for": scheduled_for, "started_at": started.isoformat()}
    except sqlite3.IntegrityError:
        connection.rollback()
        return None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def run_schedule(database: Path, schedule_id: str, operation, *, scheduled_for: str | None = None,
                 require_due: bool = False, retries: int = 2, base_delay: float = 1.0) -> dict[str, object] | None:
    run_at = scheduled_for or f"manual:{uuid.uuid4().hex}"
    claimed = _claim_run(database, schedule_id, run_at, require_due=require_due)
    if claimed is None:
        return None
    run_id, job = claimed
    attempts = 0

    def attempt():
        nonlocal attempts
        attempts += 1
        return operation(job, run_id)

    try:
        report_path = str(run_with_retry(attempt, retries=retries, base_delay=base_delay))
        status, error = "succeeded", ""
    except Exception as exc:
        report_path, status, error = "", "failed", f"{type(exc).__name__}: {exc}"[:1000]
    finished = _now()
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE dashboard_schedule_runs SET finished_at=?,status=?,attempts=?,error=?,report_path=? WHERE run_id=?",
            (finished.isoformat(), status, attempts, error, report_path, run_id),
        )
        if scheduled_for:
            state = connection.execute(
                "SELECT enabled,schedule_time,timezone FROM dashboard_schedules WHERE schedule_id=?", (schedule_id,),
            ).fetchone()
            if state:
                next_at = next_run(finished, state[1], state[2]).isoformat()
                connection.execute(
                    "UPDATE dashboard_schedules SET next_run_at=?,last_status=?,last_error=?,updated_at=? WHERE schedule_id=?",
                    (next_at, status, error, finished.isoformat(), schedule_id),
                )
        else:
            connection.execute(
                "UPDATE dashboard_schedules SET last_status=?,last_error=?,updated_at=? WHERE schedule_id=?",
                (status, error, finished.isoformat(), schedule_id),
            )
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise InputError(f"Cannot record dashboard schedule run: {exc}") from exc
    finally:
        connection.close()
    return {"run_id": run_id, "schedule_id": schedule_id, "scheduled_for": run_at,
            "started_at": job["started_at"], "finished_at": finished.isoformat(), "status": status,
            "attempts": attempts, "error": error, "report_path": report_path}


def run_due_schedules(database: Path, operation, *, now: datetime | None = None) -> int:
    current = (now or _now()).astimezone(timezone.utc).isoformat()
    connection = _connect(database)
    try:
        due = connection.execute(
            "SELECT schedule_id,next_run_at FROM dashboard_schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at",
            (current,),
        ).fetchall()
    finally:
        connection.close()
    count = 0
    for schedule_id, scheduled_for in due:
        if run_schedule(database, schedule_id, operation, scheduled_for=scheduled_for, require_due=True):
            count += 1
    return count
