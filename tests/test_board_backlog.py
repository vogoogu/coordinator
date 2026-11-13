"""The Backlog fold on the project board: notes (parked tasks) are listed, not columned."""
import re
import unittest

from coordinator import core

from tests.test_board import PERSON, BoardBase


class BacklogBoardTest(BoardBase):
    def note(self, key="PA", title="Maybe later"):
        return core.create_task(self.conn, key, title, PERSON, backlog=True)["id"]

    def test_project_api_has_backlog_newest_first(self):
        first, second, third = self.note(title="one"), self.note(title="two"), self.note(title="three")
        data = self.get_json("/api/project/PA")
        self.assertEqual([c["id"] for c in data["backlog"]], [third, second, first])
        self.assertEqual(data["backlog"][0]["title"], "three")
        self.assertIn("created", data["backlog"][0])

    def test_backlog_state_is_not_a_column(self):
        self.note()
        self.task("PA", "Real work")
        data = self.get_json("/api/project/PA")
        self.assertNotIn("backlog", [c["name"] for c in data["columns"]])
        self.assertIn("open", [c["name"] for c in data["columns"]])
        self.assertEqual(self.get_json("/api/project/PB")["backlog"], [])

    def test_project_page_shows_backlog_fold(self):
        n = self.note(title="Cache the index")
        body = self.get("/p/PA")[1]
        self.assertIn('id="backlog"', body)
        self.assertIn(f'href="/t/{n}"', body)
        self.assertIn("Cache the index", body)
        self.assertRegex(body, r'(?s)id="backlog".*?<span class="count">1</span>')
        opened = re.search(r'id="backlog".*?<details( open)?>', body, re.S)
        self.assertIsNotNone(opened)
        self.assertIsNone(opened.group(1))          # collapsed by default

    def test_project_page_without_notes_has_no_fold(self):
        self.task("PA", "Real work")
        self.assertNotIn('id="backlog"', self.get("/p/PA")[1])

    def test_board_with_only_notes_is_not_empty(self):
        self.note()
        body = self.get("/p/PA")[1]
        self.assertNotIn("No tasks yet", body)

    def test_task_page_of_note_has_no_start(self):
        n = self.note()
        self.assertFalse(self.get_json(f"/api/task/{n}")["can_start"])

    def test_parked_task_page_shows_state_word(self):
        n = self.note()
        body = self.get(f"/t/{n}")[1]
        self.assertNotIn("Unknown kind", body)
        self.assertIn("Parked", body)


if __name__ == "__main__":
    unittest.main()
