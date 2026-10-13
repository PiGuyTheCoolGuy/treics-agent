from __future__ import annotations

import dataclasses
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from treics_agent.config import Config
from treics_agent.storage import Store
from treics_agent.tools import Desktop, ToolError, Tools
from test_agent import FakeDesktop


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = dataclasses.replace(Config(), data_dir=self.temp.name + "/data",
                                          work_dir=self.temp.name + "/work")
        self.desktop = FakeDesktop(self.config)
        self.store = Store(self.config.data_path)
        self.tools = Tools(self.config, self.store, self.desktop)
        self.stop = threading.Event()

    def tearDown(self):
        self.desktop.marker.close()
        self.store.close()
        self.temp.cleanup()

    def test_function_keys_modifiers_and_common_names_ignore_capitalization(self):
        for number in range(1, 36):
            with self.subTest(number=number):
                self.assertEqual(Desktop.key_combination(["Alt", f"f{number}"]), f"alt+F{number}")
                self.assertEqual(Desktop.key_combination(["ALT", f"F{number}"]), f"alt+F{number}")
        for original, expected in (("return", "Return"), ("ENTER", "Return"), ("ESC", "Escape"),
                                   ("bAcKsPaCe", "BackSpace"), ("left", "Left"), ("HOME", "Home"),
                                   ("PgUp", "Page_Up"), ("page_down", "Page_Down"),
                                   ("control_r", "Control_R"), ("kp_enter", "KP_Enter")):
            with self.subTest(key=original):
                self.assertEqual(Desktop.key_combination([original]), expected)
        self.assertEqual(Desktop.key_combination(["CoNtRoL", "SHIFT", "v"]), "ctrl+shift+v")

    def test_letter_case_and_other_valid_x11_key_names_are_preserved(self):
        self.assertEqual(Desktop.key_combination(["CTRL", "a"]), "ctrl+a")
        self.assertEqual(Desktop.key_combination(["CTRL", "A"]), "ctrl+A")
        for name in ("Aacute", "aacute", "XF86AudioMute", "U4E2D", "0xffc1", "36"):
            with self.subTest(key=name):
                self.assertEqual(Desktop.key_combination([name]), name)

    def test_press_hotkey_and_clipboard_paste_use_and_report_normalized_keys(self):
        pressed = self.tools.execute("press", {"key": "f4"}, self.stop)
        shortcut = self.tools.execute("hotkey", {"keys": ["ALT", "f4"]}, self.stop)
        with mock.patch("treics_agent.tools.subprocess.run") as clipboard:
            pasted = self.tools.execute("type", {"text": "hello", "paste_keys": ["CTRL", "SHIFT", "v"]}, self.stop)
            self.assertEqual(clipboard.call_args.kwargs["input"], b"hello")
        self.assertEqual([pressed["keys_sent"], shortcut["keys_sent"], pasted["keys_sent"]],
                         ["F4", "alt+F4", "ctrl+shift+v"])
        for combination in ("F4", "alt+F4", "ctrl+shift+v"):
            self.assertIn(["xdotool", "key", "--clearmodifiers", combination], self.desktop.commands)

    def test_invalid_key_is_rejected_before_any_shortcut_or_clipboard_change(self):
        for key in ("f0", "F36", "not_a_real_key", "f04", "999", "--window", "alt+f4"):
            with self.subTest(key=key), self.assertRaises(ToolError):
                self.desktop.key(["alt", key])
        self.assertEqual(self.desktop.commands, [])
        with mock.patch("treics_agent.tools.subprocess.run") as clipboard:
            with self.assertRaisesRegex(ToolError, "Unknown X11 key.*no shortcut was sent"):
                self.tools.execute("type", {"text": "must not change clipboard", "paste_keys": ["ctrl", "invalid_key"]}, self.stop)
            clipboard.assert_not_called()
        self.assertEqual(self.desktop.commands, [])

    def test_xdotool_ignored_key_warning_is_failure_even_with_zero_exit_status(self):
        ignored = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"(symbol) No such key name 'F4'. Ignoring it.\n")
        with mock.patch.object(self.desktop, "command", return_value=ignored):
            with self.assertRaisesRegex(ToolError, "Part of the shortcut may have been sent") as error:
                self.tools.execute("hotkey", {"keys": ["alt", "f4"]}, self.stop)
        self.assertIn("rejected shortcut 'alt+F4'", str(error.exception))


if __name__ == "__main__":
    unittest.main()
