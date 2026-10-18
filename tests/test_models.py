from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from treics_agent.config import Config, initialize_config, load_config, set_model, set_stall_attempts, set_output_tokens
from treics_agent.model import ModelClient, ModelError


class ModelConfigTests(unittest.TestCase):
    def test_output_token_defaults_unlimited_and_saved_migration(self):
        self.assertEqual(Config().num_predict, 8192)
        Config(num_predict=-1).validate()
        Config(num_predict=32768).validate()
        for invalid in (0, -2, 127, 32769, 8192.0, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Config(num_predict=invalid).validate()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            initialize_config(path)
            self.assertEqual(load_config(path).num_predict, 8192)
            path.write_text(path.read_text().replace("num_predict = 8192", "num_predict = 1600"))
            self.assertEqual(load_config(path).num_predict, 1600)
            set_output_tokens(-1, path)
            self.assertEqual(load_config(path).num_predict, -1)
            set_output_tokens(8192, path)
            self.assertEqual(load_config(path).num_predict, 8192)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for invalid in (0, -2, 32769, True):
                with self.assertRaises(ValueError):
                    set_output_tokens(invalid, path)
            self.assertEqual(load_config(path).num_predict, 8192)

    def test_removed_click_guard_setting_does_not_break_an_existing_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            initialize_config(path)
            initial = load_config(path)
            path.write_text(path.read_text() + "click_repeat_limit = 2\n")
            self.assertEqual(load_config(path), initial)
            set_stall_attempts(0, path)
            self.assertEqual(load_config(path).stall_attempts, 0)

    def test_manual_stall_default_and_saved_repeat_count_preserve_model_and_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            initialize_config(path)
            initial = load_config(path)
            self.assertEqual(initial.stall_attempts, 0)
            path.write_text(path.read_text().replace("stall_attempts = 0\n", ""))
            self.assertEqual(load_config(path).stall_attempts, 0, "Old configs did not default to manual control")
            set_stall_attempts(25, path)
            self.assertEqual(load_config(path), dataclasses.replace(initial, stall_attempts=25))
            path.write_text(path.read_text().replace("stall_attempts = 25", "'stall_attempts' = 25 # chosen count"))
            set_model("qwen3-vl:8b", path)
            set_stall_attempts(0, path)
            self.assertEqual(load_config(path), dataclasses.replace(initial, model="qwen3-vl:8b"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_model_selection_survives_reload_and_preserves_credentials_comments_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "custom.toml"
            initialize_config(path)
            initial = load_config(path)
            path.write_text(path.read_text().replace('model = "' + initial.model + '"',
                            "  'model' = '" + initial.model + "' # current selection") + "# keep this comment\n")
            set_model("qwen3-vl:8b", path)
            selected = load_config(path)
            self.assertEqual(selected, dataclasses.replace(initial, model="qwen3-vl:8b"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("# keep this comment", path.read_text())
            self.assertEqual(path.read_text().count("model ="), 1)
            # An older config can omit the model and use the default.
            path.write_text('dashboard_token = "' + initial.dashboard_token + '"\n')
            set_model("qwen3-vl:8b", path)
            self.assertEqual(load_config(path).model, "qwen3-vl:8b")

    def test_failed_atomic_save_and_environment_override_leave_original_file_intact(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            initialize_config(path)
            original = path.read_bytes()
            with mock.patch("treics_agent.config.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    set_model("qwen3-vl:8b", path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])
            with mock.patch.dict(os.environ, {"TREICS_AGENT_MODEL": Config().model}):
                with self.assertRaisesRegex(ValueError, "environment override"):
                    set_model("qwen3-vl:8b", path)
            self.assertEqual(path.read_bytes(), original)
            for name in (None, [], "", "bad model", "model\nport=1", "x" * 257):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    set_model(name, path)
            self.assertEqual(path.read_bytes(), original)

    def test_catalog_validation_and_metadata_deadlines_do_not_change_inference_timeout(self):
        client = ModelClient(Config())
        with mock.patch.object(client, "request", return_value={"models": [{"name": "b:latest"},
                               {"name": "a:latest"}, {"name": "b:latest"}]}) as request:
            self.assertEqual(client.available_models(), ["a:latest", "b:latest"])
            request.assert_called_once_with("GET", "/api/tags", timeout=3)
        for result in ({}, {"models": "bad"}, {"models": [None]}, {"models": [{"name": "bad model"}]}):
            with self.subTest(result=result), mock.patch.object(client, "request", return_value=result):
                with self.assertRaises(ModelError):
                    client.available_models()
        with mock.patch.object(client, "request", side_effect=[{"models": [{"name": "a:latest"}]}, {}]) as request:
            client.check_model("a:latest")  # Older Ollama can omit capability metadata.
            self.assertEqual(request.call_args, mock.call("POST", "/api/show", {"model": "a:latest"}, timeout=3))
        self.assertEqual(client.config.request_timeout, 1200)
