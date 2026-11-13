import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)

from coordinator import core, db  # noqa: E402

PERSON = core.Actor("person", "daria")
REPO = Path(__file__).resolve().parent.parent


class McpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_mcp_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        root = Path(self.tmp, "proj")
        (root / ".coordinator").mkdir(parents=True)
        (root / ".coordinator" / "project.toml").write_text(
            'key = "KMU"\nname = "N"\n', encoding="utf-8")
        old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = self.home
        try:
            conn = db.connect()
            try:
                core.add_project(conn, str(root), PERSON)
            finally:
                conn.close()
        finally:
            if old is None:
                os.environ.pop("COORDINATOR_HOME", None)
            else:
                os.environ["COORDINATOR_HOME"] = old
        self.sink = os.path.join(self.tmp, "sink.jsonl")
        env = dict(os.environ, COORDINATOR_HOME=self.home,
                   COORDINATOR_NOTIFY_SINK=self.sink)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "coordinator.mcp_server"], cwd=str(REPO),
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.addCleanup(self._stop)
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.next_id = 0

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
        except (ValueError, OSError):
            pass
        self.lines.put(None)

    def _stop(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def send_raw(self, data: bytes):
        self.proc.stdin.write(data + b"\n")
        self.proc.stdin.flush()

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        self.send_raw(json.dumps(msg).encode("utf-8"))

    def recv(self):
        try:
            line = self.lines.get(timeout=15)
        except queue.Empty:
            self.proc.kill()
            self.fail("server did not answer within 15s")
        self.assertIsNotNone(line, "server closed stdout")
        return json.loads(line.decode("utf-8"))

    def rpc(self, method, params=None):
        self.send(method, params)
        return self.recv()

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = r["content"][0]["text"]
        return r["isError"], (text if r["isError"] else json.loads(text))

    def test_initialize_and_list_tools(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26",
                                    "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(r["result"]["serverInfo"]["name"], "coordinator")
        self.send("notifications/initialized", notify=True)
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), sorted([
            "task_list", "task_show", "task_create", "task_claim", "task_release",
            "task_set_state", "task_comment", "task_update", "task_lower_mode",
            "task_overlaps"]))
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])

    def test_task_create_backlog_note(self):
        err, t = self.call("task_create", session_id="s1", project="KMU",
                           title="Maybe later", backlog=True)
        self.assertFalse(err, t)
        self.assertEqual(t["state"], "backlog")
        self.assertEqual(t["goal"], "")
        err, text = self.call("task_claim", session_id="s2", id=t["id"])
        self.assertTrue(err)
        self.assertIn("backlog", text)

    def test_other_chat_specifies_note_with_forms_in_one_update(self):
        _, t = self.call("task_create", session_id="s1", project="KMU",
                         title="Later", backlog=True)
        err, out = self.call("task_update", session_id="s2", id=t["id"],
                             fields={"goal": "G", "forms": ["497"]})
        self.assertFalse(err, out)
        _, shown = self.call("task_show", id=t["id"])
        self.assertEqual((shown["goal"], shown["forms"]), ("G", ["497"]))

    def test_task_create_backlog_missing_state_is_error(self):
        root = Path(self.tmp, "nobl")
        (root / ".coordinator").mkdir(parents=True)
        (root / ".coordinator" / "project.toml").write_text(
            'key = "NB"\nname = "No backlog"\n'
            '[[states]]\nname = "open"\nkind = "waiting"\n'
            '[[states]]\nname = "building"\nkind = "active"\n'
            '[[states]]\nname = "clarify"\nkind = "handover"\n'
            '[[states]]\nname = "review"\nkind = "handover"\n'
            '[[states]]\nname = "done"\nkind = "finished"\n', encoding="utf-8")
        old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = self.home
        try:
            conn = db.connect()
            try:
                core.add_project(conn, str(root), PERSON)
            finally:
                conn.close()
        finally:
            if old is None:
                os.environ.pop("COORDINATOR_HOME", None)
            else:
                os.environ["COORDINATOR_HOME"] = old
        err, text = self.call("task_create", session_id="s1", project="NB",
                              title="x", backlog=True)
        self.assertTrue(err)
        self.assertEqual([l.split(":")[0] for l in text.splitlines()[:3]],
                         ["WHAT", "WHY", "FIX"])

    def test_create_claim_release_round_trip(self):
        err, t = self.call("task_create", session_id="s1", project="KMU",
                           title="Do it", goal="g")
        self.assertFalse(err, t)
        tid = t["id"]
        err, t = self.call("task_claim", session_id="s1", id=tid)
        self.assertFalse(err, t)
        self.assertEqual(
            self.call("task_show", id=tid)[1]["claims"][0]["session_id"], "s1")
        err, t = self.call("task_comment", session_id="s1", id=tid,
                           kind="question", text="why?")
        self.assertFalse(err, t)
        err, t = self.call("task_update", session_id="s1", id=tid,
                           fields={"goal": "new", "open_points": ["a"],
                                   "blocked": "waiting"})
        self.assertFalse(err, t)
        self.assertEqual(t["goal"], "new")
        self.assertEqual(t["open_points"], ["a"])
        err, t = self.call("task_release", session_id="s1", id=tid,
                           outcome="done", note="finished")
        self.assertFalse(err, t)
        self.assertEqual(len(self.call("task_list", project="KMU")[1]), 1)
        self.assertEqual(self.call("task_list", state="nope")[1], [])

    def sunk(self):
        if not os.path.isfile(self.sink):
            return []
        with open(self.sink, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def claimed_task(self):
        err, t = self.call("task_create", session_id="s1", project="KMU", title="Do it")
        self.assertFalse(err, t)
        err, t = self.call("task_claim", session_id="s1", id=t["id"])
        self.assertFalse(err, t)
        return t["id"]

    def test_release_done_sends_toast(self):
        tid = self.claimed_task()
        err, t = self.call("task_release", session_id="s1", id=tid,
                           outcome="done", note="all good")
        self.assertFalse(err, t)
        [rec] = self.sunk()
        self.assertRegex(rec["heading"], rf"^{tid} spec . \w+ is ready to check$")
        self.assertEqual((rec["line2"], rec["kind"]), ("all good", "finished"))

    def test_set_state_clarify_sends_toast(self):
        tid = self.claimed_task()
        err, t = self.call("task_set_state", session_id="s1", id=tid,
                           state="clarify", note="v3 or v4?")
        self.assertFalse(err, t)
        [rec] = self.sunk()
        self.assertRegex(rec["heading"], rf"^{tid} spec . \w+ needs you$")
        self.assertEqual(rec["line2"], "v3 or v4?")

    def test_release_survives_toast_failure(self):
        tid = self.claimed_task()
        os.makedirs(self.sink)  # appending to a directory fails inside notify
        err, t = self.call("task_release", session_id="s1", id=tid,
                           outcome="done", note="all good")
        self.assertFalse(err, t)
        self.assertEqual(t["state"], "review")
        self.assertTrue(os.path.isfile(os.path.join(self.home, "notify-errors.log")))

    def test_person_only_action_returns_is_error(self):
        _, t = self.call("task_create", session_id="s1", project="KMU", title="x")
        self.call("task_claim", session_id="s1", id=t["id"])
        before = self.call("task_show", id=t["id"])[1]["state"]
        err, text = self.call("task_set_state", session_id="s1", id=t["id"],
                              state="done")
        self.assertTrue(err)
        self.assertTrue(text.startswith("WHAT:"))
        self.assertEqual(self.call("task_show", id=t["id"])[1]["state"], before)
        conn = db.connect(Path(self.home) / "coordinator.db")
        try:
            n = conn.execute("SELECT COUNT(*) FROM event WHERE action = 'refused'"
                             ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_raising_mode_refused(self):
        _, t = self.call("task_create", session_id="s1", project="KMU", title="x")
        self.call("task_claim", session_id="s1", id=t["id"])
        err, text = self.call("task_lower_mode", session_id="s1", id=t["id"],
                              mode="write")
        self.assertTrue(err)
        self.assertTrue(text.startswith("WHAT:"))

    def test_validation_errors(self):
        err, text = self.call("task_create", session_id="s1", project="KMU")
        self.assertTrue(err)
        self.assertTrue(text.startswith("WHAT:"))
        err, text = self.call("no_such_tool")
        self.assertTrue(err)
        self.assertTrue(text.startswith("WHAT:"))
        err, text = self.call("task_show", id="KMU-999")
        self.assertTrue(err)

    def test_update_bad_open_points_changes_nothing(self):
        _, t = self.call("task_create", session_id="s1", project="KMU", title="x",
                         goal="old")
        self.call("task_claim", session_id="s1", id=t["id"])
        for bad in ("a string", [1, 2], ["ok", None], {"a": 1}):
            err, text = self.call("task_update", session_id="s1", id=t["id"],
                                  fields={"goal": "new", "open_points": bad,
                                          "blocked": "b"})
            self.assertTrue(err, bad)
            self.assertTrue(text.startswith("WHAT:"), text)
            self.assertIn("open_points", text)
            shown = self.call("task_show", id=t["id"])[1]
            self.assertEqual((shown["goal"], shown["open_points"], shown["blocked"]),
                             ("old", [], None))

    def test_bad_json_line_does_not_kill_server(self):
        self.send_raw(b"{")
        r = self.recv()
        self.assertEqual(r["error"]["code"], -32700)
        self.assertIsNone(r["id"])
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_unknown_method(self):
        self.assertEqual(self.rpc("bogus/method")["error"]["code"], -32601)

    def test_invalid_request_shape(self):
        self.send_raw(b"[1]")
        self.assertEqual(self.recv()["error"]["code"], -32600)

    def test_notification_gets_no_response(self):
        self.send("notifications/whatever", notify=True)
        self.send("ping")
        r = self.recv()
        self.assertEqual(r["id"], self.next_id)
        self.assertEqual(r["result"], {})


if __name__ == "__main__":
    unittest.main()
