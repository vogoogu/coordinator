"""Windows toast pop-ups: XML, a detached PowerShell process, branding, board start.
No wording here; notify.py decides what a toast says.

COORDINATOR_NOTIFY_SINK=<file> makes show() append a JSON line to that file
instead of starting PowerShell, and start_board() do nothing (used by tests)."""
import base64
import json
import os
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.sax.saxutils import escape

try:
    import winreg
except ImportError:  # not Windows
    winreg = None

AUMID = "Coordinator.Board"
PS_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
REG_PATH = r"Software\Classes\AppUserModelId\Coordinator.Board"
REPO_ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = Path(__file__).resolve().parent / "web" / "icons" / "png"
ICONS = {"finished": "finished.png", "needs_you": "needs-you.png"}
SINK_ENV = "COORDINATOR_NOTIFY_SINK"
_DETACHED = 0x08000000 | 0x00000008 | 0x00000200  # NO_WINDOW | DETACHED | NEW_GROUP


@dataclass
class Toast:
    kind: str           # "finished" | "needs_you"
    heading: str
    line1: str
    line2: str
    attribution: str
    url: str
    button: str
    sound: str          # .wav path, or "" for silence


def _x(text: str) -> str:
    return escape(str(text), {'"': "&quot;"})


def toast_xml(t: Toast) -> str:
    icon = (ICON_DIR / ICONS.get(t.kind, ICONS["needs_you"])).as_uri()
    texts = "".join(f"<text>{_x(s)}</text>" for s in (t.heading, t.line1, t.line2) if s)
    if t.attribution:
        texts += f'<text placement="attribution">{_x(t.attribution)}</text>'
    return (
        f'<toast activationType="protocol" launch="{_x(t.url)}">'
        '<visual><binding template="ToastGeneric">'
        f'<image placement="appLogoOverride" src="{_x(icon)}"/>{texts}'
        '</binding></visual><actions>'
        f'<action content="{_x(t.button)}" activationType="protocol" arguments="{_x(t.url)}"/>'
        '<action content="Later" activationType="system" arguments="dismiss"/>'
        '</actions><audio silent="true"/></toast>')


def _ps_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def ps_script(t: Toast, aumid: str) -> str:
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] | Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument,"
        " ContentType = WindowsRuntime] | Out-Null",
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
        "$xml.LoadXml(@'",
        toast_xml(t),  # single line, escaped: cannot start with '@
        "'@)",
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml",
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier({_ps_str(aumid)}).Show($toast)",
    ]
    if t.sound:
        p = _ps_str(t.sound)
        lines.append(f"if (Test-Path -LiteralPath {p}) "
                     f"{{ (New-Object System.Media.SoundPlayer {p}).PlaySync() }}")
    return "\n".join(lines)


def _sink() -> str | None:
    return os.environ.get(SINK_ENV)


def show(t: Toast, aumid: str) -> None:
    """Show the toast (and play its sound) in a detached hidden PowerShell."""
    sink = _sink()
    if sink is not None:
        with open(sink, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(t) | {"aumid": aumid}, ensure_ascii=False) + "\n")
        return
    if sys.platform != "win32":
        return
    cmd = base64.b64encode(ps_script(t, aumid).encode("utf-16-le")).decode("ascii")
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive",
                      "-WindowStyle", "Hidden", "-EncodedCommand", cmd],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=_DETACHED)


def registered() -> bool:
    if winreg is None:
        return False
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH))
        return True
    except OSError:
        return False


def register(icon: Path = ICON_DIR / "app.png") -> None:
    """Per-user entry so toasts show "Coordinator" and our icon in the header."""
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH)
    try:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Coordinator")
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
    finally:
        winreg.CloseKey(key)


def unregister() -> bool:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_PATH)
        return True
    except FileNotFoundError:
        return False


def board_up(port: int) -> bool:
    """A plain TCP connect: a board HTTP request would run expire_leases."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


def start_board(port: int) -> None:
    if _sink() is not None or sys.platform != "win32":
        return
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    python = str(pyw if pyw.is_file() else exe)
    subprocess.Popen([python, "-m", "coordinator", "board", "--no-browser",
                      "--port", str(port)],
                     cwd=str(REPO_ROOT), env=os.environ | {"PYTHONPATH": str(REPO_ROOT)},
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=_DETACHED)
