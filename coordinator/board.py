"""Local web board. Pages are formatted from the same dicts the /api routes return;
all writes go through core as the person. No HTML lives here (see web/templates)."""
import errno
import getpass
import hmac
import json
import re
import socket
import sys
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import antson, config, core, db, desk, feedback, launcher, messages, notify, paths, sources, wiki
from .pages import page_error, page_index, page_modes, page_needs, page_project, page_task

WEB_DIR = Path(__file__).parent / "web"
STATIC_DIR = WEB_DIR / "static"
KINDS = config.KINDS
SOON = timedelta(minutes=10)
MAX_BODY = 1_000_000
CONTENT_TYPES = {".css": "text/css; charset=utf-8", ".png": "image/png",
                 ".js": "text/javascript; charset=utf-8"}
UNKNOWN_LABEL = "Unknown state"
FINAL = ("finished", "closed")


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
            "holder_seen": claim["last_seen"] if claim else "",
            "name": core.task_name(task),
            "waiting_click": (claim["waiting_click"] or "") if claim else "",
            "helpers": (claim["helpers"] if claim and core.helpers_running(
                claim, now) else 0),
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
    """The home page: one entry per registered project with the tasks that wait for an answer
    (its on_clarify state), oldest first. Nothing from outside the database is read here."""
    live = core.live_claims(conn)
    now = datetime.now(timezone.utc)
    projects, clarify = [], []
    for p in core.list_projects(conn):
        cfg, cfg_error = _config_and_error(p)
        items, backlog = [], 0
        if cfg:
            kinds = _kinds(cfg)
            labels = {s.name: s.label for s in cfg.states}
            for t in core.list_tasks(conn, p["key"]):
                if kinds.get(t["state"]) == "parked":
                    backlog += 1          # a note: only counted, never listed on home
                    continue
                if t["state"] != cfg.on_clarify:
                    continue
                item = _need(conn, t, kinds.get(t["state"], "unknown"), cfg, labels,
                             live.get(t["id"]), now)
                if item and item["state_role"] == "clarify":
                    items.append({**item, "project_name": p["name"] or p["key"]})
        items.sort(key=lambda i: i["since"])
        chats = sum(1 for tid in live if tid.rsplit("-", 1)[0].upper() == p["key"].upper())
        projects.append({"key": p["key"], "name": p["name"], "config_error": cfg_error,
                         "chats": chats, "clarify": items, "backlog": backlog})
        clarify.extend(items)
    projects.sort(key=lambda p: (-len(p["clarify"]), (p["name"] or p["key"]).lower()))
    clarify.sort(key=lambda i: i["since"])
    return {"token": core.board_token(conn),
            "enforcement": core.get_setting(conn, "enforcement", "warn"),
            "projects": projects, "clarify": clarify}


