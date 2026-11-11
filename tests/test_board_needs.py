"""Needs you from outside the task database: wiki items, feedback, desk, folders,
learnings, "Make task". Sources are fake scripts in a temp folder; the real
projects and their data are never touched."""
import json
import os
import textwrap
import unittest
from pathlib import Path

from coordinator import antson, board, sources, wiki
from tests import test_board

FAKE_STATE = textwrap.dedent('''
    import json, sys
    a = sys.argv[1:]
    if "--items" in a:
        if "--detail" in a and %(detail_fails)s:
            sys.exit(3)
        d = "--detail" in a
        row = {"project": "x", "kind": "conflict", "id": "C22", "title": "Recipient: who gets it?",
               "since": "2026-10-28", "since_exact": False, "waits_on": "me",
               "explanation": "Two pages disagree." if d else "",
               "quotes": [{"label": "A", "source": "/a.md", "text": "Quote <b>A</b>"}] if d else [],
               "labels": [{"label": "Affects", "value": "3 pages", "tone": "alert"}],
               "secret_body": "never shown"}
        print(json.dumps({"items": [row, {"project": "x", "kind": "fork", "id": "F1", "title": "Pick one",
                                           "waits_on": "client", "since": "2026-11-09"}]}))
    else:
        print(json.dumps({"forks": {"operator": 0}, "backup_stale": True, "uncommitted": 2}))
''')
FAKE_TODO = ("import json; print(json.dumps([{'id': 'T-1', 'title': 'Change me', 'state': 'todo', "
             "'created': '2026-11-01'}, {'id': 'T-2', 'title': 'Done', 'state': 'dropped'}]))")
FAKE_FEEDBACK = ("import json; print(json.dumps({'meta': {'schedule_last_run': '2026-11-09T17:00:00'}, 'items': ["
                 "{'kind': 'digest-item', 'id': 'D-1', 'title': 'Empty entry', 'since': '2026-10-28', "
                 "'explanation': 'No text.'}]}))")


def q(p) -> str:
    return str(p).replace("\\", "/")


class NeedsBase(test_board.BoardBase):
    def setUp(self):
        super().setUp()
        self.fx = Path(self.tmp, "fx")
        self.fx.mkdir()
        self.proj = Path(self.roots["PA"])            # the registered coordinator project PA
        self.lonely = Path(self.tmp, "lonely")        # a wiki the coordinator does not know
        self.lonely.mkdir()
        for folder in (self.proj, self.lonely):
            (folder / "tools").mkdir(exist_ok=True)
            (folder / "tools" / "todo.py").write_text(FAKE_TODO, encoding="utf-8")
            self.write_state(folder, False)
            (folder / "desk.json").write_text(json.dumps({"project_dir": q(folder)}), encoding="utf-8")
        reg = [{"name": "kmu", "config": str(self.proj / "desk.json")},
               {"name": "other wiki", "config": str(self.lonely / "desk.json")}]
        (self.fx / "projects.json").write_text(json.dumps(reg), encoding="utf-8")
        (self.fx / "feedback.py").write_text(FAKE_FEEDBACK, encoding="utf-8")
        self.inbox = Path(self.tmp, "learnings", "inbox")
        (self.inbox / "2026-11-10").mkdir(parents=True)
        (self.inbox / "a.md").write_text("x", encoding="utf-8")
        (self.inbox / "2026-11-10" / "b.md").write_text("x", encoding="utf-8")
        self.intake = Path(self.tmp, "intake")
        self.intake.mkdir()
        (self.intake / "mail.eml").write_text("x", encoding="utf-8")
        self.write_sources()

    def write_state(self, folder, detail_fails):
        (folder / "tools" / "project_state.py").write_text(
            FAKE_STATE % {"detail_fails": detail_fails}, encoding="utf-8")

    def write_sources(self):
        # no state_script: each project runs its own tools/project_state.py (the fixture's copy)
        toml = f"""registry = "{q(self.fx / 'projects.json')}"
timeout_s = 20
[feedback]
cmd = ["python", "{q(self.fx / 'feedback.py')}"]
cwd = "{q(self.fx)}"
[folders]
"Email intake" = "{q(self.intake)}"
"Missing inbox" = "{q(Path(self.tmp, 'nope'))}"
[learnings]
inbox = "{q(self.inbox)}"
"""
        (self.fx / "sources.toml").write_text(toml, encoding="utf-8")
        os.environ["COORDINATOR_SOURCES"] = str(self.fx / "sources.toml")
        self.addCleanup(os.environ.pop, "COORDINATOR_SOURCES", None)
        board._outside["data"] = None
        sources.forget()

    def make_task_form(self, **kw):
        form = {"token": self.token, "next": "/", "name": "kmu", "key": "PA", "kind": "conflict",
                "item": "C22", "title": "Recipient: who gets it?", "goal": "Two pages disagree.", **kw}
        return self.request("POST", "/a/make-task", form=list(form.items()), headers={"Origin": self.origin()})


