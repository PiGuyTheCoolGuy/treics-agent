from __future__ import annotations

import argparse
import dataclasses
import io
import math
import os
import selectors
import struct
import subprocess
import sys
import threading
import time

from .config import Config


FRAME_HEADER = struct.Struct("!IIIIId")  # bytes, image w/h, desktop w/h, timestamp
MAX_FRAME_BYTES = 4 * 1024 * 1024
BOUNDARY = "treic-frame"


@dataclasses.dataclass(frozen=True)
class LiveFrame:
    sequence: int
    jpeg: bytes
    width: int
    height: int
    desktop_width: int
    desktop_height: int
    captured_at: float


class DesktopStream:
    """One bounded capture worker, independent of model frames and GUI locks."""

    MAX_VIEWERS = 4
    FRAME_TIMEOUT = 5.0

    def __init__(self, config: Config):
        self.config = config
        self.condition = threading.Condition()
        self.process_lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.viewers = 0
        self.closed = False
        self.frame: LiveFrame | None = None
        self.sequence = 0
        self.error: str | None = None

    def subscribe(self) -> None:
        with self.condition:
            if self.closed:
                raise RuntimeError("Desktop stream is shutting down.")
            if self.viewers >= self.MAX_VIEWERS:
                raise ValueError("Desktop stream has reached its viewer limit. Pause another live view.")
            self.viewers += 1
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="treic-desktop-stream")
                self.thread.start()
            self.condition.notify_all()

    def unsubscribe(self) -> None:
        with self.condition:
            self.viewers = max(0, self.viewers - 1)
            idle = self.viewers == 0
            if idle:
                self.frame = None
            self.condition.notify_all()
        if idle:
            self.kill_worker()

    def snapshot(self) -> dict:
        with self.condition:
            return {"fps": self.config.stream_fps, "viewers": self.viewers, "error": self.error,
                    "captured_at": self.frame.captured_at if self.frame else None}

    def next_frame(self, after: int, timeout: float = 2) -> LiveFrame | None:
        with self.condition:
            self.condition.wait_for(lambda: self.closed or (self.frame and self.frame.sequence > after), timeout)
            return self.frame if not self.closed and self.frame and self.frame.sequence > after else None

    def active(self) -> bool:
        with self.condition:
            return not self.closed and self.viewers > 0

    def kill_worker(self) -> None:
        with self.process_lock:
            if self.process is not None:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.kill_worker()
        if self.thread is not None:
            self.thread.join(timeout=2)

    @staticmethod
    def read_exact(process, count: int, deadline: float, selector) -> bytes:
        result = bytearray()
        while len(result) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("Desktop capture timed out; retrying.")
            chunk = os.read(process.stdout.fileno(), count - len(result))
            if not chunk:
                raise RuntimeError("Desktop capture worker disconnected; retrying.")
            result.extend(chunk)
        return bytes(result)

    def _run(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.viewers > 0)
                if self.closed:
                    return
            process = None
            try:
                with self.process_lock:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "treics_agent.stream", "--worker",
                         "--fps", str(self.config.stream_fps), "--quality", str(self.config.stream_quality),
                         "--width", str(self.config.screen_width), "--height", str(self.config.screen_height)],
                        env=self.config.desktop_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
                    self.process = process
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while self.active():
                        deadline = time.monotonic() + self.FRAME_TIMEOUT
                        header = self.read_exact(process, FRAME_HEADER.size, deadline, selector)
                        length, width, height, dw, dh, captured = FRAME_HEADER.unpack(header)
                        if not 0 < length <= MAX_FRAME_BYTES:
                            raise RuntimeError("Invalid desktop stream frame size.")
                        if width == height == dw == dh == 0:
                            if length > 1000:
                                raise RuntimeError("Invalid desktop capture error.")
                            message = self.read_exact(process, length, deadline, selector).decode(errors="replace")
                            raise RuntimeError(message)
                        if (not 1 <= width <= self.config.screen_width or not 1 <= height <= self.config.screen_height
                                or not width <= dw <= 16384 or not height <= dh <= 16384
                                or not math.isfinite(captured) or captured <= 0):
                            raise RuntimeError("Invalid desktop stream frame metadata.")
                        jpeg = self.read_exact(process, length, deadline, selector)
                        if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                            raise RuntimeError("Invalid desktop stream JPEG.")
                        with self.condition:
                            if self.closed or not self.viewers:
                                break
                            self.sequence += 1
                            self.frame = LiveFrame(self.sequence, jpeg, width, height, dw, dh, captured)
                            self.error = None
                            self.condition.notify_all()
            except Exception as exc:
                with self.condition:
                    if not self.closed and self.viewers:
                        self.error = str(exc)[:300]
                        self.frame = None
                        self.condition.notify_all()
            finally:
                if process is not None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                    process.stdout.close()
                with self.process_lock:
                    if self.process is process:
                        self.process = None
            with self.condition:
                if self.closed:
                    return
                if self.viewers:
                    self.condition.wait(timeout=1)  # Retry without spinning after a capture failure.


