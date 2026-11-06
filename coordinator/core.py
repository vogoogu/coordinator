"""Task operations. Every write runs in one transaction with its event row."""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from . import config, messages, paths
from .messages import CoordinatorError

CHAT_CREATE_LIMIT = 3


@dataclass(frozen=True)
class Actor:
    type: Literal["person", "chat"]
    id: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the body raises."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _event(conn, task_id, actor: Actor, action, from_value=None, to_value=None,
           note=None, at=None):
    """Write one event row. Call inside a transaction."""
    conn.execute(
        "INSERT INTO event (task_id, at, actor_type, actor_id, action,"
        " from_value, to_value, note) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, at or _now(), actor.type, actor.id, action,
         from_value, to_value, note))


def _project_dict(row) -> dict:
    return dict(row)


def _task_dict(row) -> dict:
    d = dict(row)
    d["pk"] = d.pop("id")
    d["project"] = d.pop("project_key")
    d["id"] = f"{d['project']}-{d['number']}"
    d["open_points"] = json.loads(d["open_points"] or "[]")
    d["inbox"] = bool(d["inbox"])
    return d


_TASK_SELECT = ("SELECT t.*, p.key AS project_key FROM task t"
                " JOIN project p ON p.id = t.project_id")


def _project_row(conn, key):
    row = conn.execute("SELECT * FROM project WHERE key = ? COLLATE NOCASE",
                       (key,)).fetchone()
    if row is None:
        raise messages.unknown_project(key)
    return row


def add_project(conn, root: str, actor: Actor) -> dict:
    nroot = paths.norm(root)
    try:
        cfg = config.load_project(nroot)
    except config.ConfigError as e:
        raise messages.project_config_bad(nroot, e) from None
    with transaction(conn):
        dup = conn.execute(
            "SELECT 1 FROM project WHERE key = ? COLLATE NOCASE OR root = ?",
            (cfg.key, nroot)).fetchone()
        if dup:
            raise messages.project_exists(cfg.key, nroot)
        cur = conn.execute(
            "INSERT INTO project (key, name, root, created) VALUES (?,?,?,?)",
            (cfg.key, cfg.name, nroot, _now()))
        _event(conn, None, actor, "project_added", None, cfg.key, nroot)
        row = conn.execute("SELECT * FROM project WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return _project_dict(row)


def project_for_path(conn, path: str) -> dict | None:
    """Project whose root contains `path`; the deepest root wins."""
    best = None
    for row in conn.execute("SELECT * FROM project"):
        if paths.same_or_inside(path, row["root"]):
            if best is None or len(row["root"]) > len(best["root"]):
                best = row
    return _project_dict(best) if best else None


def create_task(conn, project_key: str, title: str, actor: Actor, goal="",
                done_when="", scope="", origin="",
                inbox: bool | None = None) -> dict:
    proj = _project_row(conn, project_key)
    cfg = config.load_project(proj["root"])
    first = next((s for s in cfg.states if s.kind == "waiting"), None)
    if first is None:
        raise messages.project_config_bad(proj["root"], "no waiting state defined")
    if inbox is None:
        inbox = actor.type == "chat"
    refused = None
    with transaction(conn):
        if actor.type == "chat":
            row = conn.execute(
                "SELECT tasks_created FROM session_stat WHERE session_id = ?",
                (actor.id,)).fetchone()
            if row and row["tasks_created"] >= CHAT_CREATE_LIMIT:
                refused = messages.create_limit(CHAT_CREATE_LIMIT)
                _event(conn, None, actor, "refused", None, None,
                       f"create task in {proj['key']}: {title}")
        if refused is None:
            now = _now()
            number = conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM task WHERE project_id = ?",
                (proj["id"],)).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO task (project_id, number, title, goal, done_when, scope,"
                " mode, state, open_points, inbox, origin, created_by_type,"
                " created_by_id, created, updated, version)"
                " VALUES (?,?,?,?,?,?,?,?,'[]',?,?,?,?,?,?,1)",
                (proj["id"], number, title, goal, done_when, scope,
                 cfg.default_mode, first.name, 1 if inbox else 0, origin,
                 actor.type, actor.id, now, now))
            task_id = cur.lastrowid
            _event(conn, task_id, actor, "created", None, first.name, title, at=now)
            if actor.type == "chat":
                conn.execute(
                    "INSERT INTO session_stat (session_id, tasks_created, last_activity)"
                    " VALUES (?, 1, ?) ON CONFLICT(session_id) DO UPDATE SET"
                    " tasks_created = tasks_created + 1, last_activity = excluded.last_activity",
                    (actor.id, now))
    if refused is not None:
        raise refused
    return _task_by_pk(conn, task_id)


def _task_by_pk(conn, pk) -> dict:
    return _task_dict(conn.execute(_TASK_SELECT + " WHERE t.id = ?", (pk,)).fetchone())


def get_task(conn, display_id: str) -> dict:
    key, sep, num = str(display_id).strip().rpartition("-")
    if not sep or not key or not num.isdigit():
        raise messages.unknown_task(display_id)
    row = conn.execute(
        _TASK_SELECT + " WHERE p.key = ? COLLATE NOCASE AND t.number = ?",
        (key, int(num))).fetchone()
    if row is None:
        raise messages.unknown_task(display_id)
    d = _task_dict(row)
    pk = d["pk"]
    d["history"] = [dict(r) for r in conn.execute(
        "SELECT * FROM event WHERE task_id = ? ORDER BY id", (pk,))]
    d["comments"] = [dict(r) for r in conn.execute(
        "SELECT * FROM comment WHERE task_id = ? ORDER BY id", (pk,))]
    d["claims"] = [dict(r) for r in conn.execute(
        "SELECT * FROM claim WHERE task_id = ? ORDER BY id", (pk,))]
    return d


def list_tasks(conn, project_key: str | None = None, states: list[str] | None = None,
               inbox: bool | None = None) -> list[dict]:
    sql, args, where = _TASK_SELECT, [], []
    if project_key is not None:
        where.append("p.key = ? COLLATE NOCASE")
        args.append(project_key)
    if states is not None:
        if not states:
            return []
        where.append("t.state IN (%s)" % ",".join("?" * len(states)))
        args.extend(states)
    if inbox is not None:
        where.append("t.inbox = ?")
        args.append(1 if inbox else 0)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.key, t.number"
    return [_task_dict(r) for r in conn.execute(sql, args)]
