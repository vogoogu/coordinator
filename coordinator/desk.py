"""Mail-desk connector. The desk retires with part 4 of the modes plan: switch it off
by deleting the `[desk]` table in sources.toml, nothing else changes. Reads only."""
from . import sources
from .feedback import CARD
from .sources import Spec, clean, parse_item, rows

PROJECT = "desk"


def specs(cfg: dict) -> list:
    """The items command (asked for detail, plain as fallback) and the status command."""
    cmd = cfg.get("cmd")
    if not isinstance(cmd, list) or not cmd:
        return []
    cwd = cfg.get("cwd") or "."
    health = [p for p in cmd if p not in ("--items", "--json")]
    return [Spec("desk", sources.with_detail(cmd), cwd, fallback=list(cmd)), Spec("desk_health", health, cwd)]


def _improvement(row: dict) -> dict:
    """Improvement items (U-nnn) belong to the Companion and skills card; the title says which target they change."""
    if str(row.get("kind") or "").startswith("update_"):
        target, title = str(row.get("project") or "").strip(), str(row.get("title") or row.get("id") or "").strip()
        return {**row, "project": CARD, "title": f"{target} · {title}" if target else title}
    return row


def items(data) -> list:
    return [parse_item(_improvement(r), PROJECT, "desk") for r in rows(data)]


def warning(data) -> str:
    return sources.shorten(clean(data.get("warning") if isinstance(data, dict) else ""), 200)
