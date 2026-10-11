from __future__ import annotations

import dataclasses
import http.client
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from treics_agent.agent import Agent
from treics_agent.config import Config
from treics_agent.model import Cancelled, ModelClient, ModelError
from treics_agent.server import AgentHTTPServer
from treics_agent.storage import Store
from treics_agent.tools import Tools
from test_agent import FakeDesktop, FakeModel, decision


def script_module(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proxy_module = script_module("model-proxy")
vm_module = script_module("vm-config")


def request(server, path, method="GET", body=None, token=None, extra=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    headers.update(extra or {})
    connection.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, data


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, data):
        data = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.server.block:
            self.server.entered.set()
            self.server.release.wait(5)
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/api/tags":
            self.reply({"models": [{"name": "qwen3-vl:30b-a3b-instruct"}, {"name": "private-model"}]})
        else:
            self.reply({"version": "test"})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.payloads.append(payload)
        if self.path == "/api/show":
            return self.reply({"capabilities": ["completion", "vision"]})
        self.reply({"message": {"role": "assistant", "content": json.dumps(self.server.decision)},
                    "done": True, "total_duration": 1000000000, "eval_count": 20})


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
        self.upstream.block = False
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

    def test_real_model_client_posts_latest_image_and_schema_through_proxy(self):
        cfg = dataclasses.replace(self.cfg, ollama_url="http://%s:%s" % self.proxy.server_address,
                                   ollama_token=self.proxy_token)
        client = ModelClient(cfg)
        result, metrics = client.decide("Test task", "state", "notes", [], self.desktop.capture(), threading.Event())
        self.assertEqual(result["action"], "finish")
        self.assertEqual(metrics["eval_count"], 20)
        payload = self.upstream.payloads[-1]
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(len(payload["messages"][1]["images"]), 1)
        self.assertEqual(payload["format"]["properties"]["action"]["enum"][-1], "finish")
        client.decide("Test task", "state", "notes", [], None, threading.Event())
        self.assertNotIn("images", self.upstream.payloads[-1]["messages"][1])

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
