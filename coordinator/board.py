"""Local web board. Pages are formatted from the same dicts the /api routes return;
all writes go through core as the person. No HTML lives here (see web/templates)."""
import getpass
import hmac
import json
import re
import webbrowser
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, unquote, urlsplit

from . import config, core, db, launcher, messages

WEB_DIR = Path(__file__).parent / "web"
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
KINDS = config.KINDS
SOON = timedelta(minutes=30)
MAX_BODY = 1_000_000
CONTENT_TYPES = {".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8"}
UNKNOWN_LABEL = "Unknown state"


def person() -> core.Actor:
    return core.Actor("person", getpass.getuser())


# ------------------------------------------------------------------ rendering
class Raw(str):
    """Already-rendered HTML; everything else is escaped on substitution."""


def render(name: str, /, **values) -> Raw:
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    safe = {k: v if isinstance(v, Raw) else escape("" if v is None else str(v))
            for k, v in values.items()}
    return Raw(Template(text).substitute(safe))


def render_all(name: str, rows, /) -> Raw:
    return Raw("".join(render(name, **r) for r in rows))


def render_options(items, current=None) -> Raw:
    return render_all("_option.html", [
        {"value": v, "label": label, "selected": Raw(" selected" if v == current else "")}
        for v, label in items])


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


def _card(task: dict, live: dict) -> dict:
    claim = live.get(task["id"])
    return {"id": task["id"], "title": task["title"], "mode": task["mode"],
            "state": task["state"], "holder": claim["session_id"] if claim else "",
            "blocked": task["blocked"] or "", "open_points": task["open_points"]}


def api_index(conn) -> dict:
    live = core.live_claims(conn)
    now = datetime.now(timezone.utc)
    projects, needs = [], []
    for p in core.list_projects(conn):
        cfg = _config(p)
        kinds = _kinds(cfg)
        labels = {s.name: s.label for s in cfg.states} if cfg else {}
        counts = dict.fromkeys(KINDS, 0)
        for t in core.list_tasks(conn, p["key"]):
            kind = kinds.get(t["state"], "unknown")
            counts[kind] = counts.get(kind, 0) + 1
            reasons = []
            if kind == "handover":
                reasons.append(f"handover: {labels.get(t['state'], t['state'])}")
            if t["blocked"]:
                reasons.append(f"blocked: {t['blocked']}")
            if t["inbox"]:
                reasons.append("inbox: new task from a chat")
            claim = live.get(t["id"])
            if claim and _lease_end(claim) - now < SOON:
                reasons.append(f"lease ends {_lease_end(claim).isoformat(timespec='seconds')}")
            if reasons:
                needs.append({"id": t["id"], "title": t["title"], "project": p["key"],
                              "reasons": reasons})
        projects.append({"key": p["key"], "name": p["name"], "counts": counts,
                         "counts_text": ", ".join(f"{k} {n}" for k, n in counts.items()
                                                  if n)})
    return {"token": core.board_token(conn),
            "enforcement": core.get_setting(conn, "enforcement", "warn"),
            "projects": projects, "needs_you": needs}


def api_project(conn, key: str) -> dict:
    proj = core.get_project(conn, key)
    cfg = _config(proj)
    live = core.live_claims(conn)
    tasks = core.list_tasks(conn, proj["key"])
    states = [(s.name, s.label) for s in cfg.states] if cfg else []
    known = {n for n, _ in states}
    columns = [{"name": n, "label": label, "cards": [
        _card(t, live) for t in tasks if t["state"] == n and not t["inbox"]]}
        for n, label in states]
    unknown = [_card(t, live) for t in tasks
               if t["state"] not in known and not t["inbox"]]
    if unknown:
        columns.append({"name": "", "label": UNKNOWN_LABEL, "cards": unknown})
    return {"token": core.board_token(conn),
            "project": {"key": proj["key"], "name": proj["name"]},
            "inbox": [_card(t, live) for t in tasks if t["inbox"]],
            "columns": columns}


def api_task(conn, display_id: str) -> dict:
    t = core.get_task(conn, display_id)
    proj = core.get_project(conn, t["project"])
    cfg = _config(proj)
    kinds = _kinds(cfg)
    states = [(s.name, s.label) for s in cfg.states] if cfg else []
    if t["state"] not in kinds:
        states.append((t["state"], t["state"]))
    live = [c for c in t["claims"] if c["ended"] is None]
    for c in t["claims"]:
        c["resume"] = f"claude --resume {c['session_id']}"
    state_label = dict(states).get(t["state"], t["state"])
    fields = [("Title", t["title"]), ("State", state_label), ("Mode", t["mode"]),
              ("Goal", t["goal"]), ("Done when", t["done_when"]), ("Scope", t["scope"]),
              ("Origin", t["origin"]), ("Blocked", t["blocked"] or ""),
              ("Open points", "; ".join(t["open_points"])),
              ("In inbox", "yes" if t["inbox"] else "no"),
              ("Created by", f"{t['created_by_type']} {t['created_by_id']}"),
              ("Created", t["created"]), ("Updated", t["updated"]),
              ("Version", t["version"])]
    return {"token": core.board_token(conn), "task": t,
            "fields": [{"label": a, "value": b} for a, b in fields],
            "states": [{"name": n, "label": label} for n, label in states],
            "modes": cfg.modes if cfg else [t["mode"]],
            "live_claim": live[0]["session_id"] if live else None,
            "can_start": kinds.get(t["state"]) == "waiting" and not live,
            "can_pin": t["inbox"], "can_force_release": bool(live)}


