"""Board pages: formats the dicts the board API returns into HTML from web/templates.
Wording and look decisions live here and in web/; the data and the actions live in board.py."""
import json
import re
from urllib.parse import quote
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from string import Template

from . import core, version

WEB_DIR = Path(__file__).parent / "web"
TEMPLATE_DIR = WEB_DIR / "templates"
ICON_DIR = WEB_DIR / "icons"

# ------------------------------------------------------------------ rendering
class Raw(str):
    """Already-rendered HTML; everything else is escaped on substitution."""


_files: dict = {}


def _read(path) -> str:
    """Template and icon files are read once per change: opening 150 small files per page
    took seconds on this machine. A changed file (new modified time) is read again."""
    stamp = path.stat().st_mtime_ns
    hit = _files.get(path)
    if hit is None or hit[0] != stamp:
        hit = _files[path] = (stamp, path.read_text(encoding="utf-8"))
    return hit[1]


def render(name: str, /, **values) -> Raw:
    text = _read(TEMPLATE_DIR / name)
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
    return render(template, title=title, token_field=_token_field(token), version=version.describe(), **values)


def icon(name: str, size: str = "sm") -> Raw:
    """Inline stroke icon from web/icons (decorative: the words next to it carry the meaning)."""
    shape = _read(ICON_DIR / f"{name}.svg").strip()
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


# BACKLOG(home): the old home page (everything that "needs you": project cards, mode guard, wiki/desk/
# BACKLOG(home): feedback/learnings cards, status strip) was removed from the home page on 2026-11-11.
# BACKLOG(home): The code is kept, commented out. We must come back and build a proper backlog feature
# BACKLOG(home): from it (blocked, stopped, lease, inbox, review, mode guard, wiki/desk items).
# BACKLOG(home): def _project_card(p: dict, items: list, token: str, now: datetime) -> Raw:
# BACKLOG(home):     tone, status_icon, status = PROJECT_LOOK[p["tone"]]
# BACKLOG(home):     rows = [_need_row(i, token, now) for i in items[:INDEX_ROWS]]
# BACKLOG(home):     wiki = p.get("wiki")
# BACKLOG(home):     if wiki:
# BACKLOG(home):         rows.append(_out_rows(wiki, token, "/"))
# BACKLOG(home):         if wiki["me_count"] > OUT_ROWS:
# BACKLOG(home):             rows.append(Raw(f'<a class="more" href="/needs/{quote(wiki["name"], safe="")}">'
# BACKLOG(home):                             f'{wiki["me_count"] - OUT_ROWS} more from the wiki</a>'))
# BACKLOG(home):     if len(items) > INDEX_ROWS:
# BACKLOG(home):         rows.append(render("_card_more.html", key=p["key"],
# BACKLOG(home):                            text=f"{len(items) - INDEX_ROWS} more on the board"))
# BACKLOG(home):     if p["config_error"]:
# BACKLOG(home):         rows.insert(0, render("_card_config_error.html", icon=icon("config-error", ""),
# BACKLOG(home):                               error=p["config_error"]))
# BACKLOG(home):     body = (Raw("<div>" + "".join(rows) + "</div>") if rows
# BACKLOG(home):             else render("_card_quiet.html", icon=icon("circle-check", "")))
# BACKLOG(home):     n = p["chats"]
# BACKLOG(home):     chats = render("_chats.html", muted="" if n else "muted",
# BACKLOG(home):                    live=render("_live.html") if n else "",
# BACKLOG(home):                    text=f"{n} chat{'s' if n != 1 else ''} working" if n
# BACKLOG(home):                    else "No chat working")
# BACKLOG(home):     count = p["needs_count"]
# BACKLOG(home):     return render("_project_card.html", tone=f"{tone}", key=p["key"],
# BACKLOG(home):                   name=p["name"] or p["key"],
# BACKLOG(home):                   monogram=_logo(p["key"]) or _logo((wiki or {}).get("name", "")) or monogram(p["name"] or p["key"]),
# BACKLOG(home):                   tint=_tint(p["key"]), status_icon=icon(status_icon), status=status,
# BACKLOG(home):                   count=count, count_class="" if count else "zero",
# BACKLOG(home):                   count_word="needs you" if count == 1 else "need you",
# BACKLOG(home):                   body=body, chats=chats, icon_chevron=icon("chevron-right"))


