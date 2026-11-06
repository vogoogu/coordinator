"""Command line: a thin front over core. All writes go through core."""
import argparse
import getpass
import json
import re
import sys
from datetime import date
from pathlib import Path

from . import config, core, db, messages, paths

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_EVENTS = (
    ("SessionStart", None, "session-start"),
    ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit", "pre-tool"),
    ("PostToolUse", None, "post-tool"),
    ("Stop", None, "stop"),
)


def _actor() -> core.Actor:
    return core.Actor("person", getpass.getuser())


def _toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


# ---------------------------------------------------------------- commands

def cmd_init_project(args, conn):
    folder = Path(args.folder)
    target = folder / paths.PROJECT_FILE
    if target.exists():
        raise messages.CoordinatorError(
            f"Project {args.key!r} was not created.",
            f"{target} already exists and would be overwritten.",
            "Use add-project to register the existing folder, or edit the file by hand.")
    text = config.DEFAULT_FILE.read_text(encoding="utf-8")
    text, n1 = re.subn(r'^key = ""', lambda m: "key = " + _toml_string(args.key),
                       text, count=1, flags=re.M)
    text, n2 = re.subn(r'^name = ""', lambda m: "name = " + _toml_string(args.name),
                       text, count=1, flags=re.M)
    if n1 != 1 or n2 != 1:
        raise messages.CoordinatorError(
            "The project file was not written.",
            "default_project.toml has no key and name lines to fill in.",
            "Restore coordinator\\default_project.toml.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    proj = core.add_project(conn, str(folder), _actor())
    print(f"Project {proj['key']} registered at {proj['root']}")
    return 0


def cmd_add_project(args, conn):
    proj = core.add_project(conn, args.folder, _actor())
    print(f"Project {proj['key']} registered at {proj['root']}")
    return 0


def cmd_list(args, conn):
    live = core.live_claims(conn)
    for t in core.list_tasks(conn, args.project):
        inbox = "INBOX" if t["inbox"] else "-"
        claim = live.get(t["id"])
        holder = f"  [claimed by {claim['session_id']}]" if claim else ""
        print(f"{t['id']}  {t['state']}  {t['mode']}  {inbox}  {t['title']}{holder}")
    return 0


def cmd_show(args, conn):
    t = core.get_task(conn, args.id)
    print(f"{t['id']}: {t['title']}")
    for key in ("state", "mode", "blocked", "goal", "done_when", "scope", "origin",
                "created_by_type", "created_by_id", "created", "updated"):
        print(f"{key}: {t[key] if t[key] is not None else ''}")
    print(f"inbox: {'yes' if t['inbox'] else 'no'}")
    print("open points:")
    for p in t["open_points"]:
        print(f"  - {p}")
    print("comments:")
    for c in t["comments"]:
        print(f"  {c['at']} {c['actor_type']}:{c['actor_id']} [{c['kind']}] {c['text']}")
    print("claims:")
    for c in t["claims"]:
        end = f"ended {c['ended']} ({c['end_reason']})" if c["ended"] else "live"
        print(f"  session {c['session_id']}, started {c['started']}, {end}")
        print(f"    transcript: {c['transcript_path'] or '-'}")
        print(f"    resume: claude --resume {c['session_id']}")
    print("history:")
    for e in t["history"]:
        line = f"  {e['at']} {e['actor_type']}:{e['actor_id']} {e['action']}"
        if e["from_value"] is not None or e["to_value"] is not None:
            line += f" {e['from_value'] or ''} -> {e['to_value'] or ''}"
        if e["note"]:
            line += f" ({e['note']})"
        print(line)
    return 0


def cmd_create(args, conn):
    t = core.create_task(conn, args.project, args.title, _actor(), goal=args.goal,
                         done_when=args.done_when, scope=args.scope, inbox=False)
    print(f"Created {t['id']}")
    return 0


def cmd_state(args, conn):
    t = core.set_state(conn, args.id, args.state, _actor(),
                       expected_state=args.expected, note=args.note)
    print(f"{t['id']} is now {t['state']}")
    return 0


def cmd_mode(args, conn):
    t = core.set_mode(conn, args.id, args.mode, _actor())
    print(f"{t['id']} mode is now {t['mode']}")
    return 0


def cmd_release(args, conn):
    t = core.force_release(conn, args.id, _actor())
    print(f"{t['id']} released, state {t['state']}")
    return 0


def cmd_pin(args, conn):
    t = core.pin(conn, args.id, _actor())
    print(f"{t['id']} pinned (not in the inbox)")
    return 0


def cmd_enforcement(args, conn):
    if args.value is None:
        print(core.get_setting(conn, "enforcement", "warn"))
    else:
        core.set_enforcement(conn, args.value, _actor())
        print(f"enforcement is now {args.value}")
    return 0


def cmd_expire(args, conn):
    ids = core.expire_leases(conn, hours=args.hours)
    print("Expired: " + ", ".join(ids) if ids else "Nothing expired.")
    return 0


def _daily_backup(conn, force=True) -> Path:
    dest = paths.app_home() / "backups"
    today = dest / f"{date.today().isoformat()}.db"
    if not force and today.exists():
        return today
    return db.backup(conn, dest)


def cmd_backup(args, conn):
    print(f"Backup written to {_daily_backup(conn)}")
    return 0


def cmd_board(args, conn):
    _daily_backup(conn, force=False)
    conn.close()
    from . import board
    board.serve(port=args.port, open_browser=not args.no_browser)
    return 0


def install_help_text() -> str:
    repo_fwd = REPO_ROOT.as_posix()
    repo_back = str(REPO_ROOT).replace("/", "\\")
    hooks = {}
    for name, matcher, event in HOOK_EVENTS:
        entry = {"hooks": [{"type": "command", "timeout": 30,
                            "command": f'python "{repo_fwd}/coordinator/hooks.py" {event}'}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        hooks[name] = [entry]
    block = json.dumps({"hooks": hooks}, indent=2)
    return (
        "1. Hooks. Merge this into %USERPROFILE%\\.claude\\settings.json "
        '(the "hooks" key):\n\n'
        f"{block}\n\n"
        "2. MCP server. Run this once in a terminal:\n\n"
        f'claude mcp add --transport stdio --env "PYTHONPATH={repo_back}" '
        "--scope user coordinator -- python -m coordinator.mcp_server\n\n"
        "The hooks act only inside registered projects. "
        "Start with enforcement warn (python -m coordinator enforcement warn).\n"
        "This command applies nothing; review the text and apply it by hand.\n")


def cmd_install_help(args, conn):
    print(install_help_text(), end="")
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m coordinator",
                                description="Task coordinator for Claude Code chats.")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("init-project", cmd_init_project, "create project.toml and register")
    sp.add_argument("folder")
    sp.add_argument("--key", required=True)
    sp.add_argument("--name", required=True)
    sp = add("add-project", cmd_add_project, "register an existing project folder")
    sp.add_argument("folder")
    sp = add("list", cmd_list, "list tasks")
    sp.add_argument("--project")
    sp = add("show", cmd_show, "show one task with history")
    sp.add_argument("id")
    sp = add("create", cmd_create, "create a task")
    sp.add_argument("project")
    sp.add_argument("title")
    sp.add_argument("--goal", default="")
    sp.add_argument("--done-when", default="")
    sp.add_argument("--scope", default="")
    sp = add("state", cmd_state, "set a task state")
    sp.add_argument("id")
    sp.add_argument("state")
    sp.add_argument("--note", default="")
    sp.add_argument("--expected", default=None)
    sp = add("mode", cmd_mode, "set a task mode")
    sp.add_argument("id")
    sp.add_argument("mode")
    sp = add("release", cmd_release, "force-release a task's claim")
    sp.add_argument("id")
    sp = add("pin", cmd_pin, "take a task out of the inbox")
    sp.add_argument("id")
    sp = add("enforcement", cmd_enforcement, "show or set warn/block")
    sp.add_argument("value", nargs="?", default=None)
    sp = add("expire", cmd_expire, "expire stale claims now")
    sp.add_argument("--hours", type=float, default=core.LEASE_HOURS)
    add("backup", cmd_backup, "back up the database now")
    sp = add("board", cmd_board, "run the board")
    sp.add_argument("--port", type=int, default=8790)
    sp.add_argument("--no-browser", action="store_true")
    add("install-help", cmd_install_help, "print hook JSON and MCP command")
    return p


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _utf8()
    args = build_parser().parse_args(argv)
    conn = None
    try:
        if args.fn is cmd_install_help:
            return args.fn(args, None)
        conn = db.connect()
        return args.fn(args, conn)
    except messages.CoordinatorError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
