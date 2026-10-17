from __future__ import annotations

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from .agent import Agent
from .model import ModelError
from .stream import BOUNDARY, DesktopStream


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, agent: Agent):
        if len(agent.config.dashboard_token) < 24 or "REPLACE_" in agent.config.dashboard_token:
            raise ValueError("Set a random dashboard_token of at least 24 characters before serving.")
        self.agent = agent
        self.desktop_stream = DesktopStream(agent.config)
        super().__init__(address, Handler)

    def server_close(self) -> None:
        self.desktop_stream.close()
        super().server_close()


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

    def stream_desktop(self) -> None:
        stream = self.server.desktop_stream
        try:
            stream.subscribe()
        except (ValueError, RuntimeError) as exc:
            self.send(429 if isinstance(exc, ValueError) else 503, {"error": str(exc)})
            return
        try:
            self.connection.settimeout(5)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + BOUNDARY)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            sequence = 0
            while not stream.closed:
                frame = stream.next_frame(sequence)
                if stream.closed:
                    break
                if frame:
                    sequence = frame.sequence
                    body = frame.jpeg
                    content_type = "image/jpeg"
                    metadata = (f"X-Desktop-Width: {frame.desktop_width}\r\n"
                                f"X-Desktop-Height: {frame.desktop_height}\r\n"
                                f"X-Captured-At: {frame.captured_at}\r\n"
                                f"X-Frame-Rate: {stream.config.stream_fps}\r\n")
                else:
                    # A heartbeat also detects disconnected viewers while capture retries.
                    body = json.dumps({"message": stream.snapshot()["error"] or "Connecting to desktop…"}).encode()
                    content_type, metadata = "application/json", ""
                header = (f"--{BOUNDARY}\r\nContent-Type: {content_type}\r\n"
                          f"Content-Length: {len(body)}\r\n{metadata}\r\n").encode()
                self.wfile.write(header)
                self.wfile.write(body)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (OSError, TimeoutError):
            pass
        finally:
            stream.unsubscribe()

    def stream_model_output(self) -> None:
        agent = self.server.agent
        self.connection.settimeout(12)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        sequence = -1
        try:
            while True:
                with agent.condition:
                    agent.condition.wait_for(
                        lambda: agent.model_output["sequence"] != sequence, timeout=10)
                    state = dict(agent.model_output)
                if state["sequence"] != sequence:
                    sequence = state["sequence"]
                    payload = json.dumps(state, ensure_ascii=False)
                    self.wfile.write(("data: " + payload + "\n\n").encode())
                else:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (OSError, TimeoutError):
            pass

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self.send(200, files("treics_agent").joinpath("static/index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path in ("/assets/desktop-stream.js", "/assets/model-selector.js", "/assets/stall-controls.js", "/assets/model-output.js"):
            self.send(200, files("treics_agent").joinpath("static", path.rsplit("/", 1)[1]).read_bytes(),
                      "application/javascript; charset=utf-8")
            return
        if not self.authorized():
            return
        agent = self.server.agent
        if path == "/api/state":
            self.send(200, {**agent.snapshot(), "desktop_stream": self.server.desktop_stream.snapshot()})
        elif path == "/api/models":
            try:
                self.send(200, agent.available_models())
            except ModelError as exc:
                self.send(502, {"error": str(exc)[:1500]})
        elif path == "/api/desktop/stream":
            self.stream_desktop()
        elif path == "/api/model/output/stream":
            self.stream_model_output()
        elif match := re.fullmatch(r"/api/runs/([a-f0-9]{32})", path):
            run_id = match.group(1)
            run = agent.store.run(run_id)
            self.send(200 if run else 404, {"run": run, "events": agent.store.events(run_id, 300)} if run
                      else {"error": "Run not found."})
        elif path == "/api/screen":
            image = agent.tools.desktop.preview_image()
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
            elif path == "/api/model":
                self.send(200, agent.switch_model(payload.get("model")))
            elif path == "/api/stall":
                self.send(200, agent.set_stall_control(payload.get("stall_attempts")))
            elif path in ("/api/pause", "/api/resume", "/api/stop"):
                getattr(agent, path.rsplit("/", 1)[1])()
                self.send(200, {"ok": True})
            elif path == "/api/preview":
                frame = agent.capture_preview()
                self.send(200, {k: v for k, v in frame.items() if k != "base64"})
            elif path == "/api/memory":
                agent.store.remember(payload.get("text"))
                self.send(200, {"ok": True})
            else:
                self.send(404, {"error": "Unknown endpoint."})
        except ModelError as exc:
            self.send(502, {"error": str(exc)[:1500]})
        except (ValueError, OSError, TimeoutError) as exc:
            self.send(400, {"error": str(exc)[:1500]})
        except Exception as exc:
            self.send(500, {"error": str(exc)[:1500]})
