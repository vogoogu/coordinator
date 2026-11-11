"""Task chain support: Start notes, chat names, helper reports, permission-click wait,
helper-aware lease, overlap list, self-refresh pulse."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)

from coordinator import core, db, hooks, mcp_server, messages  # noqa: E402
from test_board import BoardBase, PERSON  # noqa: E402


class ChainBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_chain_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = os.path.join(self.tmp, "home")
        self.addCleanup(self._restore)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.root = str(Path(self.tmp, "proj"))
        (Path(self.root) / ".coordinator").mkdir(parents=True)
        (Path(self.root) / ".coordinator" / "project.toml").write_text(
            'key = "KMU"\nname = "KMU Beratung"\n', encoding="utf-8")
        core.add_project(self.conn, self.root, PERSON)

    def _restore(self):
        if self._old is None:
            os.environ.pop("COORDINATOR_HOME", None)
        else:
            os.environ["COORDINATOR_HOME"] = self._old

    def task(self, title="Build it", **kw):
        return core.create_task(self.conn, "KMU", title, PERSON, **kw)["id"]

    def sunk_during(self, fn):
        sink = os.path.join(self.tmp, "sink.jsonl")
        with mock.patch.dict(os.environ, {"COORDINATOR_NOTIFY_SINK": sink}):
            fn()
        if not os.path.isfile(sink):
            return []
        with open(sink, encoding="utf-8") as f:
            return [json.loads(line) for line in f]


class NamesTest(ChainBase):
    def test_new_task_gets_a_word_and_a_name(self):
        t = core.get_task(self.conn, self.task())
        self.assertIn(t["word"], core.NAME_WORDS)
        self.assertEqual(core.task_name(t), f"KMU-1 spec · {t['word']}")
        self.assertEqual(core.task_name(t, "build 2"), f"KMU-1 build 2 · {t['word']}")

    def test_old_task_without_word_is_named_by_id_and_role_only(self):
        tid = self.task()
        self.conn.execute("UPDATE task SET word = ''")
        t = core.get_task(self.conn, tid)
        self.assertEqual(core.task_name(t), "KMU-1 spec")
        named = core.ensure_word(self.conn, tid, PERSON)
        self.assertIn(named["word"], core.NAME_WORDS)
        self.assertEqual(core.ensure_word(self.conn, tid, PERSON)["word"], named["word"])


class StartNoteTest(ChainBase):
    def test_first_new_chat_in_the_folder_takes_the_task(self):
        tid = self.task()
        core.leave_start_note(self.conn, tid, PERSON)
        got = core.take_start_note(self.conn, "sess-1", self.root)
        self.assertEqual((got["id"], got["state"]), (tid, "building"))
        self.assertEqual(core.claim_for_session(self.conn, "sess-1")["display_id"], tid)
        self.assertIsNone(core.take_start_note(self.conn, "sess-2", self.root))  # used once

    def test_note_is_for_its_project_folder_only(self):
        tid = self.task()
        core.leave_start_note(self.conn, tid, PERSON)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        self.assertIsNone(core.take_start_note(self.conn, "sess-1", elsewhere))
        self.assertIsNone(core.claim_for_session(self.conn, "sess-1"))

    def test_old_note_is_not_picked_up(self):
        tid = self.task()
        core.leave_start_note(self.conn, tid, PERSON)
        later = datetime.now(timezone.utc) + timedelta(minutes=core.START_NOTE_MINUTES + 1)
        self.assertIsNone(core.take_start_note(self.conn, "sess-1", self.root, now=later))

    def test_task_that_is_no_longer_waiting_is_skipped_for_the_next_note(self):
        a, b = self.task("A"), self.task("B")
        core.leave_start_note(self.conn, a, PERSON)
        core.leave_start_note(self.conn, b, PERSON)
        core.claim(self.conn, a, "other-chat")       # somebody started A by hand
        got = core.take_start_note(self.conn, "sess-1", self.root)
        self.assertEqual(got["id"], b)

    def test_starting_twice_keeps_one_note(self):
        tid = self.task()
        core.leave_start_note(self.conn, tid, PERSON)
        core.leave_start_note(self.conn, tid, PERSON)
        open_notes = self.conn.execute(
            "SELECT COUNT(*) FROM start_note WHERE taken IS NULL").fetchone()[0]
        self.assertEqual(open_notes, 1)

    def test_start_is_an_event_row(self):
        tid = self.task()
        core.leave_start_note(self.conn, tid, PERSON)
        actions = [e["action"] for e in core.get_task(self.conn, tid)["history"]]
        self.assertEqual(actions[-1], "start")

    def test_session_start_hook_claims_and_names_the_chat(self):
        tid = self.task()
        core.leave_start_note(self.conn, tid, PERSON)
        word = core.get_task(self.conn, tid)["word"]
        r = hooks.handle("session-start", {"session_id": "sess-1", "cwd": self.root,
                                           "source": "startup"}, {})
        self.assertIn(f"You hold task {tid}", r.stdout)
        self.assertIn(f"Your chat name: {tid} spec · {word}", r.stdout)

    def test_resume_does_not_take_a_note(self):
        tid = self.task()
        core.leave_start_note(self.conn, tid, PERSON)
        r = hooks.handle("session-start", {"session_id": "sess-1", "cwd": self.root,
                                           "source": "resume"}, {})
        self.assertNotIn("You hold task", r.stdout)
        self.assertEqual(core.get_task(self.conn, tid)["state"], "open")


class ReportsTest(ChainBase):
    def test_role_is_kept_and_checked(self):
        tid = self.task()
        c = core.comment(self.conn, tid, core.Actor("chat", "s1"), "comment",
                         "Result: done", role="build 1")
        self.assertEqual(c["role"], "build 1")
        self.assertEqual(core.comment(self.conn, tid, PERSON, "comment", "x")["role"], "")
        with self.assertRaises(messages.CoordinatorError):
            core.comment(self.conn, tid, core.Actor("chat", "s1"), "comment", "x",
                         role="boss")

    def test_report_text_is_stored_word_for_word(self):
        tid = self.task()
        text = "Result: done\n\nDecisions: none\n  indented <b>& \"quotes\"</b>"
        core.comment(self.conn, tid, core.Actor("chat", "s1"), "comment", text,
                     role="test 1")
        self.assertEqual(core.get_task(self.conn, tid)["comments"][0]["text"], text)


class ClickWaitTest(ChainBase):
    def claimed(self):
        tid = self.task()
        core.claim(self.conn, tid, "sess-1")
        return tid

    def notification(self, **kw):
        p = {"session_id": "sess-1", "cwd": self.root, "hook_event_name": "Notification",
             "notification_type": "permission_prompt",
             "message": "Claude needs your permission to use Bash"}
        p.update(kw)
        return hooks.handle("notification", p, {})

    def test_permission_prompt_marks_the_task_and_pops_up(self):
        tid = self.claimed()
        recs = self.sunk_during(self.notification)
        [rec] = recs
        self.assertRegex(rec["heading"], rf"^{tid} spec . \w+ waits for your click$")
        self.assertEqual(rec["line2"], "Claude needs your permission to use Bash")
        self.assertTrue(core.live_claims(self.conn)[tid]["waiting_click"])
        self.assertEqual(core.get_task(self.conn, tid)["state"], "building")  # no state change

    def test_second_prompt_does_not_pop_up_again(self):
        self.claimed()
        self.assertEqual(len(self.sunk_during(self.notification)), 1)
        self.assertEqual(len(self.sunk_during(self.notification)), 1)   # the sink file is shared: still one

    def test_next_tool_call_clears_the_mark(self):
        tid = self.claimed()
        self.notification()
        core.touch(self.conn, "sess-1", "Bash")
        self.assertFalse(core.live_claims(self.conn)[tid]["waiting_click"])

    def test_other_notifications_and_unclaimed_chats_are_ignored(self):
        tid = self.claimed()
        self.assertEqual(self.sunk_during(lambda: self.notification(
            notification_type="idle_prompt", message="Claude is waiting for your input")), [])
        self.assertEqual(self.sunk_during(lambda: self.notification(session_id="nobody")), [])
        self.assertFalse(core.live_claims(self.conn)[tid]["waiting_click"])

    def test_old_claude_without_type_is_recognised_by_its_message(self):
        self.claimed()
        self.assertEqual(len(self.sunk_during(lambda: self.notification(notification_type=""))), 1)

    def test_hook_fails_open_on_bad_payload(self):
        r = hooks.handle("notification", {"session_id": 5, "message": None}, {})
        self.assertEqual(r.exit_code, 0)


class HelperLeaseTest(ChainBase):
    def claimed(self):
        tid = self.task()
        core.claim(self.conn, tid, "sess-1")
        self.conn.execute("UPDATE claim SET last_seen = '2020-01-01T00:00:00+00:00'")
        return tid

    def test_claim_with_running_helper_is_not_expired(self):
        tid = self.claimed()
        hooks.handle("subagent-start", {"session_id": "sess-1"}, {})
        self.conn.execute("UPDATE claim SET last_seen = '2020-01-01T00:00:00+00:00'")
        self.assertEqual(core.expire_leases(self.conn), [])
        self.assertEqual(core.get_task(self.conn, tid)["state"], "building")

    def test_after_the_helper_stops_the_lease_runs_as_usual(self):
        tid = self.claimed()
        hooks.handle("subagent-start", {"session_id": "sess-1"}, {})
        hooks.handle("subagent-stop", {"session_id": "sess-1"}, {})
        self.conn.execute("UPDATE claim SET last_seen = '2020-01-01T00:00:00+00:00'")
        self.assertEqual(core.expire_leases(self.conn), [tid])

    def test_two_helpers_need_two_stops(self):
        self.claimed()
        for _ in range(2):
            hooks.handle("subagent-start", {"session_id": "sess-1"}, {})
        hooks.handle("subagent-stop", {"session_id": "sess-1"}, {})
        self.conn.execute("UPDATE claim SET last_seen = '2020-01-01T00:00:00+00:00'")
        self.assertEqual(core.expire_leases(self.conn), [])

    def test_helper_that_never_reports_its_end_cannot_keep_a_dead_chat_alive(self):
        tid = self.claimed()
        hooks.handle("subagent-start", {"session_id": "sess-1"}, {})
        later = datetime.now(timezone.utc) + timedelta(hours=core.HELPER_MAX_HOURS + 1)
        self.assertEqual(core.expire_leases(self.conn, now=later), [tid])

    def test_helper_start_counts_as_activity(self):
        tid = self.claimed()
        hooks.handle("subagent-start", {"session_id": "sess-1"}, {})
        seen = core.live_claims(self.conn)[tid]["last_seen"]
        self.assertGreater(seen, "2021")

    def test_stop_never_goes_below_zero_and_unclaimed_chats_write_nothing(self):
        self.claimed()
        hooks.handle("subagent-stop", {"session_id": "sess-1"}, {})
        self.assertEqual(core.live_claims(self.conn)["KMU-1"]["helpers"], 0)
        before = self.conn.execute("SELECT COUNT(*) FROM session_stat").fetchone()[0]
        hooks.handle("subagent-start", {"session_id": "stranger"}, {})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM session_stat").fetchone()[0], before)


class OverlapTest(ChainBase):
    def test_lists_other_open_tasks_with_shared_forms_first(self):
        a, b, c, d = (self.task(x) for x in "ABCD")
        core.set_forms(self.conn, a, PERSON, ["497", "458"])
        core.set_forms(self.conn, b, PERSON, ["120"])
        core.set_forms(self.conn, c, PERSON, ["458", "999"])
        core.set_state(self.conn, d, "done", PERSON)           # closed: not listed
        out = core.overlaps(self.conn, a)
        self.assertEqual([o["id"] for o in out], [c, b])
        self.assertEqual(out[0]["shared_forms"], ["458"])
        self.assertEqual(out[1]["shared_forms"], [])
        self.assertEqual(out[0]["forms"], ["458", "999"])
        self.assertTrue(out[0]["name"].startswith(f"{c} spec"))

    def test_forms_argument_overrides_the_tasks_own(self):
        a, b = self.task("A"), self.task("B")
        core.set_forms(self.conn, b, PERSON, ["497"])
        self.assertEqual(core.overlaps(self.conn, a, ["497"])[0]["shared_forms"], ["497"])

    def test_claimed_flag_and_form_clean_up(self):
        a, b = self.task("A"), self.task("B")
        core.claim(self.conn, b, "s2")
        core.set_forms(self.conn, b, PERSON, [" 497 ", "497", ""])
        [o] = core.overlaps(self.conn, a)
        self.assertEqual((o["claimed"], o["forms"]), (True, ["497"]))

    def test_bad_forms_refused(self):
        a = self.task()
        with self.assertRaises(messages.CoordinatorError):
            core.set_forms(self.conn, a, PERSON, "497")

    def test_mcp_tools(self):
        a, b = self.task("A"), self.task("B")
        core.claim(self.conn, a, "s1")
        res = mcp_server.call_tool("task_update", {"session_id": "s1", "id": a,
                                                   "fields": {"forms": ["497"]}})
        self.assertFalse(res["isError"], res)
        core.set_forms(self.conn, b, PERSON, ["497"])
        res = mcp_server.call_tool("task_overlaps", {"id": a})
        self.assertFalse(res["isError"], res)
        [o] = json.loads(res["content"][0]["text"])
        self.assertEqual((o["id"], o["shared_forms"]), (b, ["497"]))
        bad = mcp_server.call_tool("task_update", {"session_id": "s1", "id": a,
                                                   "fields": {"forms": "497"}})
        self.assertTrue(bad["isError"])
        res = mcp_server.call_tool("task_comment", {"session_id": "s1", "id": a,
                                                    "kind": "comment", "text": "r",
                                                    "role": "build 1"})
        self.assertFalse(res["isError"], res)
        self.assertEqual(json.loads(res["content"][0]["text"])["role"], "build 1")


class ChainBoardTest(BoardBase):
    def test_start_opens_the_folder_and_leaves_a_note(self):
        tid = self.task()
        with mock.patch("coordinator.launcher.open_folder") as opened:
            status, _, _ = self.post("/a/start", {"id": tid})
        self.assertEqual(status, 303)
        opened.assert_called_once_with(self.roots["PA"])
        got = core.take_start_note(self.conn, "sess-1", self.roots["PA"])
        self.assertEqual(got["id"], tid)

    def test_failed_open_leaves_no_note(self):
        tid = self.task()
        with mock.patch("coordinator.launcher.open_folder", side_effect=OSError("no code")):
            status, body, _ = self.post("/a/start", {"id": tid})
        self.assertNotEqual(status, 303)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM start_note").fetchone()[0], 0)

    def test_task_page_shows_reports_word_for_word_under_the_name(self):
        tid = self.task()
        word = core.get_task(self.conn, tid)["word"]
        core.comment(self.conn, tid, core.Actor("chat", "h1"), "comment",
                     "Result: done\nDecisions: dropdown <b>because</b>", role="build 1")
        core.comment(self.conn, tid, core.Actor("chat", "h2"), "comment",
                     "Result: blocked", role="test 1")
        core.comment(self.conn, tid, PERSON, "comment", "ordinary note")
        body = self.get(f"/t/{tid}")[1]
        self.assertIn(f"{tid} build 1 · {word}", body)
        self.assertIn(f"{tid} test 1 · {word}", body)
        self.assertIn("Result: done\nDecisions: dropdown &lt;b&gt;because&lt;/b&gt;", body)
        self.assertLess(body.index("test 1"), body.index("build 1"))   # newest first
        api = self.get_json(f"/api/task/{tid}")
        self.assertEqual([r["role"] for r in api["reports"]], ["build 1", "test 1"])

    def test_task_page_without_reports_says_so(self):
        self.assertIn("No helper has reported", self.get(f"/t/{self.task()}")[1])

    def test_card_and_task_page_show_click_wait_and_helper(self):
        tid = self.task()
        core.claim(self.conn, tid, "sess-1")
        core.mark_waiting_click(self.conn, "sess-1", "Claude needs your permission")
        core.helper_event(self.conn, "sess-1", True)
        for path in ("/p/PA", f"/t/{tid}"):
            body = self.get(path)[1]
            self.assertIn("aits for your click", body, path)
        board_body = self.get("/p/PA")[1]
        self.assertRegex(board_body, r"PA-1 spec . \w+ working, helper running")
        self.assertIn("A helper is working for this chat", self.get(f"/t/{tid}")[1])
        core.touch(self.conn, "sess-1", "Bash")
        self.assertNotIn("aits for your click", self.get("/p/PA")[1])

    def test_a_chat_with_a_helper_is_not_shown_quiet(self):
        tid = self.task()
        core.claim(self.conn, tid, "sess-1")
        core.helper_event(self.conn, "sess-1", True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE claim SET last_seen = ?", (old,))
        self.assertNotIn("quiet for", self.get("/p/PA")[1])

    def test_pulse_changes_with_every_write_and_pages_poll_it(self):
        first = self.get_json("/api/pulse")["stamp"]
        self.assertEqual(self.get_json("/api/pulse")["stamp"], first)
        self.task()
        self.assertGreater(self.get_json("/api/pulse")["stamp"], first)
        for path in ("/", "/p/PA", f"/t/PA-1"):
            self.assertIn('data-live="1"', self.get(path)[1], path)
        self.assertIn("/api/pulse", self.get("/static/board.js")[1])
        self.assertNotIn('data-live="1"', self.get("/modes")[1])       # it refreshes by itself


if __name__ == "__main__":
    unittest.main()
