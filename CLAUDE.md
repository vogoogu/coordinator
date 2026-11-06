# CLAUDE.md

Router for Claude Code in this repo (a local task coordinator for Claude chats).
User docs: README.md.

## Rules
- Standard library only (Python 3.11+). No pip packages.
- All writes go through `coordinator/core.py` (one transaction plus one event row).
  `cli.py`, `board.py`, `mcp_server.py` and `hooks.py` are thin fronts over core.
- Never delete rows. Tasks end in a closed state; history stays.
- The core never mentions Antson, forms, wikis, mail or Telegram.
- Keep look and logic apart: web templates and CSS in `coordinator/web/`,
  the board API (logic) in `board.py`.
- Refusals use three lines: WHAT, WHY, FIX (`coordinator/messages.py`).
- Hooks fail open: an error in a hook is logged and the hook exits 0.
- Never touch the user's Claude Code settings; `install-help` only prints.

## Tests
`python -m unittest discover -s tests -v` from the repo root. Tests use a
temporary `COORDINATOR_HOME`, never the real %LOCALAPPDATA%.

## Layout
paths, config, db, core, messages (base) -> hooks, mcp_server, board, launcher, cli (fronts).
