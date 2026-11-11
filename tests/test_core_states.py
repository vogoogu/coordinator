import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from coordinator import core, db
from coordinator.messages import CoordinatorError

PERSON = core.Actor("person", "daria")


def chat(n="s1"):
    return core.Actor("chat", n)


class CoreStatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_states_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = os.path.join(self.tmp, "home")
        self.conn = db.connect()
        self.addCleanup(self._restore)
        self.addCleanup(self.conn.close)
        root = Path(self.tmp, "kmu")
        (root / ".coordinator").mkdir(parents=True)
        (root / ".coordinator" / "project.toml").write_text(
            'key = "KMU"\nname = "K"\n', encoding="utf-8")
        core.add_project(self.conn, str(root), PERSON)

    def _restore(self):
        if self._old is None:
            os.environ.pop("COORDINATOR_HOME", None)
        else:
            os.environ["COORDINATOR_HOME"] = self._old

    def task(self, actor=PERSON, **kw):
        return core.create_task(self.conn, "KMU", "t", actor, **kw)

    def claim(self, display_id, session, ended=None):
        t = core.get_task(self.conn, display_id)
        self.conn.execute(
            "INSERT INTO claim (task_id, session_id, started, last_seen, ended)"
            " VALUES (?,?,?,?,?)", (t["pk"], session, "x", "x", ended))

    def events(self, display_id, action=None):
        h = core.get_task(self.conn, display_id)["history"]
        return [e for e in h if action is None or e["action"] == action]

    def assertRefused(self, fn, *args, why=None, **kw):
        with self.assertRaises(CoordinatorError) as cm:
            fn(*args, **kw)
        lines = str(cm.exception).splitlines()
        self.assertTrue(lines[0].startswith("WHAT:"))
        self.assertTrue(lines[1].startswith("WHY:"))
        self.assertTrue(lines[2].startswith("FIX:"))
        if why:
            self.assertIn(why, str(cm.exception))
        return cm.exception

    def test_compare_and_set_refuses_when_changed(self):
        t = self.task()
        core.set_state(self.conn, t["id"], "clarify", PERSON, note="why")
        self.assertRefused(core.set_state, self.conn, t["id"], "review", PERSON,
                           expected_state="open", note="n", why="changed")
        self.assertEqual(core.get_task(self.conn, t["id"])["state"], "clarify")
        self.assertEqual(len(self.events(t["id"], "refused")), 1)

    def test_compare_and_set_ok_when_matching(self):
        t = self.task()
        r = core.set_state(self.conn, t["id"], "clarify", PERSON,
                           expected_state="open", note="why")
        self.assertEqual(r["state"], "clarify")
        self.assertEqual(r["version"], 2)

    def test_unknown_state_lists_valid(self):
        t = self.task()
        self.assertRefused(core.set_state, self.conn, t["id"], "nope", PERSON,
                           why="Use one of: open, ready")
        self.assertEqual(len(self.events(t["id"], "refused")), 1)

    def test_chat_cannot_set_done(self):
        t = self.task(chat())
        self.assertRefused(core.set_state, self.conn, t["id"], "done", chat())
        self.assertRefused(core.set_state, self.conn, t["id"], "dropped", chat(),
                           note="x")
        got = core.get_task(self.conn, t["id"])
        self.assertEqual(got["state"], "open")
        self.assertEqual(got["version"], 1)
        ev = self.events(t["id"], "refused")
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0]["actor_type"], "chat")

    def test_person_can_set_done(self):
        t = self.task()
        core.set_state(self.conn, t["id"], "done", PERSON)
        ev = self.events(t["id"], "state")
        self.assertEqual(ev[-1]["actor_type"], "person")
        self.assertEqual((ev[-1]["from_value"], ev[-1]["to_value"]), ("open", "done"))

    def test_chat_active_needs_own_live_claim(self):
        t = self.task(chat())
        self.assertRefused(core.set_state, self.conn, t["id"], "building", chat())
        self.claim(t["id"], "s1", ended="2026-01-01")  # ended claim does not count
        self.assertRefused(core.set_state, self.conn, t["id"], "building", chat())
        self.claim(t["id"], "s1")
        self.assertEqual(
            core.set_state(self.conn, t["id"], "building", chat())["state"],
            "building")

    def test_chat_handover_with_note_ok_and_note_in_history(self):
        t = self.task(chat())
        core.set_state(self.conn, t["id"], "review", chat(), note="please check")
        self.assertEqual(self.events(t["id"], "state")[-1]["note"], "please check")

    def test_ready_requires_goal_done_when_scope(self):
        t = self.task(goal="g", scope="s")
        e = self.assertRefused(core.set_state, self.conn, t["id"], "ready", PERSON,
                               why="done_when")
        self.assertIn("done_when", str(e))
        core.set_fields(self.conn, t["id"], PERSON, done_when="d")
        self.assertEqual(
            core.set_state(self.conn, t["id"], "ready", PERSON)["state"], "ready")

    def test_dropped_requires_note(self):
        t = self.task()
        self.assertRefused(core.set_state, self.conn, t["id"], "dropped", PERSON,
                           why="note")
        self.assertRefused(core.set_state, self.conn, t["id"], "dropped", PERSON,
                           note="   ", why="note")
        r = core.set_state(self.conn, t["id"], "dropped", PERSON, note="not needed")
        self.assertEqual(r["state"], "dropped")

    def test_chat_cannot_raise_mode_but_can_lower(self):
        t = self.task(chat())
        self.assertRefused(core.set_mode, self.conn, t["id"], "write", chat())
        self.assertEqual(core.get_task(self.conn, t["id"])["mode"], "read")
        self.assertEqual(len(self.events(t["id"], "refused")), 1)
        core.set_mode(self.conn, t["id"], "write", PERSON)
        self.assertEqual(
            core.set_mode(self.conn, t["id"], "read", chat())["mode"], "read")
        ev = self.events(t["id"], "mode")
        self.assertEqual([(e["from_value"], e["to_value"]) for e in ev],
                         [("read", "write"), ("write", "read")])

    def test_unknown_mode_lists_valid(self):
        t = self.task()
        self.assertRefused(core.set_mode, self.conn, t["id"], "root", PERSON,
                           why="read, write")

    def test_error_message_has_what_why_fix(self):
        t = self.task(chat())
        with self.assertRaises(CoordinatorError) as cm:
            core.set_state(self.conn, t["id"], "done", chat())
        lines = str(cm.exception).splitlines()
        self.assertTrue(lines[0].startswith("WHAT:"))
        self.assertEqual(len(lines), 3)

    def test_chat_rights_r3(self):
        mine = self.task(chat("s1"))
        theirs = self.task(PERSON)
        # no claim, not creator -> refused; creator without claims -> ok
        self.assertRefused(core.set_fields, self.conn, theirs["id"], chat("s1"),
                           goal="x")
        core.set_fields(self.conn, mine["id"], chat("s1"), goal="x")
        # another session's live claim blocks even the creator, WHY names holder
        self.claim(mine["id"], "s2")
        self.assertRefused(core.set_fields, self.conn, mine["id"], chat("s1"),
                           goal="y", why="s2")
        # own claim on a task created by a person is enough
        self.claim(theirs["id"], "s1")
        core.set_fields(self.conn, theirs["id"], chat("s1"), goal="z")
        # every flag operation applies the same rule
        for fn, args in ((core.set_blocked, ("r",)), (core.set_open_points, (["a"],)),
                         (core.set_mode, ("read",))):
            self.assertRefused(fn, self.conn, mine["id"], chat("s1"), *args) \
                if fn is not core.set_mode else \
                self.assertRefused(fn, self.conn, mine["id"], "read", chat("s1"))

    def test_chat_can_comment_without_claim_with_allowed_kinds(self):
        t = self.task(PERSON)
        for kind in ("comment", "question", "answer"):
            c = core.comment(self.conn, t["id"], chat(), kind, "hi")
            self.assertEqual(c["kind"], kind)
        for kind in ("handoff", "progress", "bogus"):
            self.assertRefused(core.comment, self.conn, t["id"], chat(), kind, "x")
        self.assertRefused(core.comment, self.conn, t["id"], chat(), "comment", " ")
        got = core.get_task(self.conn, t["id"])
        self.assertEqual(len(got["comments"]), 3)
        self.assertEqual(len(self.events(t["id"], "refused")), 3 + 1)
        core.comment(self.conn, t["id"], PERSON, "handoff", "ok")

    def test_set_fields_one_event_per_changed_field(self):
        t = self.task(goal="old")
        r = core.set_fields(self.conn, t["id"], PERSON, goal="new", scope="s",
                            title="t")
        self.assertEqual((r["goal"], r["scope"]), ("new", "s"))
        self.assertEqual(r["version"], 2)
        ev = self.events(t["id"], "fields")
        self.assertEqual({(e["note"], e["from_value"], e["to_value"]) for e in ev},
                         {("goal", "old", "new"), ("scope", "", "s")})
        self.assertRefused(core.set_fields, self.conn, t["id"], PERSON, bogus="x")
        self.assertRefused(core.set_fields, self.conn, t["id"], PERSON, title="")

    def test_blocked_and_open_points_are_recorded_in_history(self):
        t = self.task()
        core.set_blocked(self.conn, t["id"], PERSON, "waiting for data")
        self.assertEqual(core.get_task(self.conn, t["id"])["blocked"],
                         "waiting for data")
        core.set_blocked(self.conn, t["id"], PERSON, None)
        self.assertIsNone(core.get_task(self.conn, t["id"])["blocked"])
        ev = self.events(t["id"], "blocked")
        self.assertEqual([(e["from_value"], e["to_value"]) for e in ev],
                         [(None, "waiting for data"), ("waiting for data", None)])
        r = core.set_open_points(self.conn, t["id"], PERSON, ["a", "b"])
        self.assertEqual(r["open_points"], ["a", "b"])
        raw = self.conn.execute("SELECT open_points FROM task").fetchone()[0]
        self.assertEqual(json.loads(raw), ["a", "b"])
        ev = self.events(t["id"], "open_points")
        self.assertEqual(json.loads(ev[-1]["to_value"]), ["a", "b"])

    def test_pin_removes_inbox_and_is_person_only(self):
        t = self.task(chat(), inbox=True)
        self.assertTrue(t["inbox"])
        self.assertRefused(core.pin, self.conn, t["id"], chat())
        self.assertTrue(core.get_task(self.conn, t["id"])["inbox"])
        self.assertEqual(len(self.events(t["id"], "refused")), 1)
        r = core.pin(self.conn, t["id"], PERSON)
        self.assertFalse(r["inbox"])
        self.assertEqual(len(self.events(t["id"], "pin")), 1)

    def test_version_and_updated_increment(self):
        t = self.task()
        r = core.set_blocked(self.conn, t["id"], PERSON, "x")
        r = core.set_mode(self.conn, t["id"], "write", PERSON)
        self.assertEqual(r["version"], 3)

    def test_unknown_task(self):
        self.assertRefused(core.pin, self.conn, "KMU-99", PERSON)


if __name__ == "__main__":
    unittest.main()
