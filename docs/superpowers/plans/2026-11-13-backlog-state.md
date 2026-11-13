# Backlog State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `backlog` state (kind `parked`) where chats park unspecified to-dos as real tasks, which leave it only when specified, and which the board shows in a fold per project.

**Architecture:** One new state kind, `parked`, read from each project's `project.toml`. Core knows the kind (create into it, refuse claim, gate leaving it); hooks and MCP are thin changes; the board gets a `backlog` list in `api_project` and a count in `api_index`; pages and templates render a fold and a count link.

**Tech Stack:** Python 3.11+ standard library only, `unittest`, existing `coordinator/web` templates.

**Spec:** `docs/superpowers/specs/2026-11-13-backlog-state-design.md`

## Global Constraints

- Standard library only (Python 3.11+). No pip packages.
- All writes go through `coordinator/core.py` (one transaction plus one event row). `cli.py`, `board.py`, `mcp_server.py`, `hooks.py` stay thin fronts.
- Never delete rows. An unwanted note goes to `dropped`.
- The core never mentions Antson.
- Look and logic apart: templates and CSS in `coordinator/web/`, logic in `board.py`, wording in `pages.py`.
- Refusals use three lines WHAT, WHY, FIX in `coordinator/messages.py`.
- Hooks fail open.
- Never touch the user's Claude Code settings.
- Tests use a temporary `COORDINATOR_HOME`. Run all: `python -m unittest discover -s tests -v` from the repo root.
- New kind name `parked`; new state name `backlog`, label `Backlog`; order: newest first; no ranking.
- The working tree already holds uncommitted work of other tasks in `board.py`, `messages.py`, `pages.py`, `tests/test_board.py`. Commit only your own hunks (`git add -p`), never `git add -A`.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A project whose `project.toml` has no `backlog` state: creating a note refuses with three lines and creates nothing. (Task 2)
2. A different chat than the creator specifies and promotes a note, without a claim. (Task 3)
3. A note moved straight to `dropped` needs only a note, not the spec. (Task 3)
4. A project with only backlog notes and nothing else still shows its board, not the empty-board screen. (Task 5)
5. The task page of a parked task offers no Start button (`can_start` false). (Task 5)
6. Notes count toward a chat's limit of 3 created tasks per session (`CHAT_CREATE_LIMIT`). (Task 2, documented, not changed)

---

### Task 1: The `parked` kind and the `backlog` state in config

**Files:**
- Modify: `coordinator/config.py:9` (KINDS)
- Modify: `coordinator/default_project.toml` (add the state before `open`)
- Modify: `.coordinator/project.toml` (COORD, same state before `open`)
- Test: `tests/test_config.py`; fix any existing test that counts or lists default states

