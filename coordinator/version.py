"""Which version of the code runs: short commit and its date, read once from git.
"unknown" without git or outside a git checkout."""
import subprocess
from pathlib import Path

_text = None


def describe() -> str:
    global _text
    if _text is None:
        _text = "unknown"
        try:
            out = subprocess.run(
                ["git", "-C", str(Path(__file__).resolve().parent.parent), "log", "-1",
                 "--format=%h %cs"], capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if out.returncode == 0 and out.stdout.strip():
                short, _, day = out.stdout.strip().partition(" ")
                _text = f"{short} ({day})"
        except (OSError, subprocess.SubprocessError):
            pass
    return _text
