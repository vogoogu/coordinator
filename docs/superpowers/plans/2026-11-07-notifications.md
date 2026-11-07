# Desktop Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows pop-ups with sound when a chat finishes a task ("ready to check") or a task stops and needs Daria ("needs you"), with one click to the task in the board.

**Architecture:** Core is unchanged. A new `notify.py` reads the event rows a front call just wrote, decides which pop-ups to show and words them. A new `toast.py` holds the Windows mechanics: toast XML, a detached hidden PowerShell process that shows the toast and plays the sound, the registry entry for branding, and starting the board. The fronts (`mcp_server`, `hooks`, `board`, `cli`) take an event mark before the core call and call `notify.fire` after it.

**Tech Stack:** Python 3.11 stdlib only (`sqlite3`, `subprocess`, `winreg`, `socket`, `tomllib`, `base64`), Windows PowerShell 5.1 + WinRT `Windows.UI.Notifications`, `System.Media.SoundPlayer`.

**Spec:** `docs/superpowers/specs/2026-11-07-notifications-design.md`

## Global Constraints

- Standard library only. No pip packages, no PowerShell modules to install.
- `coordinator/core.py` gets no changes and no mention of toasts, PowerShell or sounds.
- Notify fails open: any error is logged to `<app_home>/notify-errors.log` and swallowed. A move, MCP call or hook never fails or waits because of it.
- Only events with `actor_type` `chat` or `system` notify. Never `person`.
- Fire only after the core call has returned (its transaction is committed).
- Non-Windows (`sys.platform != "win32"`): `toast.show` does nothing.
- Tests never start PowerShell, never play sound and never touch the real registry. Test modules set `COORDINATOR_NOTIFY_SINK` (see Task 2) and use a temporary `COORDINATOR_HOME`.
- Don't touch Claude Code settings. `install-notify` writes only `HKCU\Software\Classes\AppUserModelId\Coordinator.Board`.
- Board URL base: `http://127.0.0.1:<board_port>` (the board binds 127.0.0.1). Default port 8790.

## Review Focus

