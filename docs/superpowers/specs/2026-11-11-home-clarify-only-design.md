# Home page: only tasks that need clarification: design

Date: 2026-11-11. Status: **revision 1** (same day). Revision 1 replaces the flat list
of the first version, which COORD-4 built. Daria saw it and said: a project with nothing
to clarify (COORD) vanished, so she could not see all connected projects. The home page
must keep the **project cards with logos and to-do rows as before**; only the content
changes to clarify items.

## Revision 1: what changes (this section wins over the rest where they differ)

- **One card per connected project, always.** Every project registered in the
  coordinator (today TRY, KMU, COORD) gets a card, also when it has nothing to clarify.
  A project never disappears from home.
- **Same look as before the rework.** Masonry of cards (`.masonry`), the logo or
  monogram and tint (`_logo`, `monogram`, `_tint`), the project name linking to
  `/p/<key>`, the status pill, the count ("2 need you"), the footer with chats working
  and "Open board". This is the old `_project_card` and `_project_card.html`, restored
  and adapted, not a new design.
- **Content of a card = only its clarify tasks**, as to-do rows (the old `_need_row`
  look: question icon, "Needs clarification" label, age, ID, title, the chat's question
  as detail, link to `/t/<id>`). Oldest first inside a card.
- **Quiet card** when the project has none: the existing quiet body ("circle-check"),
  with wording "Nothing to clarify".
- **Status pill per card:** amber "Check" if it has clarify tasks, green "OK" if not.
  No red on home: red meant stopped/blocked, which are out of scope here.
  The count is the number of clarify tasks.
- **Headline** on top (kept from v1): "N tasks need clarification" with
  "in X of Y projects", or the quiet line "No task needs clarification." when all are quiet.
- **Order of cards:** projects with clarify tasks first (most first), then the quiet
  ones alphabetically, as the old page sorted.
- **Row cap per card:** show up to `INDEX_ROWS` (the old constant), then
  "N more on the board" linking to the project board, as before.
- **Config errors:** a project whose `project.toml` cannot be read still gets its card,
  with the old config-error note, instead of being skipped (v1 skipped it silently).
- **Still removed from home:** mode guard card, outside cards (mail desk, unrouted
  mails, antson-helper, learnings), the status strip, wiki/desk items inside project
  cards, and every non-clarify reason (blocked, stopped, lease, inbox, review). These
  stay commented out under `BACKLOG(home)` for the backlog feature (COORD-5).
- **Open question, my call:** wiki "Decision needed" or "Sources disagree" items of a
  project (KMU) are *not* shown, because they are not coordinator tasks in the clarify state.
  They go to the backlog feature.

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

**Revision 1 build notes.** `api_index` returns, per registered project: key, name,
config error (if any), chat count, and its clarify items (use `_need`, filter
`state_role == "clarify"`, as v1 does). Projects come from `core.list_projects`, never
filtered by "has clarify tasks". `page_index` builds one card per project
with `_project_card` (restore it from the BACKLOG(home) block, drop its wiki and
other-reasons parts) and puts them in `<div class="masonry">$cards</div>` in `index.html`.
Logos come from the existing `LOGOS` map and `_logo`; a project without a logo gets the monogram.

## Tests

- Revision 1 tests: a project with no clarify task still has a card with the quiet body;
  every registered project has exactly one card; card order; count and pill per card;
  a broken `project.toml` still gives a card with the config error; the row cap and the
  "more" link; logo vs monogram.
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