# BACKLOG(home): the old home page (everything that "needs you": stopped chats, leases, blocked,
# BACKLOG(home): inbox, handovers, review, mode guard, wiki/desk/feedback/learnings cards) was removed
# BACKLOG(home): from the home page on 2026-11-11. The code below is kept, commented out. We must come
# BACKLOG(home): back and build a proper backlog feature from it. Search for BACKLOG(home).
# BACKLOG(home): def api_index(conn) -> dict:
# BACKLOG(home):     live = core.live_claims(conn)
# BACKLOG(home):     now = datetime.now(timezone.utc)
# BACKLOG(home):     projects, needs = [], []
# BACKLOG(home):     for p in core.list_projects(conn):
# BACKLOG(home):         cfg, cfg_error = _config_and_error(p)
# BACKLOG(home):         kinds = _kinds(cfg)
# BACKLOG(home):         labels = {s.name: s.label for s in cfg.states} if cfg else {}
# BACKLOG(home):         counts = dict.fromkeys(KINDS, 0)
# BACKLOG(home):         items = []
# BACKLOG(home):         for t in core.list_tasks(conn, p["key"]):
# BACKLOG(home):             kind = kinds.get(t["state"], "unknown")
# BACKLOG(home):             counts[kind] = counts.get(kind, 0) + 1
# BACKLOG(home):             item = _need(conn, t, kind, cfg, labels, live.get(t["id"]), now)
# BACKLOG(home):             if item:
# BACKLOG(home):                 items.append(item)
# BACKLOG(home):         items.sort(key=lambda i: (TONE_ORDER[i["tone"]], i["since"]))
# BACKLOG(home):         needs.extend(items)
# BACKLOG(home):         tone = ("red" if any(i["tone"] == "red" for i in items)
# BACKLOG(home):                 else "amber" if items or cfg_error else "green")
# BACKLOG(home):         chats = sum(1 for tid in live if tid.rsplit("-", 1)[0].upper() == p["key"].upper())
# BACKLOG(home):         projects.append({"key": p["key"], "name": p["name"], "counts": counts,
# BACKLOG(home):                          "counts_text": ", ".join(f"{k} {n}" for k, n in counts.items()
# BACKLOG(home):                                                   if n),
# BACKLOG(home):                          "tone": tone, "needs_count": len(items), "chats": chats,
# BACKLOG(home):                          "config_error": cfg_error})
# BACKLOG(home):     outside = api_outside(conn, _fresh["now"])
# BACKLOG(home):     by_key = {c["key"].upper(): c for c in outside["cards"] if c["key"]}
# BACKLOG(home):     for p in projects:
# BACKLOG(home):         card = by_key.get(p["key"].upper())
# BACKLOG(home):         p["wiki"] = card
# BACKLOG(home):         if card:
# BACKLOG(home):             p["needs_count"] += card["me_count"]
# BACKLOG(home):             if card["tone"] == "red" or (card["tone"] == "amber" and p["tone"] == "green"):
# BACKLOG(home):                 p["tone"] = card["tone"]
# BACKLOG(home):     projects.sort(key=lambda p: (TONE_ORDER[p["tone"]], -p["needs_count"],
# BACKLOG(home):                                  (p["name"] or p["key"]).lower()))
# BACKLOG(home):     own = [c for c in outside["cards"] if not c["key"] and (c["items"] or c["notes"] or c.get("fixed")
# BACKLOG(home):                                                              or c["tone"] != "green")]
# BACKLOG(home):     own.sort(key=lambda c: (TONE_ORDER[c["tone"]], -c["me_count"], c["name"].lower()))
# BACKLOG(home):     return {"token": core.board_token(conn),
# BACKLOG(home):             "enforcement": core.get_setting(conn, "enforcement", "warn"),
# BACKLOG(home):             "projects": projects, "needs_you": needs, "guard": _guard(conn),
# BACKLOG(home):             "outside": {**outside, "cards": own}}


# ------------------------------------------------------------------ needs from outside (wikis, feedback, desk)
OUTSIDE_TTL = 20                       # seconds: reading all sources takes a few seconds
CHANGE_KINDS = ("change_open", "change_queued")
RED_KINDS = ("failed", "ingest_interrupted")
GROUP_OF = {"client": "others", "antson-expert": "others", "claude": "build"}
UNROUTED = "unrouted"
_outside = {"at": 0.0, "data": None}
_fresh = {"now": False}              # set by the handler for /?fresh=1


def _age_days(since, now):
    return max(0.0, (now - datetime.fromisoformat(since)).total_seconds() / 86400) if since else None


def _without_doubles(items: list) -> list:
    """A desk item that carries a Change id is the one with the action: the project's own entry for that Change is hidden."""
    covered = {(i["project"], i["change"]) for i in items if i["change"] and i["kind"] not in CHANGE_KINDS}
    handed = {i["handover_id"] for i in items if i["handover_id"] and i["kind"].startswith("update_")}
    return [i for i in items if not (i["kind"] in CHANGE_KINDS and (i["project"], i["id"]) in covered)
            and not (i["kind"] == "handover" and i["id"] in handed)]


def _decorate(i: dict, cfg: dict, folder: str, now) -> dict:
    age = _age_days(i["since"], now)
    amber, red = cfg.get("amber_days", 3), cfg.get("red_days", 7)
    group = "build" if i["kind"] == "waiting_publish" else GROUP_OF.get(i["waits_on"], "me")
    level = "none" if age is None else "red" if age > red else "amber" if age > amber else "none"
    return {**i, "age_days": age, "level": level, "group": group,
            "how_to_act": wiki.how_to(i, folder, (cfg.get("desk") or {}).get("cwd", ""))}


