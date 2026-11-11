import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from coordinator import core, db

PERSON = core.Actor("person", "daria")


def chat(n="s1"):
    return core.Actor("chat", n)


class CoreTasksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_core_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = os.path.join(self.tmp, "home")
        self.conn = db.connect()
        self.addCleanup(self._restore)
        self.addCleanup(self.conn.close)

    def _restore(self):
        if self._old is None:
            os.environ.pop("COORDINATOR_HOME", None)
        else:
            os.environ["COORDINATOR_HOME"] = self._old

    def project(self, key="KMU", sub=None):
        root = Path(self.tmp, sub or key.lower())
        (root / ".coordinator").mkdir(parents=True, exist_ok=True)
        (root / ".coordinator" / "project.toml").write_text(
            f'key = "{key}"\nname = "{key} project"\n', encoding="utf-8")
        return core.add_project(self.conn, str(root), PERSON)

    def test_schema_and_pragmas(self):
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("project", "task", "event", "comment", "claim",
                  "session_stat", "setting"):
            self.assertIn(t, names)
        db.connect().close()  # idempotent

    def test_migration_adds_claim_launch_to_old_database(self):
        old = Path(self.tmp, "old.db")
        c = sqlite3.connect(str(old))
        c.executescript(
            "CREATE TABLE claim (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL,"
            " session_id TEXT NOT NULL, transcript_path TEXT, cwd TEXT,"
            " started TEXT NOT NULL, last_seen TEXT NOT NULL, ended TEXT,"
            " end_reason TEXT);"
            "INSERT INTO claim (task_id, session_id, started, last_seen)"
            " VALUES (1, 's', 'x', 'x');"
            "PRAGMA user_version = 2;")
        c.close()
        conn = db.connect(old)
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(claim)")}
            self.assertIn("launch", cols)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             db.SCHEMA_VERSION)
            self.assertGreaterEqual(db.SCHEMA_VERSION, 3)
            row = conn.execute("SELECT session_id, launch FROM claim").fetchone()
            self.assertEqual((row["session_id"], row["launch"]), ("s", None))
        finally:
            conn.close()
        db.connect(old).close()  # idempotent

    def test_create_assigns_number_per_project(self):
        self.project("KMU")
        self.project("ABC")
        self.assertEqual(core.create_task(self.conn, "KMU", "a", PERSON)["id"], "KMU-1")
        self.assertEqual(core.create_task(self.conn, "KMU", "b", PERSON)["id"], "KMU-2")
        self.assertEqual(core.create_task(self.conn, "ABC", "c", PERSON)["id"], "ABC-1")

    def test_create_writes_created_event(self):
        self.project()
        t = core.create_task(self.conn, "KMU", "a", chat())
        h = core.get_task(self.conn, t["id"])["history"]
        self.assertEqual(h[0]["action"], "created")
        self.assertEqual(h[0]["actor_type"], "chat")
        self.assertEqual(h[0]["actor_id"], "s1")

    def test_new_task_is_read_mode_and_first_waiting_state(self):
        self.project()
        t = core.create_task(self.conn, "KMU", "a", PERSON)
        self.assertEqual(t["mode"], "read")
        self.assertEqual(t["state"], "open")
        self.assertEqual(t["open_points"], [])
        self.assertEqual(t["project"], "KMU")

    def test_chat_and_person_tasks_start_open_not_in_inbox(self):
        self.project()
        c = core.create_task(self.conn, "KMU", "a", chat())
        p = core.create_task(self.conn, "KMU", "b", PERSON)
        self.assertIs(c["inbox"], False)
        self.assertIs(p["inbox"], False)
        self.assertEqual(c["state"], "open")
        self.assertEqual(core.list_tasks(self.conn, inbox=True), [])
        forced = core.create_task(self.conn, "KMU", "c", chat(), inbox=True)
        self.assertIs(forced["inbox"], True)
        self.assertEqual([t["id"] for t in core.list_tasks(self.conn, inbox=True)],
                         [forced["id"]])

    def test_chat_cannot_create_more_than_three_per_session(self):
        self.project()
        for i in range(3):
            core.create_task(self.conn, "KMU", f"t{i}", chat())
        with self.assertRaises(core.CoordinatorError) as cm:
            core.create_task(self.conn, "KMU", "t4", chat())
        self.assertIn("3", cm.exception.why)
        self.assertIn("board or command line", cm.exception.fix)
        lines = str(cm.exception).split("\n")
        self.assertEqual([l.split(":")[0] for l in lines], ["WHAT", "WHY", "FIX"])
        # refused event persisted, no 4th task
        ev = self.conn.execute(
            "SELECT * FROM event WHERE action='refused'").fetchall()
        self.assertEqual(len(ev), 1)
        self.assertIsNone(ev[0]["task_id"])
        self.assertEqual(len(core.list_tasks(self.conn)), 3)
        # other session and persons are not limited
        core.create_task(self.conn, "KMU", "x", chat("s2"))
        for i in range(5):
            core.create_task(self.conn, "KMU", f"p{i}", PERSON)

    def test_project_for_path_finds_nested_file(self):
        p = self.project()
        f = Path(p["root"], "sub", "deep", "x.txt")
        self.assertEqual(core.project_for_path(self.conn, str(f))["key"], "KMU")
        self.assertIsNone(core.project_for_path(self.conn, self.tmp))

    def test_nested_projects_deepest_wins(self):
        outer = self.project("OUT", "outer")
        self.project("INN", os.path.join("outer", "inner"))
        f = os.path.join(outer["root"], "inner", "a.txt")
        self.assertEqual(core.project_for_path(self.conn, f)["key"], "INN")

    def test_duplicate_project_key_refused(self):
        self.project("KMU")
        with self.assertRaises(core.CoordinatorError):
            self.project("KMU", "other")

    def test_duplicate_project_root_refused(self):
        self.project("KMU")
        with self.assertRaises(core.CoordinatorError):
            core.add_project(self.conn, str(Path(self.tmp, "kmu")), PERSON)

    def test_add_project_records_event(self):
        self.project()
        ev = self.conn.execute("SELECT * FROM event WHERE action='project_added'").fetchall()
        self.assertEqual(len(ev), 1)
        self.assertIsNone(ev[0]["task_id"])

    def test_get_task_case_insensitive_and_unknown(self):
        self.project()
        core.create_task(self.conn, "KMU", "a", PERSON)
        self.assertEqual(core.get_task(self.conn, "kmu-1")["id"], "KMU-1")
        for bad in ("KMU-9", "nonsense", "XXX-1"):
            with self.assertRaises(core.CoordinatorError):
                core.get_task(self.conn, bad)

    def test_get_task_has_history_comments_claims(self):
        self.project()
        t = core.get_task(self.conn, core.create_task(self.conn, "KMU", "a", PERSON)["id"])
        self.assertEqual(t["comments"], [])
        self.assertEqual(t["claims"], [])
        self.assertIn("pk", t)

    def test_list_tasks_filters(self):
        self.project("KMU")
        self.project("ABC")
        core.create_task(self.conn, "KMU", "a", PERSON)
        core.create_task(self.conn, "ABC", "b", PERSON)
        self.assertEqual(len(core.list_tasks(self.conn, project_key="ABC")), 1)
        self.assertEqual(len(core.list_tasks(self.conn, states=["open"])), 2)
        self.assertEqual(len(core.list_tasks(self.conn, states=["done"])), 0)

    def test_nothing_is_deleted(self):
        src = (Path(core.__file__)).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\bDELETE\b", src, re.I))
        for word in ("antson", "wiki", "telegram", "mail"):
            self.assertNotIn(word, src.lower())


if __name__ == "__main__":
    unittest.main()
