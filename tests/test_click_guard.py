from __future__ import annotations

import dataclasses
import json
import threading
import unittest
from unittest import mock

from treics_agent.click_guard import ClickGuard
import test_agent
from test_agent import decision
from test_recovery import frame


class ClickGuardTests(unittest.TestCase):
    def test_limit_is_enforced_per_target_and_returned_scene_with_blink_tolerance(self):
        guard = ClickGuard(2)
        target = (30, 40, "left")
        original, blinking, popup = frame(), frame(blink=True), frame("white")
        for screen in (original, blinking):
            self.assertIsNone(guard.assess(target, screen, 1))
            guard.record(target, screen)
        blocked = guard.assess(target, original, 3)
        self.assertEqual(blocked["reason"], "repeat_limit")
        self.assertEqual(blocked["matching_attempts"], 2)
        self.assertIsNone(guard.assess((100, 40, "left"), original, 3))
        self.assertIsNone(guard.assess(target, popup, 4))
        guard.record(target, popup)
        self.assertEqual(guard.assess(target, original, 5)["matching_attempts"], 2)
        self.assertNotIn("base64", json.dumps(guard.snapshot()))

    def test_policy_change_reset_and_disable_keep_limits_user_controlled(self):
        guard = ClickGuard(1)
        target, screen = (30, 40, "left"), frame()
        guard.record(target, screen)
        self.assertIsNotNone(guard.assess(target, screen, 2))
        self.assertTrue(guard.snapshot()["request_alternative"])
        guard.observe_action("screenshot")
        self.assertFalse(guard.snapshot()["request_alternative"])
        self.assertIsNotNone(guard.assess(target, screen, 3), "Observation erased the executed click count")
        guard.configure(4, 1)
        self.assertIsNone(guard.assess(target, screen, 4))
        guard.configure(0, 2)
        for _ in range(50):
            guard.record(target, screen)
            self.assertIsNone(guard.assess(target, screen, 5))
        self.assertFalse(guard.snapshot()["enabled"])
        self.assertEqual(len(guard.scenes), 0)

    def test_scene_and_target_memory_are_bounded(self):
        guard = ClickGuard(1000)
        for index in range(30):
            color = (index * 53 % 256, index * 97 % 256, index * 131 % 256)
            guard.record((index, 0, "left"), frame(color))
        self.assertLessEqual(len(guard.scenes), guard.MAX_SCENES)
        screen = frame()
        for index in range(100):
            guard.record((index, 0, "left"), screen)
        self.assertTrue(all(len(scene["targets"]) <= guard.MAX_TARGETS for scene in guard.scenes))


