"""Opens a Claude Code chat for a task in a new console window."""
import os
import secrets
import subprocess


def start_chat(root: str, display_id: str) -> None:
    """COORD_TASK names the task; COORD_LAUNCH is a fresh token per Start, so a
    /clear in this window (new session id) can take over its own claim (R32)."""
    subprocess.Popen(["cmd", "/k", "claude"], cwd=root,
                     env=os.environ | {"COORD_TASK": display_id,
                                       "COORD_LAUNCH": secrets.token_hex(8)},
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
