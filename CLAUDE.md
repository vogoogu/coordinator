# CLAUDE.md

Router for Claude Code in this repo (a local task coordinator for Claude chats).
User docs: README.md.
Setting this up for a new person on a new laptop: follow `docs/SETUP-GUIDE-FOR-CLAUDE.md` step by step.

## Rules
- Standard library only (Python 3.11+). No pip packages.
- All writes go through `coordinator/core.py` (one transaction plus one event row).
  `cli.py`, `board.py`, `mcp_server.py` and `hooks.py` are thin fronts over core.
- Never delete rows. Tasks end in a closed state; history stays.
- The core never mentions Antson, forms, wikis, mail or Telegram. Antson lives in
  `coordinator/antson.py` (a connector: reads the guard's files, runs its `mode.py`
  as a command, never writes them).
- Keep look and logic apart: web templates and CSS in `coordinator/web/`,
  the board API (logic) in `board.py`, page building (wording) in `pages.py`.
- Refusals use three lines: WHAT, WHY, FIX (`coordinator/messages.py`).
- Hooks fail open: an error in a hook is logged and the hook exits 0.
- The `coordinator` package never touches the user's Claude Code settings;
  `install-help` only prints. The one exception is Pult's Setup dialog
  (`pult/pult.py`): the person switches each item on by hand, a
  `*.pult-backup-<time>` copy is saved first, and an unreadable file is never rewritten.
- Pult (`pult/`) is the overview board. It reads the coordinator database read-only
  and never writes it; registering a folder runs `python -m coordinator init-project`.
  It shows Claude Code chats only (terminal, VS Code, the Code tab of the desktop
  app), never Cowork sessions. Its hook list comes from `cli.HOOK_EVENTS`.

## Tests
`python -m unittest discover -s tests -v` from the repo root. Tests use a
temporary `COORDINATOR_HOME`, never the real `%USERPROFILE%\.coordinator` or `~\.antson`.

## Layout
paths, config, db, core, messages (base) -> antson (connector) -> hooks, mcp_server, board (+ pages), launcher, cli (fronts).
`pult/` stands next to the package: `pult.py` (server, Python 3.9+), `pult.html` (the board), `START.bat`.
