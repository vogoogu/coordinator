"""Board pages: formats the dicts the board API returns into HTML from web/templates.
Wording and look decisions live here and in web/; the data and the actions live in board.py."""
import json
import re
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from string import Template

from . import core

WEB_DIR = Path(__file__).parent / "web"
TEMPLATE_DIR = WEB_DIR / "templates"
ICON_DIR = WEB_DIR / "icons"

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


# ------------------------------------------------------------------ pages
def _token_field(token) -> Raw:
    return render("_token.html", token=token)


def _page(template, /, title, token="", **values) -> Raw:
    return render(template, title=title, token_field=_token_field(token), **values)


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
    "lease": ("red", "lease", "Lease ending"),
    "handover": ("k-hand", "handover", ""),
    "blocked": ("red", "blocked", "Blocked"),
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
                   live=render("_live.html") if n else "",
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




# ------------------------------------------------------------------ shared bits
KIND_LOOK = {  # kind -> (css class, icon, word); every status has an icon and a word
    "waiting": ("k-wait", "k-waiting", "Waiting"),
    "active": ("k-act", "k-active", "Active"),
    "handover": ("k-hand", "handover", "Handover"),
    "finished": ("k-fin", "circle-check", "Finished"),
    "closed": ("k-clo", "k-closed", "Closed"),
    "unknown": ("k-clo", "config-error", "Unknown kind"),
}
FINAL = ("finished", "closed")
RECENT = timedelta(days=7)    # finished and closed columns show this much by default


def _kind(kind: str):
    return KIND_LOOK.get(kind, KIND_LOOK["unknown"])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _date(stamp: str) -> str:
    return f"{_when(stamp):%d.%m.%Y}"


def _clock(stamp: str, now: datetime) -> str:
    """today 13:57, yesterday 10:02, or 05.11.2026 17:05"""
    w = _when(stamp)
    days = (now.date() - w.date()).days
    day = "today" if days == 0 else "yesterday" if days == 1 else f"{w:%d.%m.%Y}"
    return f"{day} {w:%H:%M}"


def _mode_icon(mode: str, modes: list) -> Raw:
    """The first (least) mode reads, the others may change things."""
    return icon("eye" if modes and mode == modes[0] else "pencil")


def _pill(cls: str, icon_name: str, text: str) -> Raw:
    return render("_pill.html", cls=cls, icon=icon(icon_name), text=text)


def _now() -> datetime:
    return datetime.now().astimezone()


def _head(now: datetime) -> dict:
    return {"updated": f"{now:%H:%M}", "icon_refresh": icon("refresh", ""),
            "icon_back": icon("back", "")}


def _from(c: dict, now: datetime) -> str:
    who = (f"chat {_short(c['created_by_id'])}" if c["created_by_type"] == "chat"
           else c["created_by_id"])
    return f"From {who}, {_ago(c['created'], now)}"


# ------------------------------------------------------------------ project board
URGENT_LOOK = {"stopped": ("stopped", "Chat stopped"), "lease": ("lease", "Lease ending")}
STALE_DAYS = 1   # a handover card waiting this many days shows its age in amber


