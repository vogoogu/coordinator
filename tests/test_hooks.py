import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)

from coordinator import core, db, hooks  # noqa: E402

PERSON = core.Actor("person", "daria")
REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name, **kw):
    with open(FIXTURES / name, encoding="utf-8") as f:
        data = json.load(f)
    data.update(kw)
    return data


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_hooks_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = os.path.join(self.tmp, "home")
        self.addCleanup(self._restore)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.root = self.make_project("kmu_project_with_a_long_folder_name", "KMU")
        self.other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(self.other)

    def _restore(self):
        if self._old is None:
            os.environ.pop("COORDINATOR_HOME", None)
        else:
            os.environ["COORDINATOR_HOME"] = self._old

    def make_project(self, folder, key):
        root = Path(self.tmp, folder)
        (root / ".coordinator").mkdir(parents=True)
        (root / ".coordinator" / "project.toml").write_text(
            f'key = "{key}"\nname = "N"\n', encoding="utf-8")
        core.add_project(self.conn, str(root), PERSON)
        return str(root)

    def task(self, mode="write", title="Build it", **kw):
        t = core.create_task(self.conn, "KMU", title, PERSON, **kw)
        if mode != "read":
            core.set_mode(self.conn, t["id"], mode, PERSON)
        return t["id"]

    def set_enforcement(self, value):
        self.conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES"
                          " ('enforcement', ?)", (value,))

    def start(self, **kw):
        p = fixture("session_start.json", cwd=self.root, **kw)
        return hooks.handle("session-start", p, {})

    def pre(self, target=None, tool="Write", key="file_path", **kw):
        target = target or os.path.join(self.root, "a.txt")
        p = fixture("pre_tool.json", cwd=self.root, tool_name=tool,
                    tool_input={key: target}, **kw)
        return hooks.handle("pre-tool", p, {})

    def post(self, target=None, tool="Write"):
        target = target or os.path.join(self.root, "a.txt")
        p = fixture("post_tool.json", cwd=self.root, tool_name=tool,
                    tool_input={"file_path": target})
        return hooks.handle("post-tool", p, {})

    def stop(self, active=False):
        p = fixture("stop.json", cwd=self.root, stop_hook_active=active)
        return hooks.handle("stop", p, {})

    def claimed(self, mode="write"):
        tid = self.task(mode)
        core.claim(self.conn, tid, "sess-1")
        return tid

    def work(self):
        """A write by the claimed session, one minute after its last task update
        (timestamps have a resolution of one second)."""
        self.conn.execute("UPDATE session_stat SET last_task_update ="
                          " '2020-01-01T00:00:00+00:00' WHERE session_id = 'sess-1'")
        self.post()

    def counts(self):
        return [self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("event", "comment", "claim", "session_stat", "hook_warning")]

    # ---------------------------------------------------------- session-start
    def test_session_start_with_coord_task_claims_and_prints_packet(self):
        tid = self.task(goal="the goal", done_when="all green", scope="src")
        p = fixture("session_start.json", cwd=self.root)
        r = hooks.handle("session-start", p, {"COORD_TASK": tid})
        self.assertEqual(r.exit_code, 0)
        root = core.get_project(self.conn, "KMU")["root"]
        for text in ("KMU-1", "Build it", "the goal", "all green", "release",
                     "task_release", "sess-1", root):
            self.assertIn(text, r.stdout)
        self.assertIsNotNone(core.claim_for_session(self.conn, "sess-1"))

    def test_session_start_packet_has_last_three_notes(self):
        tid = self.task()
        core.claim(self.conn, tid, "old")
        for i in range(4):
            core.release(self.conn, tid, "old", "progress", f"note-{i}")
        core.release(self.conn, tid, "old", "done", "handoff-text")
        core.set_state(self.conn, tid, "open", PERSON)  # back to waiting
        r = hooks.handle("session-start", fixture("session_start.json", cwd=self.root),
                         {"COORD_TASK": tid})
        self.assertNotIn("note-0", r.stdout)
        self.assertNotIn("note-1", r.stdout)
        for t in ("note-2", "note-3", "handoff-text"):
            self.assertIn(t, r.stdout)

    def test_session_start_reattaches_existing_claim(self):
        tid = self.claimed()
        r = self.start(source="clear")
        self.assertEqual(r.exit_code, 0)
        self.assertIn(tid, r.stdout)
        self.assertIn("sess-1", r.stdout)
        self.assertEqual(self.counts()[2], 1)

    def test_session_start_without_claim_prints_one_line_with_session_id(self):
        self.task()
        r = self.start()
        self.assertEqual(r.exit_code, 0)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertIn("sess-1", r.stdout)
        self.assertIn("has 1 open task(s)", r.stdout)

    def test_session_start_refused_claim_prints_refusal(self):
        tid = self.task()
        core.claim(self.conn, tid, "other")
        core.set_state(self.conn, tid, "open", PERSON)  # waiting, claim still live
        r = hooks.handle("session-start", fixture("session_start.json", cwd=self.root),
                         {"COORD_TASK": tid})
        self.assertEqual(r.exit_code, 0)
        self.assertIn("WHAT:", r.stdout)
        self.assertIn("FIX:", r.stdout)
        self.assertIn("Your session id is sess-1", r.stdout)
        self.assertIn("has 1 open task(s)", r.stdout)

    # ------------------------------ COORD_TASK only on startup, waiting (R30)
    def launch(self, tid, source=None, sid="sess-1", token=None):
        p = fixture("session_start.json", cwd=self.root, session_id=sid)
        if source is None:
            p.pop("source")
        else:
            p["source"] = source
        env = {"COORD_TASK": tid}
        if token is not None:
            env["COORD_LAUNCH"] = token
        return hooks.handle("session-start", p, env)

    def test_coord_task_without_source_claims(self):
        tid = self.task()
        r = self.launch(tid, source=None)
        self.assertIn("You hold task " + tid, r.stdout)
        self.assertEqual(core.claim_for_session(self.conn, "sess-1")["display_id"], tid)

    def test_compact_or_resume_after_done_does_not_reclaim(self):
        for source in ("compact", "resume", "clear"):
            with self.subTest(source=source):
                tid = self.task(title="t-" + source)
                self.launch(tid, "startup")
                core.release(self.conn, tid, "sess-1", "done", "finished")
                claims = self.counts()[2]
                r = self.launch(tid, source)
                self.assertEqual(r.exit_code, 0)
                self.assertEqual(core.get_task(self.conn, tid)["state"], "review")
                self.assertEqual(self.counts()[2], claims)
                self.assertIsNone(core.claim_for_session(self.conn, "sess-1"))
                self.assertEqual(len(r.stdout.strip().splitlines()), 1)
                self.assertIn("Your session id is sess-1", r.stdout)

    def test_startup_again_after_done_does_not_reclaim(self):
        # typing `claude` again in the same console window: new session id
        tid = self.task()
        self.launch(tid, "startup")
        core.release(self.conn, tid, "sess-1", "done", "finished")
        r = self.launch(tid, "startup", sid="sess-9")
        self.assertEqual(core.get_task(self.conn, tid)["state"], "review")
        self.assertIsNone(core.claim_for_session(self.conn, "sess-9"))
        self.assertNotIn("WHAT:", r.stdout)
        self.assertIn("Your session id is sess-9", r.stdout)

    def test_compact_keeps_reattach_with_coord_task(self):
        tid = self.task()
        self.launch(tid, "startup")
        r = self.launch(tid, "compact")
        self.assertIn("You hold task " + tid, r.stdout)
        self.assertEqual(self.counts()[2], 1)

    def test_startup_coord_task_active_claimed_by_other_falls_back(self):
        tid = self.task()
        core.claim(self.conn, tid, "other")
        before = self.counts()
        r = self.launch(tid, "startup")
        self.assertNotIn("WHAT:", r.stdout)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertEqual(self.counts(), before)

    # ------------------------------------------- /clear with launch token (R32)
    def test_clear_with_same_launch_token_reattaches(self):
        tid = self.task(goal="the goal")
        self.launch(tid, "startup", sid="sess-1", token="abc123")
        r = self.launch(tid, "clear", sid="sess-2", token="abc123")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("You hold task " + tid, r.stdout)
        self.assertIn("sess-2", r.stdout)
        self.assertIn("the goal", r.stdout)
        self.assertEqual(core.claim_for_session(self.conn, "sess-2")["display_id"], tid)
        self.assertIsNone(core.claim_for_session(self.conn, "sess-1"))
        claims = core.get_task(self.conn, tid)["claims"]
        self.assertEqual(claims[0]["end_reason"], "clear")
        self.assertEqual(core.get_task(self.conn, tid)["state"], "building")

    def test_clear_with_other_or_missing_token_does_not_steal(self):
        tid = self.task()
        self.launch(tid, "startup", sid="sess-1", token="abc123")
        for token in ("zzz999", None):
            with self.subTest(token=token):
                before = self.counts()
                r = self.launch(tid, "clear", sid="sess-2", token=token)
                self.assertEqual(r.exit_code, 0)
                self.assertNotIn("You hold task", r.stdout)
                self.assertEqual(self.counts()[:3], before[:3])
                self.assertEqual(
                    core.claim_for_session(self.conn, "sess-1")["display_id"], tid)
                self.assertIsNone(core.claim_for_session(self.conn, "sess-2"))

    def test_startup_stores_launch_token(self):
        tid = self.task()
        self.launch(tid, "startup", token="abc123")
        self.assertEqual(core.get_task(self.conn, tid)["claims"][0]["launch"], "abc123")

    def test_session_start_expires_old_leases(self):
        tid = self.claimed()
        self.conn.execute("UPDATE claim SET last_seen = '2020-01-01T00:00:00+00:00'")
        self.start(session_id="sess-2")
        self.assertIsNone(core.claim_for_session(self.conn, "sess-1"))
        self.assertEqual(core.get_task(self.conn, tid)["state"], "clarify")

    def test_outside_project_does_nothing(self):
        self.task()
        before = self.counts()
        for event, name, extra in (
                ("session-start", "session_start.json", {}),
                ("pre-tool", "pre_tool.json",
                 {"tool_input": {"file_path": os.path.join(self.other, "a.txt")}}),
                ("post-tool", "post_tool.json", {}),
                ("stop", "stop.json", {})):
            r = hooks.handle(event, fixture(name, cwd=self.other, **extra), {})
            self.assertEqual((r.exit_code, r.stdout, r.stderr), (0, "", ""), event)
        self.assertEqual(self.counts(), before)

    # --------------------------------------------------------------- pre-tool
    def test_pre_tool_blocks_without_claim_when_block(self):
        self.set_enforcement("block")
        r = self.pre()
        self.assertEqual(r.exit_code, 2)
        self.assertTrue(r.stderr.startswith("WHAT:"))
        self.assertIn("FIX:", r.stderr)

    def test_pre_tool_warns_without_claim_when_warn(self):
        r = self.pre()  # no setting row: default warn
        self.assertEqual((r.exit_code, r.stdout, r.stderr), (0, "", ""))
        r = self.post()
        self.assertEqual(r.exit_code, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        self.assertIn("claim", out["additionalContext"].lower())
        # shown once per rule per session
        self.pre()
        self.assertEqual(self.post().stdout, "")

    def test_post_tool_warning_only_once_per_rule(self):
        self.pre()
        self.assertNotEqual(self.post().stdout, "")
        self.pre()
        self.pre()
        self.assertEqual(self.post().stdout, "")
        n = self.conn.execute("SELECT COUNT(*) FROM hook_warning").fetchone()[0]
        self.assertEqual(n, 1)

    def test_pre_tool_blocks_read_mode_write(self):
        self.set_enforcement("block")
        self.claimed(mode="read")
        r = self.pre()
        self.assertEqual(r.exit_code, 2)
        self.assertTrue(r.stderr.startswith("WHAT:"))
        self.assertIn("read", r.stderr)

    def test_pre_tool_allows_write_mode_inside_project(self):
        self.set_enforcement("block")
        self.claimed()
        r = self.pre()
        self.assertEqual((r.exit_code, r.stdout, r.stderr), (0, "", ""))

    def test_pre_tool_ignores_other_tools(self):
        self.set_enforcement("block")
        for tool in ("Read", "Bash", "Grep"):
            r = hooks.handle("pre-tool", fixture("pre_tool.json", cwd=self.root,
                                                 tool_name=tool), {})
            self.assertEqual((r.exit_code, r.stderr), (0, ""))

    def test_pre_tool_blocks_path_outside_project(self):
        self.set_enforcement("block")
        self.claimed()
        r = self.pre(os.path.join(self.other, "a.txt"))
        self.assertEqual(r.exit_code, 2)
        self.assertTrue(r.stderr.startswith("WHAT:"))
        # even when the chat's cwd is outside the project (R11)
        p = fixture("pre_tool.json", cwd=self.other,
                    tool_input={"file_path": os.path.join(self.other, "a.txt")})
        self.assertEqual(hooks.handle("pre-tool", p, {}).exit_code, 2)

    def test_pre_tool_path_spellings_and_notebook_and_relative(self):
        self.set_enforcement("block")
        self.claimed()
        inside = os.path.join(self.root, "sub", "a.txt")
        for spelled in (inside.replace("\\", "/"), inside.upper(), inside.lower()):
            self.assertEqual(self.pre(spelled).exit_code, 0, spelled)
        self.assertEqual(self.pre(inside, tool="NotebookEdit",
                                  key="notebook_path").exit_code, 0)
        self.assertEqual(self.pre(os.path.join(self.other, "n.ipynb"),
                                  tool="NotebookEdit", key="notebook_path").exit_code, 2)
        self.assertEqual(self.pre("sub/rel.txt").exit_code, 0)
        self.assertEqual(self.pre("../elsewhere/rel.txt").exit_code, 2)

    def test_pre_tool_blocks_path_outside_project_short_and_long_names(self):
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(self.root, buf, 1024)
        short = buf.value if n else self.root
        if n == 0 or short.lower() == self.root.lower():
            self.skipTest("8.3 short names are not available")
        self.set_enforcement("block")
        self.claimed()
        self.assertEqual(self.pre(os.path.join(short, "x.txt")).exit_code, 0)
        self.assertEqual(self.pre(os.path.join(short, "..", "elsewhere", "x.txt")
                                  ).exit_code, 2)

    # -------------------------------------------------------------- post-tool
    def test_post_tool_touches_claim_even_outside_project(self):
        self.claimed()
        p = fixture("post_tool.json", cwd=self.other,
                    tool_input={"file_path": os.path.join(self.other, "z.txt")})
        r = hooks.handle("post-tool", p, {})
        self.assertEqual(r.exit_code, 0)
        row = self.conn.execute("SELECT * FROM session_stat WHERE session_id='sess-1'"
                                ).fetchone()
        self.assertEqual(row["last_tool"], "Write")
        self.assertTrue(row["last_path"].endswith("z.txt"))

    def test_post_tool_mcp_tool_is_not_work(self):
        self.claimed()
        p = fixture("post_tool.json", tool_name="mcp__coordinator__task_release",
                    tool_input={"task": "KMU-1"})
        hooks.handle("post-tool", p, {})
        self.assertFalse(core.needs_report(self.conn, "sess-1"))

    # ------------------------------------------------------------------- stop
    def test_stop_blocks_once_when_work_not_reported(self):
        tid = self.claimed()
        self.work()
        r = self.stop()
        self.assertEqual(r.exit_code, 2)
        self.assertTrue(r.stderr.startswith("WHAT: you worked on " + tid))
        self.assertIn("WHY: the next chat and the person rely on it.", r.stderr)
        self.assertIn("FIX: call the MCP tool task_release with id " + tid
                      + ", session_id sess-1 and outcome done", r.stderr)
        self.assertNotIn("Daria", r.stderr)

    def test_stop_ignores_enforcement_setting(self):
        self.set_enforcement("warn")
        self.claimed()
        self.work()
        self.assertEqual(self.stop().exit_code, 2)

    def test_stop_second_time_records_and_moves_to_clarify(self):
        tid = self.claimed()
        self.work()
        r = self.stop(active=True)
        self.assertEqual(r.exit_code, 0)
        t = core.get_task(self.conn, tid)
        self.assertEqual(t["state"], "clarify")
        self.assertIn("stopped without report after reminder",
                      [c["text"] for c in t["comments"]])
        self.assertIsNone(core.claim_for_session(self.conn, "sess-1"))

    def sunk_during(self, fn):
        sink = os.path.join(self.tmp, "sink.jsonl")
        with mock.patch.dict(os.environ, {"COORDINATOR_NOTIFY_SINK": sink}):
            fn()
        if not os.path.isfile(sink):
            return []
        with open(sink, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_stop_without_report_sends_needs_you(self):
        tid = self.claimed()
        self.work()
        [rec] = self.sunk_during(lambda: self.stop(active=True))
        self.assertEqual((rec["heading"], rec["line2"]),
                         (f"{tid} needs you", "Chat stopped without a report"))

    def test_stop_does_not_repeat_other_chats_pop_ups(self):
        tid = self.claimed()
        other = self.task("write")
        core.claim(self.conn, other, "sess-9")
        self.work()
        real = core.stop_without_report

        def with_other_chat_meanwhile(conn, sid, note):
            core.release(conn, other, "sess-9", "done", "other chat")  # its own front fires
            return real(conn, sid, note)

        with mock.patch.object(hooks.core, "stop_without_report", with_other_chat_meanwhile):
            recs = self.sunk_during(lambda: self.stop(active=True))
        self.assertEqual([r["heading"] for r in recs], [f"{tid} needs you"])

    def test_stop_reminder_sends_nothing(self):
        self.claimed()
        self.work()
        self.assertEqual(self.sunk_during(lambda: self.stop()), [])

    def test_session_start_expiry_sends_one_summary(self):
        self.claimed()
        tid2 = self.task("write")
        core.claim(self.conn, tid2, "sess-9")
        self.conn.execute("UPDATE claim SET last_seen = '2020-01-01T00:00:00+00:00'")
        [rec] = self.sunk_during(lambda: self.start(session_id="sess-2"))
        self.assertEqual(rec["heading"], "2 tasks need you")

    def test_stop_race_claim_gone_exits_silently(self):
        self.claimed()
        self.work()
        with mock.patch.object(hooks.core, "claim_for_session", return_value=None):
            r = self.stop()
        self.assertEqual((r.exit_code, r.stdout, r.stderr), (0, "", ""))
        log = Path(os.environ["COORDINATOR_HOME"]) / "hook-errors.log"
        self.assertFalse(log.exists())

    def test_stop_silent_when_only_talking(self):
        self.claimed()
        before = self.counts()
        for active in (False, True):
            r = self.stop(active)
            self.assertEqual((r.exit_code, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.counts(), before)

    # ------------------------------------------------------------ fail open
    def test_unknown_event_and_bad_payload_fail_open(self):
        for event, payload in (("nonsense", {}), ("stop", None), ("pre-tool", {})):
            r = hooks.handle(event, payload, {})
            self.assertEqual(r.exit_code, 0)
        log = Path(os.environ["COORDINATOR_HOME"]) / "hook-errors.log"
        self.assertTrue(log.exists())

    def test_hook_survives_missing_database(self):
        bad = os.path.join(self.tmp, "iamafile")
        Path(bad).write_text("x")
        fake_tmp = os.path.join(self.tmp, "faketemp")
        os.makedirs(fake_tmp)
        os.environ["COORDINATOR_HOME"] = bad
        with mock.patch("tempfile.gettempdir", return_value=fake_tmp):
            r = hooks.handle("pre-tool", fixture("pre_tool.json", cwd=self.root), {})
        self.assertEqual(r.exit_code, 0)
        log = Path(fake_tmp) / "coordinator-hook-errors.log"
        self.assertTrue(log.exists())
        self.assertIn("pre-tool", log.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ subprocess
    def run_proc(self, argv, stdin, **env):
        e = dict(os.environ)
        e.pop("COORD_TASK", None)
        e.update(env)
        return subprocess.run(argv, input=stdin, capture_output=True, cwd=self.other,
                              env=e, timeout=60)

    def test_entry_points_as_script_and_module(self):
        tid = self.task(title="Umlaut äöü")
        payload = json.dumps(fixture("session_start.json", cwd=self.root)).encode()
        script = str(REPO / "coordinator" / "hooks.py")
        r = self.run_proc([sys.executable, script, "session-start"], payload,
                          COORD_TASK=tid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Umlaut äöü", r.stdout.decode("utf-8"))
        core.release(self.conn, tid, "sess-1", "clarify", "bye")
        core.set_state(self.conn, tid, "open", PERSON)  # waiting again (R30)
        r = self.run_proc([sys.executable, "-m", "coordinator.hooks", "session-start"],
                          payload, COORD_TASK=tid, PYTHONPATH=str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(tid, r.stdout.decode("utf-8"))

    def test_hook_survives_invalid_stdin(self):
        script = str(REPO / "coordinator" / "hooks.py")
        for data in (b"not json", b"", b"\xff\xfe", b"[1,2]"):
            r = self.run_proc([sys.executable, script, "pre-tool"], data)
            self.assertEqual(r.returncode, 0, data)
            self.assertEqual(r.stdout, b"")
        r = self.run_proc([sys.executable, script, "bogus-event"], b"{}")
        self.assertEqual(r.returncode, 0)
        r = self.run_proc([sys.executable, script], b"{}")
        self.assertEqual(r.returncode, 0)
        self.assertTrue((Path(os.environ["COORDINATOR_HOME"]) / "hook-errors.log").exists())

    def test_import_failure_fails_open_in_script_mode(self):
        # a lone copy of hooks.py has no `coordinator` package next to it
        lone = os.path.join(self.tmp, "lone", "tools")
        os.makedirs(lone)
        shutil.copy(REPO / "coordinator" / "hooks.py", lone)
        faketmp = os.path.join(self.tmp, "faketmp")
        os.makedirs(faketmp)
        r = self.run_proc([sys.executable, os.path.join(lone, "hooks.py"), "stop"],
                          b"{}", TEMP=faketmp, TMP=faketmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b"")
        log = Path(faketmp) / "coordinator-hook-errors.log"
        self.assertIn("coordinator", log.read_text(encoding="utf-8"))

    def test_handle_fails_open_on_import_failure(self):
        faketmp = os.path.join(self.tmp, "faketmp")
        os.makedirs(faketmp)
        with mock.patch.object(hooks, "_IMPORT_FAILURE", "ImportError: boom"),                 mock.patch.object(hooks, "paths", None),                 mock.patch("tempfile.gettempdir", return_value=faketmp):
            r = hooks.handle("stop", {}, {})
        self.assertEqual(r.exit_code, 0)
        self.assertIn("boom", (Path(faketmp) / "coordinator-hook-errors.log"
                               ).read_text(encoding="utf-8"))

    def test_broken_output_pipe_exits_zero(self):
        class Broken:
            def write(self, b):
                raise BrokenPipeError()
            flush = write

        class Err:
            buffer = Broken()

        class In:
            buffer = mock.Mock(read=lambda: b"{}")

        with mock.patch("sys.stdin", In()), mock.patch("sys.stdout", Err()),                 mock.patch.object(hooks, "handle",
                                  return_value=hooks.HookResult(0, "x", "")),                 mock.patch("os._exit", side_effect=SystemExit(0)) as ex:
            with self.assertRaises(SystemExit):
                hooks.main(["stop"])
        ex.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
