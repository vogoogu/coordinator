"""Connecting a project registers its Antson forms with the guard (fake mode.py)."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)

from coordinator import antson, cli  # noqa: E402

FAKE_MODE_PY = '''
import json, os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.path.exists(os.path.join(here, "crash")):
    print("Traceback: boom", file=sys.stderr)
    sys.exit(2)
if os.path.exists(os.path.join(here, "slow")):
    import time
    time.sleep(30)
text = open(os.path.join(sys.argv[2], "CLAUDE.md"), encoding="utf-8").read()
ids = re.findall(r"(\\d+)\\s*=", re.search(r"^\\s*-\\s*Antson forms:\\s*(.+)$", text, re.M).group(1))
print("seeded: " + ", ".join(ids))
'''

DEMO = "# DEMO\\n- Antson forms: 596=DEMO Beratungsstrecke, 615=Vollmacht, 624=Beratungsprotokoll\\n"


class SeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_seed_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.guard = Path(self.tmp, "guard")
        self.guard.mkdir()
        self.mode_py = self.guard / "mode.py"
        self.mode_py.write_text(FAKE_MODE_PY, encoding="utf-8")
        keys = ("COORDINATOR_HOME", "COORDINATOR_GUARD_MODE_PY", "ANTSON_STATE_DIR")
        self._env = {k: os.environ.get(k) for k in keys}
        os.environ["COORDINATOR_HOME"] = str(Path(self.tmp, "home"))
        os.environ["COORDINATOR_GUARD_MODE_PY"] = str(self.mode_py)
        os.environ["ANTSON_STATE_DIR"] = str(Path(self.tmp, "antson"))
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def calls(self):
        f = self.guard / "calls.jsonl"
        rows = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        return [[r[0], os.path.normcase(os.path.realpath(r[1])), r[2]] if len(r) == 3 else r for r in rows]

    def project(self, claude_md=None, name="kmu"):
        folder = Path(self.tmp, name)
        folder.mkdir()
        if claude_md is not None:
            (folder / "CLAUDE.md").write_text(claude_md.replace("\\n", "\n"), encoding="utf-8")
        return folder

    def init(self, folder, key="DEMO"):
        return self.run_cli("init-project", str(folder), "--key", key, "--name", "DEMO")

    def test_init_project_seeds_forms(self):
        folder = self.project(DEMO)
        code, out, err = self.init(folder)
        self.assertEqual(code, 0, err)
        self.assertIn("seeded: 596, 615, 624", out)
        self.assertEqual(self.calls(), [["seed", os.path.normcase(os.path.realpath(folder)), "DEMO"]])

    def test_add_project_seeds_forms(self):
        first = self.project(None, name="first")           # no forms line: no call
        self.assertEqual(self.init(first, key="AA")[0], 0)
        other = self.project(DEMO, name="kmu2")
        (other / ".coordinator").mkdir()
        toml = (first / ".coordinator" / "project.toml").read_text(encoding="utf-8")
        (other / ".coordinator" / "project.toml").write_text(
            toml.replace('key = "AA"', 'key = "DEMO"'), encoding="utf-8")
        code, out, err = self.run_cli("add-project", str(other))
        self.assertEqual(code, 0, err)
        self.assertIn("seeded: 596, 615, 624", out)
        self.assertEqual(self.calls(), [["seed", os.path.normcase(os.path.realpath(other)), "DEMO"]])

    def test_no_forms_line_means_no_call(self):
        for claude_md in (None, "# Plain project\\n"):
            folder = self.project(claude_md, name=f"p{claude_md is None}")
            self.assertIsNone(antson.seed_project(folder, "PX"))
        self.assertEqual(self.calls(), [])

    def test_failing_guard_never_blocks_connect(self):
        (self.guard / "crash").write_text("1")
        folder = self.project(DEMO)
        code, out, err = self.init(folder)
        self.assertEqual(code, 0, err)
        self.assertIn("registered at", out)
        self.assertIn("were not registered", out)
        self.assertTrue((folder / ".coordinator" / "project.toml").is_file())

    def test_missing_guard_never_blocks_connect(self):
        os.environ["COORDINATOR_GUARD_MODE_PY"] = str(Path(self.tmp, "nowhere", "mode.py"))
        code, out, err = self.init(self.project(DEMO))
        self.assertEqual(code, 0, err)
        self.assertIn("mode.py was not found", out)

    def test_timeout_never_blocks_connect(self):
        (self.guard / "slow").write_text("1")
        old = antson.SEED_TIMEOUT
        antson.SEED_TIMEOUT = 1
        self.addCleanup(setattr, antson, "SEED_TIMEOUT", old)
        code, out, err = self.init(self.project(DEMO))
        self.assertEqual(code, 0, err)
        self.assertIn("did not answer in time", out)


if __name__ == "__main__":
    unittest.main()
