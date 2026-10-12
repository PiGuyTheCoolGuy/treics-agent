from __future__ import annotations

from contextlib import contextmanager
import os
import threading

from .config import Config

_AUTH_LOCK = threading.Lock()


class ClickMarker:
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

        # python-xlib reads XAUTHORITY from the process environment at connect time.
        # All desktop subprocesses already pass their own explicit environment.
        with _AUTH_LOCK:
            previous = os.environ.get("XAUTHORITY")
            os.environ["XAUTHORITY"] = self.config.desktop_env()["XAUTHORITY"]
            try:
                connection = display.Display(self.config.display)
            finally:
                if previous is None:
                    os.environ.pop("XAUTHORITY", None)
                else:
                    os.environ["XAUTHORITY"] = previous
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

    @contextmanager
    def hidden(self):
        """Keep the marker out of the pixels sent to the model."""
        visible = self.visible
        if visible:
            try:
                for window in self.windows:
                    window.unmap()
                self.display.sync()
                # Give a composited XFCE desktop a frame to repaint beneath it.
                import time

                time.sleep(0.05)
            except Exception:
                self.close()
                visible = False
        try:
            yield
        finally:
            if visible:
                from Xlib import X

                try:
                    for window in self.windows:
                        window.map()
                        window.configure(stack_mode=X.Above)
                    self.display.sync()
                except Exception:
                    self.close()

    def close(self) -> None:
        connection, self.display = self.display, None
        self.windows = []
        self.visible = False
        if connection is not None:
            try:
                connection.close()  # X11 destroys all windows owned by this connection.
            except Exception:
                pass  # The X server may already have exited.