**Interfaces:**
- Produces: `config.KINDS` contains `"parked"`. Default and COORD configs define `StateDef(name="backlog", label="Backlog", kind="parked", requires=[])` as the first state.

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`: `test_default_config_has_backlog_state_first` (first state of the default config is `backlog`, kind `parked`); `test_parked_is_a_valid_kind` (a toml with a `parked` state loads); `test_unknown_kind_still_refused` (kind `later` raises `ConfigError`).
- [ ] **Step 2: Run** `python -m unittest tests.test_config -v`. Expected: the first two FAIL.
- [ ] **Step 3: Add** `"parked"` to `KINDS` in `config.py`; add the `backlog` state block first in `default_project.toml` and in `.coordinator/project.toml` (kind `parked`, no `requires`).
- [ ] **Step 4: Run the whole suite.** Fix existing tests that assumed the old default state list or column count (the changes are expectations only; do not change behavior). Expected: all PASS.
- [ ] **Step 5: Commit** `feat(config): parked kind and backlog state`.

---

### Task 2: Create a note, refuse to claim it, keep it out of "open"

**Files:**
- Modify: `coordinator/core.py` (`create_task` at 143, `claim` at 514, `overlaps` at 949)
- Modify: `coordinator/messages.py`
- Test: `tests/test_core_tasks.py`, `tests/test_core_claims.py`

**Interfaces:**
- Consumes: the `parked` kind from Task 1.
- Produces:
  - `core.is_parked(cfg, state_name: str) -> bool`
  - `core.create_task(conn, project_key, title, actor, goal="", done_when="", scope="", origin="", inbox=None, backlog: bool = False) -> dict`; with `backlog=True` the task starts in the project's first `parked` state (event `created`, to that state; `inbox` stays false).
  - `messages.no_backlog_state(project_key) -> CoordinatorError`; `messages.claim_parked(display_id) -> ClaimError`.

- [ ] **Step 1: Write failing tests.** In `test_core_tasks.py`: `test_create_backlog_note_with_title_only` (state `backlog`, empty goal, `inbox` False, one `created` event); `test_backlog_note_counts_toward_chat_limit` (a chat creating 3 notes is refused on the 4th); `test_create_backlog_without_state_refuses` (project with an own `states` list lacking a parked state: `CoordinatorError`, WHAT mentions the project key, no task row created); `test_overlaps_ignores_parked` (a note touching form 1 does not appear in `core.overlaps`). In `test_core_claims.py`: `test_claim_refuses_parked_task` (`ClaimError`, no claim row, state still `backlog`); `test_waiting_only_claim_ignores_parked` (returns None).
- [ ] **Step 2: Run** `python -m unittest tests.test_core_tasks tests.test_core_claims -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** `is_parked` and the `backlog` argument in `create_task` (pick the first state of kind `parked`; none: raise `messages.no_backlog_state`, three lines, text says to add a state of kind `parked` to `.coordinator/project.toml`). In `claim`, refuse when `kinds.get(task["state"]) == "parked"` (after the already-mine check, before the finished/closed check) with `messages.claim_parked` (FIX: move it to open once it is specified). In `overlaps`, add `"parked"` to the `final` set.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): create backlog notes, refuse to claim them`.

---

### Task 3: Any chat may specify a note; leaving the backlog needs the spec

**Files:**
- Modify: `coordinator/core.py` (`_require_edit_rights` at 263, `set_fields` at 328, `set_state` at 286)
- Modify: `coordinator/messages.py`
- Test: `tests/test_core_states.py`, `tests/test_core_tasks.py`

**Interfaces:**
- Consumes: `core.is_parked` (Task 2).
- Produces:
  - `core._require_edit_rights(conn, task, actor, attempted, cfg=None)`; with `cfg` given and the task in a parked state, any chat may edit it (no claim needed; nobody can hold one).
  - `messages.leave_backlog_missing(display_id, field, by_chat: bool) -> CoordinatorError`.
  - Rule in `set_state`: moving from a parked state to a state of kind `waiting`, `active` or `handover` needs non-empty `goal`, `done_when`, `scope`; a chat also needs a non-empty `note` (its FIX says: quote the person's agreement). Moves to `parked`, `finished` or `closed` states are not gated by this rule.

- [ ] **Step 1: Write failing tests.** In `test_core_states.py`: `test_chat_moves_specified_note_to_open_with_note` (chat B, which did not create it, sets goal, done_when, scope, then `set_state(..., "open", note="Daria agreed")` works, event row has the chat id and the note); `test_chat_move_without_note_refuses` (spec filled, no note: `CoordinatorError` naming `note`); `test_move_without_spec_refuses` (naming the first empty of goal, done_when, scope); `test_person_move_needs_spec_but_no_note`; `test_note_can_be_dropped_without_spec` (person, `dropped`, note given: works); `test_open_still_reachable_from_clarify_without_spec` (existing behavior, a task not parked is unaffected). In `test_core_tasks.py`: `test_other_chat_can_edit_parked_note` and `test_other_chat_cannot_edit_unclaimed_open_task` (still refused with `no_edit_rights`).
- [ ] **Step 2: Run** `python -m unittest tests.test_core_states tests.test_core_tasks -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement.** Pass `cfg` from `set_fields` and `set_state` into `_require_edit_rights`. Add the gate in `set_state` after the existing `requires` loop, keyed on the current state's kind and the target's kind; one refusal path through `_refuse`.
- [ ] **Step 4: Run** the same command, then the whole suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): chats specify notes; leaving the backlog needs the spec`.

---

### Task 4: MCP and the session-start line

**Files:**
- Modify: `coordinator/mcp_server.py` (`task_create` schema at 42, handler at 213)
- Modify: `coordinator/hooks.py` (`_howto` at 111)
- Test: `tests/test_mcp.py`, `tests/test_hooks.py`

**Interfaces:**
- Consumes: `core.create_task(..., backlog=True)` (Task 2).
- Produces: MCP `task_create` accepts optional boolean `backlog`; its description says a note for later is created with `backlog: true`, needs only a title, and is not claimable until specified. `_howto` counts tasks that are neither final nor parked.

- [ ] **Step 1: Write failing tests.** `test_mcp.py`: `test_task_create_backlog_note` (call with `backlog=True`, title only; result state `backlog`); `test_task_create_backlog_missing_state_is_error` (error text has three lines). `test_hooks.py`: `test_session_start_count_ignores_backlog` (one open task and two notes: the line says "has 1 open task(s)").
- [ ] **Step 2: Run** `python -m unittest tests.test_mcp tests.test_hooks -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement.** Boolean schema property `backlog` (not in `required`); pass `backlog=bool(a.get("backlog"))`. In `_howto` exclude states whose kind is `parked`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(mcp,hooks): create notes through task_create; notes are not open work`.

---

### Task 5: The Backlog fold on the project board

**Files:**
- Modify: `coordinator/board.py` (`api_project` at 476; `api_task` uses `kind` already)
- Modify: `coordinator/pages.py` (`KIND_LOOK` at 505, `page_project` at 658)
- Create: `coordinator/web/templates/_backlog.html`, `coordinator/web/templates/_backlog_item.html` (modelled on `_inbox.html` and `_inbox_item.html`, no Pin button, an Open link, title help text "Ideas that are not specified yet.")
- Test: `tests/test_board.py` (or a new `tests/test_board_backlog.py`, reusing `BoardBase`)

**Interfaces:**
- Consumes: parked tasks from Task 2.
- Produces: `api_project(...)["backlog"]`: list of card dicts as in `inbox` (`id`, `title`, `created`, `created_by_type`, `created_by_id`, plus the `_card` fields), newest `created` first; parked-kind states no longer appear in `["columns"]`. `KIND_LOOK["parked"] = ("k-wait", "inbox", "Parked")` (reuse existing class and icon). `page_project` renders the fold with `id="backlog"`, collapsed by default, count in the summary.

- [ ] **Step 1: Write failing tests.** `test_project_api_has_backlog_newest_first`; `test_backlog_state_is_not_a_column`; `test_project_page_shows_backlog_fold` (body has `id="backlog"`, the note's `/t/<id>` link, the count, and the fold is not `open`); `test_board_with_only_notes_is_not_empty` (no empty-board markup); `test_task_page_of_note_has_no_start` (`can_start` is False in `/api/task/<id>`); `test_parked_task_page_shows_state_word` (body contains "Parked" or the label `Backlog`, never "Unknown kind").
- [ ] **Step 2: Run** `python -m unittest tests.test_board_backlog -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the API change (split parked tasks out of `columns`, newest first), the `KIND_LOOK` entry, the two templates and the `page_project` wiring. The empty-board check must also count `d["backlog"]`.
- [ ] **Step 4: Run** the same command, then the whole suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(board): Backlog fold on the project board`.

---

### Task 6: Backlog count link on the home cards

**Files:**
- Modify: `coordinator/board.py` (`api_index` at 135)
- Modify: `coordinator/pages.py` (`_project_card` at 419)
- Modify: `coordinator/web/templates/_project_card.html` (a `$backlog` slot in the footer)
- Test: `tests/test_board_home.py`

**Interfaces:**
- Consumes: parked tasks (Task 2), the `id="backlog"` fold (Task 5).
- Produces: each `api_index()["projects"][i]["backlog"]` is an int (count of parked tasks). `_project_card` shows a link `Backlog N` to `/p/<key>#backlog` only when N > 0.

