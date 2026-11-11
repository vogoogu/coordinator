"""Path helpers: app home folder and Windows-safe path comparison."""
import os
import random
import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_FILE = os.path.join(".coordinator", "project.toml")


def _copy_old_home(old: Path, new: Path) -> None:
    """Copy the pre-move folder once. The copy is made next to `new` and renamed into
    place, so a second process never sees a half-filled folder. The database goes
    through sqlite's backup API (a consistent copy even if a hook writes meanwhile)."""
    tmp = Path(f"{new}.copy-{os.getpid()}-{random.getrandbits(32):08x}")
    try:
        shutil.copytree(old, tmp, ignore=shutil.ignore_patterns(
            "coordinator.db*", "*.lock", "*.tmp"))
        src_db = old / "coordinator.db"
        if src_db.is_file():
            src = sqlite3.connect(f"file:{src_db.as_posix()}?mode=ro", uri=True)
            dst = sqlite3.connect(tmp / "coordinator.db")
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        os.replace(tmp, new)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)  # another process won the rename, or the copy failed
    except sqlite3.Error:
        shutil.rmtree(tmp, ignore_errors=True)


def app_home() -> Path:
    """Folder for the database, logs and backups. Created if missing.

    %USERPROFILE%\.coordinator, not %LOCALAPPDATA%: the Microsoft Store Python
    redirects AppData\Local into its own package folder, which other programs never
    see. The old %LOCALAPPDATA%\coordinator is copied over once on first use and left
    untouched."""
    env = os.environ.get("COORDINATOR_HOME")
    if env:
        home = Path(env)
    else:
        home = Path.home() / ".coordinator"
        if not home.exists():
            old = Path(os.environ.get("LOCALAPPDATA")
                       or Path.home() / "AppData" / "Local") / "coordinator"
            if old.is_dir():
                _copy_old_home(old, home)
    home.mkdir(parents=True, exist_ok=True)
    return home


def antson_state_dir() -> Path:
    """Where the Antson mode guard keeps its files (modes.json, reports.jsonl, go\).
    Read only: the board never creates it. ANTSON_STATE_DIR overrides it (tests)."""
    env = os.environ.get("ANTSON_STATE_DIR")
    return Path(env) if env else Path.home() / ".antson"


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
