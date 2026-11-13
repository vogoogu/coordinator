"""Refusal texts: every refusal says WHAT happened, WHY, and how to FIX it."""
import os


class CoordinatorError(Exception):
    def __init__(self, what: str, why: str, fix: str):
        self.what, self.why, self.fix = what, why, fix
        super().__init__(what, why, fix)

    def __str__(self) -> str:
        return render(self.what, self.why, self.fix)


def render(what: str, why: str, fix: str) -> str:
    return f"WHAT: {what}\nWHY: {why}\nFIX: {fix}"


def unknown_task(display_id):
    return CoordinatorError(
        f"Task {display_id!r} was not found.",
        "No task has this id.",
        "List the tasks to see the valid ids, for example KMU-1.")


def unknown_project(key):
    return CoordinatorError(
        f"Project {key!r} is not registered.",
        "No project with this key exists.",
        "Register the project folder first, or check the key.")


def project_exists(key, root):
    return CoordinatorError(
        f"Project {key!r} at {root} was not added.",
        "A project with this key or this folder already exists.",
        "Use another key in .coordinator\\project.toml, or use the existing project.")


def project_config_bad(root, detail, what=None):
    """The project file of `root` is missing or broken. `what` says what was
    not done (default: the project was not added)."""
    file = os.path.join(str(root), ".coordinator", "project.toml")
    return CoordinatorError(
        what or f"The project at {root} was not added.",
        f"Its project file {file} cannot be used: {detail}",
        f"Fix {file} and try again.")


def create_limit(limit):
    return CoordinatorError(
        "The task was not created.",
        f"A chat session may create at most {limit} tasks and this one has reached the limit.",
        "Ask the person to create it on the board or command line.")


def claimed_by_other(display_id, holder):
    return CoordinatorError(
        f"{display_id} was not changed.",
        f"Another session ({holder}) holds a live claim on this task.",
        "Wait until that session releases it, or ask the person to step in.")


def no_edit_rights(display_id):
    return CoordinatorError(
        f"{display_id} was not changed.",
        "This session has no claim on the task and did not create it.",
        "Claim the task first, or add a comment instead.")


def stale_state(display_id, expected, current):
    return CoordinatorError(
        f"{display_id} was not moved.",
        f"The state was changed in between: you expected {expected!r}, it is now {current!r}.",
        "Read the task again and decide on the current state.")


def unknown_state(name, valid):
    return CoordinatorError(
        f"State {name!r} does not exist.",
        "The project does not define this state.",
        f"Use one of: {', '.join(valid)}.")


def chat_final_state(name, kind):
    return CoordinatorError(
        f"The task was not moved to {name!r}.",
        f"Only a person may set a {kind} state.",
        "Hand the task over for review instead and let the person decide.")


def cannot_close(display_id, why):
    return CoordinatorError(
        f"{display_id} was not marked done.",
        why,
        "Open the task and change its state there.")


def chat_needs_claim(name):
    return CoordinatorError(
        f"The task was not moved to {name!r}.",
        "Moving to an active state needs this session's own live claim on the task.",
        "Claim the task first.")


def requires_missing(name, field):
    return CoordinatorError(
        f"The task was not moved to {name!r}.",
        f"State {name!r} needs {field!r} to be filled in, and it is empty.",
        f"Fill in {field!r} first" + (" (give a note)." if field == "note" else "."))


def unknown_mode(mode, valid):
    return CoordinatorError(
        f"Mode {mode!r} does not exist.",
        "The project does not define this mode.",
        f"Use one of: {', '.join(valid)}.")


def chat_raise_mode(current, mode):
    return CoordinatorError(
        f"The mode was not changed to {mode!r}.",
        f"A chat may only lower the mode, and {mode!r} is higher than {current!r}.",
        "Ask the person to raise the mode.")


def unknown_comment_kind(kind, valid):
    return CoordinatorError(
        "The comment was not added.",
        f"Kind {kind!r} is not known.",
        f"Use one of: {', '.join(valid)}.")


def chat_comment_kind(kind, allowed):
    return CoordinatorError(
        "The comment was not added.",
        f"A chat may not write comments of kind {kind!r}; those come from releasing a task.",
        f"Use one of: {', '.join(allowed)}.")


