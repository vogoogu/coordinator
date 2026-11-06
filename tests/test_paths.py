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
