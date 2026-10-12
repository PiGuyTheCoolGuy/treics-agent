from __future__ import annotations

import base64
import dataclasses
import io
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from PIL import Image

from treics_agent.config import Config
from treics_agent.stream import DesktopStream, FRAME_HEADER, MAX_FRAME_BYTES


def jpeg(color="red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(output, format="JPEG")
    return output.getvalue()


def frame_worker_script() -> str:
    encoded = [base64.b64encode(jpeg(color)).decode() for color in ("red", "blue")]
    return ("import base64,struct,sys,time\n"
            f"images=[base64.b64decode(x) for x in {encoded!r}]\n"
            "i=0\n"
            "while True:\n"
            " data=images[i%2]; i+=1\n"
            " sys.stdout.buffer.write(struct.pack('!IIIIId',len(data),320,240,800,600,time.time())+data)\n"
            " sys.stdout.buffer.flush(); time.sleep(0.05)\n")


def worker_stub(script, workers):
    popen = subprocess.Popen
    def launch(args, **kwargs):
        process = popen([sys.executable, "-u", "-c", script], **kwargs)
        workers.append(process)
        return process
    return mock.patch("treics_agent.stream.subprocess.Popen", side_effect=launch)


class StreamTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertTrue(predicate())

    def test_multiple_viewers_share_one_worker_and_only_latest_frame_is_kept(self):
        stream = DesktopStream(Config())
        workers = []
        try:
            self.assertIsNone(stream.thread)
            with worker_stub(frame_worker_script(), workers):
                stream.subscribe(); stream.subscribe()
                first = stream.next_frame(0, 2)
                self.assertIsNotNone(first)
                second = stream.next_frame(first.sequence, 1)
                self.assertNotEqual(first.jpeg, second.jpeg)
                self.assertEqual((second.desktop_width, second.desktop_height), (800, 600))
                self.assertEqual(len(workers), 1)
                self.assertEqual(stream.snapshot()["viewers"], 2)
                stream.unsubscribe()
                self.assertIsNone(workers[0].poll())
                stream.unsubscribe()
                self.wait_for(lambda: workers[0].poll() is not None and stream.process is None)
                self.assertIsNone(stream.frame)
                stream.subscribe()
                restarted = stream.next_frame(second.sequence, 2)
                self.assertIsNotNone(restarted)
                self.assertEqual(len(workers), 2)
        finally:
            stream.close()
        self.assertFalse(stream.thread.is_alive())
        self.assertTrue(all(process.poll() is not None for process in workers))

    def test_partial_frame_timeout_is_bounded_and_close_interrupts_capture(self):
        stream = DesktopStream(Config())
        stream.FRAME_TIMEOUT = 0.15
        workers = []
        script = "import sys,threading; sys.stdout.buffer.write(b'partial'); sys.stdout.buffer.flush(); threading.Event().wait()"
        try:
            with worker_stub(script, workers):
                stream.subscribe()
                self.wait_for(lambda: stream.snapshot()["error"] is not None)
                self.assertIn("timed out", stream.snapshot()["error"])
                self.wait_for(lambda: workers[0].poll() is not None)
                stream.close()
                self.assertFalse(stream.thread.is_alive())
        finally:
            stream.close()

    def test_oversized_frame_and_viewer_limit(self):
        stream = DesktopStream(Config())
        workers = []
        header = base64.b64encode(FRAME_HEADER.pack(MAX_FRAME_BYTES + 1, 320, 240, 800, 600, time.time())).decode()
        script = f"import base64,sys,threading; sys.stdout.buffer.write(base64.b64decode({header!r})); sys.stdout.buffer.flush(); threading.Event().wait()"
        try:
            with worker_stub(script, workers):
                for _ in range(stream.MAX_VIEWERS):
                    stream.subscribe()
                with self.assertRaisesRegex(ValueError, "viewer limit"):
                    stream.subscribe()
                self.wait_for(lambda: stream.snapshot()["error"] is not None)
                self.assertIn("frame size", stream.snapshot()["error"])
                self.assertIsNone(stream.frame)
        finally:
            stream.close()

    def test_stream_configuration_validates_and_legacy_configs_use_defaults(self):
        Config().validate()
        self.assertEqual(Config().stream_fps, 5)
        for values in ({"stream_fps": 0}, {"stream_fps": 16}, {"stream_fps": True},
                       {"stream_quality": 29}, {"stream_quality": 96}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                dataclasses.replace(Config(), **values).validate()


if __name__ == "__main__":
    unittest.main()
