"""SQLite connection and schema. Rows are never deleted by the application."""
import sqlite3
from pathlib import Path

from . import paths

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    root TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id),
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    done_when TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    blocked TEXT,
    open_points TEXT NOT NULL DEFAULT '[]',
    inbox INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT '',
    created_by_type TEXT NOT NULL,
    created_by_id TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (project_id, number)
);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES task(id),
    at TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_value TEXT,
    to_value TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS event_task ON event(task_id);
CREATE TABLE IF NOT EXISTS comment (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES task(id),
    at TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comment_task ON comment(task_id);
CREATE TABLE IF NOT EXISTS claim (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES task(id),
    session_id TEXT NOT NULL,
    transcript_path TEXT,
    cwd TEXT,
    started TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    ended TEXT,
    end_reason TEXT
);
CREATE INDEX IF NOT EXISTS claim_task ON claim(task_id);
CREATE TABLE IF NOT EXISTS session_stat (
    session_id TEXT PRIMARY KEY,
    tasks_created INTEGER NOT NULL DEFAULT 0,
    last_task_update TEXT,
    last_activity TEXT
);
CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (and if needed create) the database.

    Autocommit mode: core opens transactions explicitly (BEGIN IMMEDIATE)."""
    if path is None:
        path = paths.app_home() / "coordinator.db"
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return conn
