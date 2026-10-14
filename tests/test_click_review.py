from __future__ import annotations

import base64
import io
import json
import threading
import unittest
from unittest import mock

from PIL import Image

from treics_agent.click_review import comparison_image, image_point, inspection_region, marked_image
from treics_agent.config import Config
from treics_agent.model import ModelClient, ModelError, decision_schema, parse_decision
from treics_agent.recovery import RecoveryMemory

import test_agent


def frame(color, source=None, size=(400, 300), captured_at=1):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG")
    return {"width": size[0], "height": size[1], "desktop_width": 800, "desktop_height": 600,
            "view": "zoom" if source else "desktop",
            "source": source or {"x": 0, "y": 0, "width": 800, "height": 600},
            "base64": base64.b64encode(output.getvalue()).decode(), "captured_at": captured_at}


ASSESSMENT = {"intended_effect": "not_observed", "evidence": "The window is still open; the account menu covers its title bar.",
              "target_at_click": "The agent account menu", "cause": "Clicked the panel instead of the window close control",
              "next_step": "Dismiss the menu with Escape and inspect the window title bar."}


class ClickReviewTests(unittest.TestCase):
    def test_inspection_region_is_native_and_remains_inside_the_desktop_at_edges(self):
        screen = {"desktop_width": 1024, "desktop_height": 768}
        region = inspection_region(screen, {"x": 504, "y": 565})
        self.assertEqual({key: region[key] for key in ("x", "y", "width", "height")},
                         {"x": 248, "y": 373, "width": 512, "height": 384})
        for x, y in ((0, 0), (1023, 767)):
            region = inspection_region(screen, {"x": x, "y": y})
            self.assertLessEqual(region["x"], x)
            self.assertLess(x, region["x"] + region["width"])
            self.assertLessEqual(region["y"], y)
            self.assertLess(y, region["y"] + region["height"])
            self.assertLessEqual(region["x"] + region["width"], 1024)
            self.assertLessEqual(region["y"] + region["height"], 768)

    def test_current_inspection_marker_preserves_image_geometry_and_clean_frame(self):
        screen = frame("blue", {"x": 120, "y": 200, "width": 512, "height": 384}, (1024, 768))
        target = {"x": 220, "y": 340}
        original = screen["base64"]
        self.assertEqual(image_point(screen, target), {"x": 200, "y": 280})
        annotated = marked_image(screen, target)
        self.assertEqual(screen["base64"], original)
        with Image.open(io.BytesIO(base64.b64decode(annotated))) as image:
            self.assertEqual(image.size, (1024, 768))
            ring = image.crop((188, 268, 212, 292)).convert("RGB")
            pixels = zip(*[iter(ring.tobytes())] * 3)
            self.assertGreater(sum(r > 180 and r > g + 70 and r > b + 70 for r, g, b in pixels), 40)
        self.assertIsNone(image_point(screen, {"x": 0, "y": 0}))

    def test_inspection_request_has_one_current_image_and_observed_history_without_old_targets(self):
        before = frame("blue")
        target = {"x": 220, "y": 340, "button": "left", "clicks": 1}
        prior = test_agent.decision("click", {"x": 110, "y": 170})
        result = {"ok": True, "input_sent": True, "ui_effect_verified": False, "last_click": target}
        events = [{"step": 1, "kind": "action", "data": {"decision": prior, "result": result}},
                  {"step": 2, "kind": "action", "data": {"decision": {**prior, "click_assessment": ASSESSMENT},
                                                            "result": result}}]
        original_events = json.dumps(events)
        memory = RecoveryMemory()
        memory.observe(prior, result, 2, before, before)
        screen = frame("blue", {"x": 120, "y": 200, "width": 512, "height": 384}, (1024, 768), 3)
        memory.inspect_click(screen)
        self.assertIsNone(memory.click_review_image)
        self.assertNotIn("arguments", memory.snapshot()["click_review"])
        self.assertNotIn("arguments", memory.snapshot()["gui_feedback"])
        self.assertEqual(memory.snapshot()["click_review"]["inspection"]["last_click_in_image"], {"x": 200, "y": 280})
        response = {**test_agent.decision("press", {"key": "Escape"}), "click_assessment": ASSESSMENT}
        client = ModelClient(Config())
        with mock.patch.object(client, "request", return_value={"message": {"content": json.dumps(response)}}) as request:
            client.decide("Close the dialog", "", "", events, screen, threading.Event(),
                          recovery=memory.snapshot(), click_review_image="obsolete-comparison")
        payload = request.call_args.args[2]
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][-1]["images"], [screen["base64"]])
        context = json.loads(payload["messages"][-1]["content"])
        self.assertEqual(context["screen"]["view"], "zoom")
        for observation in context["recent_events"]:
            self.assertNotIn("arguments", observation["data"])
            self.assertNotIn("last_click", observation["data"]["result"])
            self.assertTrue(observation["data"]["result"]["input_sent"])
        self.assertNotIn("next_step", context["recent_events"][1]["data"]["click_assessment"])
        self.assertEqual(json.dumps(events), original_events)
        cases = {c["properties"]["action"]["enum"][0]: c for c in payload["format"]["anyOf"]}
        self.assertIn("click", cases)
        self.assertIn("screenshot", cases)
        self.assertIn("target_at_click", cases["click"]["properties"]["click_assessment"]["required"])
        self.assertIn("cause", cases["click"]["properties"]["click_assessment"]["required"])

    def test_zoom_before_and_full_after_share_native_region_and_mark_executed_target(self):
        source = {"x": 600, "y": 400, "width": 100, "height": 80}
        before, after = frame("blue", source, (300, 240)), frame("green", captured_at=2)
        originals = [before["base64"], after["base64"]]
        evidence = comparison_image(before, after, {"x": 625, "y": 420})
        self.assertEqual(evidence["desktop_region"], source)
        with Image.open(io.BytesIO(base64.b64decode(evidence["base64"]))) as sheet:
            self.assertEqual(sheet.size, (640, 280))
            # Each pane shows its own observed scene, including the crop-to-desktop mapping.
            self.assertGreater(sheet.getpixel((50, 50))[2], 200)
            self.assertGreater(sheet.getpixel((370, 50))[1], 90)
            for pane in (sheet.crop((0, 20, 320, 260)), sheet.crop((320, 20, 640, 260))):
                pixels = zip(*[iter(pane.tobytes())] * 3)
                self.assertGreater(sum(r > 180 and r > g + 70 and r > b + 70 for r, g, b in pixels), 40)
        self.assertEqual([before["base64"], after["base64"]], originals)

    def test_edge_click_and_resolution_changes_do_not_invent_comparable_evidence(self):
        before, after = frame("blue"), frame("white")
        evidence = comparison_image(before, after, {"x": 799, "y": 599})
        region = evidence["desktop_region"]
        self.assertEqual(region["x"] + region["width"], 800)
        self.assertEqual(region["y"] + region["height"], 600)
        for other in (None, {**after, "desktop_width": 1000}, {**after, "base64": "broken"}):
            self.assertEqual(comparison_image(before, other, {"x": 799, "y": 599}), {})
        self.assertEqual(comparison_image(before, after, {"x": 900, "y": 600}), {})

    def test_changed_screen_still_requires_review_of_the_intended_effect_and_is_bounded(self):
        memory = RecoveryMemory()
        before, after = frame("blue"), frame("green", captured_at=2)
        click = {**test_agent.decision("click", {"x": 20, "y": 30}), "reason": "Close the window"}
        result = {"ok": True, "last_click": {"x": 42, "y": 60, "button": "left", "clicks": 1}}
        memory.observe(click, result, 7, before, after)
        review = memory.snapshot()["click_review"]
        self.assertEqual(review["intent"], "Close the window")
        self.assertEqual(review["screen_comparison"], "changed")
        self.assertEqual(review["desktop_target"]["x"], 42)
        self.assertEqual(review["after_captured_at"], 2)
        self.assertNotIn("base64", json.dumps(memory.snapshot()))
        self.assertLessEqual(len(json.dumps(memory.snapshot())), memory.MAX_CONTEXT)
        self.assertIsNotNone(memory.click_review_image)
        memory.failure(None, 8, "Model timed out")
        self.assertEqual(memory.snapshot()["click_review"], review)
        memory.consume_click_review()
        self.assertNotIn("click_review", memory.snapshot())
        self.assertIsNone(memory.click_review_image)

    def test_request_attaches_comparison_then_current_image_and_keeps_click_available(self):
        client = ModelClient(Config())
        current = frame("white")
        response = {**test_agent.decision("press", {"key": "Escape"}), "click_assessment": ASSESSMENT}
        recovery = {"click_review": {"step": 1, "intent": "Close the window"}}
        with mock.patch.object(client, "request", return_value={"message": {"content": json.dumps(response)}}) as request:
            actual, _ = client.decide("Close the window", "", "", [], current, threading.Event(),
                                      recovery=recovery, click_review_image="comparison-image")
        self.assertEqual(actual, response)
        payload = request.call_args.args[2]
        self.assertEqual(payload["messages"][-2]["images"], ["comparison-image"])
        self.assertEqual(payload["messages"][-1]["images"], [current["base64"]])
        context = json.loads(payload["messages"][-1]["content"])
        self.assertEqual(context["screen"]["width"], 1000)
        self.assertEqual(context["screen"]["pixel_width"], 400)
        self.assertNotIn("base64", json.dumps(context))
        self.assertEqual(list(context)[-1], "task")
        cases = {case["properties"]["action"]["enum"][0]: case for case in payload["format"]["anyOf"]}
        self.assertIn("click", cases)
        self.assertEqual(cases["click"]["properties"]["arguments"]["properties"]["x"]["maximum"], 1000)
        self.assertTrue(all("click_assessment" in case["required"] for case in cases.values()))
        self.assertFalse(payload["think"])

    def test_review_is_a_short_validated_observation_and_allows_a_justified_repeat(self):
        repeat = {**test_agent.decision("click", {"x": 20, "y": 30}), "click_assessment": {
            "intended_effect": "uncertain", "evidence": "The window is focused now.",
            "target_at_click": "The intended control", "cause": "The first click focused the window",
            "next_step": "A second click activates this control, as required by the task."}}
        self.assertEqual(parse_decision(json.dumps(repeat), review_click=True), repeat)
        invalid = [test_agent.decision("click", {"x": 20, "y": 30}),
                   {**repeat, "click_assessment": {key: value for key, value in ASSESSMENT.items()
                                                   if key not in {"target_at_click", "cause"}}},
                   {**repeat, "click_assessment": {**ASSESSMENT, "intended_effect": "success"}},
                   {**repeat, "click_assessment": {**ASSESSMENT, "evidence": ""}},
                   {**repeat, "click_assessment": {**ASSESSMENT, "next_step": "x" * 161}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ModelError):
                parse_decision(json.dumps(value), review_click=True)
        # The default schema and checklist mode retain their usual action choices.
        self.assertNotIn("click_assessment", decision_schema()["required"])
        checklist = decision_schema({"width": 400, "height": 300}, True, True, True)
        self.assertEqual({c["properties"]["action"]["enum"][0] for c in checklist["anyOf"]}, {"set_checklist", "finish"})

    def test_normal_request_does_not_add_a_review_or_an_extra_image(self):
        client = ModelClient(Config())
        response = test_agent.decision("finish", {"summary": "done"})
        with mock.patch.object(client, "request", return_value={"message": {"content": json.dumps(response)}}) as request:
            client.decide("Task", "", "", [], frame("white"), threading.Event())
        payload = request.call_args.args[2]
        self.assertEqual(len(payload["messages"]), 2)
        self.assertNotIn("click_assessment", payload["format"]["required"])


class ClickReviewAgentTests(unittest.TestCase):
    setUp = test_agent.AgentTests.setUp
    tearDown = test_agent.AgentTests.tearDown
    build = test_agent.AgentTests.build
    complete = test_agent.AgentTests.complete
    wait_for_status = test_agent.AgentTests.wait_for_status

    def test_repeated_native_target_gets_fresh_inspection_without_a_click_limit(self):
        model = self.build([test_agent.decision("click", {"x": 504, "y": 565})] +
                           [test_agent.decision("click", {"x": 512, "y": 384}) for _ in range(3)] +
                           [test_agent.decision("finish", {"summary": "Still unverified"})])
        run = self.complete()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(model.calls, 5)
        for index in range(1, 5):
            self.assertEqual(model.screens[index]["view"], "zoom")
            inspection = model.recoveries[index]["click_review"]["inspection"]
            self.assertEqual(inspection["last_click_in_image"], {"x": 512, "y": 384})
            self.assertEqual(model.recoveries[index]["gui_feedback"]["matching_unchanged_attempts"], index)
            self.assertIsNone(model.click_review_images[index])
        events = self.store.events(run["id"])
        clicks = [e for e in events if e["kind"] == "action" and e["data"]["decision"]["action"] == "click"]
        self.assertEqual(len(clicks), 4)
        self.assertEqual({(e["data"]["result"]["last_click"]["x"], e["data"]["result"]["last_click"]["y"])
                          for e in clicks}, {(945, 1059)})
        self.assertEqual(len([e for e in events if e["kind"] == "click_inspection"]), 4)
        self.assertFalse(any(e["kind"] in {"blocked", "stalled"} for e in events))
        # The model sees a marked crop; the desktop coordinate frame remains a clean capture.
        self.assertNotEqual(model.screens[-1]["base64"], self.desktop.frame["base64"])

    def test_user_chosen_stall_count_still_matches_full_and_zoom_local_clicks(self):
        import dataclasses

        self.config = dataclasses.replace(self.config, stall_attempts=3)
        model = self.build([test_agent.decision("click", {"x": 504, "y": 565}),
                            test_agent.decision("click", {"x": 512, "y": 384}),
                            test_agent.decision("click", {"x": 512, "y": 384}),
                            test_agent.decision("finish", {"summary": "Stopped repeating after user review"})])
        run_id = self.agent.start("Test chosen pause count", 20)
        self.wait_for_status(run_id, "paused")
        self.assertEqual(model.calls, 3)
        self.assertIn("3 matching click actions", self.agent.snapshot()["pause_reason"])
        self.agent.resume()
        self.agent.thread.join(3)
        self.assertFalse(self.agent.thread.is_alive())
        self.assertEqual(self.store.run(run_id)["status"], "completed")

    def test_fresh_capture_and_visual_review_lead_to_inspection_then_corrected_click(self):
        dismiss = {**test_agent.decision("press", {"key": "Escape"}), "click_assessment": ASSESSMENT}
        finish = {**test_agent.decision("finish", {"summary": "Closed the intended window"}),
                  "click_assessment": {"intended_effect": "observed", "evidence": "The window is gone.",
                                       "target_at_click": "The window close control", "cause": "No failure observed",
                                       "next_step": "Finish the task."}}
        model = self.build([test_agent.decision("click", {"x": 30, "y": 40}), dismiss,
                            test_agent.decision("click", {"x": 50, "y": 80}), finish])
        # The corrected click changes the observed desktop, independently of model-authored state.
        original = self.desktop.command
        clicks = 0

        def command(args, **kwargs):
            nonlocal clicks
            if args[:2] == ["xdotool", "click"]:
                clicks += 1
                if clicks == 2:
                    self.desktop.color = "white"
            return original(args, **kwargs)

        with mock.patch.object(self.desktop, "command", side_effect=command):
            run = self.complete()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(model.calls, 4)  # Assessment shares the ordinary decision; no hidden inference steps.
        self.assertEqual(self.desktop.captures, 5)
        self.assertEqual(model.screens[1]["view"], "zoom")
        self.assertIn("inspection", model.recoveries[1]["click_review"])
        self.assertIsNone(model.click_review_images[1])
        self.assertIsNone(model.click_review_images[2])
        self.assertIsNotNone(model.click_review_images[3])
        self.assertEqual(model.recoveries[1]["click_review"]["screen_comparison"], "essentially_unchanged")
        self.assertEqual(model.recoveries[3]["click_review"]["screen_comparison"], "changed")
        self.assertNotEqual(model.screens[2]["base64"], model.screens[3]["base64"])
        events = self.store.events(run["id"])
        self.assertEqual([e["data"]["decision"]["action"] for e in events if e["kind"] == "action"],
                         ["click", "press", "click", "finish"])
        self.assertEqual(events[-1]["data"]["decision"]["click_assessment"]["intended_effect"], "observed")
        self.assertFalse(any(e["kind"] in {"blocked", "stalled"} for e in events))

    def test_model_timeout_retains_inspection_until_the_next_action(self):
        model = self.build([test_agent.decision("click", {"x": 30, "y": 40}), ModelError("Timed out"),
                            {**test_agent.decision("press", {"key": "Escape"}), "click_assessment": ASSESSMENT},
                            test_agent.decision("finish", {"summary": "Recovered"})])
        with mock.patch.object(self.agent.stop_event, "wait", side_effect=lambda timeout: self.agent.stop_event.is_set()):
            run = self.complete()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(model.click_review_images[1], model.click_review_images[2])
        self.assertEqual(model.screens[1]["base64"], model.screens[2]["base64"])
        self.assertIn("inspection", model.recoveries[2]["click_review"])
        self.assertNotIn("click_review", model.recoveries[3])

    def test_click_tool_error_gets_a_fresh_image_before_the_next_decision(self):
        model = self.build([test_agent.decision("click", {"x": 30, "y": 40}),
                            test_agent.decision("finish", {"summary": "Could not deliver the click"})])
        original = self.desktop.command
        failed_capture = False

        def command(args, **kwargs):
            nonlocal failed_capture
            if args[:2] == ["xdotool", "click"]:
                self.desktop.color = "white"
                raise test_agent.ToolError("Desktop click command failed")
            if args[0] == "scrot" and self.desktop.color == "white" and not failed_capture:
                failed_capture = True
                raise test_agent.ToolError("Transient screenshot failure")
            return original(args, **kwargs)

        with mock.patch.object(self.desktop, "command", side_effect=command), \
             mock.patch.object(self.agent.stop_event, "wait", side_effect=lambda timeout: self.agent.stop_event.is_set()):
            run = self.complete()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.desktop.captures, 3)
        self.assertEqual(model.calls, 2)  # No next decision until a fresh screenshot was obtained.
        self.assertNotEqual(model.screens[0]["base64"], model.screens[1]["base64"])
        self.assertTrue(any(item["action"] == "click" for item in model.recoveries[1]["failed_attempts"]))
        self.assertNotIn("click_review", model.recoveries[1])  # No assertion that a failed command sent input.


if __name__ == "__main__":
    unittest.main()