def empty_value(what):
    return CoordinatorError(
        f"Nothing was saved: {what} is empty.",
        f"{what.capitalize()} must contain some text.",
        "Provide a non-empty value.")


def unknown_field(name, valid):
    return CoordinatorError(
        f"Field {name!r} cannot be set.",
        "It is not an editable task field.",
        f"Use one of: {', '.join(valid)}.")


def bad_open_points(detail):
    return CoordinatorError(
        "The open points were not saved.",
        f"They must be a list of text entries: {detail}",
        "Pass a list of strings.")


def windows_only(what):
    return CoordinatorError(
        f"{what} only works on Windows.",
        "The pop-up branding is a Windows registry entry.",
        "Run it on the Windows laptop.")


def person_only(what):
    return CoordinatorError(
        f"{what} was not done.",
        "Only a person may do this.",
        "Ask the person to do it on the board or command line.")


class ClaimError(CoordinatorError):
    """A claim was refused."""


def already_claimed(display_id, holder):
    return ClaimError(
        f"{display_id} was not claimed.",
        f"It is already claimed by {holder}.",
        "Pick another task, or wait until that session releases it.")


def claim_final(display_id, state):
    return ClaimError(
        f"{display_id} was not claimed.",
        f"The task is in state {state!r}, which is finished or closed.",
        "Ask the person to reopen it, or pick another task.")


def session_has_claim(display_id, held):
    return ClaimError(
        f"{display_id} was not claimed.",
        f"This session already holds a live claim on {held}; a chat works on one task.",
        f"Release {held} first with outcome done or clarify (progress keeps the "
        "claim), then claim the next task.")


def not_your_claim(display_id):
    return CoordinatorError(
        f"{display_id} was not released.",
        "This session holds no live claim on the task.",
        "Only the session that claimed a task can release it; ask the person otherwise.")


def unknown_outcome(outcome, valid):
    return CoordinatorError(
        "The task was not released.",
        f"Outcome {outcome!r} is not known.",
        f"Use one of: {', '.join(valid)}.")


def no_live_claim(display_id):
    return CoordinatorError(
        f"{display_id} was not released.",
        "Nobody holds a live claim on this task.",
        "Nothing to release.")


def cannot_start(display_id, reason):
    return CoordinatorError(
        f"Task {display_id} was not started.",
        reason,
        "Start only tasks in a waiting state that no chat holds; release it first if needed.")


def modes_nothing_selected():
    return CoordinatorError(
        "No mode was changed.",
        "No row was selected.",
        "Tick the rows first, or use the buttons on a single row.")


def modes_bad_request():
    return CoordinatorError(
        "No mode was changed.",
        "The request names an unknown mode or an id that is not a form or interface id.",
        "Use the buttons on the Modes page.")


def modes_busy():
    return CoordinatorError(
        "No mode was changed.",
        "An earlier change is still waiting for your answer in its window.",
        "Answer the window on your desktop (Yes or No), or wait for it to time out after 60 s.")


def board_port_taken(port):
    return CoordinatorError(
        f"The board was not started on port {port}.",
        "Another program is already listening on this port, most likely a board that is still running.",
        f"Stop the other board (or use the one that is running), or start this one with --port <other port>.")


def make_task_no_project(name):
    return CoordinatorError(
        f"No task was made from this item of {name}.",
        f"{name} is not registered in the coordinator, so there is no project to put the task in.",
        "Register the project folder first: python -m coordinator add-project <folder>")


def make_task_exists(name, display_id):
    return CoordinatorError(
        "No new task was made.",
        f"This item already became {display_id} and that task is still open.",
        f"Open {display_id} on the board, or close it first.")


def source_unreadable(source, reason):
    return f"source {source} could not be read: {reason}"


def bad_role(role):
    return CoordinatorError(
        f"The comment was not added: unknown role {role!r}.",
        "A helper role is spec, or build / test with the round number.",
        "Pass role as spec, build 1, test 1, build 2 and so on, or leave it out.")


def bad_forms(detail):
    return CoordinatorError(
        "The forms were not changed.",
        f"Forms must be a list of text entries, got {detail}.",
        "Pass forms as a list of strings, for example [\"497\", \"458\"].")
