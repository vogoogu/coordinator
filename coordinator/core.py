"""Task operations. Every write runs in one transaction with its event row."""
import json
import random
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from . import config, messages, paths
from .messages import ClaimError, CoordinatorError  # noqa: F401

CHAT_CREATE_LIMIT = 3


@dataclass(frozen=True)
class Actor:
    type: Literal["person", "chat", "system"]
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
    d["forms"] = json.loads(d.get("forms") or "[]")
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


def _config(root: str, what: str):
    """The project config; a broken project file becomes a CoordinatorError."""
    try:
        return config.load_project(root)
    except config.ConfigError as e:
        raise messages.project_config_bad(root, e, what) from None


def create_task(conn, project_key: str, title: str, actor: Actor, goal="",
                done_when="", scope="", origin="",
                inbox: bool | None = None) -> dict:
    proj = _project_row(conn, project_key)
    cfg = _config(proj["root"], "The task was not created.")
    first = next((s for s in cfg.states if s.kind == "waiting"), None)
    if first is None:
        raise messages.project_config_bad(proj["root"], "no waiting state defined",
                                          "The task was not created.")
    if inbox is None:
        inbox = False  # chat tasks go straight to the first waiting state (Daria, 2026-11-11)
    if not str(title or "").strip():
        raise messages.empty_value("the title")
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
            " created_by_id, created, updated, version, word)"
            " VALUES (?,?,?,?,?,?,?,?,'[]',?,?,?,?,?,?,1,?)",
            (proj["id"], number, title, goal, done_when, scope,
             cfg.default_mode, first.name, 1 if inbox else 0, origin,
             actor.type, actor.id, now, now, random.choice(NAME_WORDS)))
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
ROLE_RE = re.compile(r"spec|(build|test) [1-9][0-9]?")   # who wrote a report (task chain)
NAME_WORDS = ("otter", "heron", "lynx", "badger", "falcon", "marten", "ibex", "wren",
              "stoat", "raven", "fox", "crane", "newt", "hare", "finch", "vole")
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
    task = _task_dict(row)
    return task, _config(root, f"{task['id']} was not changed.")


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
        now = _now()
        if target.name != task["state"]:
            _bump(conn, pk, now, state=target.name)
            _event(conn, pk, actor, "state", task["state"], target.name,
                   note.strip() or None, at=now)
        if actor.type == "person" and target.kind in ("finished", "closed"):
            # R31: a task the person finished or closed keeps no live claim
            for c in _live_claim_rows(conn, task_pk=pk):
                _end_claim(conn, c["id"], now, CLOSED_BY_PERSON)
                _event(conn, pk, actor, "release", c["session_id"],
                       CLOSED_BY_PERSON, at=now)
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


def comment(conn, display_id, actor: Actor, kind, text, role: str = "") -> dict:
    """Add a comment (no claim needed). Returns the comment row. `role` marks a
    helper's report ("build 1", "test 2", "spec"); it is kept with the comment."""
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
        role = (role or "").strip().lower()
        if role and not ROLE_RE.fullmatch(role):
            _refuse(conn, pk, actor, messages.bad_role(role), attempted)
        now = _now()
        cur = conn.execute(
            "INSERT INTO comment (task_id, at, actor_type, actor_id, kind, text, role)"
            " VALUES (?,?,?,?,?,?,?)", (pk, now, actor.type, actor.id, kind, text, role))
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


# ---------------------------------------------------------------- claims
# Functions taking `now` accept a timezone-aware datetime (default: the current time).

LEASE_HOURS = 0.5   # a claim with no activity this long is ended and the task goes to clarify (COORD-8)
HELPER_MAX_HOURS = 2.0   # a helper counts as running at most this long after it started
START_NOTE_MINUTES = 60  # a "waiting task" note is picked up by the first chat within this time
OUTCOMES = ("done", "clarify", "progress")
_OUTCOME_COMMENT = {"done": "handoff", "clarify": "question", "progress": "progress"}
COORDINATOR_TOOL_PREFIX = "mcp__coordinator__"
SYSTEM_LEASE = Actor("system", "lease")
CLOSED_BY_PERSON = "closed_by_person"


