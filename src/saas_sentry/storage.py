"""Small SQLite-backed append-only event store for shared operator workflows."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterable
from urllib.parse import quote

from .engine import InputError


SCHEMA_VERSION = 3


def _migration_1_core(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS sentry_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def _migration_2_schedules(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_schedules ("
        "schedule_id TEXT PRIMARY KEY, name TEXT NOT NULL, schedule_time TEXT NOT NULL, "
        "timezone TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, next_run_at TEXT NOT NULL, "
        "created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "last_status TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '')"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_schedule_runs ("
        "run_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, scheduled_for TEXT NOT NULL, "
        "started_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', report_path TEXT NOT NULL DEFAULT '', "
        "UNIQUE(schedule_id, scheduled_for), FOREIGN KEY(schedule_id) REFERENCES dashboard_schedules(schedule_id))"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS dashboard_schedule_runs_recent "
        "ON dashboard_schedule_runs(schedule_id, started_at DESC)"
    )


def _migration_3_alert_delivery(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS alert_deliveries ("
        "alert_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT NOT NULL DEFAULT '', "
        "delivered_at TEXT NOT NULL DEFAULT '', destination TEXT NOT NULL DEFAULT '', "
        "last_error TEXT NOT NULL DEFAULT '', manual_follow_up INTEGER NOT NULL DEFAULT 0)"
    )


def is_sqlite_path(path: Path) -> bool:
    return path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        migrate_database_connection(connection)
        return connection
    except sqlite3.Error as exc:
        raise InputError(f"Cannot open SQLite ledger {path}: {exc}") from exc


def migrate_database_connection(connection: sqlite3.Connection) -> int:
    """Apply ordered, forward-only schema migrations to an open database."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    current = int(connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise InputError(f"SQLite database schema version {current} is newer than supported version {SCHEMA_VERSION}")
    migrations = (_migration_1_core, _migration_2_schedules, _migration_3_alert_delivery)
    for version, migration in enumerate(migrations, start=1):
        if version <= current:
            continue
        try:
            connection.execute("BEGIN IMMEDIATE")
            migration(connection)
            connection.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return SCHEMA_VERSION


def database_version(path: Path) -> int:
    connection = _connect(path)
    try:
        return int(connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
    except sqlite3.Error as exc:
        raise InputError(f"Cannot read SQLite schema version from {path}: {exc}") from exc
    finally:
        connection.close()


def read_events(path: Path, table: str) -> list[dict[str, str]]:
    if not re.fullmatch(r"[a-z_]+", table):
        raise ValueError("invalid SQLite event table")
    if not path.exists():
        return []
    connection = _connect(path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        if not exists:
            return []
        rows = connection.execute(f"SELECT payload FROM {table} ORDER BY event_id").fetchall()
        try:
            return [json.loads(row[0]) for row in rows]
        except (TypeError, json.JSONDecodeError) as exc:
            raise InputError(f"{path}: SQLite event ledger contains invalid JSON") from exc
    except sqlite3.Error as exc:
        raise InputError(f"Cannot read SQLite ledger {path}: {exc}") from exc
    finally:
        connection.close()


def append_event(
    path: Path,
    table: str,
    fields: Iterable[str],
    values: dict[str, str],
    *,
    validate: Callable[[list[dict[str, str]]], object],
) -> None:
    """Validate a new event against current state and append it under a write transaction."""
    if not re.fullmatch(r"[a-z_]+", table):
        raise ValueError("invalid SQLite event table")
    columns = tuple(fields)
    event = {field: values.get(field, "") for field in columns}
    connection = _connect(path)
    try:
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "event_id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
        )
        connection.execute("BEGIN IMMEDIATE")
        current = [
            json.loads(row[0])
            for row in connection.execute(f"SELECT payload FROM {table} ORDER BY event_id").fetchall()
        ]
        validate([*current, event])
        connection.execute(
            f"INSERT INTO {table} (payload) VALUES (?)",
            (json.dumps(event, ensure_ascii=False, separators=(",", ":")),),
        )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, InputError):
            raise
        if isinstance(exc, sqlite3.Error):
            raise InputError(f"Cannot append SQLite ledger {path}: {exc}") from exc
        raise
    finally:
        connection.close()


def migrate_csv_ledger(source: Path, database: Path, kind: str) -> int:
    """Validate and copy one legacy CSV event log into its SQLite event table."""
    if source.resolve() == database.resolve():
        raise InputError("CSV source and SQLite destination must be different files")
    if not is_sqlite_path(database):
        raise InputError("SQLite destination must use a .db, .sqlite, or .sqlite3 extension")
    if kind == "actions":
        from .actions import _append_event, _load_events

        events, _ = _load_events(source)
        table = "action_events"
        append = _append_event
    elif kind == "access-review":
        from .access_reviews import _append, _load_events

        events, _ = _load_events(source)
        table = "access_review_events"
        append = _append
    elif kind == "exceptions":
        from .policy import FIELDS, _append

        events = _read_csv_events(source, FIELDS)
        table = "exception_events"
        append = _append
    elif kind == "reassignments":
        from .reassignment_workflow import _read_csv_events as read_reassignment_events, _append

        events = read_reassignment_events(source)
        table = "reassignment_events"
        append = _append
    elif kind == "reviews":
        from .engine import load_reviews

        records = load_reviews(source)
        events = [records[key] for key in sorted(records)]
        table = "review_events"
        append = None
    else:
        raise InputError("unsupported ledger kind")
    if read_events(database, table):
        raise InputError(f"SQLite {kind} ledger already contains events")
    for event in events:
        if kind == "reviews":
            append_event(database, table, event.keys(), event, validate=lambda _events: None)
        else:
            append(database, event)
    return len(events)


