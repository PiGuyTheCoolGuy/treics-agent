from __future__ import annotations

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from .agent import Agent


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, agent: Agent):
        if len(agent.config.dashboard_token) < 24 or "REPLACE_" in agent.config.dashboard_token:
            raise ValueError("Set a random dashboard_token of at least 24 characters before serving.")
        self.agent = agent
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: AgentHTTPServer
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        # Do not log headers, tokens, task bodies, terminal text, or query strings.
        pass

    def send(self, status: int, body, content_type: str = "application/json") -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self) -> bool:
        expected = "Bearer " + self.server.agent.config.dashboard_token
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual.encode(), expected.encode()):
            self.send(401, {"error": "Enter the dashboard token from your guest configuration."})
            return False
        # No CORS. Reject cross-origin browser calls even if the token has been provided.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in ("http://" + host, "https://" + host):
            self.send(403, {"error": "Cross-origin requests are not accepted."})
            return False
        return True

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self.send(200, files("treics_agent").joinpath("static/index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if not self.authorized():
            return
        agent = self.server.agent
        if path == "/api/state":
            self.send(200, agent.snapshot())
        elif match := re.fullmatch(r"/api/runs/([a-f0-9]{32})", path):
            run_id = match.group(1)
            run = agent.store.run(run_id)
            self.send(200 if run else 404, {"run": run, "events": agent.store.events(run_id, 300)} if run
                      else {"error": "Run not found."})
        elif path == "/api/screen":
            with agent.tools.desktop.lock:
                image = agent.tools.desktop.image
            if image:
                self.send(200, image, "image/jpeg")
            else:
                self.send(404, {"error": "No screenshot yet. Use Capture desktop or start a task."})
        elif path == "/api/memory":
            self.send(200, {"text": agent.store.memory()})
        else:
            self.send(404, {"error": "Unknown endpoint."})

    def do_POST(self) -> None:
        if not self.authorized():
            return
        if self.headers.get("X-Treic-Agent") != "1":
            self.send(403, {"error": "Missing X-Treic-Agent control header."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 64000:
                self.send(413, {"error": "Request body is too large."})
                return
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            agent = self.server.agent
            path = urlsplit(self.path).path
            if path == "/api/start":
                run_id = agent.start(payload.get("task"), payload.get("max_steps"))
                self.send(201, {"run_id": run_id})
            elif path in ("/api/pause", "/api/resume", "/api/stop"):
                getattr(agent, path.rsplit("/", 1)[1])()
                self.send(200, {"ok": True})
            elif path == "/api/preview":
                with agent.lock:
                    if agent.thread and agent.thread.is_alive():
                        raise ValueError("Desktop capture updates automatically during a run.")
                    frame = agent.tools.desktop.capture()
                self.send(200, {k: v for k, v in frame.items() if k != "base64"})
            elif path == "/api/memory":
                agent.store.remember(payload.get("text"))
                self.send(200, {"ok": True})
            else:
                self.send(404, {"error": "Unknown endpoint."})
        except (ValueError, OSError, TimeoutError) as exc:
            self.send(400, {"error": str(exc)[:1500]})
        except Exception as exc:
            self.send(500, {"error": str(exc)[:1500]})