def _stamp(now: datetime | None = None) -> str:
    """ISO string in the same format as _now(), for a datetime or the current time."""
    if now is None:
        return _now()
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def _set_session(conn, session_id, **cols):
    names = ", ".join(cols)
    marks = ", ".join("?" * len(cols))
    sets = ", ".join(f"{c} = excluded.{c}" for c in cols)
    conn.execute(
        f"INSERT INTO session_stat (session_id, {names}) VALUES (?, {marks})"
        f" ON CONFLICT(session_id) DO UPDATE SET {sets}",
        (session_id, *cols.values()))


def _live_claim_rows(conn, task_pk=None, session_id=None) -> list:
    sql = ("SELECT c.*, p.key || '-' || t.number AS display_id,"
           " COALESCE(s.helpers, 0) AS helpers, s.helpers_at AS helpers_at FROM claim c"
           " JOIN task t ON t.id = c.task_id JOIN project p ON p.id = t.project_id"
           " LEFT JOIN session_stat s ON s.session_id = c.session_id"
           " WHERE c.ended IS NULL")
    args = []
    if task_pk is not None:
        sql += " AND c.task_id = ?"
        args.append(task_pk)
    if session_id is not None:
        sql += " AND c.session_id = ?"
        args.append(session_id)
    return conn.execute(sql + " ORDER BY c.id", args).fetchall()


def _is_active(cfg, state_name) -> bool:
    return any(s.name == state_name and s.kind == "active" for s in cfg.states)


def _move(conn, task, state_name, actor, now, note=None):
    """Move to a configured state without the rights/requires checks of set_state."""
    if state_name != task["state"]:
        _bump(conn, task["pk"], now, state=state_name)
        _event(conn, task["pk"], actor, "state", task["state"], state_name, note,
               at=now)


def _add_comment(conn, task_pk, actor, kind, text, now):
    conn.execute(
        "INSERT INTO comment (task_id, at, actor_type, actor_id, kind, text)"
        " VALUES (?,?,?,?,?,?)", (task_pk, now, actor.type, actor.id, kind, text))
    _bump(conn, task_pk, now)
    _event(conn, task_pk, actor, "comment", None, kind, text, at=now)


def _end_claim(conn, claim_id, now, reason):
    conn.execute("UPDATE claim SET ended = ?, end_reason = ? WHERE id = ?",
                 (now, reason, claim_id))


def claim(conn, display_id, session_id, transcript_path="", cwd="",
          now: datetime | None = None, launch: str | None = None,
          waiting_only: bool = False) -> dict | None:
    """Claim a task for a chat session and move it to the first active state
    (unless the project's `claim` setting is "keep": the state stays as it is).

    `launch` is the board's launch token (COORD_LAUNCH), stored on the claim.
    With `waiting_only` (the hook's COORD_TASK path, R30) a task that is not in a
    waiting-kind state is left alone and None is returned, with nothing written."""
    actor = Actor("chat", session_id)
    with _write(conn):
        task, cfg = _load(conn, display_id)
        pk = task["pk"]
        attempted = f"claim {task['id']}"
        holders = _live_claim_rows(conn, task_pk=pk)
        if any(h["session_id"] == session_id for h in holders):
            return _task_by_pk(conn, pk)  # already mine: nothing to do
        kinds = {s.name: s.kind for s in cfg.states}
        if waiting_only and kinds.get(task["state"]) != "waiting":
            return None
        if holders:
            _refuse(conn, pk, actor,
                    messages.already_claimed(task["id"], holders[0]["session_id"]),
                    attempted)
        if kinds.get(task["state"]) in ("finished", "closed"):
            _refuse(conn, pk, actor, messages.claim_final(task["id"], task["state"]),
                    attempted)
        other = _live_claim_rows(conn, session_id=session_id)
        if other:
            _refuse(conn, pk, actor,
                    messages.session_has_claim(task["id"], other[0]["display_id"]),
                    attempted)
        first = next((s for s in cfg.states if s.kind == "active"), None)
        if first is None:
            raise messages.project_config_bad(
                _project_row(conn, task["project"])["root"], "no active state defined",
                f"{task['id']} was not claimed.")
        stamp = _stamp(now)
        conn.execute(
            "INSERT INTO claim (task_id, session_id, transcript_path, cwd, started,"
            " last_seen, launch) VALUES (?,?,?,?,?,?,?)",
            (pk, session_id, transcript_path, cwd, stamp, stamp, launch or None))
        _bump(conn, pk, stamp, inbox=0)
        _event(conn, pk, actor, "claim", None, session_id, at=stamp)
        if cfg.claim != "keep":  # "keep": the chat moves the task itself
            _move(conn, task, first.name, actor, stamp)
        _set_session(conn, session_id, last_task_update=stamp)
    return _task_by_pk(conn, pk)