def _guard_chat(c: dict) -> str:
    """"chat TRIAL-1 (Title)" when the coordinator knows the session, else "chat fc0d"."""
    if c["task"]:
        return f"chat {c['task']} ({c['title']})" if c["title"] else f"chat {c['task']}"
    return f"chat {_short(c['session'])}"


def _guard_row(n: dict, now: datetime) -> Raw:
    """One "Needs you" row of the mode guard. The wording lives here."""
    age = _age(n["since"], now) if n.get("since") else ""
    kind = n["kind"]
    if kind == "hash":
        return render("_guard_row.html", tone="red", icon=icon("warn"), label="Mode file", age=age,
                      title="Mode file changed outside the switch",
                      detail="modes.json is not what the last logged write produced. "
                             "Find out who changed it before you raise anything.")
    chat = _guard_chat(n["chat"])
    if kind == "go":
        steps = ", ".join(f"{i['n']}. {i['what']} (form {i['form']})" if i["form"]
                          else f"{i['n']}. {i['what']}" for i in n["items"])
        nums = ", ".join(str(i["n"]) for i in n["items"])
        return render("_guard_row.html", tone="amb", icon=icon("unlock"), label="Waiting for go",
                      age=age, title=f"{chat} waits for your go: {steps}",
                      detail=f"Answer in that chat with: go {nums}")
    return render("_guard_row.html", tone="red", icon=icon("blocked"), label="Ready check refused",
                  age=age, title=f"{chat}: ready check for form {n['form']} was refused",
                  detail="Reason: " + ", ".join(n["reasons"]))


# BACKLOG(home): def _guard_card(g: dict, now: datetime) -> Raw:
# BACKLOG(home):     needs = g["needs"]
# BACKLOG(home):     tone = ("red" if any(n["tone"] == "red" for n in needs)
# BACKLOG(home):             else "amber" if needs or g["new"] else "green")
# BACKLOG(home):     pill, status_icon, status = PROJECT_LOOK[tone]
# BACKLOG(home):     rows = [_guard_row(n, now) for n in needs]
# BACKLOG(home):     count = len(needs)
# BACKLOG(home):     new = g["new"]
# BACKLOG(home):     return render("_guard_card.html", tone=pill, icon_mark=Raw(icon("sliders", "")),
# BACKLOG(home):                   status_icon=icon(status_icon), status=status, count=count,
# BACKLOG(home):                   count_class="" if count else "zero",
# BACKLOG(home):                   count_word="needs you" if count == 1 else "need you",
# BACKLOG(home):                   rows=Raw("".join(rows)),
# BACKLOG(home):                   new_text=(f"{_plural(new, 'new item')} from chats" if new else "Nothing new"),
# BACKLOG(home):                   icon_chevron=icon("chevron-right"))


