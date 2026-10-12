from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from unittest import mock

from treics_agent.click_marker import ClickMarker
from treics_agent.config import Config


class ClickMarkerTests(unittest.TestCase):
    def fake_worker(self, script, workers, created=None):
        popen = subprocess.Popen
        def launch(args, **kwargs):
            worker = popen([sys.executable, "-u", "-c", script], **kwargs)
            workers.append(worker)
            if created:
                created.set()
            return worker
        return mock.patch("treics_agent.click_marker.subprocess.Popen", side_effect=launch)

    def test_partial_response_times_out_and_worker_is_destroyed(self):
        marker = ClickMarker(Config())
        marker.TIMEOUT = 0.2
        workers = []
        script = 'import sys, threading; sys.stdin.readline(); sys.stdout.write("{\\\"visible\\\":"); sys.stdout.flush(); threading.Event().wait()'
        try:
            with self.fake_worker(script, workers), self.assertRaisesRegex(TimeoutError, "marker timed out"):
                marker.show(10, 20)
            self.assertIsNone(marker.process)
            self.assertFalse(marker.visible)
            self.assertIsNotNone(workers[0].poll())
        finally:
            marker.close()

    def test_stop_interrupts_pending_worker_without_waiting_for_deadline(self):
        marker = ClickMarker(Config())
        marker.TIMEOUT = 20
        workers, failures = [], []
        created = threading.Event()
        def show():
            try:
                marker.show(10, 20)
            except Exception as exc:
                failures.append(exc)
        script = 'import sys, threading; sys.stdin.readline(); threading.Event().wait()'
        with self.fake_worker(script, workers, created):
            thread = threading.Thread(target=show)
            thread.start()
            try:
                self.assertTrue(created.wait(1))
                marker.cancel()
                thread.join(2)
                self.assertFalse(thread.is_alive(), "Cancel waited for the marker timeout")
                self.assertTrue(failures)
                self.assertIsNotNone(workers[0].poll())
            finally:
                marker.close()
                thread.join(2)

    def test_idle_marker_survives_task_stop_and_close_cleans_it_up(self):
        marker = ClickMarker(Config())
        workers = []
        script = ('import sys, json\n'
                  'for line in sys.stdin:\n'
                  ' command=json.loads(line)\n'
                  ' print(json.dumps({"visible":command["action"]!="hide","window_ids":[123]}),flush=True)\n')
        try:
            with self.fake_worker(script, workers):
                marker.show(10, 20)
                self.assertTrue(marker.visible)
                marker.cancel()
                self.assertIsNone(workers[0].poll())
                with marker.hidden():
                    self.assertFalse(marker.visible)
                self.assertTrue(marker.visible)
                marker.close()
                self.assertIsNotNone(workers[0].poll())
        finally:
            marker.close()


if __name__ == "__main__":
    unittest.main()
