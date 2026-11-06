"""Path helpers: app home folder and Windows-safe path comparison."""
import os
import sys
from pathlib import Path

PROJECT_FILE = os.path.join(".coordinator", "project.toml")


def app_home() -> Path:
    """Folder for the database, logs and backups. Created if missing."""
    env = os.environ.get("COORDINATOR_HOME")
    if env:
        home = Path(env)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        home = Path(base) / "coordinator"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _long_name(path: str) -> str:
    """Expand 8.3 short names of an existing path; unchanged if not possible."""
    if sys.platform != "win32":
        return path
    import ctypes
    fn = ctypes.windll.kernel32.GetLongPathNameW
    size = 1024
    for _ in range(2):
        buf = ctypes.create_unicode_buffer(size)
        n = fn(path, buf, size)
        if n == 0:
            return path
        if n < size:
            return buf.value
        size = n + 1
    return path


def norm(p) -> str:
    """Absolute, long-name, case-folded path without trailing separator."""
    full = os.path.abspath(os.fspath(p))
    # GetLongPathNameW fails on missing paths: expand the longest existing
    # prefix and append the remainder unchanged.
    prefix, rest = full, []
    while not os.path.exists(prefix):
        parent, tail = os.path.split(prefix)
        if parent == prefix:
            break
        rest.insert(0, tail)
        prefix = parent
    out = _long_name(prefix)
    if rest:
        out = os.path.join(out, *rest)
    # normpath strips trailing separators (a bare drive root keeps its one).
    return os.path.normcase(os.path.normpath(out))


def same_or_inside(child: str, root: str) -> bool:
    c, r = norm(child), norm(root)
    if c == r:
        return True
    if not r.endswith(os.sep):
        r += os.sep
    return c.startswith(r)


def find_project_root(start: str):
    """Walk up from `start` to the first folder holding .coordinator\\project.toml."""
    cur = norm(start)
    while True:
        if os.path.isfile(os.path.join(cur, PROJECT_FILE)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
