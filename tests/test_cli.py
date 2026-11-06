import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from coordinator import board, cli, core, db, paths


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_cli_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = os.path.join(self.tmp, "home")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("COORDINATOR_HOME", None)
        else:
            os.environ["COORDINATOR_HOME"] = self._old

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def init(self, name="proj", key="PX"):
        folder = os.path.join(self.tmp, name)
        code, out, err = self.run_cli("init-project", folder, "--key", key,
                                      "--name", 'My "Proj"')
        self.assertEqual(code, 0, err)
        return folder

    def test_init_project_writes_toml_and_registers(self):
        folder = self.init()
        toml = Path(folder, ".coordinator", "project.toml")
        self.assertTrue(toml.is_file())
        text = toml.read_text(encoding="utf-8")
        self.assertIn('on_done = "review"', text)
        conn = db.connect()
        try:
            projects = core.list_projects(conn)
        finally:
            conn.close()
        self.assertEqual([p["key"] for p in projects], ["PX"])
        self.assertEqual(projects[0]["name"], 'My "Proj"')

    def test_init_project_refuses_overwrite(self):
        folder = self.init()
        toml = Path(folder, ".coordinator", "project.toml")
        before = toml.read_text(encoding="utf-8")
        code, out, err = self.run_cli("init-project", folder, "--key", "PY",
                                      "--name", "Other")
        self.assertEqual(code, 1)
        self.assertIn("WHAT:", err)
        self.assertEqual(toml.read_text(encoding="utf-8"), before)

    def test_person_state_done_works(self):
        self.init()
        code, out, err = self.run_cli("create", "PX", "First task")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("state", "PX-1", "done")
        self.assertEqual(code, 0, err)
        conn = db.connect()
        try:
            self.assertEqual(core.get_task(conn, "PX-1")["state"], "done")
            self.assertFalse(core.get_task(conn, "PX-1")["inbox"])
        finally:
            conn.close()

    def test_unknown_task_prints_what_why_fix(self):
        code, out, err = self.run_cli("show", "NOPE-9")
        self.assertEqual(code, 1)
        for word in ("WHAT:", "WHY:", "FIX:"):
            self.assertIn(word, err)

    def test_list_and_show_contain_task(self):
        self.init()
        self.run_cli("create", "PX", "Find the bug", "--goal", "g1")
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0, err)
        self.assertIn("PX-1", out)
        self.assertIn("Find the bug", out)
        code, out, err = self.run_cli("list", "--project", "PX")
        self.assertIn("PX-1", out)
        code, out, err = self.run_cli("show", "PX-1")
        self.assertEqual(code, 0, err)
        self.assertIn("Find the bug", out)
        self.assertIn("g1", out)
        self.assertIn("created", out)

    def test_show_includes_resume_command(self):
        self.init()
        self.run_cli("create", "PX", "T")
        conn = db.connect()
        try:
            core.claim(conn, "PX-1", "sess-123", transcript_path="C:/t/x.jsonl")
        finally:
            conn.close()
        code, out, err = self.run_cli("show", "PX-1")
        self.assertIn("claude --resume sess-123", out)
        self.assertIn("C:/t/x.jsonl", out)

    def test_release_without_live_claim_refused(self):
        self.init()
        self.run_cli("create", "PX", "T")
        code, out, err = self.run_cli("release", "PX-1")
        self.assertEqual(code, 1)
        self.assertIn("WHAT:", err)

    def test_enforcement_default_is_warn(self):
        code, out, err = self.run_cli("enforcement")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "warn")

    def test_enforcement_block_recorded(self):
        code, out, err = self.run_cli("enforcement", "block")
        self.assertEqual(code, 0, err)
        self.assertIn("block", out)
        code, out, err = self.run_cli("enforcement")
        self.assertEqual(out.strip(), "block")
        conn = db.connect()
        try:
            ev = conn.execute("SELECT * FROM event WHERE action = 'enforcement'"
                              ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(ev), 1)
        self.assertIsNone(ev[0]["task_id"])
        self.assertEqual((ev[0]["from_value"], ev[0]["to_value"]), ("warn", "block"))
        self.assertEqual(ev[0]["actor_type"], "person")

    def test_enforcement_invalid_refused(self):
        code, out, err = self.run_cli("enforcement", "loud")
        self.assertEqual(code, 1)
        self.assertIn("WHAT:", err)
        code, out, err = self.run_cli("enforcement")
        self.assertEqual(out.strip(), "warn")

    def test_expire_prints_ids(self):
        self.init()
        self.run_cli("create", "PX", "T")
        conn = db.connect()
        try:
            core.claim(conn, "PX-1", "s1")
        finally:
            conn.close()
        code, out, err = self.run_cli("expire", "--hours=-1")
        self.assertEqual(code, 0, err)
        self.assertIn("PX-1", out)

    def test_backup_keeps_14(self):
        dest = Path(self.tmp, "bk")
        dest.mkdir()
        today = date.today()
        for i in range(1, 17):
            (dest / f"{today - timedelta(days=i + 1)}.db").write_text("x")
        other = dest / "notes.txt"
        other.write_text("keep me")
        conn = db.connect()
        try:
            out = db.backup(conn, dest)
        finally:
            conn.close()
        self.assertEqual(out, dest / f"{today}.db")
        dated = sorted(p.name for p in dest.glob("*.db"))
        self.assertEqual(len(dated), 14)
        self.assertIn(f"{today}.db", dated)
        self.assertEqual(other.read_text(), "keep me")
        db.connect(out).close()

    def test_backup_command_writes_to_app_home(self):
        code, out, err = self.run_cli("backup")
        self.assertEqual(code, 0, err)
        self.assertTrue((paths.app_home() / "backups" / f"{date.today()}.db").is_file())

    def test_board_backs_up_then_serves(self):
        with mock.patch.object(board, "serve") as serve:
            code, out, err = self.run_cli("board", "--port", "9999", "--no-browser")
        self.assertEqual(code, 0, err)
        serve.assert_called_once_with(port=9999, open_browser=False)
        self.assertTrue((paths.app_home() / "backups" / f"{date.today()}.db").is_file())

    def test_install_help(self):
        with mock.patch("subprocess.run") as run:
            code, out, err = self.run_cli("install-help")
        run.assert_not_called()
        self.assertEqual(code, 0, err)
        for word in ("SessionStart", "PreToolUse", "PostToolUse", "Stop",
                     "coordinator/hooks.py", "claude mcp add",
                     "coordinator.mcp_server"):
            self.assertIn(word, out)
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        hooks = data["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "PreToolUse", "PostToolUse", "Stop"})
        self.assertEqual(hooks["PreToolUse"][0]["matcher"],
                         "Write|Edit|MultiEdit|NotebookEdit")
        self.assertNotIn("matcher", hooks["PostToolUse"][0])
        cmd = hooks["Stop"][0]["hooks"][0]
        self.assertEqual(cmd["timeout"], 30)
        self.assertTrue(cmd["command"].startswith('python "'))
        self.assertTrue(cmd["command"].endswith('/coordinator/hooks.py" stop'))
        self.assertIn("--scope user --env PYTHONPATH=", out)


if __name__ == "__main__":
    unittest.main()
