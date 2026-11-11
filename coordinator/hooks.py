"""Claude Code hooks: a thin front over core. Fails open: never crashes a session.

Run as `python -m coordinator.hooks <event>` or `python <path>\\hooks.py <event>`
with event in session-start, pre-tool, post-tool, stop, prompt. The hook payload is JSON
on stdin."""
import json
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime

if not __package__:  # started as a script: make `coordinator` importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from coordinator import config, core, db, messages, notify, paths  # noqa: E402
    _IMPORT_FAILURE = None
except Exception:  # fail open: an import problem must not break the session
    config = core = db = messages = notify = paths = None
    _IMPORT_FAILURE = traceback.format_exc()

EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
PACKET_NOTES = 3
STOP_NOTE = "stopped without report after reminder"


@dataclass
class HookResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


# ------------------------------------------------------------------ logging
def _append(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def log_error(event: str) -> None:
    """Log the current exception; app_home first, temp dir as fallback."""
    text = (f"{datetime.now().isoformat(timespec='seconds')} {event}\n"
            f"{traceback.format_exc()}\n")
    try:
        _append(paths.app_home() / "hook-errors.log", text)
        return
    except Exception:
        pass
    try:
        _append(os.path.join(tempfile.gettempdir(), "coordinator-hook-errors.log"), text)
    except Exception:
        pass


# ------------------------------------------------------------------ helpers
def _target(payload: dict) -> str:
    """Absolute path an edit tool works on, or '' if it has none."""
    ti = payload.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    target = ti.get("file_path") or ti.get("notebook_path") or ""
    if not target:
        return ""
    if not os.path.isabs(target):
        target = os.path.join(payload.get("cwd") or "", target)
    return target


def _project_of(conn, *candidates):
    for c in candidates:
        if c:
            proj = core.project_for_path(conn, c)
            if proj:
                return proj
    return None


def _packet(conn, task: dict, session_id: str) -> str:
    root = core.get_project(conn, task["project"])["root"]
    cfg = config.load_project(root)
    full = core.get_task(conn, task["id"])
    notes = [c for c in full["comments"] if c["kind"] in ("handoff", "progress")]
    lines = [
        f"You hold task {task['id']}: {task['title']}",
        f"Mode: {task['mode']} | State: {task['state']}",
        f"Goal: {task['goal'] or '-'}",
        f"Done when: {task['done_when'] or '-'}",
        f"Scope: {task['scope'] or '-'}",
        "Open points: " + ("; ".join(task["open_points"]) or "-"),
    ]
    if notes:
        lines.append("Last notes:")
        lines += [f"- [{c['kind']}] {c['text']}" for c in notes[-PACKET_NOTES:]]
    if cfg.claim == "keep":
        lines.append("Claiming did not change the state; move the task yourself with "
                     "task_set_state as the work progresses.")
    lines += [
        "Rules:",
        f"1. Your session id is {session_id}; pass it to every coordinator MCP tool.",
        "2. When you stop, call the MCP tool task_release with outcome done, "
        "clarify or progress and a short note.",
        f"3. You may not set done or raise the mode (modes: {', '.join(cfg.modes)}).",
        f"4. Write only inside {root}.",
    ]
    return "\n".join(lines)


def _howto(conn, sid: str, cwd: str) -> str:
    """One line: open tasks of the project (if any) and how to claim, with the
    session id (R25)."""
    claim = (f"Your session id is {sid}; to work on a task, claim it with the MCP "
             "tool task_claim (task id and this session id).")
    proj = _project_of(conn, cwd)
    if not proj:
        return f"Coordinator: {claim}"
    cfg = config.load_project(proj["root"])
    final = {s.name for s in cfg.states if s.kind in ("finished", "closed")}
    n = sum(1 for t in core.list_tasks(conn, proj["key"]) if t["state"] not in final)
    return f"Coordinator: project {proj['key']} has {n} open task(s). {claim}"


def _finding(rule: str, what: str, why: str, fix: str):
    return rule, messages.render(what, why, fix)


# ------------------------------------------------------------------ events
def _session_start(conn, payload, env) -> HookResult:
    sid = payload.get("session_id") or ""
    if not sid:
        return HookResult()
    cwd = payload.get("cwd") or ""
    transcript = payload.get("transcript_path") or ""
    source = payload.get("source") or "startup"
    m = notify.mark(conn)
    expired = core.expire_leases(conn)
    if expired:
        notify.fire(conn, m, expired)
    wanted = env.get("COORD_TASK")
    launch = env.get("COORD_LAUNCH") or None
    if wanted and source == "clear" and launch:
        # R32: /clear started a new session id in the window the board opened
        try:
            task = core.reclaim_after_clear(conn, wanted, sid, launch, transcript, cwd)
        except core.CoordinatorError:
            task = None
        if task:
            return HookResult(stdout=_packet(conn, task, sid) + "\n")
    elif wanted and source == "startup":
        # R30: only a fresh start claims, and only a task that is still waiting;
        # resume/compact/clear re-attach below instead of claiming again.
        try:
            task = core.claim(conn, wanted, sid, transcript, cwd, launch=launch,
                              waiting_only=True)
        except core.CoordinatorError as e:
            return HookResult(stdout=f"{e}\n{_howto(conn, sid, cwd)}\n")
        if task:
            return HookResult(stdout=_packet(conn, task, sid) + "\n")
    held = core.claim_for_session(conn, sid)
    if held:
        return HookResult(stdout=_packet(conn, core.get_task(conn, held["display_id"]),
                                         sid) + "\n")
    if not _project_of(conn, cwd):
        return HookResult()
    return HookResult(stdout=_howto(conn, sid, cwd) + "\n")


def _pre_tool(conn, payload, env) -> HookResult:
    if payload.get("tool_name") not in EDIT_TOOLS:
        return HookResult()
    sid = payload.get("session_id") or ""
    if not sid:
        return HookResult()
    cwd = payload.get("cwd") or ""
    target = _target(payload)
    held = core.claim_for_session(conn, sid)
    findings = []
    if held is None:
        if not _project_of(conn, cwd, target):
            return HookResult()
        findings.append(_finding(
            "no_claim",
            "you are changing a file without a claimed task.",
            "work in this project is tracked per task, and your session holds no claim.",
            f"claim a task first with the MCP tool task_claim (session id {sid})."))
    else:
        task = core.get_task(conn, held["display_id"])
        proj = core.get_project(conn, task["project"])
        cfg = config.load_project(proj["root"])
        if task["mode"] == cfg.modes[0]:
            findings.append(_finding(
                "read_mode",
                f"{task['id']} is in mode {task['mode']!r}, which is read-only.",
                "this mode allows looking and commenting, not changing files.",
                "ask the person to raise the mode, or add a comment instead."))
        if target and not paths.same_or_inside(target, proj["root"]):
            findings.append(_finding(
                "outside_scope",
                f"{target} is outside the project folder of {task['id']}.",
                f"a claimed task allows writing only inside {proj['root']}.",
                "write inside the project folder, or ask the person."))
    if not findings:
        return HookResult()
    if core.get_setting(conn, "enforcement", "warn") == "block":
        return HookResult(2, "", "\n\n".join(t for _, t in findings) + "\n")
    for rule, text in findings:
        core.record_hook_warning(conn, sid, rule, text)
    return HookResult()


def _post_tool(conn, payload, env) -> HookResult:
    sid = payload.get("session_id") or ""
    if not sid:
        return HookResult()
    core.touch(conn, sid, payload.get("tool_name") or "", _target(payload))
    texts = core.take_hook_warnings(conn, sid)
    if not texts:
        return HookResult()
    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                  "additionalContext": "\n\n".join(texts)}}
    return HookResult(stdout=json.dumps(out) + "\n")