def _outside_card(name, key, folder, items, extra) -> dict:
    me = [i for i in items if i["group"] == "me"]
    down = extra.get("down", [])
    red = bool(down) or any(i["level"] == "red" or i["kind"] in RED_KINDS for i in me)
    amber = any(i["level"] == "amber" for i in me) or bool(extra.get("health"))
    notes = [{"text": messages.source_unreadable(d["name"], d["error"]), "level": "red"} for d in down]
    notes += [{"text": f"showing older data of {s['name']} (from {s['at'][:16].replace('T', ' ')})",
               "level": "amber"} for s in extra.get("stale", [])]
    return {"name": name, "key": key, "project_dir": folder or "", "items": items,
            "me_count": len(me), "health": extra.get("health", []), "notes": notes,
            "tone": "red" if red else "amber" if amber else "green", **{k: v for k, v in extra.items()
                                                                          if k in ("fixed", "logo")}}


def _read_outside(coord: list) -> dict:
    cfg = sources.settings()
    empty = {"configured": False, "cards": [], "strip": [], "learnings": None, "warnings": []}
    now = datetime.now().astimezone()
    if cfg.get("registry"):
        registry = wiki.load_registry(cfg["registry"])
    else:                       # no mail desk: the board's own projects that have a state script
        registry = wiki.own_projects(coord)
    if not cfg and not registry:
        return empty
    state_script = cfg.get("state_script", "")
    fb, dk = feedback.spec(cfg.get("feedback") or {}), desk.specs(cfg.get("desk") or {})
    specs = [s for e in registry if e["project_dir"] for s in wiki.specs(e["name"], e["project_dir"], state_script)]
    specs += ([fb] if fb else []) + dk
    res = sources.fetch_all(specs, int(cfg.get("timeout_s", 60)))
    items, extras, warnings = [], {}, []
    folders = {e["name"]: e["project_dir"] for e in registry}
    for e in registry:
        name, folder = e["name"], e["project_dir"]
        if not folder:
            extras[name] = {"down": [{"name": "the project folder", "error": "folder not found"}]}
            continue
        r = wiki.read_project(name, res)
        extras[name] = r
        items += [_decorate(i, cfg, folder, now) for i in r["items"]]
    fb_meta = None
    if fb:
        r = res["feedback"]
        extras[feedback.CARD] = {"fixed": True, "logo": "antson"}
        if r.ok:
            more, fb_meta = feedback.items(r.data)
            items += [_decorate(i, cfg, "", now) for i in more]
            if r.stale:
                extras[feedback.CARD]["stale"] = [{"name": "feedback", "at": r.at.isoformat(), "error": r.error}]
        else:
            extras[feedback.CARD]["down"] = [{"name": "feedback", "error": r.error}]
    if dk:
        r = res["desk"]
        if r.ok:
            items += [_decorate(i, cfg, "", now) for i in desk.items(r.data)]
            if desk.warning(r.data):
                warnings.append(desk.warning(r.data))
            if r.stale:
                warnings.append(messages.source_unreadable("desk", r.error) + f" (showing items from {r.at:%d.%m. %H:%M})")
        else:
            warnings.append(messages.source_unreadable("desk", r.error))
    learn = antson.unprocessed_learnings((cfg.get("learnings") or {}).get("inbox"))
    if learn is not None:
        extras.setdefault(feedback.CARD, {"fixed": True, "logo": "antson"})
    items = _without_doubles(items)
    names = [e["name"] for e in registry]
    for n in sorted({i["project"] for i in items} - set(names) - {feedback.CARD}):
        extras.setdefault(n, {})
        names.append(n)
    if feedback.CARD in extras:
        names.append(feedback.CARD)
    cards = []
    for n in names:
        key = wiki.coordinator_key(folders.get(n), coord)
        cards.append(_outside_card(n, key, folders.get(n), [i for i in items if i["project"] == n], extras.get(n, {})))
    strip = feedback.folder_signals(cfg.get("folders"), now)
    if dk:
        h = res["desk_health"]
        nxt = (h.data.get("next_checks") or [None])[0] if h.ok and isinstance(h.data, dict) else None
        try:
            text = "next check " + datetime.fromisoformat(nxt).strftime("%d.%m. %H:%M")
        except (TypeError, ValueError):
            text = "not reachable" if not h.ok else "mode " + str((h.data or {}).get("mode", "unknown"))
        strip.insert(0, {"name": "Mail desk", "text": text})
    if fb_meta is not None:
        strip.insert(0, {"name": "Feedback digest", "digest": fb_meta.get("schedule_last_run")})
    return {"configured": True, "cards": cards, "strip": strip, "learnings": learn, "warnings": warnings,
            "generated": now.isoformat(timespec="seconds")}


