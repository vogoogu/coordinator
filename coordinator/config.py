"""Project configuration: defaults merged with <root>\\.coordinator\\project.toml."""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .paths import PROJECT_FILE

KINDS = ("parked", "waiting", "active", "handover", "finished", "closed")
DEFAULT_FILE = Path(__file__).parent / "default_project.toml"


class ConfigError(Exception):
    pass


@dataclass
class StateDef:
    name: str
    label: str
    kind: str
    requires: list = field(default_factory=list)


@dataclass
class ProjectConfig:
    key: str
    name: str
    default_mode: str
    modes: list
    on_done: str
    on_clarify: str
    states: list
    claim: str = "move"   # "move": a claim moves the task to the first active state; "keep": it stays


CLAIM_MODES = ("move", "keep")


def _read(path, what):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{what} not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{what} is not valid TOML ({path}): {e}") from None


def _state(raw):
    if not isinstance(raw, dict):
        raise ConfigError(f"state entry must be a table, got {raw!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"state needs a name: {raw!r}")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ConfigError(
            f"state {name!r} has unknown kind {kind!r}; allowed: {', '.join(KINDS)}")
    requires = raw.get("requires", [])
    if not isinstance(requires, list):
        raise ConfigError(f"state {name!r}: requires must be a list, got {requires!r}")
    return StateDef(name=name, label=raw.get("label", name), kind=kind,
                    requires=list(requires))


def load_project(root: str) -> ProjectConfig:
    path = os.path.join(root, PROJECT_FILE)
    data = _read(DEFAULT_FILE, "default config")
    data.update(_read(path, "project config"))

    key = data.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ConfigError(f"key is required in {path}, got {key!r}")
    modes = data.get("modes")
    if (not isinstance(modes, list) or not modes
            or not all(isinstance(m, str) for m in modes)):
        raise ConfigError(f"modes must be a non-empty list of names, got {modes!r}")
    states = [_state(s) for s in data.get("states", [])]
    by_name = {}
    for s in states:
        if s.name in by_name:
            raise ConfigError(f"duplicate state name {s.name!r}")
        by_name[s.name] = s

    default_mode = data.get("default_mode")
    if default_mode not in modes:
        raise ConfigError(f"default_mode {default_mode!r} is not in modes {modes!r}")

    on_done = data.get("on_done")
    if on_done not in by_name:
        raise ConfigError(f"on_done {on_done!r} is not a defined state")
    if by_name[on_done].kind not in ("handover", "finished"):
        raise ConfigError(
            f"on_done {on_done!r} must be a handover or finished state, "
            f"it is {by_name[on_done].kind!r}")
    on_clarify = data.get("on_clarify")
    if on_clarify not in by_name:
        raise ConfigError(f"on_clarify {on_clarify!r} is not a defined state")
    if by_name[on_clarify].kind != "handover":
        raise ConfigError(
            f"on_clarify {on_clarify!r} must be a handover state, "
            f"it is {by_name[on_clarify].kind!r}")

    claim = data.get("claim", "move")
    if claim not in CLAIM_MODES:
        raise ConfigError(
            f"claim must be one of {', '.join(CLAIM_MODES)}, got {claim!r} in {path}")

    return ProjectConfig(key=key.strip(), name=str(data.get("name", "")),
                         default_mode=default_mode, modes=list(modes),
                         on_done=on_done, on_clarify=on_clarify, states=states,
                         claim=claim)
