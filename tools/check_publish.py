#!/usr/bin/env python3
r"""Is the coordinator safe to push? Run it before every `git push`; push only when it says OK.

Checks the files that would ship (everything tracked at HEAD except tests/fixtures and the
history under docs/superpowers) for: a `C:\Users\<name>` or OneDrive path, client names from
the banned-terms list, e-mail addresses that are not dummies, and files that hold one person's
settings or data (sources.toml, antson.toml, notify.toml, database files). Then runs the tests.

The client terms are client names themselves, so they never live in this repo: they are read
from the antson-helper skill repo on this machine (override with ANTSON_BANNED_TERMS). Without
that file the check cannot run and says so.

Usage: python tools/check_publish.py [--skip-tests]
Exit code: 0 = OK to push, 1 = problems found, 2 = the check itself could not run.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TERMS = Path(r"C:\Projects\antson-helper\tools\banned_terms.txt")
EXCLUDE = [":(exclude)tests/fixtures", ":(exclude)docs/superpowers", ":(exclude)tools/check_publish.py"]
USER_PATH = r"[A-Za-z]:[\/]+Users[\/]+[^\/ \"'<>]+|OneDrive - Specific-Group"
EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
DUMMY_DOMAINS = {"b.at", "example.invalid", "example.test", "example.com", "anthropic.com"}
ALLOWED_ADDRESSES = {"git@scm.specific-group.at", "noreply@anthropic.com"}
PERSONAL_FILES = re.compile(r"(^|/)(sources|antson|notify)\.toml$|\.(db|db-wal|db-shm|sqlite)$", re.I)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def load_terms() -> list[str] | None:
    path = Path(os.environ.get("ANTSON_BANNED_TERMS", DEFAULT_TERMS))
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def scan(terms: list[str]) -> list[str]:
    problems = []
    for name in git("ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines():
        if PERSONAL_FILES.search(name):
            problems.append(f"personal file is tracked: {name}")
    for hit in git("grep", "-I", "-n", "-o", "-E", USER_PATH, "HEAD", "--", ".", *EXCLUDE).stdout.splitlines():
        problems.append("personal path: " + hit.split(":", 1)[1])
    for hit in git("grep", "-I", "-n", "-o", "-E", EMAIL, "HEAD", "--", ".", *EXCLUDE).stdout.splitlines():
        address = hit.rsplit(":", 1)[-1].lower()
        if address not in ALLOWED_ADDRESSES and address.rsplit("@", 1)[1] not in DUMMY_DOMAINS:
            problems.append("e-mail: " + hit.split(":", 1)[1])
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(terms) + "\n")
        terms_file = fh.name
    try:
        for hit in git("grep", "-I", "-i", "-n", "-F", "-f", terms_file, "HEAD", "--", ".", *EXCLUDE).stdout.splitlines():
            problems.append("client term: " + hit.split(":", 1)[1][:160])
    finally:
        os.unlink(terms_file)
    return problems


def main(argv: list[str]) -> int:
    if git("status", "--porcelain").stdout.strip():
        print("STOP: there are uncommitted changes. Commit them first, so the check covers exactly what you push.")
        return 1
    terms = load_terms()
    if terms is None:
        print(f"CANNOT CHECK: banned terms file not found ({os.environ.get('ANTSON_BANNED_TERMS', DEFAULT_TERMS)}).")
        return 2
    problems = scan(terms)
    if problems:
        print(f"STOP: {len(problems)} problem(s) in the files that would ship:")
        print("\n".join("  " + p for p in problems[:60]))
        return 1
    print(f"Scan OK: branch {git('branch', '--show-current').stdout.strip()}, {len(terms)} terms checked.")
    if "--skip-tests" not in argv:
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print("STOP: tests failed.\n" + result.stderr[-1500:])
            return 1
        print("Tests OK.")
    print("OK, you can push.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
