"""A colleague's laptop: no sources.toml, no antson.toml, no mail desk. Everything is found
from the board's own registered projects. Fixtures in a temp folder only."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coordinator import antson, version
from tests import test_antson, test_board_needs

ROOT = Path(__file__).resolve().parent.parent


class CleanLaptopTest(test_board_needs.NeedsBase):
    def write_sources(self):
        # no sources.toml at all
        os.environ["COORDINATOR_SOURCES"] = os.path.join(self.tmp, "no-sources.toml")
        self.addCleanup(os.environ.pop, "COORDINATOR_SOURCES", None)
        test_board_needs.board._outside["data"] = None
        test_board_needs.sources.forget()

    def test_needs_you_uses_registered_projects_that_have_a_state_script(self):
        idx = self.get_json("/api/index")
        self.assertTrue(idx["outside"]["configured"])
        projects = {p["key"]: p for p in idx["projects"]}
        self.assertIn("C22", {i["id"] for i in projects["PA"]["wiki"]["items"]})
        self.assertIsNone(projects["PB"]["wiki"], "PB has no tools/project_state.py")
        self.assertGreaterEqual(projects["PA"]["needs_count"], 2)

    def test_no_projects_with_a_state_script_shows_nothing_outside(self):
        (self.proj / "tools" / "project_state.py").unlink()
        test_board_needs.board._outside["data"] = None
        out = self.get_json("/api/index")["outside"]
        self.assertFalse(out["configured"])

    def test_guard_is_found_in_a_registered_project(self):
        os.environ.pop("COORDINATOR_GUARD_MODE_PY")
        self.assertEqual(antson.mode_py_path(), "")
        self.assertIn("No mode.py found", self.get("/modes")[1])
        guard = Path(self.roots["PB"], "tools", "chrome_guard", "guard")
        guard.mkdir(parents=True)
        (guard / "mode.py").write_text(test_antson.FAKE_MODE_PY, encoding="utf-8")
        self.assertEqual(antson.mode_py_path(), str(guard / "mode.py"))
        self.assertNotIn("The switch was not found", self.get("/modes")[1])
        # antson.toml wins over the project's copy
        Path(os.environ["COORDINATOR_HOME"], "antson.toml").write_text('mode_py = "D:/x/mode.py"\n', encoding="utf-8")
        self.assertEqual(antson.mode_py_path(), "D:/x/mode.py")

    def test_every_page_shows_the_version(self):
        for path in ("/", "/modes", "/p/PA"):
            html = self.get(path)[1]
            self.assertIn('class="ver">Version ' + version.describe(), html, path)


class VersionTest(unittest.TestCase):
    def test_describe_is_short_commit_and_date_or_unknown(self):
        text = version.describe()
        self.assertRegex(text, r"^([0-9a-f]{7,} \(\d{4}-\d\d-\d\d\)|unknown)$")


class ShipCheckTest(unittest.TestCase):
    """tools/check_publish.py on a throw-away git repo that holds a copy of the script."""

    def run_check(self, files):
        tmp = tempfile.mkdtemp(prefix="coord_ship_")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        repo = Path(tmp, "repo")
        (repo / "tools").mkdir(parents=True)
        (repo / "tools" / "check_publish.py").write_text(
            (ROOT / "tools" / "check_publish.py").read_text(encoding="utf-8"), encoding="utf-8")
        terms = Path(tmp, "terms.txt")
        terms.write_text("# client names\nAcmeCorp\n", encoding="utf-8")
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text, encoding="utf-8")
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "x"], check=True)
        env = {**os.environ, "ANTSON_BANNED_TERMS": str(terms)}
        r = subprocess.run([sys.executable, str(repo / "tools" / "check_publish.py"), "--skip-tests"],
                           capture_output=True, text=True, env=env)
        return r.returncode, r.stdout

    def test_clean_repo_passes(self):
        code, out = self.run_check({"a.md": "hello\n", "tests/fixtures/x.json": r"C:\Users\someone\x AcmeCorp"})
        self.assertEqual(code, 0, out)

    def test_personal_path_fails(self):
        code, out = self.run_check({"a.md": r"see C:\Users\someone\Documents" + "\n"})
        self.assertEqual(code, 1)
        self.assertIn("personal path", out)

    def test_onedrive_path_fails(self):
        code, out = self.run_check({"a.md": "in OneDrive - Specific-Group Holding\n"})
        self.assertEqual(code, 1, out)

    def test_client_term_fails(self):
        code, out = self.run_check({"a.md": "for acmecorp only\n"})
        self.assertEqual(code, 1)
        self.assertIn("client term", out)

    def test_personal_files_fail(self):
        for name in ("sources.toml", "antson.toml", "notify.toml", "coordinator.db"):
            code, out = self.run_check({name: "x"})
            self.assertEqual(code, 1, name)
            self.assertIn("personal file", out)

    def test_real_address_fails_dummy_passes(self):
        self.assertEqual(self.run_check({"a.md": "t@example.com\n"})[0], 0)
        self.assertEqual(self.run_check({"a.md": "jane@firma.at\n"})[0], 1)

    def test_missing_terms_file_is_not_ok(self):
        tmp = tempfile.mkdtemp(prefix="coord_ship_")
        env = {**os.environ, "ANTSON_BANNED_TERMS": os.path.join(tmp, "none.txt")}
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_publish.py"), "--skip-tests"],
                           capture_output=True, text=True, env=env)
        self.assertIn(r.returncode, (1, 2))      # 1 if the repo has uncommitted changes, else 2


if __name__ == "__main__":
    unittest.main()