# ------------------------------------------------------------------ needs from outside
OUT_ROWS = 5                  # items per card on the index; the card's own page shows all
LOGOS = {"kmu": "logo-kmu.png", "real": "logo-real.png", "antson": "logo-beta.png"}
KINDS = {  # kind -> (label, meaning): the words the Daybook used
    "conflict": ("Sources disagree", "Two sources say different things about the same fact. You decide which is right, ask the client, or dismiss it."),
    "fork": ("Decision needed", "A choice the documents do not settle. Answer it, or put it to the client or an Antson expert."),
    "suggestion": ("Idea from the analyst", "A suggestion for the solution, based on what the client wished for. Accept, change or reject it."),
    "new_file": ("New document not read yet", "A file in raw/ has not been read into the knowledge base yet."),
    "ingest_interrupted": ("Import interrupted", "Reading the documents stopped half way. Continue it so nothing is missing."),
    "change_open": ("Change decided, not built", "You decided this change. Hand it to the desk so it gets built in Antson."),
    "change_queued": ("Change with the desk", "The desk has this change and will make it after your approval."),
    "change_manual": ("Change for you to do by hand", "No automatic action can do this change. Do it in the Antson editor yourself."),
    "waiting_publish": ("Saved, not published", "The change is saved in Antson but not live. Publish it yourself when you are happy with it."),
    "waiting_approve": ("Mail plan to approve", "The desk read a mail and made a plan. Approve it, or skip it."),
    "approved": ("Approved", "Approved. The desk still has to run its ready check."),
    "plan_missing": ("No plan yet", "The desk has no plan for this change yet."),
    "waiting_go": ("Waiting for your GO", "Approved and checked. Say GO and the desk makes the change in Antson."),
    "needs_action": ("Needs a new tool", "No approved action can do this change yet. It needs a new action first, or you do it by hand."),
    "executing": ("Being changed now", "The desk is making this change right now."),
    "mail_question": ("Customer question", "The customer asked something in a mail. Answer it yourself; a draft is shown if the desk prepared one."),
    "mail_unreadable": ("Attachment not readable", "The desk could not read this attachment (it may be empty or damaged). Ask for it again or open the original."),
    "ingest_running": ("Ingest running", "New material is being read into this project right now. Wait until it ends before you run /ingest here."),
    "update_answer": ("Update conflicts", "A prepared improvement disagrees with what the skill says today. Keep the skill as it is or change it."),
    "update_go": ("Update waits for you", "An improvement is prepared. Look at it, then say GO and the desk applies it to the repo."),
    "update_applied": ("Applied, not released", "The improvement is in the repo but colleagues do not have it yet. Upload the new version, then say released."),
    "update_failed": ("Update failed", "An improvement could not be applied: tests failed or the owner is needed. Look at it."),
    "failed": ("Change failed", "The desk could not finish this change. Look at it before anything else."),
    "unrouted": ("Mail without a project", "The desk could not tell which project this mail belongs to. Tell it which one."),
    "digest-item": ("Feedback to decide", "Feedback from a colleague about the template or a skill. Do it, reject it, park it, or ask."),
    "handover": ("Feedback fix on its way", "A feedback fix was handed over to the template. Check how far it is."),
}
KINDS["fork_count"], KINDS["conflict_count"] = KINDS["fork"], KINDS["conflict"]
KINDS["suggestion_count"], KINDS["new_file_count"] = KINDS["suggestion"], KINDS["new_file"]
GROUPS = (("me", "Waiting for me"), ("others", "Waiting for others"), ("build", "Build state"))
CARD_NAMES = {"desk": "Mail desk", "unrouted": "Unrouted mails"}


def _days(age) -> str:
    n = int(age)
    return "today" if n < 1 else "1 day" if n == 1 else f"{n} days"


def _item_age(i: dict) -> str:
    if i["age_days"] is None:
        return "no date"
    text = _days(i["age_days"])
    if i["since_exact"]:
        return text
    return "today (date estimated)" if text == "today" else f"about {text} (date estimated)"


def _logo(name: str, fixed: bool = False):
    f = LOGOS.get("antson" if fixed else (name or "").lower())
    return Raw(f'<img src="/static/{f}" alt="{escape(name)} logo">') if f else None


