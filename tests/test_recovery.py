from __future__ import annotations

import json
import unittest

from treics_agent.recovery import RecoveryMemory
from treics_agent.model import compact_arguments, recent_observations


def action(name, arguments):
    return {"action": name, "arguments": arguments}


class RecoveryTests(unittest.TestCase):
    def test_failed_command_arguments_exit_status_and_repeat_count_are_kept(self):
        memory = RecoveryMemory()
        command = action("shell", {"command": "python3 parser.py", "cwd": "project", "timeout": 20})
        failure = {"ok": False, "exit_code": 1, "timed_out": False, "cwd": "/work/project", "output": "NameError: parser"}
        memory.observe(command, failure, 1)
        memory.observe(command, failure, 2)
        for step in range(3, 12):
            memory.observe(action("read_file", {"path": "report.md"}), {"ok": True, "text": "same"}, step)
        item = memory.snapshot()["failed_attempts"][0]
        self.assertEqual(item["arguments"], command["arguments"])
        self.assertEqual(item["failure_count"], 2)
        self.assertEqual(item["exit_code"], 1)
        self.assertEqual(item["error"], "NameError: parser")
        self.assertTrue(item["action_executed"])
        self.assertEqual(memory.path_events()[0]["data"]["decision"]["arguments"], {"cwd": "project"})

    def test_changed_preconditions_and_success_allow_a_corrected_retry(self):
        memory = RecoveryMemory()
        command = action("shell", {"command": "python3 parser.py"})
        memory.failure(command, 1, "SyntaxError")
        memory.observe(action("write_file", {"path": "parser.py", "content": "print('fixed')\n"}),
                       {"ok": True, "path": "parser.py"}, 2)
        item = memory.snapshot()["failed_attempts"][0]
        self.assertEqual(item["observed_change_after_failure_step"], 2)
        memory.observe(command, {"ok": True, "exit_code": 0, "output": "fixed"}, 3)
        self.assertEqual(memory.snapshot()["failed_attempts"], [])
        self.assertEqual(memory.path_events(), [])

    def test_changed_arguments_are_separate_and_memory_is_bounded_without_file_bodies(self):
        memory = RecoveryMemory()
        for step in range(20):
            memory.failure(action("write_file", {"path": str(step), "content": "SECRET FILE BODY" * 10000}), step, "Permission denied")
        context = memory.snapshot()
        self.assertLessEqual(len(memory.failures), 8)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 4000)
        self.assertNotIn("SECRET FILE BODY", json.dumps(context))
        self.assertEqual(len(memory.references), 8)
        memory.failure(action("shell", {"command": "python3 parser.py", "cwd": "one"}), 21, "missing")
        memory.failure(action("shell", {"command": "python3 parser.py", "cwd": "two"}), 22, "missing")
        latest = memory.snapshot()["failed_attempts"][:2]
        self.assertEqual([item["failure_count"] for item in latest], [1, 1])
        long = compact_arguments({"command": "python3 -c '" + "bad code;" * 1000 + "'"}, True)
        self.assertTrue(long["command_truncated"])
        self.assertLess(len(long["command_excerpt"]), 650)

    def test_repeated_successful_observations_are_advisory_and_actual_change_clears_them(self):
        memory = RecoveryMemory()
        command = action("shell", {"command": "cat report.txt"})
        for step in (1, 2, 3):
            memory.observe(command, {"ok": True, "exit_code": 0, "output": "same"}, step)
        self.assertEqual(memory.snapshot()["repeated_observation"]["matching_calls"], 3)
        memory.observe(command, {"ok": True, "exit_code": 0, "output": "changed"}, 4)
        self.assertNotIn("repeated_observation", memory.snapshot())
        frame = {"width": 10, "height": 10, "base64": "image"}
        click = action("click", {"x": 5, "y": 5})
        memory.observe(click, {"ok": True}, 5, frame, frame)
        memory.observe(click, {"ok": True}, 6, frame, frame)
        self.assertEqual(memory.snapshot()["repeated_observation"]["matching_calls"], 2)
        memory.observe(click, {"ok": True}, 7, frame, {**frame, "base64": "changed"})
        self.assertNotIn("repeated_observation", memory.snapshot())

    def test_rejected_arguments_stay_visible_without_becoming_executed_evidence(self):
        memory = RecoveryMemory()
        paste = action("type", {"text": "hello", "paste_keys": []})
        memory.failure(paste, 1, "paste_keys must not be empty", action_executed=False)
        self.assertFalse(memory.snapshot()["failed_attempts"][0]["action_executed"])
        observations = recent_observations([{"step": 1, "kind": "error", "data": {
            "decision": paste, "message": "paste_keys must not be empty", "action_executed": False}}])
        self.assertEqual(observations[0]["data"]["arguments"]["paste_keys"], [])
        self.assertFalse(observations[0]["data"]["action_executed"])
