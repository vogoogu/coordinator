"""Local web board. Pages are formatted from the same dicts the /api routes return;
all writes go through core as the person. No HTML lives here (see web/templates)."""
import getpass
import hmac
import json
import re
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import config, core, db, launcher, messages, notify, paths
from .pages import page_error, page_index, page_project, page_task

WEB_DIR = Path(__file__).parent / "web"
STATIC_DIR = WEB_DIR / "static"
KINDS = config.KINDS
SOON = timedelta(minutes=30)
MAX_BODY = 1_000_000
CONTENT_TYPES = {".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8"}
UNKNOWN_LABEL = "Unknown state"


def person() -> core.Actor:
    return core.Actor("person", getpass.getuser())


# ------------------------------------------------------------------ data (the API)
def _config(project: dict):
    try:
        return config.load_project(project["root"])
    except config.ConfigError:
        return None


def _kinds(cfg) -> dict:
    return {s.name: s.kind for s in cfg.states} if cfg else {}


def _lease_end(claim) -> datetime:
    return (datetime.fromisoformat(claim["last_seen"])
            + timedelta(hours=core.LEASE_HOURS))


def _card(conn, task: dict, live: dict, cfg, kinds: dict, labels: dict, now) -> dict:
    claim = live.get(task["id"])
    kind = kinds.get(task["state"], "unknown")
    need = _need(conn, task, kind, cfg, labels, claim, now)
    return {"id": task["id"], "title": task["title"], "mode": task["mode"],
            "state": task["state"], "kind": kind,
            "holder": claim["session_id"] if claim else "",
            "holder_since": claim["started"] if claim else "",
            "blocked": task["blocked"] or "", "open_points": task["open_points"],
            "since": task["updated"], "why": need["why"] if need else "",
            "note": (need or {}).get("note", ""), "note_kind": (need or {}).get("note_kind", "")}


# Why a task needs the person, in the order the most urgent reason wins.
# "red" reasons make the whole project card red ("act now"); the rest amber.
WHY_ORDER = ("stopped", "lease", "handover", "blocked", "inbox")
URGENT = {"stopped", "lease"}
STOPPED_REASONS = {"expired", "no_report"}   # claim end reasons: chat left without a report
TONE_ORDER = {"red": 0, "amber": 1, "green": 2}


def _config_and_error(project: dict):
    try:
        return config.load_project(project["root"]), ""
    except config.ConfigError as e:
        return None, str(e)


def _need(conn, t, kind, cfg, labels, claim, now) -> dict | None:
    """The "Needs you" entry for one task, or None. `why` is the most urgent reason;
    the facts after it are what the index shows in the item's detail line."""
    reasons, whys = [], set()
    item = {"id": t["id"], "title": t["title"], "project": t["project"],
            "since": t["updated"], "state": t["state"],
            "state_label": labels.get(t["state"], t["state"]), "state_role": "",
            "inbox": t["inbox"]}
    if kind == "handover":
        full = core.get_task(conn, t["id"])
        ended = [c for c in full["claims"] if c["ended"]]
        last = ended[-1] if ended else None
        if claim is None and last and last["end_reason"] in STOPPED_REASONS:
            whys.add("stopped")
            reasons.append("chat stopped without report")
            stat = core.get_session_stat(conn, last["session_id"]) or {}
            item.update(session=last["session_id"],
                        last_activity=stat.get("last_activity") or "",
                        last_tool=stat.get("last_tool") or "",
                        last_path=stat.get("last_path") or "")
        whys.add("handover")
        reasons.append(f"handover: {item['state_label']}")
        last_comment = full["comments"][-1] if full["comments"] else {}
        item["note"] = last_comment.get("text", "")
        item["note_kind"] = last_comment.get("kind", "")
        if cfg and t["state"] == cfg.on_clarify:
            item["state_role"] = "clarify"
        elif cfg and t["state"] == cfg.on_done:
            item["state_role"] = "done"
    if t["blocked"]:
        whys.add("blocked")
        reasons.append(f"blocked: {t['blocked']}")
        item["blocked"] = t["blocked"]
    if t["inbox"]:
        whys.add("inbox")
        reasons.append("inbox: new task from a chat")
        item.update(created=t["created"], created_by_type=t["created_by_type"],
                    created_by_id=t["created_by_id"])
    if claim and _lease_end(claim) - now < SOON:
        whys.add("lease")
        reasons.append(f"lease ends {_lease_end(claim).isoformat(timespec='seconds')}")
        item.update(session=claim["session_id"], last_seen=claim["last_seen"],
                    lease_end=_lease_end(claim).isoformat(timespec="seconds"))
    if not whys:
        return None
    why = next(w for w in WHY_ORDER if w in whys)
    return {**item, "why": why, "tone": "red" if why in URGENT else "amber",
            "reasons": reasons}


