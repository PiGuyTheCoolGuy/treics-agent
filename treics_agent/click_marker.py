from __future__ import annotations

from contextlib import contextmanager
import json
import os
import selectors
import subprocess
import sys
import threading
import time

from .config import Config


class ClickMarker:
    """Bound X11 marker work in a disposable helper process."""

    TIMEOUT = 3.0

    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.RLock()
        self.process_lock = threading.Lock()
        self.pending = threading.Event()
        self.process: subprocess.Popen | None = None
        self.visible = False
        self.window_ids: list[int] = []

    def request(self, action: str, **arguments) -> None:
        with self.lock:
            self.pending.set()
            try:
                with self.process_lock:
                    if self.process is None:
                        self.process = subprocess.Popen(
                            [sys.executable, "-m", "treics_agent.click_marker", "--worker"],
                            env=self.config.desktop_env(), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
                    process = self.process
                deadline = time.monotonic() + self.TIMEOUT
                process.stdin.write((json.dumps({"action": action, **arguments}) + "\n").encode())
                process.stdin.flush()
                output = bytearray()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while b"\n" not in output:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise TimeoutError("Live click marker timed out; the dashboard preview still marks clicks.")
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            raise RuntimeError("Live click marker worker exited before responding.")
                        output.extend(chunk)
                        if len(output) > 4096:
                            raise RuntimeError("Unexpected live click marker response.")
                response = json.loads(output)
                if response.get("error"):
                    raise RuntimeError(response["error"])
                self.visible = response["visible"]
                self.window_ids = response["window_ids"]
            except Exception:
                self.close()
                raise
            finally:
                self.pending.clear()

    def show(self, x: int, y: int) -> None:
        self.request("show", x=x, y=y)

    @contextmanager
    def hidden(self):
        restore = self.visible
        if restore:
            try:
                self.request("hide")
            except Exception:
                # Closing the worker destroys its windows, leaving a clean capture.
                restore = False
        try:
            yield
        finally:
            if restore and self.process is not None:
                try:
                    self.request("restore")
                except Exception:
                    pass

    def cancel(self, force: bool = False) -> None:
        # Stop must not wait for the request lock held by a stalled X11 call.
        with self.process_lock:
            process = self.process
            if process is not None and (force or self.pending.is_set()):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    def close(self) -> None:
        self.cancel(force=True)
        with self.lock, self.process_lock:
            process, self.process = self.process, None
            self.visible = False
            self.window_ids = []
            if process is not None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                for pipe in (process.stdin, process.stdout):
                    try:
                        pipe.close()
                    except OSError:
                        pass


class NativeClickMarker:
    """Small shaped X11 windows with empty input regions; never take focus."""

    RADIUS = 20
    # A hollow square around the click, with four separated crosshair arms.
    OUTLINE = [(10, 10, 21, 3), (10, 28, 21, 3), (10, 13, 3, 15), (28, 13, 3, 15),
               (19, 0, 3, 8), (19, 33, 3, 8), (0, 19, 8, 3), (33, 19, 8, 3)]
    COLOR = [(11, 11, 19, 1), (11, 29, 19, 1), (11, 12, 1, 17), (29, 12, 1, 17),
             (20, 1, 1, 6), (20, 34, 1, 6), (1, 20, 6, 1), (34, 20, 6, 1)]

    def __init__(self, config: Config):
        self.config = config
        self.display = None
        self.windows = []
        self.visible = False

    def _connect(self) -> None:
        from Xlib import X, display
        from Xlib.ext import shape

        # This worker already inherited the explicit desktop environment.
        connection = display.Display(self.config.display)
        self.display = connection
        if not connection.has_extension("SHAPE"):
            raise RuntimeError("The desktop does not support the X11 SHAPE extension.")
        version = connection.shape_query_version()
        if (version.major_version, version.minor_version) < (1, 1):
            raise RuntimeError("The click-through marker requires X11 SHAPE 1.1.")
        screen = connection.screen()
        for color, rectangles in (("white", self.OUTLINE), ("#ff4040", self.COLOR)):
            pixel = screen.default_colormap.alloc_named_color(color).pixel
            window = screen.root.create_window(
                0, 0, 41, 41, 0, screen.root_depth, X.InputOutput, X.CopyFromParent,
                background_pixel=pixel, override_redirect=True)
            self.windows.append(window)
            window.set_wm_name("Treic's Agent last click")
            window.shape_rectangles(shape.SO.Set, shape.SK.Bounding, X.Unsorted, 0, 0, rectangles)
            # Empty input shapes let *all* mouse events reach the underlying app.
            window.shape_rectangles(shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, [])
        connection.sync()

    def show(self, x: int, y: int) -> None:
        from Xlib import X

        if self.display is None:
            self._connect()
        for window in self.windows:
            window.configure(x=x - self.RADIUS, y=y - self.RADIUS, stack_mode=X.Above)
            window.map()
        self.display.sync()
        self.visible = True

    def hide(self) -> None:
        for window in self.windows:
            window.unmap()
        self.display.sync()
        self.visible = False
        # Give a composited XFCE desktop a frame to repaint beneath it.
        time.sleep(0.05)

    def restore(self) -> None:
        from Xlib import X

        for window in self.windows:
            window.map()
            window.configure(stack_mode=X.Above)
        self.display.sync()
        self.visible = True

    def close(self) -> None:
        connection, self.display = self.display, None
        self.windows = []
        self.visible = False
        if connection is not None:
            try:
                connection.close()  # X11 destroys all windows owned by this connection.
            except Exception:
                pass  # The X server may already have exited.


def run_worker() -> None:
    marker = NativeClickMarker(Config(display=os.environ["DISPLAY"], xauthority=os.environ["XAUTHORITY"]))
    try:
        for line in sys.stdin:
            try:
                command = json.loads(line)
                action = command["action"]
                if action == "show":
                    marker.show(command["x"], command["y"])
                elif action == "hide":
                    marker.hide()
                elif action == "restore":
                    marker.restore()
                else:
                    raise ValueError("Unknown click marker action.")
                print(json.dumps({"visible": marker.visible,
                                  "window_ids": [window.id for window in marker.windows]}), flush=True)
            except Exception as exc:
                print(json.dumps({"error": str(exc)[:300]}), flush=True)
                return
    finally:
        marker.close()


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("This module is an internal desktop marker worker.")
    run_worker()
