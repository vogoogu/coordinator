"""Refusal texts: every refusal says WHAT happened, WHY, and how to FIX it."""


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


def project_config_bad(root, detail):
    return CoordinatorError(
        f"The project at {root} was not added.",
        f"Its configuration cannot be used: {detail}",
        "Fix .coordinator\\project.toml in that folder and try again.")


def create_limit(limit):
    return CoordinatorError(
        "The task was not created.",
        f"A chat session may create at most {limit} tasks and this one has reached the limit.",
        "Ask the person to create it on the board or command line.")
