"""Windows toast pop-ups: XML, a detached PowerShell process, branding, board start.
No wording here; notify.py decides what a toast says."""
from dataclasses import dataclass


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
