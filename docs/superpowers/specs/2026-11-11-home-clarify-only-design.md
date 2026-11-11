# Home page: only tasks that need clarification: design

Date: 2026-11-11. Status: draft, awaiting Daria's read.

## Intent

The home page (`/`) shows too many unrelated things. Daria opens it to answer
one question: **which chats are waiting for an answer from me?** The page
should show exactly that, nothing else.

## What the page shows now (to be removed from view)

Built in `page_index` (`coordinator/pages.py`) from `api_index` (`coordinator/board.py`):

1. The "N things need you" headline, and the status strip (wiki, feedback, desk, learnings).
2. The mode guard card (waiting for go, mode file changed, ready check refused).
3. One card per project: status pill, chat count, and every "needs you" row
   (stopped chat, lease ending, blocked, new in inbox, handover, wiki items).
4. Cards from outside the task database (mail desk, unrouted mails, antson-helper, learnings).

## Goals

- The home page lists every coordinator task whose state is the project's
  `on_clarify` state (default `clarify`, label "Needs clarification"), across all projects.
- Each row has what Daria needs to answer without opening the task: task ID,
  title, project, the last comment (the chat's question), and how long it has waited.
  The row links to the task page `/t/<id>`.
- Oldest first, so the longest-waiting question is on top.
- When nothing is in clarify, one calm line: "No task needs clarification." (with the existing "all good" look).
- Light theme, existing CSS tokens, handover colours (`k-hand`) and the question icon.
  Never colour alone: icon plus word, as in `docs/board-design.md`.

## Non-goals

- No other kind of "needs you": no blocked, no stopped chat, no lease ending, no
  inbox, no "To check" (review) tasks, no mode guard, no wiki/desk/feedback/learnings items.
- No new states, no change to what moves a task into clarify.
- No change to the project board (`/p/<key>`), task page, modes page, needs page
  (`/needs/<name>`), notifications, hooks, MCP or CLI.

## Anti-goals

- Deleting the old code. It stays, commented out, with a backlog marker (see below).
- Deleting rows or data. Nothing in the database changes.
- Making the home page slower: the outside readers (`api_outside`, `_guard`) must
  not run while their output is hidden.
- Breaking `/needs/<name>` or `/modes`, which reuse parts of the same code.

## Decisions (Daria's)

- The old content disappears from the home page **for good**, no replacement page now.
- The code is **commented out, not deleted**, with a note that we must come back
  and develop a proper backlog feature from it.

## How it works

**Data (`board.py`).** `api_index` returns the token plus a list of clarify tasks.
Reuse `_need(...)`: it already computes, for a handover task, `state_role == "clarify"`,
the last comment (`note`, `note_kind`) and `since`. Filter `needs` to
`state_role == "clarify"`. A task that is in clarify and also stopped or blocked still
shows, once, as a clarify task. Sort by `since` ascending. `api_index` no longer
calls `api_outside` or `_guard`.

**Page (`pages.py`).** `page_index` renders a heading ("1 task needs clarification" /
"N tasks need clarification" / the quiet line) and one row per task. Reuse `_need_row`
(it already shows the question icon, state label, age, note) and add the project key as a link to `/p/<key>`.
Wording stays in `pages.py`, look in templates and CSS, logic in `board.py`.

**Template.** `index.html` keeps the header (brand, date, Updated, Modes, Refresh) and
loses the masonry of cards. Refresh keeps working (`/?fresh=1`).

## The commented-out code and the backlog note

- Old index code is kept in place as commented blocks: `page_index` summary/cards
  logic, `_project_card`, `_guard_card`, `_out_card`, `_strip` call sites, and the
  `api_index` parts for outside/guard/project cards. Functions still used by other
  pages (`_need_row`, `_out_item`, `_out_rows`, `_guard_row`, `page_needs`) are **not** touched.
- Every commented block starts with the same marker, so it can be found with one search:
  `# BACKLOG(home): ...`
- A top-of-block note says: *this was the old home page (all "needs you" things).
  Removed from the home page on 2026-11-11. Come back and build a proper backlog
  feature from it (blocked, stopped, lease, inbox, review, mode guard, wiki/desk items).*
- The same note goes into the repo's docs (README, "Backlog ideas") so it is not
  only in the code.
- Tests that cover the removed index content are skipped with the same marker
  (`@unittest.skip("BACKLOG(home): ...")`), not deleted.

## Tests

- New tests: index lists only clarify tasks; oldest first; a blocked-only, review-only
  or inbox-only task does not appear; empty state shows the quiet line; row links to `/t/<id>`
  and the project key links to `/p/<key>`; `api_index` does not call the outside readers.
- Run: `python -m unittest discover -s tests -v` (temporary `COORDINATOR_HOME`).

## My calls (please check)

1. Header stays as it is, including the **Modes** link and Refresh. They are navigation, not content.
2. The project key on each row links to the project board. Without project cards this is the only way
   from home to a board, so I kept it.
3. Only the exact clarify state counts. "To check" (review) is a different thing and is out.
4. A clarify task with no comment shows "Waiting in Needs clarification." (the existing fallback).
5. Limit: no row cap. If clarify tasks pile up, they all show.
6. Notifications are untouched, so desktop pop-ups for "needs you" still fire as before.

## Left to the builder

CSS details for the row layout, exact heading wording within the above, and which
index tests to skip vs. rewrite.
