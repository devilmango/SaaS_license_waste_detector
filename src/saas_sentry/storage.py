"""Small SQLite-backed append-only event store for shared operator workflows."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterable

from .engine import InputError


def is_sqlite_path(path: Path) -> bool:
    return path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations (version) VALUES (1)"
        )
        return connection
    except sqlite3.Error as exc:
        raise InputError(f"Cannot open SQLite ledger {path}: {exc}") from exc


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


def restore_database(backup: Path, destination: Path) -> None:
    """Restore a consistent SQLite backup to a database path."""
    if not is_sqlite_path(backup) or not is_sqlite_path(destination):
        raise InputError("SQLite restore source and destination must use .db, .sqlite, or .sqlite3")
    if backup.resolve() == destination.resolve():
        raise InputError("SQLite restore source and destination must differ")
    if not backup.exists():
        raise InputError(f"SQLite backup does not exist: {backup}")
    check = sqlite3.connect(backup)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise InputError(f"SQLite backup failed integrity check: {backup}")
    except sqlite3.Error as exc:
        raise InputError(f"Cannot validate SQLite backup {backup}: {exc}") from exc
    finally:
        check.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.restore-tmp{destination.suffix}")
    try:
        temporary.unlink(missing_ok=True)
        backup_database(backup, temporary)
        temporary.replace(destination)
    except OSError as exc:
        raise InputError(f"Cannot restore SQLite database to {destination}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