1. **Expiry sweep with many stale claims** → exactly one summary pop-up, not one per task. Pinned in Task 1 (`test_three_stopped_make_one_summary`).
2. **A chat's note with XML specials or quotes** (`<`, `&`, `"`, `'`, umlauts) → the toast still shows, with the text intact. Pinned in Task 2 (`test_xml_escapes_text`, `test_script_is_encoded_command`).
3. **Broken `notify.toml`** → defaults are used, a log line is written, and the move still succeeds. Pinned in Task 1 (`test_broken_settings_use_defaults`).
4. **Board request that only reads** (a page load that runs `expire_leases` and expires nothing) → no pop-up, and no repeat pop-up for older events. Pinned in Task 1 (`test_no_new_events_no_alert`) and Task 3 (`test_board_page_load_without_expiry_is_quiet`).
5. **Toast launch raising** (PowerShell missing, `Popen` OSError) → the MCP call still returns its normal result. Pinned in Task 3 (`test_release_survives_toast_failure`).

---

## File Structure

- Create `coordinator/notify.py`: settings, event mark, the decision (`collect`), wording (`compose`), and `fire` (fail-open entry point for fronts). No Windows calls.
- Create `coordinator/toast.py`: `Toast` → XML, PowerShell script, detached launch (or sink file), branding registry, board up/start. No wording.
- Create `coordinator/web/icons/png/finished.png`, `needs-you.png`, `app.png` (96×96).
- Modify `coordinator/mcp_server.py` (`_dispatch`), `coordinator/hooks.py` (`_stop`, `_session_start`), `coordinator/board.py` (`_get`, POST path), `coordinator/cli.py` (`cmd_expire`, new `install-notify`, `uninstall-notify`).
- Create `tests/test_notify.py`, `tests/test_toast.py`. Modify `tests/test_board.py`, `tests/test_cli.py`, `tests/test_hooks.py`, `tests/test_mcp.py` (sink env at import).
- Modify `README.md` (section "Notifications").

---

### Task 1: Decision and wording (`notify.py`, pure part)

**Files:**
- Create: `coordinator/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: `core.release`, `core.set_state`, `core.expire_leases`, `core.stop_without_report`, `core.get_task` (tests only); tables `event`, `comment`, `task`, `project`.
- Produces:
  - `@dataclass Settings: enabled: bool = True; sound: bool = True; sound_finished: str = ""; sound_needs_you: str = ""; board_port: int = 8790`
  - `load_settings() -> Settings`: reads `paths.app_home() / "notify.toml"`. Missing file → defaults. Unreadable/invalid → defaults + `log_error("settings")`. Unknown keys are ignored. Wrong types → the default for that key.
  - `mark(conn) -> int`: `SELECT COALESCE(MAX(id), 0) FROM event`.
  - `@dataclass Alert: kind: str  # "finished" | "needs_you"; task_id: str; project: str; title: str; note: str; stopped: bool`
  - `collect(conn, since: int, task_ids: Iterable[str] | None = None) -> list[Alert]`
  - `compose(alerts: list[Alert], settings: Settings) -> list[toast.Toast]` (`Toast` is defined in Task 2. Until then, Task 1 may define `Toast` in `toast.py` as the bare dataclass from Task 2's Interfaces block.)
  - `log_error(where: str) -> None`: same pattern as `hooks.log_error`, file `notify-errors.log`.

**`collect` rules** (one `Alert` per task, ordered by first event id):
- Look at events with `id > since`, `actor_type IN ('chat','system')`, and (if `task_ids` is given) task display id in `task_ids`.
- `action='state'` and `to_value == cfg.on_done` → `finished`. `to_value == cfg.on_clarify` → `needs_you`. (`cfg` is the task's project config; if it can't be loaded, skip that task.)
- `action='release'` and `to_value IN ('expired','no_report')` → `needs_you` with `stopped=True`, even if no state event followed.
- One task, several matches → `needs_you` wins over `finished`. `stopped=True` if any stopped release.
- `note` = text of the newest `comment` row for that task (any actor), whitespace collapsed, cut to 140 chars with `…`. `""` if none.
- `project` = project `name`, falling back to `key`.

**`compose` wording** (exact copy):
- finished: heading `"{id} is ready to check"`, line1 `title`, line2 `note`.
- needs_you: heading `"{id} needs you"`, line1 `title`, line2 `"Chat stopped without a report"` if `stopped` else `note`.
- 2+ `needs_you` alerts in one call → replaced by one toast: heading `"{n} tasks need you"`, line1 `", ".join(ids)`, line2 `""`, kind `"needs_you"`, url = board root, button `"Open board"`. Finished alerts are still one toast each.
- Single toast: url `f"http://127.0.0.1:{port}/t/{id}"`, button `"Open task"`, attribution = project.
- `sound` = `""` if `not settings.sound`, else the configured path, else the defaults `C:\Windows\Media\Windows Notify System Generic.wav` (finished) and `C:\Windows\Media\Windows Notify Messaging.wav` (needs_you).
- `settings.enabled == False` → `[]`.

- [ ] **Step 1: Write the failing tests** in `tests/test_notify.py`

Setup like `tests/test_hooks.py` (temp `COORDINATOR_HOME`, project `KMU` with `name = "KMU Beratung"`, write-mode task claimed by session `"s1"` via `core.claim`). At module top: `os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)`.

```python
def test_release_done_is_finished(self):
    m = notify.mark(self.conn)
    core.release(self.conn, self.tid, "s1", "done", "All 9 variables done")
    [a] = notify.collect(self.conn, m)
    self.assertEqual((a.kind, a.task_id, a.project, a.note, a.stopped),
                     ("finished", self.tid, "KMU Beratung", "All 9 variables done", False))

def test_release_clarify_is_needs_you(self):  # kind "needs_you", note = the question

def test_set_state_by_chat_into_review_is_finished(self):
    # core.set_state(conn, tid, "review", core.Actor("chat","s1"), note="x") -> one "finished"

def test_person_move_is_quiet(self):
    # core.set_state(..., "review", PERSON, note="x") -> collect == []

def test_stop_without_report_is_stopped(self):
    # core.stop_without_report(conn, "s1", "n") -> one needs_you, stopped True

def test_expiry_on_person_moved_task_still_alerts(self):
    # person moves task to "review" (claim stays live), then expire_leases(now=+5h)
    # -> one needs_you, stopped True (no state event, release 'expired' counts)

def test_release_on_task_person_already_moved_is_quiet(self):
    # person sets "done" (claim ends closed_by_person); chat release done -> []

def test_no_new_events_no_alert(self):
    core.release(self.conn, self.tid, "s1", "done", "n")
    self.assertEqual(notify.collect(self.conn, notify.mark(self.conn)), [])

def test_task_ids_filter(self):
    # two tasks released by chats after the mark; collect(..., task_ids=[first]) -> only first

def test_three_stopped_make_one_summary(self):
    # three claimed tasks, expire_leases(now=+5h); toasts = compose(collect(...), Settings())
    self.assertEqual(len(toasts), 1)
    self.assertEqual(toasts[0].heading, "3 tasks need you")
    self.assertEqual(toasts[0].url, "http://127.0.0.1:8790/")
    self.assertEqual(toasts[0].button, "Open board")

def test_compose_single_finished(self):
    t, = notify.compose([notify.Alert("finished","KMU-1","KMU Beratung","Build it","ok",False)],
                        notify.Settings())
    self.assertEqual((t.heading, t.line1, t.line2, t.url, t.button, t.attribution, t.kind),
        ("KMU-1 is ready to check", "Build it", "ok", "http://127.0.0.1:8790/t/KMU-1",
         "Open task", "KMU Beratung", "finished"))
    self.assertEqual(t.sound, r"C:\Windows\Media\Windows Notify System Generic.wav")

def test_compose_stopped_wording(self):  # line2 == "Chat stopped without a report"
def test_note_is_shortened(self):        # 300-char note -> len 140, endswith "…"
def test_sound_off_and_custom(self):     # Settings(sound=False) -> sound ""; sound_finished="C:\\x.wav" -> used
def test_disabled_gives_nothing(self):   # Settings(enabled=False) -> []

def test_settings_file(self):
    # write notify.toml: enabled=false, board_port=9000 -> load_settings() matches
def test_broken_settings_use_defaults(self):
    # notify.toml = "enabled = [" -> load_settings() == Settings(); notify-errors.log exists
def test_wrong_type_falls_back(self):    # board_port = "x" -> 8790
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m unittest tests.test_notify -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'coordinator.notify'` (or `cannot import name`).

- [ ] **Step 3: Implement** `Settings`, `load_settings`, `mark`, `Alert`, `collect`, `compose`, `log_error` in `coordinator/notify.py` per the Interfaces and rules above. Get project config through `config` the same way `core._load` does (a `core` read helper is fine; no new writes).

- [ ] **Step 4: Run the tests to see them pass**

Run: `python -m unittest tests.test_notify -v`
Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git add coordinator/notify.py coordinator/toast.py tests/test_notify.py
git commit -m "feat(notify): decide and word finished / needs-you alerts from event rows"
```

---

### Task 2: Windows toast mechanics (`toast.py`) and icons

**Files:**
- Create/complete: `coordinator/toast.py`
- Create: `coordinator/web/icons/png/finished.png`, `needs-you.png`, `app.png`
- Test: `tests/test_toast.py`

**Interfaces:**
- Produces:
  - `@dataclass Toast: kind: str; heading: str; line1: str; line2: str; attribution: str; url: str; button: str; sound: str`
  - `AUMID = "Coordinator.Board"`; `PS_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"`; `REG_PATH = r"Software\Classes\AppUserModelId\Coordinator.Board"`
  - `ICON_DIR = Path(__file__).parent / "web" / "icons" / "png"`; icon for kind: `finished.png` / `needs-you.png`.
  - `toast_xml(t: Toast) -> str`
  - `ps_script(t: Toast, aumid: str) -> str`
  - `show(t: Toast, aumid: str) -> None`: if env `COORDINATOR_NOTIFY_SINK` is set, append `json.dumps(asdict(t) | {"aumid": aumid})` + `"\n"` to that file (when it is `os.devnull`, writing is a no-op) and return. Else, on win32, start `powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand <b64 UTF-16LE of ps_script>` with `creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, stdio to DEVNULL. Don't wait.
  - `registered() -> bool`: `winreg.OpenKey(HKEY_CURRENT_USER, REG_PATH)` succeeds.
  - `register(icon: Path = ICON_DIR / "app.png") -> None`: create the key with `DisplayName = "Coordinator"` and `IconUri = str(icon)` (REG_SZ).
  - `unregister() -> bool`: delete the key. False if it didn't exist.
  - `board_up(port: int) -> bool`: `socket.create_connection(("127.0.0.1", port), timeout=0.3)`. No HTTP request (a board request would run `expire_leases`).
  - `start_board(port: int) -> None`: detached `[pythonw.exe beside sys.executable, or sys.executable] -m coordinator board --no-browser --port <port>`, `cwd` = repo root (`Path(__file__).parent.parent`), `env` with `PYTHONPATH` = repo root, same creationflags, stdio DEVNULL. Skipped when the sink env is set.

**XML** (`toast_xml`), every text value escaped with `xml.sax.saxutils.escape(..., {'"': "&quot;"})`:

```xml
<toast activationType="protocol" launch="{url}">
  <visual><binding template="ToastGeneric">
    <image placement="appLogoOverride" src="{file URI of icon}"/>
    <text>{heading}</text><text>{line1}</text><text>{line2}</text>
    <text placement="attribution">{attribution}</text>
  </binding></visual>
  <actions>
    <action content="{button}" activationType="protocol" arguments="{url}"/>
    <action content="Later" activationType="system" arguments="dismiss"/>
  </actions>
  <audio silent="true"/>
</toast>
```

Leave out `<text>` elements that are empty (line2, attribution). Icon URI: `Path.as_uri()`.

**Script** (`ps_script`): load `Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime` and `Windows.Data.Xml.Dom.XmlDocument, ...`. Then `LoadXml` of the XML in a PowerShell single-quoted here-string (`@'`…`'@`, which is literal; the XML must not contain a line that starts with `'@`, which escaping guarantees). Then `CreateToastNotifier('<aumid>').Show(...)`. If `t.sound` is set and the file exists: `(New-Object System.Media.SoundPlayer '<path with ' doubled>').PlaySync()`.

- [ ] **Step 1: Make the icons.** Write a throwaway HTML file per icon in the scratchpad: a 96×96 rounded tile (radius 20) in the board's colours, with the board's SVG path centred at 56px and a stroke width of 1.8 scaled. Finished uses `circle-check.svg` on `#E9F3EE`, stroke `#2E6B4B`. Needs-you uses `question.svg` on `#FFF4E2`, stroke `#8A5300`. App is a white bold "C" on `#03172B`. Render each with `msedge --headless --disable-gpu --screenshot=<out.png> --window-size=96,96 --default-background-color=00000000 <file.html>` into `coordinator/web/icons/png/`. Check: three PNG files, 96×96. Open one with the Read tool to eyeball it.

