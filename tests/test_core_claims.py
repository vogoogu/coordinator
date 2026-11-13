import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coordinator import core, db
from coordinator.messages import ClaimError, CoordinatorError

PERSON = core.Actor("person", "daria")
T0 = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


def chat(n="s1"):
    return core.Actor("chat", n)


class CoreClaimsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_claims_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = os.path.join(self.tmp, "home")
        self.addCleanup(self._restore)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        root = Path(self.tmp, "kmu")
        (root / ".coordinator").mkdir(parents=True)
        (root / ".coordinator" / "project.toml").write_text(
            'key = "KMU"\nname = "K"\n', encoding="utf-8")
        core.add_project(self.conn, str(root), PERSON)

    def _restore(self):
        if self._old is None:
            os.environ.pop("COORDINATOR_HOME", None)
        else:
            os.environ["COORDINATOR_HOME"] = self._old

    def task(self, actor=PERSON, **kw):
        return core.create_task(self.conn, "KMU", "t", actor, **kw)

    def events(self, display_id, action=None):
        h = core.get_task(self.conn, display_id)["history"]
        return [e for e in h if action is None or e["action"] == action]

    def test_claim_refuses_parked_task(self):
        note = self.task(chat(), backlog=True)
        self.assertRefused(core.claim, self.conn, note["id"], "s9", why="backlog")
        self.assertEqual(core.get_task(self.conn, note["id"])["state"], "backlog")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claim").fetchone()[0], 0)

    def test_waiting_only_claim_ignores_parked(self):
        note = self.task(chat(), backlog=True)
        self.assertIsNone(core.claim(self.conn, note["id"], "s9", waiting_only=True))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claim").fetchone()[0], 0)

    def stat(self, session):
        return self.conn.execute(
            "SELECT * FROM session_stat WHERE session_id = ?", (session,)).fetchone()

    def assertRefused(self, fn, *args, why=None, **kw):
        with self.assertRaises(CoordinatorError) as cm:
            fn(*args, **kw)
        lines = str(cm.exception).splitlines()
        self.assertTrue(lines[0].startswith("WHAT:"))
        self.assertTrue(lines[1].startswith("WHY:"))
        self.assertTrue(lines[2].startswith("FIX:"))
        if why:
            self.assertIn(why, str(cm.exception))
        return cm.exception

    # ------------------------------------------------------------ claim

    def test_claim_keep_leaves_the_waiting_state(self):
        Path(self.tmp, "kmu", ".coordinator", "project.toml").write_text(
            'key = "KMU"\nname = "K"\nclaim = "keep"\n', encoding="utf-8")
        t = self.task(chat("s0"))
        self.assertEqual(t["state"], "open")
        r = core.claim(self.conn, t["id"], "s1", now=T0)
        self.assertEqual(r["state"], "open")
        self.assertEqual(len(self.events(t["id"], "claim")), 1)
        self.assertEqual(self.events(t["id"], "state"), [])
        # the chat moves the task itself, with its own claim
        core.set_state(self.conn, t["id"], "building", chat("s1"))
        self.assertEqual(core.get_task(self.conn, t["id"])["state"], "building")

    def test_claim_moves_to_active_and_records_transcript(self):
        t = self.task(chat("s0"), inbox=True)
        self.assertTrue(t["inbox"])
        r = core.claim(self.conn, t["id"], "s1", "C:\\t\\s1.jsonl", "C:\\work", now=T0)
        self.assertEqual(r["state"], "building")
        self.assertFalse(r["inbox"])
        c = core.get_task(self.conn, t["id"])["claims"][0]
        self.assertEqual((c["session_id"], c["transcript_path"], c["cwd"]),
                         ("s1", "C:\\t\\s1.jsonl", "C:\\work"))
        self.assertIsNone(c["ended"])
        self.assertEqual(self.stat("s1")["last_task_update"], T0.isoformat())
        self.assertEqual(len(self.events(t["id"], "claim")), 1)
        st = self.events(t["id"], "state")[-1]
        self.assertEqual((st["from_value"], st["to_value"], st["actor_type"],
                          st["actor_id"]), ("open", "building", "chat", "s1"))

    def test_claim_accepts_handover_and_unclaimed_active_refuses_final(self):
        t = self.task()
        core.set_state(self.conn, t["id"], "clarify", PERSON, note="n")
        self.assertEqual(core.claim(self.conn, t["id"], "s1")["state"], "building")
        u = self.task()
        core.set_state(self.conn, u["id"], "building", PERSON)  # active, no claim
        self.assertEqual(core.claim(self.conn, u["id"], "s2")["state"], "building")
        d = self.task()
        core.set_state(self.conn, d["id"], "done", PERSON)
        self.assertRefused(core.claim, self.conn, d["id"], "s3")
        x = self.task()
        core.set_state(self.conn, x["id"], "dropped", PERSON, note="n")
        self.assertRefused(core.claim, self.conn, x["id"], "s3")
        self.assertEqual(len(self.events(d["id"], "refused")), 1)

    def test_claim_same_session_again_is_idempotent(self):
        t = self.task()
        a = core.claim(self.conn, t["id"], "s1", now=T0)
        b = core.claim(self.conn, t["id"], "s1", now=T0 + timedelta(minutes=5))
        self.assertEqual(a["version"], b["version"])
        self.assertEqual(len(core.get_task(self.conn, t["id"])["claims"]), 1)
        self.assertEqual(len(self.events(t["id"], "claim")), 1)

    def test_second_session_cannot_claim(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        err = self.assertRefused(core.claim, self.conn, t["id"], "s2",
                                 why="already claimed by s1")
        self.assertIsInstance(err, ClaimError)
        ev = self.events(t["id"], "refused")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["actor_type"], ev[0]["actor_id"]), ("chat", "s2"))
        self.assertEqual(len(core.get_task(self.conn, t["id"])["claims"]), 1)

    def test_parallel_claims_one_wins(self):
        t = self.task()
        barrier = threading.Barrier(2)
        results = {}

        def worker(session):
            conn = db.connect()
            try:
                barrier.wait()
                try:
                    core.claim(conn, t["id"], session)
                    results[session] = "ok"
                except ClaimError as e:
                    results[session] = e
            except BaseException as e:  # pragma: no cover - reported below
                results[session] = e
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(s,)) for s in ("a", "b")]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        oks = [s for s, r in results.items() if r == "ok"]
        errs = [r for r in results.values() if isinstance(r, ClaimError)]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(len(errs), 1, results)
        self.assertIn("already claimed by " + oks[0], str(errs[0]))
        live = [c for c in core.get_task(self.conn, t["id"])["claims"]
                if c["ended"] is None]
        self.assertEqual(len(live), 1)

    def test_one_task_per_session(self):
        a, b = self.task(), self.task()
        core.claim(self.conn, a["id"], "s1")
        err = self.assertRefused(core.claim, self.conn, b["id"], "s1", why=a["id"])
        self.assertIsInstance(err, ClaimError)
        self.assertEqual(err.fix, f"Release {a['id']} first with outcome done or "
                                  "clarify (progress keeps the claim), then claim "
                                  "the next task.")
        self.assertEqual(core.get_task(self.conn, b["id"])["state"], "open")
        # after releasing, the session may claim another task
        core.release(self.conn, a["id"], "s1", "done", "fertig")
        self.assertEqual(core.claim(self.conn, b["id"], "s1")["state"], "building")

    def test_chat_can_set_active_only_with_own_claim(self):
        t = self.task(chat("s1"))
        self.assertRefused(core.set_state, self.conn, t["id"], "building", chat("s1"))
        core.claim(self.conn, t["id"], "s1")
        core.set_state(self.conn, t["id"], "clarify", chat("s1"), note="n")
        # still holds the live claim: may go back to active
        self.assertEqual(
            core.set_state(self.conn, t["id"], "building", chat("s1"))["state"],
            "building")
        self.assertRefused(core.set_state, self.conn, t["id"], "building", chat("s2"))
        core.release(self.conn, t["id"], "s1", "clarify", "q?")
        self.assertRefused(core.set_state, self.conn, t["id"], "building", chat("s1"))

    # ---------------------------------------------------------- release

    def test_release_done_goes_to_review_with_handoff_comment(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        r = core.release(self.conn, t["id"], "s1", "done", "all finished")
        self.assertEqual(r["state"], "review")
        full = core.get_task(self.conn, t["id"])
        self.assertEqual((full["comments"][-1]["kind"], full["comments"][-1]["text"]),
                         ("handoff", "all finished"))
        self.assertEqual(full["comments"][-1]["actor_id"], "s1")
        c = full["claims"][0]
        self.assertIsNotNone(c["ended"])
        self.assertEqual(c["end_reason"], "done")
        self.assertGreater(self.stat("s1")["last_task_update"], T0.isoformat())

    def test_release_clarify_goes_to_clarify_and_shows_question(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        r = core.release(self.conn, t["id"], "s1", "clarify", "which file?")
        self.assertEqual(r["state"], "clarify")
        full = core.get_task(self.conn, t["id"])
        self.assertEqual((full["comments"][-1]["kind"], full["comments"][-1]["text"]),
                         ("question", "which file?"))
        self.assertEqual(full["claims"][0]["end_reason"], "clarify")

    def test_release_progress_keeps_claim(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        r = core.release(self.conn, t["id"], "s1", "progress", "half way")
        self.assertEqual(r["state"], "building")
        full = core.get_task(self.conn, t["id"])
        self.assertEqual(full["comments"][-1]["kind"], "progress")
        self.assertIsNone(full["claims"][0]["ended"])

    def test_release_requires_note_and_own_claim_and_known_outcome(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        for note in ("", "   "):
            self.assertRefused(core.release, self.conn, t["id"], "s1", "done", note)
        self.assertRefused(core.release, self.conn, t["id"], "s2", "done", "x")
        self.assertRefused(core.release, self.conn, t["id"], "s1", "bogus", "x")
        full = core.get_task(self.conn, t["id"])
        self.assertEqual(full["state"], "building")
        self.assertIsNone(full["claims"][0]["ended"])
        self.assertEqual(full["comments"], [])
        self.assertEqual(len(self.events(t["id"], "refused")), 4)

    def test_release_done_follows_on_done_even_if_finished_kind(self):
        root = Path(self.tmp, "fin")
        (root / ".coordinator").mkdir(parents=True)
        (root / ".coordinator" / "project.toml").write_text(
            'key = "FIN"\non_done = "done"\n', encoding="utf-8")
        core.add_project(self.conn, str(root), PERSON)
        t = core.create_task(self.conn, "FIN", "t", PERSON)
        core.claim(self.conn, t["id"], "s1")
        self.assertEqual(core.release(self.conn, t["id"], "s1", "done", "ok")["state"],
                         "done")

    # ------------------------------------------------------------ touch

    def test_touch_updates_claim_and_session(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        later = T0 + timedelta(minutes=10)
        core.touch(self.conn, "s1", "Edit", "C:\\a.txt", now=later)
        c = core.get_task(self.conn, t["id"])["claims"][0]
        self.assertEqual(c["last_seen"], later.isoformat())
        s = self.stat("s1")
        self.assertEqual((s["last_activity"], s["last_tool"], s["last_path"]),
                         (later.isoformat(), "Edit", "C:\\a.txt"))

    def test_touch_ignores_coordinator_tools(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        core.touch(self.conn, "s1", "mcp__coordinator__task_get", "",
                   now=T0 + timedelta(minutes=1))
        self.assertEqual(core.get_task(self.conn, t["id"])["claims"][0]["last_seen"],
                         T0.isoformat())
        self.assertIsNone(self.stat("s1")["last_activity"])

    def test_touch_without_claim_writes_nothing(self):
        t = self.task()
        before = self.conn.total_changes
        core.touch(self.conn, "s1", "Edit", "x")
        self.assertEqual(self.conn.total_changes, before)
        self.assertIsNone(self.stat("s1"))
        core.claim(self.conn, t["id"], "s1")
        core.release(self.conn, t["id"], "s1", "done", "ok")
        n = len(self.events(t["id"]))
        before = self.conn.total_changes
        core.touch(self.conn, "s1", "Edit", "x")
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(len(self.events(t["id"])), n)

    # ------------------------------------------------------- needs_report

    def test_needs_report_true_after_activity_false_after_release(self):
        t = self.task()
        self.assertFalse(core.needs_report(self.conn, "s1"))
        core.claim(self.conn, t["id"], "s1", now=T0)
        self.assertFalse(core.needs_report(self.conn, "s1"))
        core.touch(self.conn, "s1", "Edit", "a", now=T0 + timedelta(minutes=1))
        self.assertTrue(core.needs_report(self.conn, "s1"))
        core.release(self.conn, t["id"], "s1", "progress", "so far")
        self.assertFalse(core.needs_report(self.conn, "s1"))
        core.touch(self.conn, "s1", "Edit", "b", now=datetime.now(timezone.utc)
                   + timedelta(hours=1))
        self.assertTrue(core.needs_report(self.conn, "s1"))
        core.release(self.conn, t["id"], "s1", "done", "end")
        self.assertFalse(core.needs_report(self.conn, "s1"))

    def test_claim_for_session(self):
        t = self.task()
        self.assertIsNone(core.claim_for_session(self.conn, "s1"))
        core.claim(self.conn, t["id"], "s1", "tp", "cwd1")
        c = core.claim_for_session(self.conn, "s1")
        self.assertEqual((c["display_id"], c["transcript_path"], c["cwd"]),
                         (t["id"], "tp", "cwd1"))
        core.release(self.conn, t["id"], "s1", "done", "x")
        self.assertIsNone(core.claim_for_session(self.conn, "s1"))

    # ----------------------------------------------------------- leases

    def test_expired_lease_moves_to_clarify_with_last_activity(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        core.touch(self.conn, "s1", "Bash", "C:\\p", now=T0)
        self.assertEqual(core.expire_leases(self.conn, now=T0 + timedelta(hours=core.LEASE_HOURS)), [])
        ids = core.expire_leases(self.conn, now=T0 + timedelta(hours=core.LEASE_HOURS, seconds=1))
        self.assertEqual(ids, [t["id"]])
        full = core.get_task(self.conn, t["id"])
        self.assertEqual(full["state"], "clarify")
        self.assertEqual(full["claims"][0]["end_reason"], "expired")
        self.assertEqual(
            full["comments"][-1]["text"],
            "chat stopped without hand-off; last activity: Bash C:\\p at "
            + T0.isoformat())
        self.assertEqual(full["comments"][-1]["kind"], "comment")
        ev = self.events(t["id"], "state")[-1]
        self.assertEqual((ev["actor_type"], ev["actor_id"]), ("system", "lease"))
        self.assertEqual(core.expire_leases(self.conn, now=T0 + timedelta(hours=9)), [])

    def test_expire_without_activity_says_none_and_hours_zero(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        self.assertEqual(core.expire_leases(self.conn, now=T0 + timedelta(seconds=1), hours=0), [t["id"]])
        self.assertTrue(core.get_task(self.conn, t["id"])["comments"][-1]["text"]
                        .endswith("last activity: none"))

    def test_fresh_claim_not_expired(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        core.touch(self.conn, "s1", "Edit", "x", now=T0 + timedelta(hours=3))
        self.assertEqual(core.expire_leases(self.conn, now=T0 + timedelta(hours=3.4)), [])
        self.assertEqual(
            len(core.expire_leases(self.conn, now=T0 + timedelta(hours=3.6))), 1)

    # ---------------------------------------------------- force release

    def test_force_release_chat_refused_person_works(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        self.assertRefused(core.force_release, self.conn, t["id"], chat("s2"))
        self.assertRefused(core.force_release, self.conn, t["id"], chat("s1"))
        full = core.get_task(self.conn, t["id"])
        self.assertEqual(full["state"], "building")
        self.assertIsNone(full["claims"][0]["ended"])
        self.assertEqual(len(self.events(t["id"], "refused")), 2)
        r = core.force_release(self.conn, t["id"], PERSON)
        self.assertEqual(r["state"], "clarify")
        full = core.get_task(self.conn, t["id"])
        self.assertEqual(full["claims"][0]["end_reason"], "force")
        self.assertEqual((full["comments"][-1]["kind"], full["comments"][-1]["text"]),
                         ("comment", "released by person"))
        self.assertIsNone(core.claim_for_session(self.conn, "s1"))

    def test_force_release_without_claim_refused(self):
        t = self.task()
        self.assertRefused(core.force_release, self.conn, t["id"], PERSON)

    # ---------------------------------------------- stop without report

    def test_stop_without_report(self):
        t = self.task()
        self.assertIsNone(core.stop_without_report(self.conn, "s1", "n"))
        self.assertEqual(self.events(t["id"], "comment"), [])
        core.claim(self.conn, t["id"], "s1", now=T0)
        r = core.stop_without_report(self.conn, "s1", "stopped without report")
        self.assertEqual(r["state"], "clarify")
        full = core.get_task(self.conn, t["id"])
        self.assertEqual(full["claims"][0]["end_reason"], "no_report")
        self.assertEqual((full["comments"][-1]["kind"], full["comments"][-1]["text"],
                          full["comments"][-1]["actor_type"]),
                         ("comment", "stopped without report", "chat"))
        self.assertGreater(self.stat("s1")["last_task_update"], T0.isoformat())
        self.assertIsNone(core.stop_without_report(self.conn, "s1", "again"))


    # ------------------------------------- person finishes or drops (R31)

    def test_person_drop_ends_live_claim(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        r = core.set_state(self.conn, t["id"], "dropped", PERSON, note="not needed")
        self.assertEqual(r["state"], "dropped")
        full = core.get_task(self.conn, t["id"])
        self.assertIsNotNone(full["claims"][0]["ended"])
        self.assertEqual(full["claims"][0]["end_reason"], "closed_by_person")
        ev = self.events(t["id"], "release")[-1]
        self.assertEqual((ev["actor_type"], ev["from_value"], ev["to_value"]),
                         ("person", "s1", "closed_by_person"))
        self.assertIsNone(core.claim_for_session(self.conn, "s1"))

    def test_person_done_ends_live_claim(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        core.set_state(self.conn, t["id"], "done", PERSON)
        full = core.get_task(self.conn, t["id"])
        self.assertEqual(full["claims"][0]["end_reason"], "closed_by_person")

    def test_person_handover_state_keeps_claim(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        core.set_state(self.conn, t["id"], "review", PERSON, note="look")
        self.assertIsNotNone(core.claim_for_session(self.conn, "s1"))

    def test_expire_after_person_drop_keeps_dropped(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        core.set_state(self.conn, t["id"], "dropped", PERSON, note="not needed")
        self.assertEqual(
            core.expire_leases(self.conn, now=T0 + timedelta(hours=9)), [])
        self.assertEqual(core.get_task(self.conn, t["id"])["state"], "dropped")

    def test_expire_on_non_active_task_ends_claim_without_move(self):
        # a live claim on a dropped task (left by an older version): the claim
        # ends and the comment is written, but the state stays.
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        self.conn.execute("UPDATE task SET state = 'dropped'")
        ids = core.expire_leases(self.conn, now=T0 + timedelta(hours=5))
        self.assertEqual(ids, [t["id"]])
        full = core.get_task(self.conn, t["id"])
        self.assertEqual(full["state"], "dropped")
        self.assertEqual(full["claims"][0]["end_reason"], "expired")
        self.assertTrue(full["comments"][-1]["text"].startswith(
            "chat stopped without hand-off"))
        self.assertEqual([e for e in self.events(t["id"], "state")
                          if e["actor_type"] == "system"], [])

    def test_force_release_and_no_report_on_handover_task_keep_state(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        core.set_state(self.conn, t["id"], "review", PERSON, note="look")
        core.force_release(self.conn, t["id"], PERSON)
        self.assertEqual(core.get_task(self.conn, t["id"])["state"], "review")
        t2 = self.task()
        core.claim(self.conn, t2["id"], "s2")
        core.set_state(self.conn, t2["id"], "review", PERSON, note="look")
        core.stop_without_report(self.conn, "s2", "stopped")
        full = core.get_task(self.conn, t2["id"])
        self.assertEqual(full["state"], "review")
        self.assertEqual(full["claims"][0]["end_reason"], "no_report")
        self.assertEqual(full["comments"][-1]["text"], "stopped")

    def test_release_done_after_person_drop_keeps_dropped_and_records_note(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        core.set_state(self.conn, t["id"], "dropped", PERSON, note="not needed")
        r = core.release(self.conn, t["id"], "s1", "done", "my hand-off")
        self.assertEqual(r["state"], "dropped")
        full = core.get_task(self.conn, t["id"])
        self.assertEqual((full["comments"][-1]["kind"], full["comments"][-1]["text"],
                          full["comments"][-1]["actor_id"]),
                         ("handoff", "my hand-off", "s1"))
        self.assertEqual(len(full["claims"]), 1)
        self.assertEqual(full["claims"][0]["end_reason"], "closed_by_person")
        self.assertEqual([e["to_value"] for e in self.events(t["id"], "state")][-1],
                         "dropped")
        # another session still cannot release it
        self.assertRefused(core.release, self.conn, t["id"], "s2", "done", "x")

    def test_release_with_live_claim_on_non_active_task_does_not_move(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        self.conn.execute("UPDATE task SET state = 'dropped'")  # older version
        r = core.release(self.conn, t["id"], "s1", "clarify", "question?")
        self.assertEqual(r["state"], "dropped")
        full = core.get_task(self.conn, t["id"])
        self.assertEqual(full["claims"][0]["end_reason"], "clarify")
        self.assertEqual(full["comments"][-1]["text"], "question?")


    # --------------------------------------------- launch token and /clear (R32)

    def test_claim_stores_launch_token(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", launch="tok")
        self.assertEqual(core.get_task(self.conn, t["id"])["claims"][0]["launch"], "tok")

    def test_answer_reclaims_after_clarify_or_expired_only(self):    # COORD-10
        t = self.task()
        core.claim(self.conn, t["id"], "s1")
        core.release(self.conn, t["id"], "s1", "clarify", "which db?")
        r = core.reclaim_after_answer(self.conn, "s1", "tp", "cwd")
        self.assertEqual((r["id"], r["state"]), (t["id"], "building"))
        self.assertEqual(core.claim_for_session(self.conn, "s1")["display_id"], t["id"])
        self.assertIsNone(core.reclaim_after_answer(self.conn, "s1"))     # already holds it
        core.release(self.conn, t["id"], "s1", "done", "x")
        self.assertIsNone(core.reclaim_after_answer(self.conn, "s1"))     # ended with done
        u = self.task()
        core.claim(self.conn, u["id"], "s2")
        core.expire_leases(self.conn, now=datetime.now(timezone.utc) + timedelta(hours=2))
        self.assertEqual(core.get_task(self.conn, u["id"])["state"], "clarify")
        self.assertEqual(core.reclaim_after_answer(self.conn, "s2")["id"], u["id"])

    def test_answer_does_not_reclaim_a_task_someone_else_holds_or_moved_on(self):
        a, b = self.task(), self.task()
        core.claim(self.conn, a["id"], "s1")
        core.release(self.conn, a["id"], "s1", "clarify", "q")
        core.claim(self.conn, a["id"], "s9")                               # taken by another chat
        self.assertIsNone(core.reclaim_after_answer(self.conn, "s1"))
        core.claim(self.conn, b["id"], "s3")
        core.release(self.conn, b["id"], "s3", "clarify", "q")
        core.set_state(self.conn, b["id"], "open", PERSON)                 # person moved it on
        self.assertIsNone(core.reclaim_after_answer(self.conn, "s3"))
        core.claim(self.conn, b["id"], "s4")
        core.release(self.conn, b["id"], "s4", "progress", "pause")
        self.assertIsNone(core.reclaim_after_answer(self.conn, "s4"))      # progress is not a question
        self.assertIsNone(core.reclaim_after_answer(self.conn, "never-claimed"))

    def test_reclaim_after_clear_moves_claim_to_new_session(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", launch="tok")
        r = core.reclaim_after_clear(self.conn, t["id"], "s2", "tok", "tp2", "cwd2")
        self.assertEqual(r["state"], "building")
        full = core.get_task(self.conn, t["id"])
        old, new = full["claims"]
        self.assertEqual((old["session_id"], old["end_reason"]), ("s1", "clear"))
        self.assertIsNotNone(old["ended"])
        self.assertEqual((new["session_id"], new["launch"], new["ended"],
                          new["transcript_path"]), ("s2", "tok", None, "tp2"))
        ev = self.events(t["id"], "release")[-1]
        self.assertEqual((ev["actor_id"], ev["from_value"], ev["to_value"]),
                         ("s2", "s1", "clear"))
        self.assertEqual(self.events(t["id"], "claim")[-1]["to_value"], "s2")

    def test_reclaim_after_clear_needs_same_token(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", launch="tok")
        before = len(self.events(t["id"]))
        for token in ("other", "", None):
            self.assertIsNone(
                core.reclaim_after_clear(self.conn, t["id"], "s2", token))
        t2 = self.task()
        core.claim(self.conn, t2["id"], "s3")  # no token stored
        self.assertIsNone(core.reclaim_after_clear(self.conn, t2["id"], "s4", None))
        self.assertIsNone(core.reclaim_after_clear(self.conn, t2["id"], "s4", ""))
        self.assertEqual(len(self.events(t["id"])), before)
        self.assertEqual(core.claim_for_session(self.conn, "s1")["display_id"], t["id"])
        self.assertIsNone(core.claim_for_session(self.conn, "s2"))


    # ------------------------------------------------ broken project file

    def test_broken_project_file_is_a_coordinator_error(self):
        t = self.task()
        core.claim(self.conn, t["id"], "s1", now=T0)
        Path(self.tmp, "kmu", ".coordinator", "project.toml").write_text(
            "key = [", encoding="utf-8")
        e = self.assertRefused(core.set_state, self.conn, t["id"], "review", PERSON,
                               why="project.toml")
        self.assertIn("was not changed", e.what)
        self.assertRefused(core.create_task, self.conn, "KMU", "x", PERSON,
                           why="not valid TOML")
        # expiry skips the broken project instead of failing as a whole
        self.assertEqual(core.expire_leases(self.conn, now=T0 + timedelta(hours=9)), [])
        self.assertIsNotNone(core.claim_for_session(self.conn, "s1"))


if __name__ == "__main__":
    unittest.main()
