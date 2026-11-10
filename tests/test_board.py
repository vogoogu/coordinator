import getpass
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)

from coordinator import board, core, db, launcher, pages, sources  # noqa: E402

PERSON = core.Actor("person", "daria")


class BoardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_board_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = os.environ.get("COORDINATOR_HOME")
        os.environ["COORDINATOR_HOME"] = os.path.join(self.tmp, "home")
        self._env = {k: os.environ.get(k) for k in ("ANTSON_STATE_DIR", "COORDINATOR_GUARD_MODE_PY")}
        os.environ["ANTSON_STATE_DIR"] = os.path.join(self.tmp, "antson")      # never the real ~/.antson
        os.environ["COORDINATOR_GUARD_MODE_PY"] = os.path.join(self.tmp, "no-mode.py")
        self.addCleanup(self._restore)
        board._outside["data"] = None            # the sources cache is per process
        sources.forget()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.roots = {}
        self.make_project("a folder with spaces", "PA")
        self.make_project("other", "PB")
        self.server = board.make_server(port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.token = self.get_json("/api/index")["token"]

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self._old is None:
            os.environ.pop("COORDINATOR_HOME", None)
        else:
            os.environ["COORDINATOR_HOME"] = self._old

    def make_project(self, folder, key):
        root = Path(self.tmp, folder)
        (root / ".coordinator").mkdir(parents=True)
        (root / ".coordinator" / "project.toml").write_text(
            f'key = "{key}"\nname = "Name {key}"\n', encoding="utf-8")
        self.roots[key] = core.add_project(self.conn, str(root), PERSON)["root"]

    def task(self, key="PA", title="Build it", **kw):
        return core.create_task(self.conn, key, title, PERSON, **kw)["id"]

    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def request(self, method, path, form=None, headers=None, host=True):
        h = {}
        if host:
            h["Host"] = f"127.0.0.1:{self.port}"
        h.update(headers or {})
        body = urlencode(form).encode() if form is not None else None
        if body is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, r.read().decode("utf-8"), dict(r.getheaders())
        finally:
            c.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def get_json(self, path):
        status, body, _ = self.get(path)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def post(self, path, form, token=True, origin=True, headers=None):
        f = dict(form)
        if token:
            f["token"] = self.token
        h = dict(headers or {})
        if origin:
            h.setdefault("Origin", self.origin())
        return self.request("POST", path, form=f, headers=h)

    def sunk_during(self, fn):
        sink = os.path.join(self.tmp, "sink.jsonl")
        with mock.patch.dict(os.environ, {"COORDINATOR_NOTIFY_SINK": sink}):
            fn()
        if not os.path.isfile(sink):
            return []
        with open(sink, encoding="utf-8") as f:
            return [json.loads(line) for line in f]


class BoardTest(BoardBase):
    def test_board_page_load_expires_and_notifies(self):
        tid = self.task("PA", "Lost chat")
        core.claim(self.conn, tid, "sess-gone")
        self.conn.execute("UPDATE claim SET last_seen = '2020-01-01T00:00:00+00:00'")
        [rec] = self.sunk_during(lambda: self.get("/"))
        self.assertEqual(rec["heading"], f"{tid} needs you")

    def test_board_page_load_without_expiry_is_quiet(self):
        tid = self.task("PA", "Busy")
        core.claim(self.conn, tid, "sess-live")
        core.release(self.conn, tid, "sess-live", "done", "earlier")
        self.assertEqual(self.sunk_during(lambda: (self.get("/"), self.get("/"))), [])

    # ------------------------------------------------------------ the brief's tests
    def test_index_lists_needs_you_across_projects(self):
        a = self.task("PA", "Check me")
        core.set_state(self.conn, a, "review", PERSON, note="x")
        b = self.task("PB", "Unclear one")
        core.set_state(self.conn, b, "clarify", PERSON, note="x")
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(f'href="/t/{a}"', body)
        self.assertIn(f'href="/t/{b}"', body)
        self.assertIn("Check me", body)
        self.assertIn("Unclear one", body)
        self.assertIn("2 things need you", body)
        data = self.get_json("/api/index")
        self.assertEqual({p["key"] for p in data["projects"]}, {"PA", "PB"})
        self.assertEqual({i["id"] for i in data["needs_you"]}, {a, b})

    def test_index_cards_sorted_red_amber_green(self):
        self.make_project("third", "PC")
        a = self.task("PA", "Check me")
        core.set_state(self.conn, a, "review", PERSON, note="x")      # PA amber
        b = self.task("PB", "Lost chat")
        core.claim(self.conn, b, "sess-gone")
        core.expire_leases(self.conn, now=datetime.now(timezone.utc)
                           + timedelta(hours=5))                     # PB red
        body = self.get("/")[1]
        pos = [body.index(f'id="pc-{k}"') for k in ("PB", "PA", "PC")]
        self.assertEqual(pos, sorted(pos))
        self.assertIn("h-red", body)
        self.assertIn("Act now", body)
        self.assertIn("Chat stopped", body)
        self.assertIn("Nothing needs you", body)                       # PC is quiet
        tones = {p["key"]: p["tone"] for p in self.get_json("/api/index")["projects"]}
        self.assertEqual(tones, {"PA": "amber", "PB": "red", "PC": "green"})

    def test_stopped_chat_shows_last_activity(self):
        tid = self.task()
        core.claim(self.conn, tid, "abcd-1234")
        core.touch(self.conn, "abcd-1234", "Write", "C:/x/notes.md")
        core.stop_without_report(self.conn, "abcd-1234", "no report")
        item = self.get_json("/api/index")["needs_you"][0]
        self.assertEqual((item["why"], item["tone"]), ("stopped", "red"))
        body = self.get("/")[1]
        self.assertIn("Write notes.md", body)
        self.assertIn("Last activity", body)

    def test_index_shows_at_most_five_rows_per_project(self):
        for n in range(7):
            core.set_blocked(self.conn, self.task(title=f"T{n}"), PERSON, "wait")
        body = self.get("/")[1]
        self.assertEqual(body.count('class="irow"'), 5)
        self.assertIn("2 more on the board", body)
        self.assertIn("7 things need you", body)

    def test_monogram_and_tint(self):
        self.assertEqual(pages.monogram("Name PA"), "NP")
        self.assertEqual(pages.monogram("Skill"), "SK")
        self.assertEqual(pages.monogram("--"), "?")
        body = self.get("/")[1]
        self.assertRegex(body, r'class="mark m[1-5]" aria-hidden="true">NP<')

    def test_pin_from_index_returns_to_index(self):
        tid = self.task()
        self.conn.execute("UPDATE task SET inbox = 1")
        body = self.get("/")[1]
        self.assertIn('action="/a/pin"', body)
        status, _, h = self.post("/a/pin", {"id": tid, "next": "/"})
        self.assertEqual((status, h["Location"]), (303, "/"))
        self.conn.execute("UPDATE task SET inbox = 1")
        status, _, h = self.post("/a/pin", {"id": tid, "next": "//evil.example/"})
        self.assertEqual(h.get("Location"), f"/t/{tid}")

    def test_index_quiet_and_empty(self):
        self.assertIn("All 2 projects are quiet.", self.get("/")[1])
        self.conn.execute("DELETE FROM project")  # test-only: the app never deletes rows
        self.assertIn("No projects yet", self.get("/")[1])

    def test_refusal_shows_inline_on_task_page(self):
        tid = self.task()
        status, body, _ = self.post("/a/state", {"id": tid, "state": "dropped", "note": ""})
        self.assertEqual(status, 400)
        self.assertIn('class="inline-err" role="alert"', body)
        for w in ("WHAT:", "WHY:", "FIX:"):
            self.assertIn(w, body)
        self.assertIn("<h1>Build it</h1>", body)          # still the task page
        self.assertEqual(core.get_task(self.conn, tid)["state"], "open")

    def test_create_with_mode_from_dialog(self):
        body = self.get("/p/PA")[1]
        self.assertIn('<dialog class="dlg" id="new-task"', body)
        self.assertIn('value="read" checked', body)
        status, _, h = self.post("/a/create", {"project": "PB", "title": "T", "mode": "write"})
        self.assertEqual(status, 303)
        t = core.get_task(self.conn, h["Location"].rsplit("/", 1)[1])
        self.assertEqual((t["project"], t["mode"]), ("PB", "write"))

    def test_finished_column_shows_last_7_days_and_older_on_all(self):
        new, old = self.task(title="Fresh done"), self.task(title="Old done")
        for tid in (new, old):
            core.set_state(self.conn, tid, "done", PERSON)
        long_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
            timespec="seconds")
        self.conn.execute("UPDATE task SET updated = ? WHERE number = 2", (long_ago,))
        body = self.get("/p/PA")[1]
        self.assertIn("Fresh done", body)
        self.assertNotIn("Old done", body)
        self.assertIn('href="/p/PA/all">Show 1 older<', body)
        status, body, _ = self.get("/p/PA/all")
        self.assertEqual(status, 200)
        self.assertIn("Old done", body)
        self.assertIn("Show last 7 days only", body)

    def test_empty_board_and_start_from_card(self):
        self.assertIn("No tasks yet", self.get("/p/PB")[1])
        tid = self.task()
        body = self.get("/p/PA")[1]
        self.assertIn("Start<span class=\"vh\"> PA-1</span>", body)
        self.assertNotIn("Start · ", body)
        with mock.patch("coordinator.launcher.start_chat") as start:
            status, _, h = self.post("/a/start", {"id": tid, "next": "/p/PA"})
        self.assertEqual((status, h["Location"]), (303, "/p/PA"))
        start.assert_called_once()

    def test_task_page_timeline_and_chats(self):
        tid = self.task()
        core.claim(self.conn, tid, "abcd-9999", transcript_path="C:\\t\\a.jsonl")
        body = self.get(f"/t/{tid}")[1]
        self.assertIn("chat abcd working", body)
        self.assertIn('data-action="claim"', body)
        self.assertIn('data-action="created"', body)
        self.assertIn(">Working<", body)
        self.assertIn("Chat abcd already holds this task.", body)
        core.stop_without_report(self.conn, "abcd-9999", "gone")
        body = self.get(f"/t/{tid}")[1]
        self.assertIn("The chat stopped without a report.", body)
        self.assertIn(">No report<", body)

    def test_project_board_columns_follow_config(self):
        self.task("PA")
        body = self.get("/p/PA")[1]
        labels = ["Open", "Ready", "In work", "Needs clarification", "To check",
                  "Done", "Dropped"]
        pos = [body.index(f">{label}<") for label in labels]
        self.assertEqual(pos, sorted(pos))
        self.assertIn('action="/a/create"', body)

    def test_task_page_shows_history_and_transcript_path(self):
        tid = self.task("PA")
        core.claim(self.conn, tid, "sess-1", transcript_path="C:\\t\\x y\\s.jsonl")
        core.release(self.conn, tid, "sess-1", "done", "all done")
        body = self.get(f"/t/{tid}")[1]
        self.assertIn("C:\\t\\x y\\s.jsonl", body)
        self.assertIn("claim", body)
        self.assertIn("release", body)
        self.assertIn("all done", body)
        self.assertIn("claude --resume sess-1", body)
        self.assertIn("Copy", body)

    def test_post_without_token_is_403(self):
        tid = self.task()
        status, _, _ = self.post("/a/state", {"id": tid, "state": "done"}, token=False)
        self.assertEqual(status, 403)
        f = {"id": tid, "state": "done", "token": "wrong"}
        status, _, _ = self.request("POST", "/a/state", form=f,
                                    headers={"Origin": self.origin()})
        self.assertEqual(status, 403)
        self.assertEqual(core.get_task(self.conn, tid)["state"], "open")

    def test_post_with_foreign_origin_is_403(self):
        tid = self.task()
        for origin in ("http://evil.example", f"http://127.0.0.1:{self.port + 1}",
                       "null"):
            status, _, _ = self.post("/a/state", {"id": tid, "state": "done"},
                                     headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
        status, _, _ = self.post("/a/state", {"id": tid, "state": "done"}, origin=False,
                                 headers={"Referer": "http://evil.example/x"})
        self.assertEqual(status, 403)
        self.assertEqual(core.get_task(self.conn, tid)["state"], "open")

    def test_start_calls_launcher_with_root_and_id(self):
        tid = self.task("PA")
        with mock.patch("coordinator.launcher.start_chat") as start:
            status, _, h = self.post("/a/start", {"id": tid})
        self.assertEqual(status, 303)
        self.assertEqual(h["Location"], f"/t/{tid}")
        start.assert_called_once_with(self.roots["PA"], tid)

    def test_person_set_done_via_board_records_person_actor(self):
        tid = self.task()
        status, _, h = self.post("/a/state", {"id": tid, "state": "done", "note": "ok"})
        self.assertEqual(status, 303)
        self.assertEqual(h["Location"], f"/t/{tid}")
        t = core.get_task(self.conn, tid)
        self.assertEqual(t["state"], "done")
        ev = [e for e in t["history"] if e["action"] == "state"][-1]
        self.assertEqual((ev["actor_type"], ev["actor_id"]),
                         ("person", getpass.getuser()))

    def test_html_escapes_task_text(self):
        tid = self.task("PA", "<script>alert(1)</script>", goal="<b>g</b>")
        core.comment(self.conn, tid, PERSON, "comment", "<img src=x>")
        for path in ("/", "/p/PA", f"/t/{tid}"):
            body = self.get(path)[1]
            self.assertNotIn("<script>alert", body, path)
            self.assertNotIn("<img src=x>", body, path)
        self.assertIn("&lt;script&gt;", self.get("/p/PA")[1])
        self.conn.execute("UPDATE task SET inbox = 1")  # shows on index via Needs you
        self.assertIn("&lt;script&gt;", self.get("/")[1])

    # ------------------------------------------------------------ extra
    def test_wrong_host_is_403(self):
        for host in ("evil.example", f"127.0.0.1:{self.port + 1}", "127.0.0.1"):
            status, _, _ = self.request("GET", "/", headers={"Host": host}, host=False)
            self.assertEqual(status, 403, host)
        status, _, _ = self.request("GET", "/", headers={"Host": f"localhost:{self.port}"},
                                    host=False)
        self.assertEqual(status, 200)

    def test_post_with_token_but_no_origin_or_referer_is_403(self):
        tid = self.task()
        status, _, _ = self.post("/a/state", {"id": tid, "state": "done"}, origin=False)
        self.assertEqual(status, 403)
        status, _, _ = self.post("/a/state", {"id": tid, "state": "done"}, origin=False,
                                 headers={"Referer": f"http://localhost:{self.port}/t/x"})
        self.assertEqual(status, 303)

    def test_api_unchanged_by_swapping_templates_and_css(self):
        tid = self.task()
        paths = ["/api/index", "/api/project/PA", f"/api/task/{tid}"]
        before = [self.get_json(p) for p in paths]
        page_before = self.get("/")[1]
        alt = Path(self.tmp, "alt")
        shutil.copytree(board.WEB_DIR, alt)
        for f in (alt / "templates").glob("*.html"):
            f.write_text(f.read_text(encoding="utf-8") + "<!-- swapped -->",
                         encoding="utf-8")
        (alt / "static" / "board.css").write_text("body{color:red}", encoding="utf-8")
        with mock.patch.object(pages, "TEMPLATE_DIR", alt / "templates"), \
                mock.patch.object(board, "STATIC_DIR", alt / "static"):
            self.assertEqual([self.get_json(p) for p in paths], before)
            self.assertIn("swapped", self.get("/")[1])
            self.assertEqual(self.get("/static/board.css")[1], "body{color:red}")
        self.assertEqual(self.get("/")[1], page_before)

    def test_unknown_state_gets_its_own_column(self):
        tid = self.task()
        self.conn.execute("UPDATE task SET state = 'weird'")
        data = self.get_json("/api/project/PA")
        col = [c for c in data["columns"] if c["label"] == "Unknown state"]
        self.assertEqual(len(col), 1)
        self.assertEqual([c["id"] for c in col[0]["cards"]], [tid])
        self.assertIn(">Unknown state<", self.get("/p/PA")[1])

    def test_inbox_tasks_only_in_inbox_strip(self):
        tid = self.task()
        self.conn.execute("UPDATE task SET inbox = 1")
        data = self.get_json("/api/project/PA")
        self.assertEqual([c["id"] for c in data["inbox"]], [tid])
        self.assertTrue(all(not c["cards"] for c in data["columns"]))

    def test_needs_you_lists_expiring_lease(self):
        tid = self.task()
        old = datetime.now(timezone.utc) - timedelta(hours=3, minutes=45)
        core.claim(self.conn, tid, "sess-old", now=old)
        fresh = self.task("PB", "fresh")
        core.claim(self.conn, fresh, "sess-new")
        items = self.get_json("/api/index")["needs_you"]
        self.assertEqual([i["id"] for i in items], [tid])
        self.assertIn("lease", items[0]["reasons"][0])

    def test_card_shows_holder_and_blocked_once(self):
        tid = self.task()
        core.claim(self.conn, tid, "sess-9")
        core.set_blocked(self.conn, tid, PERSON, "waiting for key")
        core.set_open_points(self.conn, tid, PERSON, ["which db?"])
        body = self.get("/p/PA")[1]
        self.assertIn("Chat sess working", body)
        self.assertIn('<article class="tcard urgent">', body)       # red edge
        self.assertIn('class="pill red pw"', body)
        self.assertIn("Blocked: waiting for key", body)
        self.assertNotIn("which db?", body)                         # on the task page only
        self.assertEqual(body.count('<span class="idc">PA-1</span>'), 1)

    def test_handover_card_shows_note_and_no_state_word(self):
        tid = self.task()
        core.claim(self.conn, tid, "sess-7")
        core.release(self.conn, tid, "sess-7", "clarify", "Which database?")
        body = self.get("/p/PA")[1]
        card = body[body.index('<article class="tcard'):]
        card = card[:card.index("</article>")]
        self.assertIn('class="cnote" title="Which database?"', card)
        self.assertNotIn("Needs you", card)
        self.assertNotIn("class=\"pill", card)
        self.assertIn('class="mode-w"', card)

    def test_task_page_actions_and_start_visibility(self):
        tid = self.task()
        body = self.get(f"/t/{tid}")[1]
        self.assertIn('action="/a/start"', body)
        self.assertIn(f'name="token" value="{self.token}"', body)
        self.assertNotIn('action="/a/force-release"', body)
        core.claim(self.conn, tid, "s1")
        body = self.get(f"/t/{tid}")[1]
        self.assertNotIn('action="/a/start"', body)
        self.assertIn('action="/a/force-release"', body)

    def test_start_refused_with_live_claim(self):
        tid = self.task()
        core.claim(self.conn, tid, "s1")
        with mock.patch("coordinator.launcher.start_chat") as start:
            status, body, _ = self.post("/a/start", {"id": tid})
        self.assertEqual(status, 400)
        self.assertIn("WHAT:", body)
        start.assert_not_called()

    def test_coordinator_error_shows_what_why_fix_escaped(self):
        status, body, _ = self.post("/a/state", {"id": "PA-99", "state": "<x>"})
        self.assertEqual(status, 400)
        for w in ("WHAT:", "WHY:", "FIX:"):
            self.assertIn(w, body)
        self.assertIn("href=", body)

    def test_create_comment_mode_pin_force_release(self):
        status, _, h = self.post("/a/create", {"project": "PA", "title": "New <t>",
                                               "goal": "g", "done_when": "d",
                                               "scope": "s"})
        self.assertEqual(status, 303)
        tid = h["Location"].rsplit("/", 1)[1]
        t = core.get_task(self.conn, tid)
        self.assertEqual((t["title"], t["goal"], t["inbox"]), ("New <t>", "g", False))
        self.assertEqual(self.post("/a/comment", {"id": tid, "kind": "question",
                                                  "text": "why?"})[0], 303)
        self.assertEqual(self.post("/a/mode", {"id": tid, "mode": "write"})[0], 303)
        self.assertEqual(core.get_task(self.conn, tid)["mode"], "write")
        self.conn.execute("UPDATE task SET inbox = 1")
        self.assertEqual(self.post("/a/pin", {"id": tid})[0], 303)
        self.assertFalse(core.get_task(self.conn, tid)["inbox"])
        core.claim(self.conn, tid, "s1")
        self.assertEqual(self.post("/a/force-release", {"id": tid})[0], 303)
        self.assertEqual(core.get_task(self.conn, tid)["state"], "clarify")

    def test_unknown_route_404_and_static(self):
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get("/static/../board.py")[0], 404)
        self.assertEqual(self.post("/a/nope", {})[0], 404)
        status, body, h = self.get("/static/board.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", h["Content-Type"])
        self.assertIn(":root", body)
        self.assertEqual(self.get("/static/board.js")[0], 200)

    def test_post_errors_drain_the_body(self):
        # an error answer to a POST must read the body first, or the client
        # may see ConnectionResetError instead of the status
        big = {"text": "x" * 600_000}
        for _ in range(5):
            self.assertEqual(self.post("/a/nope", big)[0], 404)
            self.assertEqual(self.post("/a/state", big, headers={
                "Host": "evil.example"})[0], 403)

    def test_html_responses_forbid_framing(self):
        tid = self.task()
        pages = [self.get("/"), self.get("/p/PA"), self.get(f"/t/{tid}"),
                 self.get("/t/PA-999"),
                 self.post("/a/state", {"id": tid, "state": "nope"})]
        for status, _, h in pages:
            self.assertIn("text/html", h["Content-Type"], status)
            self.assertEqual(h["X-Frame-Options"], "DENY")
            self.assertEqual(h["Content-Security-Policy"], "frame-ancestors 'none'")

    def test_no_external_resources(self):
        body = self.get("/")[1] + self.get("/p/PA")[1]
        self.assertNotIn("http://", body.replace("http://127.0.0.1", ""))
        self.assertNotIn("https://", body)


    def test_start_with_missing_root_is_400_and_server_survives(self):
        tid = self.task("PA")
        shutil.rmtree(self.roots["PA"])
        status, body, _ = self.post("/a/start", {"id": tid})
        self.assertEqual(status, 400)
        for w in ("WHAT:", "WHY:", "FIX:"):
            self.assertIn(w, body)
        self.assertEqual(self.get("/")[0], 200)

    def test_bad_content_length_is_400(self):
        for value in ("abc", "-5"):
            status, _, _ = self.request(
                "POST", "/a/state", headers={"Origin": self.origin(),
                                             "Content-Length": value})
            self.assertEqual(status, 400, value)
        self.assertEqual(self.get("/")[0], 200)

    def test_unexpected_exception_is_500_page_and_server_keeps_serving(self):
        tid = self.task()
        boom = mock.Mock(side_effect=RuntimeError("boom <x>"))
        with mock.patch.dict(board.API, {"index": (boom, board.page_index)}):
            status, body, _ = self.get("/")
        self.assertEqual(status, 500)
        self.assertNotIn("Traceback", body)
        self.assertIn("WHAT:", body)
        with mock.patch("coordinator.core.set_state", side_effect=RuntimeError("x")):
            status, body, _ = self.post("/a/state", {"id": tid, "state": "done"})
        self.assertEqual(status, 500)
        self.assertEqual(self.get("/")[0], 200)
        self.assertTrue((Path(os.environ["COORDINATOR_HOME"]) / "board-errors.log").exists())


class LauncherTest(unittest.TestCase):
    def test_start_chat_opens_new_console_with_task_env(self):
        with mock.patch("subprocess.Popen") as popen:
            launcher.start_chat("C:\\root dir", "KMU-3")
        args, kw = popen.call_args
        self.assertEqual(args[0], ["cmd", "/k", "claude"])
        self.assertEqual(kw["cwd"], "C:\\root dir")
        self.assertEqual(kw["env"]["COORD_TASK"], "KMU-3")
        self.assertEqual(kw["creationflags"], subprocess.CREATE_NEW_CONSOLE)

    def test_start_chat_passes_fresh_launch_token(self):
        with mock.patch("subprocess.Popen") as popen:
            launcher.start_chat("C:\\root", "KMU-3")
            launcher.start_chat("C:\\root", "KMU-3")
        tokens = [c.kwargs["env"]["COORD_LAUNCH"] for c in popen.call_args_list]
        for tok in tokens:
            self.assertRegex(tok, r"^[0-9a-f]{16}$")
        self.assertNotEqual(tokens[0], tokens[1])
        self.assertEqual(popen.call_args.args[0], ["cmd", "/k", "claude"])


if __name__ == "__main__":
    unittest.main()