def api_index(conn) -> dict:
    live = core.live_claims(conn)
    now = datetime.now(timezone.utc)
    projects, needs = [], []
    for p in core.list_projects(conn):
        cfg, cfg_error = _config_and_error(p)
        kinds = _kinds(cfg)
        labels = {s.name: s.label for s in cfg.states} if cfg else {}
        counts = dict.fromkeys(KINDS, 0)
        items = []
        for t in core.list_tasks(conn, p["key"]):
            kind = kinds.get(t["state"], "unknown")
            counts[kind] = counts.get(kind, 0) + 1
            item = _need(conn, t, kind, cfg, labels, live.get(t["id"]), now)
            if item:
                items.append(item)
        items.sort(key=lambda i: (TONE_ORDER[i["tone"]], i["since"]))
        needs.extend(items)
        tone = ("red" if any(i["tone"] == "red" for i in items)
                else "amber" if items or cfg_error else "green")
        chats = sum(1 for tid in live if tid.rsplit("-", 1)[0].upper() == p["key"].upper())
        projects.append({"key": p["key"], "name": p["name"], "counts": counts,
                         "counts_text": ", ".join(f"{k} {n}" for k, n in counts.items()
                                                  if n),
                         "tone": tone, "needs_count": len(items), "chats": chats,
                         "config_error": cfg_error})
    projects.sort(key=lambda p: (TONE_ORDER[p["tone"]], -p["needs_count"],
                                 (p["name"] or p["key"]).lower()))
    return {"token": core.board_token(conn),
            "enforcement": core.get_setting(conn, "enforcement", "warn"),
            "projects": projects, "needs_you": needs}


def api_project(conn, key: str) -> dict:
    proj = core.get_project(conn, key)
    cfg, cfg_error = _config_and_error(proj)
    live = core.live_claims(conn)
    now = datetime.now(timezone.utc)
    tasks = core.list_tasks(conn, proj["key"])
    kinds = _kinds(cfg)
    labels = {s.name: s.label for s in cfg.states} if cfg else {}
    cards = {t["id"]: _card(conn, t, live, cfg, kinds, labels, now) for t in tasks}
    states = [(s.name, s.label, s.kind) for s in cfg.states] if cfg else []
    known = {n for n, _, _ in states}
    columns = [{"name": n, "label": label, "kind": kind, "cards": [
        cards[t["id"]] for t in tasks if t["state"] == n and not t["inbox"]]}
        for n, label, kind in states]
    unknown = [cards[t["id"]] for t in tasks
               if t["state"] not in known and not t["inbox"]]
    if unknown:
        columns.append({"name": "", "label": UNKNOWN_LABEL, "kind": "unknown",
                        "cards": unknown})
    inbox = [{**cards[t["id"]], "created": t["created"],
              "created_by_type": t["created_by_type"], "created_by_id": t["created_by_id"]}
             for t in tasks if t["inbox"]]
    return {"token": core.board_token(conn),
            "project": {"key": proj["key"], "name": proj["name"],
                        "config_error": cfg_error},
            "projects": [{"key": p["key"], "name": p["name"]}
                         for p in core.list_projects(conn)],
            "modes": cfg.modes if cfg else [],
            "default_mode": cfg.default_mode if cfg else "",
            "inbox": inbox, "columns": columns}


