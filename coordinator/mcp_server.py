"""MCP server (stdio, newline-delimited JSON-RPC 2.0): a thin front over core.

Run as `python -m coordinator.mcp_server`. Stdout carries protocol messages only;
diagnostics go to stderr. Chats pass their own `session_id` (shown in the
session-start message) because the server never learns the Claude Code session."""
import json
import os
import sys
import traceback

if not __package__:  # started as a script: make `coordinator` importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coordinator import core, db  # noqa: E402
from coordinator.messages import CoordinatorError, render  # noqa: E402

SERVER_INFO = {"name": "coordinator", "version": "0.1.0"}
SID = ("The id of this chat session, as shown in the session-start message "
       "(\"session id\").")


def _s(desc, **extra):
    return {"type": "string", "description": desc, **extra}


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_SESSION = {"session_id": _s(SID)}
_ID = {"id": _s("Task id, for example KMU-12.")}

TOOLS = [
    {"name": "task_list",
     "description": "List tasks, optionally filtered by project key and/or state name.",
     "inputSchema": _schema({
         "project": _s("Project key, for example KMU."),
         "state": _s("State name to filter by.")})},
    {"name": "task_show",
     "description": "Show one task with its comments, events and claims.",
     "inputSchema": _schema(dict(_ID), ["id"])},
    {"name": "task_create",
     "description": ("Create a task in a project (no claim needed; at most 3 per "
                     "session). It lands in the person's inbox. " + SID),
     "inputSchema": _schema({
         **_SESSION,
         "project": _s("Project key, for example KMU."),
         "title": _s("Short title."),
         "goal": _s("What the task should achieve."),
         "done_when": _s("How to tell the task is done."),
         "scope": _s("What is in and out of scope."),
         "origin": _s("Where the task came from.")},
         ["session_id", "project", "title"])},
    {"name": "task_claim",
     "description": ("Claim a task to work on it; moves it to its first active "
                     "state. Needed before editing it. " + SID),
     "inputSchema": _schema({**_SESSION, **_ID}, ["session_id", "id"])},
    {"name": "task_release",
     "description": ("Report on a task you claimed. outcome done = work finished, "
                     "ready for review; clarify = you need an answer from the "
                     "person; progress = a status note, the claim stays. "
                     "A note is required. " + SID),
     "inputSchema": _schema({
         **_SESSION, **_ID,
         "outcome": _s("How the work stands.", enum=["done", "clarify", "progress"]),
         "note": _s("What was done, or what is needed.")},
         ["session_id", "id", "outcome", "note"])},
    {"name": "task_set_state",
     "description": ("Move a task to another state. Chats cannot set finished or "
                     "closed states; those are for the person. Active states need "
                     "your own claim. " + SID),
     "inputSchema": _schema({
         **_SESSION, **_ID,
         "state": _s("Target state name."),
         "expected_state": _s("Only move if the task is currently in this state."),
         "note": _s("Note; some states require one.")},
         ["session_id", "id", "state"])},
    {"name": "task_comment",
     "description": ("Add a comment to a task (no claim needed). Kinds: comment, "
                     "question, answer. Handoff and progress notes are written by "
                     "task_release. " + SID),
     "inputSchema": _schema({
         **_SESSION, **_ID,
         "kind": _s("Comment kind.", enum=["comment", "question", "answer"]),
         "text": _s("The comment text.")},
         ["session_id", "id", "kind", "text"])},
    {"name": "task_update",
     "description": ("Edit task fields: goal, done_when, scope (text), open_points "
                     "(list of text), blocked (text, or null to clear). Needs your "
                     "own claim. " + SID),
     "inputSchema": _schema({
         **_SESSION, **_ID,
         "fields": {
             "type": "object", "description": "Fields to change.",
             "properties": {
                 "goal": _s("New goal."), "done_when": _s("New done-when."),
                 "scope": _s("New scope."),
                 "open_points": {"type": "array", "items": {"type": "string"},
                                 "description": "Replacement list of open points."},
                 "blocked": {"type": ["string", "null"],
                             "description": "Blocking reason, or null to clear."}},
             "additionalProperties": False}},
         ["session_id", "id", "fields"])},
    {"name": "task_lower_mode",
     "description": ("Lower a task's mode to a more restrictive one. Chats cannot "
                     "raise a mode; ask the person. " + SID),
     "inputSchema": _schema({
         **_SESSION, **_ID, "mode": _s("The new, lower mode.")},
         ["session_id", "id", "mode"])},
]
_TOOL_NAMES = {t["name"] for t in TOOLS}


def _bad(what, why, fix):
    return CoordinatorError(what, why, fix)


def _req(args, name):
    v = args.get(name)
    if not isinstance(v, str) or not v.strip():
        raise _bad(f"The argument '{name}' is missing or empty.",
                   "This tool needs it as a non-empty text value.",
                   f"Call the tool again and pass '{name}' as a non-empty string.")
    return v


def _opt(args, name):
    v = args.get(name)
    if v is None:
        return None
    if not isinstance(v, str):
        raise _bad(f"The argument '{name}' has the wrong type.",
                   "It must be text.", f"Pass '{name}' as a string.")
    return v


def _actor(args):
    return core.Actor("chat", _req(args, "session_id"))


