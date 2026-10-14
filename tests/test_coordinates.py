from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from treics_agent.config import Config, initialize_config, load_config
from treics_agent.coordinates import coordinate_space, model_history, model_recovery, to_pixels
from treics_agent.model import ModelClient, ModelError, recent_observations
import test_agent


class CoordinateTests(unittest.TestCase):
    def test_qwen3_model_family_and_explicit_overrides(self):
        for model in ("qwen3-vl:30b-a3b-instruct", "qwen3-vl:8b", "Qwen/Qwen3-VL-32B-Instruct", "qwen3vl:latest"):
            self.assertEqual(coordinate_space(model), "normalized_1000")
        self.assertEqual(coordinate_space("qwen2.5vl:7b"), "pixels")
        self.assertEqual(coordinate_space("other-vision"), "pixels")
        self.assertEqual(coordinate_space("qwen3-vl:8b", "pixels"), "pixels")
        self.assertEqual(coordinate_space("custom-name", "normalized_1000"), "normalized_1000")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            initialize_config(path)
            path.write_text(path.read_text().replace('coordinate_space = "auto"\n', ""))
            self.assertEqual(load_config(path).coordinate_space, "auto")
        with self.assertRaises(ValueError):
            dataclasses.replace(Config(), coordinate_space="guess").validate()

    def test_reported_point_is_scaled_on_each_axis_not_executed_as_pixels(self):
        screen = {"width": 1024, "height": 768, "view": "desktop"}
        arguments, mapping = to_pixels("click", {"x": 504, "y": 565, "clicks": 1}, screen, "normalized_1000")
        self.assertEqual(arguments, {"x": 516, "y": 434, "clicks": 1})
        self.assertEqual(mapping["model_arguments"], {"x": 504, "y": 565})
        self.assertEqual(mapping["image_arguments"], {"x": 516, "y": 434})
        for action, values in (("move", {"x": 1000, "y": 1000}),
                               ("drag", {"x": 0, "y": 0, "end_x": 1000, "end_y": 1000}),
                               ("scroll", {"x": 1000, "y": 1000, "amount": -2})):
            actual, _ = to_pixels(action, values, screen, "normalized_1000")
            self.assertEqual(actual.get("end_x", actual["x"]), 1023)
            self.assertEqual(actual.get("end_y", actual["y"]), 767)
        self.assertEqual(to_pixels("scroll", {"amount": 3}, None, "normalized_1000"), ({"amount": 3}, None))

    def test_zoom_rectangles_cover_native_image_pixels_and_reject_overflow(self):
        screen = {"width": 1024, "height": 768}
        actual, _ = to_pixels("zoom_screenshot", {"x": 250, "y": 250, "width": 500, "height": 500, "scale": 3},
                              screen, "normalized_1000")
        self.assertEqual(actual, {"x": 256, "y": 192, "width": 512, "height": 384, "scale": 3})
        edge, _ = to_pixels("zoom_screenshot", {"x": 999, "y": 999, "width": 1, "height": 1}, screen, "normalized_1000")
        self.assertEqual(edge["x"] + edge["width"], 1024)
        self.assertEqual(edge["y"] + edge["height"], 768)
        for action, arguments, current in (("click", {"x": 1001, "y": 500}, screen),
                                            ("click", {"x": 500, "y": 500}, None),
                                            ("zoom_screenshot", {"x": 750, "y": 500, "width": 500, "height": 100}, screen)):
            with self.assertRaises(ValueError):
                to_pixels(action, arguments, current, "normalized_1000")

    def test_model_client_declares_normalized_grid_and_records_conversion(self):
        screen = {"width": 1024, "height": 768, "base64": "current-image", "view": "desktop"}
        response = test_agent.decision("click", {"x": 504, "y": 565})
        client = ModelClient(Config())
        with mock.patch.object(client, "request", return_value={"message": {"content": json.dumps(response)}}) as request:
            actual, metrics = client.decide("Close the dialog", "", "", [], screen, threading.Event())
        self.assertEqual(actual["arguments"], {"x": 516, "y": 434})
        self.assertEqual(metrics["coordinate_mapping"]["space"], "normalized_1000")
        payload = request.call_args.args[2]
        context = json.loads(payload["messages"][-1]["content"])
        self.assertEqual(context["screen"]["width"], 1000)
        self.assertEqual(context["screen"]["height"], 1000)
        self.assertEqual((context["screen"]["pixel_width"], context["screen"]["pixel_height"]), (1024, 768))
        self.assertEqual(context["action_coordinate_system"], "normalized_1000")
        self.assertIn("1000x1000", payload["messages"][0]["content"])
        self.assertNotIn("not a normalized 0-1000 scale", payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][-1]["images"], ["current-image"])
        client = ModelClient(dataclasses.replace(Config(), coordinate_space="pixels"))
        with mock.patch.object(client, "request", return_value={"message": {"content": json.dumps(response)}}):
            actual, metrics = client.decide("Close the dialog", "", "", [], screen, threading.Event())
        self.assertEqual(actual["arguments"], response["arguments"])
        self.assertNotIn("coordinate_mapping", metrics)
        client = ModelClient(Config())
        invalid = test_agent.decision("click", {"x": 1001, "y": 500})
        with mock.patch.object(client, "request", return_value={"message": {"content": json.dumps(invalid)}}), \
             self.assertRaises(ModelError):
            client.decide("Close the dialog", "", "", [], screen, threading.Event())

    def test_history_and_inspection_point_keep_coordinate_units_without_mutating_logs(self):
        screen = {"width": 1024, "height": 768}
        recovery = {"gui_feedback": {"action": "click", "arguments": {"x": 512, "y": 384}},
                    "click_review": {"arguments": {"x": 512, "y": 384}, "desktop_target": {"x": 400, "y": 350},
                                     "inspection": {"last_click_in_image": {"x": 512, "y": 384}}}}
        original = json.dumps(recovery)
        actual = model_recovery(recovery, screen, "normalized_1000")
        self.assertEqual(actual["click_review"]["inspection"]["last_click_in_image"], {"x": 500, "y": 500})
        self.assertEqual(json.dumps(recovery), original)
        _, mapping = to_pixels("click", {"x": 504, "y": 565}, screen, "normalized_1000")
        events = [{"step": 1, "kind": "action", "data": {"decision": test_agent.decision("click", {"x": 516, "y": 434}),
                                                          "metrics": {"coordinate_mapping": mapping}}},
                  {"step": 2, "kind": "action", "data": {"decision": test_agent.decision("click", {"x": 504, "y": 565})}}]
        observations = model_history(recent_observations(events), events, "normalized_1000")
        self.assertEqual(observations[0]["data"]["arguments"], {"x": 504, "y": 565})
        self.assertEqual(observations[0]["data"]["arguments_coordinate_space"], "normalized_1000_of_historical_image")
        self.assertEqual(observations[1]["data"]["arguments"], {})
        self.assertTrue(observations[1]["data"]["historical_coordinates_omitted"])


if __name__ == "__main__":
    unittest.main()