def _task_card(c: dict, modes: list, token: str, here: str, now: datetime) -> Raw:
    """One card. Each fact appears once; the column already names the state."""
    final = c["kind"] in FINAL
    metas = []
    if c["holder"]:
        metas.append(render("_meta.html", title=c["holder"], lead=render("_live.html"),
                            text=f"Chat {_short(c['holder'])} working · "
                                 f"{_span(now - _when(c['holder_since']))}"))
    if c["why"] in URGENT_LOOK and not final:
        metas.append(render("_meta_pill.html", pill=_pill("red pw", *URGENT_LOOK[c["why"]])))
    if c["blocked"] and not final:
        metas.append(render("_meta_pill.html",
                            pill=_pill("red pw", "blocked", f"Blocked: {c['blocked']}")))
    if c.get("note") and c["kind"] == "handover":
        metas.append(render("_card_note.html", text=c["note"],
                            icon=icon("question" if c.get("note_kind") == "question"
                                      else "comment", "sm")))
    urgent = not final and (c["blocked"] or c["why"] in URGENT_LOOK)
    if final:
        top, foot = render("_card_date.html", date=_date(c["since"])), ""
    else:
        top = render("_mode_word.html", icon=_mode_icon(c["mode"], modes), mode=c["mode"])
        age = _age(c["since"], now)
        if c["kind"] == "handover" and (now.date() - _when(c["since"]).date()).days >= STALE_DAYS:
            age = render("_stale.html", icon=icon("lease", "sm"), age=age,
                         title=f"Waiting on you for {age}")
        start = (render("_card_start.html", token_field=_token_field(token), id=c["id"],
                        next=here, icon_play=icon("play"))
                 if c["kind"] == "waiting" and not c["holder"] else "")
        foot = render("_card_foot.html", age=age, start=start)
    extra = (" quiet" if final else "") + (" urgent" if urgent else "")
    return render("_task_card.html", extra=extra, id=c["id"], top=top, title=c["title"],
                  metas=Raw("".join(metas)), foot=foot)


def _column(col: dict, d: dict, here: str, older: bool, now: datetime) -> Raw:
    key = d["project"]["key"]
    cls, icon_name, word = _kind(col["kind"])
    cards = [{**c, "state_label": col["label"]} for c in col["cards"]]
    more = ""
    if col["kind"] in FINAL:
        if older:
            more = render("_more_link.html", href=f"/p/{key}", text="Show last 7 days only")
        else:
            word += " · last 7 days"
            recent = [c for c in cards if now - _when(c["since"]) <= RECENT]
            if len(recent) < len(cards):
                more = render("_more_link.html", href=f"/p/{key}/all",
                              text=f"Show {len(cards) - len(recent)} older")
            cards = recent
    body = Raw("".join(_task_card(c, d["modes"], d["token"], here, now) for c in cards))
    return render("_column.html", label=col["label"], kind_class=cls,
                  kind_icon=icon(icon_name), kind_text=word, count=len(cards),
                  cards=body, more=more, icon_chevron=icon("chevron-down"),
                  fold="closed" if col["kind"] in FINAL or not cards else "open")


def _new_task_dialog(d: dict) -> Raw:
    key, modes = d["project"]["key"], d["modes"]
    projects = render_options([(p["key"], p["name"] or p["key"]) for p in d["projects"]],
                              key)
    radios = render_all("_mode_radio.html", [
        {"value": m, "icon": _mode_icon(m, modes),
         "checked": Raw(" checked" if m == d["default_mode"] else "")} for m in modes])
    return render("_new_task.html", token_field=_token_field(d["token"]),
                  project_options=projects, mode_radios=radios,
                  icon_close=icon("close", ""), icon_info=icon("info", ""),
                  icon_plus=icon("plus", ""))


def page_project(d, older: bool = False) -> Raw:
    now = _now()
    p = d["project"]
    here = f"/p/{p['key']}" + ("/all" if older else "")
    if d["inbox"] or any(c["cards"] for c in d["columns"]):
        columns = Raw("".join(_column(c, d, here, older, now) for c in d["columns"]))
    else:
        columns = render("_empty_board.html", icon=icon("folder", "lg"),
                         icon_plus=icon("plus", ""))
    inbox = ""
    if d["inbox"]:
        items = render_all("_inbox_item.html", [
            {"id": c["id"], "title": c["title"], "origin": _from(c, now), "next": here,
             "token_field": _token_field(d["token"]), "icon_pin": icon("pin")}
            for c in d["inbox"]])
        inbox = render("_inbox.html", count=len(d["inbox"]), items=items,
                       icon_inbox=icon("inbox"), icon_chevron=icon("chevron-down"))
    notice = (render("_note.html", tone="amb page-note", icon=icon("config-error", ""),
                     strong="The project file could not be read.",
                     text=p["config_error"]) if p["config_error"] else "")
    name = p["name"] or p["key"]
    return _page("project.html", f"{name} board", d["token"], name=name,
                 monogram=monogram(name), tint=_tint(p["key"]), here=here,
                 icon_plus=icon("plus", ""), notice=notice, inbox=inbox,
                 columns=columns, dialog=_new_task_dialog(d), **_head(now))