def _update(conn, args):
    actor, tid = _actor(args), _req(args, "id")
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise _bad("The argument 'fields' is missing or empty.",
                   "It must be an object with the fields to change.",
                   "Pass fields such as {\"goal\": \"...\"}.")
    allowed = ("goal", "done_when", "scope", "open_points", "blocked")
    unknown = [k for k in fields if k not in allowed]
    if unknown:
        raise _bad(f"Unknown field(s): {', '.join(unknown)}.",
                   f"Chats can change only: {', '.join(allowed)}.",
                   "Remove the unknown fields and call again.")
    for k in ("goal", "done_when", "scope"):
        if k in fields and not isinstance(fields[k], str):
            raise _bad(f"The field '{k}' has the wrong type.", "It must be text.",
                       f"Pass '{k}' as a string.")
    if "blocked" in fields and fields["blocked"] is not None \
            and not isinstance(fields["blocked"], str):
        raise _bad("The field 'blocked' has the wrong type.",
                   "It must be text, or null to clear.",
                   "Pass a string or null.")
    points = fields.get("open_points")
    if "open_points" in fields and (not isinstance(points, list)
                                    or not all(isinstance(p, str) for p in points)):
        raise _bad("The field 'open_points' has the wrong type.",
                   "It must be a list of text entries; nothing was changed.",
                   "Pass open_points as a list of strings, for example [\"a\", \"b\"].")
    # all fields are checked above, so a type error never leaves a partial update
    text ={k: fields[k] for k in ("goal", "done_when", "scope") if k in fields}
    result = None
    if text:
        result = core.set_fields(conn, tid, actor, **text)
    if "open_points" in fields:
        result = core.set_open_points(conn, tid, actor, fields["open_points"])
    if "blocked" in fields:
        result = core.set_blocked(conn, tid, actor, fields["blocked"])
    return result


def _dispatch(conn, name, a):
    if name == "task_list":
        state = _opt(a, "state")
        return core.list_tasks(conn, _opt(a, "project"),
                               [state] if state is not None else None)
    if name == "task_show":
        return core.get_task(conn, _req(a, "id"))
    if name == "task_create":
        return core.create_task(
            conn, _req(a, "project"), _req(a, "title"), _actor(a),
            goal=_opt(a, "goal") or "", done_when=_opt(a, "done_when") or "",
            scope=_opt(a, "scope") or "", origin=_opt(a, "origin") or "")
    if name == "task_claim":
        _actor(a)
        return core.claim(conn, _req(a, "id"), a["session_id"], "", "")
    if name == "task_release":
        _actor(a)
        return core.release(conn, _req(a, "id"), a["session_id"],
                            _req(a, "outcome"), _req(a, "note"))
    if name == "task_set_state":
        return core.set_state(conn, _req(a, "id"), _req(a, "state"), _actor(a),
                              expected_state=_opt(a, "expected_state"),
                              note=_opt(a, "note") or "")
    if name == "task_comment":
        return core.comment(conn, _req(a, "id"), _actor(a), _req(a, "kind"),
                            _req(a, "text"))
    if name == "task_update":
        return _update(conn, a)
    if name == "task_lower_mode":
        return core.set_mode(conn, _req(a, "id"), _req(a, "mode"), _actor(a))
    raise _bad(f"Unknown tool {name!r}.",
               f"This server offers: {', '.join(sorted(_TOOL_NAMES))}.",
               "Call one of the listed tools.")


def _tool_result(text, is_error):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name, args) -> dict:
    try:
        if not isinstance(name, str) or name not in _TOOL_NAMES:
            raise _bad(f"Unknown tool {name!r}.",
                       f"This server offers: {', '.join(sorted(_TOOL_NAMES))}.",
                       "Call one of the listed tools.")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise _bad("The tool arguments are not an object.",
                       "'arguments' must be a JSON object.",
                       "Pass the arguments as an object of named values.")
        conn = db.connect()
        try:
            result = _dispatch(conn, name, args)
        finally:
            conn.close()
        return _tool_result(json.dumps(result, indent=2, ensure_ascii=False), False)
    except CoordinatorError as e:
        return _tool_result(str(e), True)
    except Exception as e:
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        return _tool_result(render(
            f"The tool {name!r} failed with {type(e).__name__}.",
            f"Unexpected internal error: {e}",
            "Retry once; if it persists, tell the person and use the board or "
            "command line instead."), True)


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle_message(msg) -> dict | None:
    """Return the response for one decoded message, or None (notification)."""
    if (not isinstance(msg, dict) or not isinstance(msg.get("method"), str)
            or (msg.get("id") is not None and isinstance(msg["id"], (dict, list, bool)))):
        return _error(None, -32600, "Invalid Request")
    method, params = msg["method"], msg.get("params")
    if "id" not in msg:
        return None
    id_ = msg["id"]
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        version = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": version if isinstance(version, str) else "2025-03-26",
            "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": id_,
                "result": call_tool(params.get("name"), params.get("arguments"))}
    return _error(id_, -32601, f"Method not found: {method}")


def _write(out, obj):
    out.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    out.flush()


def main() -> int:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    for raw in stdin:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = _error(None, -32700, "Parse error")
        else:
            try:
                resp = handle_message(msg)
            except Exception:
                print(traceback.format_exc(), file=sys.stderr, flush=True)
                resp = _error(msg.get("id") if isinstance(msg, dict) else None,
                              -32603, "Internal error")
        if resp is not None:
            try:
                _write(stdout, resp)
            except OSError:
                return 0  # client went away
    return 0


if __name__ == "__main__":
    sys.exit(main())
