import shutil
import tempfile
import unittest
from pathlib import Path

from coordinator import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="coord_cfg_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, text):
        d = Path(self.root, ".coordinator")
        d.mkdir(exist_ok=True)
        (d / "project.toml").write_text(text, encoding="utf-8")

    def load(self, text):
        self.write(text)
        return config.load_project(self.root)

    def test_defaults_fill_missing_states(self):
        cfg = self.load('key = "T"\nname = "Test"\n')
        self.assertEqual(cfg.key, "T")
        self.assertEqual(cfg.name, "Test")
        self.assertEqual(cfg.states[0].name, "open")
        self.assertEqual([s.name for s in cfg.states],
                         ["open", "ready", "building", "clarify", "review", "done", "dropped"])
        self.assertEqual(cfg.default_mode, "read")
        self.assertEqual(cfg.on_done, "review")
        self.assertEqual(cfg.on_clarify, "clarify")
        ready = cfg.states[1]
        self.assertEqual(ready.requires, ["goal", "done_when", "scope"])
        self.assertEqual(cfg.states[0].requires, [])

    def test_missing_file(self):
        with self.assertRaises(config.ConfigError):
            config.load_project(self.root)

    def test_invalid_toml(self):
        with self.assertRaises(config.ConfigError):
            self.load("key = = ")

    def test_unknown_kind_rejected(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load('key="T"\n[[states]]\nname="a"\nlabel="A"\nkind="maybe"\n')
        self.assertIn("maybe", str(cm.exception))

    def test_states_replace_whole_list(self):
        cfg = self.load(
            'key="T"\non_done="fin"\non_clarify="ask"\n'
            '[[states]]\nname="go"\nlabel="Go"\nkind="active"\n'
            '[[states]]\nname="ask"\nlabel="Ask"\nkind="handover"\n'
            '[[states]]\nname="fin"\nlabel="Fin"\nkind="finished"\n')
        self.assertEqual([s.name for s in cfg.states], ["go", "ask", "fin"])

    def test_on_done_must_be_handover_or_finished(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load('key="T"\non_done="open"\n')
        self.assertIn("open", str(cm.exception))

    def test_on_done_unknown_state(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load('key="T"\non_done="nowhere"\n')
        self.assertIn("nowhere", str(cm.exception))

    def test_on_clarify_must_be_handover(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load('key="T"\non_clarify="done"\n')
        self.assertIn("done", str(cm.exception))

    def test_default_mode_must_be_in_modes(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load('key="T"\ndefault_mode="admin"\n')
        self.assertIn("admin", str(cm.exception))

    def test_key_required(self):
        with self.assertRaises(config.ConfigError):
            self.load('name = "NoKey"\n')

    def test_modes_order_kept(self):
        cfg = self.load('key="T"\nmodes=["read","write"]\n')
        self.assertEqual(cfg.modes, ["read", "write"])
        cfg = self.load('key="T"\nmodes=["write","read"]\ndefault_mode="write"\n')
        self.assertEqual(cfg.modes, ["write", "read"])


if __name__ == "__main__":
    unittest.main()
