"""The Modes page, the guard's "Needs you" rows and the switch buttons on the board.
Fixtures live in a temp ANTSON_STATE_DIR (set by BoardBase); mode.py is a fake that
records its command line."""
import json
import os
import time
import unittest
from pathlib import Path

from coordinator import antson, core
from tests import test_antson, test_board


class ModesBoardTest(test_board.BoardBase):
    def setUp(self):
        super().setUp()
        self.state = Path(self.tmp, "antson")
        self.state.mkdir()
        self.guard = Path(self.tmp, "guard")
        self.guard.mkdir()
        self.mode_py = self.guard / "mode.py"
        self.mode_py.write_text(test_antson.FAKE_MODE_PY, encoding="utf-8")
        os.environ["COORDINATOR_GUARD_MODE_PY"] = str(self.mode_py)
        antson._hash_cache.update(key=None, value=None)
        antson._job = None
        self.addCleanup(self._reset_job)
        self.make_project("trial", "TRIAL")

    def _reset_job(self):
        antson.wait_idle(10)
        antson._job = None

    def write_modes(self):
        (self.state / "modes.json").write_text(json.dumps({"version": 1, "items": {
            "614": {"kind": "form", "name": "Sandbox form", "project": "TRIAL", "mode": "dev",
                    "parent": None, "inherit": False, "new": False, "set_by": "daria"},
            "625": {"kind": "form", "name": "Second form", "project": "TRIAL", "mode": "read",
                    "parent": None, "inherit": False, "new": True, "set_by": "chat:s9"},
            "2225": {"kind": "interface", "name": "2225", "project": "TRIAL", "mode": "read",
                     "parent": "614", "inherit": True, "new": True, "set_by": "guard"}}}),
            encoding="utf-8")

    def write_reports(self):
        rows = [{"at": "2026-11-10T08:00:00+00:00", "session": "s9", "event": "created",
                 "kind": "interface", "id": "2230", "name": None, "parent": "614", "tenant": None},
                {"at": "2026-11-10T08:01:00+00:00", "session": "s9", "event": "seen",
                 "kind": "form", "id": "700", "name": "Stranger", "parent": None, "tenant": None}]
        (self.state / "reports.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def write_go(self, session="sess-1", **data):
        (self.state / "go").mkdir(exist_ok=True)
        (self.state / "go" / f"{session}.json").write_text(
            json.dumps({"lists": [], "approved": [], "windows": {}, **data}), encoding="utf-8")

    def post_modes(self, pairs):
        return self.request("POST", "/a/modes",
                            form=[("token", self.token), ("next", "/modes"), *pairs],
                            headers={"Origin": self.origin()})

    def calls(self):
        p = self.guard / "calls.jsonl"
        return ([json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
                if p.exists() else [])

    # ---- the page
    def test_modes_page_shows_one_table_per_project_with_inherited_and_new_rows(self):
        self.write_modes()
        self.write_reports()
        status, html, _ = self.get("/modes")
        self.assertEqual(status, 200)
        self.assertIn("Name TRIAL", html)
        for text in ("Sandbox form", "Second form", "Stranger"):
            self.assertIn(text, html)
        self.assertEqual(html.count("(from form)"), 1, "only 2225 inherits; 2230 is not in modes.json")
        data = self.get_json("/api/modes")
        (group,) = [g for g in data["groups"] if g["key"] == "TRIAL"]
        rows = {r["id"]: r for r in group["rows"]}
        self.assertEqual((rows["614"]["mode"], rows["614"]["new"]), ("dev", False))
        self.assertEqual((rows["2225"]["mode"], rows["2225"]["inherited"], rows["2225"]["new"]),
                         ("dev", True, True))
        self.assertTrue(rows["625"]["new"])
        self.assertEqual([r["id"] for r in group["rows"]], ["614", "2225", "2230", "625"],
                         "children follow their form")
        self.assertEqual(rows["2230"]["depth"], 1)
        self.assertEqual((rows["2230"]["kind"], rows["2230"]["new"], rows["2230"]["mode"]),
                         ("interface", True, "read"))

    def test_a_row_set_by_daria_is_not_new_any_more(self):
        self.write_modes()
        data = json.loads((self.state / "modes.json").read_text())
        data["items"]["625"].update(set_by="board", mode="dev")
        (self.state / "modes.json").write_text(json.dumps(data), encoding="utf-8")
        rows = {r["id"]: r for g in self.get_json("/api/modes")["groups"] for r in g["rows"]}
        self.assertFalse(rows["625"]["new"])

    def test_report_items_not_in_modes_json_are_new_and_stay_out_of_the_file(self):
        self.write_modes()
        self.write_reports()
        before = (self.state / "modes.json").read_bytes()
        data = self.get_json("/api/modes")
        ids = {r["id"]: r for g in data["groups"] for r in g["rows"]}
        self.assertTrue(ids["700"]["new"] and ids["2230"]["new"])
        self.assertEqual(ids["700"]["name"], "Stranger")
        self.assertEqual(ids["700"]["project"], "", "no project known: listed as not assigned")
        self.assertEqual((self.state / "modes.json").read_bytes(), before)
        self.assertIn("Not assigned to a project", self.get("/modes")[1])
        store = Path(os.environ["COORDINATOR_HOME"]) / "reports-intake.json"
        self.assertEqual(json.loads(store.read_text())["offset"],
                         (self.state / "reports.jsonl").stat().st_size)

    def test_a_report_item_finds_its_project_through_the_chat(self):
        tid = self.task("TRIAL", "Trial build")
        core.claim(self.conn, tid, "s9")
        self.write_reports()
        self.assertEqual([g["key"] for g in self.get_json("/api/modes")["groups"]], ["TRIAL"])

    def test_empty_state_is_a_calm_page(self):
        status, html, _ = self.get("/modes")
        self.assertEqual(status, 200)
        self.assertIn("No forms yet", html)

    def test_modes_page_warns_when_mode_py_is_missing(self):
        os.environ["COORDINATOR_GUARD_MODE_PY"] = str(self.guard / "nope.py")
        self.assertIn("The switch was not found", self.get("/modes")[1])

    # ---- Needs you
    def test_hash_change_shows_under_needs_you(self):
        (self.state / "modes.json").write_text('{"version": 1, "items": {}}', encoding="utf-8")
        (self.guard / "hash_bad").write_text("x")
        self.assertIn("Mode file changed outside the switch", self.get("/")[1])
        self.assertTrue(any(n["kind"] == "hash"
                            for n in self.get_json("/api/index")["guard"]["needs"]))
        (self.guard / "hash_bad").unlink()
        (self.state / "modes.json").write_text('{"version": 1, "items": {"1": {}}}', encoding="utf-8")
        self.assertNotIn("Mode file changed outside the switch", self.get("/")[1])

    def test_go_waiting_shows_the_numbered_list_and_the_chat_name(self):
        tid = self.task("TRIAL", "Trial build")
        core.claim(self.conn, tid, "sess-1")
        self.write_go(lists=[{"list_id": "L1", "at": "2026-11-10T09:30:39", "items": [
            {"n": 1, "form": "625", "ref": "T-1", "what": "label of one element"},
            {"n": 2, "form": "614", "ref": "T-2", "what": "read it back"}]}])
        html = self.get("/")[1]
        self.assertIn("waits for your go: 1. label of one element (form 625), "
                      "2. read it back (form 614)", html)
        self.assertIn(f"chat {tid} (Trial build)", html)
        self.assertIn("go 1, 2", html)
        self.assertIn("1 thing needs you", html)

    def test_go_waiting_with_an_unknown_session_names_it_by_its_short_id(self):
        self.write_go("abcd1234", lists=[{"list_id": "L1", "at": "x", "items": [
            {"n": 1, "form": "625", "ref": "T-1", "what": "do it"}]}])
        self.assertIn("chat abcd waits for your go: 1. do it (form 625)", self.get("/")[1])

    def test_an_approved_list_is_not_shown(self):
        self.write_go(lists=[{"list_id": "L1", "at": "x", "items": [
            {"n": 1, "form": "625", "ref": "T-1", "what": "do it"}]}],
            approved=[{"list_id": "L1", "n": [1], "at": "x"}])
        self.assertNotIn("waits for your go", self.get("/")[1])

    def test_a_refused_ready_check_shows_with_its_reason(self):
        self.write_go(ready_refused={"625": ["form-mismatch"]})
        html = self.get("/")[1]
        self.assertIn("ready check for form 625 was refused", html)
        self.assertIn("form-mismatch", html)

    def test_a_quiet_guard_adds_nothing_to_the_index(self):
        self.assertNotIn('id="gd-h"', self.get("/")[1])

    def test_new_items_get_a_card_on_the_index(self):
        self.write_modes()
        self.assertIn('id="gd-h"', self.get("/")[1])

    # ---- the switch
    def test_one_row_button_calls_mode_py_as_a_command(self):
        status, body, headers = self.post_modes([("row", "614|prod")])
        self.assertEqual((status, headers["Location"]), (303, "/modes"))
        antson.wait_idle(15)
        self.assertEqual(self.calls(), [["set", "614", "prod", "--by", "board", "--json"]])

    def test_selected_rows_are_one_call_so_one_window(self):
        status, *_ = self.post_modes([("ids", "614"), ("ids", "625"), ("ids", "2225"),
                                      ("mode", "dev")])
        self.assertEqual(status, 303)
        antson.wait_idle(15)
        self.assertEqual(self.calls(),
                         [["set", "614", "625", "2225", "dev", "--by", "board", "--json"]])

    def test_the_post_is_not_blocked_by_a_waiting_window(self):
        (self.guard / "slow").write_text("1.5")
        t0 = time.time()
        status, *_ = self.post_modes([("row", "614|dev")])
        self.assertEqual(status, 303)
        self.assertLess(time.time() - t0, 1.0)
        page = self.get("/modes")[1]
        self.assertIn("Changing 614 to dev.", page)
        self.assertIn('http-equiv="refresh"', page, "the page refreshes itself while it waits")
        status, body, _ = self.post_modes([("row", "625|dev")])
        self.assertEqual(status, 400)
        self.assertIn("still waiting for your answer", body)
        antson.wait_idle(15)
        done = self.get("/modes")[1]
        self.assertIn("Changed: 614 to dev.", done)
        self.assertNotIn('http-equiv="refresh"', done)

    def test_a_refused_switch_is_shown_not_hidden(self):
        (self.guard / "refuse").write_text("x")
        self.post_modes([("row", "614|dev")])
        antson.wait_idle(15)
        self.assertIn("Not changed: 614 (not confirmed).", self.get("/modes")[1])

    def test_nothing_selected_and_bad_input_are_refused_without_a_call(self):
        self.assertEqual(self.post_modes([("mode", "dev")])[0], 400)
        self.assertEqual(self.post_modes([("ids", "--version"), ("mode", "dev")])[0], 400)
        self.assertEqual(self.post_modes([("ids", "614"), ("mode", "root")])[0], 400)
        self.assertEqual(self.calls(), [])

    def test_the_switch_needs_the_token_and_the_origin(self):
        f = [("row", "614|dev")]
        status, *_ = self.request("POST", "/a/modes", form=f, headers={"Origin": self.origin()})
        self.assertEqual(status, 403)
        status, *_ = self.request("POST", "/a/modes", form=[("token", self.token), *f])
        self.assertEqual(status, 403)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
