from __future__ import annotations

import base64
import dataclasses
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

from treics_agent.agent import Agent
from treics_agent.config import Config, initialize_config, load_config
from treics_agent.model import ModelError, parse_decision
from treics_agent.storage import Store
from treics_agent.tools import Desktop, ToolError, Tools


class FakeDesktop(Desktop):
    def __init__(self, config):
        super().__init__(config)
        self.captures = 0
        self.commands = []

    def capture(self):
        self.captures += 1
        image = Image.new("RGB", (1024, 768), "#122037")
        output = io.BytesIO()
        image.save(output, format="JPEG")
        self.image = output.getvalue()
        self.frame = {"width": 1024, "height": 768, "desktop_width": 1920,
                      "desktop_height": 1440, "captured_at": time.time(),
                      "base64": base64.b64encode(self.image).decode()}
        return self.frame.copy()

    def command(self, args, **kwargs):
        self.commands.append(args)


def decision(action, arguments, state="Working state"):
    return {"reason": "Test action", "action": action, "arguments": arguments, "state": state}


class FakeModel:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.screens = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.calls = 0

    def decide(self, task, state, memory, events, screen, stop):
        self.screens.append(screen)
        self.calls += 1
        self.entered.set()
        self.release.wait(5)
        return next(self.decisions), {}

    def cancel(self):
        self.release.set()


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = dataclasses.replace(Config(), data_dir=self.temp.name + "/data",
                                          work_dir=self.temp.name + "/work", dashboard_token="a" * 32)
        self.store = Store(self.config.data_path)
        self.desktop = FakeDesktop(self.config)
        self.tools = Tools(self.config, self.store, self.desktop)
        self.agent = None

    def tearDown(self):
        if self.agent:
            self.agent.shutdown()
        self.store.close()
        self.temp.cleanup()

    def build(self, decisions):
        model = FakeModel(decisions)
        self.agent = Agent(self.config, self.store, self.tools, model)
        return model

    def complete(self, task="Test", max_steps=None):
        run_id = self.agent.start(task, max_steps)
        self.agent.thread.join(5)
        self.assertFalse(self.agent.thread.is_alive(), "Agent did not terminate")
        return self.store.run(run_id)

    def test_real_shell_file_memory_and_finish(self):
        self.build([
            decision("write_file", {"path": "hello.txt", "content": "你好 Owen"}),
            decision("shell", {"command": "cat hello.txt"}),
            decision("remember", {"text": "Output is in ~/agent-workspace/hello.txt"}),
            decision("finish", {"summary": "Created and verified hello.txt"}),
        ])
        run = self.complete()
        self.assertEqual(run["status"], "completed")
        self.assertEqual((self.config.work_path / "hello.txt").read_text(), "你好 Owen")
        events = self.store.events(run["id"])
        self.assertTrue(any(e["data"].get("result", {}).get("output") == "你好 Owen" for e in events))
        self.assertIn("hello.txt", self.store.memory())
        self.assertEqual(run["step"], 4)

    def test_only_latest_screenshot_and_refresh_after_gui(self):
        model = self.build([
            decision("shell", {"command": "printf ready"}),
            decision("screenshot", {}),
            decision("click", {"x": 512, "y": 384}),
            decision("finish", {"summary": "Done"}),
        ])
        self.complete()
        self.assertIsNotNone(model.screens[0])
        self.assertIsNone(model.screens[1])
        self.assertIsNotNone(model.screens[2])
        self.assertEqual(self.desktop.captures, 3)
        self.assertIn(["xdotool", "mousemove", "--sync", "960", "720"], self.desktop.commands)

    def test_coordinate_mapping_and_invalid_coordinates(self):
        self.desktop.capture()
        self.assertEqual(self.desktop.coordinates(1023, 767), (1918, 1438))
        for x, y in ((-1, 0), (1024, 0), (False, 0), (2.5, 0)):
            with self.assertRaises(ToolError):
                self.desktop.coordinates(x, y)
        self.desktop.frame = None
        with self.assertRaises(ToolError):
            self.desktop.coordinates(1, 1)

    def test_stop_during_inference_never_executes_returned_action(self):
        model = self.build([decision("write_file", {"path": "must-not-exist", "content": "bad"})])
        model.release.clear()
        run_id = self.agent.start("Stop test")
        self.assertTrue(model.entered.wait(2))
        self.agent.stop()
        self.agent.thread.join(3)
        self.assertEqual(self.store.run(run_id)["status"], "stopped")
        self.assertFalse((self.config.work_path / "must-not-exist").exists())

    def test_pause_gates_action_then_resume(self):
        model = self.build([decision("write_file", {"path": "after-resume", "content": "ok"}),
                            decision("finish", {"summary": "Done"})])
        model.release.clear()
        run_id = self.agent.start("Pause test")
        self.assertTrue(model.entered.wait(2))
        self.agent.pause()
        model.release.set()
        deadline = time.monotonic() + 2
        while self.store.run(run_id)["status"] != "paused" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.store.run(run_id)["status"], "paused")
        self.assertFalse((self.config.work_path / "after-resume").exists())
        self.agent.resume()
        self.agent.thread.join(3)
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertTrue((self.config.work_path / "after-resume").exists())

    def test_reject_concurrent_runs_and_step_budget(self):
        model = self.build([decision("remember", {"text": "one step"})])
        model.release.clear()
        run_id = self.agent.start("First", 1)
        self.assertTrue(model.entered.wait(2))
        with self.assertRaises(ValueError):
            self.agent.start("Second")
        model.release.set()
        self.agent.thread.join(3)
        self.assertEqual(self.store.run(run_id)["status"], "limit_reached")

    def test_shell_timeout_and_output_limit(self):
        stopped = threading.Event()
        result = self.tools.execute("shell", {"command": "printf begin; sleep 5", "timeout": 1}, stopped)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["ok"])
        result = self.tools.execute("shell", {"command": "python3 -c 'print(\"x\"*100000)'"}, stopped)
        self.assertEqual(len(result["output"]), 48000)
        self.assertTrue(result["truncated"])

    def test_cancel_running_shell_process_group(self):
        stopped = threading.Event()
        caught = []
        def run():
            try:
                self.tools.execute("shell", {"command": "sleep 30 & wait"}, stopped)
            except Exception as exc:
                caught.append(type(exc).__name__)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 2
        while self.tools.process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        stopped.set()
        self.tools.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(caught, ["Cancelled"])

    def test_restart_marks_incomplete_runs_interrupted_and_preserves_notes(self):
        run_id = self.store.create_run("Interrupted", 100, self.config.model)
        self.store.remember("Keep this")
        self.store.recover()
        self.assertEqual(self.store.run(run_id)["status"], "interrupted")
        self.assertEqual(self.store.memory(), "Keep this")

    def test_tool_arguments_and_keys_cannot_be_options(self):
        for action, args in (("click", {"x": 1}), ("screenshot", {"path": "/tmp/a"}),
                             ("hotkey", {"keys": ["--window"]}), ("unknown", {})):
            with self.assertRaises(ToolError):
                self.tools.execute(action, args, threading.Event())

    def test_config_generation_permissions_and_validation(self):
        path = Path(self.temp.name) / "config.toml"
        initialize_config(path)
        cfg = load_config(path)
        self.assertGreaterEqual(len(cfg.dashboard_token), 24)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        original = path.read_text()
        initialize_config(path)
        self.assertEqual(path.read_text(), original)
        path.write_text("unexpected_field = 1")
        with self.assertRaises(ValueError):
            load_config(path)
        with self.assertRaises(ValueError):
            dataclasses.replace(self.config, max_steps=True).validate()

    def test_decision_schema_validation(self):
        valid = decision("finish", {"summary": "done"})
        self.assertEqual(parse_decision(json.dumps(valid)), valid)
        self.assertEqual(parse_decision("```json\n" + json.dumps(valid) + "\n```"), valid)
        for invalid in ("[]", "not JSON", '{"action":"shell"}', json.dumps({**valid, "action": "eval"}),
                        json.dumps({**valid, "state": "x" * 3001}), json.dumps({**valid, "extra": True})):
            with self.assertRaises(ModelError):
                parse_decision(invalid)


if __name__ == "__main__":
    unittest.main()
