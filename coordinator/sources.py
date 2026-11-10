"""Generic side of "Needs you" for things outside the task database: run a source
command, turn what it prints into items, keep the last good answer in memory.

Nothing here knows Antson, wikis, forms, the mail desk or feedback; those live in
their own connector modules (wiki.py, feedback.py, desk.py) and only describe
commands and how to read their output.

Project text (explanation, quotes) is shown on the screen only: results live in
memory, never on disk. Which sources exist is set in `sources.toml` in the app home;
without that file there are none and the board shows only its own tasks.
"""
import json
import os
import re
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from . import paths

ELLIPSIS = "…"
SETTINGS_FILE = "sources.toml"
WAITS_ON = ("me", "client", "antson-expert", "claude")
RISKS = ("low", "medium", "high")
TONES = ("neutral", "info", "warn", "alert")
# Limits for text from a source. Longer text is cut at a word and ends with "…";
# a how-to line is dropped instead, because half a command is worse than none.
TITLE_MAX, PROJECT_MAX, ID_MAX, SOURCE_MAX = 160, 120, 120, 120
LABEL_MAX, HOW_TO_MAX, EXPLANATION_MAX, QUOTE_MAX = 24, 600, 500, 500
QUOTES_MAX, LABELS_MAX, LABEL_VALUE_MAX, SENDER_MAX = 4, 6, 80, 24

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KEY_TITLE = re.compile(r"^(?:Conflict\s+C\d+:\s*)?(?:[\w.-]+/)*([\w-]+)#([\w-]+)$")


# ------------------------------------------------------------------ text
def shorten(text, limit: int) -> str:
    """The text itself when it fits; otherwise whole words ending with a visible
    ellipsis, never longer than `limit`. One very long word (a path) is cut hard but
    still marked."""
    text = str(text)
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit * 0.5:
        cut = cut[:space]
    return cut.rstrip(" ,;:-–—") + ELLIPSIS


def clean(value, limit=None) -> str:
    """Control characters out, white space collapsed; shortened with an ellipsis if a limit is given."""
    text = _CONTROL.sub("", str(value if value is not None else ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if limit is None else shorten(text, limit)


def _readable_title(title: str) -> str:
    """A page key such as 'processes/quote-request#preview-saves' becomes words."""
    m = _KEY_TITLE.match(title)
    return title if not m else "%s: %s" % (m.group(1).replace("-", " "), m.group(2).replace("-", " "))


def _quotes(value) -> list:
    out = []
    for e in value if isinstance(value, list) else []:
        text = clean(e.get("text"), QUOTE_MAX) if isinstance(e, dict) else ""
        if text:
            out.append({"label": clean(e.get("label"), LABEL_MAX),
                        "source": clean(e.get("source"), SOURCE_MAX), "text": text})
        if len(out) == QUOTES_MAX:
            break
    return out


def _labels(value) -> list:
    out = []
    for e in value if isinstance(value, list) else []:
        if not isinstance(e, dict):
            continue
        name, text = clean(e.get("label"), LABEL_MAX), clean(e.get("value"), LABEL_VALUE_MAX)
        if name and text:
            out.append({"label": name, "value": text,
                        "tone": e["tone"] if e.get("tone") in TONES else "neutral"})
        if len(out) == LABELS_MAX:
            break
    return out


def _since(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        t = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return t.astimezone() if t.tzinfo is None else t


def parse_item(raw: dict, default_project: str, source: str) -> dict:
    """The one shape every source is turned into, and the only place that decides
    what may reach the page: other fields a source prints are dropped."""
    since = _since(raw.get("since"))
    exact = bool(raw["since_exact"]) if "since_exact" in raw else since is not None
    how = clean(raw.get("how_to_act"))
    sent = _since(raw.get("sent"))
    return {"project": clean(raw.get("project") or default_project, PROJECT_MAX),
            "kind": clean(raw.get("kind"), 40), "id": clean(raw.get("id"), ID_MAX),
            "title": shorten(_readable_title(clean(raw.get("title"))), TITLE_MAX),
            "since": since.isoformat() if since else None, "since_exact": exact and since is not None,
            "waits_on": raw["waits_on"] if raw.get("waits_on") in WAITS_ON else "me",
            "how_to_act": how if len(how) <= HOW_TO_MAX else "",
            "source": clean(raw.get("source") or source, SOURCE_MAX),
            "explanation": clean(raw.get("explanation"), EXPLANATION_MAX),
            "quotes": _quotes(raw.get("quotes")), "labels": _labels(raw.get("labels")),
            "sender": clean(raw.get("sender"), SENDER_MAX),
            "sent": sent.date().isoformat() if sent else "",
            "risk": raw["risk"] if raw.get("risk") in RISKS else "",
            "change": clean(raw.get("change"), ID_MAX),
            "handover_id": clean(raw.get("handover_id"), ID_MAX)}


def rows(data, key="items") -> list:
    r = data.get(key) if isinstance(data, dict) else None
    return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []


# ------------------------------------------------------------------ settings
def settings() -> dict:
    """`sources.toml` in the app home ({} if missing or broken). COORDINATOR_SOURCES overrides the path."""
    path = os.environ.get("COORDINATOR_SOURCES") or str(paths.app_home() / SETTINGS_FILE)
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# ------------------------------------------------------------------ running commands
@dataclass(frozen=True)
class Spec:
    name: str
    cmd: list
    cwd: str
    fallback: list | None = None   # run instead when `cmd` fails (the plain command when `cmd` asks for detail)


@dataclass(frozen=True)
class Result:
    name: str
    ok: bool          # data is usable (fresh, or the last good answer)
    data: object
    error: str        # why the latest run failed ("" when it worked)
    at: datetime      # when the data was read
    stale: bool


_last_good: dict = {}            # name -> (time, data); memory only
_last_lock = threading.Lock()


def _run(cmd: list, cwd: str, timeout: int):
    argv = [sys.executable if part == "python" and i == 0 else part for i, part in enumerate(cmd)]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL, creationflags=flags)
    except subprocess.TimeoutExpired:
        return None, f"no answer after {timeout} s"
    except (OSError, ValueError) as e:
        return None, f"cannot run: {e}"
    if done.returncode != 0:
        return None, f"exit {done.returncode}: " + shorten(" ".join((done.stderr or "").split()), 200)
    try:
        return json.loads(done.stdout), ""
    except ValueError:
        return None, "the answer is not JSON"


def _fetch(spec: Spec, timeout: int) -> Result:
    now = datetime.now().astimezone()
    data, error = _run(spec.cmd, spec.cwd, timeout)
    if error and spec.fallback:
        data, error = _run(spec.fallback, spec.cwd, timeout)
    with _last_lock:
        if not error:
            _last_good[spec.name] = (now, data)
            return Result(spec.name, True, data, "", now, False)
        if spec.name in _last_good:
            when, saved = _last_good[spec.name]
            return Result(spec.name, True, saved, error, when, True)
    return Result(spec.name, False, None, error, now, False)


def fetch_all(specs: list, timeout: int = 60) -> dict:
    """Run all commands in parallel; {name: Result}."""
    if not specs:
        return {}
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        return {r.name: r for r in pool.map(lambda s: _fetch(s, timeout), specs)}


def forget() -> None:
    """Drop the remembered answers (tests)."""
    with _last_lock:
        _last_good.clear()


def with_detail(cmd: list, flag: str = "--detail") -> list:
    return list(cmd) + [flag]