# ------------------------------------------------------------------ task page
COMMENT_WHAT = {"comment": "Comment", "question": "Question", "answer": "Answer",
                "handoff": "Hand-off note", "progress": "Progress"}
COMMENT_ICON = {"question": "question", "handoff": "handover"}
COMMENT_CLASS = {"question": "ask", "handoff": "hand"}
RELEASE_WHAT = {"done": "Reported done", "clarify": "Asked for clarification",
                "no_report": "Stopped without a report", "expired": "Lease ran out",
                "force": "Released by force", "closed_by_person": "Released, task closed",
                "clear": "Released after /clear"}
CLAIM_LOOK = {  # claim end reason -> (pill class, icon, word)
    "done": ("k-fin", "ok", "Reported done"),
    "clarify": ("k-hand", "question", "Asked"),
    "expired": ("amb", "lease", "Expired"),
    "no_report": ("red", "stopped", "No report"),
    "force": ("amb", "unlock", "Force released"),
    "closed_by_person": ("k-clo", "x-small", "Closed"),
    "clear": ("k-clo", "x-small", "Cleared"),
}
PERSON_COMMENT_KINDS = ("comment", "answer", "question")


def _who(actor_type: str, actor_id: str) -> str:
    if actor_type == "chat":
        return f"Chat {_short(actor_id)}"
    if actor_type == "system":
        return "Lease" if actor_id == "lease" else "System"
    return actor_id


def _ev_text(text: str, box: bool = False) -> Raw:
    return render("_ev_text.html", cls="box" if box else "tx", text=text) if text else Raw("")


def _event(e: dict, labels: dict, modes: list, now: datetime) -> Raw:
    act, frm, to, note = e["action"], e["from_value"], e["to_value"], e["note"] or ""
    cls, what_cls, body = "", "muted", Raw("")
    if act == "created":
        ic, what = "plus", "Created the task"
    elif act == "state":
        ic, what = "arrow-right", "Changed the state"
        body = Raw(_ev_text(f"{labels.get(frm, frm)} to {labels.get(to, to)}.")
                   + _ev_text(note, box=True))
    elif act == "mode":
        up = frm in modes and to in modes and modes.index(to) > modes.index(frm)
        ic, what = "sliders", "Raised the mode" if up else "Lowered the mode"
        body = _ev_text(f"{frm} to {to}")
    elif act == "comment":
        ic, what = COMMENT_ICON.get(to, "comment"), COMMENT_WHAT.get(to, to)
        cls = COMMENT_CLASS.get(to, "")
        body = _ev_text(note, box=bool(cls))
    elif act == "claim":
        ic, what = "play", "Claimed the task"
        body = _ev_text(f"Session {to}")
    elif act == "release":
        ic = "stopped" if to in ("no_report", "expired") else "handover"
        what = RELEASE_WHAT.get(to, f"Released ({to})")
    elif act == "refused":
        ic, what, cls, what_cls = "warn", "Refused", "refused", ""
        body = _ev_text(f"Tried to {note}." if note else "")
    elif act == "fields":
        ic, what = "pencil", f"Changed {note.replace('_', ' ')}"
        body = _ev_text(to or "")
    elif act == "blocked":
        ic, what = "blocked", "Blocked" if to else "Unblocked"
        body = _ev_text(to or "")
    elif act == "open_points":
        ic, what = "list", "Changed the open points"
        try:
            body = _ev_text("; ".join(json.loads(to or "[]")))
        except ValueError:
            body = _ev_text(to or "")
    elif act == "pin":
        ic, what = "pin", "Pinned to the board"
    else:
        ic, what = "circle", act
    return render("_event.html", cls=cls, action=act, icon=icon(ic), what_cls=what_cls,
                  who=_who(e["actor_type"], e["actor_id"]), what=what,
                  when=_clock(e["at"], now), body=body)


