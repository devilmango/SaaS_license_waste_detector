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
    else:
        raise InputError("ledger kind must be actions or access-review")
    if read_events(database, table):
        raise InputError(f"SQLite {kind} ledger already contains events")
    for event in events:
        append(database, event)
    return len(events)