class ClickExecutionTests(unittest.TestCase):
    setUp = test_agent.AgentTests.setUp
    tearDown = test_agent.AgentTests.tearDown
    build = test_agent.AgentTests.build
    complete = test_agent.AgentTests.complete
    def test_guard_withholds_repeated_clicks_without_pausing_and_permits_another_target(self):
        self.config = dataclasses.replace(self.config, history_steps=1)
        click = decision("click", {"x": 30, "y": 40})
        model = self.build([click, decision("screenshot", {}), click, decision("screenshot", {}), click,
                            decision("screenshot", {}), click, decision("screenshot", {}),
                            decision("click", {"x": 100, "y": 100}), decision("finish", {"summary": "Changed target"})])
        run = self.complete(max_steps=20)
        self.assertEqual(run["status"], "completed")
        events = self.store.events(run["id"])
        blocked = [event for event in events if event["kind"] == "blocked"]
        self.assertEqual(len(blocked), 2)
        self.assertTrue(all(event["data"]["action_executed"] is False for event in blocked))
        self.assertTrue(all(event["data"]["result"]["input_sent"] is False for event in blocked))
        clicks = [command for command in self.desktop.commands if command[:2] == ["xdotool", "click"]]
        self.assertEqual(len(clicks), 3, "Withheld attempts reached the mouse tool")
        self.assertFalse(any(event["kind"] == "stalled" for event in events))
        self.assertTrue(model.recoveries[5]["click_guard"]["request_alternative"])
        for event in blocked:
            self.assertIsNone(self.store.evidence(run["id"], event["id"]))

    def test_guard_blocks_changed_desktop_after_inference_before_mouse_input(self):
        class ChangingModel:
            def __init__(inner):
                inner.calls = 0
            def decide(inner, *args, **kwargs):
                inner.calls += 1
                if inner.calls == 1:
                    self.desktop.color = "white"
                    return decision("click", {"x": 30, "y": 40}), {}
                self.assertEqual(kwargs["recovery"]["click_guard"]["last_blocked"]["reason"], "stale_image")
                return decision("finish", {"summary": "Observed fresh desktop"}), {}
            def cancel(inner):
                pass
        self.build([])
        self.agent.model = ChangingModel()
        run = self.complete()
        self.assertEqual(run["status"], "completed")
        self.assertFalse(any(command[0] == "xdotool" for command in self.desktop.commands))
        self.assertEqual(self.agent.snapshot()["last_click_block"]["reason"], "stale_image")

    def test_disabled_guard_preserves_intentional_repeated_clicks(self):
        self.config = dataclasses.replace(self.config, click_repeat_limit=0)
        self.build([decision("click", {"x": 30, "y": 40})] * 5 + [decision("finish", {"summary": "Done"})])
        run = self.complete()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len([command for command in self.desktop.commands if command[:2] == ["xdotool", "click"]]), 5)
        self.assertFalse(any(event["kind"] == "blocked" for event in self.store.events(run["id"])))

    def test_stop_during_preflight_capture_sends_no_click(self):
        self.build([decision("click", {"x": 30, "y": 40})])
        capture = self.desktop.capture
        def stop_on_refresh(region=None):
            captured = capture(region)
            if self.desktop.captures == 2:
                self.agent.stop()
            return captured
        with mock.patch.object(self.desktop, "capture", side_effect=stop_on_refresh):
            run = self.complete()
        self.assertEqual(run["status"], "stopped")
        self.assertFalse(any(command[0] == "xdotool" for command in self.desktop.commands))

    def test_popup_toggle_loop_is_withheld_when_the_scene_returns(self):
        click = decision("click", {"x": 30, "y": 40})
        self.build([click] * 5 + [decision("finish", {"summary": "Stopped toggling"})])
        command = self.desktop.command
        def toggle(args, **kwargs):
            result = command(args, **kwargs)
            if args[:2] == ["xdotool", "click"]:
                self.desktop.color = "white" if self.desktop.color == "#122037" else "#122037"
            return result
        with mock.patch.object(self.desktop, "command", side_effect=toggle):
            run = self.complete()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len([cmd for cmd in self.desktop.commands if cmd[:2] == ["xdotool", "click"]]), 4)
        blocked = [event for event in self.store.events(run["id"]) if event["kind"] == "blocked"]
        self.assertEqual(blocked[0]["data"]["result"]["matching_attempts"], 2)

    def test_disabling_guard_during_preflight_honors_current_policy(self):
        self.config = dataclasses.replace(self.config, click_repeat_limit=1)
        self.build([decision("click", {"x": 30, "y": 40})] * 2 + [decision("finish", {"summary": "Done"})])
        entered, release = threading.Event(), threading.Event()
        capture = self.desktop.capture
        def hold_second_click(region=None):
            result = capture(region)
            if self.desktop.captures == 4:
                entered.set()
                release.wait(3)
            return result
        with mock.patch.object(self.desktop, "capture", side_effect=hold_second_click):
            run_id = self.agent.start("Disable while a repeat is being checked")
            try:
                self.assertTrue(entered.wait(2))
                with self.agent.lock:
                    self.agent.config = dataclasses.replace(self.agent.config, click_repeat_limit=0)
                    self.agent.click_policy_version += 1
            finally:
                release.set()
                self.agent.thread.join(5)
        self.assertFalse(self.agent.thread.is_alive())
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertEqual(len([cmd for cmd in self.desktop.commands if cmd[:2] == ["xdotool", "click"]]), 2)
        self.assertFalse(any(event["kind"] == "blocked" for event in self.store.events(run_id)))