def run_worker(fps: int, quality: int, width: int, height: int) -> None:
    from PIL import Image, ImageGrab, features

    cursor_connection = None
    cursor_serial, cursor_bitmap = None, None
    try:
        if not features.check_feature("xcb"):
            raise RuntimeError("Pillow X11 capture support is unavailable. Install its Linux wheel and restart the agent.")
        # Native grabs omit the hardware pointer; XFIXES supplies its pixels/hotspot.
        try:
            from Xlib import display

            cursor_connection = display.Display(os.environ["DISPLAY"])
            if not cursor_connection.has_extension("XFIXES") or cursor_connection.xfixes_query_version().major_version < 2:
                cursor_connection.close()
                cursor_connection = None
        except Exception:
            if cursor_connection:
                cursor_connection.close()
            cursor_connection = None
        interval = 1 / fps
        while True:
            started = time.monotonic()
            image = ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).convert("RGB")
            desktop_width, desktop_height = image.size
            captured = time.time()
            if cursor_connection:
                try:
                    cursor = cursor_connection.xfixes_get_cursor_image(cursor_connection.screen().root)
                    if 0 < cursor.width <= 256 and 0 < cursor.height <= 256:
                        if cursor_serial != cursor.cursor_serial:
                            pixels = struct.pack("<" + "I" * len(cursor.cursor_image), *cursor.cursor_image)
                            cursor_bitmap = Image.frombytes("RGBA", (cursor.width, cursor.height), pixels, "raw", "BGRA")
                            cursor_serial = cursor.cursor_serial
                        image.paste(cursor_bitmap, (cursor.x - cursor.xhot, cursor.y - cursor.yhot), cursor_bitmap)
                except Exception:
                    cursor_connection.close()
                    cursor_connection = None
            image.thumbnail((width, height), Image.Resampling.BILINEAR)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality)
            jpeg = output.getvalue()
            sys.stdout.buffer.write(FRAME_HEADER.pack(len(jpeg), image.width, image.height,
                                                      desktop_width, desktop_height, captured))
            sys.stdout.buffer.write(jpeg)
            sys.stdout.buffer.flush()
            time.sleep(max(0, interval - (time.monotonic() - started)))
    except (BrokenPipeError, ConnectionResetError):
        return
    except Exception as exc:
        message = str(exc).encode()[:900]
        sys.stdout.buffer.write(FRAME_HEADER.pack(len(message), 0, 0, 0, 0, time.time()) + message)
        sys.stdout.buffer.flush()
    finally:
        if cursor_connection:
            cursor_connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal live desktop capture worker")
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--fps", type=int, choices=range(1, 16), required=True)
    parser.add_argument("--quality", type=int, choices=range(30, 96), required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    options = parser.parse_args()
    run_worker(options.fps, options.quality, options.width, options.height)
