import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from coordinator import paths


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_paths_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home = os.path.join(self.tmp, "home")
        old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = home
        if old is None:
            self.addCleanup(os.environ.pop, "COORDINATOR_HOME", None)
        else:
            self.addCleanup(os.environ.__setitem__, "COORDINATOR_HOME", old)

    def make_dir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def test_app_home_uses_env_and_creates(self):
        h = paths.app_home()
        self.assertEqual(h, Path(os.environ["COORDINATOR_HOME"]))
        self.assertTrue(h.is_dir())

    def _fake_home(self):
        """USERPROFILE and LOCALAPPDATA in a temp folder; COORDINATOR_HOME unset."""
        user = self.make_dir("user")
        local = self.make_dir("user", "AppData", "Local")
        for k, v in (("USERPROFILE", user), ("HOME", user), ("LOCALAPPDATA", local)):
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(os.environ.pop if old is None else os.environ.__setitem__,
                            *((k,) if old is None else (k, old)))
        old = os.environ.pop("COORDINATOR_HOME")
        self.addCleanup(os.environ.__setitem__, "COORDINATOR_HOME", old)
        return Path(user), Path(local)

    def test_default_home_is_dot_coordinator_and_copies_old_folder_once(self):
        import sqlite3
        user, local = self._fake_home()
        old = local / "coordinator"
        (old / "backups").mkdir(parents=True)
        (old / "backups" / "x.db").write_text("b", encoding="utf-8")
        (old / "notify.toml").write_text("enabled = false", encoding="utf-8")
        c = sqlite3.connect(old / "coordinator.db")
        c.execute("CREATE TABLE t (v)")
        c.execute("INSERT INTO t VALUES ('kept')")
        c.commit()
        c.close()
        home = paths.app_home()
        self.assertEqual(home, user / ".coordinator")
        self.assertEqual((home / "notify.toml").read_text(encoding="utf-8"), "enabled = false")
        self.assertTrue((home / "backups" / "x.db").is_file())
        c = sqlite3.connect(home / "coordinator.db")
        self.assertEqual(c.execute("SELECT v FROM t").fetchone()[0], "kept")
        c.close()
        self.assertTrue((old / "coordinator.db").is_file(), "the old folder stays")
        # once only: a later change in the old folder is not copied again
        (old / "notify.toml").write_text("enabled = true", encoding="utf-8")
        paths.app_home()
        self.assertEqual((home / "notify.toml").read_text(encoding="utf-8"), "enabled = false")
        self.assertEqual([p.name for p in user.iterdir() if "copy" in p.name], [])

    def test_default_home_without_old_folder_starts_empty(self):
        user, _ = self._fake_home()
        self.assertEqual(list(paths.app_home().iterdir()), [])

    def test_antson_state_dir_default_and_override(self):
        user, _ = self._fake_home()
        old = os.environ.pop("ANTSON_STATE_DIR", None)
        self.addCleanup(lambda: old is None or os.environ.__setitem__("ANTSON_STATE_DIR", old))
        self.assertEqual(paths.antson_state_dir(), user / ".antson")
        self.assertFalse((user / ".antson").exists(), "reading the path creates nothing")
        os.environ["ANTSON_STATE_DIR"] = str(user / "x")
        self.addCleanup(os.environ.pop, "ANTSON_STATE_DIR", None)
        self.assertEqual(paths.antson_state_dir(), user / "x")

    def test_norm_case_and_slashes(self):
        self.assertEqual(paths.norm("C:/Temp/X"), paths.norm("c:\\temp\\x\\"))

    def test_norm_no_trailing_separator(self):
        self.assertFalse(paths.norm(self.make_dir("t") + "\\").endswith("\\"))

    def test_norm_nonexistent_path(self):
        root = self.make_dir("Exists")
        a = paths.norm(os.path.join(root, "Not", "Yet", "There"))
        b = paths.norm(os.path.join(root.upper(), "not/yet/there"))
        self.assertEqual(a, b)
        self.assertTrue(a.endswith(os.path.normcase("\\not\\yet\\there")))

    def test_inside_with_spaces(self):
        root = self.make_dir("a b")
        self.assertTrue(paths.same_or_inside(root + "\\sub\\f.md", root))
        self.assertTrue(paths.same_or_inside(root.replace("\\", "/") + "/SUB/f.md", root))

    def test_same_folder(self):
        root = self.make_dir("same")
        self.assertTrue(paths.same_or_inside(root, root + "\\"))

    def test_outside_is_false(self):
        root = self.make_dir("proj")
        self.assertFalse(paths.same_or_inside("C:\\other", root))

    def test_sibling_with_common_prefix_is_outside(self):
        root = self.make_dir("proj")
        sibling = self.make_dir("proj2")
        self.assertFalse(paths.same_or_inside(sibling, root))

    def test_short_name_matches_long(self):
        if sys.platform != "win32":
            self.skipTest("Windows only")
        import ctypes
        long_dir = self.make_dir("a_rather_long_directory_name", "child")
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(long_dir, buf, 1024)
        short = buf.value
        if n == 0 or short.lower() == long_dir.lower():
            self.skipTest("8.3 names disabled on this volume")
        root = os.path.dirname(long_dir)
        self.assertEqual(paths.norm(short), paths.norm(long_dir))
        self.assertTrue(paths.same_or_inside(short, root))

    def test_find_project_root_walks_up(self):
        root = self.make_dir("proj root")
        os.makedirs(os.path.join(root, ".coordinator"))
        Path(root, ".coordinator", "project.toml").write_text('key="T"\n')
        nested = self.make_dir("proj root", "x", "y")
        self.assertEqual(paths.find_project_root(nested), paths.norm(root))

    def test_find_project_root_none(self):
        self.assertIsNone(paths.find_project_root(self.make_dir("empty", "z")))


if __name__ == "__main__":
    unittest.main()
