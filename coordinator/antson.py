"""Antson mode guard connector: reads the guard's files and calls its `mode.py` as a
command. The core never knows about this module (see CLAUDE.md).

What it does and does not do:
- Reads `modes.json`, `reports.jsonl` and `go\\<session>.json` from the guard's state
  folder (`paths.antson_state_dir()`). It never writes any of them.
- Changes a mode only by running `python <guard>\\mode.py set <ids...> <mode> --by board
  --json`. Raising opens Daria's Yes/No window inside that command; this module never
  answers it. The command can take up to 60 s, so it runs in a background thread.
- Remembers how far it has read `reports.jsonl` in `reports-intake.json` (app home).
"""
import json
import os
import re
import subprocess
import sys
import threading
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import paths

MODES = ("read", "dev", "prod")
RANK = {"read": 0, "prod": 1, "dev": 2}             # the guard's order
DEFAULT_MODE_PY = r"C:\Projects\_mail-desk\tools\guard\mode.py"
SETTINGS_FILE = "antson.toml"                       # in the app home: mode_py = "<path>"
INTAKE_FILE = "reports-intake.json"
MAX_INTAKE = 5000
STALE_GO = timedelta(hours=12)                      # a go file untouched this long is ignored
SET_TIMEOUT = 90                                    # seconds: the window itself waits 60
ID_OK = re.compile(r"^(\d+|[A-Za-z0-9_]+:\d+)$")    # never starts with "-": it goes to a command line
_lock = threading.Lock()


# ------------------------------------------------------------------ where things are
def mode_py_path() -> str:
    """The guard's mode.py: env COORDINATOR_GUARD_MODE_PY, else `mode_py` in
    antson.toml in the app home, else the mail desk's copy."""
    env = os.environ.get("COORDINATOR_GUARD_MODE_PY")
    if env:
        return env
    try:
        with open(paths.app_home() / SETTINGS_FILE, "rb") as f:
            value = tomllib.load(f).get("mode_py")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return DEFAULT_MODE_PY


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ modes.json
def load_modes() -> dict:
    """The items of modes.json ({} if the file is missing or unreadable)."""
    data = _read_json(paths.antson_state_dir() / "modes.json")
    items = data.get("items") if isinstance(data, dict) else None
    return {str(k): v for k, v in items.items() if isinstance(v, dict)} if isinstance(items, dict) else {}


def effective(item_id: str, items: dict) -> tuple:
    """(mode, inherited) the way the guard computes it: an inheriting item has its
    parent's mode; unknown or broken is read."""
    seen, inherited = set(), False
    while item_id not in seen:
        seen.add(item_id)
        item = items.get(item_id)
        if not isinstance(item, dict):
            return "read", inherited
        if item.get("inherit") and item.get("parent"):
            item_id, inherited = str(item["parent"]), True
            continue
        mode = item.get("mode")
        return (mode if mode in RANK else "read"), inherited
    return "read", inherited


# ------------------------------------------------------------------ reports.jsonl
def _merge_report(items: dict, r: dict) -> None:
    rid = r.get("id")
    if not isinstance(rid, (str, int)) or not str(rid):
        return
    rid = str(rid)
    cur = items.get(rid)
    if cur is None:
        items[rid] = {"id": rid, "kind": r.get("kind") or "unknown", "name": r.get("name"),
                      "parent": r.get("parent"), "session": r.get("session") or "",
                      "at": r.get("at") or "", "event": r.get("event") or "seen",
                      "tenant": r.get("tenant")}
        return
    if r.get("name"):
        cur["name"] = r["name"]
    if r.get("parent"):
        cur["parent"] = r["parent"]
    if r.get("kind") and cur.get("kind") in (None, "unknown"):
        cur["kind"] = r["kind"]
    if r.get("event") == "created":
        cur["event"] = "created"
        cur["session"] = r.get("session") or cur["session"]