def api_outside(conn, fresh: bool = False) -> dict:
    """What waits for Daria outside the task database, read live. One short in-memory
    cache so a page load does not start every source each time; nothing is written to disk."""
    import threading
    import time
    stale = time.monotonic() - _outside["at"] > OUTSIDE_TTL
    if fresh or _outside["data"] is None:           # first load or "Refresh": wait for the answer
        _outside["data"] = _read_outside(core.list_projects(conn))
        _outside["at"] = time.monotonic()
    elif stale and not _outside.get("busy"):         # going back to the page: show the last answer now,
        _outside["busy"] = True                      # read again in the background for the next load
        coord = core.list_projects(conn)

        def reload():
            try:
                _outside["data"] = _read_outside(coord)
                _outside["at"] = time.monotonic()
            except Exception:
                pass                                 # keep the older answer
            finally:
                _outside["busy"] = False
        threading.Thread(target=reload, daemon=True).start()
    return _outside["data"]


def api_needs(conn, name: str) -> dict:
    out = api_outside(conn)
    card = next((c for c in out["cards"] if c["name"] == name), None)
    if card is None:
        raise messages.unknown_project(name)
    return {"token": core.board_token(conn), "card": card, "learnings": out["learnings"] if card.get("fixed") else None}


# ------------------------------------------------------------------ modes (Antson guard)
def _chat(conn, session: str) -> dict:
    """The chat behind a guard session id, with its task if the coordinator knows it."""
    t = core.session_task(conn, session) or {}
    return {"session": session, "task": t.get("task", ""), "title": t.get("title", "")}


def _guard(conn) -> dict:
    """What the guard needs from Daria, for "Needs you": the mode file changed outside
    the switch, a chat waiting for her go, a refused ready check. `new` counts items
    chats opened or created that have no mode of her own yet."""
    needs = []
    if antson.hash_ok() is False:
        needs.append({"kind": "hash", "tone": "red"})
    for w in antson.go_waiting():
        needs.append({**w, "tone": "amber" if w["kind"] == "go" else "red",
                      "chat": _chat(conn, w["session"])})
    return {"needs": needs,
            "new": sum(r["new"] for g in _mode_groups(conn) for r in g["rows"])}


def _mode_groups(conn) -> list:
    """Modes per project: [{"key", "name", "rows": [...]}]. A row is one form, sub-form
    or interface from modes.json or from a chat's report. Rows are "new" when only a
    report knows them, or when a chat registered them and Daria has not set them."""
    items, reports = antson.load_modes(), antson.read_reports()
    coord = {p["key"].upper(): p for p in core.list_projects(conn)}

    def project_of(rid, it, rep):
        if it.get("project"):
            return str(it["project"])
        parent = str(it.get("parent") or rep.get("parent") or "")
        if parent and items.get(parent, {}).get("project"):
            return str(items[parent]["project"])
        sid = rep.get("session") or str(it.get("set_by") or "").removeprefix("chat:")
        t = core.session_task(conn, sid) if sid else None
        return t["project"] if t else ""

    rows = {}
    for rid in list(items) + [r for r in reports if r not in items]:
        it, rep = items.get(rid, {}), reports.get(rid, {})
        kind = it.get("kind") if it.get("kind") not in (None, "unknown") else rep.get("kind")
        name = it.get("name") if it.get("name") not in (None, "", rid) else rep.get("name")
        parent = it.get("parent") or rep.get("parent")
        mode, inherited = antson.effective(rid, items)
        rows[rid] = {"id": rid, "kind": kind or "unknown", "name": name or "",
                     "parent": str(parent) if parent else "", "mode": mode,
                     "inherited": inherited,
                     "new": not it or bool(it.get("new")
                                           and it.get("set_by") not in ("daria", "board")),
                     "project": project_of(rid, it, rep)}
    groups = {}
    for r in rows.values():
        groups.setdefault(r["project"].upper(), {"key": r["project"], "rows": []})["rows"].append(r)
    out = []
    for gk, g in sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0])):
        p = coord.get(gk)
        out.append({"key": g["key"], "name": (p["name"] or p["key"]) if p else g["key"],
                    "rows": _tree(g["rows"])})
    return out


