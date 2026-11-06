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


class _Refusal(Exception):
    """Internal: carries a CoordinatorError out of a transaction that must commit."""
    def __init__(self, error):
        self.error = error


@contextmanager
def _write(conn: sqlite3.Connection):
    """Like transaction(), but a refusal (see _refuse) commits its event, then raises."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except _Refusal as r:
        conn.execute("COMMIT")
        raise r.error from None
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _refuse(conn, task_id, actor: "Actor", error, attempted: str):
    """Record a `refused` event (committed by _write) and abort the operation."""
    _event(conn, task_id, actor, "refused", None, None, attempted)
    raise _Refusal(error)


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
    with _write(conn):
        if actor.type == "chat":
            row = conn.execute(
                "SELECT tasks_created FROM session_stat WHERE session_id = ?",
                (actor.id,)).fetchone()
            if row and row["tasks_created"] >= CHAT_CREATE_LIMIT:
                _refuse(conn, None, actor, messages.create_limit(CHAT_CREATE_LIMIT),
                        f"create task in {proj['key']}: {title}")
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


# ---------------------------------------------------------------- changes

COMMENT_KINDS = ("comment", "question", "answer", "handoff", "progress")
CHAT_COMMENT_KINDS = ("comment", "question", "answer")
EDITABLE_FIELDS = ("title", "goal", "done_when", "scope", "origin")


def _load(conn, display_id):
    """Task dict and its project config, or unknown_task."""
    key, sep, num = str(display_id).strip().rpartition("-")
    row = None
    if sep and key and num.isdigit():
        row = conn.execute(
            _TASK_SELECT + " WHERE p.key = ? COLLATE NOCASE AND t.number = ?",
            (key, int(num))).fetchone()
    if row is None:
        raise messages.unknown_task(display_id)
    root = conn.execute("SELECT root FROM project WHERE id = ?",
                        (row["project_id"],)).fetchone()["root"]
    return _task_dict(row), config.load_project(root)


def _live_claims(conn, task_pk) -> list:
    return [r["session_id"] for r in conn.execute(
        "SELECT session_id FROM claim WHERE task_id = ? AND ended IS NULL"
        " ORDER BY id", (task_pk,))]


def _require_edit_rights(conn, task, actor: Actor, attempted: str):
    """R3: a person may edit; a chat needs its own live claim, or must have
    created the task while nobody holds a live claim."""
    if actor.type == "person":
        return
    holders = _live_claims(conn, task["pk"])
    if actor.id in holders:
        return
    if holders:
        _refuse(conn, task["pk"], actor,
                messages.claimed_by_other(task["id"], holders[0]), attempted)
    if task["created_by_type"] == "chat" and task["created_by_id"] == actor.id:
        return
    _refuse(conn, task["pk"], actor, messages.no_edit_rights(task["id"]), attempted)


def _bump(conn, task_pk, now, **cols):
    sets = "".join(f", {c} = ?" for c in cols)
    conn.execute(
        f"UPDATE task SET updated = ?, version = version + 1{sets} WHERE id = ?",
        (now, *cols.values(), task_pk))


def set_state(conn, display_id, new_state, actor: Actor,
              expected_state: str | None = None, note: str = "") -> dict:
    with _write(conn):
        task, cfg = _load(conn, display_id)
        pk = task["pk"]
        attempted = f"set state {task['state']} -> {new_state}"
        _require_edit_rights(conn, task, actor, attempted)
        if expected_state is not None and expected_state != task["state"]:
            _refuse(conn, pk, actor,
                    messages.stale_state(task["id"], expected_state, task["state"]),
                    attempted)
        target = next((s for s in cfg.states if s.name == new_state), None)
        if target is None:
            _refuse(conn, pk, actor,
                    messages.unknown_state(new_state, [s.name for s in cfg.states]),
                    attempted)
        if actor.type == "chat":
            if target.kind in ("finished", "closed"):
                _refuse(conn, pk, actor,
                        messages.chat_final_state(target.name, target.kind), attempted)
            if target.kind == "active" and actor.id not in _live_claims(conn, pk):
                _refuse(conn, pk, actor, messages.chat_needs_claim(target.name),
                        attempted)
        for req in target.requires:
            value = note if req == "note" else task.get(req)
            if not str(value or "").strip():
                _refuse(conn, pk, actor, messages.requires_missing(target.name, req),
                        attempted)
        if target.name != task["state"]:
            now = _now()
            _bump(conn, pk, now, state=target.name)
            _event(conn, pk, actor, "state", task["state"], target.name,
                   note.strip() or None, at=now)
    return _task_by_pk(conn, pk)


def set_fields(conn, display_id, actor: Actor, **fields) -> dict:
    with _write(conn):
        task, _cfg = _load(conn, display_id)
        pk = task["pk"]
        attempted = "set fields " + ", ".join(sorted(fields))
        _require_edit_rights(conn, task, actor, attempted)
        for name, value in fields.items():
            if name not in EDITABLE_FIELDS:
                _refuse(conn, pk, actor,
                        messages.unknown_field(name, EDITABLE_FIELDS), attempted)
            if name == "title" and not str(value or "").strip():
                _refuse(conn, pk, actor, messages.empty_value("the title"), attempted)
        changes = {n: str(v) for n, v in fields.items() if str(v) != task[n]}
        if changes:
            now = _now()
            _bump(conn, pk, now, **changes)
            for name, value in changes.items():
                _event(conn, pk, actor, "fields", task[name], value, name, at=now)
    return _task_by_pk(conn, pk)


def set_mode(conn, display_id, mode, actor: Actor) -> dict:
    with _write(conn):
        task, cfg = _load(conn, display_id)
        pk = task["pk"]
        attempted = f"set mode {task['mode']} -> {mode}"
        _require_edit_rights(conn, task, actor, attempted)
        if mode not in cfg.modes:
            _refuse(conn, pk, actor, messages.unknown_mode(mode, cfg.modes), attempted)
        cur = cfg.modes.index(task["mode"]) if task["mode"] in cfg.modes else 0
        if actor.type == "chat" and cfg.modes.index(mode) > cur:
            _refuse(conn, pk, actor, messages.chat_raise_mode(task["mode"], mode),
                    attempted)
        if mode != task["mode"]:
            now = _now()
            _bump(conn, pk, now, mode=mode)
            _event(conn, pk, actor, "mode", task["mode"], mode, at=now)
    return _task_by_pk(conn, pk)


def comment(conn, display_id, actor: Actor, kind, text) -> dict:
    """Add a comment (no claim needed). Returns the comment row."""
    with _write(conn):
        task, _cfg = _load(conn, display_id)
        pk = task["pk"]
        attempted = f"comment ({kind})"
        if kind not in COMMENT_KINDS:
            _refuse(conn, pk, actor,
                    messages.unknown_comment_kind(kind, COMMENT_KINDS), attempted)
        if actor.type == "chat" and kind not in CHAT_COMMENT_KINDS:
            _refuse(conn, pk, actor,
                    messages.chat_comment_kind(kind, CHAT_COMMENT_KINDS), attempted)
        if not str(text or "").strip():
            _refuse(conn, pk, actor, messages.empty_value("the comment text"),
                    attempted)
        now = _now()
        cur = conn.execute(
            "INSERT INTO comment (task_id, at, actor_type, actor_id, kind, text)"
            " VALUES (?,?,?,?,?,?)", (pk, now, actor.type, actor.id, kind, text))
        _bump(conn, pk, now)
        _event(conn, pk, actor, "comment", None, kind, text, at=now)
        row = conn.execute("SELECT * FROM comment WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return dict(row)


def set_blocked(conn, display_id, actor: Actor, reason: str | None) -> dict:
    reason = (reason or "").strip() or None
    with _write(conn):
        task, _cfg = _load(conn, display_id)
        pk = task["pk"]
        attempted = "set blocked" if reason else "clear blocked"
        _require_edit_rights(conn, task, actor, attempted)
        if reason != task["blocked"]:
            now = _now()
            _bump(conn, pk, now, blocked=reason)
            _event(conn, pk, actor, "blocked", task["blocked"], reason, at=now)
    return _task_by_pk(conn, pk)


def set_open_points(conn, display_id, actor: Actor, points: list) -> dict:
    with _write(conn):
        task, _cfg = _load(conn, display_id)
        pk = task["pk"]
        attempted = "set open points"
        _require_edit_rights(conn, task, actor, attempted)
        if (not isinstance(points, (list, tuple))
                or not all(isinstance(p, str) for p in points)):
            _refuse(conn, pk, actor, messages.bad_open_points(repr(points)), attempted)
        new = json.dumps(list(points), ensure_ascii=False)
        old = json.dumps(task["open_points"], ensure_ascii=False)
        if new != old:
            now = _now()
            _bump(conn, pk, now, open_points=new)
            _event(conn, pk, actor, "open_points", old, new, at=now)
    return _task_by_pk(conn, pk)


def pin(conn, display_id, actor: Actor) -> dict:
    """Person only: take the task out of the inbox."""
    with _write(conn):
        task, _cfg = _load(conn, display_id)
        pk = task["pk"]
        if actor.type != "person":
            _refuse(conn, pk, actor, messages.person_only("Pinning"), "pin")
        now = _now()
        _bump(conn, pk, now, inbox=0)
        _event(conn, pk, actor, "pin", "1" if task["inbox"] else "0", "0", at=now)
    return _task_by_pk(conn, pk)
