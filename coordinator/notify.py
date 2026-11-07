"""Desktop notifications: decide from new event rows which pop-ups to show, word
them, and hand them to toast.py. Fronts call fire() after a core call returns.
Never raises into the caller: errors go to notify-errors.log."""
import tomllib
import traceback
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Iterable

from . import config, paths, toast

SETTINGS_FILE = "notify.toml"
LOG_FILE = "notify-errors.log"
STOPPED = ("expired", "no_report")
NOTE_MAX = 140
DEFAULT_SOUND = {
    "finished": r"C:\Windows\Media\Windows Notify System Generic.wav",
    "needs_you": r"C:\Windows\Media\Windows Notify Messaging.wav",
}


@dataclass
class Settings:
    enabled: bool = True
    sound: bool = True
    sound_finished: str = ""
    sound_needs_you: str = ""
    board_port: int = 8790


@dataclass
class Alert:
    kind: str           # "finished" | "needs_you"
    task_id: str
    project: str
    title: str
    note: str
    stopped: bool


def log_error(where: str) -> None:
    """Log the current exception to the app home; never raises."""
    try:
        with open(paths.app_home() / LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {where}\n"
                    f"{traceback.format_exc()}\n")
    except Exception:
        pass


def load_settings() -> Settings:
    path = paths.app_home() / SETTINGS_FILE
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return Settings()
    except Exception:
        log_error("settings")
        return Settings()
    s = Settings()
    for f in fields(Settings):
        value = raw.get(f.name)
        if type(value) is type(getattr(s, f.name)):  # bool is not int here
            setattr(s, f.name, value)
    return s


def mark(conn) -> int:
    """Id of the newest event row; take it before a core call."""
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0]


def _short(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= NOTE_MAX else text[:NOTE_MAX - 1] + "…"


def collect(conn, since: int, task_ids: Iterable[str] | None = None) -> list[Alert]:
    """One Alert per task moved by a chat or the system since event `since`."""
    wanted = None if task_ids is None else {str(t).upper() for t in task_ids}
    rows = conn.execute(
        "SELECT e.id, e.action, e.to_value, e.note, t.id AS pk, t.number, t.title,"
        " p.key, p.name, p.root FROM event e JOIN task t ON t.id = e.task_id"
        " JOIN project p ON p.id = t.project_id"
        " WHERE e.id > ? AND e.actor_type IN ('chat', 'system')"
        " AND e.action IN ('state', 'release') ORDER BY e.id", (since,)).fetchall()
    alerts: dict[int, Alert] = {}
    cfgs = {}
    for r in rows:
        tid = f"{r['key']}-{r['number']}"
        if wanted is not None and tid.upper() not in wanted:
            continue
        if r["root"] not in cfgs:
            try:
                cfgs[r["root"]] = config.load_project(r["root"])
            except Exception:
                cfgs[r["root"]] = None
        cfg = cfgs[r["root"]]
        if cfg is None:
            continue
        kind, stopped, note = None, False, None
        if r["action"] == "state" and r["to_value"] == cfg.on_done:
            kind, note = "finished", r["note"]
        elif r["action"] == "state" and r["to_value"] == cfg.on_clarify:
            kind, note = "needs_you", r["note"]
        elif r["action"] == "release" and r["to_value"] in STOPPED:
            kind, stopped = "needs_you", True
        if kind is None:
            continue
        a = alerts.get(r["pk"])
        if a is None:
            a = alerts[r["pk"]] = Alert(kind, tid, r["name"] or r["key"], r["title"],
                                        "", stopped)
        elif kind == "needs_you":
            a.kind = "needs_you"
        a.stopped = a.stopped or stopped
        if note:
            a.note = _short(note)
    for pk, a in alerts.items():
        if not a.note:
            c = conn.execute("SELECT text FROM comment WHERE task_id = ?"
                             " ORDER BY id DESC LIMIT 1", (pk,)).fetchone()
            a.note = _short(c["text"]) if c else ""
    return list(alerts.values())


def fire(conn, since: int, task_ids: Iterable[str] | None = None) -> None:
    """Show pop-ups for chat/system moves since event `since`. Call after the core
    call has returned. Never raises; errors go to notify-errors.log."""
    try:
        settings = load_settings()
        toasts = compose(collect(conn, since, task_ids), settings)
        if not toasts:
            return
        if not toast.board_up(settings.board_port):
            toast.start_board(settings.board_port)
        aumid = toast.AUMID if toast.registered() else toast.PS_AUMID
        for t in toasts:
            toast.show(t, aumid)
    except Exception:
        log_error("fire")


def _sound(kind: str, s: Settings) -> str:
    if not s.sound:
        return ""
    own = s.sound_finished if kind == "finished" else s.sound_needs_you
    return own or DEFAULT_SOUND[kind]


def compose(alerts: list[Alert], settings: Settings) -> list[toast.Toast]:
    """Word the alerts. Two or more needs-you alerts become one summary toast."""
    if not settings.enabled:
        return []
    base = f"http://127.0.0.1:{settings.board_port}"
    needs = [a for a in alerts if a.kind == "needs_you"]
    out = []
    for a in alerts:
        if a.kind == "needs_you" and len(needs) > 1:
            continue
        if a.kind == "finished":
            heading, line2 = f"{a.task_id} is ready to check", a.note
        else:
            heading = f"{a.task_id} needs you"
            line2 = "Chat stopped without a report" if a.stopped else a.note
        out.append(toast.Toast(a.kind, heading, a.title, line2, a.project,
                               f"{base}/t/{a.task_id}", "Open task",
                               _sound(a.kind, settings)))
    if len(needs) > 1:
        projects = sorted({a.project for a in needs})
        out.append(toast.Toast("needs_you", f"{len(needs)} tasks need you",
                               ", ".join(a.task_id for a in needs), "",
                               projects[0] if len(projects) == 1 else "",
                               f"{base}/", "Open board",
                               _sound("needs_you", settings)))
    return out
