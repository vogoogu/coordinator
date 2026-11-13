"""The home page lists only tasks that wait in the clarify state (COORD-4)."""
import unittest
from pathlib import Path
from unittest import mock

from coordinator import board, core, pages

from tests.test_board import PERSON, BoardBase


class HomeClarifyTest(BoardBase):
    def clarify(self, key="PA", title="Unclear", question="Which database?", session="sess-1"):
        tid = self.task(key, title)
        core.claim(self.conn, tid, session)
        core.release(self.conn, tid, session, "clarify", question)
        return tid

    def age(self, tid, stamp):
        key, number = tid.rsplit("-", 1)
        self.conn.execute("UPDATE task SET updated = ? WHERE number = ? AND project_id = "
                          "(SELECT id FROM project WHERE key = ?)", (stamp, int(number), key))

    def note(self, key="PA", title="Maybe later"):
        return core.create_task(self.conn, key, title, PERSON, backlog=True)["id"]

    def test_card_shows_backlog_count_link(self):
        self.note("PA", "one")
        self.note("PA", "two")
        body = self.get("/")[1]
        self.assertIn('href="/p/PA#backlog"', body)
        self.assertIn("Backlog 2", body)
        self.assertEqual(body.count("#backlog"), 1)           # PB has no notes
        pa = next(p for p in self.get_json("/api/index")["projects"] if p["key"] == "PA")
        self.assertEqual(pa["backlog"], 2)

    def test_card_without_notes_has_no_backlog_link(self):
        self.clarify()
        body = self.get("/")[1]
        self.assertNotIn("#backlog", body)
        for p in self.get_json("/api/index")["projects"]:
            self.assertEqual(p["backlog"], 0)

    def test_notes_never_count_as_clarify(self):
        n = self.note()
        body = self.get("/")[1]
        self.assertEqual(self.get_json("/api/index")["clarify"], [])
        self.assertIn("No task needs clarification.", body)
        self.assertNotIn(f'href="/t/{n}"', body)

    def test_lists_only_clarify_tasks(self):
        c = self.clarify()
        blocked = self.task("PA", "Blocked one")
        core.set_blocked(self.conn, blocked, PERSON, "waiting")
        review = self.task("PB", "Review one")
        core.set_state(self.conn, review, "review", PERSON, note="x")
        inbox = self.task("PB", "Inbox one")
        self.conn.execute("UPDATE task SET inbox = 1 WHERE id = ?", (inbox,))
        body = self.get("/")[1]
        self.assertIn(f'href="/t/{c}"', body)
        for other in (blocked, review, inbox):
            self.assertNotIn(f'href="/t/{other}"', body)
        data = self.get_json("/api/index")
        self.assertEqual([i["id"] for i in data["clarify"]], [c])

    def test_oldest_first_inside_a_card(self):
        new = self.clarify("PA", "Newer", session="s-a")
        old = self.clarify("PA", "Older", session="s-b")
        self.age(old, "2026-01-01T10:00:00+00:00")
        self.age(new, "2026-02-01T10:00:00+00:00")
        body = self.get("/")[1]
        self.assertLess(body.index(f'href="/t/{old}"'), body.index(f'href="/t/{new}"'))
        self.assertEqual([i["id"] for i in self.get_json("/api/index")["clarify"]], [old, new])

    def test_row_is_chip_age_title_only(self):
        tid = self.clarify("PB", "Pick a store", "Which database?")
        body = self.get("/")[1]
        self.assertIn(f'href="/t/{tid}"', body)
        self.assertIn(f'<span class="idc">{tid}</span>', body)
        self.assertIn("Pick a store", body)
        self.assertIn('href="/p/PB"', body)
        self.assertNotIn("Which database?", body)
        self.assertNotIn("Needs clarification", body)
        self.assertIn("1 task needs clarification", body)
        self.assertIn("in 1 of 2 projects", body)

    def test_heading_counts_several(self):
        self.clarify("PA", "One", session="s-a")
        self.clarify("PB", "Two", session="s-b")
        body = self.get("/")[1]
        self.assertIn("2 tasks need clarification", body)
        self.assertIn("in 2 of 2 projects", body)

    def test_clarify_task_that_is_also_blocked_shows_once(self):
        tid = self.clarify()
        core.set_blocked(self.conn, tid, PERSON, "waiting")
        body = self.get("/")[1]
        self.assertEqual(body.count(f'href="/t/{tid}"'), 1)

    def test_every_project_has_exactly_one_card(self):
        self.clarify("PA")
        body = self.get("/")[1]
        for key in ("PA", "PB"):
            self.assertEqual(body.count(f'id="pc-{key}"'), 1)
            self.assertEqual(body.count(f'<a href="/p/{key}">Open board'), 1)
        self.assertEqual(body.count('class="masonry"'), 1)

    def test_quiet_project_card(self):
        self.clarify("PA")
        body = self.get("/")[1]
        quiet = body[body.index('id="pc-PB"'):]
        self.assertIn("Nothing to clarify", quiet)
        self.assertIn('pill grn', quiet)
        self.assertIn("OK", quiet)
        busy = body[body.index('id="pc-PA"'):body.index('id="pc-PB"')]
        self.assertIn("pill amb", busy)
        self.assertIn("Check", busy)
        self.assertNotIn("Nothing to clarify", busy)

    def test_all_quiet_still_shows_all_cards(self):
        body = self.get("/")[1]
        self.assertIn("No task needs clarification.", body)
        self.assertNotIn("irow", body.replace(".irow", ""))
        self.assertEqual(body.count("Nothing to clarify"), 2)
        for gone in ("things need you", "Mode file", "All 2 projects are quiet"):
            self.assertNotIn(gone, body)

    def test_card_order_busiest_first_then_alphabetical(self):
        self.clarify("PB", "x", session="s-b")
        data = self.get_json("/api/index")
        self.assertEqual([p["key"] for p in data["projects"]], ["PB", "PA"])
        self.assertEqual([p["key"] for p in data["projects"] if not p["clarify"]], ["PA"])
        self.make_project("third", "PC")
        data = self.get_json("/api/index")
        self.assertEqual([p["key"] for p in data["projects"]], ["PB", "PA", "PC"])

    def test_count_per_card(self):
        self.clarify("PA", "One", session="s-a")
        self.clarify("PA", "Two", session="s-b")
        body = self.get("/")[1]
        self.assertIn('<b class="">2</b><span>need you</span>', body)
        self.assertIn('<b class="zero">0</b><span>need you</span>', body)

    def test_row_cap_and_more_link(self):
        for n in range(pages.INDEX_ROWS + 2):
            self.clarify("PA", f"T{n}", session=f"s-{n}")
        body = self.get("/")[1]
        self.assertEqual(body.count('class="idc"'), pages.INDEX_ROWS)
        self.assertIn("2 more on the board", body)
        self.assertIn(f'<b class="">{pages.INDEX_ROWS + 2}</b>', body)

    def test_broken_project_toml_still_gets_a_card(self):
        Path(self.roots["PB"], ".coordinator", "project.toml").write_text("key = [", encoding="utf-8")
        self.clarify("PA")
        body = self.get("/")[1]
        card = body[body.index('id="pc-PB"'):]
        self.assertIn("The project file could not be read", card)
        self.assertEqual(body.count('id="pc-PB"'), 1)
        self.assertIn("1 task needs clarification", body)

    def test_logo_or_monogram(self):
        body = self.get("/")[1]
        self.assertIn('class="mark m', body)
        self.assertIn(">NP<", body)
        self.assertIn(pages.monogram("Name PA"), body)
        self.assertIn(".idc{", (Path(pages.__file__).parent / "web" / "static" / "board.css").read_text(encoding="utf-8"))

    def test_header_stays(self):
        body = self.get("/")[1]
        self.assertIn('href="/modes"', body)
        self.assertIn('href="/?fresh=1"', body)

    def test_old_extras_are_gone_from_home(self):
        self.clarify()
        body = self.get("/")[1]
        for gone in ("Act now", "Mode file", "Waiting for go", "Mail desk", "from the wiki"):
            self.assertNotIn(gone, body)

    def test_index_does_not_run_outside_readers(self):
        self.clarify()
        with mock.patch.object(board, "api_outside", side_effect=AssertionError("outside")) as out, \
                mock.patch.object(board, "_guard", side_effect=AssertionError("guard")) as guard:
            status, body, _ = self.get("/")
            self.assertEqual(status, 200, body)
            board.api_index(self.conn)
        out.assert_not_called()
        guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