def reclaim_after_clear(conn, display_id, session_id, launch, transcript_path="",
                        cwd="") -> dict | None:
    """/clear gave the chat a new session id (R32). If the live claim on the task
    holds the same launch token under another session id, end it (end_reason
    "clear") and give the task to `session_id`; the state is kept. Otherwise
    write nothing and return None."""
    if not launch:
        return None
    actor = Actor("chat", session_id)
    with _write(conn):
        task, _cfg = _load(conn, display_id)
        pk = task["pk"]
        holders = _live_claim_rows(conn, task_pk=pk)
        old = [h for h in holders
               if h["launch"] == launch and h["session_id"] != session_id]
        if not old or len(old) != len(holders):
            return None
        if _live_claim_rows(conn, session_id=session_id):
            return None  # this session already works on a task
        stamp = _now()
        for h in old:
            _end_claim(conn, h["id"], stamp, "clear")
            _event(conn, pk, actor, "release", h["session_id"], "clear", at=stamp)
        conn.execute(
            "INSERT INTO claim (task_id, session_id, transcript_path, cwd, started,"
            " last_seen, launch) VALUES (?,?,?,?,?,?,?)",
            (pk, session_id, transcript_path, cwd, stamp, stamp, launch))
        _bump(conn, pk, stamp, inbox=0)
        _event(conn, pk, actor, "claim", None, session_id, at=stamp)
        _set_session(conn, session_id, last_task_update=stamp)
    return _task_by_pk(conn, pk)


def reclaim_after_answer(conn, session_id, transcript_path="", cwd="") -> dict | None:
    """The person wrote to a chat that had asked a question (COORD-10). If the session's
    last claim ended with "clarify" or "expired", its task is still in the project's
    clarify state and nobody holds it, claim it again. Otherwise write nothing and
    return None."""
    if _live_claim_rows(conn, session_id=session_id):
        return None
    last = conn.execute(
        "SELECT c.end_reason, p.key || '-' || t.number AS display_id FROM claim c"
        " JOIN task t ON t.id = c.task_id JOIN project p ON p.id = t.project_id"
        " WHERE c.session_id = ? ORDER BY c.id DESC LIMIT 1", (session_id,)).fetchone()
    if last is None or last["end_reason"] not in ("clarify", "expired"):
        return None
    try:
        task, cfg = _load(conn, last["display_id"])
    except CoordinatorError:
        return None
    if task["state"] != cfg.on_clarify or _live_claim_rows(conn, task_pk=task["pk"]):
        return None
    return claim(conn, last["display_id"], session_id, transcript_path, cwd)


def release(conn, display_id, session_id, outcome, note) -> dict:
    """Report on a claimed task: done / clarify end the claim, progress keeps it.
    The state moves only from an active state. If a person finished or closed the
    task meanwhile (claim ended "closed_by_person"), the note is still kept."""
    actor = Actor("chat", session_id)
    with _write(conn):
        task, cfg = _load(conn, display_id)
        pk = task["pk"]
        attempted = f"release ({outcome})"
        mine = [c for c in _live_claim_rows(conn, task_pk=pk)
                if c["session_id"] == session_id]
        closed = not mine and _closed_by_person(conn, pk, session_id)
        if not mine and not closed:
            _refuse(conn, pk, actor, messages.not_your_claim(task["id"]), attempted)
        if outcome not in OUTCOMES:
            _refuse(conn, pk, actor, messages.unknown_outcome(outcome, OUTCOMES),
                    attempted)
        if not str(note or "").strip():
            _refuse(conn, pk, actor, messages.empty_value("the note"), attempted)
        stamp = _now()
        _add_comment(conn, pk, actor, _OUTCOME_COMMENT[outcome], note, stamp)
        if closed:
            pass  # R31: the person already ended the claim; keep the note only
        elif outcome == "progress":
            conn.execute("UPDATE claim SET last_seen = ? WHERE id = ?",
                         (stamp, mine[0]["id"]))
        else:
            _end_claim(conn, mine[0]["id"], stamp, outcome)
            _event(conn, pk, actor, "release", session_id, outcome, at=stamp)
            if _is_active(cfg, task["state"]):  # R31: never undo a person's move
                _move(conn, task, cfg.on_done if outcome == "done" else cfg.on_clarify,
                      actor, stamp)
        _set_session(conn, session_id, last_task_update=stamp)
    return _task_by_pk(conn, pk)