def _details(t: dict) -> Raw:
    def row(label, value, empty):
        return render("_dl_row.html", label=label, value=value or empty,
                      cls="" if value else "muted")
    points = (render("_points.html", items=render_all("_point.html", [
        {"icon": icon("circle", ""), "text": x} for x in t["open_points"]]))
        if t["open_points"] else "")
    origin = (f"Created by {_who(t['created_by_type'], t['created_by_id'])} "
              f"on {_date(t['created'])}.")
    if t["origin"]:
        origin += f" {t['origin']}"
    return Raw("".join([row("Goal", t["goal"], "Not set"),
                        row("Done when", t["done_when"], "Not set"),
                        row("Scope", t["scope"], "Not set"),
                        row("Open points", points, "None"),
                        row("Blocked", t["blocked"], "Not blocked"),
                        row("Origin", origin, "")]))


def _chat_row(c: dict) -> Raw:
    if c["ended"] is None:
        pill = render("_pill.html", cls="k-act", icon=render("_live.html"), text="Working")
    else:
        pill = _pill(*CLAIM_LOOK.get(c["end_reason"],
                                     ("k-clo", "x-small", c["end_reason"] or "Ended")))
    start = _when(c["started"])
    if c["ended"] is None:
        span = f"{start:%H:%M} to now"
    else:
        end = _when(c["ended"])
        span = (f"{start:%H:%M} to {end:%H:%M}" if end.date() == start.date()
                else f"{start:%H:%M} to {end:%d.%m.%Y %H:%M}")
    transcript = (render("_chat_path.html", path=c["transcript_path"], icon_copy=icon("copy"))
                  if c["transcript_path"] else "")
    return render("_chat_row.html", session_id=c["session_id"], short=_short(c["session_id"]),
                  pill=pill, date=_date(c["started"]), span=span, transcript=transcript,
                  resume=c["resume"], icon_copy=icon("copy"))


def _notes(d: dict, now: datetime) -> Raw:
    t, need, out = d["task"], d["need"] or {}, []
    why = need.get("why")
    if why == "lease":
        left = _when(d["lease_end"]) - now
        out.append(render("_note.html", tone="amb", icon=icon("lease", ""),
                          strong=(f"Lease ends in {_span(left)}." if left > timedelta(0)
                                  else "Lease has run out."),
                          text=f"Chat {_short(need['session'])} has been silent for "
                               f"{_span(now - _when(need['last_seen']))}."))
    if why == "stopped":
        out.append(render("_note.html", tone="red", icon=icon("stopped", ""),
                          strong="The chat stopped without a report.",
                          text=_need_detail(need, now)))
    if t["inbox"]:
        out.append(render("_note.html", tone="info", icon=icon("inbox", ""),
                          strong="New in inbox.",
                          text=f"{_from(t, now)}. Pin it to put it on the board."))
    if t["blocked"]:
        out.append(render("_note.html", tone="amb", icon=icon("blocked", ""),
                          strong="Blocked:", text=t["blocked"]))
    return Raw("".join(out))


ERROR_SLOT = {"/a/start": "start", "/a/pin": "pin", "/a/state": "state",
              "/a/mode": "mode", "/a/force-release": "release", "/a/comment": "comment"}


