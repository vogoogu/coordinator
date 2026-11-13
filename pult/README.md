# Pult

A local control board for your AI work. Pult builds itself from the Claude Code
history on your computer: every chat becomes a card, grouped by project and
status. No task is created by hand, nothing is installed, and your data stays
on your computer.

Pult sits next to [coordinator](https://github.com/vogoogu/coordinator)
(`coordinator-main\pult`). Without coordinator it works as a board on its own;
with it, chats follow rules and report their exact state.

## Start

Double-click **`START.bat`**. The board opens at http://127.0.0.1:8791.
Close the black window to stop it.

Needs Python 3.9+ (3.11+ for coordinator) and Claude Code. No packages.
Running `START.bat` again replaces an older running version of Pult.

## Main features

### Board
- **Every Claude Code chat on this computer**, read from
  `%USERPROFILE%\.claude\projects`, including old chats: the terminal, the
  VS Code extension and the Code tab of the Claude desktop app. Cowork
  sessions are not shown.
- **Two views:** *By status* and *By project* (one column per folder), plus
  the *Archive*.
- **Status worked out automatically:** Running now, Waiting for you,
  Done - check it, Interrupted. With coordinator: the exact task state, a
  Planned column for tasks nobody has started and a Backlog column for notes
  for the future. A card also shows the chat-name word of its task, "waits for
  your click" (Claude Code stopped on a permission question) and "helper
  running" (a background helper works while the chat itself is quiet).
- **Card per chat:** project in its own colour, title, 3-4 points from the last
  answer, your last request, files changed, git branch, coordinator task id.
- **Period:** Today / 7 / 30 / 90 days / All, or any from-to dates.
- **Project filter chips**, and a click on a project name jumps to its column.

### Manage the cards
- **✓ Done:** moves a card to the Done column; **Reopen** brings it back.
- **Archive** (More menu): takes a card off the board into the Archive tab;
  **Restore** returns it.
- **Priority** High / Medium / Low: cards with a priority come first in every
  column and carry a coloured label.
- A done or archived card comes back by itself when its chat gets new activity.
- **Compact / Detailed** view, **Show more** on long cards, and **A− / A+**
  to scale the whole board (for a second monitor or a small laptop).

### Start and continue work from the board
- **Continue chat:** reopens that chat in a new terminal window
  (`claude --resume`).
- **New chat:** starts Claude Code in a project folder with a first message
  and a model (Opus / Sonnet / Haiku).
- **Open folder** and **Copy path**.
- **Command:** copies the terminal command for a chat.

### Folders
- **+ Add folder:** the folder works as a workspace; each subfolder is a
  project (a column in *By project*), and chats deeper inside are grouped
  under it. Added folders are also the ones Setup can register in coordinator.

### AI on demand (uses your Claude subscription via `claude -p`)
- **Summarise with AI:** the last answer of a chat in 2-4 points, ending with
  the next step.
- **Analyse with AI:** an overview of a project: what it is, what is open, what
  was discussed last, the next step. Reads the folder listing, the newest
  files, the README and the recent chats.
- **Translate cards:** the whole board in one language. Each chat is translated
  once and again only after it changes.

### Languages
- Interface in **English** or **German** (EN / DE, top right). AI texts are
  written in the chosen language whatever the language of the chat.

### Setup: coordinator rules in one click
The **Setup** button switches on and off:
- **Hooks in Claude Code:** every chat in a registered folder gets the rules at
  start and must report when it stops. Pult writes the same hook list as
  `python -m coordinator install-help` (it reads it from the coordinator next
  to it), including the lease heartbeat, the permission-click mark and the
  helper counters. After a coordinator update switch Hooks off and on once.
- **Coordinator tools for chats** (MCP): chats create, take and report tasks.
- **Chats create their own tasks:** a short rule in your global `CLAUDE.md`.
- **Where the rules apply:** registers a board folder in coordinator.
- **Repair settings.json** when it holds two JSON blocks one after the other.

Every change first saves a `*.pult-backup-<time>` copy next to the file, other
settings are kept, and an unreadable file is never rewritten.

### Test workspace
**`TEST.bat`** (or *Setup > Test workspace*) creates five small projects with
reference files in `<home>\pult-testbed`, adds them to the board and starts
five Claude Code chats on Opus and Sonnet. See [TESTING.md](TESTING.md).

## Privacy

- Pult reads files on your computer and serves the board only on
  `127.0.0.1`. Nothing is uploaded anywhere.
- Its own data (summaries, translations, added folders) lives in `pult\cache`,
  which is excluded from git.
- Only the AI buttons send text to Claude, through your own Claude Code login,
  like any Claude Code chat.

## Not there yet

- Normal chats in the Claude app and ChatGPT: their history is not on this
  computer. Cowork sessions are left out on purpose.
- Without coordinator, the status is a guess from the last message.
- The card does not show which model ran a chat.

## Files

| File | What it is |
|---|---|
| `START.bat` | starts Pult |
| `pult.py` | the local server: reads history, actions, setup |
| `pult.html` | the board |
| `TEST.bat`, `make_testbed.py` | the test workspace |
| `TESTING.md` | how to test |

## Remove

Delete the `pult` folder. If you used Setup, switch the items off first (or
restore the `*.pult-backup-*` files).