def _closed_by_person(conn, task_pk, session_id) -> bool:
    """True if this session's latest claim on the task was ended by a person
    finishing or closing the task (its report is still kept as a comment)."""
    row = conn.execute(
        "SELECT end_reason FROM claim WHERE task_id = ? AND session_id = ?"
        " ORDER BY id DESC LIMIT 1", (task_pk, session_id)).fetchone()
    return bool(row) and row["end_reason"] == CLOSED_BY_PERSON


def touch(conn, session_id, tool="", path="", now: datetime | None = None) -> None:
    """Record work by a session that holds a live claim; otherwise write nothing."""
    if str(tool).startswith(COORDINATOR_TOOL_PREFIX):
        return
    if not _live_claim_rows(conn, session_id=session_id):
        return
    stamp = _stamp(now)
    with transaction(conn):
        conn.execute("UPDATE claim SET last_seen = ?, waiting_click = NULL"
                     " WHERE session_id = ? AND ended IS NULL", (stamp, session_id))
        _set_session(conn, session_id, last_activity=stamp, last_tool=tool,
                     last_path=path)


def _release_claims(conn, task, cfg, claims, actor, reason, text, stamp):
    """Shared tail of force/expire/no-report: end claims, comment, and move to
    clarify, but only from an active state (R31: a person's move stays)."""
    for c in claims:
        _end_claim(conn, c["id"], stamp, reason)
    _event(conn, task["pk"], actor, "release", claims[0]["session_id"], reason,
           at=stamp)
    _add_comment(conn, task["pk"], actor, "comment", text, stamp)
    if _is_active(cfg, task["state"]):
        _move(conn, task, cfg.on_clarify, actor, stamp)


def force_release(conn, display_id, actor: Actor) -> dict:
    """Person only: end whatever live claim the task has."""
    with _write(conn):
        task, cfg = _load(conn, display_id)
        pk = task["pk"]
        if actor.type != "person":
            _refuse(conn, pk, actor, messages.person_only("Force release"),
                    "force release")
        claims = _live_claim_rows(conn, task_pk=pk)
        if not claims:
            _refuse(conn, pk, actor, messages.no_live_claim(task["id"]),
                    "force release")
        _release_claims(conn, task, cfg, claims, actor, "force",
                        "released by person", _now())
    return _task_by_pk(conn, pk)


def expire_leases(conn, now: datetime | None = None, hours: float = LEASE_HOURS) -> list:
    """End claims not seen for `hours`; the task goes to the clarify state."""
    stamp = _stamp(now)
    cutoff = datetime.fromisoformat(stamp) - timedelta(hours=hours)
    expired = []
    with _write(conn):
        for c in _live_claim_rows(conn):
            if datetime.fromisoformat(c["last_seen"]) >= cutoff:
                continue
            if helpers_running(c, datetime.fromisoformat(stamp)):
                continue  # a background helper works for this chat (task chain)
            try:
                task, cfg = _load(conn, c["display_id"])
            except CoordinatorError:
                continue  # broken project file: leave this claim, expire the rest
            s = conn.execute("SELECT * FROM session_stat WHERE session_id = ?",
                             (c["session_id"],)).fetchone()
            last = "none"
            if s and s["last_activity"]:
                what = " ".join(x for x in (s["last_tool"], s["last_path"]) if x)
                last = f"{what or 'none'} at {s['last_activity']}"
            _release_claims(
                conn, task, cfg, [c], SYSTEM_LEASE, "expired",
                "chat stopped without hand-off; last activity: " + last, stamp)
            expired.append(task["id"])
    return expired


