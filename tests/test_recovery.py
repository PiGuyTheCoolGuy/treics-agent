from __future__ import annotations

import json
import base64
import io
import unittest

from PIL import Image

from treics_agent.recovery import RecoveryMemory
from treics_agent.model import compact_arguments, recent_observations


def action(name, arguments):
    return {"action": name, "arguments": arguments}


def frame(color="#122037", blink=False):
    image = Image.new("RGB", (512, 384), color)
    if blink:
        image.paste("white", (100, 100, 102, 102))
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return {"width": 512, "height": 384, "desktop_width": 1024, "desktop_height": 768,
            "source": {"x": 0, "y": 0, "width": 1024, "height": 768}, "view": "desktop",
            "base64": base64.b64encode(output.getvalue()).decode()}


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

    def test_click_repetition_survives_screenshots_waits_and_minor_image_changes(self):
        memory = RecoveryMemory()
        original, blinking = frame(), frame(blink=True)
        click = action("click", {"x": 30, "y": 40})
        memory.observe(click, {"ok": True}, 1, original, blinking)
        memory.observe(action("screenshot", {}), {"ok": True}, 2, blinking, original)
        memory.observe(action("wait", {"seconds": 1}), {"ok": True}, 3, original, blinking)
        memory.observe(click, {"ok": True}, 4, blinking, original)
        context = memory.snapshot()
        self.assertEqual(context["gui_feedback"]["screen_comparison"], "essentially_unchanged")
        self.assertEqual(context["gui_feedback"]["matching_unchanged_attempts"], 2)
        self.assertEqual(context["repeated_observation"]["matching_calls"], 2)
        self.assertIn("Do not adjust coordinates blindly", context["gui_feedback"]["guidance"])
        self.assertFalse(context["gui_feedback"]["ui_effect_verified"])
        memory.observe(action("screenshot", {}), {"ok": True}, 5, original, blinking)
        self.assertEqual(memory.snapshot()["gui_feedback"]["matching_unchanged_attempts"], 2)

    def test_matching_input_uses_executed_target_and_keys_not_proposal_format(self):
        memory = RecoveryMemory()
        screen = frame()
        result = {"ok": True, "last_click": {"x": 60, "y": 80, "button": "left", "clicks": 1}}
        memory.observe(action("click", {"x": 30, "y": 40}), result, 1, screen, screen)
        memory.observe(action("click", {"x": 31, "y": 40}), result, 2, screen, screen)
        feedback = memory.snapshot()["gui_feedback"]
        self.assertEqual(feedback["matching_unchanged_attempts"], 2)
        self.assertEqual(feedback["desktop_target"], result["last_click"])
        memory.observe(action("hotkey", {"keys": ["alt", "f4"]}), {"ok": True, "keys_sent": "alt+F4"}, 3, screen, screen)
        memory.observe(action("hotkey", {"keys": ["ALT", "F4"]}), {"ok": True, "keys_sent": "alt+F4"}, 4, screen, screen)
        self.assertEqual(memory.snapshot()["gui_feedback"]["matching_unchanged_attempts"], 2)

    def test_observed_change_clears_click_repetition_without_claiming_goal_success(self):
        memory = RecoveryMemory()
        before, changed = frame(), frame("white")
        click = action("click", {"x": 30, "y": 40})
        memory.observe(click, {"ok": True}, 1, before, before)
        memory.observe(click, {"ok": True}, 2, before, before)
        memory.observe(action("wait", {"seconds": 1}), {"ok": True}, 3, before, changed)
        self.assertNotIn("gui_feedback", memory.snapshot())
        memory.observe(click, {"ok": True}, 4, changed, changed)
        self.assertEqual(memory.snapshot()["gui_feedback"]["matching_unchanged_attempts"], 1)
        memory.observe(click, {"ok": True}, 5, changed, before)
        feedback = memory.snapshot()["gui_feedback"]
        self.assertEqual(feedback["screen_comparison"], "changed")
        self.assertEqual(feedback["matching_unchanged_attempts"], 0)
        self.assertFalse(feedback["ui_effect_verified"])
        self.assertNotIn("repeated_observation", memory.snapshot())

    def test_missing_or_cropped_images_are_not_claimed_to_be_unchanged(self):
        memory = RecoveryMemory()
        before = frame()
        click = action("click", {"x": 30, "y": 40})
        cropped = {**before, "view": "zoom", "source": {"x": 20, "y": 30, "width": 100, "height": 80}}
        memory.observe(click, {"ok": True}, 1, cropped, before)
        self.assertEqual(memory.snapshot()["gui_feedback"]["screen_comparison"], "view_changed")
        memory.observe(click, {"ok": True}, 2, None, before)
        self.assertEqual(memory.snapshot()["gui_feedback"]["screen_comparison"], "unavailable")
        self.assertEqual(memory.snapshot()["gui_feedback"]["matching_unchanged_attempts"], 0)

    def test_repeated_zoomed_clicks_compare_full_desktop_baseline_and_actual_target(self):
        memory = RecoveryMemory()
        full = frame()
        cropped = {**full, "view": "zoom", "width": 100, "height": 100,
                   "source": {"x": 20, "y": 30, "width": 100, "height": 100}}
        zoom = action("zoom_screenshot", {"x": 20, "y": 30, "width": 50, "height": 50})
        click = action("click", {"x": 5, "y": 5})
        result = {"ok": True, "last_click": {"x": 25, "y": 35, "button": "left", "clicks": 1}}
        for step in (1, 3):
            memory.observe(zoom, {"ok": True}, step, full, cropped)
            memory.observe(click, result, step + 1, cropped, full)
        context = memory.snapshot()
        self.assertEqual(context["gui_feedback"]["screen_comparison"], "essentially_unchanged")
        self.assertEqual(context["gui_feedback"]["matching_unchanged_attempts"], 2)
        self.assertEqual(context["gui_feedback"]["desktop_target"], result["last_click"])

    def test_gui_feedback_stays_bounded_and_is_not_erased_by_a_model_timeout(self):
        memory = RecoveryMemory()
        screen = frame()
        click = action("click", {"x": 30, "y": 40})
        for step in range(30):
            memory.observe(click, {"ok": True}, step, screen, screen)
        memory.failure(None, 30, "Model request timed out")
        context = memory.snapshot()
        self.assertEqual(context["gui_feedback"]["matching_unchanged_attempts"], 12)
        self.assertEqual(context["gui_feedback"]["input_window"], 12)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), memory.MAX_CONTEXT)
        self.assertNotIn("base64", json.dumps(context))

    def test_new_desktop_before_retry_resets_previous_gui_attempts(self):
        memory = RecoveryMemory()
        old, new = frame(), frame("white")
        click = action("click", {"x": 30, "y": 40})
        memory.observe(click, {"ok": True}, 1, old, old)
        memory.observe(click, {"ok": True}, 2, old, old)
        # A fresh scene after pause/manual intervention is a changed precondition.
        memory.observe(click, {"ok": True}, 3, new, new)
        self.assertEqual(memory.snapshot()["gui_feedback"]["matching_unchanged_attempts"], 1)