# ------------------------------------------------------------------ pages
def _token_field(token) -> Raw:
    return render("_token.html", token=token)


def _page(template, /, title, token="", **values) -> Raw:
    return render(template, title=title, token_field=_token_field(token), **values)


def _cards(cards) -> Raw:
    return render_all("_card.html", [
        {**c, "open_points_html": render_all("_li.html", [{"text": p} for p in c["open_points"]]),
         "holder_text": f"claimed by {c['holder']}" if c["holder"] else "",
         "blocked_text": f"blocked: {c['blocked']}" if c["blocked"] else ""}
        for c in cards])


def page_index(d) -> Raw:
    rows = [{"key": p["key"], "name": p["name"], "counts": p["counts_text"]}
            for p in d["projects"]]
    needs = [{"id": i["id"], "title": i["title"], "reasons": "; ".join(i["reasons"])}
             for i in d["needs_you"]]
    return _page("index.html", "Coordinator", d["token"], enforcement=d["enforcement"],
                 projects=render_all("_project_row.html", rows),
                 needs=render_all("_needs_item.html", needs))


def page_project(d) -> Raw:
    cols = render_all("_column.html", [
        {"label": c["label"], "count": len(c["cards"]), "cards": _cards(c["cards"])}
        for c in d["columns"]])
    p = d["project"]
    return _page("project.html", f"{p['key']} {p['name']}", d["token"], key=p["key"],
                 name=p["name"], inbox=_cards(d["inbox"]), columns=cols)


def page_task(d) -> Raw:
    t = d["task"]
    tid, tok = t["id"], d["token"]
    form = {"id": tid, "token_field": _token_field(tok)}
    start = (render("_start_form.html", mode=t["mode"], **form)
             if d["can_start"] else "")
    release = render("_release_form.html", **form) if d["can_force_release"] else ""
    pin = render("_pin_form.html", **form) if d["can_pin"] else ""
    history = render_all("_event_row.html", t["history"])
    return _page(
        "task.html", f"{tid} {t['title']}", tok, id=tid, title_text=t["title"],
        fields=render_all("_field_row.html", d["fields"]),
        state_options=render_options([(s["name"], s["label"]) for s in d["states"]],
                                     t["state"]),
        mode_options=render_options([(m, m) for m in d["modes"]], t["mode"]),
        kind_options=render_options([(k, k) for k in core.COMMENT_KINDS], "comment"),
        start_form=start, release_form=release, pin_form=pin, history=history,
        comments=render_all("_comment_item.html", t["comments"]),
        claims=render_all("_claim_row.html", t["claims"]))


def page_error(error, back="/") -> Raw:
    return render("error.html", title="Not done", text=str(error), back=back)


# ------------------------------------------------------------------ actions
def _f(form, name) -> str:
    return form.get(name, [""])[0]


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
    return core.create_task(conn, _f(f, "project"), _f(f, "title"), person(),
                            goal=_f(f, "goal"), done_when=_f(f, "done_when"),
                            scope=_f(f, "scope"))["id"]


def do_start(conn, f):
    tid = _f(f, "id")
    root = _ensure_startable(conn, tid)
    launcher.start_chat(root, tid)
    return tid


ACTIONS = {"/a/state": do_state, "/a/mode": do_mode, "/a/force-release": do_force_release,
           "/a/pin": do_pin, "/a/comment": do_comment, "/a/create": do_create,
           "/a/start": do_start}
GET_ROUTES = [(re.compile(r"/"), "index"), (re.compile(r"/p/([^/]+)"), "project"),
              (re.compile(r"/t/([^/]+)"), "task"),
              (re.compile(r"/api/index"), "index"),
              (re.compile(r"/api/project/([^/]+)"), "project"),
              (re.compile(r"/api/task/([^/]+)"), "task")]
API = {"index": (api_index, page_index), "project": (api_project, page_project),
       "task": (api_task, page_task)}


class Handler(BaseHTTPRequestHandler):
    server_version = "CoordinatorBoard"

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
            core.expire_leases(conn)
            fetch, page = API[name]
            data = fetch(conn, arg) if arg is not None else fetch(conn)
            if as_json:
                return self._send(200, json.dumps(data, ensure_ascii=False),
                                  "application/json; charset=utf-8")
            self._send(200, page(data))
        except messages.CoordinatorError as e:
            self._send(404, page_error(e))
        finally:
            conn.close()

    def do_POST(self):
        if self.headers.get("Host") not in self._allowed():
            return self._plain(403, "Forbidden host")
        action = ACTIONS.get(urlsplit(self.path).path)
        if action is None:
            return self._plain(404, "Not found")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._plain(413, "Too large")
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"),
                        keep_blank_values=True)
        conn = db.connect()
        try:
            if not (self._origin_ok() and hmac.compare_digest(
                    _f(form, "token").encode(), core.board_token(conn).encode())):
                return self._plain(403, "Forbidden: bad token or origin")
            core.expire_leases(conn)
            try:
                tid = action(conn, form)
            except messages.CoordinatorError as e:
                back = f"/t/{_f(form, 'id')}" if _f(form, "id") else "/"
                return self._send(400, page_error(e, back))
            self._send(303, "", location=f"/t/{tid}")
        finally:
            conn.close()


def make_server(port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


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