def helpers_running(claim, now: datetime) -> bool:
    """True while background helpers of the claim's chat run: started, not stopped, and
    started less than HELPER_MAX_HOURS ago (a helper that never reported its end must not
    keep a dead chat alive for ever)."""
    if not claim["helpers"] or not claim["helpers_at"]:
        return False
    return now - datetime.fromisoformat(claim["helpers_at"]) < timedelta(hours=HELPER_MAX_HOURS)


def helper_event(conn, session_id, started: bool, now: datetime | None = None) -> bool:
    """A background helper of this chat started or stopped (SubagentStart/Stop hooks).
    Counts them and counts the event as activity, so the lease does not run out while
    helpers work. Writes nothing for a session that never held a claim; returns whether it
    did. The count is kept even while the claim is gone (a helper's stop often arrives just
    after the chat lost the claim), otherwise the count drifts and stays at 1."""
    if not conn.execute("SELECT 1 FROM claim WHERE session_id = ? LIMIT 1",
                        (session_id,)).fetchone():
        return False
    stamp = _stamp(now)
    with transaction(conn):
        row = conn.execute("SELECT helpers FROM session_stat WHERE session_id = ?",
                           (session_id,)).fetchone()
        n = max((row["helpers"] if row else 0) + (1 if started else -1), 0)
        conn.execute("UPDATE claim SET last_seen = ? WHERE session_id = ? AND ended IS NULL",
                     (stamp, session_id))
        extra = {"helpers_at": stamp} if started else {}
        _set_session(conn, session_id, helpers=n, last_activity=stamp, **extra)
    return True


def mark_waiting_click(conn, session_id, message: str = "") -> dict | None:
    """The chat (or a helper of it) stopped on a permission question: mark the task.
    None, and nothing written, if the session holds no claim or is already marked."""
    rows = _live_claim_rows(conn, session_id=session_id)
    if not rows or rows[0]["waiting_click"]:
        return None
    actor = Actor("chat", session_id)
    with _write(conn):
        task, _cfg = _load(conn, rows[0]["display_id"])
        stamp = _now()
        conn.execute("UPDATE claim SET waiting_click = ? WHERE id = ?",
                     (stamp, rows[0]["id"]))
        _event(conn, task["pk"], actor, "click_wait", None, session_id,
               (message or "").strip()[:200] or None, at=stamp)
    return _task_by_pk(conn, task["pk"])


def stop_without_report(conn, session_id, note) -> dict | None:
    """Stop hook: the chat ended its turn with no report. None if it holds no claim."""
    if not _live_claim_rows(conn, session_id=session_id):
        return None
    actor = Actor("chat", session_id)
    with _write(conn):
        claims = _live_claim_rows(conn, session_id=session_id)
        if not claims:
            return None
        task, cfg = _load(conn, claims[0]["display_id"])
        stamp = _now()
        _release_claims(conn, task, cfg, claims, actor, "no_report", note, stamp)
        _set_session(conn, session_id, last_task_update=stamp)
    return _task_by_pk(conn, task["pk"])


def claim_for_session(conn, session_id) -> dict | None:
    """The session's live claim (with `display_id` of its task), or None."""
    rows = _live_claim_rows(conn, session_id=session_id)
    return dict(rows[0]) if rows else None


def needs_report(conn, session_id) -> bool:
    """True if the session holds a live claim and worked since its last task update."""
    if not _live_claim_rows(conn, session_id=session_id):
        return False
    s = conn.execute("SELECT last_activity, last_task_update FROM session_stat"
                     " WHERE session_id = ?", (session_id,)).fetchone()
    if not s or s["last_activity"] is None:
        return False
    return s["last_task_update"] is None or s["last_activity"] > s["last_task_update"]


# ---------------------------------------------------------------- hook support
def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return default if row is None or row["value"] is None else row["value"]


