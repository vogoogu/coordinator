# coordinator

A small local task board for Claude Code chats. It keeps one list of tasks per
project, lets a chat take a task (a "claim"), and makes sure the chat hands
back a short report when it stops. Written in plain Python (standard library only).

## Start the board

    python -m coordinator board

This opens the board in your browser (`--port 8790` by default, `--no-browser` to
skip the browser). A daily backup of the database is made at start.

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
`claude mcp add` line once).

## Warn or block

Hooks act only inside registered projects. `python -m coordinator enforcement`
shows the setting. With `warn` (the default) a chat that edits files without a
task only gets a notice. With `block` the edit is refused. Start with `warn`:
`python -m coordinator enforcement block` switches it.

## Where the data lives

`%LOCALAPPDATA%\coordinator\`

- `coordinator.db` - all tasks, comments, claims and history (nothing is deleted)
- `backups\` - one copy per day, the newest 14 are kept
- `hook-errors.log` and `board-errors.log` - problems that were caught
  (if that folder is unusable, hook errors go to `%TEMP%\coordinator-hook-errors.log`)

## How a chat works with a task

Press the Start button on a task in the board. It opens a chat with a packet
(goal, done-when, scope, open points). The chat works inside the scope and ends
by releasing the task with one of three outcomes: `done` (hand over for review),
`clarify` (needs an answer) or `progress` (partial result). Only a person can set
a task to finished or closed.

## Run the tests

    python -m unittest discover -s tests -v
