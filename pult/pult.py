"""Пульт: доска, которая сама собирается из истории Claude Code.

Запуск: двойной клик по START.bat (или `python pult.py`).
Только стандартная библиотека. Ничего не устанавливает, ничего не меняет в
настройках Claude Code, никуда не отправляет данные: читает файлы истории
(%USERPROFILE%\\.claude\\projects) и показывает их на http://127.0.0.1:8791
"""
import hashlib
import urllib.request
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("PULT_PORT", "8791"))
TOKEN = secrets.token_hex(16)


def code_version() -> str:
    """Hash of the program files: tells a running Pult that it was updated."""
    h = hashlib.md5()
    for name in ("pult.py", "pult.html"):
        try:
            h.update((HERE / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12]


VERSION = code_version()
CACHE_DIR = HERE / "cache"
SUMMARY_FILE = CACHE_DIR / "summaries.json"
BRIEF_FILE = CACHE_DIR / "briefs.json"
TRANSLATION_FILE = CACHE_DIR / "translations.json"
CARD_FILE = CACHE_DIR / "cards.json"
CONFIG_FILE = CACHE_DIR / "config.json"
LANGS = {"en": "English", "de": "German"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next", "cache"}
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
RUNNING_SECONDS = 150          # файл менялся недавно = чат ещё работает
SID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

_cache = {}                    # path -> (mtime, size, parsed)
_paths = {}                    # session id -> transcript path
_lock = threading.Lock()


APPS = {"claude-vscode": "vscode", "claude-desktop": "desktop", "cli": "terminal"}


def find_claude() -> str:
    """Full path of the Claude Code program, or ''. The `claude` command from PATH;
    without it (Claude Code used only inside VS Code) the copy that the newest
    VS Code extension brings along."""
    exe = shutil.which("claude")
    if exe:
        return exe
    home = Path.home()
    found = [home / ".local" / "bin" / "claude.exe"]
    try:
        found += sorted((home / ".vscode" / "extensions").glob(
            "anthropic.claude-code-*/resources/native-binary/claude.exe"),
            key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        pass
    for f in found:
        if f.is_file():
            return str(f)
    return ""


def claude_command() -> str:
    """How to type the program in a terminal: `claude`, or the quoted full path."""
    if shutil.which("claude"):
        return "claude"
    exe = find_claude()
    return f'"{exe}"' if exe else "claude"


def open_in_vscode(cwd: str, query: str) -> str:
    """Open the folder in VS Code, then hand the Claude Code extension a link
    (vscode://anthropic.claude-code/open?session=<id> or ?prompt=<text>). The link
    goes to the VS Code window that is in front, so the folder is opened first and
    the link follows a few seconds later. Returns an error text or ''."""
    if sys.platform != "win32":
        return "This button works on Windows only."
    if not shutil.which("code"):
        return "The VS Code command (code) was not found in PATH."
    try:
        subprocess.Popen(["cmd", "/c", "code", cwd], creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as e:
        return f"Could not open VS Code: {e}"
    threading.Timer(3.0, os.startfile, [f"vscode://anthropic.claude-code/open?{query}"]).start()
    return ""


def claude_dir() -> Path:
    return Path(os.environ.get("PULT_CLAUDE_DIR") or (Path.home() / ".claude"))


# ----------------------------------------------------------------- разбор
def _text_of(content) -> str:
    """Текст сообщения; пустая строка, если это только результаты инструментов."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _is_real_prompt(text: str) -> bool:
    return bool(text) and not text.startswith(("<", "Caveat:", "[Request interrupted"))


def _short(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _plain(line: str) -> str:
    line = re.sub(r"`([^`]*)`", r"\1", line)
    line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    return line.strip(" #*_>")


def bullets_from(text: str, limit: int = 4) -> list:
    """3-4 коротких пункта из последнего ответа, без ИИ: списки, иначе предложения."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    items = [_plain(re.sub(r"^([-*•]|\d+[.)])\s+", "", l)) for l in lines
             if re.match(r"^([-*•]|\d+[.)])\s+", l)]
    if len(items) < 2:
        flat = " ".join(_plain(l) for l in lines if not l.startswith("|"))
        items = [s for s in re.split(r"(?<=[.!?])\s+", flat) if len(s) > 3]
    return [_short(i, 170) for i in items if i][:limit]


def _asks_question(text: str) -> bool:
    tail = [l for l in text.strip().splitlines() if l.strip()][-3:]
    return any("?" in l for l in tail)


def parse_session(path: Path):
    title = cwd = branch = entry = ""
    first_prompt = last_prompt = last_answer = ""
    turns = 0
    files = set()
    last = ""            # prompt | answer | tool | result | interrupted
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("isSidechain"):
                    continue
                for k in ("customTitle", "aiTitle", "summary"):
                    if isinstance(e.get(k), str) and e[k].strip():
                        title = e[k].strip()
                cwd = e.get("cwd") or cwd
                branch = e.get("gitBranch") or branch
                entry = e.get("entrypoint") or entry
                msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                content = msg.get("content")
                kind = e.get("type")
                if kind == "user" and not e.get("isMeta"):
                    text = _text_of(content)
                    if text.startswith("[Request interrupted"):
                        last = "interrupted"
                    elif _is_real_prompt(text):
                        turns += 1
                        first_prompt = first_prompt or text
                        last_prompt = text
                        last = "prompt"
                    elif isinstance(content, list):
                        last = "result"
                elif kind == "assistant":
                    text = _text_of(content)
                    tools = [b for b in content or [] if isinstance(b, dict)
                             and b.get("type") == "tool_use"] if isinstance(content, list) else []
                    for b in tools:
                        if b.get("name") in EDIT_TOOLS:
                            p = (b.get("input") or {}).get("file_path")
                            if p:
                                files.add(p)
                    if text:
                        last_answer, last = text, "answer"
                    if tools:
                        last = "tool"
    except OSError:
        return None
    if not turns:
        return None
    return {"title": _short(title or first_prompt, 90), "cwd": cwd, "branch": branch,
            "first_prompt": _short(first_prompt, 300), "last_prompt": _short(last_prompt, 300),
            "last_answer": last_answer[-4000:], "bullets": bullets_from(last_answer),
            "asks": _asks_question(last_answer) if last_answer else False,
            "turns": turns, "files": len(files), "last": last, "app": APPS.get(entry, "")}


def _status(parsed, age: float) -> str:
    if age < RUNNING_SECONDS:
        return "running"
    if parsed["last"] == "answer":
        return "waiting" if parsed["asks"] else "review"
    return "broken"


def _hue(name: str) -> int:
    return int(hashlib.md5(name.encode("utf-8")).hexdigest()[:4], 16) % 360


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _norm(p: str) -> str:
    return re.sub(r"[\\/]+", "/", p or "").rstrip("/").lower()


def _name(p: str) -> str:
    return re.split(r"[\\/]", p.rstrip("\\/"))[-1] or p


def folders() -> list:
    return [f for f in _load(CONFIG_FILE).get("folders", []) if isinstance(f, str)]


def _root_of(cwd: str, added: list) -> str:
    """Project folder of a chat. An added folder works as a workspace: a chat in
    one of its subfolders belongs to that first-level subfolder (one project per
    subfolder); a chat started in the added folder itself belongs to it."""
    n = _norm(cwd)
    best = ""
    for f in added:
        nf = _norm(f)
        if (n == nf or n.startswith(nf + "/")) and len(nf) > len(_norm(best)):
            best = f
    if not best:
        return cwd
    nb = _norm(best)
    if n == nb:
        return best
    parts = re.split(r"[\\/]+", cwd.rstrip("\\/"))
    sep = "\\" if "\\" in best else "/"
    return best.rstrip("\\/") + sep + parts[len(nb.split("/"))]


def _subfolders(folder: str) -> list:
    """First-level subfolders of an added folder: each one is a project."""
    try:
        names = sorted(e.name for e in os.scandir(folder)
                       if e.is_dir() and not e.name.startswith((".", "_", "$"))
                       and e.name not in SKIP_DIRS)
    except OSError:
        return []
    sep = "\\" if "\\" in folder else "/"
    return [folder.rstrip("\\/") + sep + n for n in names[:80]]


def sources() -> list:
    """Folders where chat transcripts are kept. Only Claude Code: the terminal, the
    VS Code extension and the Code tab of the Claude desktop app all write to
    <home>/.claude/projects. Cowork sessions (the desktop app's
    local-agent-mode-sessions folder) are left out on purpose."""
    return [claude_dir() / "projects"]


# ---------------------------------------------- coordinator (read-only)
STATE_STATUS = {"clarify": "waiting", "review": "review", "done": "done", "dropped": "done"}
ACTIVE_STATES = ("building",)          # a chat holds the task and works on it
PLANNED_STATES = ("open", "ready")     # waiting for a chat
BACKLOG_STATES = ("backlog",)          # notes for the future, nobody may claim them


def coordinator_home() -> Path:
    """The coordinator data folder, found the way coordinator/paths.py finds it:
    COORDINATOR_HOME, then <home>/.coordinator, then the old %LOCALAPPDATA% folder
    (only while the new one has no database yet). Never creates or copies anything."""
    env = os.environ.get("COORDINATOR_HOME")
    if env:
        return Path(env)
    new = Path(os.environ.get("USERPROFILE") or str(Path.home())) / ".coordinator"
    old = Path(os.environ.get("LOCALAPPDATA")
               or str(Path.home() / "AppData" / "Local")) / "coordinator"
    if not (new / "coordinator.db").is_file() and (old / "coordinator.db").is_file():
        return old
    return new


def coordinator_db() -> Path:
    return coordinator_home() / "coordinator.db"


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def coordinator_data():
    """Tasks from the coordinator database, if it exists on this computer.
    Returns ({session_id: task info}, [tasks without a chat]). Never writes.
    Columns of newer database versions (chat-name word, waits-for-click mark,
    running helpers) are read only when they exist."""
    db = coordinator_db()
    if not db.is_file():
        return {}, []
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            word = "t.word" if "word" in _columns(conn, "task") else "''"
            click = "cl.waiting_click" if "waiting_click" in _columns(conn, "claim") else "NULL"
            helpers = ("(SELECT ss.helpers FROM session_stat ss WHERE ss.session_id = "
                       "cl.session_id)" if "helpers" in _columns(conn, "session_stat") else "0")
            note = ("(SELECT text FROM comment c WHERE c.task_id = t.id AND c.kind IN"
                    " ('handoff','question','progress') ORDER BY c.id DESC LIMIT 1)")
            by_session = {}
            for r in conn.execute(
                    "SELECT cl.session_id, cl.ended, t.number, t.state, t.title, p.key, "
                    f"{word} AS word, {click} AS click, {helpers} AS helpers, "
                    f"{note} AS note FROM claim cl JOIN task t ON t.id = cl.task_id "
                    "JOIN project p ON p.id = t.project_id ORDER BY cl.id"):
                live = r["ended"] is None
                by_session[r["session_id"]] = {
                    "task": f"{r['key']}-{r['number']}", "state": r["state"],
                    "task_title": r["title"], "live": live, "note": r["note"] or "",
                    "word": r["word"] or "", "click": live and bool(r["click"]),
                    "helpers": int(r["helpers"] or 0) if live else 0}
            states = PLANNED_STATES + BACKLOG_STATES
            marks = ",".join("?" * len(states))
            planned = [dict(r) for r in conn.execute(
                "SELECT t.number, t.state, t.title, t.goal, t.done_when, t.updated, p.key, "
                f"p.root, {word} AS word "
                "FROM task t JOIN project p ON p.id = t.project_id "
                f"WHERE t.state IN ({marks}) AND NOT EXISTS "
                "(SELECT 1 FROM claim cl WHERE cl.task_id = t.id AND cl.ended IS NULL)",
                states)]
        finally:
            conn.close()
        return by_session, planned
    except (sqlite3.Error, OSError, ValueError):
        return {}, []


def task_status(task: dict, guess: str) -> str:
    """Board column of a chat that holds or held a coordinator task. The reported
    state wins over the guess from the transcript."""
    if task["state"] in STATE_STATUS:
        return STATE_STATUS[task["state"]]
    if task["live"] and task["state"] in ACTIVE_STATES:
        if task["click"]:
            return "waiting"           # Claude Code stopped on a permission question
        if task["helpers"]:
            return "running"           # a background helper works, the chat file is quiet
    return guess


def _epoch(iso: str, default: float) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ scan
def sessions_raw(since: float, until: float = 0):
    """([(path, stat, parsed)], stats) for chats changed between since and until.
    Looks in every subfolder of every source; one chat id is taken once."""
    own = _norm(str(CACHE_DIR))
    until = until or time.time() + 60
    newest, stats = {}, []
    for root in sources():
        st_ = {"path": str(root), "found": root.is_dir(), "files": 0, "in_range": 0}
        stats.append(st_)
        if not st_["found"]:
            continue
        for path in root.rglob("*.jsonl"):
            if not SID_RE.match(path.stem):
                continue               # sub-agent logs, audit files
            try:
                st = path.stat()
            except OSError:
                continue
            st_["files"] += 1
            _paths[path.stem] = path
            if not since <= st.st_mtime <= until:
                continue
            st_["in_range"] += 1
            old = newest.get(path.stem)
            if not old or old[1].st_mtime < st.st_mtime:
                newest[path.stem] = (path, st)
    out = []
    for path, st in newest.values():
        key = str(path)
        with _lock:
            hit = _cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            parsed = hit[2]
        else:
            parsed = parse_session(path)
            with _lock:
                _cache[key] = (st.st_mtime, st.st_size, parsed)
        if parsed and not _norm(parsed["cwd"]).startswith(own):
            out.append((path, st, parsed))
    return out, stats


def scan(since: float, until: float = 0) -> dict:
    now = time.time()
    raw, stats = sessions_raw(since, until)
    added = folders()
    summaries, briefs, trans = _load(SUMMARY_FILE), _load(BRIEF_FILE), _load(TRANSLATION_FILE)
    by_session, planned = coordinator_data()
    out = []
    for path, st, parsed in raw:
        cwd = parsed["cwd"] or path.parent.name
        sid = path.stem
        summ = summaries.get(sid)
        if summ and summ.get("mtime") != st.st_mtime:
            summ = None            # the chat went on, the summary is stale
        s = {"id": sid, "cwd": cwd, "root": _root_of(cwd, added), "updated": st.st_mtime,
             "status": _status(parsed, now - st.st_mtime),
             "ai": summ["bullets"] if summ else None, "resumable": os.path.isdir(cwd),
             "chat": True, "app": parsed["app"], "resume_in": resume_where(parsed["app"]),
             **{k: parsed[k] for k in ("title", "branch", "first_prompt",
                                       "last_prompt", "bullets", "turns", "files")}}
        task = by_session.get(sid)
        if task:                   # exact state reported through coordinator
            s.update(task=task["task"], state=task["state"], word=task["word"],
                     click=task["click"], helpers=task["helpers"])
            s["status"] = task_status(task, s["status"])
            if task["note"] and not task["live"]:
                s["bullets"] = bullets_from(task["note"]) or s["bullets"]
        s["tr"] = {lang: t for lang in LANGS
                   for t in [trans.get(f"{sid}:{lang}")]
                   if t and t.get("mtime") == st.st_mtime}
        out.append(s)
    for t in planned:
        goal = [x for x in (t["goal"], t["done_when"]) if x]
        out.append({"id": f"task:{t['key']}-{t['number']}", "cwd": t["root"],
                    "root": _root_of(t["root"], added), "updated": _epoch(t["updated"], now),
                    "status": "backlog" if t["state"] in BACKLOG_STATES else "planned",
                    "word": t["word"] or "", "ai": None, "resumable": False, "chat": False,
                    "task": f"{t['key']}-{t['number']}", "state": t["state"],
                    "title": _short(t["title"], 90), "branch": "", "first_prompt": "",
                    "last_prompt": "", "bullets": [_short(g, 170) for g in goal],
                    "turns": 0, "files": 0, "tr": {}})
    marks = _load(CARD_FILE)
    for s in out:                  # the user's own marks: done, archived, priority
        m = marks.get(s["id"]) or {}
        fresh = m.get("at") == s["updated"]     # new activity puts a card back
        s["priority"] = m.get("priority") or 0
        s["archived"] = bool(m.get("archived")) and fresh
        if m.get("done") and fresh:
            s["status"], s["marked_done"] = "done", True
    out.sort(key=lambda s: (-s["priority"], -s["updated"]))
    roots = {}
    for f in added:
        roots[_norm(f)] = f
    for s in out:
        roots.setdefault(_norm(s["root"]), s["root"])
        s["root"] = roots[_norm(s["root"])]
    for f in added:                # every subfolder of an added folder is a project
        for sub in _subfolders(f):
            roots.setdefault(_norm(sub), sub)
    added_n = {_norm(f) for f in added}
    projects = [{"root": r, "name": _name(r), "hue": _hue(n), "added": n in added_n,
                 "brief": (briefs.get(n) or {}).get("bullets")}
                for n, r in roots.items()]
    return {"sessions": out, "projects": projects, "now": now, "sources": stats,
            "found": any(x["found"] for x in stats),
            "has_claude": bool(find_claude()), "claude_cmd": claude_command(),
            "coordinator": bool(by_session or planned),
            "version": VERSION, "stale": code_version() != VERSION}


# --------------------------------------------------------------- actions
def _session_file(sid: str):
    if not SID_RE.match(sid or ""):
        return None
    if sid not in _paths:
        sessions_raw(time.time())      # fills the id -> path index
    return _paths.get(sid)


MODELS = ("opus", "sonnet", "haiku")


def start_claude(cwd: str, model: str = "", prompt: str = "", title: str = "") -> str:
    """Open Claude Code in a new console window in `cwd`, optionally with a model and a
    first message. Returns an error text, or '' when the window was opened.
    The prompt travels in an environment variable, so cmd never parses its text."""
    model = model if model in MODELS else ""
    prompt = re.sub(r"\s+", " ", prompt.replace('"', "'")).strip()[:4000]
    if not find_claude():
        return "Claude Code was not found: neither the claude command nor the VS Code extension."
    line = claude_command() + (f" --model {model}" if model else "") + (' "%PULT_PROMPT%"' if prompt else "")
    if sys.platform != "win32":
        return f'This button works on Windows only. Command: cd "{cwd}" && {line}'
    title = re.sub(r'[^\w .\-]', "", title)[:60] or "claude"
    try:
        subprocess.Popen(f"cmd /k title {title} & {line}", cwd=cwd,
                         env={**os.environ, "PULT_PROMPT": prompt},
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError as e:
        return f"Could not open the window: {e}"
    return ""


def _known_folder(path: str) -> bool:
    data = scan(time.time() - 365 * 86400)
    known = {_norm(p["root"]) for p in data["projects"]} | {_norm(x["cwd"]) for x in data["sessions"]}
    return _norm(path) in known


def new_chat(body: dict) -> dict:
    """Start a new Claude Code chat in a project folder shown on the board."""
    root = str(body.get("id") or "")
    if not _known_folder(root):
        return {"ok": False, "error": "Unknown folder."}
    if not os.path.isdir(root):
        return {"ok": False, "error": "This folder is not on this computer."}
    err = start_claude(root, str(body.get("model") or ""), str(body.get("prompt") or ""),
                       _name(root))
    return {"ok": not err, "error": err}


def resume_where(app: str) -> str:
    """'vscode' or 'terminal': where Continue chat opens a chat. A chat that ran in
    VS Code goes back to VS Code; so does every chat when there is no claude
    program for a terminal."""
    if shutil.which("code") and (app == "vscode" or not find_claude()):
        return "vscode"
    return "terminal"


def resume(body: dict) -> dict:
    """Continue this chat: in VS Code (the Claude Code panel opens the session) or in
    a new console window (claude --resume)."""
    sid = str(body.get("id") or "")
    path = _session_file(sid)
    parsed = parse_session(path) if path else None
    if not parsed or not os.path.isdir(parsed["cwd"]):
        return {"ok": False, "error": "The project folder of this chat was not found."}
    if sys.platform != "win32":
        return {"ok": False, "error": "This button works on Windows only. Command: "
                f'cd "{parsed["cwd"]}" && claude --resume {sid}'}
    if resume_where(parsed["app"]) == "vscode":
        err = open_in_vscode(parsed["cwd"], "session=" + sid)
        return {"ok": not err, "error": err}
    exe = find_claude()
    if not exe:
        return {"ok": False, "error": "Claude Code was not found: neither the claude command "
                "nor the VS Code extension."}
    try:
        subprocess.Popen(["cmd", "/k", exe, "--resume", sid], cwd=parsed["cwd"],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError as e:
        return {"ok": False, "error": f"Could not open the window: {e}"}
    return {"ok": True}


def run_claude(prompt: str, timeout: int = 180):
    """Run `claude -p` (uses the subscription, so only on a button press).
    Returns (answer text, error text)."""
    exe = find_claude()
    if not exe:
        return "", "Claude Code was not found: neither the claude command nor the VS Code extension."
    CACHE_DIR.mkdir(exist_ok=True)
    try:
        r = subprocess.run([exe, "-p", "--model", "haiku"], input=prompt,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(CACHE_DIR))
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", f"claude did not answer: {e}"
    out = (r.stdout or "").strip()
    return out, "" if out else _short(r.stderr or "empty answer", 300)


def ask_claude(prompt: str):
    """Like run_claude, but returns the '- ' lines of the answer as a list."""
    out, err = run_claude(prompt)
    items = [_short(_plain(l.strip()[2:]), 260) for l in out.splitlines()
             if l.strip().startswith("- ")][:6]
    return items, "" if items else err or _short(out, 300)


def translate(body: dict) -> dict:
    """Translate the texts of several cards in one AI call; cached per chat."""
    code = str(body.get("lang") or "")
    if code not in LANGS:
        return {"ok": False, "error": "Unknown language."}
    ids = [i for i in body.get("ids") or [] if isinstance(i, str)][:12]
    cards, mtimes = [], {}
    for sid in ids:
        path = _session_file(sid)
        parsed = parse_session(path) if path else None
        if not parsed:
            continue
        mtimes[sid] = path.stat().st_mtime
        summ = _load(SUMMARY_FILE).get(sid) or {}
        ai = summ.get("bullets") if summ.get("mtime") == mtimes[sid] else None
        cards.append({"id": sid, "title": parsed["title"], "points": ai or parsed["bullets"],
                      "request": parsed["last_prompt"]})
    if not cards:
        return {"ok": True, "done": 0}
    out, err = run_claude(
        f"Translate every text value in this JSON into {LANGS[code]}. Keep the structure, "
        "the keys and the \"id\" values exactly as they are; keep file names, code and "
        f"product names untranslated; text that is already in {LANGS[code]} stays as it is. "
        "Reply with the JSON array only, no code fence, no comment.\n\n"
        + json.dumps(cards, ensure_ascii=False), timeout=240)
    try:
        result = json.loads(out[out.index("["): out.rindex("]") + 1])
    except ValueError:
        return {"ok": False, "error": err or "The AI did not return a translation."}
    done = 0
    with _lock:
        data = _load(TRANSLATION_FILE)
        for item in result if isinstance(result, list) else []:
            sid = item.get("id") if isinstance(item, dict) else None
            if sid in mtimes:
                data[f"{sid}:{code}"] = {
                    "mtime": mtimes[sid], "title": _short(str(item.get("title") or ""), 120),
                    "bullets": [_short(str(p), 260) for p in item.get("points") or []][:6],
                    "last_prompt": _short(str(item.get("request") or ""), 400)}
                done += 1
        _save(TRANSLATION_FILE, data)
    return {"ok": True, "done": done}


def _lang(body: dict) -> str:
    return LANGS.get(str(body.get("lang") or ""), "English")


def summarize(body: dict) -> dict:
    sid = str(body.get("id") or "")
    path = _session_file(sid)
    parsed = parse_session(path) if path else None
    if not parsed or not parsed["last_answer"]:
        return {"ok": False, "error": "This chat has no answer to summarise."}
    items, err = ask_claude(
        "Below is the last answer of an AI assistant in a work chat. Condense it for the "
        "owner of the task. Reply only with 2-4 lines, each starting with '- ': first what "
        f"was done, and as the last line what to do next (start it with 'Next:'). Write in "
        f"{_lang(body)}, whatever the language of the text. No introduction.\n\n"
        + parsed["last_answer"])
    if not items:
        return {"ok": False, "error": err}
    with _lock:
        data = _load(SUMMARY_FILE)
        data[sid] = {"bullets": items, "mtime": path.stat().st_mtime}
        _save(SUMMARY_FILE, data)
    return {"ok": True, "bullets": items}


def _folder_facts(root: str) -> str:
    """A short text picture of a folder: tree, newest files, README start."""
    tree, files = [], []
    base = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(base).parts)
        dirnames[:] = [d for d in sorted(dirnames)
                       if d not in SKIP_DIRS and not d.startswith(".")][:25]
        if depth >= 2:
            dirnames[:] = []
        if depth and len(tree) < 70:
            tree.append("  " * (depth - 1) + Path(dirpath).name + "/")
        for fn in filenames[:200]:
            fp = Path(dirpath) / fn
            try:
                files.append((fp.stat().st_mtime, str(fp.relative_to(base))))
            except OSError:
                pass
        if len(files) > 4000:
            break
    files.sort(reverse=True)
    recent = [f"{time.strftime('%Y-%m-%d', time.localtime(m))}  {p}" for m, p in files[:15]]
    readme = ""
    for cand in ("README.md", "readme.md", "README.txt", "CLAUDE.md"):
        try:
            readme = (base / cand).read_text(encoding="utf-8", errors="replace")[:1500]
            break
        except OSError:
            continue
    return ("Folders:\n" + "\n".join(tree or ["(no subfolders)"]) +
            f"\n\nFiles in total: {len(files)}\nNewest files:\n" + "\n".join(recent) +
            ("\n\nREADME start:\n" + readme if readme else ""))


def brief(body: dict) -> dict:
    """AI overview of one project: folder contents plus its recent chats."""
    root = str(body.get("id") or "")
    recent = scan(time.time() - 90 * 86400)
    if _norm(root) not in {_norm(p["root"]) for p in recent["projects"]}:
        return {"ok": False, "error": "Unknown project."}
    facts = _folder_facts(root) if os.path.isdir(root) else "(the folder is not on this computer)"
    chats = []
    for s in recent["sessions"]:
        if s["chat"] and _norm(s["root"]) == _norm(root) and len(chats) < 8:
            chats.append(f"[{time.strftime('%Y-%m-%d', time.localtime(s['updated']))}] "
                         f"{s['title']}\n  asked last: {_short(s['last_prompt'], 200)}\n"
                         f"  AI answered last: {' / '.join(s['ai'] or s['bullets'])}")
    items, err = ask_claude(
        "You are looking at one project folder on a person's computer and their recent AI "
        "chats in it. Give the owner a status overview. Reply only with exactly 4 lines, "
        "each starting with '- ' and the label shown: 'What it is: ...', 'Open: ...' (work "
        "that is unfinished or waiting), 'Last discussed: ...', 'Next step: ...'. Be specific, "
        f"one sentence each. Translate the labels and write in {_lang(body)}. Say so plainly "
        "if something cannot be told from the material.\n\n"
        f"Folder: {root}\n\n{facts}\n\nRecent chats (newest first):\n"
        + ("\n".join(chats) or "(none)"))
    if not items:
        return {"ok": False, "error": err}
    with _lock:
        data = _load(BRIEF_FILE)
        data[_norm(root)] = {"bullets": items, "lang": str(body.get("lang") or "en"),
                             "at": time.time()}
        _save(BRIEF_FILE, data)
    return {"ok": True}


def open_folder(body: dict) -> dict:
    """Open a project folder in the file manager. Only folders Pult itself shows."""
    path = str(body.get("id") or "")
    if not _known_folder(path):
        return {"ok": False, "error": "Unknown folder."}
    if not os.path.isdir(path):
        return {"ok": False, "error": "This folder is not on this computer."}
    if sys.platform != "win32":
        return {"ok": False, "error": f"This button works on Windows only. Folder: {path}"}
    try:
        os.startfile(path)
    except OSError as e:
        return {"ok": False, "error": f"Could not open the folder: {e}"}
    return {"ok": True}


# ---------------------------------------------------------------- setup
# One-click switches for the coordinator rules. Every change makes a backup
# next to the file first, and a file that cannot be read is never rewritten.
RULE_START = "<!-- pult: coordinator rules -->"
RULE_END = "<!-- /pult: coordinator rules -->"
RULE_TEXT = """## Coordinator
In a folder registered in coordinator, before you change any file:
1. Create a task for the user's request with the MCP tool task_create
   (short title, goal, done_when, scope), then claim it with task_claim.
2. When you finish or stop, call task_release with outcome done, clarify
   or progress and a short note in English.
3. When the user asks for a note for the future, create it with task_create
   and backlog: true (only the title is needed). Do not claim it.
4. A background helper reports with task_comment and its role
   (for example build 1, test 1)."""
# The same list as coordinator/cli.py (install-help). hook_events() reads the list
# from the coordinator next to Pult, so a newer coordinator brings its own hooks;
# this copy is used only when that import fails.
POST_TOOL_MATCHER = ("Write|Edit|MultiEdit|NotebookEdit|Read|Grep|Glob|Bash|PowerShell"
                     "|WebFetch|WebSearch|Agent|Task|mcp__.*")
HOOK_EVENTS = (("SessionStart", None, "session-start"),
               ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit", "pre-tool"),
               ("PostToolUse", POST_TOOL_MATCHER, "post-tool"),
               ("Stop", None, "stop"),
               ("UserPromptSubmit", None, "prompt"),
               ("Notification", "permission_prompt", "notification"),
               ("SubagentStart", None, "subagent-start"),
               ("SubagentStop", None, "subagent-stop"))


def hook_events() -> tuple:
    repo = coordinator_repo()
    if repo:
        try:
            if str(repo) not in sys.path:
                sys.path.insert(0, str(repo))
            from coordinator.cli import HOOK_EVENTS as events
            return tuple(events)
        except Exception:
            pass
    return HOOK_EVENTS


def coordinator_repo():
    """The folder that holds the coordinator package (Pult sits inside it)."""
    for cand in (os.environ.get("COORDINATOR_REPO"), HERE.parent, HERE.parent.parent):
        if cand and (Path(cand) / "coordinator" / "hooks.py").is_file():
            return Path(cand).resolve()
    return None


def _claude_json() -> Path:
    return Path(os.environ.get("PULT_CLAUDE_JSON") or (Path.home() / ".claude.json"))


def _backup(path: Path) -> None:
    if path.is_file():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.pult-backup-{stamp}"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".pult-tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read_settings():
    """(settings dict, error text). A missing file is an empty dict."""
    path = claude_dir() / "settings.json"
    if not path.exists():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError):
        return None, (f"{path} is not valid JSON, so Claude Code ignores it. Fix it first "
                      "(Claude Code offers 'Fix with Claude' when it starts).")
    if not isinstance(data, dict):
        return None, f"{path} does not hold a JSON object."
    return data, ""


def _json_parts(text: str) -> list:
    """JSON objects written back to back in one file (the usual broken settings.json:
    a hooks block pasted after the closing brace). Raises ValueError otherwise."""
    dec, i, parts = json.JSONDecoder(), 0, []
    text = text.strip()
    while i < len(text):
        obj, i = dec.raw_decode(text, i)
        if not isinstance(obj, dict):
            raise ValueError("not an object")
        parts.append(obj)
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
    return parts


def _merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge(out[k], v) if k in out else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        seen = {json.dumps(x, sort_keys=True) for x in a}
        return a + [x for x in b if json.dumps(x, sort_keys=True) not in seen]
    return b


def _repairable() -> bool:
    try:
        text = (claude_dir() / "settings.json").read_text(encoding="utf-8-sig")
        json.loads(text)
        return False
    except ValueError:
        pass
    except OSError:
        return False
    try:
        return len(_json_parts(text)) > 1
    except ValueError:
        return False


def setup_repair(body=None) -> dict:
    """Merge back-to-back JSON objects in settings.json into one (backup first)."""
    path = claude_dir() / "settings.json"
    try:
        text = path.read_text(encoding="utf-8-sig")
        json.loads(text)
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": f"Could not read {path}: {e}"}
    except ValueError:
        pass
    try:
        parts = _json_parts(text)
    except ValueError:
        parts = []
    if len(parts) < 2:
        return {"ok": False, "error": "Pult cannot repair this file automatically. In Claude "
                "Code choose 'Fix with Claude'."}
    merged = {}
    for p in parts:
        merged = _merge(merged, p)
    _backup(path)
    _write_text(path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True}


def _is_coord_hook(entry) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return any(isinstance(h, dict) and "coordinator/hooks.py" in
               str(h.get("command", "")).replace("\\", "/") for h in hooks or [])


def _hooks_on(data) -> bool:
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return isinstance(hooks, dict) and all(
        any(_is_coord_hook(e) for e in hooks.get(name) or [] if isinstance(hooks.get(name), list))
        for name, _, _ in hook_events())


def _mcp_on() -> bool:
    try:
        servers = json.loads(_claude_json().read_text(encoding="utf-8")).get("mcpServers") or {}
        return "coordinator" in servers
    except (OSError, ValueError, AttributeError):
        return False


def _rule_on() -> bool:
    try:
        return RULE_START in (claude_dir() / "CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        return False


def _registered() -> list:
    db = coordinator_db()
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            return [{"key": r[0], "name": r[1], "root": r[2]}
                    for r in conn.execute("SELECT key, name, root FROM project ORDER BY key")]
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return []


def setup_status(body=None) -> dict:
    repo = coordinator_repo()
    settings, err = _read_settings()
    regs = _registered()
    reg_n = [_norm(r["root"]) for r in regs]
    folders_ = [{"path": f, "covered": any(_norm(f) == r or _norm(f).startswith(r + "/")
                                           for r in reg_n)} for f in folders()]
    return {"ok": True, "repo": str(repo or ""), "has_claude": bool(find_claude()),
            "settings_error": err, "repairable": bool(err) and _repairable(), "hooks": bool(settings) and _hooks_on(settings),
            "mcp": _mcp_on(), "rule": _rule_on(), "registered": regs, "folders": folders_}


def setup_hooks(body: dict) -> dict:
    on = bool(body.get("on"))
    repo = coordinator_repo()
    if on and not repo:
        return {"ok": False, "error": "The coordinator folder was not found next to Pult."}
    data, err = _read_settings()
    if data is None:
        return {"ok": False, "error": err}
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        return {"ok": False, "error": "The hooks entry in settings.json has an unexpected form."}
    hooks = dict(hooks or {})
    for name, matcher, event in hook_events():
        kept = [e for e in hooks.get(name) or [] if not _is_coord_hook(e)]
        if on:
            entry = {"hooks": [{"type": "command", "timeout": 30, "command":
                                f'python "{repo.as_posix()}/coordinator/hooks.py" {event}'}]}
            if matcher:
                entry = {"matcher": matcher, **entry}
            kept.append(entry)
        if kept:
            hooks[name] = kept
        else:
            hooks.pop(name, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    path = claude_dir() / "settings.json"
    _backup(path)
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True}


def setup_mcp(body: dict) -> dict:
    on = bool(body.get("on"))
    exe = find_claude()
    if not exe:
        return {"ok": False, "error": "Claude Code was not found: neither the claude command "
                "nor the VS Code extension."}
    if on == _mcp_on():
        return {"ok": True}
    if on:
        repo = coordinator_repo()
        if not repo:
            return {"ok": False, "error": "The coordinator folder was not found next to Pult."}
        args = [exe, "mcp", "add", "--transport", "stdio", "--env", f"PYTHONPATH={repo}",
                "--scope", "user", "coordinator", "--", "python", "-m", "coordinator.mcp_server"]
    else:
        args = [exe, "mcp", "remove", "coordinator", "--scope", "user"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, cwd=str(Path.home()))
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"claude mcp did not finish: {e}"}
    if r.returncode != 0:
        return {"ok": False, "error": _short(r.stderr or r.stdout or "claude mcp failed", 300)}
    return {"ok": True}


def setup_rule(body: dict) -> dict:
    on = bool(body.get("on"))
    path = claude_dir() / "CLAUDE.md"
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as e:
        return {"ok": False, "error": f"Could not read {path}: {e}"}
    text = re.sub(re.escape(RULE_START) + r".*?" + re.escape(RULE_END) + r"\n?", "",
                  text, flags=re.S)
    if on:
        text = text.rstrip() + ("\n\n" if text.strip() else "") + \
            f"{RULE_START}\n{RULE_TEXT}\n{RULE_END}\n"
    _backup(path)
    _write_text(path, text)
    return {"ok": True}


def _new_key(name: str, taken: set) -> str:
    base = re.sub(r"[^A-Z0-9]", "", name.upper())[:5] or "P"
    key, n = base, 2
    while key.lower() in taken:
        key, n = f"{base}{n}", n + 1
    return key


def setup_register(body: dict) -> dict:
    """Register an added Pult folder in coordinator, so the rules act there."""
    folder = str(body.get("id") or "")
    if _norm(folder) not in {_norm(f) for f in folders()}:
        return {"ok": False, "error": "Only folders added in Pult can be registered here."}
    repo = coordinator_repo()
    if not repo:
        return {"ok": False, "error": "The coordinator folder was not found next to Pult."}
    if (Path(folder) / ".coordinator" / "project.toml").is_file():
        args = ["add-project", folder]
    else:
        taken = {r["key"].lower() for r in _registered()}
        args = ["init-project", folder, "--key", _new_key(_name(folder), taken),
                "--name", _name(folder)]
    env = {**os.environ, "PYTHONPATH": str(repo), "PYTHONIOENCODING": "utf-8"}
    try:
        r = subprocess.run([sys.executable, "-m", "coordinator", *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60,
                           cwd=str(repo), env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"coordinator did not finish: {e}"}
    if r.returncode != 0:
        return {"ok": False, "error": _short(r.stderr or r.stdout or "coordinator failed", 400)}
    return {"ok": True}


PICKER = ("import tkinter, tkinter.filedialog as f\nr = tkinter.Tk(); r.withdraw()\n"
          "r.attributes('-topmost', True)\nprint(f.askdirectory() or '')")


def add_folder(body: dict) -> dict:
    path = str(body.get("path") or "").strip().strip('"')
    if not path:                   # no path given: show the system folder dialog
        try:
            r = subprocess.run([sys.executable, "-c", PICKER], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=600,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "need_path": True}
        if r.returncode != 0:
            return {"ok": False, "need_path": True}
        path = r.stdout.strip()
        if not path:
            return {"ok": True, "cancelled": True}
    if not os.path.isdir(path):
        return {"ok": False, "error": f"Not a folder on this computer: {path}"}
    return {"ok": True, "root": remember_folder(path)}


def remember_folder(path) -> str:
    path = str(Path(path).resolve())
    with _lock:
        cfg = _load(CONFIG_FILE)
        have = [f for f in cfg.get("folders", []) if _norm(f) != _norm(path)]
        cfg["folders"] = have + [path]
        _save(CONFIG_FILE, cfg)
    return path


def testbed(body: dict) -> dict:
    """Create the test workspace, add it to the board and start its test chats."""
    import make_testbed
    root = make_testbed.create()
    remember_folder(root)
    errors = make_testbed.launch(root)
    if errors:
        return {"ok": False, "error": errors[0], "root": str(root)}
    return {"ok": True, "root": str(root), "started": len(make_testbed.TASKS)}


def mark_card(body: dict) -> dict:
    """Store the user's mark on a card: done, archived, priority (0-3)."""
    cid = str(body.get("id") or "")
    if not (SID_RE.match(cid) or re.match(r"^task:[\w-]{1,40}$", cid)):
        return {"ok": False, "error": "Unknown card."}
    with _lock:
        data = _load(CARD_FILE)
        m = dict(data.get(cid) or {})
        if "priority" in body:
            try:
                m["priority"] = max(0, min(3, int(body["priority"])))
            except (TypeError, ValueError):
                return {"ok": False, "error": "Priority must be 0 to 3."}
        for key in ("done", "archived"):
            if key in body:
                m[key] = bool(body[key])
                m["at"] = body.get("at")       # the card's activity time when marked
        data[cid] = m
        _save(CARD_FILE, data)
    return {"ok": True}


def remove_folder(body: dict) -> dict:
    root = str(body.get("id") or "")
    with _lock:
        cfg = _load(CONFIG_FILE)
        cfg["folders"] = [f for f in cfg.get("folders", []) if _norm(f) != _norm(root)]
        _save(CONFIG_FILE, cfg)
    return {"ok": True}


ACTIONS = {"/api/resume": resume, "/api/summarize": summarize, "/api/brief": brief,
           "/api/translate": translate, "/api/open-folder": open_folder,
           "/api/setup": setup_status, "/api/setup/hooks": setup_hooks,
           "/api/setup/mcp": setup_mcp, "/api/setup/rule": setup_rule,
           "/api/setup/register": setup_register, "/api/setup/repair": setup_repair,
           "/api/add-folder": add_folder, "/api/new-chat": new_chat, "/api/card": mark_card, "/api/testbed": testbed, "/api/remove-folder": remove_folder}


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "{}")
        url = urlparse(self.path)
        if url.path == "/":
            html = (HERE / "pult.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN).replace("__VERSION__", VERSION)
            return self._send(200, html, "text/html; charset=utf-8")
        if url.path == "/api/version":
            return self._send(200, json.dumps({"version": VERSION}))
        if url.path == "/api/data":
            q = parse_qs(url.query)

            def num(name, default):
                try:
                    return float(q.get(name, [""])[0])
                except ValueError:
                    return default
            since = num("from", time.time() - 7 * 86400)
            return self._send(200, json.dumps(scan(since, num("to", 0)), ensure_ascii=False))
        self._send(404, "{}")

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "{}")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError:
            body = {}
        if not isinstance(body, dict) or body.get("token") != TOKEN:
            return self._send(403, json.dumps({"ok": False, "error": "Reload the page."}))
        fn = ACTIONS.get(urlparse(self.path).path)
        if not fn:
            return self._send(404, "{}")
        try:
            result = fn(body)
        except Exception as e:     # one failed action must not kill the board
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self._send(200, json.dumps(result, ensure_ascii=False))


def _running_version(url: str) -> str:
    """Version of the Pult already running on the port; '' for an old one."""
    try:
        with urllib.request.urlopen(url + "api/version", timeout=3) as r:
            return json.loads(r.read().decode("utf-8")).get("version", "")
    except (OSError, ValueError):
        return ""


def _stop_old_instance() -> bool:
    """Windows: end the python process that listens on PORT (an older Pult)."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, errors="replace", timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    pids = {line.split()[-1] for line in out.splitlines()
            if f"127.0.0.1:{PORT} " in line and "LISTEN" in line.upper()}
    stopped = False
    for pid in pids:
        try:
            name = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                  capture_output=True, text=True, errors="replace",
                                  timeout=15).stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "python" in name or "py.exe" in name:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=15)
            stopped = True
    return stopped


def _port_busy() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main() -> int:
    url = f"http://127.0.0.1:{PORT}/"
    if _port_busy():
        if _running_version(url) == VERSION:   # this version runs: just open a tab
            webbrowser.open(url)
            return 0
        print("An older Pult is running. Stopping it...")
        if _stop_old_instance():
            for _ in range(25):
                if not _port_busy():
                    break
                time.sleep(0.2)
        if _port_busy():
            print("Could not stop it. Close the other black Pult window, then run "
                  "START.bat again.")
            return 1
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Pult {VERSION} is running: {url}\n"
          "Close this window to stop it.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
