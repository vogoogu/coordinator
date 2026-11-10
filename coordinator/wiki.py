"""Wiki connector: what a project's wiki and Changes list say needs Daria.

Reads the project registry, runs the template's `project_state.py` and the project's
`tools/todo.py` as commands and turns their output into items (see sources.py). It
reads only; the core never knows this module. The "how to act" lines are the ones
the Daybook showed.
"""
import json
import os

from . import paths, sources
from .sources import Spec, parse_item, rows

DETAIL = "--detail"
HOW_TO = {
    "fork": "Open {project_dir} in VS Code, then type /queue",
    "conflict": "Open {project_dir} in VS Code, then type /conflicts",
    "suggestion": "Open {project_dir} in VS Code, then type /analyze",
    "new_file": "Open {project_dir} in VS Code, then type /ingest",
    "ingest_interrupted": "Open {project_dir} in VS Code, then type /ingest to resume",
    "change_open": "Hand it to the desk: open {project_dir} and run: python tools/todo.py request {id}",
    "change_queued": "Waiting for the desk: open {desk_dir} and type: status",
    "change_manual": "Do this change yourself in the Antson editor, details in TODO.md of {project_dir}",
    "waiting_publish": "Publish in Antson yourself, then open {project_dir} and run: python tools/todo.py publish",
}
HOW_TO["fork_count"], HOW_TO["conflict_count"] = HOW_TO["fork"], HOW_TO["conflict"]
HOW_TO["suggestion_count"], HOW_TO["new_file_count"] = HOW_TO["suggestion"], HOW_TO["new_file"]
COUNT_NOTE = " (dates not available yet)"
COUNTS = {  # kind -> (singular, plural)
    "fork_me": ("{n} open decision", "{n} open decisions"),
    "fork_client": ("{n} question waiting for the client", "{n} questions waiting for the client"),
    "fork_expert": ("{n} question waiting for an Antson expert", "{n} questions waiting for an Antson expert"),
    "conflict_open": ("{n} source disagreement", "{n} source disagreements"),
    "conflict_asked": ("{n} disagreement put to the client", "{n} disagreements put to the client"),
    "suggestion": ("{n} new idea from the analyst", "{n} new ideas from the analyst"),
    "new_file": ("{n} new document not read yet", "{n} new documents not read yet"),
}
INGEST_TEXT = "Import interrupted ({count} of {total} documents left)"
DRIFT_STATES = {"older", "newer", "missing", "unstamped", "no-catalogue"}


def how_to(item: dict, project_dir: str, desk_dir: str) -> str:
    """The item's own line if its source gave one, else the Daybook's line for its kind."""
    if item["how_to_act"]:
        return item["how_to_act"]
    t = HOW_TO.get(item["kind"], "")
    return t.format(project_dir=project_dir or "the project folder", desk_dir=desk_dir or "the desk folder",
                    id=item["id"]) if t else ""


# ------------------------------------------------------------------ which projects
def load_registry(path: str) -> list:
    """[{"name", "project_dir"}] from the desk's registry; project_dir is None when it cannot be found."""
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        folder = None
        try:
            with open(e.get("config", ""), encoding="utf-8") as f:
                folder = json.load(f).get("project_dir")
            folder = folder if folder and os.path.isdir(folder) else None
        except (OSError, ValueError, AttributeError):
            pass
        out.append({"name": str(e["name"]), "project_dir": folder})
    return out


def coordinator_key(project_dir, coord_projects: list):
    """The coordinator project that lives in this folder, or None."""
    if not project_dir:
        return None
    here = paths.norm(project_dir)
    for p in coord_projects:
        if paths.norm(p["root"]) == here:
            return p["key"]
    return None


# ------------------------------------------------------------------ commands
def specs(name: str, folder: str, state_script: str) -> list:
    items_cmd = (["python", state_script, folder, "--items", "--json"] if state_script
                 else ["python", "tools/project_state.py", ".", "--items", "--json"])
    return [Spec(f"{name}:todo", ["python", "tools/todo.py", "list"], folder),
            Spec(f"{name}:state", ["python", "tools/project_state.py", ".", "--json"], folder),
            Spec(f"{name}:state_items", sources.with_detail(items_cmd, DETAIL), folder, fallback=items_cmd)]