class NeedsTest(NeedsBase):
    def card(self, name):
        out = self.get_json("/api/index")["outside"]
        return next((c for c in out["cards"] if c["name"] == name), None)

    @unittest.skip("BACKLOG(home): old home page content, see README Backlog ideas")

    def test_items_are_individual_with_detail_and_how_to(self):
        idx = self.get_json("/api/index")
        pa = next(p for p in idx["projects"] if p["key"] == "PA")
        items = {i["id"]: i for i in pa["wiki"]["items"]}
        c = items["C22"]
        self.assertEqual(c["explanation"], "Two pages disagree.")
        self.assertEqual(c["quotes"][0]["text"], "Quote <b>A</b>")
        self.assertIn("/conflicts", c["how_to_act"])
        self.assertIn(pa["wiki"]["project_dir"], c["how_to_act"])
        self.assertNotIn("secret_body", json.dumps(idx))
        self.assertEqual(items["F1"]["group"], "others")
        self.assertEqual(items["T-1"]["kind"], "change_open")
        self.assertIn("todo.py request T-1", items["T-1"]["how_to_act"])
        self.assertNotIn("T-2", items)
        self.assertGreaterEqual(pa["needs_count"], 2)

    @unittest.skip("BACKLOG(home): old home page content, see README Backlog ideas")

    def test_project_not_in_coordinator_gets_its_own_card(self):
        c = self.card("other wiki")
        self.assertIsNotNone(c)
        self.assertIsNone(c["key"])
        status, html, _ = self.get("/")
        self.assertIn("other wiki", html)
        self.assertIn("This project is not registered in the coordinator.", html)

    @unittest.skip("BACKLOG(home): old home page content, see README Backlog ideas")

    def test_index_page_shows_quote_how_to_copy_and_make_task_escaped(self):
        status, html, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Quote &lt;b&gt;A&lt;/b&gt;", html)
        self.assertNotIn("Quote <b>A</b>", html)
        self.assertIn('data-copy="Open ', html)
        self.assertIn("Make task", html)
        self.assertIn("date estimated", html)
        self.assertIn("Affects: 3 pages", html)
        self.assertIn("Email intake", html)
        self.assertIn("folder not found", html)

    @unittest.skip("BACKLOG(home): old home page content, see README Backlog ideas")

    def test_feedback_card_and_learnings(self):
        c = self.card("Companion and skills")
        self.assertTrue(c["fixed"])
        self.assertEqual(c["items"][0]["id"], "D-1")
        status, html, _ = self.get("/")
        self.assertIn("logo-beta.png", html)
        self.assertIn("2 files in the learnings inbox", html)
        self.assertIn("last ran 2026-11-09 17:00", html)

    def test_learnings_count_includes_day_folders_and_empty_is_quiet(self):
        self.assertEqual(antson.unprocessed_learnings(str(self.inbox))["count"], 2)
        for f in list(self.inbox.rglob("*.md")):
            f.unlink()
        status, html, _ = self.get("/")
        self.assertNotIn("in the learnings inbox", html)
        self.assertIsNone(antson.unprocessed_learnings(""))
        self.assertFalse(antson.unprocessed_learnings(str(self.inbox / "gone"))["exists"])

    def test_needs_page_lists_all_groups_and_health(self):
        status, html, _ = self.get("/needs/kmu")
        self.assertEqual(status, 200)
        for word in ("Waiting for me", "Waiting for others", "no backup for more than a day",
                     "changes not saved yet in the project: 2"):
            self.assertIn(word, html)
        self.assertEqual(self.get("/needs/none")[0], 404)

    @unittest.skip("BACKLOG(home): old home page content, see README Backlog ideas")

    def test_failing_source_is_one_line_not_a_crash(self):
        (self.lonely / "tools" / "todo.py").write_text("import sys; sys.exit(2)", encoding="utf-8")
        status, html, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("source other wiki:todo could not be read: exit 2", html)

    @unittest.skip("BACKLOG(home): old home page content, see README Backlog ideas")

    def test_detail_failure_falls_back_to_plain_command(self):
        for f in (self.proj, self.lonely):
            self.write_state(f, True)
        pa = next(p for p in self.get_json("/api/index")["projects"] if p["key"] == "PA")
        c = next(i for i in pa["wiki"]["items"] if i["id"] == "C22")
        self.assertEqual(c["explanation"], "")          # plain output: no detail
        self.assertEqual(pa["wiki"]["notes"], [])

    @unittest.skip("BACKLOG(home): old home page content, see README Backlog ideas")

    def test_older_data_is_shown_when_a_source_breaks_later(self):
        self.get("/")
        (self.lonely / "tools" / "todo.py").write_text("import sys; sys.exit(2)", encoding="utf-8")
        status, html, _ = self.get("/?fresh=1")
        self.assertIn("showing older data of other wiki:todo", html)

    def test_nothing_is_written_to_disk(self):
        self.get("/")
        for f in Path(os.environ["COORDINATOR_HOME"]).rglob("*"):
            if f.is_file():
                self.assertNotIn("Two pages disagree", f.read_bytes().decode("utf-8", "replace"), f)

    @unittest.skip("BACKLOG(home): old home page content, see README Backlog ideas")

    def test_no_sources_file_means_no_outside_cards(self):
        os.environ["COORDINATOR_SOURCES"] = str(self.fx / "none.toml")
        board._outside["data"] = None
        out = self.get_json("/api/index")["outside"]
        self.assertFalse(out["configured"])
        self.assertEqual(out["cards"], [])

    def test_text_is_never_cut_silently(self):
        self.assertEqual(sources.shorten("short", 10), "short")
        cut = sources.shorten("word " * 40, 30)
        self.assertTrue(cut.endswith("…") and len(cut) <= 30)
        self.assertEqual(sources.parse_item({"how_to_act": "x " * 400}, "p", "s")["how_to_act"], "")
        self.assertEqual(wiki.how_to(sources.parse_item({"kind": "conflict", "id": "1"}, "p", "s"), "C:/p", ""),
                         "Open C:/p in VS Code, then type /conflicts")


class MakeTaskTest(NeedsBase):
    def test_one_item_becomes_one_task_of_its_project(self):
        status, _, _ = self.make_task_form()
        self.assertEqual(status, 303)
        t = self.get_json("/api/task/PA-1")["task"]
        self.assertEqual(t["title"], "Recipient: who gets it?")
        self.assertEqual(t["origin"], "needs you: kmu conflict C22")
        self.assertEqual(t["goal"], "Two pages disagree.")
        self.assertEqual(t["created_by_type"], "person")

    def test_same_item_twice_is_refused_while_the_task_is_open(self):
        self.make_task_form()
        status, html, _ = self.make_task_form()
        self.assertEqual(status, 400)
        self.assertIn("already became PA-1", html)

    def test_project_not_registered_is_refused_with_three_lines(self):
        status, html, _ = self.make_task_form(key="", name="other wiki")
        self.assertEqual(status, 400)
        for w in ("WHAT:", "WHY:", "FIX:"):
            self.assertIn(w, html)

    def test_bad_token_is_forbidden(self):
        status, _, _ = self.make_task_form(token="nope")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
