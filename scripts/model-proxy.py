#!/usr/bin/env python3
"""Authenticated, model-only gateway; never exposes Ollama's model-management API."""
from __future__ import annotations

import hmac
import http.client
import json
import os
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, token: str, models: set[str], upstream=("127.0.0.1", 11434)):
        if len(token) < 24:
            raise ValueError("MODEL_PROXY_TOKEN must contain at least 24 characters")
        self.token = token
        self.models = models
        self.upstream = upstream
        self.inference = threading.BoundedSemaphore(1)
        super().__init__(address, ProxyHandler)


class ProxyHandler(BaseHTTPRequestHandler):
    server: ProxyServer

    def log_message(self, format, *args):
        pass

    def respond(self, status, data):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def proxy(self, method):
        expected = ("Bearer " + self.server.token).encode()
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
            return self.respond(401, {"error": "Model gateway authentication failed"})
        allowed = {"GET": {"/api/version", "/api/tags"}, "POST": {"/api/chat", "/api/show"}}
        if self.path not in allowed[method]:
            return self.respond(403, {"error": "This model gateway exposes only chat, show, tags and version"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 12 * 1024 * 1024:
                return self.respond(413, {"error": "Request exceeds 12 MiB"})
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Body must be a JSON object")
            if method == "POST" and payload.get("model") not in self.server.models:
                return self.respond(403, {"error": "Model is not in the gateway allowlist"})
            is_chat = self.path == "/api/chat"
            if is_chat:
                if not isinstance(payload.get("messages"), list) or not 1 <= len(payload["messages"]) <= 30:
                    raise ValueError("Provide 1–30 chat messages")
                requested = payload.get("options", {})
                if not isinstance(requested, dict):
                    raise ValueError("options must be an object")
                options = {}
                for name, default, low, high in (("num_ctx", 8192, 4096, 16384),
                    ("num_predict", 1600, 1, 4096), ("num_thread", 16, 1, 20)):
                    value = requested.get(name, default)
                    if type(value) is not int or not low <= value <= high:
                        raise ValueError(f"{name} must be an integer from {low} to {high}")
                    options[name] = value
                temperature = requested.get("temperature", 0.1)
                if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
                    raise ValueError("temperature must be between 0 and 2")
                options["temperature"] = temperature
                payload["options"] = options
                payload["keep_alive"] = "30m"
                payload["stream"] = payload.get("stream") is True
                if not self.server.inference.acquire(blocking=False):
                    return self.respond(429, {"error": "A model request is still active; retry shortly"})
        except (ValueError, OSError) as exc:
            return self.respond(400, {"error": str(exc)[:300]})

        conn = http.client.HTTPConnection(*self.server.upstream, timeout=10)
        finished = threading.Event()
        disconnected = threading.Event()

        def watch_disconnect():
            while not finished.wait(0.2):
                try:
                    ready, _, _ = select.select([self.connection], [], [], 0)
                    if ready and not self.connection.recv(1, socket.MSG_PEEK):
                        disconnected.set()
                        if upstream_socket:
                            try:
                                upstream_socket.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                        conn.close()
                        return
                except (OSError, ValueError):
                    return

        watcher = None
        upstream_socket = None
        try:
            conn.connect()
            conn.sock.settimeout(1200)
            upstream_socket = conn.sock
            watcher = threading.Thread(target=watch_disconnect, daemon=True)
            watcher.start()
            conn.request(method, self.path, json.dumps(payload) if method == "POST" else None,
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            if is_chat and payload["stream"] and response.status == 200:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                total = 0
                while not disconnected.is_set():
                    line = response.readline(4 * 1024 * 1024 + 1)
                    if not line:
                        break
                    total += len(line)
                    if total > 4 * 1024 * 1024:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
                return
            data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("Upstream response exceeds 4 MiB")
            if self.path == "/api/tags" and response.status == 200:
                value = json.loads(data)
                value["models"] = [m for m in value.get("models", []) if m.get("name") in self.server.models]
                data = json.dumps(value).encode()
            if not disconnected.is_set():
                self.respond(response.status, data)
        except (OSError, ValueError, http.client.HTTPException, AttributeError) as exc:
            if not disconnected.is_set():
                self.respond(502, {"error": f"Host Ollama unavailable: {exc}"[:500]})
        finally:
            finished.set()
            conn.close()
            if watcher:
                watcher.join(timeout=1)
            if is_chat:
                self.server.inference.release()

    def do_GET(self):
        self.proxy("GET")

    def do_POST(self):
        self.proxy("POST")


if __name__ == "__main__":
    ProxyServer((os.environ.get("MODEL_PROXY_BIND", "192.168.152.1"), 11435),
                os.environ["MODEL_PROXY_TOKEN"],
                set(os.environ.get("MODEL_PROXY_MODELS", "qwen3-vl:30b-a3b-instruct,qwen3-vl:8b").split(","))).serve_forever()
