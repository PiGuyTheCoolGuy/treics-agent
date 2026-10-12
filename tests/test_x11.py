from __future__ import annotations

import dataclasses
import io
import os
import secrets
import selectors
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops

from treics_agent.config import Config
from treics_agent.storage import Store
from treics_agent.stream import DesktopStream
from treics_agent.tools import Desktop, Tools


@unittest.skipUnless(all(shutil.which(command) for command in ("Xvfb", "scrot", "xdotool", "xauth")),
                     "Real X11 desktop tools are not installed")
class X11Tests(unittest.TestCase):
    def test_live_marker_click_through_focus_clean_capture_and_cleanup(self):
        from Xlib import X, display
        from Xlib.ext import shape

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority"
            authority.touch()
            cookie = secrets.token_hex(16)
            # Xvfb reads cookies from this file before choosing its display number.
            subprocess.run(["xauth", "-f", str(authority), "add", ":0", ".", cookie], check=True,
                           capture_output=True)
            log = (root / "xvfb.log").open("wb")
            # Some test containers allow loopback TCP but not AF_UNIX sockets.
            tcp = False
            try:
                probe = socket.socket(socket.AF_UNIX)
                probe.close()
            except PermissionError:
                tcp = True
            transports = (["-nolisten", "local", "-nolisten", "unix", "-listen", "tcp"]
                          if tcp else ["-nolisten", "tcp"])
            xvfb = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "800x600x24",
                                     *transports, "-auth", str(authority)],
                                    stdout=subprocess.PIPE, stderr=log)
            desktop = store = client = live = None
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(xvfb.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(8), "Xvfb did not become ready")
                number = xvfb.stdout.readline().decode().strip()
                self.assertTrue(number.isdigit(), (root / "xvfb.log").read_text()[-1600:])
                address = ("127.0.0.1:" if tcp else ":") + number
                subprocess.run(["xauth", "-f", str(authority), "add", address, ".", cookie], check=True,
                               capture_output=True)
                cfg = dataclasses.replace(Config(), display=address, xauthority=str(authority),
                                          work_dir=str(root / "work"), data_dir=str(root / "data"))
                with mock.patch.dict(os.environ, {"XAUTHORITY": str(authority)}):
                    client = display.Display(address)
                screen = client.screen()
                pixel = screen.default_colormap.alloc_named_color("#122037").pixel
                window = screen.root.create_window(0, 0, 800, 600, 0, screen.root_depth,
                                                   X.InputOutput, X.CopyFromParent,
                                                   background_pixel=pixel,
                                                   event_mask=X.ButtonPressMask | X.ButtonReleaseMask)
                window.map()
                window.set_input_focus(X.RevertToParent, X.CurrentTime)
                client.sync()
                desktop = Desktop(cfg)
                store = Store(cfg.data_path)
                tools = Tools(cfg, store, desktop)
                before = desktop.capture()
                result = tools.execute("click", {"x": 400, "y": 300}, threading.Event())
                self.assertTrue(result["ok"])
                self.assertNotIn("click_marker_error", result)
                self.assertEqual((result["last_click"]["x"], result["last_click"]["y"]), (400, 300))
                self.assertEqual(client.get_input_focus().focus.id, window.id)
                self.assertTrue(desktop.marker.visible)
                for window_id in desktop.marker.window_ids:
                    layer = client.create_resource_object("window", window_id)
                    self.assertEqual(list(layer.shape_get_rectangles(shape.SK.Input).rectangles), [])
                marked = root / "marked.png"
                desktop.command(["scrot", "-o", str(marked)])
                # Click a visible part of the marker, not just its central hole.
                desktop.command(["xdotool", "mousemove", "--sync", "410", "300", "click", "1"])
                client.sync()
                clicks = []
                while client.pending_events():
                    event = client.next_event()
                    if event.type == X.ButtonPress:
                        clicks.append((event.event_x, event.event_y))
                self.assertIn((400, 300), clicks)
                self.assertIn((410, 300), clicks)
                self.assertEqual(client.get_input_focus().focus.id, window.id)
                after = desktop.capture()
                self.assertEqual(before["base64"], after["base64"])
                self.assertTrue(desktop.marker.visible)  # Restored after clean capture.
                with desktop.marker.hidden():
                    clean = root / "clean.png"
                    desktop.command(["scrot", "-o", str(clean)])
                with Image.open(marked) as marked_image, Image.open(clean) as clean_image:
                    bbox = ImageChops.difference(marked_image, clean_image).getbbox()
                    self.assertIsNotNone(bbox, "The live marker was not drawn")
                    self.assertGreaterEqual(bbox[0], 379)
                    self.assertLessEqual(bbox[2], 422)
                marker_ids = set(desktop.marker.window_ids)
                desktop.marker.close()
                client.sync()
                remaining = {child.id for child in screen.root.query_tree().children}
                self.assertTrue(marker_ids.isdisjoint(remaining))
                # Live capture is independent of the model's cached image and GUI lock.
                saved_frame, saved_preview = desktop.frame, desktop.image
                live = DesktopStream(cfg)
                live.subscribe()
                first = live.next_frame(0, 3)
                self.assertIsNotNone(first, live.snapshot())
                self.assertEqual((first.width, first.height, first.desktop_width, first.desktop_height),
                                 (800, 600, 800, 600))
                red = screen.default_colormap.alloc_named_color("#ef2020").pixel
                window.change_attributes(background_pixel=red)
                window.clear_area(0, 0, 800, 600)
                client.sync()
                latest = first
                for _ in range(12):
                    latest = live.next_frame(latest.sequence, 1)
                    self.assertIsNotNone(latest, live.snapshot())
                    with Image.open(io.BytesIO(latest.jpeg)) as picture:
                        r, g, b = picture.getpixel((50, 50))
                    if r > g + 100:
                        break
                self.assertGreater(r, g + 100, "Live frames did not update when the desktop changed")
                with Image.open(io.BytesIO(latest.jpeg)) as picture:
                    pointer = picture.crop((390, 280, 440, 330))
                    self.assertGreater(pointer.getextrema()[1][1], 120,
                                       "The live stream did not include the mouse pointer")
                self.assertIs(desktop.frame, saved_frame)
                self.assertEqual(desktop.image, saved_preview)
                self.assertEqual(client.get_input_focus().focus.id, window.id)
                live.unsubscribe()
                live.close()
                self.assertIsNone(live.process)
            finally:
                if live:
                    live.close()
                if desktop:
                    desktop.marker.close()
                if client:
                    client.close()
                if store:
                    store.close()
                xvfb.terminate()
                try:
                    xvfb.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    xvfb.kill()
                    xvfb.wait(timeout=5)
                xvfb.stdout.close()
                log.close()


if __name__ == "__main__":
    unittest.main()