- [ ] **Step 1: Write failing tests** in `test_board_home.py`: `test_card_shows_backlog_count_link` (two notes in PA: body contains `href="/p/PA#backlog"` and `Backlog 2`); `test_card_without_notes_has_no_backlog_link`; `test_notes_never_count_as_clarify` (a note does not appear in `api_index()["clarify"]` and the headline is unchanged); `test_home_still_reads_no_outside_sources` (existing guard test must still pass).
- [ ] **Step 2: Run** `python -m unittest tests.test_board_home -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement.** Count parked tasks per project in `api_index` (via `core.list_tasks` and the project's kinds, config errors count 0); wire the link into the card footer.
- [ ] **Step 4: Run** the same command, then the whole suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(board): backlog count link on home cards`.

---

### Task 7: Roll out and close the loop (needs Daria's yes for the writes outside this repo)

**Files:**
- Modify: `C:\Projects\raiffeisen-sme-insurance\.coordinator\project.toml` (KMU), `C:\Projects\_coordinator-trial\.coordinator\project.toml` (TRY): add the `backlog` state (kind `parked`) before `open`. Read each file first and keep its own states and `claim` setting. Ask before editing files outside this repo.
- Modify: `README.md` ("Backlog ideas" note: point at the new backlog item; add a short "Backlog" section: how to create a note, how it leaves)
- Modify: `docs/superpowers/specs/2026-11-13-backlog-state-design.md` (decision 5: the rule lives in core, see "Spec change" below)

