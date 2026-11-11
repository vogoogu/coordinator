"""Readers of the Antson guard's files (fixtures in a temp folder, never the real
~/.antson) and the call of mode.py as a command."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from coordinator import antson

FAKE_MODE_PY = '''
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
cmd = sys.argv[1]
if cmd == "check-hash":
    sys.exit(1 if os.path.exists(os.path.join(here, "hash_bad")) else 0)
if os.path.exists(os.path.join(here, "slow")):
    time.sleep(float(open(os.path.join(here, "slow")).read()))
ids, mode = sys.argv[2:-4], sys.argv[-4]
if os.path.exists(os.path.join(here, "refuse")):
    print(json.dumps({"changed": [], "refused": ids, "reason": "not confirmed"}))
    sys.exit(1)
if os.path.exists(os.path.join(here, "garbage")):
    print("Traceback: boom", file=sys.stderr)
    sys.exit(2)
print(json.dumps({"changed": ids, "refused": [], "reason": None}))
'''


class GuardFilesCase(unittest.TestCase):
    """A temp state folder (ANTSON_STATE_DIR), a temp app home, a fake mode.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_antson_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = Path(self.tmp, "antson")
        self.state.mkdir()
        self.guard = Path(self.tmp, "guard")
        self.guard.mkdir()
        self.mode_py = self.guard / "mode.py"
        self.mode_py.write_text(FAKE_MODE_PY, encoding="utf-8")
        self._env = {k: os.environ.get(k) for k in
                     ("ANTSON_STATE_DIR", "COORDINATOR_HOME", "COORDINATOR_GUARD_MODE_PY")}
        os.environ["ANTSON_STATE_DIR"] = str(self.state)
        os.environ["COORDINATOR_HOME"] = str(Path(self.tmp, "home"))
        os.environ["COORDINATOR_GUARD_MODE_PY"] = str(self.mode_py)
        self.addCleanup(self._restore)
        antson._hash_cache.update(key=None, value=None)
        antson._job = None

    def _restore(self):
        antson.wait_idle(10)
        antson._job = None
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write_modes(self, items):
        (self.state / "modes.json").write_text(
            json.dumps({"version": 1, "items": items}), encoding="utf-8")

    def append_reports(self, *lines, raw=None):
        with open(self.state / "reports.jsonl", "ab") as f:
            for r in lines:
                f.write((json.dumps(r) + "\n").encode())
            if raw:
                f.write(raw.encode())

    def calls(self):
        p = self.guard / "calls.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class ModesFileTest(GuardFilesCase):
    def test_missing_or_broken_file_is_empty(self):
        self.assertEqual(antson.load_modes(), {})
        (self.state / "modes.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(antson.load_modes(), {})

    def test_effective_follows_inheritance_and_unknown_is_read(self):
        items = {"596": {"mode": "dev"},
                 "2216": {"mode": "read", "parent": "596", "inherit": True},
                 "2217": {"mode": "prod", "parent": "596", "inherit": False},
                 "1": {"mode": "dev", "parent": "2", "inherit": True},
                 "2": {"mode": "dev", "parent": "1", "inherit": True}}
        self.assertEqual(antson.effective("596", items), ("dev", False))
        self.assertEqual(antson.effective("2216", items), ("dev", True))
        self.assertEqual(antson.effective("2217", items), ("prod", False))
        self.assertEqual(antson.effective("999", items), ("read", False))
        self.assertEqual(antson.effective("1", items)[0], "read", "a parent cycle is read")

    def test_mode_py_setting_order(self):
        os.environ.pop("COORDINATOR_GUARD_MODE_PY")
        self.assertEqual(antson.mode_py_path(), antson.DEFAULT_MODE_PY)
        home = Path(os.environ["COORDINATOR_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "antson.toml").write_text('mode_py = "D:\\\\proj\\\\mode.py"\n', encoding="utf-8")
        self.assertEqual(antson.mode_py_path(), "D:\\proj\\mode.py")
        os.environ["COORDINATOR_GUARD_MODE_PY"] = "X"
        self.assertEqual(antson.mode_py_path(), "X")


class ReportsIntakeTest(GuardFilesCase):
    def report(self, rid, event="seen", **kw):
        return {"at": "2026-11-10T08:00:00+00:00", "session": "s1", "event": event,
                "kind": "interface", "id": rid, "name": None, "parent": None, "tenant": None, **kw}

    def test_reads_new_lines_only_and_remembers_the_offset(self):
        self.assertEqual(antson.read_reports(), {})
        self.append_reports(self.report("2225", "created", parent="614"))
        first = antson.read_reports()
        self.assertEqual(first["2225"]["parent"], "614")
        store = json.loads((Path(os.environ["COORDINATOR_HOME"]) / "reports-intake.json").read_text())
        self.assertEqual(store["offset"], (self.state / "reports.jsonl").stat().st_size)
        self.append_reports(self.report("2230", "created", parent="614"))
        self.assertEqual(sorted(antson.read_reports()), ["2225", "2230"])

    def test_a_half_written_line_waits(self):
        self.append_reports(self.report("1", "created"), raw='{"id": "2", "ev')
        self.assertEqual(sorted(antson.read_reports()), ["1"])
        with open(self.state / "reports.jsonl", "ab") as f:
            f.write(b'ent": "seen", "kind": "form"}\n')
        self.assertEqual(sorted(antson.read_reports()), ["1", "2"])

    def test_bad_lines_are_skipped_and_later_reports_fill_in_the_name(self):
        self.append_reports(self.report("7", "seen"), raw="garbage\n[1, 2]\n")
        self.append_reports(self.report("7", "created", name="W&B", parent="596"))
        item = antson.read_reports()["7"]
        self.assertEqual((item["name"], item["parent"], item["event"]), ("W&B", "596", "created"))

    def test_a_replaced_shorter_file_starts_over(self):
        self.append_reports(self.report("1"), self.report("2"))
        antson.read_reports()
        (self.state / "reports.jsonl").write_text(json.dumps(self.report("3")) + "\n", encoding="utf-8")
        self.assertEqual(sorted(antson.read_reports()), ["3"])

    def test_the_intake_survives_a_restart(self):
        self.append_reports(self.report("5"))
        antson.read_reports()
        (self.state / "reports.jsonl").write_text(
            (self.state / "reports.jsonl").read_text() + json.dumps(self.report("6")) + "\n")
        self.assertEqual(sorted(antson.read_reports()), ["5", "6"])


class GoFilesTest(GuardFilesCase):
    def write_go(self, session, data, age_hours=0):
        d = self.state / "go"
        d.mkdir(exist_ok=True)
        f = d / f"{session}.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        if age_hours:
            t = time.time() - age_hours * 3600
            os.utime(f, (t, t))

    LIST = {"list_id": "L2", "at": "2026-11-10T09:42:35", "items": [
        {"n": 1, "form": "625", "ref": "T-1", "what": "change a label"},
        {"n": 2, "form": "614", "ref": "T-2", "what": "read it back"}]}

    def test_a_list_without_approval_waits(self):
        self.write_go("sess-a", {"lists": [self.LIST], "approved": [], "windows": {}})
        (w,) = antson.go_waiting()
        self.assertEqual((w["kind"], w["session"], w["list_id"]), ("go", "sess-a", "L2"))
        self.assertEqual([(i["n"], i["form"], i["what"]) for i in w["items"]],
                         [(1, "625", "change a label"), (2, "614", "read it back")])

    def test_an_approved_list_and_an_empty_file_do_not_wait(self):
        self.write_go("a", {"lists": [self.LIST], "approved": [{"list_id": "L2", "n": [1]}]})
        self.write_go("b", {"lists": [], "approved": [], "windows": {}})
        self.assertEqual(antson.go_waiting(), [])

    def test_only_the_newest_list_counts(self):
        old = {**self.LIST, "list_id": "L1"}
        self.write_go("a", {"lists": [old, self.LIST], "approved": [{"list_id": "L1", "n": [1]}]})
        self.assertEqual([w["list_id"] for w in antson.go_waiting()], ["L2"])
        self.write_go("a", {"lists": [old, self.LIST], "approved": [{"list_id": "L2", "n": [1]}]})
        self.assertEqual(antson.go_waiting(), [])

    def test_a_refused_ready_check_shows_with_its_reasons(self):
        self.write_go("a", {"lists": [], "approved": [],
                            "ready_refused": {"625": ["form-mismatch", "wrong-tenant"]}})
        (w,) = antson.go_waiting()
        self.assertEqual((w["kind"], w["form"], w["reasons"]),
                         ("ready_refused", "625", ["form-mismatch", "wrong-tenant"]))

    def test_old_files_and_broken_files_are_ignored(self):
        self.write_go("old", {"lists": [self.LIST], "approved": []}, age_hours=13)
        (self.state / "go").mkdir(exist_ok=True)
        (self.state / "go" / "bad.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(antson.go_waiting(), [])

    def test_no_go_folder_is_fine(self):
        self.assertEqual(antson.go_waiting(), [])


class HashCheckTest(GuardFilesCase):
    def test_nothing_to_check_without_a_mode_file(self):
        self.assertIsNone(antson.hash_ok())
        self.assertEqual(self.calls(), [])

    def test_asks_mode_py_and_caches_until_the_files_change(self):
        self.write_modes({})
        self.assertTrue(antson.hash_ok())
        self.assertTrue(antson.hash_ok())
        self.assertEqual(self.calls(), [["check-hash"]], "one run for two answers")
        (self.guard / "hash_bad").write_text("x")
        self.assertTrue(antson.hash_ok(), "still cached while the files are unchanged")
        self.write_modes({"1": {"mode": "read"}})                    # modes.json changed
        self.assertFalse(antson.hash_ok())

    def test_missing_mode_py_is_unknown_not_a_warning(self):
        self.write_modes({})
        os.environ["COORDINATOR_GUARD_MODE_PY"] = str(self.guard / "nope.py")
        self.assertIsNone(antson.hash_ok())


class SetCommandTest(GuardFilesCase):
    def run_set(self, ids, mode):
        job = antson.start_set(ids, mode)
        antson.wait_idle(15)
        return job

    def test_calls_mode_py_as_a_command_with_by_board_and_json(self):
        job = self.run_set(["614", "625"], "dev")
        self.assertEqual(self.calls(), [["set", "614", "625", "dev", "--by", "board", "--json"]])
        self.assertEqual((job.state, job.changed, job.refused, job.error),
                         ("done", ["614", "625"], [], ""))

    def test_the_board_does_not_touch_modes_json(self):
        self.write_modes({"614": {"mode": "read"}})
        before = (self.state / "modes.json").read_bytes()
        self.run_set(["614"], "dev")
        self.assertEqual((self.state / "modes.json").read_bytes(), before)

    def test_a_refusal_is_reported_as_given(self):
        (self.guard / "refuse").write_text("x")
        job = self.run_set(["614"], "dev")
        self.assertEqual((job.changed, job.refused, job.reason), ([], ["614"], "not confirmed"))

    def test_an_answer_that_is_not_json_becomes_an_error_text(self):
        (self.guard / "garbage").write_text("x")
        job = self.run_set(["614"], "dev")
        self.assertEqual(job.state, "done")
        self.assertIn("boom", job.error)
        self.assertEqual(job.changed, [])

    def test_missing_mode_py_is_an_error_text(self):
        os.environ["COORDINATOR_GUARD_MODE_PY"] = str(self.guard / "nope.py")
        job = self.run_set(["614"], "read")
        self.assertIn("was not found", job.error)

    def test_bad_ids_and_modes_never_reach_the_command_line(self):
        for ids, mode in ((["--version"], "dev"), ([], "dev"), (["614"], "admin"), (["614; x"], "dev"),
                          (["-1"], "dev"), ([""], "dev")):
            with self.assertRaises(ValueError, msg=(ids, mode)):
                antson.start_set(ids, mode)
        self.assertEqual(self.calls(), [])
        antson.start_set(["TENANT:12"], "read")          # tenant:id keys are allowed
        antson.wait_idle(15)

    def test_start_returns_at_once_and_a_second_call_waits_its_turn(self):
        (self.guard / "slow").write_text("1.5")
        t0 = time.time()
        job = antson.start_set(["614"], "prod")
        self.assertLess(time.time() - t0, 1.0, "the caller is not blocked by the window")
        self.assertEqual(job.state, "waiting")
        with self.assertRaises(RuntimeError):
            antson.start_set(["625"], "prod")
        antson.wait_idle(15)
        self.assertEqual(job.state, "done")
        antson.start_set(["625"], "prod")                # free again
        antson.wait_idle(15)


if __name__ == "__main__":
    unittest.main()