- [ ] **Step 2: Write the failing tests** in `tests/test_toast.py` (module top: `os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)`)

```python
T = toast.Toast("finished", "KMU-1 is ready to check", "Build <it> & \"go\"", "it's Grüß",
                "KMU Beratung", "http://127.0.0.1:8790/t/KMU-1", "Open task", "")

def test_xml_parses_and_has_parts(self):
    root = ET.fromstring(toast.toast_xml(T))
    self.assertEqual(root.get("launch"), "http://127.0.0.1:8790/t/KMU-1")
    texts = [e.text for e in root.iter("text")]
    self.assertEqual(texts, ["KMU-1 is ready to check", 'Build <it> & "go"', "it's Grüß", "KMU Beratung"])
    self.assertEqual([a.get("content") for a in root.iter("action")], ["Open task", "Later"])
    self.assertTrue(root.find(".//image").get("src").endswith("finished.png"))
    self.assertEqual(root.find("audio").get("silent"), "true")

def test_xml_escapes_text(self):     # raw XML string contains "&lt;it&gt;" and "&amp;"
def test_empty_line2_left_out(self):  # line2="" -> 3 <text> elements
def test_needs_you_icon(self):        # kind "needs_you" -> src endswith "needs-you.png"
def test_script_uses_aumid_and_sound(self):
    s = toast.ps_script(dataclasses.replace(T, sound=r"C:\a'b.wav"), toast.AUMID)
    self.assertIn("CreateToastNotifier('Coordinator.Board')", s)
    self.assertIn(r"'C:\a''b.wav'", s)
def test_script_without_sound_has_no_player(self):

def test_script_is_encoded_command(self):
    with mock.patch.dict(os.environ, {"COORDINATOR_NOTIFY_SINK": ""}), \
         mock.patch.object(toast.sys, "platform", "win32"), \
         mock.patch.object(toast.subprocess, "Popen") as p:
        os.environ.pop("COORDINATOR_NOTIFY_SINK")
        toast.show(T, toast.AUMID)
    args = p.call_args.args[0]
    self.assertIn("-EncodedCommand", args)
    decoded = base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")
    self.assertEqual(decoded, toast.ps_script(T, toast.AUMID))

def test_sink_records_and_launches_nothing(self):
    # sink = temp file; Popen patched; show() -> one JSON line with heading and aumid; Popen not called
def test_not_windows_does_nothing(self):  # sink unset, platform "linux", Popen not called

def test_register_writes_key(self):
    # mock.patch.object(toast, "winreg") -> register(); CreateKey called with HKEY_CURRENT_USER, REG_PATH;
    # SetValueEx called for "DisplayName" = "Coordinator" and "IconUri" ending "app.png"
def test_unregister_missing_key(self):     # winreg mock DeleteKey raises FileNotFoundError -> False
def test_board_up_false_on_free_port(self):  # bind a socket, read its port, close it -> board_up(port) False
def test_board_up_true_on_listener(self):    # listening socket -> True
```