def _tree(rows: list) -> list:
    """Forms first, each followed by what belongs to it (depth 1, 2, ...)."""
    def num(r):
        return (0, int(r["id"]), "") if r["id"].isdigit() else (1, 0, r["id"])
    ids = {r["id"] for r in rows}
    kids = {}
    for r in sorted(rows, key=num):
        top = r["parent"] if r["parent"] in ids and r["parent"] != r["id"] else ""
        kids.setdefault(top, []).append(r)
    out, seen = [], set()

    def walk(parent, depth):
        for r in kids.get(parent, []):
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append({**r, "depth": depth})
                walk(r["id"], depth + 1)
    walk("", 0)
    out.extend({**r, "depth": 0} for r in rows if r["id"] not in seen)   # a parent cycle
    return out


def api_modes(conn) -> dict:
    job = antson.current_job()
    return {"token": core.board_token(conn), "groups": _mode_groups(conn),
            "mode_py": antson.mode_py_path(),
            "mode_py_ok": Path(antson.mode_py_path()).is_file(),
            "hash_ok": antson.hash_ok(),
            "job": ({"state": job.state, "ids": job.ids, "mode": job.mode,
                     "started": job.started, "changed": job.changed,
                     "refused": job.refused, "reason": job.reason, "error": job.error}
                    if job else None)}


def _check_and_done(cfg) -> tuple[str, str]:
    """The "To check" state (where chats hand finished work: on_done, if it is a handover)
    and the first finished state the person closes it to. ("", "") when there is no such pair."""
    if not cfg:
        return "", ""
    kinds = _kinds(cfg)
    done = next((s.name for s in cfg.states if s.kind == "finished"), "")
    check = cfg.on_done if kinds.get(cfg.on_done) == "handover" else ""
    return (check, done) if check and done else ("", "")


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
        for n, label, kind in states if kind != "parked"]   # parked tasks go to the backlog fold
    parked = {n for n, _, kind in states if kind == "parked"}
    notes = sorted((t for t in tasks if t["state"] in parked),
                   key=lambda t: (t["created"], t["number"]), reverse=True)
    backlog = [{**cards[t["id"]], "created": t["created"],
                "created_by_type": t["created_by_type"], "created_by_id": t["created_by_id"]}
               for t in notes]
    unknown = [cards[t["id"]] for t in tasks
               if t["state"] not in known and not t["inbox"]]
    if unknown:
        columns.append({"name": "", "label": UNKNOWN_LABEL, "kind": "unknown",
                        "cards": unknown})
    inbox = [{**cards[t["id"]], "created": t["created"],
              "created_by_type": t["created_by_type"], "created_by_id": t["created_by_id"]}
             for t in tasks if t["inbox"]]
    check, done = _check_and_done(cfg)
    return {"token": core.board_token(conn),
            "check_state": check, "done_state": done,
            "project": {"key": proj["key"], "name": proj["name"],
                        "config_error": cfg_error},
            "projects": [{"key": p["key"], "name": p["name"]}
                         for p in core.list_projects(conn)],
            "modes": cfg.modes if cfg else [],
            "default_mode": cfg.default_mode if cfg else "",
            "inbox": inbox, "backlog": backlog, "columns": columns}