def _out_item(i: dict, card: dict, token: str, here: str) -> Raw:
    label, meaning = KINDS.get(i["kind"], (i["kind"], ""))
    tone = {"red": "red", "amber": "amb"}.get(i["level"], "k-wait")
    chips = [f'<span class="chip {escape(c["tone"])}">{escape(c["label"])}: {escape(c["value"])}</span>'
             for c in i["labels"]]
    if i["risk"]:
        chips.append(f'<span class="chip">Risk: {escape(i["risk"])}</span>')
    if i["sender"] or i["sent"]:
        when = i["sent"][8:10] + "." + i["sent"][5:7] + "." if i["sent"] else ""
        who = " · ".join(x for x in ((f"from {i['sender']}" if i["sender"] else ""), when) if x)
        chips.append(f'<span class="chip">{escape(who)}</span>')
    chips = Raw('<span class="chips">' + "".join(chips) + "</span>" if chips else "")
    quotes = Raw("".join(
        f'<blockquote class="qt"><b>{escape(q["label"])}</b><i>{escape(q["source"])}</i>{escape(q["text"])}</blockquote>'
        for q in i["quotes"]))
    expl = Raw(f'<div class="dt">{escape(i["explanation"])}</div>' if i["explanation"] else "")
    how = (render("_out_how.html", text=i["how_to_act"], icon_copy=icon("copy")) if i["how_to_act"] else Raw(""))
    make = render("_out_make.html", token_field=_token_field(token), name=card["name"], key=card["key"] or "",
                  kind=i["kind"], id=i["id"], title=i["title"], goal=i["explanation"], next=here,
                  icon_plus=icon("plus"), disabled=Raw("" if card["key"] else " disabled"),
                  hint=Raw("" if card["key"] else '<span class="hint">This project is not registered in the coordinator.</span>'))
    return render("_out_item.html", tone=tone, meaning=meaning, label=label, chips=chips,
                  agecls=i["level"] if i["level"] != "none" else "", age=_item_age(i), id=i["id"],
                  title=i["title"], explanation=expl, quotes=quotes, how=how, make=make)


def _out_rows(card: dict, token: str, here: str, limit=OUT_ROWS) -> Raw:
    me = [i for i in card["items"] if i["group"] == "me"]
    notes = [render("_out_note.html", level=n["level"], text=n["text"]) for n in card["notes"]]
    return Raw("".join(notes) + "".join(_out_item(i, card, token, here) for i in me[:limit]))


def _learnings_row(l) -> Raw:
    if not l or (l["exists"] and not l["count"]):
        return Raw("")
    if not l["exists"]:
        return render("_out_note.html", level="amber", text=f"learnings folder not found: {l['path']}")
    n = l["count"]
    return render("_need_row.html", tone="k-wait", icon=icon("inbox"), label="Unprocessed learnings",
                  age="", pin="", id="", title=f"{n} file{'s' if n != 1 else ''} in the learnings inbox",
                  detail="Open the antson-helper project and type /learn.")


def _out_count(card: dict, l) -> int:
    return card["me_count"] + (1 if card.get("fixed") and l and l["exists"] and l["count"] else 0)


# BACKLOG(home): def _out_card(card: dict, l, token: str) -> Raw:
# BACKLOG(home):     tone, status_icon, status = PROJECT_LOOK[card["tone"]]
# BACKLOG(home):     count = _out_count(card, l)
# BACKLOG(home):     name = CARD_NAMES.get(card["name"], card["name"])
# BACKLOG(home):     rows = Raw((str(_learnings_row(l)) if card.get("fixed") else "") + str(_out_rows(card, token, "/")))
# BACKLOG(home):     more = card["me_count"] - OUT_ROWS
# BACKLOG(home):     other = sum(1 for i in card["items"] if i["group"] != "me")
# BACKLOG(home):     foot = (f"{more} more on the card's page" if more > 0
# BACKLOG(home):             else f"{other} waiting for others or the build" if other else "Nothing else waiting")
# BACKLOG(home):     return render("_out_card.html", tone=tone, anchor=quote(card["name"]), urlname=quote(card["name"], safe=""),
# BACKLOG(home):                   name=name, mark=_logo(name, bool(card.get("fixed"))) or monogram(name), tint=_tint(card["name"]),
# BACKLOG(home):                   status_icon=icon(status_icon), status=status, count=count, count_class="" if count else "zero",
# BACKLOG(home):                   count_word="needs you" if count == 1 else "need you", rows=rows, foot=foot,
# BACKLOG(home):                   icon_chevron=icon("chevron-right"))