- [ ] **Step 3: Run them to see them fail**

Run: `python -m unittest tests.test_toast -v`
Expected: FAIL / ERROR (missing functions).

- [ ] **Step 4: Implement** the `toast.py` functions per the Interfaces above. Import `winreg` lazily or guarded (`try: import winreg except ImportError: winreg = None`) so the module imports on any platform.

- [ ] **Step 5: Run the tests to see them pass**

Run: `python -m unittest tests.test_toast tests.test_notify -v`
Expected: all OK.

- [ ] **Step 6: Commit**

```bash
git add coordinator/toast.py coordinator/web/icons/png tests/test_toast.py
git commit -m "feat(notify): Windows toast XML, detached PowerShell launch, branding key, icons"
```

---

### Task 3: `fire` and wiring into the fronts

**Files:**
- Modify: `coordinator/notify.py` (add `fire`)
- Modify: `coordinator/mcp_server.py:180-205` (`_dispatch`), `coordinator/hooks.py:125-240` (`_session_start`, `_stop`), `coordinator/board.py:366-370` and `:430-433`, `coordinator/cli.py:143-146` (`cmd_expire`)
- Test: `tests/test_notify.py` (fire), `tests/test_mcp.py`, `tests/test_hooks.py`, `tests/test_board.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 `mark`, `collect`, `compose`, `load_settings`, `log_error`; Task 2 `show`, `registered`, `AUMID`, `PS_AUMID`, `board_up`, `start_board`.
- Produces: `fire(conn, since: int, task_ids: Iterable[str] | None = None) -> None`. It never raises. It loads settings, collects and composes. If there's nothing to show, it returns before any Windows call. If the board isn't up, it starts it (once per call). Then `show(t, AUMID if registered() else PS_AUMID)` for each toast. Any exception → `log_error("fire")`.

**Wiring** (`m = notify.mark(conn)` before the core call, `notify.fire(conn, m, ids)` after it returns, never inside a transaction):
- `mcp_server._dispatch`: `task_release` and `task_set_state` → `ids=[id]`.
- `hooks._stop`: around `core.stop_without_report` → `ids=None` (the session's task is the only one moved).
- `hooks._session_start`: around `core.expire_leases(conn)` → `ids=<returned list>`; skip `fire` if the list is empty.
- `board._get` and the POST path: around `core.expire_leases(conn)` → same as above.
- `cli.cmd_expire`: same as above.

- [ ] **Step 1: Write the failing tests.** In each of the four existing test modules, add at module top: `os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)`. Front tests that check pop-ups set the env to a temp sink file instead (for the MCP subprocess, pass it in `env`) and read its JSON lines.

```python
# tests/test_notify.py
def test_fire_shows_and_starts_board(self):
    with mock.patch.object(notify.toast, "show") as show, \
         mock.patch.object(notify.toast, "board_up", return_value=False), \
         mock.patch.object(notify.toast, "start_board") as start, \
         mock.patch.object(notify.toast, "registered", return_value=True):
        m = notify.mark(self.conn)
        core.release(self.conn, self.tid, "s1", "done", "ok")
        notify.fire(self.conn, m, [self.tid])
    start.assert_called_once_with(8790)
    self.assertEqual(show.call_args.args[1], "Coordinator.Board")
