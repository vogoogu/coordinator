"""Local web board. Pages are formatted from the same dicts the /api routes return;
all writes go through core as the person. No HTML lives here (see web/templates)."""
import getpass
import hmac
import json
import re
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, unquote, urlsplit

from . import config, core, db, launcher, messages, paths

WEB_DIR = Path(__file__).parent / "web"
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
ICON_DIR = WEB_DIR / "icons"
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
        item["note"] = full["comments"][-1]["text"] if full["comments"] else ""
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


def icon(name: str, size: str = "sm") -> Raw:
    """Inline stroke icon from web/icons (decorative: the words next to it carry the meaning)."""
    shape = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8").strip()
    cls = f"ic {size}".strip()
    return Raw(f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true">{shape}</svg>')


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
INDEX_ROWS = 5            # "Needs you" rows per project card; the rest are on the board
TINTS = 5                 # monogram tints m1..m5 in board.css


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).astimezone()


def _span(delta: timedelta) -> str:
    """A length of time in words: 12 min, 3 h, 1 day, 9 days."""
    mins = max(int(delta.total_seconds() // 60), 0)
    if mins < 60:
        return f"{max(mins, 1)} min"
    if mins < 24 * 60:
        return f"{mins // 60} h"
    days = mins // (24 * 60)
    return "1 day" if days == 1 else f"{days} days"


def _ago(stamp: str, now: datetime) -> str:
    delta = now - _when(stamp)
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(days=1):
        return f"{_span(delta)} ago"
    return "yesterday" if delta < timedelta(days=2) else f"{_span(delta)} ago"


def _age(stamp: str, now: datetime) -> str:
    """How long an item has waited, by calendar day: today, 1 day, 6 days."""
    days = (now.date() - _when(stamp).date()).days
    return "today" if days <= 0 else ("1 day" if days == 1 else f"{days} days")


def _short(session_id: str) -> str:
    return (session_id or "")[:4]


def monogram(name: str) -> str:
    """Two-letter mark until projects have logos: initials of the first two words,
    or the first two letters of a one-word name."""
    words = re.findall(r"[^\W_]+", name)
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (words[0][:2] if words else "?").upper()


def _tint(key: str) -> str:
    return f"m{sum(map(ord, key.upper())) % TINTS + 1}"


NEED_LOOK = {  # why -> (pill class, icon, label); the handover label is the state's own
    "stopped": ("red", "stopped", "Chat stopped"),
    "lease": ("amb", "lease", "Lease ending"),
    "handover": ("k-hand", "handover", ""),
    "blocked": ("amb", "blocked", "Blocked"),
    "inbox": ("k-wait", "inbox", "New in inbox"),
}
ROLE_ICON = {"clarify": "question", "done": "eye"}
PROJECT_LOOK = {"red": ("red", "act-now", "Act now"), "amber": ("amb", "check-tri", "Check"),
                "green": ("grn", "ok", "OK")}


def _need_detail(i: dict, now: datetime) -> str:
    why = i["why"]
    if why == "lease":
        silent = _span(now - _when(i["last_seen"]))
        left = _when(i["lease_end"]) - now
        end = f"Lease ends in {_span(left)}." if left > timedelta(0) else "Lease has run out."
        return f"Chat {_short(i['session'])} silent for {silent}. {end}"
    if why == "stopped":
        if i["last_activity"]:
            what = " ".join(x for x in (i["last_tool"], Path(i["last_path"]).name
                                        if i["last_path"] else "") if x)
            return (f"Last activity {_ago(i['last_activity'], now)}"
                    + (f": {what}" if what else ""))
        return f"Chat {_short(i['session'])} stopped without a report."
    if why == "handover":
        return i["note"] or f"Waiting in {i['state_label']}."
    if why == "blocked":
        return i["blocked"]
    who = (f"chat {_short(i['created_by_id'])}" if i["created_by_type"] == "chat"
           else i["created_by_id"])
    return f"From {who}, {_ago(i['created'], now)}"


def _need_row(i: dict, token: str, now: datetime) -> Raw:
    pill, icon_name, label = NEED_LOOK[i["why"]]
    if i["why"] == "handover":
        icon_name, label = ROLE_ICON.get(i["state_role"], icon_name), i["state_label"]
    pin = (render("_pin_button.html", id=i["id"], token_field=_token_field(token),
                  icon_pin=icon("pin")) if i["inbox"] else "")
    return render("_need_row.html", tone=pill, icon=icon(icon_name), label=label,
                  age=_age(i["since"], now), pin=pin, id=i["id"], title=i["title"],
                  detail=_need_detail(i, now))


def _project_card(p: dict, items: list, token: str, now: datetime) -> Raw:
    tone, status_icon, status = PROJECT_LOOK[p["tone"]]
    rows = [_need_row(i, token, now) for i in items[:INDEX_ROWS]]
    if len(items) > INDEX_ROWS:
        rows.append(render("_card_more.html", key=p["key"],
                           text=f"{len(items) - INDEX_ROWS} more on the board"))
    if p["config_error"]:
        rows.insert(0, render("_card_config_error.html", icon=icon("config-error", ""),
                              error=p["config_error"]))
    body = (Raw("<div>" + "".join(rows) + "</div>") if rows
            else render("_card_quiet.html", icon=icon("circle-check", "")))
    n = p["chats"]
    chats = render("_chats.html", muted="" if n else "muted",
                   live=Raw('<span class="live" aria-hidden="true"></span>' if n else ""),
                   text=f"{n} chat{'s' if n != 1 else ''} working" if n
                   else "No chat working")
    count = p["needs_count"]
    return render("_project_card.html", tone=f"{tone}", key=p["key"],
                  name=p["name"] or p["key"], monogram=monogram(p["name"] or p["key"]),
                  tint=_tint(p["key"]), status_icon=icon(status_icon), status=status,
                  count=count, count_class="" if count else "zero",
                  count_word="needs you" if count == 1 else "need you",
                  body=body, chats=chats, icon_chevron=icon("chevron-right"))


def page_index(d) -> Raw:
    now = datetime.now().astimezone()
    projects, needs = d["projects"], d["needs_you"]
    by_project = {}
    for i in needs:
        by_project.setdefault(i["project"], []).append(i)
    cards = Raw("".join(_project_card(p, by_project.get(p["key"], []), d["token"], now)
                        for p in projects))
    total, n = len(needs), len(projects)
    if not projects:
        summary = render("_index_empty.html", icon=icon("folder", "lg"))
    elif not total:
        quiet = "Your project is quiet." if n == 1 else f"All {n} projects are quiet."
        summary = render("_index_quiet.html", icon=icon("circle-check", "lg"), text=quiet)
    else:
        busy = sum(1 for p in projects if p["needs_count"])
        summary = render("_index_heading.html",
                         headline="1 thing needs you" if total == 1
                         else f"{total} things need you",
                         where=f"in {busy} of {n} project{'s' if n != 1 else ''}")
    return _page("index.html", "Coordinator", d["token"],
                 date=f"{WEEKDAYS[now.weekday()]}, {now:%d.%m.%Y}",
                 updated=f"{now:%H:%M}", icon_refresh=icon("refresh", ""),
                 summary=summary, cards=cards)


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
    return core.create_task(conn, _f(f, "project"), _f(f, "title"), person(),
                            goal=_f(f, "goal"), done_when=_f(f, "done_when"),
                            scope=_f(f, "scope"))["id"]


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
              (re.compile(r"/t/([^/]+)"), "task"),
              (re.compile(r"/api/index"), "index"),
              (re.compile(r"/api/project/([^/]+)"), "project"),
              (re.compile(r"/api/task/([^/]+)"), "task")]
API = {"index": (api_index, page_index), "project": (api_project, page_project),
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
            core.expire_leases(conn)
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
            core.expire_leases(conn)
            try:
                tid = action(conn, form)
            except messages.CoordinatorError as e:
                back = f"/t/{_f(form, 'id')}" if _f(form, "id") else "/"
                return self._send(400, page_error(e, back))
            self._send(303, "", location=_next(form) or f"/t/{tid}")
        except Exception:
            self._internal_error()
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
