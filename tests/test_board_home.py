"""The home page lists only tasks that wait in the clarify state (COORD-4)."""
import unittest
from datetime import datetime
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

    def test_oldest_first(self):
        new = self.clarify("PA", "Newer", session="s-a")
        old = self.clarify("PB", "Older", session="s-b")
        self.age(old, "2026-01-01T10:00:00+00:00")
        self.age(new, "2026-02-01T10:00:00+00:00")
        body = self.get("/")[1]
        self.assertLess(body.index(f'href="/t/{old}"'), body.index(f'href="/t/{new}"'))
        self.assertEqual([i["id"] for i in self.get_json("/api/index")["clarify"]], [old, new])

    def test_row_shows_question_and_links(self):
        tid = self.clarify("PB", "Pick a store", "Which database?")
        body = self.get("/")[1]
        self.assertIn(f'href="/t/{tid}"', body)
        self.assertIn("Pick a store", body)
        self.assertIn('href="/p/PB"', body)
        self.assertIn("Which database?", body)
        self.assertIn("1 task needs clarification", body)

    def test_heading_counts_several(self):
        self.clarify("PA", "One", session="s-a")
        self.clarify("PB", "Two", session="s-b")
        self.assertIn("2 tasks need clarification", self.get("/")[1])

    def test_clarify_task_that_is_also_blocked_shows_once(self):
        tid = self.clarify()
        core.set_blocked(self.conn, tid, PERSON, "waiting")
        body = self.get("/")[1]
        self.assertEqual(body.count(f'href="/t/{tid}"'), 1)

    def test_clarify_without_comment_uses_fallback(self):
        item = {"id": "PA-1", "title": "No note", "project": "PA", "project_name": "Name PA",
                "state_label": "Needs clarification", "note": "", "since": "2026-11-11T10:00:00+00:00"}
        row = str(pages._clarify_row(item, datetime.now().astimezone()))
        self.assertIn("Waiting in Needs clarification.", row)

    def test_empty_state_is_one_quiet_line(self):
        body = self.get("/")[1]
        self.assertIn("No task needs clarification.", body)
        self.assertNotIn('class="irow"', body)
        for gone in ("things need you", "masonry", "Mode file", "All 2 projects are quiet"):
            self.assertNotIn(gone, body)

    def test_header_stays(self):
        body = self.get("/")[1]
        self.assertIn('href="/modes"', body)
        self.assertIn('href="/?fresh=1"', body)

    def test_old_cards_are_gone_from_home(self):
        self.clarify()
        body = self.get("/")[1]
        for gone in ('id="pc-PA"', "Chats working", "chat working", "Act now"):
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
