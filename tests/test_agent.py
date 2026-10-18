from __future__ import annotations

import base64
import dataclasses
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image

from treics_agent.agent import Agent
from treics_agent.config import Config, initialize_config, load_config
from treics_agent.model import ARGUMENT_SCHEMAS, ModelError, decision_schema, parse_decision
from treics_agent.progress import ProgressGuard
from treics_agent.storage import Store
from treics_agent.tools import Desktop, ToolError, Tools


class FakeDesktop(Desktop):
    def __init__(self, config):
        super().__init__(config)
        self.captures = 0
        self.commands = []
        self.color = "#122037"
        self.marker = mock.Mock()
        self.marker.hidden.return_value = mock.MagicMock()
        self.pointer = (0, 0)

    def capture(self, region=None):
        self.captures += 1
        return super().capture(region)

    def command(self, args, **kwargs):
        self.commands.append(args)
        output = b""
        if args[0] == "scrot":
            Image.new("RGB", (1920, 1440), self.color).save(args[-1])
        elif args[:2] == ["xdotool", "mousemove"]:
            self.pointer = tuple(map(int, args[-2:]))
        elif args[:2] == ["xdotool", "getmouselocation"]:
            output = f"X={self.pointer[0]}\nY={self.pointer[1]}\nSCREEN=0\nWINDOW=0\n".encode()
        return subprocess.CompletedProcess(args, 0, stdout=output)


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
        self.budgets = []
        self.events = []
        self.states = []
        self.filesystems = []
        self.checklists = []
        self.recoveries = []
        self.click_review_images = []

    def decide(self, task, state, memory, events, screen, stop, budget=None, filesystem=None, checklist=None, recovery=None,
               click_review_image=None, on_chunk=None, on_thinking=None):
        self.screens.append(screen)
        self.budgets.append(budget)
        self.events.append(events)
        self.states.append(state)
        self.filesystems.append(filesystem)
        self.checklists.append(checklist)
        self.recoveries.append(recovery)
        self.click_review_images.append(click_review_image)
        self.calls += 1
        self.entered.set()
        self.release.wait(5)
        result = next(self.decisions)
        if isinstance(result, Exception):
            raise result
        return result, {}

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

    def test_logged_missing_cwd_then_mkdir_loop_skips_repeats_and_preserves_observed_state(self):
        self.config = dataclasses.replace(self.config, stall_attempts=3)
        target = self.config.work_path / "sales-test"
        setup = {"command": "mkdir -p sales-test", "cwd": str(self.config.work_path)}
        failed = decision("shell", {"command": "mkdir -p sales-test && touch sales.csv",
                                    "cwd": str(target)}, "Falsely claimed CSV was created")
        model = self.build([failed, decision("shell", setup, "Directory created; next write CSV"),
                            decision("shell", setup, "Falsely claimed CSV was written"),
                            decision("shell", setup, "Falsely claimed analysis was finished"),
                            decision("write_file", {"path": "sales-test/sales.csv", "content": "order_id\n1001\n"}),
                            decision("finish", {"summary": "Created CSV after resuming"})])
        with mock.patch.object(self.agent.stop_event, "wait", side_effect=lambda timeout: self.agent.stop_event.is_set()):
            run_id = self.agent.start("Reproduce uploaded directory loop", 20)
            self.wait_for_status(run_id, "paused")
            self.assertEqual(model.calls, 4)
            self.assertIn("3 matching shell", self.agent.snapshot()["pause_reason"])
            self.assertEqual(model.states[1], "")
            self.assertEqual(self.store.run(run_id)["state"], "Directory created; next write CSV")
            before = {p["path"]: p["kind"] for p in model.filesystems[1]["paths"]}
            after = {p["path"]: p["kind"] for p in model.filesystems[2]["paths"]}
            self.assertEqual(before[str(target)], "missing")
            self.assertEqual(after[str(target)], "directory")
            results = [e["data"]["result"] for e in self.store.events(run_id) if e["kind"] == "action"]
            self.assertTrue(results[0]["directories"][0]["directory"])
            self.assertTrue(all(r.get("skipped") for r in results[1:]))
            self.assertFalse((target / "sales.csv").exists())
            self.agent.resume()
            self.agent.thread.join(3)
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertEqual((target / "sales.csv").read_text(), "order_id\n1001\n")

    def test_directory_setup_feedback_skips_only_plain_mkdir_with_existing_targets(self):
        stop = threading.Event()
        args = {"command": "mkdir -p sales-test archive"}
        result = self.tools.execute("shell", args, stop)
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("skipped", False))
        self.assertEqual(result["output"], "")
        self.assertIn("completed successfully", result["message"])
        with mock.patch("treics_agent.tools.subprocess.Popen") as popen:
            repeated = self.tools.execute("shell", args, stop)
            popen.assert_not_called()
        self.assertTrue(repeated["skipped"])
        self.assertEqual(repeated["directories"], result["directories"])
        # Compound commands, quoted paths, shell expansions, flags and newlines
        # must retain normal Bash semantics, even when a directory exists.
        for command in ("mkdir -p sales-test && printf done", "mkdir -p 'sales-test'",
                        "mkdir -p $HOME/sales-test", "mkdir -m 700 -p sales-test",
                        "mkdir -p sales-test\nprintf done", "mkdir -p ~/x; printf done",
                        "mkdir -p sales-test\varchive", "mkdir -p sales-test\u00a0archive"):
            self.assertEqual(Tools.directory_setup_paths(command, self.config.work_path), [])
        combined = self.tools.execute("shell", {"command": "mkdir -p sales-test && printf done"}, stop)
        self.assertFalse(combined.get("skipped", False))
        self.assertEqual(combined["output"], "done")
        parsed = Tools.directory_setup_paths("mkdir -p -- ~/dir /tmp/dir relative", self.config.work_path)
        self.assertEqual(parsed, [Path.home() / "dir", Path("/tmp/dir"), self.config.work_path / "relative"])
        (self.config.work_path / "ordinary-file").write_text("preserve me")
        result = self.tools.execute("shell", {"command": "mkdir -p ordinary-file"}, stop)
        self.assertFalse(result["ok"])
        self.assertFalse(result["directories"][0]["directory"])
        self.assertEqual((self.config.work_path / "ordinary-file").read_text(), "preserve me")

    def test_missing_cwd_does_not_create_it_and_reports_command_was_not_started(self):
        path = self.config.work_path / "missing"
        with self.assertRaisesRegex(ToolError, "No command was started"):
            self.tools.execute("shell", {"command": "mkdir -p missing", "cwd": str(path)}, threading.Event())
        self.assertFalse(path.exists())

    def test_identical_file_write_is_skipped_without_changing_mtime_and_still_pauses_loop(self):
        self.config = dataclasses.replace(self.config, stall_attempts=3)
        args = {"path": "new/nested/script.py", "content": "print('你好')\n"}
        first = self.tools.execute("write_file", args, threading.Event())
        self.assertTrue(first["ok"])
        path = self.config.work_path / args["path"]
        before = path.stat().st_mtime_ns
        with mock.patch.object(Path, "write_text", side_effect=AssertionError("unexpected rewrite")):
            repeated = self.tools.execute("write_file", args, threading.Event())
        self.assertTrue(repeated["skipped"])
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(path.read_text(), args["content"])
        model = self.build([decision("write_file", args, "Unsupported new progress")] * 3 +
                           [decision("finish", {"summary": "Blocked"})])
        run_id = self.agent.start("Identical writes", 6)
        self.wait_for_status(run_id, "paused")
        self.assertEqual(self.store.run(run_id)["state"], "")
        self.assertEqual(model.calls, 3)
        self.assertIn("3 matching write_file", self.agent.snapshot()["pause_reason"])
        self.agent.resume()
        self.agent.thread.join(3)
        changed = {**args, "content": "print('再见')\n"}
        result = self.tools.execute("write_file", changed, threading.Event())
        self.assertFalse(result.get("skipped", False))
        self.assertEqual(path.read_text(), changed["content"])

    def test_filesystem_observations_are_bounded_and_never_read_file_contents(self):
        names = [f"file-{i}-" + "a" * 210 + ".txt" for i in range(25)]
        for name in names:
            (self.config.work_path / name).write_text("contents must not enter the context")
        events = [{"data": {"decision": decision("read_file", {"path": name})}} for name in names]
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("read contents")):
            context = self.tools.filesystem_context(events)
        self.assertLessEqual(len(context["paths"]), 12)
        self.assertLessEqual(len(context["workspace_entries"]), 20)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 3000)
        self.assertTrue(context["entries_truncated"])
        self.assertNotIn("contents must not enter the context", json.dumps(context))

    def test_write_file_can_replace_a_writable_file_that_cannot_be_read_for_comparison(self):
        path = self.config.work_path / "write-only.txt"
        path.write_text("old data")
        original_open = Path.open
        def deny_comparison_open(path, mode="r", *args, **kwargs):
            if mode == "rb":
                raise PermissionError("File is not readable")
            return original_open(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", deny_comparison_open):
            result = self.tools.execute("write_file", {"path": str(path), "content": "new data"}, threading.Event())
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("skipped", False))
        self.assertEqual(path.read_text(), "new data")

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
        self.assertEqual(self.desktop.captures, 4)
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

    def wait_for_status(self, run_id, status):
        deadline = time.monotonic() + 3
        while self.store.run(run_id)["status"] != status and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.store.run(run_id)["status"], status)

    def test_model_receives_decreasing_step_budget_and_can_finish_early(self):
        model = self.build([decision("shell", {"command": "printf ready"}),
                            decision("finish", {"summary": "Done early"})])
        run = self.complete(max_steps=100)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["step"], 2)
        self.assertEqual(model.budgets, [
            {"current_step": 1, "max_steps": 100, "steps_remaining": 100},
            {"current_step": 2, "max_steps": 100, "steps_remaining": 99}])

    def test_stalled_screenshots_pause_and_resume_with_fresh_observation(self):
        self.config = dataclasses.replace(self.config, stall_attempts=3)
        model = self.build([decision("screenshot", {}, "Claimed progress " + str(i)) for i in range(3)] +
                           [decision("write_file", {"path": "after-loop", "content": "ok"}),
                            decision("finish", {"summary": "Recovered"})])
        run_id = self.agent.start("Loop test", 100)
        self.wait_for_status(run_id, "paused")
        self.assertEqual(model.calls, 3)
        self.assertFalse((self.config.work_path / "after-loop").exists())
        self.assertIn("3 matching screenshot", self.agent.snapshot()["pause_reason"])
        events = self.store.events(run_id)
        self.assertEqual(events[-1]["kind"], "stalled")
        # Inference stops while paused, even though every screenshot tool succeeded.
        time.sleep(0.05)
        self.assertEqual(model.calls, 3)
        old_image = model.screens[-1]["base64"]
        self.desktop.color = "#bbaadd"
        self.agent.resume()
        self.agent.thread.join(3)
        self.assertFalse(self.agent.thread.is_alive())
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertIsNone(self.agent.snapshot()["pause_reason"])
        self.assertNotEqual(model.screens[3]["base64"], old_image)
        self.assertTrue(any(e["kind"] == "resumed" for e in model.events[3]))
        self.assertEqual(model.budgets[3]["steps_remaining"], 97)
        self.assertEqual((self.config.work_path / "after-loop").read_text(), "ok")

    def test_stop_while_automatically_paused_prevents_next_action(self):
        self.config = dataclasses.replace(self.config, stall_attempts=3)
        model = self.build([decision("screenshot", {})] * 3 +
                           [decision("write_file", {"path": "forbidden", "content": "bad"})])
        run_id = self.agent.start("Stop loop")
        self.wait_for_status(run_id, "paused")
        self.agent.stop()
        self.agent.thread.join(3)
        self.assertFalse(self.agent.thread.is_alive())
        self.assertEqual(self.store.run(run_id)["status"], "stopped")
        self.assertEqual(model.calls, 3)
        self.assertFalse((self.config.work_path / "forbidden").exists())

    def test_interleaved_typing_errors_pause_without_recording_failed_state(self):
        self.config = dataclasses.replace(self.config, stall_attempts=3)
        # Reproduce the uploaded log: focus, failed paste, focus, failed paste...
        focus = decision("hotkey", {"keys": ["ctrl", "l"]}, "Address bar focused")
        paste = decision("type", {"text": "https://example.com", "paste_keys": []}, "URL was typed")
        model = self.build([focus, paste] * 3 + [decision("finish", {"summary": "Blocked"})])
        # Skip tool/error delays; the condition-based pause gate remains real.
        with mock.patch.object(self.agent.stop_event, "wait", side_effect=lambda timeout: self.agent.stop_event.is_set()):
            run_id = self.agent.start("Reproduce typing loop", 15)
            self.wait_for_status(run_id, "paused")
            self.assertEqual(model.calls, 6)
            self.assertEqual(self.store.run(run_id)["state"], "")  # Sending focus keys has not verified focus.
            self.assertIn("3 repeated type errors", self.agent.snapshot()["pause_reason"])
            errors = [e for e in self.store.events(run_id) if e["kind"] == "error"]
            self.assertEqual(len(errors), 3)
            self.assertEqual(errors[-1]["data"]["decision"]["action"], "type")
            self.assertIn("type failed:", errors[-1]["data"]["message"])
            self.agent.resume()
            self.agent.thread.join(3)
            self.assertEqual(self.store.run(run_id)["status"], "completed")

    def test_typing_uses_default_keys_and_rejects_empty_keys_before_clipboard(self):
        with mock.patch("treics_agent.tools.subprocess.run") as run:
            with self.assertRaises(ToolError):
                self.tools.execute("type", {"text": "https://example.com", "paste_keys": []}, threading.Event())
            run.assert_not_called()
            result = self.tools.execute("type", {"text": "https://example.com"}, threading.Event())
            self.assertTrue(result["ok"])
            self.assertEqual(run.call_args.kwargs["input"], b"https://example.com")
            self.assertIn(["xdotool", "key", "--clearmodifiers", "ctrl+v"], self.desktop.commands)

    def test_step_limit_still_ends_run_when_last_action_would_trigger_pause(self):
        self.config = dataclasses.replace(self.config, stall_attempts=3)
        model = self.build([decision("screenshot", {})] * 3)
        run = self.complete(max_steps=3)
        self.assertEqual(run["status"], "limit_reached")
        self.assertEqual(model.budgets[-1]["steps_remaining"], 1)

    def test_manual_default_allows_repeated_screenshots_until_finish(self):
        model = self.build([decision("screenshot", {})] * 7 + [decision("finish", {"summary": "Done"})])
        run = self.complete(max_steps=20)
        self.assertEqual(self.config.stall_attempts, 0)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(model.calls, 8)
        self.assertFalse(any(event["kind"] == "stalled" for event in self.store.events(run["id"])))

    def test_manual_mode_retries_more_than_five_model_failures(self):
        model = self.build([ModelError("Output limit reached")] * 7 + [decision("finish", {"summary": "Recovered"})])
        with mock.patch.object(self.agent.stop_event, "wait", side_effect=lambda timeout: self.agent.stop_event.is_set()):
            run = self.complete(max_steps=20)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(model.calls, 8)
        self.assertEqual(len([event for event in self.store.events(run["id"]) if event["kind"] == "error"]), 7)
        self.assertEqual(model.recoveries[-1]["failed_attempts"][0]["failure_count"], 7)

    def test_manual_click_loop_reports_repeated_target_and_does_not_save_claimed_gui_success(self):
        self.config = dataclasses.replace(self.config, history_steps=1)
        click = decision("click", {"x": 30, "y": 40}, "Falsely claimed window closed")
        model = self.build([
            decision("write_file", {"path": "note.txt", "content": "baseline"}, "Note file created"),
            decision("screenshot", {}, "Observed desktop"), click,
            decision("screenshot", {}, "Observed unchanged desktop"), click,
            decision("wait", {"seconds": 0.1}, "Observed unchanged desktop"), click,
            decision("finish", {"summary": "The intended GUI effect is unverified"}),
        ])
        run = self.complete(max_steps=20)
        self.assertEqual(run["status"], "completed")
        self.assertNotIn("Falsely claimed window closed", model.states)
        self.assertEqual(model.states[3], "Observed desktop")
        self.assertEqual(model.states[5], "Observed unchanged desktop")
        feedback = model.recoveries[-1]["gui_feedback"]
        self.assertEqual(feedback["matching_unchanged_attempts"], 3)
        self.assertFalse(feedback["ui_effect_verified"])
        self.assertEqual((feedback["desktop_target"]["x"], feedback["desktop_target"]["y"]), (56, 75))
        events = self.store.events(run["id"])
        clicks = [event for event in events if event["kind"] == "action" and event["data"]["decision"]["action"] == "click"]
        self.assertEqual(len(clicks), 3)  # Feedback does not block the user's manual retry policy.
        for event in clicks:
            self.assertTrue(event["data"]["result"]["input_sent"])
            self.assertFalse(event["data"]["result"]["ui_effect_verified"])
            self.assertFalse(event["data"]["result"]["state_saved"])
        self.assertFalse(any(event["kind"] == "stalled" for event in events))

    def test_recovery_survives_short_history_and_refreshes_failed_cwd_metadata(self):
        self.config = dataclasses.replace(self.config, history_steps=1)
        failing = decision("shell", {"command": "python3 script.py", "cwd": "new-project"})
        model = self.build([failing, failing,
            decision("write_file", {"path": "new-project/script.py", "content": "print('fixed')\n"})] +
            [decision("read_file", {"path": "new-project/script.py"})] * 7 +
            [failing, decision("finish", {"summary": "Done"})])
        with mock.patch.object(self.agent.stop_event, "wait", side_effect=lambda timeout: self.agent.stop_event.is_set()):
            run = self.complete(max_steps=20)
        self.assertEqual(run["status"], "completed")
        context = model.recoveries[10]
        self.assertEqual(context["failed_attempts"][0]["failure_count"], 2)
        self.assertEqual(context["failed_attempts"][0]["arguments"]["command"], "python3 script.py")
        self.assertEqual(context["failed_attempts"][0]["observed_change_after_failure_step"], 3)
        self.assertTrue(all(event["kind"] != "error" for event in model.events[10]))
        paths = {item["path"]: item["kind"] for item in model.filesystems[10]["paths"]}
        self.assertEqual(paths[str(self.config.work_path / "new-project")], "directory")
        self.assertEqual(model.recoveries[-1]["failed_attempts"], [])
        finished = self.store.events(run["id"])[-1]
        self.assertIn("recovery_context", finished["data"])

    def test_custom_error_threshold_can_exceed_old_five_failure_cutoff(self):
        self.config = dataclasses.replace(self.config, stall_attempts=7)
        model = self.build([ModelError("Output limit reached")] * 7 + [decision("finish", {"summary": "Recovered"})])
        with mock.patch.object(self.agent.stop_event, "wait", side_effect=lambda timeout: self.agent.stop_event.is_set()):
            run_id = self.agent.start("Choose my retry count", 20)
            self.wait_for_status(run_id, "paused")
            self.assertEqual(model.calls, 7)
            self.assertIn("7 repeated model errors", self.agent.snapshot()["pause_reason"])
            self.agent.resume()
            self.agent.thread.join(3)
        self.assertEqual(self.store.run(run_id)["status"], "completed")

    def test_guard_supports_thresholds_over_twelve_and_policy_changes_clear_evidence(self):
        guard = ProgressGuard(17)
        cmd = decision("shell", {"command": "printf ready"})
        for _ in range(16):
            self.assertIsNone(guard.observe(cmd, {"ok": True, "output": "ready"}, None, None))
        self.assertIn("17 matching shell", guard.observe(cmd, {"ok": True, "output": "ready"}, None, None))
        guard.configure(0)
        with mock.patch.object(guard, "same_screen", side_effect=AssertionError("Manual mode decoded screenshots")):
            for _ in range(20):
                self.assertIsNone(guard.observe(decision("screenshot", {}), {"ok": True}, {}, {}))
                self.assertIsNone(guard.error(None, "Repeated error"))
        guard.configure(2)
        self.assertIsNone(guard.error(None, "Repeated error"))
        self.assertIn("2 repeated model errors", guard.error(None, "Repeated error"))

    def test_loop_guard_detects_cycles_but_resets_on_observed_progress(self):
        guard = ProgressGuard()
        screen = self.desktop.capture()
        result = {"ok": True}
        click = decision("click", {"x": 30, "y": 40})
        screenshot = decision("screenshot", {})
        for action in (click, screenshot, click, screenshot):
            self.assertIsNone(guard.observe(action, result, screen, screen))
        self.assertIn("3 matching click", guard.observe(click, result, screen, screen))
        self.desktop.color = "#8899ff"
        changed = self.desktop.capture()
        self.assertIsNone(guard.observe(click, result, screen, changed))
        self.assertIsNone(guard.observe(click, result, changed, changed))
        self.assertIsNone(guard.observe(click, result, changed, changed))
        # A deliberate text/file step breaks the unchanged desktop sequence.
        guard.observe(decision("write_file", {"path": "x", "content": "y"}), result, None, None)
        self.assertIsNone(guard.observe(click, result, changed, changed))

    def test_loop_guard_ignores_tiny_cursor_changes_and_detects_changed_output(self):
        guard = ProgressGuard()
        screen = self.desktop.capture()
        image = Image.open(io.BytesIO(base64.b64decode(screen["base64"])))
        image.paste("white", (100, 100, 102, 102))
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG")
        changed = {**screen, "captured_at": time.time(), "base64": base64.b64encode(encoded.getvalue()).decode()}
        self.assertTrue(guard.same_screen(screen, changed))
        cmd = decision("shell", {"command": "printf ready"})
        for output in ("ready", "ready", "different", "different"):
            self.assertIsNone(guard.observe(cmd, {"ok": True, "output": output}, None, None))
        self.assertIn("same result", guard.observe(cmd, {"ok": True, "output": "different"}, None, None))
        guard.reset()
        self.assertIsNone(guard.observe(cmd, {"ok": True, "output": "different"}, None, None))

    def test_shell_timeout_and_output_limit(self):
        stopped = threading.Event()
        result = self.tools.execute("shell", {"command": "printf begin; sleep 5", "timeout": 1}, stopped)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["ok"])
        result = self.tools.execute("shell", {"command": "python3 -c 'print(\"x\"*100000)'"}, stopped)
        self.assertEqual(len(result["output"]), 48000)
        self.assertTrue(result["truncated"])

    def test_shell_pipeline_does_not_hide_missing_heading(self):
        # The run log's grep/sed pipeline falsely reported success on no match.
        self.tools.execute("write_file", {"path": "page.html", "content": '<h1 class="title">Example</h1>'},
                           threading.Event())
        result = self.tools.execute("shell", {"command": "grep -o '<h1>[^<]*</h1>' page.html | sed 's/<[^>]*>//g'"},
                                    threading.Event())
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["output"], "")
        result = self.tools.execute("shell", {"command": "printf 'Example\\n' | sed 's/Example/Verified/'"},
                                    threading.Event())
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "Verified\n")

    def test_failed_pipeline_does_not_save_claimed_success(self):
        model = self.build([decision("shell", {"command": "false | cat"}, "Claimed extraction succeeded"),
                    decision("finish", {"summary": "Reported missing output"}, "Extraction was blocked")])
        run = self.complete(max_steps=3)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["state"], "Extraction was blocked")
        self.assertEqual(model.states[1], "")
        result = next(e["data"]["result"] for e in self.store.events(run["id"]) if e["kind"] == "action")
        self.assertFalse(result["ok"])

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

    def test_action_schema_rejects_the_logged_empty_keys_and_malformed_coordinates(self):
        invalid = [decision("type", {"text": "https://example.com", "paste_keys": []}),
                   decision("hotkey", {"keys": "ctrl+l"}),
                   decision("click", {"x": [624, 668]}),
                   decision("click", {"x": 10, "y": True}),
                   decision("screenshot", {"text": "invented"})]
        for obj in invalid:
            with self.assertRaises(ModelError):
                parse_decision(json.dumps(obj))
        for obj in (decision("type", {"text": "hello"}),
                    decision("type", {"text": "hello", "paste_keys": ["ctrl", "shift", "v"]}),
                    decision("shell", {"command": "curl -fsSL https://example.com", "timeout": 60})):
            self.assertEqual(parse_decision(json.dumps(obj)), obj)
        schema = decision_schema({"width": 1024, "height": 768})
        cases = {c["properties"]["action"]["enum"][0]: c for c in schema["anyOf"]}
        self.assertEqual(cases["click"]["properties"]["arguments"]["properties"]["y"]["maximum"], 767)
        self.assertEqual(cases["type"]["properties"]["arguments"]["properties"]["paste_keys"]["minItems"], 1)
        # Keep generation/validation fields aligned with the actual tool interface.
        for action, (required, optional) in Tools.ARGS.items():
            self.assertEqual(set(ARGUMENT_SCHEMAS[action]["required"]), required)
            self.assertEqual(set(ARGUMENT_SCHEMAS[action]["properties"]), required | optional)


if __name__ == "__main__":
    unittest.main()
