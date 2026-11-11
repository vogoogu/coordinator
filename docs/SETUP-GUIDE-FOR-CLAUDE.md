# Setup guide for Claude: help a colleague install the coordinator

You are Claude Code, opened in a fresh clone of this repo by a colleague who wants to
use the coordinator (a local task board for Claude chats). Guide them step by step.
Talk plainly and short. Do one step at a time, say what you will do, run it, show the
result, and only then go on. Ask before anything that changes files outside this repo.

## Ground rules

- Everything stays on this laptop. Their tasks live in `%USERPROFILE%\.coordinator\`,
  never in git. Never copy another person's database, `sources.toml`, `antson.toml`
  or `notify.toml`.
- Python standard library only. No `pip install`.
- **Their Claude Code settings are theirs.** `python -m coordinator install-help` only
  prints. Show the colleague exactly what you would add to
  `%USERPROFILE%\.claude\settings.json`, make a backup copy first
  (`settings.json.bak-coordinator`), and edit the file only after they say yes.
  Add to existing `hooks` entries; never replace the file.
- Do not push anything. Do not change the template or any project other than the ones
  the colleague names.

## Steps

### 1. Check the tools

- `python --version` must be 3.11 or newer (the code uses `tomllib`). On Windows the
  Microsoft Store shortcut `python.exe` can be a stub: if it opens the Store or prints
  nothing, ask them to install Python 3.11+ from python.org and tick "Add to PATH".
- `git --version`.
- `claude --version` (Claude Code). `code --version` (VS Code; the board's Start button
  uses the `code` command. If missing: VS Code, Command Palette, "Shell Command:
  Install 'code' command in PATH").

Tell them what is missing before going on.

### 2. Get the code

If this folder is already a clone, skip to step 3. Otherwise:

    git clone ssh://git@scm.specific-group.at:7999/ants/antson-coordinator.git C:\Projects\_coordinator

(needs access to the Bitbucket project ANTS and an SSH key; if the clone is refused,
tell them to ask the project owner.) Stay on `main`. Remember the clone folder: below
it is called `<repo>`. All `python -m coordinator ...` commands run from `<repo>`.

### 3. Run the tests

    python -m unittest discover -s tests

It takes about 2 minutes and must end with `OK`. Skips are fine. If something fails,
stop and look at it with them before going on.

### 4. Hook the coordinator into Claude Code

Run `python -m coordinator install-help` from `<repo>`. It prints a JSON block (hooks)
and a `claude mcp add` line. The paths in it already point at this clone.

1. **Hooks.** Compare the JSON with `%USERPROFILE%\.claude\settings.json`. Eight events
   are needed: SessionStart, PreToolUse, PostToolUse, Stop, UserPromptSubmit,
   Notification, SubagentStart, SubagentStop. Show what is missing, back up the file,
   and after their yes add only the missing entries. Check the file still parses
   (`python -c "import json,sys;json.load(open(sys.argv[1],encoding='utf-8-sig'))" <file>`).
   The PostToolUse matcher must contain `mcp__.*`.
2. **MCP server.** Run the printed `claude mcp add ...` line once (after their yes).
   Then `claude mcp list` should show `coordinator`.
3. Tell them: hooks and the MCP server load when a Claude Code session starts, so they
   must open a **new** chat afterwards.

### 5. Optional: pop-ups that say "Coordinator"

    python -m coordinator install-notify

This writes one entry under their user in the registry (undo: `uninstall-notify`).
Ask first. Without it pop-ups still work but say "Windows PowerShell".

### 6. Register their projects

Ask which project folders they want on the board. For each:

- Folder without a project file: `python -m coordinator init-project <folder> --key KEY --name "Name"`
  (KEY: 2 to 5 capital letters, for example `ABC`).
- Folder that already has `.coordinator\project.toml`: `python -m coordinator add-project <folder>`

For Antson projects the `project.toml` needs `default_mode = "write"` (see the README
section "Modes"). The hooks act only inside registered project folders.

### 7. Start the board

    python -m coordinator board

It opens http://127.0.0.1:8790/. If it says the port is taken, a board is already
running: use that one, or stop the old listener first. Starting the board at login is
optional (a shortcut in `shell:startup` to `python -m coordinator board --no-browser`);
do not create it unless they ask.

### 8. Check that it works (five minutes)

1. On the board open a project and press **New task** (or `python -m coordinator create KEY "test task"`).
2. Press **Start** on it. VS Code opens the project folder.
3. In the Claude panel there, start a **new** chat. It should say "You hold task ..."
   and give a chat name like `ABC-1 spec · otter`.
4. Ask the chat to run `git status`. When Claude Code asks for permission, wait a few
   seconds before clicking: the board card shows "Waits for your click" and a Windows
   pop-up appears. Click Allow; the mark goes away.
5. Let the chat release the task (`done`). The task lands in "To check" and a
   "ready to check" pop-up appears. Only the person sets it to Done.

If a step fails, look at `%USERPROFILE%\.coordinator\hook-errors.log` and
`board-errors.log`, and the hook entries in settings, before guessing.

### 9. Updating later

`git pull` on `main`, stop the board (every listener on port 8790), start it again,
and run `python -m coordinator install-help` to see whether new hooks need merging.
Their tasks stay; the database upgrades itself on start.

## Common problems

| Symptom | Likely cause |
|---|---|
| Start says VS Code could not open the folder | `code` is not on PATH (step 1) |
| No "You hold task" message in a new chat | hooks not merged, old session, or the folder is not a registered project |
| No pop-ups | `notify.toml` has `enabled = false`, Windows "Do not disturb", or the hook did not run (see `hook-errors.log`) |
| Board will not start | an old board holds port 8790 |
| Modes page: "switch not found" | no project with the Antson guard registered; optional, ignore unless they use Antson modes |

When you are done, tell them in two or three lines what is installed, which projects
are registered, and what still needs their attention.