# BACKLOG(home): def _strip(out: dict, now: datetime) -> Raw:
# BACKLOG(home):     parts = []
# BACKLOG(home):     for f in out["strip"]:
# BACKLOG(home):         if "text" in f:
# BACKLOG(home):             val = f["text"]
# BACKLOG(home):         elif "digest" in f:
# BACKLOG(home):             val = f"last ran {f['digest'][:16].replace('T', ' ')}" if f["digest"] else "has not run yet"
# BACKLOG(home):         elif not f["exists"]:
# BACKLOG(home):             val = "folder not found"
# BACKLOG(home):         elif not f["count"]:
# BACKLOG(home):             val = "no files yet"
# BACKLOG(home):         else:
# BACKLOG(home):             days = (now - datetime.fromisoformat(f["newest"])).days if f["newest"] else None
# BACKLOG(home):             when = "unknown" if days is None else "today" if days < 1 else f"{_days(days)} ago"
# BACKLOG(home):             val = f"{f['count']} file{'s' if f['count'] != 1 else ''}, newest {when}"
# BACKLOG(home):         parts.append(f"<span><b>{escape(f['name'])}</b>{escape(val)}</span>")
# BACKLOG(home):     parts += [f"<span><b>Warning</b>{escape(w)}</span>" for w in out["warnings"]]
# BACKLOG(home):     return render("_out_strip.html", items=Raw("".join(parts))) if parts else Raw("")


def page_needs(d) -> Raw:
    now = datetime.now().astimezone()
    card, l = d["card"], d["learnings"]
    name = CARD_NAMES.get(card["name"], card["name"])
    here = "/needs/" + quote(card["name"], safe="")
    notes = [render("_out_note.html", level=n["level"], text=n["text"]) for n in card["notes"]]
    notes += [render("_out_note.html", level="amber", text=t) for t in card["health"]]
    if card.get("fixed"):
        notes.append(_learnings_row(l))
    groups = []
    for key, heading in GROUPS:
        items = [i for i in card["items"] if i["group"] == key]
        if items:
            groups.append(render("_out_group.html", heading=heading, count=len(items),
                                 rows=Raw("".join(_out_item(i, card, d["token"], here) for i in items))))
    if not groups and not notes:
        groups.append(render("_card_quiet.html", icon=icon("circle-check", "")))
    return _page("needs.html", f"{name} · Needs you", d["token"], name=name, urlname=quote(card["name"], safe=""),
                 mark=_logo(name, bool(card.get("fixed"))) or monogram(name), tint=_tint(card["name"]),
                 updated=f"{now:%H:%M}", icon_back=icon("back"), icon_refresh=icon("refresh", ""),
                 notes=Raw("".join(map(str, notes))), groups=Raw("".join(map(str, groups))))


CLARIFY_NONE = "No task needs clarification."


def _clarify_row(i: dict, now: datetime) -> Raw:
    """One waiting question: ID, title, project, the chat's last comment, how long it waited."""
    return render("_clarify_row.html", icon=icon(ROLE_ICON["clarify"]), label=i["state_label"],
                  age=_age(i["since"], now), id=i["id"], title=i["title"], project=i["project"],
                  project_name=i["project_name"],
                  detail=i["note"] or f"Waiting in {i['state_label']}.")


def page_index(d) -> Raw:
    now = datetime.now().astimezone()
    items = d["clarify"]
    if items:
        n = len(items)
        summary = render("_index_heading.html",
                         headline="1 task needs clarification" if n == 1 else f"{n} tasks need clarification",
                         where="oldest first")
        rows = Raw('<section class="card clarify">' + "".join(_clarify_row(i, now) for i in items) + "</section>")
    else:
        summary = render("_index_quiet.html", icon=icon("circle-check", "lg"), text=CLARIFY_NONE)
        rows = Raw("")
    return _page("index.html", "Coordinator", d["token"],
                 date=f"{WEEKDAYS[now.weekday()]}, {now:%d.%m.%Y}",
                 updated=f"{now:%H:%M}", icon_refresh=icon("refresh", ""),
                 icon_modes=icon("sliders", ""), summary=summary, rows=rows)


