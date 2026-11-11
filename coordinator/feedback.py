"""Feedback connector: colleague feedback for the Companion and the skills (the
feedback digester), the two intake folders, and the learnings inbox of the skill.
Reads only; the core never knows this module."""
import os
from datetime import datetime

from . import sources
from .sources import Spec, parse_item, rows

CARD = "Companion and skills"


def spec(cfg: dict):
    """The digester command from `[feedback]` in sources.toml, or None."""
    cmd = cfg.get("cmd")
    if not isinstance(cmd, list) or not cmd:
        return None
    return Spec("feedback", sources.with_detail(cmd), cfg.get("cwd") or ".", fallback=list(cmd))


def items(data) -> tuple:
    """(items, meta) from the digester's answer."""
    meta = data.get("meta") if isinstance(data, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    # the digester dates an item by its digest file: approximate unless it says otherwise
    out = [parse_item({**r, "since_exact": r.get("since_exact", False)}, CARD, "feedback") for r in rows(data)]
    return out, {"last_digest": meta.get("last_digest"), "schedule_last_run": meta.get("schedule_last_run")}


def folder_signals(dirs: dict, now: datetime) -> list:
    """How many files a folder holds and when the newest arrived: only counts and times, never names."""
    out = []
    for name, path in (dirs or {}).items():
        count, newest, exists = 0, None, os.path.isdir(path)
        if exists:
            try:
                with os.scandir(path) as it:
                    for e in it:
                        if e.is_file(follow_symlinks=False):
                            count += 1
                            newest = max(newest or 0.0, e.stat().st_mtime)
            except OSError:
                exists = False
        when = datetime.fromtimestamp(newest).astimezone() if newest else None
        out.append({"name": name, "exists": exists, "count": count,
                    "newest": when.isoformat() if when else None})
    return out