def api_task(conn, display_id: str) -> dict:
    t = core.get_task(conn, display_id)
    proj = core.get_project(conn, t["project"])
    cfg = _config(proj)
    kinds = _kinds(cfg)
    labels = {s.name: s.label for s in cfg.states} if cfg else {}
    states = ([{"name": s.name, "label": s.label, "kind": s.kind,
                "requires": list(s.requires)} for s in cfg.states] if cfg else [])
    if t["state"] not in kinds:
        states.append({"name": t["state"], "label": t["state"], "kind": "unknown",
                       "requires": []})
    live = [c for c in t["claims"] if c["ended"] is None]
    for c in t["claims"]:
        c["resume"] = f"claude --resume {c['session_id']}"
    state_label = next(s["label"] for s in states if s["name"] == t["state"])
    fields = [("Title", t["title"]), ("State", state_label), ("Mode", t["mode"]),
              ("Goal", t["goal"]), ("Done when", t["done_when"]), ("Scope", t["scope"]),
              ("Origin", t["origin"]), ("Blocked", t["blocked"] or ""),
              ("Open points", "; ".join(t["open_points"])),
              ("In inbox", "yes" if t["inbox"] else "no"),
              ("Created by", f"{t['created_by_type']} {t['created_by_id']}"),
              ("Created", t["created"]), ("Updated", t["updated"]),
              ("Version", t["version"])]
    kind = kinds.get(t["state"], "unknown")
    need = _need(conn, t, kind, cfg, labels, live[0] if live else None,
                 datetime.now(timezone.utc))
    return {"token": core.board_token(conn), "task": t,
            "project": {"key": proj["key"], "name": proj["name"]},
            "kind": kind, "state_label": state_label, "need": need,
            "fields": [{"label": a, "value": b} for a, b in fields],
            "states": states,
            "modes": cfg.modes if cfg else [t["mode"]],
            "live_claim": live[0]["session_id"] if live else None,
            "live_since": live[0]["started"] if live else None,
            "lease_end": (_lease_end(live[0]).isoformat(timespec="seconds")
                          if live else None),
            "can_start": kind == "waiting" and not live,
            "can_pin": t["inbox"], "can_force_release": bool(live)}


# ------------------------------------------------------------------ actions
def _f(form, name) -> str:
    return form.get(name, [""])[0]


def _next(form) -> str:
    """Where to go after an action: a local path from the form, or "" for the task page."""
    nxt = _f(form, "next")
    return nxt if re.fullmatch(r"/[A-Za-z0-9/_.-]*", nxt) and not nxt.startswith("//") else ""


def _ensure_startable(conn, display_id):
    d = api_task(conn, display_id)
    if d["live_claim"]:
        raise messages.cannot_start(display_id, f"{d['live_claim']} already holds it.")
    if not d["can_start"]:
        raise messages.cannot_start(display_id, "Its state is not a waiting state.")
    return core.get_project(conn, d["task"]["project"])["root"]


def do_state(conn, f):
    tid = _f(f, "id")
    core.set_state(conn, tid, _f(f, "state"), person(), note=_f(f, "note"))
    return tid


def do_mode(conn, f):
    core.set_mode(conn, _f(f, "id"), _f(f, "mode"), person())
    return _f(f, "id")


def do_force_release(conn, f):
    core.force_release(conn, _f(f, "id"), person())
    return _f(f, "id")


def do_pin(conn, f):
    core.pin(conn, _f(f, "id"), person())
    return _f(f, "id")


def do_comment(conn, f):
    core.comment(conn, _f(f, "id"), person(), _f(f, "kind"), _f(f, "text"))
    return _f(f, "id")


def do_create(conn, f):
    t = core.create_task(conn, _f(f, "project"), _f(f, "title"), person(),
                         goal=_f(f, "goal"), done_when=_f(f, "done_when"),
                         scope=_f(f, "scope"))
    if _f(f, "mode") and _f(f, "mode") != t["mode"]:
        core.set_mode(conn, t["id"], _f(f, "mode"), person())
    return t["id"]


def do_start(conn, f):
    tid = _f(f, "id")
    root = _ensure_startable(conn, tid)
    try:
        launcher.start_chat(root, tid)
    except OSError as e:
        raise messages.cannot_start(tid, f"The chat could not be opened in {root}: {e}") from None
    return tid


ACTIONS = {"/a/state": do_state, "/a/mode": do_mode, "/a/force-release": do_force_release,
           "/a/pin": do_pin, "/a/comment": do_comment, "/a/create": do_create,
           "/a/start": do_start}
GET_ROUTES = [(re.compile(r"/"), "index"), (re.compile(r"/p/([^/]+)"), "project"),
              (re.compile(r"/p/([^/]+)/all"), "project_all"),
              (re.compile(r"/t/([^/]+)"), "task"),
              (re.compile(r"/api/index"), "index"),
              (re.compile(r"/api/project/([^/]+)"), "project"),
              (re.compile(r"/api/task/([^/]+)"), "task")]
API = {"index": (api_index, page_index), "project": (api_project, page_project),
       "project_all": (api_project, lambda d: page_project(d, older=True)),
       "task": (api_task, page_task)}