# BACKLOG(home): (old page_index)
# BACKLOG(home): def page_index(d) -> Raw:
# BACKLOG(home):     now = datetime.now().astimezone()
# BACKLOG(home):     projects, needs = d["projects"], d["needs_you"]
# BACKLOG(home):     guard = d.get("guard") or {"needs": [], "new": 0}
# BACKLOG(home):     out = d.get("outside") or {"cards": [], "strip": [], "learnings": None, "warnings": []}
# BACKLOG(home):     by_project = {}
# BACKLOG(home):     for i in needs:
# BACKLOG(home):         by_project.setdefault(i["project"], []).append(i)
# BACKLOG(home):     own = [_out_card(c, out["learnings"], d["token"]) for c in out["cards"]]
# BACKLOG(home):     cards = Raw(("".join([_guard_card(guard, now)]) if guard["needs"] or guard["new"] else "")
# BACKLOG(home):                 + "".join(_project_card(p, by_project.get(p["key"], []), d["token"], now)
# BACKLOG(home):                           for p in projects) + "".join(own))
# BACKLOG(home):     g = len(guard["needs"])
# BACKLOG(home):     wiki_n = sum(p["wiki"]["me_count"] for p in projects if p.get("wiki"))
# BACKLOG(home):     outside_n = sum(_out_count(c, out["learnings"]) for c in out["cards"])
# BACKLOG(home):     total, n = len(needs) + g + wiki_n + outside_n, len(projects)
# BACKLOG(home):     if not projects and not total and not own:
# BACKLOG(home):         summary = render("_index_empty.html", icon=icon("folder", "lg"))
# BACKLOG(home):     elif not total:
# BACKLOG(home):         quiet = "Your project is quiet." if n == 1 and not own else f"All {n + len(own)} projects are quiet."
# BACKLOG(home):         summary = render("_index_quiet.html", icon=icon("circle-check", "lg"), text=quiet)
# BACKLOG(home):     else:
# BACKLOG(home):         busy = sum(1 for p in projects if p["needs_count"]) + sum(1 for c in out["cards"] if _out_count(c, out["learnings"]))
# BACKLOG(home):         n += len(out["cards"])
# BACKLOG(home):         summary = render("_index_heading.html",
# BACKLOG(home):                          headline="1 thing needs you" if total == 1
# BACKLOG(home):                          else f"{total} things need you",
# BACKLOG(home):                          where=(f"in {busy} of {n} project{'s' if n != 1 else ''}"
# BACKLOG(home):                                 + (" and the mode guard" if g else "") if busy or not g
# BACKLOG(home):                                 else "in the mode guard"))
# BACKLOG(home):     summary = Raw(str(summary) + str(_strip(out, now)))
# BACKLOG(home):     return _page("index.html", "Coordinator", d["token"],
# BACKLOG(home):                  date=f"{WEEKDAYS[now.weekday()]}, {now:%d.%m.%Y}",
# BACKLOG(home):                  updated=f"{now:%H:%M}", icon_refresh=icon("refresh", ""),
# BACKLOG(home):                  icon_modes=icon("sliders", ""), summary=summary, cards=cards)
# BACKLOG(home):
# BACKLOG(home):


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
            "icon_back": icon("back", ""), "icon_modes": icon("sliders", "")}


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


# ------------------------------------------------------------------ modes page
MODE_LOOK = {"read": ("k-clo", "eye"), "prod": ("k-hand", "handover"), "dev": ("k-act", "pencil")}
JOB_SHOWN = timedelta(minutes=10)   # how long the result of the last switch stays on the page