def api_pulse(conn) -> dict:
    """A tiny fingerprint of what the pages show: it changes with every write (each write
    leaves an event row). The pages poll it and reload only when it changed."""
    return {"stamp": conn.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0]}


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
    claim = core.live_claims(conn).get(t["id"])
    now_utc = datetime.now(timezone.utc)
    reports = [{**c, "name": core.task_name(t, c["role"])}
               for c in t["comments"] if c["role"]]
    state_label = next(s["label"] for s in states if s["name"] == t["state"])
    fields = [("Title", t["title"]), ("State", state_label), ("Mode", t["mode"]),
              ("Goal", t["goal"]), ("Done when", t["done_when"]), ("Scope", t["scope"]),
              ("Origin", t["origin"]), ("Blocked", t["blocked"] or ""),
              ("Open points", "; ".join(t["open_points"])),
              ("Forms", ", ".join(t["forms"])),
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
            "name": core.task_name(t), "reports": reports,
            "waiting_click": (claim["waiting_click"] or "") if claim else "",
            "helpers": (claim["helpers"] if claim and core.helpers_running(claim, now_utc)
                        else 0),
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


def do_done(conn, f):
    """The Done button on a To check card: the person closes it in one step, through core."""
    tid = _f(f, "id")
    t = core.get_task(conn, tid)
    check, done = _check_and_done(_config(core.get_project(conn, t["project"])))
    if not check:
        raise messages.cannot_close(tid, "The project has no To check state to close from.")
    if t["state"] != check:
        raise messages.cannot_close(
            tid, f"Only a task in {check!r} can be closed this way; it is in {t['state']!r}.")
    core.set_state(conn, tid, done, person(), expected_state=check)
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


def do_modes(conn, f):
    """Start `mode.py set` in the background (one row button, or the selected rows).
    The board never writes modes.json and never answers the confirmation window."""
    row = _f(f, "row")
    if row:
        rid, _, mode = row.partition("|")
        ids = [rid]
    else:
        ids, mode = f.get("ids", []), _f(f, "mode")
    if not ids:
        raise messages.modes_nothing_selected()
    try:
        antson.start_set(ids, mode)
    except ValueError:
        raise messages.modes_bad_request() from None
    except RuntimeError:
        raise messages.modes_busy() from None
    return ""


def do_make_task(conn, f):
    """One "Needs you" item becomes one task of its project. Several items into one
    task is not offered (grouping is decided in the project)."""
    name, key = _f(f, "name"), _f(f, "key")
    if not key:
        raise messages.make_task_no_project(name)
    origin = f"needs you: {name} {_f(f, 'kind')} {_f(f, 'item')}".strip()
    kinds = _kinds(_config(core.get_project(conn, key)))
    for t in core.list_tasks(conn, key):
        if t["origin"] == origin and kinds.get(t["state"], "unknown") not in FINAL:
            raise messages.make_task_exists(name, t["id"])
    t = core.create_task(conn, key, _f(f, "title"), person(), goal=_f(f, "goal"), origin=origin)
    return t["id"]


def do_start(conn, f):
    tid = _f(f, "id")
    root = _ensure_startable(conn, tid)
    try:
        launcher.open_folder(root)
    except OSError as e:
        raise messages.cannot_start(tid, f"VS Code could not open {root}: {e}") from None
    core.leave_start_note(conn, tid, person())
    return tid


ACTIONS = {"/a/state": do_state, "/a/done": do_done, "/a/mode": do_mode, "/a/force-release": do_force_release,
           "/a/pin": do_pin, "/a/comment": do_comment, "/a/create": do_create,
           "/a/start": do_start, "/a/modes": do_modes,
           "/a/make-task": do_make_task}
GET_ROUTES = [(re.compile(r"/"), "index"), (re.compile(r"/p/([^/]+)"), "project"),
              (re.compile(r"/p/([^/]+)/all"), "project_all"),
              (re.compile(r"/t/([^/]+)"), "task"),
              (re.compile(r"/needs/([^/]+)"), "needs"),
              (re.compile(r"/modes"), "modes"), (re.compile(r"/api/modes"), "modes"),
              (re.compile(r"/api/index"), "index"),
              (re.compile(r"/api/pulse"), "pulse"),
              (re.compile(r"/api/project/([^/]+)"), "project"),
              (re.compile(r"/api/task/([^/]+)"), "task")]
API = {"index": (api_index, page_index), "project": (api_project, page_project),
       "project_all": (api_project, lambda d: page_project(d, older=True)),
       "task": (api_task, page_task), "modes": (api_modes, page_modes),
       "needs": (api_needs, page_needs), "pulse": (api_pulse, None)}


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
        _fresh["now"] = "fresh=1" in urlsplit(self.path).query
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


class _BoardServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # in use; ask for exclusive use there so a second board is refused.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    try:
        return _BoardServer((host, port), Handler)
    except OSError as e:
        if port and (e.errno in (errno.EADDRINUSE, errno.EACCES)
                     or getattr(e, "winerror", None) in (10048, 10013)):
            raise messages.board_port_taken(port) from e
        raise


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