class Handler(BaseHTTPRequestHandler):
    server_version = "CoordinatorBoard"
    # Buffered output: headers and body leave in one send when the request ends.
    # Unbuffered, the body went out as a second small segment that was sometimes
    # lost when the server closed the socket right after it (seen on Windows).
    wbufsize = -1

    def log_message(self, *args):
        pass

    def _allowed(self) -> set:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, status, body, ctype="text/html; charset=utf-8", location=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if ctype.startswith("text/html"):  # no framing: clickjacking of the buttons
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        if self.close_connection:
            self.send_header("Connection", "close")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def _plain(self, status, text):
        self._send(status, text, "text/plain; charset=utf-8")

    def _origin_ok(self) -> bool:
        ref = self.headers.get("Origin") or self.headers.get("Referer")
        if not ref:
            return False
        u = urlsplit(ref)
        return u.scheme == "http" and u.netloc in self._allowed()

    def do_GET(self):
        if self.headers.get("Host") not in self._allowed():
            return self._plain(403, "Forbidden host")
        path = urlsplit(self.path).path
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        for rx, name in GET_ROUTES:
            m = rx.fullmatch(path)
            if m:
                return self._get(name, path.startswith("/api/"),
                                 unquote(m.group(1)) if m.groups() else None)
        self._plain(404, "Not found")

    def _static(self, name):
        f = STATIC_DIR / name
        if "/" in name or "\\" in name or not f.is_file() or f.suffix not in CONTENT_TYPES:
            return self._plain(404, "Not found")
        self._send(200, f.read_bytes(), CONTENT_TYPES[f.suffix])

    def _get(self, name, as_json, arg):
        conn = db.connect()
        try:
            _expire(conn)
            fetch, page = API[name]
            data = fetch(conn, arg) if arg is not None else fetch(conn)
            if as_json:
                return self._send(200, json.dumps(data, ensure_ascii=False),
                                  "application/json; charset=utf-8")
            self._send(200, page(data))
        except messages.CoordinatorError as e:
            self._send(404, page_error(e))
        except Exception:
            self._internal_error()
        finally:
            conn.close()

    def _internal_error(self):
        try:
            with open(paths.app_home() / "board-errors.log", "a", encoding="utf-8") as f:
                f.write(f"{self.command} {self.path}" + chr(10) + traceback.format_exc() + chr(10))
        except Exception:
            pass
        err = messages.CoordinatorError(
            "The board could not show this page.",
            "An unexpected error happened inside the board.",
            "Go back and try again; details are in board-errors.log in the coordinator folder.")
        self._send(500, page_error(err))

    def _post_error(self, status, text, length):
        """Answer a POST with an error. The body is read first (up to MAX_BODY):
        closing a socket with unread data makes Windows reset the connection, and
        the client would see ConnectionResetError instead of the status."""
        self.close_connection = True
        left = min(max(length, 0), MAX_BODY)
        try:
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass
        self._plain(status, text)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if self.headers.get("Host") not in self._allowed():
            return self._post_error(403, "Forbidden host", length)
        action = ACTIONS.get(urlsplit(self.path).path)
        if action is None:
            return self._post_error(404, "Not found", length)
        if length < 0:
            return self._post_error(400, "Bad Content-Length", length)
        if length > MAX_BODY:
            return self._post_error(413, "Too large", length)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"),
                        keep_blank_values=True)
        conn = db.connect()
        try:
            if not (self._origin_ok() and hmac.compare_digest(
                    _f(form, "token").encode(), core.board_token(conn).encode())):
                return self._plain(403, "Forbidden: bad token or origin")
            _expire(conn)
            try:
                tid = action(conn, form)
            except messages.CoordinatorError as e:
                return self._send(400, self._refused_page(conn, form, e))
            self._send(303, "", location=_next(form) or f"/t/{tid}")
        except Exception:
            self._internal_error()
        finally:
            conn.close()


    def _refused_page(self, conn, form, error):
        """A refused action: the task page with the reason next to the action that
        was refused, or the error page when there is no task to show."""
        tid, path = _f(form, "id"), urlsplit(self.path).path
        if tid:
            try:
                return page_task(api_task(conn, tid), error=(path, error))
            except messages.CoordinatorError:
                pass
        return page_error(error, _next(form) or (f"/t/{tid}" if tid else "/"))


def make_server(port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def _expire(conn) -> None:
    """Expire stale claims; pop-ups for what expired."""
    m = notify.mark(conn)
    expired = core.expire_leases(conn)
    if expired:
        notify.fire(conn, m, expired)


def serve(host: str = "127.0.0.1", port: int = 8790, open_browser: bool = True) -> None:
    server = make_server(port, host)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Board running at {url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