def _job_note(job: dict, now: datetime) -> Raw:
    if not job or now - _when(job["started"]) > JOB_SHOWN:
        return Raw("")
    ids, mode = ", ".join(job["ids"]), job["mode"]
    if job["state"] == "waiting":
        return render("_note.html", tone="info", icon=icon("info", ""),
                      strong=f"Changing {ids} to {mode}.",
                      text="If this raises a mode, answer the Yes/No window on your desktop. "
                           "No answer after 60 s counts as No. This page refreshes itself.")
    if job["error"]:
        return render("_note.html", tone="red", icon=icon("warn", ""),
                      strong="Nothing was changed.", text=job["error"])
    parts = []
    if job["changed"]:
        parts.append(f"Changed: {', '.join(job['changed'])} to {mode}.")
    if job["refused"]:
        parts.append(f"Not changed: {', '.join(job['refused'])} ({job['reason'] or 'refused'}).")
    if not parts:
        parts.append(f"{ids} already {mode}.")
    return render("_note.html", tone="amb" if job["refused"] else "grn",
                  icon=icon("warn" if job["refused"] else "ok", ""),
                  strong="Mode switch finished.", text=" ".join(parts))


def _mode_buttons(r: dict) -> Raw:
    return Raw("".join(render(
        "_modes_btn.html", name="row", value=f"{r['id']}|{m}", label=m,
        off=Raw(" disabled" if m == r["mode"] and not r["inherited"] else ""))
        for m in ("read", "dev", "prod")))


def _mode_row(r: dict) -> Raw:
    cls, ic = MODE_LOOK.get(r["mode"], MODE_LOOK["read"])
    inherited = Raw(' <span class="muted">(from form)</span>' if r["inherited"] else "")
    kind = {"subform": "sub-form"}.get(r["kind"], r["kind"] if r["kind"] != "unknown" else "?")
    return render("_modes_row.html", id=r["id"], indent=f"{10 + 18 * r['depth']}px",
                  name=r["name"] or "(no name yet)", kind=kind, parent=r["parent"] or "",
                  mode=render("_modes_mode.html", cls=cls, icon=icon(ic), mode=r["mode"],
                              inh=inherited),
                  new=Raw('<span class="newtag">new</span>' if r["new"] else ""),
                  buttons=_mode_buttons(r))


def page_modes(d) -> Raw:
    now = _now()
    notes = []
    if not d["mode_py_ok"]:
        notes.append(render("_note.html", tone="red", icon=icon("config-error", ""),
                            strong="The switch was not found.",
                            text=(f"No mode.py at {d['mode_py']}. " if d["mode_py"] else "No mode.py found. ")
                                 + "Set mode_py in antson.toml in the "
                                 "coordinator folder, or COORDINATOR_GUARD_MODE_PY. "
                                 "Changing modes does not work until then."))
    if d["hash_ok"] is False:
        notes.append(render("_note.html", tone="red", icon=icon("warn", ""),
                            strong="Mode file changed outside the switch.",
                            text="modes.json is not what the last logged write produced."))
    notes.append(_job_note(d["job"], now))
    groups = []
    for g in d["groups"]:
        anchor = "g-" + re.sub(r"[^A-Za-z0-9]+", "-", g["key"] or "none")
        groups.append(render("_modes_group.html", anchor=anchor, token_field=_token_field(d["token"]),
                             name=g["name"] or "Not assigned to a project",
                             count=_plural(len(g["rows"]), "item"),
                             rows=Raw("".join(_mode_row(r) for r in g["rows"])),
                             bulk=Raw("".join(render("_modes_btn.html", name="mode", value=m,
                                                     label=m, off="") for m in ("read", "dev", "prod")))))
    waiting = bool(d["job"]) and d["job"]["state"] == "waiting"
    return render("modes.html", title="Modes", version=version.describe(),
                  refresh=Raw('<meta http-equiv="refresh" content="3">\n' if waiting else ""),
                  notices=Raw("".join(notes)),
                  groups=Raw("".join(groups)) if groups
                  else render("_modes_empty.html", icon=icon("info", "")),
                  **_head(now))


def page_error(error, back="/") -> Raw:
    return render("error.html", title="Not done", text=str(error), back=back, version=version.describe(),
                  icon=icon("config-error", ""), icon_back=icon("back", ""))