def _read_csv_events(source: Path, fields: Iterable[str]) -> list[dict[str, str]]:
    import csv

    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not set(fields).issubset(reader.fieldnames):
                raise InputError(f"{source}: ledger is missing required columns")
            return [{field: row.get(field, "") or "" for field in fields} for row in reader]
    except OSError as exc:
        raise InputError(f"Cannot read ledger {source}: {exc}") from exc


def backup_database(source: Path, destination: Path) -> None:
    """Create a consistent online SQLite backup using SQLite's backup API."""
    if not is_sqlite_path(source) or not is_sqlite_path(destination):
        raise InputError("SQLite backup source and destination must use .db, .sqlite, or .sqlite3")
    if source.resolve() == destination.resolve():
        raise InputError("SQLite backup destination must differ from its source")
    if not source.exists():
        raise InputError(f"SQLite database does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = _connect(source)
    try:
        target_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    except sqlite3.Error as exc:
        raise InputError(f"Cannot back up SQLite database {source}: {exc}") from exc
    finally:
        source_connection.close()
    result = verify_database(destination)
    if not result["valid"]:
        raise InputError(f"SQLite backup failed verification: {destination}")


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def verify_database(path: Path) -> dict[str, object]:
    """Check SQLite integrity, foreign keys, and recorded schema level without writing."""
    if not is_sqlite_path(path) or not path.exists():
        raise InputError(f"SQLite database does not exist or has an unsupported extension: {path}")
    try:
        connection = _readonly_connection(path)
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
            has_migrations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            version = int(connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]) if has_migrations else 0
            tables = sorted(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ))
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise InputError(f"Cannot verify SQLite database {path}: {exc}") from exc
    current = version <= SCHEMA_VERSION and version > 0
    required_tables = {
        1: {"schema_migrations"},
        2: {"schema_migrations", "dashboard_schedules", "dashboard_schedule_runs"},
        3: {"schema_migrations", "dashboard_schedules", "dashboard_schedule_runs", "alert_deliveries"},
    }
    tables_complete = current and required_tables.get(version, set()).issubset(tables)
    return {
        "path": str(path), "valid": integrity == "ok" and not foreign_keys and tables_complete,
        "integrity_check": integrity, "foreign_key_violations": foreign_keys,
        "schema_version": version, "supported_schema_version": SCHEMA_VERSION,
        "schema_supported": current, "schema_complete": tables_complete,
        "schema_current": version == SCHEMA_VERSION, "tables": tables,
    }


def _table_counts(path: Path) -> dict[str, int]:
    connection = _readonly_connection(path)
    try:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        counts = {}
        for name in names:
            quoted_name = name.replace('"', '""')
            counts[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{quoted_name}"').fetchone()[0])
        return counts
    finally:
        connection.close()


def preview_restore(backup: Path, destination: Path) -> dict[str, object]:
    """Describe a restore and verify its source without modifying either database."""
    if not is_sqlite_path(backup) or not is_sqlite_path(destination):
        raise InputError("SQLite restore source and destination must use .db, .sqlite, or .sqlite3")
    if backup.resolve() == destination.resolve():
        raise InputError("SQLite restore source and destination must differ")
    if not backup.exists():
        raise InputError(f"SQLite backup does not exist: {backup}")
    verification = verify_database(backup)
    if not verification["valid"]:
        raise InputError(f"SQLite backup failed integrity or schema verification: {backup}")
    source_counts = _table_counts(backup)
    target_exists = destination.exists()
    target_counts = _table_counts(destination) if target_exists else {}
    return {
        "backup": str(backup), "destination": str(destination), "backup_valid": True,
        "backup_schema_version": verification["schema_version"],
        "destination_exists": target_exists,
        "will_replace_existing_database": target_exists,
        "backup_table_rows": source_counts, "destination_table_rows": target_counts,
    }


def restore_database(backup: Path, destination: Path) -> None:
    """Restore a verified SQLite backup to a database path."""
    preview_restore(backup, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.restore-tmp{destination.suffix}")
    try:
        temporary.unlink(missing_ok=True)
        source_connection = _readonly_connection(backup)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        migrated_connection = _connect(temporary)
        migrated_connection.close()
        Path(str(destination) + "-wal").unlink(missing_ok=True)
        Path(str(destination) + "-shm").unlink(missing_ok=True)
        temporary.replace(destination)
    except (OSError, sqlite3.Error) as exc:
        raise InputError(f"Cannot restore SQLite database to {destination}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + "-wal").unlink(missing_ok=True)
        Path(str(temporary) + "-shm").unlink(missing_ok=True)
