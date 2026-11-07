import base64
import dataclasses
import json
import os
import shutil
import socket
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

os.environ.setdefault("COORDINATOR_NOTIFY_SINK", os.devnull)

from coordinator import toast  # noqa: E402

T = toast.Toast("finished", "KMU-1 is ready to check", 'Build <it> & "go"', "it's Grüß",
                "KMU Beratung", "http://127.0.0.1:8790/t/KMU-1", "Open task", "")


class ToastXmlTest(unittest.TestCase):
    def test_xml_parses_and_has_parts(self):
        root = ET.fromstring(toast.toast_xml(T))
        self.assertEqual(root.get("launch"), "http://127.0.0.1:8790/t/KMU-1")
        self.assertEqual(root.get("activationType"), "protocol")
        self.assertEqual([e.text for e in root.iter("text")],
                         ["KMU-1 is ready to check", 'Build <it> & "go"', "it's Grüß",
                          "KMU Beratung"])
        self.assertEqual([a.get("content") for a in root.iter("action")],
                         ["Open task", "Later"])
        self.assertEqual(root.find(".//action").get("arguments"), T.url)
        self.assertTrue(root.find(".//image").get("src").endswith("finished.png"))
        self.assertTrue(root.find(".//image").get("src").startswith("file:///"))
        self.assertEqual(root.find("audio").get("silent"), "true")

    def test_xml_escapes_text(self):
        x = toast.toast_xml(T)
        self.assertIn("&lt;it&gt;", x)
        self.assertIn("&amp;", x)

    def test_empty_line2_left_out(self):
        root = ET.fromstring(toast.toast_xml(dataclasses.replace(T, line2="")))
        self.assertEqual(len(list(root.iter("text"))), 3)

    def test_empty_attribution_left_out(self):
        root = ET.fromstring(toast.toast_xml(dataclasses.replace(T, attribution="")))
        self.assertEqual([e.get("placement") for e in root.iter("text")], [None] * 3)

    def test_needs_you_icon(self):
        root = ET.fromstring(toast.toast_xml(dataclasses.replace(T, kind="needs_you")))
        self.assertTrue(root.find(".//image").get("src").endswith("needs-you.png"))

    def test_icons_exist(self):
        for name in ("finished.png", "needs-you.png", "app.png"):
            self.assertTrue((toast.ICON_DIR / name).is_file(), name)

    def test_script_uses_aumid_and_sound(self):
        s = toast.ps_script(dataclasses.replace(T, sound=r"C:\a'b.wav"), toast.AUMID)
        self.assertIn("CreateToastNotifier('Coordinator.Board')", s)
        self.assertIn(r"'C:\a''b.wav'", s)
        self.assertIn("SoundPlayer", s)
        self.assertIn(toast.toast_xml(T), s)

    def test_script_without_sound_has_no_player(self):
        self.assertNotIn("SoundPlayer", toast.ps_script(T, toast.AUMID))


class ToastShowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coord_toast_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_script_is_encoded_command(self):
        env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_NOTIFY_SINK"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(toast.sys, "platform", "win32"), \
                mock.patch.object(toast.subprocess, "Popen") as p:
            toast.show(T, toast.AUMID)
        args = p.call_args.args[0]
        self.assertIn("-EncodedCommand", args)
        self.assertIn("-NoProfile", args)
        decoded = base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")
        self.assertEqual(decoded, toast.ps_script(T, toast.AUMID))

    def test_sink_records_and_launches_nothing(self):
        sink = os.path.join(self.tmp, "sink.jsonl")
        with mock.patch.dict(os.environ, {"COORDINATOR_NOTIFY_SINK": sink}), \
                mock.patch.object(toast.subprocess, "Popen") as p:
            toast.show(T, toast.AUMID)
            toast.start_board(8790)
        p.assert_not_called()
        with open(sink, encoding="utf-8") as f:
            [line] = f.readlines()
        rec = json.loads(line)
        self.assertEqual((rec["heading"], rec["aumid"]),
                         ("KMU-1 is ready to check", "Coordinator.Board"))

    def test_not_windows_does_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_NOTIFY_SINK"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(toast.sys, "platform", "linux"), \
                mock.patch.object(toast.subprocess, "Popen") as p:
            toast.show(T, toast.AUMID)
            toast.start_board(8790)
        p.assert_not_called()

    def test_start_board_command(self):
        env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_NOTIFY_SINK"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(toast.sys, "platform", "win32"), \
                mock.patch.object(toast.subprocess, "Popen") as p:
            toast.start_board(9001)
        args = p.call_args.args[0]
        self.assertEqual(args[1:], ["-m", "coordinator", "board", "--no-browser",
                                    "--port", "9001"])
        self.assertEqual(p.call_args.kwargs["env"]["PYTHONPATH"], str(toast.REPO_ROOT))


class ToastRegistryTest(unittest.TestCase):
    def test_register_writes_key(self):
        with mock.patch.object(toast, "winreg") as reg:
            toast.register()
        reg.CreateKey.assert_called_once_with(reg.HKEY_CURRENT_USER, toast.REG_PATH)
        values = {c.args[1]: c.args[4] for c in reg.SetValueEx.call_args_list}
        self.assertEqual(values["DisplayName"], "Coordinator")
        self.assertTrue(values["IconUri"].endswith("app.png"))

    def test_unregister(self):
        with mock.patch.object(toast, "winreg") as reg:
            self.assertTrue(toast.unregister())
        reg.DeleteKey.assert_called_once_with(reg.HKEY_CURRENT_USER, toast.REG_PATH)

    def test_unregister_missing_key(self):
        with mock.patch.object(toast, "winreg") as reg:
            reg.DeleteKey.side_effect = FileNotFoundError
            self.assertFalse(toast.unregister())

    def test_registered(self):
        with mock.patch.object(toast, "winreg") as reg:
            self.assertTrue(toast.registered())
            reg.OpenKey.side_effect = FileNotFoundError
            self.assertFalse(toast.registered())

    def test_registered_without_winreg(self):
        with mock.patch.object(toast, "winreg", None):
            self.assertFalse(toast.registered())


class BoardUpTest(unittest.TestCase):
    def test_board_up_false_on_free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(toast.board_up(port))

    def test_board_up_true_on_listener(self):
        s = socket.socket()
        self.addCleanup(s.close)
        s.bind(("127.0.0.1", 0))
        s.listen()
        self.assertTrue(toast.board_up(s.getsockname()[1]))


if __name__ == "__main__":
    unittest.main()
