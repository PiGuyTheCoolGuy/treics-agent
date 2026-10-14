from __future__ import annotations

import base64
import dataclasses
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from treics_agent.agent import Agent
from treics_agent.config import Config
from treics_agent.model import ModelError, decision_schema, parse_decision
from treics_agent.progress import ProgressGuard
from treics_agent.storage import Store
from treics_agent.tools import ToolError, Tools
from test_agent import FakeDesktop, FakeModel, decision


class DesktopTests(unittest.TestCase):
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

    def zoom(self, **arguments):
        result = self.tools.execute("zoom_screenshot", arguments, self.stop)
        return self.desktop.capture(result["capture_region"])

    def test_zoom_uses_fresh_native_pixels_and_maps_nested_local_coordinates(self):
        self.desktop.capture()
        self.desktop.color = "white"  # Changed since the full screenshot.
        frame = self.zoom(x=900, y=50, width=100, height=60)
        self.assertEqual(frame["source"], {"x": 1687, "y": 93, "width": 188, "height": 114})
        self.assertEqual((frame["width"], frame["height"]), (564, 342))
        self.assertEqual(self.desktop.coordinates(300, 150), (1787, 143))
        self.assertEqual(self.desktop.coordinates(563, 341), (1874, 206))
        with Image.open(io.BytesIO(base64.b64decode(frame["base64"]))) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        nested = self.zoom(x=30, y=60, width=60, height=90)
        self.assertEqual(nested["source"], {"x": 1697, "y": 113, "width": 20, "height": 30})
        self.assertEqual(self.desktop.coordinates(30, 45), (1707, 128))
        # The dashboard always shows the whole desktop, even during a crop.
        with Image.open(io.BytesIO(self.desktop.image)) as preview:
            self.assertEqual(preview.size, (1024, 768))

    def test_zoom_budget_edges_and_resolution_change(self):
        self.desktop.capture()
        frame = self.zoom(x=0, y=0, width=1024, height=768, scale=6)
        self.assertEqual((frame["width"], frame["height"]), (1024, 768))
        tiny = self.zoom(x=1023, y=767, width=1, height=1, scale=6)
        self.assertEqual(tiny["source"], {"x": 1918, "y": 1438, "width": 2, "height": 2})
        self.assertEqual(self.desktop.coordinates(11, 11), (1919, 1439))
        region = self.desktop.zoom_region({"x": 0, "y": 0, "width": 12, "height": 12})
        region["desktop_width"] = 800
        with self.assertRaisesRegex(ToolError, "resolution changed"):
            self.desktop.capture(region)
        self.assertIsNone(self.desktop.frame)

    def test_invalid_zoom_is_rejected_without_input_or_capture(self):
        self.desktop.capture()
        arguments = {"x": 900, "y": 50, "width": 100, "height": 60}
        count = len(self.desktop.commands)
        for change in ({"width": 0}, {"height": -1}, {"width": True}, {"width": 125},
                       {"x": -1}, {"x": 1024}, {"y": 768}, {"height": 720},
                       {"scale": 1}, {"scale": 7}, {"scale": 2.5}):
            with self.subTest(change=change), self.assertRaises(ToolError):
                self.tools.execute("zoom_screenshot", {**arguments, **change}, self.stop)
        self.assertEqual(len(self.desktop.commands), count)
        self.desktop.frame = None
        with self.assertRaisesRegex(ToolError, "No current screenshot"):
            self.tools.execute("zoom_screenshot", arguments, self.stop)

    def test_agent_keeps_zoom_until_action_then_inspects_an_unchanged_click(self):
        model = FakeModel([decision("zoom_screenshot", {"x": 900, "y": 50, "width": 100, "height": 60}),
                           decision("click", {"x": 300, "y": 150}),
                           decision("finish", {"summary": "Clicked zoom target"})])
        agent = Agent(self.config, self.store, self.tools, model)
        try:
            run_id = agent.start("Inspect and click a small target")
            agent.thread.join(5)
            self.assertFalse(agent.thread.is_alive())
            self.assertEqual(self.store.run(run_id)["status"], "completed")
            self.assertEqual([screen["view"] for screen in model.screens], ["desktop", "zoom", "zoom"])
            self.assertEqual(model.screens[2]["source"], {"x": 1408, "y": 0, "width": 512, "height": 384})
            self.assertEqual(model.recoveries[2]["click_review"]["inspection"]["last_click_in_image"],
                             {"x": 758, "y": 286})
            self.assertIn(["xdotool", "mousemove", "--sync", "1787", "143"], self.desktop.commands)
            click = next(event["data"]["result"]["last_click"] for event in self.store.events(run_id)
                         if event["kind"] == "action" and event["data"]["decision"]["action"] == "click")
            self.assertEqual((click["x"], click["y"]), (1787, 143))
            self.assertEqual(click["source"]["view"], "zoom")
            self.assertEqual((click["source"]["x"], click["source"]["y"]), (300, 150))
            self.assertEqual(agent.snapshot()["last_click"], click)
            self.assertNotIn("base64", agent.snapshot()["screen"])
        finally:
            agent.shutdown()

    def test_marker_failure_does_not_retry_click_and_model_image_stays_clean(self):
        self.desktop.run_directory = Path(self.temp.name) / "run"
        before = self.desktop.capture()
        self.desktop.marker.show.side_effect = RuntimeError("SHAPE unavailable")
        result = self.tools.execute("click", {"x": 400, "y": 300, "button": "right", "clicks": 2}, self.stop)
        self.assertTrue(result["ok"])
        self.assertIn("SHAPE unavailable", result["click_marker_error"])
        self.assertEqual((result["last_click"]["x"], result["last_click"]["y"]), (750, 562))
        after = self.desktop.capture()
        self.assertEqual(before["base64"], after["base64"])
        self.assertNotEqual(base64.b64decode(after["base64"]), self.desktop.image)
        self.assertEqual((self.desktop.run_directory / "screen.jpg").read_bytes(), self.desktop.image)
        with Image.open(io.BytesIO(self.desktop.image)) as preview, \
                Image.open(io.BytesIO(base64.b64decode(after["base64"]))) as clean:
            difference = ImageChops.difference(preview, clean).getbbox()
            self.assertIsNotNone(difference)
            self.assertLess(abs((difference[0] + difference[2]) / 2 - 400), 5)
            self.assertLess(abs((difference[1] + difference[3]) / 2 - 300), 5)

    def test_zoom_drag_scroll_and_move_use_crop_coordinates(self):
        self.desktop.capture()
        self.zoom(x=900, y=50, width=100, height=60)
        for action, arguments in (("move", {"x": 300, "y": 150}),
                                  ("scroll", {"amount": -1, "x": 300, "y": 150}),
                                  ("drag", {"x": 300, "y": 150, "end_x": 330, "end_y": 180,
                                            "duration": 0.1})):
            self.assertTrue(self.tools.execute(action, arguments, self.stop)["ok"])
        self.assertIn(["xdotool", "mousemove", "--sync", "1787", "143"], self.desktop.commands)
        self.assertIn(["xdotool", "mousemove", "--sync", "1797", "153"], self.desktop.commands)
        self.assertEqual(self.desktop.pointer, (1797, 153))

    def test_repeated_click_at_current_pointer_skips_sync_movement(self):
        self.desktop.capture()
        self.tools.execute("click", {"x": 400, "y": 300}, self.stop)
        before = len(self.desktop.commands)
        self.assertTrue(self.tools.execute("click", {"x": 400, "y": 300}, self.stop)["ok"])
        repeated = self.desktop.commands[before:]
        self.assertFalse(any(command[:2] == ["xdotool", "mousemove"] for command in repeated))
        self.assertTrue(any(command[:2] == ["xdotool", "click"] for command in repeated))

    def test_schema_zoom_bounds_and_progress_respect_crop_origin(self):
        frame = self.desktop.capture()
        schema = decision_schema(frame)
        case = next(case for case in schema["anyOf"]
                    if case["properties"]["action"]["enum"] == ["zoom_screenshot"])
        self.assertEqual(case["properties"]["arguments"]["properties"]["width"]["maximum"], 1024)
        self.assertEqual(case["properties"]["arguments"]["properties"]["x"]["maximum"], 1023)
        valid = decision("zoom_screenshot", {"x": 2, "y": 3, "width": 50, "height": 60})
        self.assertEqual(parse_decision(json.dumps(valid)), valid)
        with self.assertRaises(ModelError):
            parse_decision(json.dumps(decision("zoom_screenshot", {"x": 2, "y": 3, "width": 0, "height": 60})))
        other_origin = {**frame, "source": {**frame["source"], "x": 10}}
        self.assertFalse(ProgressGuard.same_screen(frame, other_origin))


if __name__ == "__main__":
    unittest.main()