# ------------------------------------------------------------------ output to items
def _num(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _make(project, kind, item_id, title, waits_on, since=None) -> dict:
    return parse_item({"project": project, "kind": kind, "id": item_id, "title": title,
                       "waits_on": waits_on, "since": since}, project, "project:todo")


def _change_item(project, row):
    state = row.get("state")
    if not row.get("id"):
        return None
    if state == "todo":
        return _make(project, "change_manual" if row.get("manual") else "change_open", row["id"],
                     row.get("title"), "me", row.get("created"))
    if state == "queued":
        return _make(project, "change_queued", row["id"], row.get("title"), "claude", row.get("created"))
    if state == "done" and row.get("saved_at") and not row.get("published_at") and row.get("closed_by") != "checked":
        return _make(project, "waiting_publish", row["id"], row.get("title"), "claude", row.get("saved_at"))
    return None


def _count_text(n: int, key: str) -> str:
    one, many = COUNTS[key]
    return (one if n == 1 else many).format(n=n) + COUNT_NOTE


def _count_items(project, state, covered) -> list:
    """When the item list did not carry a kind, the counts from the status still say that something waits."""
    out = []
    forks = state.get("forks") if isinstance(state.get("forks"), dict) else {}
    if "fork" not in covered:
        for key, who, text in (("operator", "me", "fork_me"), ("client", "client", "fork_client"),
                               ("antson-expert", "antson-expert", "fork_expert")):
            if _num(forks.get(key)):
                out.append(_make(project, "fork_count", "forks-" + who, _count_text(forks[key], text), who))
    conflicts = state.get("conflicts") if isinstance(state.get("conflicts"), dict) else {}
    if "conflict" not in covered:
        if _num(conflicts.get("open")):
            out.append(_make(project, "conflict_count", "conflicts-open",
                             _count_text(conflicts["open"], "conflict_open"), "me"))
        if _num(conflicts.get("asked")):
            out.append(_make(project, "conflict_count", "conflicts-asked",
                             _count_text(conflicts["asked"], "conflict_asked"), "client"))
    if "suggestion" not in covered and _num(state.get("suggestions_new")):
        out.append(_make(project, "suggestion_count", "suggestions-new",
                         _count_text(state["suggestions_new"], "suggestion"), "me"))
    files = state.get("new_files") if isinstance(state.get("new_files"), list) else []
    if "new_file" not in covered and files:
        out.append(_make(project, "new_file_count", "new-files", _count_text(len(files), "new_file"), "me"))
    unf = state.get("ingest_unfinished") if isinstance(state.get("ingest_unfinished"), dict) else {}
    if "ingest_interrupted" not in covered and _num(unf.get("count")):
        out.append(_make(project, "ingest_interrupted", "ingest",
                         INGEST_TEXT.format(count=unf["count"], total=_num(unf.get("total"))), "me"))
    return out


def health_lines(state) -> list:
    """Short facts about the project's own state (not items)."""
    if not isinstance(state, dict):
        return []
    out = []
    last = state.get("last_commit") if isinstance(state.get("last_commit"), dict) else {}
    if last.get("when"):
        out.append("last save " + str(last["when"])[:16].replace("T", " "))
    if _num(state.get("uncommitted")):
        out.append("changes not saved yet in the project: %d" % state["uncommitted"])
    if state.get("backup_stale"):
        out.append("no backup for more than a day")
    drift = state.get("skill_drift") if isinstance(state.get("skill_drift"), dict) else {}
    if drift.get("state") in DRIFT_STATES:
        out.append("template or skill is out of date (%s): run /update" % drift["state"])
    unf = state.get("ingest_unfinished") if isinstance(state.get("ingest_unfinished"), dict) else {}
    if _num(unf.get("count")):
        out.append("import interrupted: %d document(s) left" % unf["count"])
    if _num(state.get("outbox")):
        out.append("feedback entries waiting to be sent: %d" % state["outbox"])
    return out


def read_project(name: str, results: dict) -> dict:
    """{"items", "health", "down": [source names], "stale": [{"name", "at"}]} from the three results."""
    todo, state, listed = (results.get(f"{name}:{p}") for p in ("todo", "state", "state_items"))
    data = lambda r: r.data if r is not None and r.ok else None   # noqa: E731
    items = [i for i in (_change_item(name, r) for r in data(todo) if isinstance(r, dict)) if i] \
        if isinstance(data(todo), list) else []
    covered = set()
    for row in rows(data(listed)):
        it = {**parse_item(row, name, "project"), "project": name}
        items.append(it)
        covered.add(it["kind"])
    st = data(state) if isinstance(data(state), dict) else {}
    items.extend(_count_items(name, st, covered))
    return {"items": items, "health": health_lines(st),
            "down": [{"name": r.name, "error": r.error} for r in (todo, state, listed) if r is not None and not r.ok],
            "stale": [{"name": r.name, "at": r.at.isoformat(), "error": r.error}
                      for r in (todo, state, listed) if r is not None and r.ok and r.stale]}
