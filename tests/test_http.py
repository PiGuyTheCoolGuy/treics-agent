from __future__ import annotations

import dataclasses
import http.client
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from treics_agent.agent import Agent
from treics_agent.config import Config, load_config
from treics_agent.model import Cancelled, ModelClient, ModelError
from treics_agent.progress import ProgressGuard
from treics_agent.server import AgentHTTPServer
from treics_agent.storage import Store
from treics_agent.tools import Tools
from test_agent import FakeDesktop, FakeModel, decision
from test_stream import frame_worker_script, worker_stub


def script_module(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proxy_module = script_module("model-proxy")
vm_module = script_module("vm-config")
diagnostic_module = script_module("diagnose-model")


def request(server, path, method="GET", body=None, token=None, extra=None, timeout=3):
    connection = http.client.HTTPConnection(*server.server_address, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    headers.update(extra or {})
    try:
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, data):
        data = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.server.block and (self.server.block_paths is None or self.path in self.server.block_paths):
            self.server.entered.set()
            self.server.release.wait(5)
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/api/tags":
            self.reply({"models": [{"name": "qwen3-vl:30b-a3b-instruct"},
                                   {"name": "qwen3-vl:8b"}, {"name": "private-model"}]})
        else:
            self.reply({"version": "test"})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.payloads.append(payload)
        if self.path == "/api/show":
            return self.reply({"capabilities": self.server.capabilities.get(payload["model"], ["completion", "vision"])})
        if self.path == "/api/chat" and payload.get("stream"):
            output = json.dumps(self.server.decision)
            pieces = [output[:len(output) // 2], output[len(output) // 2:]]
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            if self.server.block and (self.server.block_paths is None or self.path in self.server.block_paths):
                self.server.entered.set()
                self.server.release.wait(5)
            for index, piece in enumerate(pieces):
                data = {"message": {"role": "assistant", "content": piece},
                        "done": index == len(pieces) - 1}
                if index == 0 and self.server.thinking:
                    data["message"]["thinking"] = self.server.thinking
                if data["done"]:
                    data.update({"eval_count": 20, **self.server.reply_overrides})
                try:
                    self.wfile.write((json.dumps(data) + "\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return
        self.reply({"message": {"role": "assistant", "content": json.dumps(self.server.decision)},
                    "done": True, "total_duration": 1000000000, "eval_count": 20, **self.server.reply_overrides})


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = dataclasses.replace(Config(), data_dir=self.temp.name + "/data",
                                       work_dir=self.temp.name + "/work", dashboard_token="dashboard-" + "a" * 32)
        self.store = Store(self.cfg.data_path)
        self.desktop = FakeDesktop(self.cfg)
        self.agent = Agent(self.cfg, self.store, Tools(self.cfg, self.store, self.desktop),
                           FakeModel([decision("finish", {"summary": "Done"})]))
        self.api = AgentHTTPServer(("127.0.0.1", 0), self.agent)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        self.upstream.daemon_threads = True
        self.upstream.payloads = []
        self.upstream.reply_overrides = {}
        self.upstream.thinking = ""
        self.upstream.block = False
        self.upstream.block_paths = None
        self.upstream.capabilities = {}
        self.upstream.entered = threading.Event()
        self.upstream.release = threading.Event()
        self.upstream.decision = decision("finish", {"summary": "Done"})
        self.proxy_token = "proxy-" + "b" * 32
        self.proxy = proxy_module.ProxyServer(("127.0.0.1", 0), self.proxy_token,
                     {"qwen3-vl:30b-a3b-instruct"}, self.upstream.server_address)
        self.servers = [self.api, self.upstream, self.proxy]
        self.threads = []
        for server in self.servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        self.upstream.release.set()
        self.agent.shutdown()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(2)
        self.store.close()
        self.temp.cleanup()

    def test_dashboard_authentication_and_same_origin_controls(self):
        status, html = request(self.api, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Treic's Agent", html)
        self.assertIn(b"/assets/desktop-stream.js", html)
        self.assertEqual(request(self.api, "/assets/desktop-stream.js")[0], 200)
        self.assertEqual(request(self.api, "/api/desktop/stream")[0], 401)
        self.assertIsNone(self.api.desktop_stream.thread, "Unauthenticated viewing started capture")
        self.assertEqual(request(self.api, "/api/desktop/stream", token=self.cfg.dashboard_token,
                                 extra={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(request(self.api, "/api/state")[0], 401)
        self.assertEqual(request(self.api, "/api/state", token="bad")[0], 401)
        status, body = request(self.api, "/api/state", token=self.cfg.dashboard_token)
        self.assertEqual(status, 200)
        self.assertNotIn(self.cfg.dashboard_token.encode(), body)
        self.assertEqual(request(self.api, "/api/start", "POST", {"task": "Test"}, self.cfg.dashboard_token)[0], 403)
        controls = {"X-Treic-Agent": "1", "Origin": "https://attacker.example"}
        self.assertEqual(request(self.api, "/api/start", "POST", {"task": "Test"},
                                 self.cfg.dashboard_token, controls)[0], 403)
        controls.pop("Origin")
        status, body = request(self.api, "/api/start", "POST", {"task": "Test"}, self.cfg.dashboard_token, controls)
        self.assertEqual(status, 201)
        run_id = json.loads(body)["run_id"]
        self.agent.thread.join(2)
        status, body = request(self.api, "/api/runs/" + run_id, token=self.cfg.dashboard_token)
        self.assertEqual(json.loads(body)["run"]["status"], "completed")
        self.assertEqual(request(self.api, "/api/screen", token=self.cfg.dashboard_token)[0], 200)

    def test_authenticated_live_model_output_stream(self):
        self.assertEqual(request(self.api, "/api/model/output/stream")[0], 401)
        self.assertEqual(request(self.api, "/api/model/output/stream", token=self.cfg.dashboard_token,
                                 extra={"Origin": "https://attacker.example"})[0], 403)
        connection = http.client.HTTPConnection(*self.api.server_address, timeout=2)
        connection.request("GET", "/api/model/output/stream",
                           headers={"Authorization": "Bearer " + self.cfg.dashboard_token})
        response = connection.getresponse()
        try:
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type"))
            first = response.readline()
            self.assertTrue(first.startswith(b"data: "))
            self.agent.update_model_output("a" * 32, 2, "generating", "partial response")
            response.readline()  # End of initial event.
            state = json.loads(response.readline()[6:])
            self.assertEqual(state["text"], "partial response")
            self.assertEqual(state["step"], 2)
            self.assertEqual(state["chunks"], 1)
            self.assertIsNotNone(state["first_token_at"])
            self.assertIsNotNone(state["last_token_at"])
            self.agent.update_model_output("a" * 32, 2, "thinking", thinking="checking")
            response.readline()
            thinking_state = json.loads(response.readline()[6:])
            self.assertEqual(thinking_state["thinking"], "checking")
            self.assertEqual(thinking_state["thinking_chunks"], 1)
        finally:
            connection.close()

    def test_model_stream_round_trip_through_gateway(self):
        self.configure_models()
        chunks = []
        client = ModelClient(self.cfg)
        result, metrics = client.decide("Stream decision", "", "", [], None,
                                         threading.Event(), on_chunk=chunks.append)
        self.assertEqual(result["action"], "finish")
        self.assertEqual("".join(chunks), json.dumps(self.upstream.decision))
        self.assertEqual(metrics["eval_count"], 20)
        self.assertIs(self.upstream.payloads[-1]["stream"], True)

    def test_model_stream_thinking_is_reported_without_corrupting_structured_output(self):
        self.configure_models()
        self.upstream.thinking = "Model-provided analysis text."
        text_chunks, thinking_chunks = [], []
        decision_result, metrics = ModelClient(self.cfg).decide(
            "Stream decision", "", "", [], None, threading.Event(),
            on_chunk=text_chunks.append, on_thinking=thinking_chunks.append)
        self.assertEqual(decision_result["action"], "finish")
        self.assertEqual("".join(text_chunks), json.dumps(self.upstream.decision))
        self.assertEqual(thinking_chunks, ["Model-provided analysis text."])
        self.assertEqual(metrics["eval_count"], 20)

    def configure_models(self):
        self.cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                       ollama_token=self.proxy_token)
        path = Path(self.temp.name) / "custom-config.toml"
        path.write_text("# preserve local settings\n" + "\n".join(
            f"{name} = {json.dumps(value)}" for name, value in dataclasses.asdict(self.cfg).items()) + "\n")
        path.chmod(0o600)
        self.agent.config = self.cfg
        self.agent.config_file = path
        self.agent.model = ModelClient(self.cfg)
        self.proxy.models.add("qwen3-vl:8b")
        return path

    def test_dashboard_discovers_switches_and_saves_models_without_losing_run_history(self):
        path = self.configure_models()
        self.assertEqual(request(self.api, "/assets/model-selector.js")[0], 200)
        self.assertEqual(request(self.api, "/api/models")[0], 401)
        self.assertEqual(request(self.api, "/api/models", token=self.cfg.dashboard_token,
                                 extra={"Origin": "https://attacker.example"})[0], 403)
        status, body = request(self.api, "/api/models", token=self.cfg.dashboard_token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"model": self.cfg.model,
                         "models": ["qwen3-vl:30b-a3b-instruct", "qwen3-vl:8b"]})
        self.assertNotIn(self.proxy_token.encode(), body)
        first = self.agent.start("Use the original model")
        self.agent.thread.join(2)
        self.assertEqual(self.store.run(first)["status"], "completed")
        self.assertTrue(self.proxy.inference.acquire(timeout=2))
        self.proxy.inference.release()
        controls = {"X-Treic-Agent": "1"}
        selected = {"model": "qwen3-vl:8b"}
        self.assertEqual(request(self.api, "/api/model", "POST", selected)[0], 401)
        self.assertEqual(request(self.api, "/api/model", "POST", selected, self.cfg.dashboard_token)[0], 403)
        self.assertEqual(request(self.api, "/api/model", "POST", selected, self.cfg.dashboard_token,
                                 {**controls, "Origin": "https://attacker.example"})[0], 403)
        status, body = request(self.api, "/api/model", "POST", selected, self.cfg.dashboard_token, controls)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), selected)
        self.assertEqual(load_config(path), dataclasses.replace(self.cfg, **selected))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("# preserve local settings", path.read_text())
        status, body = request(self.api, "/api/start", "POST", {"task": "Use the smaller model"},
                               self.cfg.dashboard_token, controls)
        self.assertEqual(status, 201, body)
        second = json.loads(body)["run_id"]
        self.agent.thread.join(2)
        self.assertEqual(self.store.run(second)["status"], "completed")
        self.assertEqual(self.store.run(first)["model"], self.cfg.model)
        self.assertEqual(self.store.run(second)["model"], "qwen3-vl:8b")
        self.assertEqual(self.upstream.payloads[-1]["model"], "qwen3-vl:8b")
        restarted = Agent(load_config(path), self.store, self.agent.tools, config_file=path)
        self.assertEqual(restarted.snapshot()["model"], "qwen3-vl:8b")
        restarted.shutdown()

    def test_failed_model_switches_preserve_current_model_and_configuration(self):
        path = self.configure_models()
        original, client = path.read_bytes(), self.agent.model
        controls = {"X-Treic-Agent": "1"}
        for name in (None, [], "bad model", "not-installed", "private-model"):
            with self.subTest(name=name):
                self.assertEqual(request(self.api, "/api/model", "POST", {"model": name},
                                         self.cfg.dashboard_token, controls)[0], 400)
        self.upstream.capabilities["qwen3-vl:8b"] = ["completion"]
        status, body = request(self.api, "/api/model", "POST", {"model": "qwen3-vl:8b"},
                               self.cfg.dashboard_token, controls)
        self.assertEqual(status, 400)
        self.assertIn(b"vision", body)
        with mock.patch("treics_agent.model.ModelClient.request", side_effect=ModelError("gateway offline")):
            self.assertEqual(request(self.api, "/api/models", token=self.cfg.dashboard_token)[0], 502)
            self.assertEqual(request(self.api, "/api/model", "POST", {"model": "qwen3-vl:8b"},
                                     self.cfg.dashboard_token, controls)[0], 502)
        self.upstream.capabilities.clear()
        with mock.patch("treics_agent.config.os.replace", side_effect=OSError("disk error")):
            self.assertEqual(request(self.api, "/api/model", "POST", {"model": "qwen3-vl:8b"},
                                     self.cfg.dashboard_token, controls)[0], 400)
        self.assertEqual(path.read_bytes(), original)
        self.assertIs(self.agent.model, client)
        self.assertEqual(self.agent.config, self.cfg)
        self.assertFalse(self.agent.model_switching)

    def test_model_changes_are_rejected_during_running_and_paused_tasks(self):
        path = self.configure_models()
        original = path.read_bytes()
        model = FakeModel([decision("finish", {"summary": "Done"})])
        model.release.clear()
        self.agent.model = model
        run_id = self.agent.start("Keep the original model for the entire task")
        self.assertTrue(model.entered.wait(2))
        controls = {"X-Treic-Agent": "1"}
        for paused in (False, True):
            if paused:
                self.agent.pause()
                model.release.set()
                for _ in range(100):
                    if self.store.run(run_id)["status"] == "paused":
                        break
                    time.sleep(0.01)
                self.assertEqual(self.store.run(run_id)["status"], "paused")
            status, body = request(self.api, "/api/model", "POST", {"model": "qwen3-vl:8b"},
                                   self.cfg.dashboard_token, controls)
            self.assertEqual(status, 400)
            self.assertIn(b"current task", body)
        self.assertEqual(path.read_bytes(), original)
        self.assertIs(self.agent.model, model)
        self.agent.stop()
        self.agent.thread.join(2)
        self.assertEqual(self.store.run(run_id)["status"], "stopped")

    def test_model_verification_keeps_status_and_stop_responsive_and_reserves_new_tasks(self):
        path = self.configure_models()
        original = path.read_bytes()
        self.upstream.block = True
        self.upstream.block_paths = {"/api/tags"}
        controls = {"X-Treic-Agent": "1"}
        result = []
        worker = threading.Thread(target=lambda: result.append(request(self.api, "/api/model", "POST",
                     {"model": "qwen3-vl:8b"}, self.cfg.dashboard_token, controls, timeout=8)))
        worker.start()
        try:
            self.assertTrue(self.upstream.entered.wait(2))
            status, body = request(self.api, "/api/state", token=self.cfg.dashboard_token, timeout=1)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["model_switching"])
            self.assertFalse(json.loads(body)["task_active"])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(request(self.api, "/api/start", "POST", {"task": "Wait for the selection"},
                                     self.cfg.dashboard_token, controls, timeout=1)[0], 400)
            self.assertEqual(request(self.api, "/api/model", "POST", {"model": self.cfg.model},
                                     self.cfg.dashboard_token, controls, timeout=1)[0], 400)
            self.assertEqual(request(self.api, "/api/stop", "POST", {}, self.cfg.dashboard_token,
                                     controls, timeout=1)[0], 200)
        finally:
            self.upstream.release.set()
            worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0], 200, result)
        self.assertFalse(self.agent.model_switching)
        self.assertEqual(load_config(path).model, "qwen3-vl:8b")

    def test_discovery_during_inference_preserves_model_cancellation(self):
        self.configure_models()
        self.upstream.block = True
        self.upstream.block_paths = {"/api/chat"}
        run_id = self.agent.start("Keep stop working while listing models")
        self.assertTrue(self.upstream.entered.wait(2))
        client, connection = self.agent.model, self.agent.model.connection
        status, _ = request(self.api, "/api/models", token=self.cfg.dashboard_token, timeout=1)
        self.assertEqual(status, 200)
        self.assertIs(client.connection, connection)
        self.assertEqual(request(self.api, "/api/stop", "POST", {}, self.cfg.dashboard_token,
                                 {"X-Treic-Agent": "1"}, timeout=1)[0], 200)
        self.agent.thread.join(2)
        self.assertFalse(self.agent.thread.is_alive())
        self.assertEqual(self.store.run(run_id)["status"], "stopped")

    def test_model_metadata_timeout_releases_switch_reservation_without_changing_config(self):
        path = self.configure_models()
        original, client = path.read_bytes(), self.agent.model
        self.upstream.block = True
        self.upstream.block_paths = {"/api/tags"}
        started = time.monotonic()
        status, body = request(self.api, "/api/model", "POST", {"model": "qwen3-vl:8b"},
                               self.cfg.dashboard_token, {"X-Treic-Agent": "1"}, timeout=8)
        self.assertEqual(status, 502, body)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(self.agent.model_switching)
        self.assertEqual(path.read_bytes(), original)
        self.assertIs(self.agent.model, client)

    def test_status_cached_screen_and_controls_respond_while_desktop_is_locked(self):
        self.desktop.capture()
        image = self.desktop.image
        held, release = threading.Event(), threading.Event()
        def block_desktop():
            with self.desktop.lock:
                held.set()
                release.wait(5)
        worker = threading.Thread(target=block_desktop)
        worker.start()
        self.assertTrue(held.wait(1))
        try:
            run_id = self.agent.start("Test responsive controls during stalled capture")
            status, body = request(self.api, "/api/state", token=self.cfg.dashboard_token, timeout=1)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["current"]["id"], run_id)
            self.assertEqual(request(self.api, "/api/screen", token=self.cfg.dashboard_token, timeout=1),
                             (200, image))
            for action in ("pause", "resume", "stop"):
                self.assertEqual(request(self.api, "/api/" + action, "POST", {}, self.cfg.dashboard_token,
                                         {"X-Treic-Agent": "1"}, timeout=1)[0], 200)
            self.assertFalse(release.is_set(), "Controls needed the desktop operation to return")
            self.assertTrue(self.agent.stop_event.is_set())
        finally:
            release.set()
            worker.join(2)
            if self.agent.thread:
                self.agent.thread.join(3)
        self.assertEqual(self.store.run(run_id)["status"], "stopped")

    def test_manual_preview_keeps_status_responsive_and_excludes_new_tasks(self):
        self.desktop.capture()
        entered, release = threading.Event(), threading.Event()
        original = self.desktop.command
        def blocked_capture(args, **kwargs):
            if args[0] == "scrot":
                entered.set()
                release.wait(5)
            return original(args, **kwargs)
        results = []
        def preview():
            results.append(request(self.api, "/api/preview", "POST", {}, self.cfg.dashboard_token,
                                   {"X-Treic-Agent": "1"}))
        with mock.patch.object(self.desktop, "command", side_effect=blocked_capture):
            worker = threading.Thread(target=preview)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                status, body = request(self.api, "/api/state", token=self.cfg.dashboard_token, timeout=1)
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["previewing"])
                self.assertEqual(request(self.api, "/api/screen", token=self.cfg.dashboard_token, timeout=1)[0], 200)
                controls = {"X-Treic-Agent": "1"}
                self.assertEqual(request(self.api, "/api/start", "POST", {"task": "Must not race preview"},
                                         self.cfg.dashboard_token, controls, timeout=1)[0], 400)
                self.assertEqual(request(self.api, "/api/preview", "POST", {}, self.cfg.dashboard_token,
                                         controls, timeout=1)[0], 400)
                self.assertEqual(request(self.api, "/api/stop", "POST", {}, self.cfg.dashboard_token,
                                         controls, timeout=1)[0], 200)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0][0], 200)
        self.assertFalse(self.agent.snapshot()["previewing"])

    def test_authenticated_live_frames_share_capture_and_do_not_block_task_controls(self):
        self.desktop.capture()
        model_frame, model_image = self.desktop.frame, self.desktop.image
        stream = self.api.desktop_stream
        workers, viewers = [], []
        held, release = threading.Event(), threading.Event()
        def block_desktop():
            with self.desktop.lock:
                held.set()
                release.wait(5)
        blocker = threading.Thread(target=block_desktop)
        def read_frame(response):
            line = response.readline(8192)
            while not line.strip():
                line = response.readline(8192)
            self.assertEqual(line.strip(), b"--treic-frame")
            headers = {}
            while (line := response.readline(8192)).strip():
                key, value = line.decode().split(":", 1)
                headers[key.lower()] = value.strip()
            return headers, response.read(int(headers["content-length"]))
        try:
            with worker_stub(frame_worker_script(), workers):
                for _ in range(2):
                    connection = http.client.HTTPConnection(*self.api.server_address, timeout=2)
                    connection.request("GET", "/api/desktop/stream", headers={"Authorization": "Bearer " + self.cfg.dashboard_token})
                    response = connection.getresponse()
                    viewers.append((connection, response))
                    self.assertEqual(response.status, 200)
                    self.assertIn("multipart/x-mixed-replace", response.getheader("Content-Type"))
                    headers, body = read_frame(response)
                    self.assertEqual(headers["content-type"], "image/jpeg")
                    self.assertEqual(headers["x-desktop-width"], "800")
                    self.assertTrue(body.startswith(b"\xff\xd8"))
                self.assertEqual(len(workers), 1)
                self.assertIs(self.desktop.frame, model_frame)
                self.assertEqual(self.desktop.image, model_image)
                blocker.start()
                self.assertTrue(held.wait(1))
                run_id = self.agent.start("Stop during independent desktop streaming")
                self.assertEqual(request(self.api, "/api/state", token=self.cfg.dashboard_token, timeout=1)[0], 200)
                self.assertEqual(request(self.api, "/api/stop", "POST", {}, self.cfg.dashboard_token,
                                         {"X-Treic-Agent": "1"}, timeout=1)[0], 200)
                self.assertEqual(read_frame(viewers[0][1])[0]["content-type"], "image/jpeg")
                self.assertIsNone(workers[0].poll(), "Stopping the task stopped the live view")
                self.assertIs(self.desktop.frame, model_frame)
                release.set()
                blocker.join(2)
                self.agent.thread.join(3)
                self.assertEqual(self.store.run(run_id)["status"], "stopped")
                for connection, response in viewers:
                    response.close(); connection.close()
                viewers.clear()
                deadline = time.monotonic() + 3
                while stream.snapshot()["viewers"] and time.monotonic() < deadline:
                    threading.Event().wait(0.02)
                self.assertEqual(stream.snapshot()["viewers"], 0)
        finally:
            release.set()
            if blocker.ident is not None:
                blocker.join(2)
            for connection, response in viewers:
                response.close(); connection.close()
            stream.close()

    def test_proxy_restricts_management_model_and_resource_parameters(self):
        self.assertEqual(request(self.proxy, "/api/version")[0], 401)
        self.assertEqual(request(self.proxy, "/api/pull", "POST", {"model": self.cfg.model}, self.proxy_token)[0], 403)
        payload = {"model": "not-allowed", "messages": [{"role": "user", "content": "Hi"}]}
        self.assertEqual(request(self.proxy, "/api/chat", "POST", payload, self.proxy_token)[0], 403)
        payload["model"] = self.cfg.model
        payload["options"] = {"num_ctx": 1000000}
        self.assertEqual(request(self.proxy, "/api/chat", "POST", payload, self.proxy_token)[0], 400)
        payload["options"] = {"num_ctx": 8192, "num_predict": 100, "num_gpu": 999}
        self.assertEqual(request(self.proxy, "/api/chat", "POST", payload, self.proxy_token)[0], 200)
        forwarded = self.upstream.payloads[-1]
        self.assertNotIn("num_gpu", forwarded["options"])
        self.assertIs(forwarded["stream"], False)
        status, data = request(self.proxy, "/api/tags", token=self.proxy_token)
        self.assertEqual([x["name"] for x in json.loads(data)["models"]], [self.cfg.model])

    def test_proxy_accepts_extended_and_unlimited_token_limits(self):
        for limit in (8192, 32768, -1):
            with self.subTest(limit=limit):
                payload = {"model": self.cfg.model, "messages": [{"role": "user", "content": "Hi"}],
                           "options": {"num_predict": limit}}
                self.assertEqual(request(self.proxy, "/api/chat", "POST", payload, self.proxy_token)[0], 200)
                self.assertEqual(self.upstream.payloads[-1]["options"]["num_predict"], limit)
        for limit in (0, -2, 32769, "8192", True):
            with self.subTest(invalid=limit):
                payload = {"model": self.cfg.model, "messages": [{"role": "user", "content": "Hi"}],
                           "options": {"num_predict": limit}}
                self.assertEqual(request(self.proxy, "/api/chat", "POST", payload, self.proxy_token)[0], 400)

    def test_real_model_client_posts_latest_image_and_schema_through_proxy(self):
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                   ollama_token=self.proxy_token)
        client = ModelClient(cfg)
        budget = {"current_step": 7, "max_steps": 30, "steps_remaining": 24}
        filesystem = self.agent.tools.filesystem_context([])
        result, metrics = client.decide("Test task", "state", "notes", [], self.desktop.capture(),
                                        threading.Event(), budget=budget, filesystem=filesystem)
        self.assertEqual(result["action"], "finish")
        self.assertEqual(metrics["eval_count"], 20)
        payload = self.upstream.payloads[-1]
        self.assertEqual(len(payload["messages"]), 2)
        self.assertIs(payload["think"], False)
        self.assertEqual(len(payload["messages"][1]["images"]), 1)
        self.assertEqual(json.loads(payload["messages"][1]["content"])["step_budget"], budget)
        self.assertEqual(json.loads(payload["messages"][1]["content"])["filesystem"], filesystem)
        cases = {c["properties"]["action"]["enum"][0]: c for c in payload["format"]["anyOf"]}
        self.assertIn("finish", cases)
        self.assertEqual(cases["click"]["properties"]["arguments"]["properties"]["y"]["maximum"], 1000)
        # The simulated model answers immediately; wait for the gateway's
        # request cleanup before sending another inference request.
        self.assertTrue(self.proxy.inference.acquire(timeout=2))
        self.proxy.inference.release()
        client.decide("Test task", "state", "notes", [], None, threading.Event())
        self.assertNotIn("images", self.upstream.payloads[-1]["messages"][1])

    def test_real_model_client_rejects_invalid_paste_keys_before_action_execution(self):
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                   ollama_token=self.proxy_token)
        self.upstream.decision = decision("type", {"text": "https://example.com", "paste_keys": []})
        with self.assertRaisesRegex(ModelError, "type.arguments.paste_keys"):
            ModelClient(cfg).decide("Type", "", "", [], self.desktop.capture(), threading.Event())

    def test_small_vision_model_disables_thinking_and_rejects_token_limited_actions(self):
        self.configure_models()
        cfg = dataclasses.replace(self.cfg, model="qwen3-vl:8b")
        client = ModelClient(cfg)
        result, _ = client.decide("Inspect the screenshot", "", "", [], self.desktop.capture(), threading.Event())
        self.assertEqual(result["action"], "finish")
        payload = self.upstream.payloads[-1]
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"]["num_predict"], cfg.num_predict)
        self.assertEqual(len(payload["messages"][1]["images"]), 1)
        self.assertTrue(self.proxy.inference.acquire(timeout=2))
        self.proxy.inference.release()
        # Even valid-looking JSON must not be dispatched after Ollama reports truncation.
        self.upstream.decision = decision("write_file", {"path": "must-not-run.txt", "content": "partial output"})
        self.upstream.reply_overrides = {"done_reason": "length"}
        with self.assertRaisesRegex(ModelError, f"done_reason=length.*num_predict={cfg.num_predict}.*No action was executed"):
            client.decide("Write the file", "", "", [], None, threading.Event())
        self.assertFalse((cfg.work_path / "must-not-run.txt").exists())

    def test_unlimited_length_reports_real_cause_and_ollama_counts(self):
        self.configure_models()
        cfg = dataclasses.replace(self.cfg, num_predict=-1)
        self.upstream.reply_overrides = {
            "done_reason": "length", "eval_count": 4096, "prompt_eval_count": 8000}
        with self.assertRaisesRegex(ModelError, "Output tokens are already unlimited") as caught:
            ModelClient(cfg).decide("Small action", "", "", [], None, threading.Event(),
                                    on_chunk=lambda fragment: None)
        self.assertIn("4096 tokens", str(caught.exception))
        self.assertIn("8000 tokens", str(caught.exception))
        self.assertIs(self.upstream.payloads[-1]["options"]["num_predict"], -1)

    def test_checklist_mode_adds_plan_schema_and_evidence_context_only_when_enabled(self):
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                  ollama_token=self.proxy_token, task_checklist=True)
        client = ModelClient(cfg)
        task = "List the source directory, write the report, then verify it"
        client.decide(task, "", "", [], None, threading.Event(), checklist=[])
        payload = self.upstream.payloads[-1]
        cases = {c["properties"]["action"]["enum"][0]: c for c in payload["format"]["anyOf"]}
        self.assertEqual(set(cases), {"set_checklist", "finish"})
        self.assertIn("checklist_updates", cases["finish"]["properties"])
        self.assertIn("checklist mode is ON", payload["messages"][0]["content"])
        self.assertTrue(self.proxy.inference.acquire(timeout=2))
        self.proxy.inference.release()
        items = [{"id": "list", "title": "List source", "check": {"kind": "directory_listed", "target": "source", "expected": ""},
                  "status": "verified", "evidence_ids": [9], "note": ""}]
        events = [{"id": 9, "step": 2, "kind": "action", "data": {
            "decision": decision("list_files", {"path": "source"}),
            "result": {"ok": True, "entries": [], "path": "source"}}}]
        client.decide(task, "", "", events, None, threading.Event(), checklist=items)
        payload = self.upstream.payloads[-1]
        context = json.loads(payload["messages"][1]["content"])
        self.assertEqual(context["task_checklist"], items)
        self.assertEqual(context["recent_events"][0]["event_id"], 9)
        self.assertEqual(list(context)[-1], "task")
        self.assertIn("shell", {c["properties"]["action"]["enum"][0] for c in payload["format"]["anyOf"]})
        self.assertTrue(self.proxy.inference.acquire(timeout=2))
        self.proxy.inference.release()
        off = ModelClient(dataclasses.replace(cfg, task_checklist=False))
        off.decide(task, "", "", events, None, threading.Event(), checklist=items)
        payload = self.upstream.payloads[-1]
        context = json.loads(payload["messages"][1]["content"])
        self.assertNotIn("task_checklist", context)
        self.assertNotIn("event_id", context["recent_events"][0])
        self.assertNotIn("set_checklist", payload["messages"][0]["content"])
        self.assertTrue(all("checklist_updates" not in c["properties"] for c in payload["format"]["anyOf"]))

    def test_model_context_uses_observed_results_and_keeps_current_task_last(self):
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                   ollama_token=self.proxy_token)
        failed_code = "class BrokenParser: " + "invalid syntax; " * 75
        failed = decision("shell", {"command": failed_code}, "Falsely claimed the report was finished")
        events = [{"step": 2, "kind": "action", "data": {"decision": failed,
                  "result": {"ok": False, "exit_code": 1,
                             "output": 'File "<string>", line 1\n' + failed_code + '\nSyntaxError: invalid syntax'}}},
                  {"step": 3, "kind": "action", "data": {
                      "decision": decision("write_file", {"path": "report.md", "content": "old report"}),
                      "result": {"ok": True, "path": "report.md", "characters": 10}}}]
        ModelClient(cfg).decide("Write a NEW script with real newlines", "Observed state", "", events,
                                None, threading.Event())
        message = self.upstream.payloads[-1]["messages"][1]
        context = json.loads(message["content"])
        self.assertEqual(list(context)[-1], "task")
        self.assertIsNone(context["screen"])
        self.assertNotIn(failed_code, message["content"])
        self.assertNotIn("Falsely claimed", message["content"])
        self.assertNotIn("old report", message["content"])
        first = context["recent_events"][0]["data"]
        self.assertTrue(first["failed_action"])
        self.assertTrue(first["arguments"]["command_truncated"])
        self.assertIn("SyntaxError: invalid syntax", first["result"]["output"])
        self.assertEqual(context["recent_events"][1]["data"]["arguments"]["path"], "report.md")
        # The full evidence stays intact in the original events / database.
        self.assertEqual(events[0]["data"]["decision"]["arguments"]["command"], failed_code)

    def test_failure_context_keeps_short_commands_tail_errors_and_recovery_before_current_task(self):
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                   ollama_token=self.proxy_token)
        run_id = self.store.create_run("Recovery context", 20, cfg.model)
        output = "older output\n" * 2000 + "Traceback:\n" + "broken source" * 1000 + "\nAttributeError: missing_field"
        self.store.event(run_id, 1, "action", {"decision": decision("shell", {"command": "python3 parser.py", "cwd": "project"}),
             "result": {"ok": False, "exit_code": 1, "timed_out": False, "output": output}})
        events = self.agent._context_events(run_id)
        recovery = {"failed_attempts": [{"action": "shell", "arguments": {"command": "python3 parser.py"},
                                        "failure_count": 4, "error": "AttributeError: missing_field"}]}
        ModelClient(cfg).decide("Fix the parser and verify it", "", "", events, None, threading.Event(), recovery=recovery)
        context = json.loads(self.upstream.payloads[-1]["messages"][1]["content"])
        failed = context["recent_events"][0]["data"]
        self.assertEqual(failed["arguments"], {"command": "python3 parser.py", "cwd": "project"})
        self.assertEqual(failed["result"]["exit_code"], 1)
        self.assertIn("AttributeError: missing_field", failed["result"]["output"])
        self.assertNotIn("broken source" * 1000, failed["result"]["output"])
        self.assertEqual(context["recovery"], recovery)
        self.assertEqual(list(context)[-1], "task")
        self.assertEqual(self.store.events(run_id)[0]["data"]["result"]["output"], output)

    def test_model_diagnostic_changes_only_history_without_executing_actions_or_recovering_runs(self):
        run_id = self.store.create_run("Failed extraction", 10, self.cfg.model)
        self.store.event(run_id, 1, "action", {"decision": decision("shell", {"command": "broken parser"}),
                                               "result": {"ok": False, "output": "SyntaxError"}})
        target = Path(self.temp.name) / "must-not-exist.py"
        self.upstream.decision = decision("write_file", {"path": str(target), "content": "print('Example Domain')\n"})
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                   ollama_token=self.proxy_token, task_checklist=True)
        before = self.store.run(run_id)
        report = diagnostic_module.collect_report(cfg, run_id)
        self.assertTrue(cfg.task_checklist)
        self.assertIn("checklist is disabled", report["note"])
        self.assertEqual(self.store.run(run_id), before)
        self.assertFalse(target.exists())
        self.assertEqual(len(report["cases"]), 2)
        self.assertTrue(all(case["validation"]["syntax_valid"] for case in report["cases"]))
        fresh, history = [case["request"] for case in report["cases"]]
        self.assertIn("write_file", {c["properties"]["action"]["enum"][0] for c in fresh["format"]["anyOf"]})
        fresh_context = json.loads(fresh["messages"][1]["content"])
        history_context = json.loads(history["messages"][1]["content"])
        self.assertEqual(fresh_context["recent_events"], [])
        self.assertEqual(history_context["recent_events"][0]["data"]["result"]["output"], "SyntaxError")
        history_context["recent_events"] = []
        self.assertEqual(fresh_context, history_context)
        history["messages"][1]["content"] = fresh["messages"][1]["content"]
        self.assertEqual(fresh, history)
        self.assertNotIn(self.proxy_token, json.dumps(report))

    def test_model_diagnostic_retains_invalid_response_and_detects_bad_python(self):
        run_id = self.store.create_run("Failed run", 10, self.cfg.model)
        self.store.event(run_id, 1, "error", {"message": "Example failure"})
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                   ollama_token=self.proxy_token)
        self.upstream.decision = decision("type", {"text": "x", "paste_keys": []})
        report = diagnostic_module.collect_report(cfg, run_id)
        self.assertTrue(all("error" in case and case["response"] for case in report["cases"]))
        review = diagnostic_module.check_proposal(decision("write_file", {"path": "x.py", "content": "import sys; class P: pass"}))
        self.assertFalse(review["syntax_valid"])

    def test_recovery_format_comparison_preserves_evidence_source_and_run(self):
        run_id = self.store.create_run("Fix the attribute, then print False", 6, self.cfg.model)
        self.store.update(run_id, step=6, state="Script written; next run it", status="limit_reached")
        self.store.event(run_id, 3, "action", {"decision": decision("shell", {"command": "python3 parser.py"}),
                                               "result": {"ok": False, "output": "NameError: parser"}})
        self.cfg.work_path.mkdir(parents=True, exist_ok=True)
        path = self.cfg.work_path / "parser.py"
        original = "print(parser.in_paragraph)\nparser.in_paragraph = False\n"
        path.write_text(original)
        self.upstream.decision = decision("write_file", {"path": str(path), "content": "print(False)\n"})
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                   ollama_token=self.proxy_token)
        before = self.store.run(run_id)
        report = diagnostic_module.collect_format_report(cfg, run_id)
        self.assertEqual(path.read_text(), original)
        self.assertEqual(self.store.run(run_id), before)
        packed, dialogue = [case["request"] for case in report["cases"]]
        for key in ("model", "format", "options", "stream"):
            self.assertEqual(packed[key], dialogue[key])
        self.assertEqual(packed["messages"][0], dialogue["messages"][0])
        current = json.loads(packed["messages"][1]["content"])
        history = current["recent_events"]
        self.assertEqual(dialogue["messages"][1]["role"], "assistant")
        prior = json.loads(dialogue["messages"][1]["content"])
        observed = json.loads(dialogue["messages"][2]["content"])
        self.assertEqual(prior["previous_action"], history[0]["data"]["action"])
        self.assertEqual(observed["observation"]["result"], history[0]["data"]["result"])
        current["recent_events"] = []
        self.assertEqual(current, json.loads(dialogue["messages"][-1]["content"]))
        self.assertIn(original, report["source"])

    def test_dashboard_exposes_automatic_pause_reason_and_resume_control(self):
        self.agent.config = dataclasses.replace(self.agent.config, stall_attempts=3)
        self.agent.model = FakeModel([decision("screenshot", {})] * 3 +
                                     [decision("finish", {"summary": "Recovered"})])
        controls = {"X-Treic-Agent": "1"}
        status, body = request(self.api, "/api/start", "POST", {"task": "Loop", "max_steps": 100},
                               self.cfg.dashboard_token, controls)
        self.assertEqual(status, 201)
        run_id = json.loads(body)["run_id"]
        deadline = time.monotonic() + 3
        while self.store.run(run_id)["status"] != "paused" and time.monotonic() < deadline:
            time.sleep(0.01)
        status, body = request(self.api, "/api/state", token=self.cfg.dashboard_token)
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(state["current"]["status"], "paused")
        self.assertIn("Automatically paused", state["pause_reason"])
        self.assertEqual(state["current"]["summary"], state["pause_reason"])
        self.assertEqual(request(self.api, "/api/resume", "POST", {}, self.cfg.dashboard_token, controls)[0], 200)
        self.agent.thread.join(3)
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertIsNone(self.agent.snapshot()["pause_reason"])

    def test_stall_controls_are_authenticated_saved_and_failed_changes_preserve_settings(self):
        path = self.configure_models()
        controls = {"X-Treic-Agent": "1"}
        payload = {"stall_attempts": 7}
        self.assertEqual(request(self.api, "/assets/stall-controls.js")[0], 200)
        self.assertEqual(request(self.api, "/api/stall", "POST", payload)[0], 401)
        self.assertEqual(request(self.api, "/api/stall", "POST", payload, self.cfg.dashboard_token)[0], 403)
        self.assertEqual(request(self.api, "/api/stall", "POST", payload, self.cfg.dashboard_token,
                                 {**controls, "Origin": "https://attacker.example"})[0], 403)
        status, body = request(self.api, "/api/stall", "POST", payload, self.cfg.dashboard_token, controls)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), payload)
        self.assertEqual(load_config(path), dataclasses.replace(self.cfg, stall_attempts=7))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        original = path.read_bytes()
        for invalid in (None, True, -1, 1001, 1.5, "7"):
            self.assertEqual(request(self.api, "/api/stall", "POST", {"stall_attempts": invalid},
                                     self.cfg.dashboard_token, controls)[0], 400)
        with mock.patch("treics_agent.config.os.replace", side_effect=OSError("disk error")):
            self.assertEqual(request(self.api, "/api/stall", "POST", {"stall_attempts": 0},
                                     self.cfg.dashboard_token, controls)[0], 400)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.agent.config.stall_attempts, 7)
        self.assertFalse(self.agent.stall_setting_saving)
        state = json.loads(request(self.api, "/api/state", token=self.cfg.dashboard_token)[1])
        self.assertEqual(state["stall_attempts"], 7)
        self.assertEqual(request(self.api, "/api/stall", "POST", {"stall_attempts": 0},
                                 self.cfg.dashboard_token, controls)[0], 200)
        self.assertEqual(load_config(path).stall_attempts, 0)

    def test_manual_control_can_be_applied_to_an_automatically_paused_task(self):
        path = self.configure_models()
        controls = {"X-Treic-Agent": "1"}
        self.assertEqual(request(self.api, "/api/stall", "POST", {"stall_attempts": 3},
                                 self.cfg.dashboard_token, controls)[0], 200)
        self.agent.model = FakeModel([decision("screenshot", {})] * 10 + [decision("finish", {"summary": "Done"})])
        run_id = self.agent.start("Change stall controls during a task", 30)
        deadline = time.monotonic() + 3
        while self.store.run(run_id)["status"] != "paused" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.store.run(run_id)["status"], "paused")
        self.assertEqual(self.agent.model.calls, 3)
        self.assertEqual(request(self.api, "/api/stall", "POST", {"stall_attempts": 0},
                                 self.cfg.dashboard_token, controls)[0], 200)
        self.assertEqual(self.store.run(run_id)["status"], "paused", "Saving resumed a task without Resume")
        self.assertEqual(load_config(path).stall_attempts, 0)
        self.assertEqual(request(self.api, "/api/resume", "POST", {}, self.cfg.dashboard_token, controls)[0], 200)
        self.agent.thread.join(5)
        self.assertFalse(self.agent.thread.is_alive())
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertEqual(self.agent.model.calls, 11)
        self.assertTrue(any(event["kind"] == "stall_control" for event in self.store.events(run_id)))

    def test_settings_save_keeps_controls_responsive_and_serializes_configuration_changes(self):
        self.configure_models()
        entered, release = threading.Event(), threading.Event()
        from treics_agent.config import set_stall_attempts
        def blocked_save(*args):
            entered.set()
            release.wait(4)
            return set_stall_attempts(*args)
        controls = {"X-Treic-Agent": "1"}
        result = []
        with mock.patch("treics_agent.agent.set_stall_attempts", side_effect=blocked_save):
            worker = threading.Thread(target=lambda: result.append(request(self.api, "/api/stall", "POST",
                {"stall_attempts": 8}, self.cfg.dashboard_token, controls, timeout=6)))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                state = json.loads(request(self.api, "/api/state", token=self.cfg.dashboard_token, timeout=1)[1])
                self.assertTrue(state["stall_setting_saving"])
                self.assertEqual(request(self.api, "/api/model", "POST", {"model": "qwen3-vl:8b"},
                                         self.cfg.dashboard_token, controls, timeout=1)[0], 400)
                self.assertEqual(request(self.api, "/api/stall", "POST", {"stall_attempts": 0},
                                         self.cfg.dashboard_token, controls, timeout=1)[0], 400)
                self.assertEqual(request(self.api, "/api/stop", "POST", {}, self.cfg.dashboard_token,
                                         controls, timeout=1)[0], 200)
            finally:
                release.set()
                worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0], 200)
        self.assertEqual(self.agent.config.stall_attempts, 8)

    def test_changing_policy_off_and_back_during_observation_prevents_a_stale_pause(self):
        self.configure_models()
        controls = {"X-Treic-Agent": "1"}
        self.assertEqual(request(self.api, "/api/stall", "POST", {"stall_attempts": 3},
                                 self.cfg.dashboard_token, controls)[0], 200)
        self.agent.model = FakeModel([decision("screenshot", {})] * 3 + [decision("finish", {"summary": "Done"})])
        entered, release = threading.Event(), threading.Event()
        original = ProgressGuard.same_screen
        comparisons = []
        def comparison(before, after):
            comparisons.append(1)
            if len(comparisons) == 3:
                entered.set()
                release.wait(4)
            return original(before, after)
        with mock.patch.object(ProgressGuard, "same_screen", side_effect=comparison):
            run_id = self.agent.start("Change policy before a pause is published", 20)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(request(self.api, "/api/stall", "POST", {"stall_attempts": 0},
                                         self.cfg.dashboard_token, controls, timeout=1)[0], 200)
                self.assertEqual(request(self.api, "/api/stall", "POST", {"stall_attempts": 3},
                                         self.cfg.dashboard_token, controls, timeout=1)[0], 200)
            finally:
                release.set()
            self.agent.thread.join(3)
        self.assertFalse(self.agent.thread.is_alive())
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertFalse(any(event["kind"] == "stalled" for event in self.store.events(run_id)))

    def test_cancel_interrupts_a_response_body_and_releases_proxy_slot(self):
        self.upstream.block = True
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                   ollama_token=self.proxy_token)
        client = ModelClient(cfg)
        stopped = threading.Event()
        caught = []
        def run():
            try:
                client.decide("Wait", "", "", [], None, stopped)
            except Exception as exc:
                caught.append(type(exc).__name__)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(self.upstream.entered.wait(2))
        stopped.set()
        client.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive(), "Model cancellation left the socket read blocked")
        self.assertEqual(caught, ["Cancelled"])
        deadline = time.monotonic() + 2
        acquired = False
        while time.monotonic() < deadline:
            acquired = self.proxy.inference.acquire(blocking=False)
            if acquired:
                self.proxy.inference.release()
                break
            time.sleep(0.01)
        self.assertTrue(acquired, "Proxy inference slot did not recover after disconnect")

    def test_vm_cloud_config_is_valid_yaml_and_guest_only_credentials(self):
        import tomllib
        cloud = vm_module.cloud_config("ssh-ed25519 AAAA sample", "proxy-secret", "dashboard-secret",
                                       "host-private-key", "ssh-ed25519 BBBB")
        config = tomllib.loads(cloud["write_files"][0]["content"])
        self.assertEqual(config["ollama_url"], "http://192.168.152.1:11435")
        self.assertEqual(config["dashboard_token"], "dashboard-secret")
        self.assertEqual(cloud["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 AAAA sample"])
        serialized = json.dumps(cloud)
        self.assertNotIn("treic-agent-vm", serialized)  # Host SSH private key is never injected.
        self.assertEqual(cloud["users"][0]["sudo"], ["ALL=(ALL) NOPASSWD:ALL"])
        self.assertFalse(cloud["ssh_pwauth"])


if __name__ == "__main__":
    unittest.main()