def read_reports() -> dict:
    """Items reported by chats ({id: report}), read incrementally: only the lines
    added since the last call are parsed. The offset and the result are kept in
    reports-intake.json. A shorter file than the offset means it was replaced: start over."""
    src = paths.antson_state_dir() / "reports.jsonl"
    store = paths.app_home() / INTAKE_FILE
    with _lock:
        state = _read_json(store)
        if not (isinstance(state, dict) and isinstance(state.get("items"), dict)
                and isinstance(state.get("offset"), int)):
            state = {"offset": 0, "items": {}}
        offset, items = state["offset"], state["items"]
        try:
            size = src.stat().st_size
        except OSError:
            return items
        changed = False
        if size < offset:
            offset, items, changed = 0, {}, True
        if size > offset:
            with open(src, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            cut = chunk.rfind(b"\n")          # a half-written last line waits for the next call
            if cut >= 0:
                for line in chunk[:cut + 1].decode("utf-8", "replace").splitlines():
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        _merge_report(items, r)
                offset += cut + 1
                changed = True
        if len(items) > MAX_INTAKE:
            keep = sorted(items.values(), key=lambda v: v.get("at") or "")[-MAX_INTAKE:]
            items = {v["id"]: v for v in keep}
        if changed:
            tmp = store.with_name(store.name + ".tmp")
            try:
                tmp.write_text(json.dumps({"offset": offset, "items": items},
                                          ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, store)
            except OSError:
                pass                           # next call reads the same lines again
        return items


# ------------------------------------------------------------------ the hash check
_hash_cache = {"key": None, "value": None}


def _stat_key(*names):
    out = []
    for n in names:
        try:
            st = (paths.antson_state_dir() / n).stat()
            out.append((n, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((n, None, None))
    return tuple(out)


def _run(args, timeout):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run([sys.executable, mode_py_path(), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=flags)


def hash_ok():
    """True/False from `mode.py check-hash`; None when there is nothing to check or
    the command cannot run. The answer is cached until modes.json or its log change,
    so a page load does not start Python each time."""
    key = (mode_py_path(), _stat_key("modes.json", "modes-log.jsonl"))
    with _lock:
        if _hash_cache["key"] == key:
            return _hash_cache["value"]
    value = None
    if key[1][0][1] is not None and Path(mode_py_path()).is_file():
        try:
            value = _run(["check-hash"], 30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            value = None
    with _lock:
        _hash_cache.update(key=key, value=value)
    return value


# ------------------------------------------------------------------ go files
def go_waiting() -> list:
    """What the chats wait for. Per go file (one per chat session, ignored when
    untouched for STALE_GO):
      {"kind": "go", "session", "list_id", "items": [{"n", "form", "ref", "what"}], "since"}
          the newest list has no approval yet;
      {"kind": "ready_refused", "session", "form", "reasons": [...], "since"}
          the last ready check of that form was refused."""
    out, now = [], datetime.now(timezone.utc)
    for f in sorted((paths.antson_state_dir() / "go").glob("*.json")):
        try:
            st = f.stat()
        except OSError:
            continue
        touched = datetime.fromtimestamp(st.st_mtime, timezone.utc)
        if now - touched > STALE_GO:
            continue
        data = _read_json(f)
        if not isinstance(data, dict):
            continue
        since, session = touched.isoformat(timespec="seconds"), f.stem
        lists = [x for x in data.get("lists") or [] if isinstance(x, dict)]
        approved = {a.get("list_id") for a in data.get("approved") or [] if isinstance(a, dict)}
        if lists and lists[-1].get("list_id") not in approved:
            last = lists[-1]
            items = [{"n": i.get("n"), "form": str(i.get("form") or ""), "ref": i.get("ref") or "",
                      "what": i.get("what") or ""}
                     for i in last.get("items") or [] if isinstance(i, dict)]
            out.append({"kind": "go", "session": session, "list_id": last.get("list_id") or "",
                        "items": items, "since": since})
        refused = data.get("ready_refused")
        for form, reasons in (refused.items() if isinstance(refused, dict) else []):
            if reasons:
                out.append({"kind": "ready_refused", "session": session, "form": str(form),
                            "reasons": [str(r) for r in reasons] if isinstance(reasons, list)
                            else [str(reasons)], "since": since})
    return out


# ------------------------------------------------------------------ switching a mode
class Job:
    """One `mode.py set` call in the background."""
    def __init__(self, ids, mode):
        self.ids, self.mode = list(ids), mode
        self.started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.state = "waiting"          # waiting | done
        self.changed, self.refused, self.reason, self.error = [], [], None, ""
        self.thread = None


_job = None


def current_job():
    return _job


def start_set(ids, mode) -> Job:
    """Start `mode.py set` for these ids; returns at once. Only one call at a time.
    Raises ValueError for a bad id or mode and RuntimeError while another call waits."""
    global _job
    ids = list(dict.fromkeys(ids))
    if mode not in MODES or not ids or not all(ID_OK.match(i) for i in ids):
        raise ValueError("bad ids or mode")
    with _lock:
        if _job is not None and _job.state == "waiting":
            raise RuntimeError("busy")
        job = _job = Job(ids, mode)
    job.thread = threading.Thread(target=_run_set, args=(job,), daemon=True)
    job.thread.start()
    return job


def _run_set(job: Job) -> None:
    try:
        if not Path(mode_py_path()).is_file():
            job.error = f"mode.py was not found at {mode_py_path()}"
            return
        proc = _run(["set", *job.ids, job.mode, "--by", "board", "--json"], SET_TIMEOUT)
        line = next((x for x in reversed(proc.stdout.splitlines()) if x.strip().startswith("{")), "")
        try:
            res = json.loads(line)
        except ValueError:
            job.error = ((proc.stderr or proc.stdout).strip().splitlines() or ["mode.py gave no answer"])[-1]
            return
        job.changed = list(res.get("changed") or [])
        job.refused = list(res.get("refused") or [])
        job.reason = res.get("reason")
    except subprocess.TimeoutExpired:
        job.error = "mode.py did not answer in time; nothing was changed"
    except (OSError, subprocess.SubprocessError) as e:
        job.error = f"mode.py could not run: {e}"
    finally:
        job.state = "done"


def wait_idle(timeout: float = 10) -> None:
    """Wait for the running call to end (tests)."""
    job = _job
    if job is not None and job.thread is not None:
        job.thread.join(timeout)

