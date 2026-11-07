"""Board pages: formats the dicts the board API returns into HTML from web/templates.
Wording and look decisions live here and in web/; the data and the actions live in board.py."""
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
