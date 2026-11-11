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

## Set up on a new laptop

Easiest: open this folder in Claude Code and say "set this up for me". It follows
`docs/SETUP-GUIDE-FOR-CLAUDE.md` and asks before it changes anything. By hand:

Each person has their own board and their own tasks; only the code is shared.
Your tasks live in `%USERPROFILE%\.coordinator\` on your laptop, never in git.

1. Install Python 3.11 or newer (the code uses `tomllib`) and git.
2. Clone the repository (ask for its address) and stay on `main`:

       git clone <repository address> C:\Projects\_coordinator

3. Run `python -m coordinator install-help` from that folder. It prints a JSON
   block and one `claude mcp add` line. You apply both yourself: merge the JSON
   into `%USERPROFILE%\.claude\settings.json` and run the `claude mcp add` line
   once. The program never changes your Claude Code settings.
4. Optional, so the pop-up says "Coordinator": `python -m coordinator install-notify`.
5. Register each project (one command per project):

       python -m coordinator init-project <folder> --key KEY --name "Name"

   For Antson projects then set `default_mode = "write"` in the `project.toml`
   (see "Modes" below).
6. Start the board: `python -m coordinator board`.

Starting it at login is optional. To do it, make a shortcut to
`python -m coordinator board --no-browser` and put it in your Startup folder
(`shell:startup`). Nothing creates that shortcut for you.

Without any further settings the board shows your registered projects, and for
each project that has `tools\project_state.py` (template projects do) what waits
for you there. The Modes page finds the guard in the first registered project
that has `tools\chrome_guard\guard\mode.py`. The footer shows the version
(short commit and date), so you can tell whether two laptops run the same one.

**Update:** `git pull` on `main`, then stop the board and start it again (it
refuses to start while an old one holds the port). Your tasks stay; the database
is upgraded in place on start.

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

The PostToolUse hook keeps a chat's lease alive (30 minutes without activity ends
the claim and moves the task to the clarify state). Its matcher must include the
MCP tools (`mcp__.*`, browser steps above all), otherwise a chat that works only
through them loses its lease while it is busy. If you installed the hooks before
11 November 2026, replace the `"matcher"` of your PostToolUse entry (run
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

Press the Start button on a task in the board. It opens the project folder in
VS Code (`code <folder>`; the `code` command must be on PATH) and leaves a "waiting
task" note for that folder. Open the Claude panel and start a chat there: the first
new chat in that folder takes the task and gets a packet (goal, done-when, scope, open
points, its chat name). A note older than an hour is ignored; a task that is no longer
waiting is skipped. The chat works inside the scope and ends by releasing the task
with one of three outcomes: `done` (hand over for review), `clarify` (needs an answer)
or `progress` (partial result). Only a person can set a task to finished or closed.

### Task chain (spec chat with background helpers)

- **Names.** Every task has a short word, for example `otter`. The chat is
  `KMU-7 spec · otter`; its helpers are `KMU-7 build 1 · otter`, `KMU-7 test 1 · otter`
  (the number counts fix rounds). The board cards, the task page and the pop-ups use
  these names. Tasks made before this existed get a word when you press Start.
  The packet tells the chat its name; the coordinator cannot set the title of the
  VS Code chat tab.
- **Helper reports.** A helper reports with `task_comment` and `role` (`build 1`,
  `test 1`, `spec`). The task page shows these under "Helper reports", word for word,
  newest first, each under its chat name.
- **Waits for your click.** When Claude Code stops on a permission question, the
  `Notification` hook marks the task ("waits for your click" on the card and the task
  page) and gives the pop-up. The mark goes away with the next tool call of the chat.
- **Lease while helpers work.** The `SubagentStart` and `SubagentStop` hooks count a
  chat's running helpers. While one runs the claim does not expire (for at most
  2 hours after the helper started, in case its stop is never seen) and the card says
  "working, helper running" instead of "quiet".
- **Overlap list.** `task_overlaps` (MCP) lists the other open tasks of the project
  with the forms they touch, tasks sharing a form first. A chat records the forms of
  its task with `task_update` and `fields: {"forms": ["497", "458"]}`. Forms are
  free text (ids or names); the coordinator does not read them from the spec.
- **Self-refresh.** Home, project and task pages ask `/api/pulse` (a few bytes) every
  5 seconds and reload only when something was written; never while a dialog is open
  or a field has text in it.

After a `git pull`, run `python -m coordinator install-help` and merge the new hook
entries (`Notification`, `SubagentStart`, `SubagentStop`) into your settings.

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
noticed only after 30 minutes without activity, and only when the board is used or a chat
starts. Until then its card shows "quiet for N min" (from 10 minutes).
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
- `mode.py` is found in this order: the environment variable
  `COORDINATOR_GUARD_MODE_PY`; `mode_py = "<path>"` in `antson.toml` in the
  coordinator folder; the first registered project that has
  `tools\chrome_guard\guard\mode.py`. Modes live per laptop in `%USERPROFILE%\.antson\`,
  so any project's copy reads and writes the same state. With none found the Modes
  page says so.
- Antson projects set `default_mode = "write"` in their `project.toml` and keep
  `modes = ["read", "write"]`: the modes of forms replace per-task modes there.

## When you answer a question

A chat that released its task with `clarify` (or lost it to the 30 minute lease) takes
the task back when you write to it: the UserPromptSubmit hook (`hooks.py prompt`, printed by
`install-help`) claims it again if the task is still in the clarify state and nobody
holds it. If your message is about something else, the chat releases it with `progress`.

## Backlog ideas

- **BACKLOG(home)**: since 2026-11-11 the home page lists only tasks that need clarification.
  The old "needs you" overview (stopped chats, leases, blocked, inbox, review, mode guard,
  wiki/desk/feedback/learnings cards) is kept commented out in `board.py` and `pages.py`,
  and its tests are skipped, all under the marker `BACKLOG(home)`. We must come back and
  build a proper backlog feature from it (task COORD-5). Search for `BACKLOG(home)`.

## Before a push

`python tools/check_publish.py` runs the tests and checks the files that would ship:
no user or OneDrive path, no client name (the list is read from the antson-helper
skill repo, or from `ANTSON_BANNED_TERMS`), no non-dummy e-mail address, no
`sources.toml`, `antson.toml`, `notify.toml` or database file. Push only when it says OK.

## Run the tests

    python -m unittest discover -s tests -v