def get_project(conn, key: str) -> dict:
    """Project row by key (unknown_project if missing)."""
    return _project_dict(_project_row(conn, key))


def record_hook_warning(conn, session_id: str, rule: str, text: str) -> bool:
    """Remember a pre-tool finding for the next post-tool. Each rule is kept at
    most once per session; returns False if it was already recorded.
    Not a task change, so no event row."""
    with transaction(conn):
        if conn.execute("SELECT 1 FROM hook_warning WHERE session_id = ? AND rule = ?",
                        (session_id, rule)).fetchone():
            return False
        conn.execute("INSERT INTO hook_warning (session_id, rule, text, created)"
                     " VALUES (?,?,?,?)", (session_id, rule, text, _now()))
    return True


def take_hook_warnings(conn, session_id: str) -> list:
    """Texts of this session's pending warnings; they are marked shown."""
    with transaction(conn):
        rows = conn.execute("SELECT id, text FROM hook_warning WHERE session_id = ?"
                            " AND shown IS NULL ORDER BY id", (session_id,)).fetchall()
        now = _now()
        for r in rows:
            conn.execute("UPDATE hook_warning SET shown = ? WHERE id = ?", (now, r["id"]))
    return [r["text"] for r in rows]


# ---------------------------------------------------------------- board support
def list_projects(conn) -> list:
    return [_project_dict(r) for r in conn.execute("SELECT * FROM project ORDER BY key")]


def live_claims(conn) -> dict:
    """Live claims of all tasks, keyed by task display id."""
    return {r["display_id"]: dict(r) for r in _live_claim_rows(conn)}


def session_task(conn, session_id: str) -> dict | None:
    """The task a chat session claimed most recently: {"task", "title", "project"} or
    None if the coordinator does not know the session. Read only."""
    row = conn.execute(
        "SELECT p.key AS project, p.key || '-' || t.number AS task, t.title AS title"
        " FROM claim c JOIN task t ON t.id = c.task_id JOIN project p ON p.id = t.project_id"
        " WHERE c.session_id = ? ORDER BY c.id DESC LIMIT 1", (session_id,)).fetchone()
    return dict(row) if row else None


def get_session_stat(conn, session_id: str) -> dict | None:
    """What a chat session last did (tool, path, time); None if it never did anything."""
    row = conn.execute("SELECT * FROM session_stat WHERE session_id = ?",
                       (session_id,)).fetchone()
    return dict(row) if row else None


def board_token(conn) -> str:
    """The board's POST token; created on first use and kept in the settings."""
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)",
                     ("board_token", secrets.token_urlsafe(32)))
    return get_setting(conn, "board_token")


ENFORCEMENT_VALUES = ("warn", "block")


