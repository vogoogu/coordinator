# Desktop notifications — design

Date: 2026-11-07. Status: approved scope, awaiting final read.

## Intent

Daria runs several Claude chats on coordinator tasks at once and works on other
things meanwhile. She wants her laptop to tell her, with a pop-up and a sound,
at exactly two moments:

1. **A task is finished**: a chat has done its part and handed it over for checking.
2. **A task has stopped and needs her**: a chat asked a question, or a chat
   ended without reporting.

The pop-ups should look nice. Clicking one opens that task in the board.

## Goals

- A Windows toast for each of the two moments, in the look of mockup B (below).
- A distinct sound for each moment, so she can tell them apart without looking.
- One click from the pop-up to the task page `http://localhost:8790/t/<id>`, which
  works even if the board was not running.
- Pop-ups in the header show "Coordinator" with the coordinator icon (after a
  one-time `coordinator install-notify`; see Branding).

## Non-goals

- No phone, Telegram, mail or other remote channels.
- No reply box or other input inside the pop-up.
- No repeat reminders when she doesn't react. The toast stays in the Windows
  notification centre, which is enough.
- No pop-up for moves Daria makes herself (board or CLI, actor type `person`).
- No other events (claims, comments, field edits, `open`/`ready`/`done`/`dropped`).
- Not macOS or Linux. On other platforms, notify does nothing.

## Anti-goals (would be a failure even if it works)

- A notification problem breaks or slows a task move, an MCP call or a hook.
- A chat waits on the pop-up or the sound.
- Pop-up spam: duplicates for one move, or a burst on board start-up for old events.
- A pip package, a PowerShell module that needs installing, or anything that
  changes Claude Code settings.
- The core learns about toasts, PowerShell or sounds.

## Triggers (exact)

Fire when an event row is written whose actor type is `chat` or `system` and that is one of:

| Moment | Event | Pop-up |
|---|---|---|
| Finished | `state` event into the project's `on_done` state (default `review`, "To check") | "finished" |
| Needs you | `state` event into the project's `on_clarify` state (default `clarify`, "Needs clarification") | "needs you" |
| Needs you | a claim ended as "chat stopped without report" (`stop_without_report`, `expire_leases`; the board's `STOPPED_REASONS`: `expired`, `no_report`) | "needs you" |

If one move produces both (for example an expiry that also moves the task to
`clarify`), show one pop-up, not two.

**Pile-ups (Daria's decision):** if one front call (one `expire_leases` sweep, for
example after a night) produces "needs you" for more than one task, show a single
summary pop-up instead: "<n> tasks need you", listing the IDs, with "Open board"
opening `http://localhost:<port>/`. One task gives the normal pop-up.

**Known limitation (Daria accepted it):** a chat that dies without its stop hook
(window closed, crash) is only noticed when its claim expires (4 hours) *and*
something runs `expire_leases`: the board in use or a new chat starting. No
timer is added in this version.

**The event rows decide** (builder's call, confirmed in review): whatever writes
a matching event fires, including a chat moving a task straight into `review` or
`clarify` with `task_set_state`. A chat that releases a task Daria already moved
writes no `state` event and gets no pop-up. She moved it, so she already knows.

These moves happen in these fronts today:

- `mcp_server.py` → `core.release` (chat reports done/clarify), `core.set_state` (`task_set_state`)
- `hooks.py` → `core.stop_without_report` (stop hook), `core.expire_leases` (session start)
- `board.py` → `core.expire_leases` (board sweep)
- `cli.py` → `core.expire_leases` (`coordinator expire`)

## Architecture

- **The core stays clean.** Core does not call notify. Notifications are
  derived from what core returns or from the event rows it already writes.
- **New module `coordinator/notify.py`** (front-level, next to `launcher.py`):
  - decides from a task dict plus the moment kind which pop-up to build;
  - builds the toast XML and starts a detached, hidden PowerShell process that
    shows it through `Windows.UI.Notifications.ToastNotificationManager`;
    the method is borrowed from winotify (MIT), written in our own code;
  - plays the sound in the same detached process as the toast (PowerShell
    `System.Media.SoundPlayer`), with the toast itself set `silent="true"`. It is not
    played in the calling process: hooks and the CLI exit right away, which would cut
    an async sound off. (Builder's call.)
  - makes sure the board is reachable before showing the toast (see Click);
  - never raises: any error is logged (same log as hooks) and swallowed.
- **Fronts call notify after core returns**, only after the transaction has
  committed, with the task(s) the call moved. Fire-and-forget: the call returns
  without waiting for PowerShell.

## Look (mockup B, chosen)

Header: coordinator icon + "Coordinator · <project name>". Body:

- **Finished**: icon tile with the board's `circle-check` icon on the "finished"
  green (`--k-fin-bg` / `--k-fin-t`). Title: "<ID> is ready to check". Line 2:
  task title. Line 3 (smaller): the chat's release note, shortened.
- **Needs you**: icon tile with the board's `question` icon on amber
  (`--amb-bg` / `--amb-t`). Title: "<ID> needs you". Line 2: task title. Line 3:
  the chat's question/note, or "Chat stopped without a report" for the stop case.
- Buttons: **Open task** (primary) and **Later** (dismiss). Clicking the body = Open task.

The icon tiles are PNGs (Windows toasts can't take SVG), rendered once from the
board's SVG icons and shipped in `coordinator/web/icons/` (or a sibling folder).
Windows draws the frame and font itself.

Wording lives in one place (alongside `pages.py`/`messages.py` style), not in
the PowerShell/XML builder.

## Click

"Open task" and the body use protocol activation with the URL
`http://localhost:<board port>/t/<id>`, which opens the default browser.

If nothing answers on the board port when a notification fires, notify first
starts the board detached, with no browser and no console window
(`coordinator board --no-browser`), then shows the toast. If the board can't be
started, still show the toast (the click then fails in the browser, which is
acceptable).

## Sound

- Two sounds: a soft chime for "finished", a slightly more urgent one for "needs you".
- Default: Windows' built-in sounds (files under `C:\Windows\Media`).
- Overridable by `.wav` paths in a settings file (below).
- Windows Focus / Do Not Disturb suppresses the toast. The sound should be
  skipped too when it's on, if that can be detected cheaply. Otherwise this is
  a known limitation.

## Branding (`install-notify`)

Without registration, Windows shows "Windows PowerShell" in the header.
`coordinator install-notify` writes one per-user registry key:
`HKCU\Software\Classes\AppUserModelId\<our AUMID>` with `DisplayName = Coordinator`
and `IconUri = <path to coordinator icon PNG>`, via stdlib `winreg`.
`coordinator uninstall-notify` deletes that key. Notify uses our AUMID if the key
exists and falls back to PowerShell's AUMID if not. No admin rights, no shortcut,
no Claude settings. Daria agreed to this.

## Settings

A small settings file in the app home (`%LOCALAPPDATA%\coordinator\`, or
`COORDINATOR_HOME`), e.g. `notify.toml`:

```toml
enabled = true      # pop-ups on/off
sound = true        # sound on/off
sound_finished = "" # optional .wav path; empty = built-in
sound_needs_you = ""
```

Missing file = all defaults (on). A broken file = log it and use defaults; never block a move.

## Testing

- Unit-test the trigger decision (actor type × target state × claim end reason
  → none / finished / needs-you, including the "one pop-up per move" rule) and
  the XML/wording builder, without starting PowerShell.
- The process launch and `winsound` sit behind one small function that tests replace.
- Tests use a temporary `COORDINATOR_HOME` as usual. No test may touch the real
  registry; `install-notify` writes to a key path that tests can override.
- One manual check per moment on the real laptop, recorded in the plan.

## Left to the builder

- Exact AUMID string, PNG sizes, the specific built-in `.wav` files.
- How fronts pass "what moved" to notify (return values vs. reading new event
  rows since a known id).
- How to detect the board is up. It must not be a request that runs
  `expire_leases` (every board request does), so a plain TCP connect to the port
  is the safe choice.
- Whether the README gets a short "Notifications" section (it should).