def _actions(d: dict, error) -> Raw:
    t = d["task"]
    base = {"id": t["id"], "token_field": _token_field(d["token"])}
    errors = {}
    if error:
        path, problem = error
        errors[ERROR_SLOT.get(path, "top")] = render(
            "_inline_error.html", icon=icon("warn", ""), text=str(problem))

    def err(slot):
        return errors.get(slot, "")

    blocks = [err("top")]
    if d["can_start"]:
        blocks.append(render("_act_start.html", mode=t["mode"], icon_play=icon("play", ""),
                             error=err("start"), **base))
    else:
        waiting = [s["label"] for s in d["states"] if s["kind"] == "waiting"]
        why = (f"Chat {_short(d['live_claim'])} already holds this task." if d["live_claim"]
               else f"Start works from a waiting state ({' or '.join(waiting) or 'none'}).")
        blocks.append(render("_act_start_off.html", mode=t["mode"], why=why,
                             icon_play=icon("play", ""), error=err("start")))
    if d["can_pin"]:
        blocks.append(render("_act_pin.html", icon_pin=icon("pin", ""), error=err("pin"),
                             **base))
    options = render_all("_state_option.html", [
        {"value": s["name"], "label": s["label"],
         "selected": Raw(" selected" if s["name"] == t["state"] else ""),
         "needs_note": "1" if "note" in s["requires"] else ""} for s in d["states"]])
    noted = [s["label"] for s in d["states"] if "note" in s["requires"]]
    blocks.append(render("_act_state.html", state_options=options, error=err("state"),
                         help=f"{', '.join(noted)} ask for a note." if noted else "",
                         **base))
    modes = d["modes"]
    i = modes.index(t["mode"]) if t["mode"] in modes else 0
    lower, higher = modes[max(i - 1, 0)], modes[min(i + 1, len(modes) - 1)]
    blocks.append(render("_act_mode.html", mode=t["mode"],
                         mode_icon=_mode_icon(t["mode"], modes), lower=lower, higher=higher,
                         lower_off=Raw(" disabled" if lower == t["mode"] else ""),
                         higher_off=Raw(" disabled" if higher == t["mode"] else ""),
                         icon_down=icon("chevron-down"), icon_up=icon("chevron-up"),
                         error=err("mode"), **base))
    if d["can_force_release"]:
        blocks.append(render("_act_release.html", short=_short(d["live_claim"]),
                             lab="A chat holds this task", icon_unlock=icon("unlock", ""),
                             error=err("release"), **base))
    blocks.append(render("_act_comment.html", error=err("comment"),
                         kind_options=render_options(
                             [(k, COMMENT_WHAT[k]) for k in PERSON_COMMENT_KINDS], "comment"),
                         icon_comment=icon("comment", ""), **base))
    return Raw("".join(blocks))


def page_task(d, error=None) -> Raw:
    now = _now()
    t, p = d["task"], d["project"]
    labels = {s["name"]: s["label"] for s in d["states"]}
    cls, icon_name, word = _kind(d["kind"])
    state_pill = render("_pill.html", cls=f"lg {cls}", icon=icon(icon_name),
                        text=d["state_label"])
    live = (render("_t_live.html", session=d["live_claim"], short=_short(d["live_claim"]),
                   span=_span(now - _when(d["live_since"]))) if d["live_claim"] else "")
    history = t["history"]
    timeline = Raw("".join(_event(e, labels, d["modes"], now) for e in reversed(history)))
    claims = list(reversed(t["claims"]))
    chats = (Raw("".join(_chat_row(c) for c in claims)) if claims
             else render("_empty_small.html", text="No chat has worked on this task yet."))
    name = p["name"] or p["key"]
    return _page("task.html", f"{t['id']} {t['title']}", d["token"], key=p["key"],
                 project_name=name, monogram=monogram(name), tint=_tint(p["key"]),
                 id=t["id"], title_text=t["title"], state_pill=state_pill, kind_text=word,
                 mode=t["mode"], mode_icon=_mode_icon(t["mode"], d["modes"]), live=live,
                 notes=_notes(d, now), details=_details(t), timeline=timeline,
                 event_count=len(history), actions=_actions(d, error),
                 chats=chats, chat_count=len(claims), **_head(now))


def page_error(error, back="/") -> Raw:
    return render("error.html", title="Not done", text=str(error), back=back,
                  icon=icon("config-error", ""), icon_back=icon("back", ""))
