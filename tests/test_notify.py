import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)

from coordinator import core, db, notify, paths  # noqa: E402

PERSON = core.Actor("person", "daria")
LATER = datetime.now(timezone.utc) + timedelta(hours=5)


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_notify_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = os.path.join(self.tmp, "home")
        self.addCleanup(self._restore)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        root = Path(self.tmp, "proj")
        (root / ".coordinator").mkdir(parents=True)
        (root / ".coordinator" / "project.toml").write_text(
            'key = "KMU"\nname = "KMU Beratung"\n', encoding="utf-8")
        core.add_project(self.conn, str(root), PERSON)
        self.tid = self.claimed("s1")

    def _restore(self):
        if self._old is None:
            os.environ.pop("COORDINATOR_HOME", None)
        else:
            os.environ["COORDINATOR_HOME"] = self._old

    def claimed(self, sid, title="Build it"):
        t = core.create_task(self.conn, "KMU", title, PERSON)
        core.set_mode(self.conn, t["id"], "write", PERSON)
        core.claim(self.conn, t["id"], sid, "", "")
        return t["id"]

    # ------------------------------------------------------------ collect
    def test_release_done_is_finished(self):
        m = notify.mark(self.conn)
        core.release(self.conn, self.tid, "s1", "done", "All 9 variables done")
        [a] = notify.collect(self.conn, m)
        self.assertEqual((a.kind, a.task_id, a.project, a.title, a.note, a.stopped),
                         ("finished", self.tid, "KMU Beratung", "Build it",
                          "All 9 variables done", False))

    def test_release_clarify_is_needs_you(self):
        m = notify.mark(self.conn)
        core.release(self.conn, self.tid, "s1", "clarify", "v3 or v4?")
        [a] = notify.collect(self.conn, m)
        self.assertEqual((a.kind, a.note, a.stopped), ("needs_you", "v3 or v4?", False))

    def test_set_state_by_chat_into_review_is_finished(self):
        m = notify.mark(self.conn)
        core.set_state(self.conn, self.tid, "review", core.Actor("chat", "s1"),
                       note="please check")
        [a] = notify.collect(self.conn, m)
        self.assertEqual((a.kind, a.note), ("finished", "please check"))

    def test_person_move_is_quiet(self):
        m = notify.mark(self.conn)
        core.set_state(self.conn, self.tid, "review", PERSON, note="x")
        self.assertEqual(notify.collect(self.conn, m), [])

    def test_stop_without_report_is_stopped(self):
        m = notify.mark(self.conn)
        core.stop_without_report(self.conn, "s1", "n")
        [a] = notify.collect(self.conn, m)
        self.assertEqual((a.kind, a.stopped), ("needs_you", True))

    def test_expiry_on_person_moved_task_still_alerts(self):
        core.set_state(self.conn, self.tid, "review", PERSON, note="x")
        m = notify.mark(self.conn)
        core.expire_leases(self.conn, now=LATER)
        [a] = notify.collect(self.conn, m)
        self.assertEqual((a.kind, a.stopped), ("needs_you", True))

    def test_release_on_task_person_already_moved_is_quiet(self):
        core.set_state(self.conn, self.tid, "done", PERSON)
        m = notify.mark(self.conn)
        core.release(self.conn, self.tid, "s1", "done", "n")
        self.assertEqual(notify.collect(self.conn, m), [])

    def test_no_new_events_no_alert(self):
        core.release(self.conn, self.tid, "s1", "done", "n")
        self.assertEqual(notify.collect(self.conn, notify.mark(self.conn)), [])

    def test_task_ids_filter(self):
        other = self.claimed("s2", "Other")
        m = notify.mark(self.conn)
        core.release(self.conn, self.tid, "s1", "done", "a")
        core.release(self.conn, other, "s2", "done", "b")
        self.assertEqual([a.task_id for a in notify.collect(self.conn, m, [self.tid])],
                         [self.tid])

    def test_three_stopped_make_one_summary(self):
        self.claimed("s2")
        self.claimed("s3")
        m = notify.mark(self.conn)
        core.expire_leases(self.conn, now=LATER)
        toasts = notify.compose(notify.collect(self.conn, m), notify.Settings())
        self.assertEqual(len(toasts), 1)
        self.assertEqual(toasts[0].heading, "3 tasks need you")
        self.assertEqual(toasts[0].url, "http://127.0.0.1:8790/")
        self.assertEqual(toasts[0].button, "Open board")
        self.assertEqual(toasts[0].kind, "needs_you")

    # ------------------------------------------------------------ compose
    def alert(self, kind="finished", note="ok", stopped=False):
        return notify.Alert(kind, "KMU-1", "KMU Beratung", "Build it", note, stopped)

    def test_compose_single_finished(self):
        t, = notify.compose([self.alert()], notify.Settings())
        self.assertEqual(
            (t.heading, t.line1, t.line2, t.url, t.button, t.attribution, t.kind),
            ("KMU-1 is ready to check", "Build it", "ok", "http://127.0.0.1:8790/t/KMU-1",
             "Open task", "KMU Beratung", "finished"))
        self.assertEqual(t.sound, r"C:\Windows\Media\Windows Notify System Generic.wav")

    def test_compose_needs_you(self):
        t, = notify.compose([self.alert("needs_you", "v3?")], notify.Settings())
        self.assertEqual((t.heading, t.line2), ("KMU-1 needs you", "v3?"))
        self.assertEqual(t.sound, r"C:\Windows\Media\Windows Notify Messaging.wav")

    def test_compose_stopped_wording(self):
        t, = notify.compose([self.alert("needs_you", "n", True)], notify.Settings())
        self.assertEqual(t.line2, "Chat stopped without a report")

    def test_note_is_shortened(self):
        m = notify.mark(self.conn)
        core.release(self.conn, self.tid, "s1", "done", "word " * 60)
        [a] = notify.collect(self.conn, m)
        self.assertEqual(len(a.note), 140)
        self.assertTrue(a.note.endswith("…"))

    def test_sound_off_and_custom(self):
        t, = notify.compose([self.alert()], notify.Settings(sound=False))
        self.assertEqual(t.sound, "")
        t, = notify.compose([self.alert()], notify.Settings(sound_finished="C:\\x.wav"))
        self.assertEqual(t.sound, "C:\\x.wav")

    def test_disabled_gives_nothing(self):
        self.assertEqual(notify.compose([self.alert()], notify.Settings(enabled=False)), [])

    def test_custom_port_in_url(self):
        t, = notify.compose([self.alert()], notify.Settings(board_port=9000))
        self.assertEqual(t.url, "http://127.0.0.1:9000/t/KMU-1")

    # ------------------------------------------------------------ fire
    def test_fire_shows_and_starts_board(self):
        with mock.patch.object(notify.toast, "show") as show, \
                mock.patch.object(notify.toast, "board_up", return_value=False), \
                mock.patch.object(notify.toast, "start_board") as start, \
                mock.patch.object(notify.toast, "registered", return_value=True):
            m = notify.mark(self.conn)
            core.release(self.conn, self.tid, "s1", "done", "ok")
            notify.fire(self.conn, m, [self.tid])
        start.assert_called_once_with(8790)
        self.assertEqual(show.call_args.args[0].heading, f"{self.tid} is ready to check")
        self.assertEqual(show.call_args.args[1], "Coordinator.Board")

    def test_fire_board_already_up_is_not_started(self):
        with mock.patch.object(notify.toast, "show"), \
                mock.patch.object(notify.toast, "board_up", return_value=True), \
                mock.patch.object(notify.toast, "start_board") as start:
            m = notify.mark(self.conn)
            core.release(self.conn, self.tid, "s1", "done", "ok")
            notify.fire(self.conn, m)
        start.assert_not_called()

    def test_fire_falls_back_to_powershell_aumid(self):
        with mock.patch.object(notify.toast, "show") as show, \
                mock.patch.object(notify.toast, "board_up", return_value=True), \
                mock.patch.object(notify.toast, "registered", return_value=False):
            m = notify.mark(self.conn)
            core.release(self.conn, self.tid, "s1", "done", "ok")
            notify.fire(self.conn, m)
        self.assertEqual(show.call_args.args[1], notify.toast.PS_AUMID)

    def test_fire_nothing_to_show_touches_nothing(self):
        with mock.patch.object(notify.toast, "show") as show, \
                mock.patch.object(notify.toast, "board_up") as up:
            notify.fire(self.conn, notify.mark(self.conn))
        show.assert_not_called()
        up.assert_not_called()

    def test_fire_swallows_errors(self):
        with mock.patch.object(notify.toast, "show", side_effect=OSError("boom")), \
                mock.patch.object(notify.toast, "board_up", return_value=True):
            m = notify.mark(self.conn)
            core.release(self.conn, self.tid, "s1", "done", "ok")
            self.assertIsNone(notify.fire(self.conn, m))
        log = (paths.app_home() / "notify-errors.log").read_text(encoding="utf-8")
        self.assertIn("fire", log)

    # ------------------------------------------------------------ settings
    def test_settings_file(self):
        (paths.app_home() / "notify.toml").write_text(
            "enabled = false\nboard_port = 9000\n", encoding="utf-8")
        self.assertEqual(notify.load_settings(),
                         notify.Settings(enabled=False, board_port=9000))

    def test_missing_settings_are_defaults(self):
        self.assertEqual(notify.load_settings(), notify.Settings())

    def test_broken_settings_use_defaults(self):
        (paths.app_home() / "notify.toml").write_text("enabled = [", encoding="utf-8")
        self.assertEqual(notify.load_settings(), notify.Settings())
        self.assertTrue((paths.app_home() / "notify-errors.log").exists())

    def test_wrong_type_falls_back(self):
        (paths.app_home() / "notify.toml").write_text(
            'board_port = "x"\nsound = 1\n', encoding="utf-8")
        self.assertEqual(notify.load_settings(), notify.Settings())


if __name__ == "__main__":
    unittest.main()
