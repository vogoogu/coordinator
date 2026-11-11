"""SQLite connection and schema. Rows are never deleted by the application."""
import re
import sqlite3
from datetime import date
from pathlib import Path

from . import paths

SCHEMA_VERSION = 4

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
    end_reason TEXT,
    launch TEXT
);
CREATE INDEX IF NOT EXISTS claim_task ON claim(task_id);
CREATE TABLE IF NOT EXISTS session_stat (
    session_id TEXT PRIMARY KEY,
    tasks_created INTEGER NOT NULL DEFAULT 0,
    last_task_update TEXT,
    last_activity TEXT,
    last_tool TEXT,
    last_path TEXT
);
CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS hook_warning (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    text TEXT NOT NULL,
    created TEXT NOT NULL,
    shown TEXT
);
CREATE INDEX IF NOT EXISTS hook_warning_session ON hook_warning(session_id);
CREATE TABLE IF NOT EXISTS start_note (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES task(id),
    root TEXT NOT NULL,
    created TEXT NOT NULL,
    taken TEXT
);
"""

# Columns added after a table was first created: CREATE TABLE IF NOT EXISTS does
# not add them to an existing database, so connect() adds any that are missing.
ADDED_COLUMNS = (
    ("session_stat", "last_tool", "TEXT"),   # version 2
    ("session_stat", "last_path", "TEXT"),   # version 2
    ("claim", "launch", "TEXT"),             # version 3: launch token (R32)
    ("task", "word", "TEXT NOT NULL DEFAULT ''"),     # version 4: chat-name word
    ("task", "forms", "TEXT NOT NULL DEFAULT '[]'"),  # version 4: what the task touches
    ("comment", "role", "TEXT NOT NULL DEFAULT ''"),  # version 4: helper role of a report
    ("claim", "waiting_click", "TEXT"),               # version 4: waits for a permission click
    ("session_stat", "helpers", "INTEGER NOT NULL DEFAULT 0"),  # version 4: running helpers
    ("session_stat", "helpers_at", "TEXT"),           # version 4: when a helper last started
)


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
            for table, column, decl in ADDED_COLUMNS:
                have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return conn


_BACKUP_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.db$")


def backup(conn: sqlite3.Connection, dest_dir: Path, keep: int = 14) -> Path:
    """Copy the database to dest_dir/YYYY-MM-DD.db (local date, today's file is
    overwritten) and keep only the newest `keep` dated files. Other files stay."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{date.today().isoformat()}.db"
    tmp = dest_dir / (target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(str(tmp))
    try:
        conn.backup(out)
    finally:
        out.close()
    tmp.replace(target)
    dated = sorted(p.name for p in dest_dir.iterdir()
                   if p.is_file() and _BACKUP_NAME.match(p.name))
    for name in dated[:-keep] if keep > 0 else dated:
        (dest_dir / name).unlink()
    return target
