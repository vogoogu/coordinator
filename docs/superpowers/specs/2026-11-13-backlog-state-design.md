# Backlog: a parking lot for future to-dos (COORD-5, redefined)

Date: 2026-11-13. Status: draft for Daria's approval.

## Intent

Daria says "make a note for the future" in any chat. Today there is nowhere to put such a
note: a new task lands in `open`, which chats count and claim. COORD-5 first meant "bring
the old needs-you overview back". Daria redefined it in the design talk: **the backlog is
the set of tasks that are not specified yet**, a parking lot, not an overview of what
waits for her.

## Goals

- A task can be created as a note: a title is enough, no goal, done-when or scope.
- A note is a real task (ID, history, events), never a separate list.
- Chats are not offered a note as work: not counted as open, not claimable.
- A note leaves the backlog only when it is specified and Daria has agreed.
- Daria sees the notes of a project on its board, and a count on the home card.

## Non-goals

- No cross-project `/backlog` page now (easy later, same data).
- No manual ranking or drag ordering.
- No change to the home page content (stays clarify-only; only a count link is added to
  each project card).
- No "needs me" overview (see "Old code" below).
- Notifications, hooks other than the ones named, CLI beyond what is named: untouched.

## Anti-goals

- A chat moving a note into work without Daria's agreement.
- A note that shows up in the "N open tasks" session-start line or can be claimed.
- Deleting rows. A note that is not wanted is moved to `dropped`.

## Decisions (Daria's)

1. **Real task in a new `backlog` state** (not the inbox flag, not a separate list).
   The inbox flag means "a chat created it, the person has not looked"; backlog means
   "parked on purpose". They stay separate.
2. **Separate state in front of `open`**, with a new state kind `parked`, so the code can
   tell it from `open`. Lifecycle: backlog, open, ready, building, and so on.
3. **Existing projects get the state by hand-editing each `project.toml`** (COORD, KMU,
   TRY; a project's `states` list replaces the default list, see `config.load_project`).
   Also added to `default_project.toml` for new projects. A project without the state
   refuses "note for later" with WHAT/WHY/FIX naming the line to add. No automatic insert.
4. **Chats may create notes and may specify them. They may not decide to start one.**
   The flow: the chat fills goal, done-when, scope and asks "the spec is ready, move to
   open?"; Daria says yes; then the chat moves it.
5. **Enforcement is by convention plus a paper trail**: leaving a parked state for work
   needs goal, done-when and scope (for a chat also a note quoting Daria's agreement).
   The rule lives in core and is keyed on the `parked` kind, not on `requires` of `open`:
   `requires` would also block the person sending a task back to `open` after a
   hand-off (changed 2026-11-13 while planning). The event row records the chat and the note. A hard gate (request that Daria
   confirms on the board, or an approval token) is the fallback if a chat ever breaks this.
6. **Where it shows:** a collapsed Backlog fold on each project board `/p/<key>`, like the
   Inbox fold: count, and per row title, age, link to the task page. Plus a small count
   link "Backlog N" on each project card on home.
7. **Order: newest first**, no ranking.
8. **Old `BACKLOG(home)` code stays commented out for now.** A backlog item is created
   (once the state exists) to deal with the broader needs-you overview later. The README
   "Backlog ideas" note is changed to point at that item.

## Approaches considered

| Approach | Trade-off | Outcome |
|---|---|---|
| A. New `backlog` state of kind `parked` | One new kind and a config line per project; uses all existing task, history and board machinery | **Picked** |
| B. Reuse the `inbox` flag | Nothing new, but "not looked at" and "parked" are mixed | Rejected |
| C. Separate notes list outside tasks | Light, but no ID or history and promoting means copying | Rejected |
| `open` as the backlog | Chats still count and claim it | Rejected |

## My calls (please check)

1. New kind name `parked` (added to `config.KINDS`). Builder may pick another word.
2. `task_create` gets an optional state argument (or a `backlog` flag) so a chat can create
   a note through the existing MCP tool; only the title is required; `origin` fills in.
3. Claim refuses a `parked` task with WHAT/WHY/FIX ("not specified; move it to open first").
4. The session-start hook count and `task_list` hints exclude `parked` tasks from "open".
   `task_list` can still list them with the state filter.
5. `ready` stays in the config. With `open` now requiring the spec, `ready` is almost
   redundant for COORD, but KMU uses it. COORD may drop it later.
6. Moving `backlog` to `dropped` follows the existing `dropped` rule (needs a note).
7. Notes are created in `backlog` bypassing the normal "first state" rule in `core`.
   The builder checks whether `requires` is applied on creation and keeps notes exempt.

## What is left to the builder

Exact state and kind wording, the CSS and markup of the fold and the count link, the
shape of the new `task_create` argument, which existing tests need updating, and where
the hook count is computed.

## Old code

The `BACKLOG(home)` comment blocks in `board.py` and `pages.py`, the skipped tests and the
README note stay as they are. COORD-5's old done-when ("no BACKLOG(home) comment blocks
remain") is replaced by this design's done-when below.

## Done when

1. `parked` kind and `backlog` state exist; COORD, KMU and TRY `project.toml` files and the
   default file define `backlog`; a project without it refuses with WHAT/WHY/FIX.
2. A chat can create a note with only a title through MCP; it appears in `backlog`.
3. A `parked` task cannot be claimed and is not counted as open at session start.
4. Moving a note to `open` is refused until goal, done-when, scope and a note are set;
   the event row shows who moved it.
5. Each project board has a Backlog fold, newest first; each home card has a count link.
6. A backlog item for the broader needs-you overview exists; the README note points to it.
7. New tests for each point; `python -m unittest discover -s tests -v` passes.
8. Checked live in the browser.
