"""Opens a Claude Code chat for a task in a new console window."""
import os
import subprocess


def start_chat(root: str, display_id: str) -> None:
    subprocess.Popen(["cmd", "/k", "claude"], cwd=root,
                     env=os.environ | {"COORD_TASK": display_id},
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