def test_fire_falls_back_to_powershell_aumid(self):  # registered False -> PS_AUMID
def test_fire_nothing_to_show_touches_nothing(self):  # no events -> board_up/show not called
def test_fire_swallows_errors(self):
    # show raises OSError -> fire returns None; notify-errors.log contains "fire"

# tests/test_mcp.py (sink file in env)
def test_release_done_sends_toast(self):    # task_release done -> sink has 1 line, heading "KMU-1 is ready to check"
def test_set_state_clarify_sends_toast(self):
def test_release_survives_toast_failure(self):
    # env COORDINATOR_NOTIFY_SINK = a directory path (opening it to append fails)
    # -> task_release result is the normal task JSON, no MCP error; notify-errors.log exists

# tests/test_hooks.py
def test_stop_without_report_sends_needs_you(self):  # sink line heading "KMU-1 needs you", line2 "Chat stopped without a report"
def test_session_start_expiry_sends_one_summary(self):  # 2 stale claims -> 1 line "2 tasks need you"

# tests/test_board.py
def test_board_page_load_expires_and_notifies(self):  # stale claim, GET / -> 1 sink line
def test_board_page_load_without_expiry_is_quiet(self):  # nothing stale, GET / twice -> sink empty

# tests/test_cli.py
def test_expire_command_notifies(self):  # `expire --hours 0` on a claimed task -> 1 sink line
```

Use the existing helpers in each module for claims and stale claims (move `last_seen` back the same way the existing expiry tests do).

- [ ] **Step 2: Run them to see them fail**

Run: `python -m unittest discover -s tests -v`
Expected: the new tests FAIL (no sink lines / no `fire`). Old tests still pass.

- [ ] **Step 3: Implement** `notify.fire` and the six wiring points listed above.

- [ ] **Step 4: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: all OK. No PowerShell window appears and no sound plays during the run.

- [ ] **Step 5: Commit**

```bash
git add coordinator/notify.py coordinator/mcp_server.py coordinator/hooks.py coordinator/board.py coordinator/cli.py tests
git commit -m "feat(notify): fire pop-ups after chat/system moves from MCP, hooks, board and CLI"
```

---

### Task 4: `install-notify` / `uninstall-notify`, README, manual check

**Files:**
- Modify: `coordinator/cli.py` (two subcommands, wired like `install-help`: they need no DB connection)
- Modify: `README.md` (section "Notifications")
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `toast.register`, `toast.unregister`, `toast.registered`, `toast.ICON_DIR`.
- Produces: `cmd_install_notify(args, conn) -> int`, `cmd_uninstall_notify(args, conn) -> int`.

**Exact output:**
- install: `Pop-ups will show as "Coordinator". Undo with: coordinator uninstall-notify` → exit 0.
- uninstall: `Removed. Pop-ups will show as "Windows PowerShell".` or, if there was nothing to remove, `Nothing to remove.` → exit 0.
- Not Windows: a three-line WHAT/WHY/FIX error via `messages` (WHAT: install-notify only works on Windows; WHY: the pop-up branding is a Windows registry entry; FIX: run it on the Windows laptop) → exit 1.

- [ ] **Step 1: Write the failing tests** (`toast.winreg` patched with a mock in each)

```python
def test_install_notify(self):     # main(["install-notify"]) == 0; register called once; stdout has 'uninstall-notify'
def test_uninstall_notify(self):   # unregister -> True: "Removed."; -> False: "Nothing to remove."
def test_install_notify_not_windows(self):  # platform "linux" -> exit 1, stderr has WHAT/WHY/FIX lines
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m unittest tests.test_cli -v`
Expected: FAIL (`invalid choice: 'install-notify'`).

- [ ] **Step 3: Implement** both commands, the parser entries ("show pop-ups as Coordinator" / "undo install-notify") and the `main` short-cut that skips `db.connect()` for them. Add the "Notifications" section to `README.md`. It covers: when pop-ups fire, `install-notify` / `uninstall-notify`, `notify.toml` keys with defaults, and the known limitation (a crashed chat is noticed after the 4-hour lease, when the board is used or a chat starts).

- [ ] **Step 4: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: all OK.

- [ ] **Step 5: Manual check on the laptop** (Daria, or the agent with her OK):
  1. `python -m coordinator install-notify` → message printed.
  2. With the board **stopped**, in a test project: claim a task from a chat, call `task_release` done. Expected: a pop-up with "Coordinator", the green icon, "<ID> is ready to check", a chime. "Open task" opens `/t/<ID>` (the board was started in the background).
  3. `task_release` clarify on another task → amber pop-up "needs you" and a different sound.
  4. Move a task to "To check" on the board yourself → no pop-up.
  5. `python -m coordinator uninstall-notify` → the next pop-up's header shows "Windows PowerShell".

- [ ] **Step 6: Commit**

```bash
git add coordinator/cli.py README.md tests/test_cli.py
git commit -m "feat(notify): install-notify / uninstall-notify and README section"
```

---

## Known limitations (from the spec, not tasks)

- A chat that crashes or is closed without its stop hook is noticed only after the 4-hour lease, and only when the board is used or a chat starts.
- With Windows Do Not Disturb on, the toast is held back but the sound still plays (the sound is in the same PowerShell process; no cheap DND check in PowerShell 5.1).