def set_enforcement(conn, value: str, actor: Actor) -> str:
    """Person only: set the hook enforcement (warn or block); recorded as an event."""
    if actor.type != "person":
        raise messages.person_only("Changing the enforcement")
    if value not in ENFORCEMENT_VALUES:
        raise messages.CoordinatorError(
            f"Enforcement {value!r} was not set.",
            "Only these values are known: " + ", ".join(ENFORCEMENT_VALUES) + ".",
            "Use warn or block.")
    with transaction(conn):
        old = get_setting(conn, "enforcement", "warn")
        conn.execute("INSERT INTO setting (key, value) VALUES ('enforcement', ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (value,))
        _event(conn, None, actor, "enforcement", old, value)
    return value


# ---------------------------------------------------------------- task chain
def task_name(task: dict, role: str = "spec") -> str:
    """The chat name: "KMU-7 spec \u00b7 otter" (id, role, the task's word); without
    a word yet just "KMU-7 spec"."""
    word = task.get("word")
    return f"{task['id']} {role} \u00b7 {word}" if word else f"{task['id']} {role}"


def ensure_word(conn, display_id, actor: Actor) -> dict:
    """Give a task that has no name word yet one (tasks made before the chain existed)."""
    with _write(conn):
        task, _cfg = _load(conn, display_id)
        if not task["word"]:
            now = _now()
            word = random.choice(NAME_WORDS)
            _bump(conn, task["pk"], now, word=word)
            _event(conn, task["pk"], actor, "named", None, word, at=now)
    return _task_by_pk(conn, task["pk"])


def set_forms(conn, display_id, actor: Actor, forms: list) -> dict:
    """The forms or interfaces the task touches (for the overlap check)."""
    with _write(conn):
        task, _cfg = _load(conn, display_id)
        pk = task["pk"]
        attempted = "set forms"
        _require_edit_rights(conn, task, actor, attempted)
        if (not isinstance(forms, (list, tuple))
                or not all(isinstance(f, str) for f in forms)):
            _refuse(conn, pk, actor, messages.bad_forms(repr(forms)), attempted)
        clean = []
        for f in forms:
            f = f.strip()
            if f and f not in clean:
                clean.append(f)
        new = json.dumps(clean, ensure_ascii=False)
        old = json.dumps(task["forms"], ensure_ascii=False)
        if new != old:
            now = _now()
            _bump(conn, pk, now, forms=new)
            _event(conn, pk, actor, "forms", old, new, at=now)
    return _task_by_pk(conn, pk)


def overlaps(conn, display_id: str, forms: list | None = None) -> list:
    """The other open tasks of this task's project, with the forms they touch.
    Each entry says which forms it shares with `forms` (default: the task's own).
    Open means not in a finished or closed state. Read only."""
    task, cfg = _load(conn, display_id)
    mine = set(task["forms"] if forms is None else forms)
    final = {s.name for s in cfg.states if s.kind in ("finished", "closed")}
    live = live_claims(conn)
    out = []
    for t in list_tasks(conn, task["project"]):
        if t["id"] == task["id"] or t["state"] in final:
            continue
        out.append({"id": t["id"], "title": t["title"], "state": t["state"],
                    "name": task_name(t), "forms": t["forms"],
                    "shared_forms": [f for f in t["forms"] if f in mine],
                    "claimed": t["id"] in live})
    out.sort(key=lambda o: (not o["shared_forms"], o["id"]))
    return out


def leave_start_note(conn, display_id, actor: Actor) -> dict:
    """The person pressed Start: the first new chat in the project folder picks the task
    up. An older untaken note for the same task is replaced."""
    with _write(conn):
        task, _cfg = _load(conn, display_id)
        pk = task["pk"]
        now = _now()
        conn.execute("UPDATE start_note SET taken = 'replaced' WHERE task_id = ?"
                     " AND taken IS NULL", (pk,))
        if not task["word"]:
            _bump(conn, pk, now, word=random.choice(NAME_WORDS))
        root = conn.execute("SELECT root FROM project WHERE id = ?",
                            (task["project_id"],)).fetchone()["root"]
        conn.execute("INSERT INTO start_note (task_id, root, created) VALUES (?,?,?)",
                     (pk, root, now))
        _event(conn, pk, actor, "start", None, None, "waiting for a chat in the folder",
               at=now)
    return _task_by_pk(conn, pk)


def take_start_note(conn, session_id, cwd, transcript_path="",
                    now: datetime | None = None) -> dict | None:
    """A new chat started in `cwd`: if a Start note waits for that project, claim its
    task (only if it is still waiting) and mark the note taken. Oldest note first.
    Returns the claimed task, or None."""
    proj = project_for_path(conn, cwd) if cwd else None
    if not proj:
        return None
    cutoff = (datetime.fromisoformat(_stamp(now))
              - timedelta(minutes=START_NOTE_MINUTES)).isoformat(timespec="seconds")
    for n in conn.execute(
            "SELECT n.id, p.key || '-' || t.number AS display_id FROM start_note n"
            " JOIN task t ON t.id = n.task_id JOIN project p ON p.id = t.project_id"
            " WHERE p.id = ? AND n.taken IS NULL AND n.created >= ? ORDER BY n.id",
            (proj["id"], cutoff)).fetchall():
        try:
            task = claim(conn, n["display_id"], session_id, transcript_path, cwd,
                         now=now, waiting_only=True)
        except CoordinatorError:
            task = None
        with transaction(conn):
            conn.execute("UPDATE start_note SET taken = ? WHERE id = ?",
                         (session_id if task else "skipped", n["id"]))
        if task:
            return task
    return None
