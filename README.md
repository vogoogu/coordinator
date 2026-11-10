# coordinator

A small local task board for Claude Code chats. It keeps one list of tasks per
project, lets a chat take a task (a "claim"), and makes sure the chat hands
back a short report when it stops. Written in plain Python (standard library only).

## Where to run the commands

Run every `python -m coordinator ...` command from `C:\Projects\_coordinator`
(the folder that contains the `coordinator` package). From any other folder, set
`PYTHONPATH` to it first, for example `set PYTHONPATH=C:\Projects\_coordinator`
in `cmd`.

## Start the board

    python -m coordinator board

This opens the board in your browser (`--port 8790` by default, `--no-browser` to
skip the browser). A daily backup of the database is made at start.

To check that the board is running, test whether port 8790 accepts a connection,
for example `Test-NetConnection 127.0.0.1 -Port 8790` in PowerShell. Don't load a
page for that: every page request also expires stale leases, which can move tasks
and show pop-ups.

## Add a project

- New project: `python -m coordinator init-project <folder> --key KEY --name "Name"`
  writes `<folder>\.coordinator\project.toml` and registers the project. It never
  overwrites an existing file.
- Folder that already has a project.toml: `python -m coordinator add-project <folder>`

Other commands: `list`, `show ID`, `create KEY "title"`, `state ID state`,
`mode ID mode`, `release ID`, `pin ID`, `expire`, `backup`. Run
`python -m coordinator --help` for the list.

## Install into Claude Code

Run `python -m coordinator install-help`. It prints the hook settings and the
`claude mcp add` command. It changes nothing: read the text, then apply it
yourself (merge the JSON into `%USERPROFILE%\.claude\settings.json`, run the
`claude mcp add` line once). The MCP line looks like this:

    claude mcp add --transport stdio --env "PYTHONPATH=C:\Projects\_coordinator" --scope user coordinator -- python -m coordinator.mcp_server

`--env` takes several values, so `--scope user` comes after it and before the
server name `coordinator`; the `PYTHONPATH` value is quoted.

The PostToolUse hook keeps a chat's lease alive. Its matcher lists only Claude
Code's built-in tools, because each run costs about 0.6 s and MCP tools (browser
steps above all) are called most often. A chat that uses only MCP tools for more
than 4 hours can therefore lose its lease. If you installed the hooks before
8 November 2026, add this `"matcher"` to your PostToolUse entry (run
`install-help` to see it).

## Warn or block

Hooks act only inside registered projects. `python -m coordinator enforcement`
shows the setting. With `warn` (the default) a chat that edits files without a
task only gets a notice. With `block` the edit is refused. Start with `warn`:
`python -m coordinator enforcement block` switches it.

## Where the data lives

`%USERPROFILE%\.coordinator\`

It used to be `%LOCALAPPDATA%\coordinator\`. The Microsoft Store Python silently
redirects that folder into its own package folder, where other programs never see
it. On first use the old folder is copied over once (the database through sqlite's
backup, so the copy is consistent); the old folder is left untouched. Restart the
board after updating so that it uses the new folder too.

- `coordinator.db` - all tasks, comments, claims and history (nothing is deleted)
- `backups\` - one copy per day, the newest 14 are kept
- `hook-errors.log`, `board-errors.log` and `notify-errors.log` - problems that were caught
- `notify.toml` - pop-up settings (optional, see Notifications)
- `antson.toml` - where the mode guard's `mode.py` is (optional, see Modes)
- `reports-intake.json` - how far `reports.jsonl` has been read, and what it reported
  (if that folder is unusable, hook errors go to `%TEMP%\coordinator-hook-errors.log`)

## How a chat works with a task

Press the Start button on a task in the board. It opens a chat with a packet
(goal, done-when, scope, open points). The chat works inside the scope and ends
by releasing the task with one of three outcomes: `done` (hand over for review),
`clarify` (needs an answer) or `progress` (partial result). Only a person can set
a task to finished or closed.

## Notifications

A Windows pop-up with a sound appears when a chat:

- finishes a task: it lands in "To check" (green tick, "KMU-14 is ready to check")
- needs you: it lands in "Needs clarification", or the chat stopped without a
  report (amber question mark, "KMU-9 needs you")

Moves you make yourself on the board or command line give no pop-up. If several
tasks need you at the same moment (for example after a night), one pop-up says
"3 tasks need you". "Open task" opens the task in the board; if the board is
not running, it is started in the background first.

Once, so the pop-up header says "Coordinator" instead of "Windows PowerShell":

    python -m coordinator install-notify

This writes one entry under your user in the registry
(`HKCU\Software\Classes\AppUserModelId\Coordinator.Board`). Undo it with
`python -m coordinator uninstall-notify`.

Settings in `%USERPROFILE%\.coordinator\notify.toml` (all optional):

    enabled = true        # pop-ups on/off
    sound = true          # sound on/off
    sound_finished = ""   # your own .wav, empty = Windows sound
    sound_needs_you = ""
    board_port = 8790

Known limits: a chat window that crashes or is closed without its stop hook is
noticed only after 4 hours, and only when the board is used or a chat starts.
With Windows "Do not disturb" on, the pop-up waits but the sound still plays.

## Modes (Antson projects)

The **Modes** button on the board opens one table per project with every form,
sub-form and interface the mode guard knows: its mode (read, dev or prod), "(from form)"
when it inherits, its parent, and a **new** tag for items that a chat opened or created
and that you have not set yet. Each row has read / dev / prod buttons; tick several rows
and use the buttons under the table to set them in one call (one confirmation window).

- The board never writes the guard's files and never answers its window. A button runs
  `python <guard>\mode.py set <ids...> <mode> --by board --json`. Lowering needs no
  window; raising opens a Yes/No window on your desktop (60 s, a timeout counts as No).
  The page shows "Changing ..." and refreshes itself until you have answered.
- It reads `modes.json`, `reports.jsonl` and `go\<session>.json` from
  `%USERPROFILE%\.antson\` (`ANTSON_STATE_DIR` overrides it).
- **Needs you** on the first page lists: "Mode file changed outside the switch"
  (`mode.py check-hash`), a chat waiting for your go ("chat X waits for your go: 1. ...,
  2. ..."; answer in that chat with `go 1, 2`) and a refused ready check with its reason.
  Go files untouched for 12 hours are ignored.
- `mode.py` is found at `C:\Projects\_mail-desk\tools\guard\mode.py` unless
  `mode_py = "<path>"` is set in `antson.toml` in the coordinator folder (or the
  environment variable `COORDINATOR_GUARD_MODE_PY` is set).
- Antson projects set `default_mode = "write"` in their `project.toml` and keep
  `modes = ["read", "write"]`: the modes of forms replace per-task modes there.

## Run the tests

    python -m unittest discover -s tests -v
