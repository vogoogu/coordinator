"""Opens a project folder in VS Code (the board's Start button)."""
import shutil
import subprocess

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: the `code` launcher is a .cmd file


def open_folder(root: str) -> None:
    """Open `root` in VS Code (`code <folder>`); an already open window is reused.
    The chat itself is started by the person in the Claude panel: the first new chat
    in that folder picks the task up (core.take_start_note). Raises OSError if the
    `code` command is not found or cannot start."""
    exe = shutil.which("code")
    if not exe:
        raise FileNotFoundError("the 'code' command is not on PATH (VS Code: "
                                "Command Palette, 'Shell Command: Install code in PATH')")
    subprocess.Popen([exe, root], creationflags=NO_WINDOW)