def _stop(conn, payload, env) -> HookResult:
    sid = payload.get("session_id") or ""
    if not sid or not core.needs_report(conn, sid):
        return HookResult()
    if payload.get("stop_hook_active"):
        m = notify.mark(conn)
        stopped = core.stop_without_report(conn, sid, STOP_NOTE)
        if stopped:  # filter: other fronts' events may land in this window
            notify.fire(conn, m, [stopped["id"]])
        return HookResult()
    held = core.claim_for_session(conn, sid)
    if held is None:  # the claim ended after needs_report: nothing to remind
        return HookResult()
    tid = held["display_id"]
    return HookResult(2, "", (
        f"WHAT: you worked on {tid} and are stopping without a report. "
        "WHY: the next chat and the person rely on it. "
        f"FIX: call the MCP tool task_release with id {tid}, session_id {sid} and "
        "outcome done (your part is finished), clarify (you need an answer) or "
        "progress (you are only pausing), with a short note.\n"))


def _prompt(conn, payload, env) -> HookResult:
    """The person wrote: a chat that asked a question takes its task back (COORD-10)."""
    sid = payload.get("session_id") or ""
    if not sid:
        return HookResult()
    try:
        task = core.reclaim_after_answer(conn, sid, payload.get("transcript_path") or "",
                                         payload.get("cwd") or "")
    except core.CoordinatorError:
        return HookResult()
    if not task:
        return HookResult()
    return HookResult(stdout=(
        f"Coordinator: you asked a question on {task['id']} and the person has written "
        f"back, so this session holds {task['id']} again (state: {task['state']}). If the "
        f"message is not about {task['id']}, call task_release with outcome progress.\n"))


EVENTS = {"session-start": _session_start, "pre-tool": _pre_tool,
          "post-tool": _post_tool, "stop": _stop, "prompt": _prompt}


def handle(event: str, payload: dict, env: dict) -> HookResult:
    """Run one hook event. Any exception is logged and the hook exits 0."""
    try:
        if _IMPORT_FAILURE:
            raise ImportError(_IMPORT_FAILURE)
        fn = EVENTS.get(event)
        if fn is None:
            raise ValueError(f"unknown hook event {event!r}")
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not a JSON object")
        conn = db.connect()
        try:
            return fn(conn, payload, env or {})
        finally:
            conn.close()
    except Exception:
        log_error(str(event))
        return HookResult()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception:
        log_error(event)
        return 0
    res = handle(event, payload, dict(os.environ))
    try:
        sys.stdout.buffer.write(res.stdout.encode("utf-8"))
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(res.stderr.encode("utf-8"))
        sys.stderr.buffer.flush()
    except BaseException:
        log_error(event)
        os._exit(0)  # a second flush failure at shutdown must not change the status
    return res.exit_code


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        log_error("main")
        code = 0
    sys.exit(code)