**Interfaces:** none new.

- [ ] **Step 1: Edit** the two project files (after confirmation) and run `python -m coordinator.cli` project check if one exists (`python -m unittest discover -s tests -v` otherwise) to confirm each loads.
- [ ] **Step 2: Create the backlog item** for the old overview through MCP `task_create` with `project="COORD"`, `backlog=true`, title "Broader needs-you overview (stopped, blocked, mode guard, wiki/desk)", `origin` "COORD-5 design talk, 2026-11-13; old code under BACKLOG(home), see git history". Then update the README note to name that task id.
- [ ] **Step 3: Rewrite COORD-5** with `task_update` so its goal and done-when match the approved design (not the old overview).
- [ ] **Step 4: Live check** in the browser at the board URL: create a note from a chat, see it in the fold and on the home count, try to claim it (refused), specify it from another chat and move it to open, drop a second note.
- [ ] **Step 5: Run** `python -m unittest discover -s tests -v`; all PASS. **Commit** `docs: backlog in README, design update`.

## Spec change

Design decision 5 said `open` gets `requires = [goal, done_when, scope, note]`. That would also block the person moving a task back to `open` after a hand-off. The rule now lives in core: **leaving a parked state for work needs goal, done_when, scope, and, for a chat, a note.** `open` in the config is unchanged.

## Self-review

- Spec coverage: create note (T2, T4), real task and history (T2), not counted or claimable (T2, T4), leaves only specified plus agreed (T3), fold and count link (T5, T6), config per project with refusal (T1, T2, T7), newest first (T5), backlog item and README (T7), old comments stay (no task touches them).
- Names checked across tasks: `is_parked`, `backlog` argument, `_require_edit_rights(..., cfg=None)`, `api_project()["backlog"]`, `api_index()["projects"][i]["backlog"]`, anchor `#backlog`.
- Added beyond the spec, on purpose: chats may edit any parked note without a claim (needed so a later chat can specify it); notes count toward the chat create limit of 3.
