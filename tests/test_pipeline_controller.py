"""
Unit tests for PipelineController (pause, continue, stop).
"""

import threading
import time
import unittest

from enhancer.pipeline_controller import PipelineController


class TestPipelineController(unittest.TestCase):
    def test_initial_state(self):
        ctrl = PipelineController()
        self.assertFalse(ctrl.is_paused)
        self.assertFalse(ctrl.is_stopped)
        self.assertTrue(ctrl.check_point())

    def test_pause_and_resume(self):
        ctrl = PipelineController()
        ctrl.pause()
        self.assertTrue(ctrl.is_paused)
        self.assertFalse(ctrl.is_stopped)

        resumed = False

        def _worker():
            nonlocal resumed
            ok = ctrl.check_point(poll_interval=0.05)
            if ok:
                resumed = True

        t = threading.Thread(target=_worker)
        t.start()

        # Should be paused initially
        time.sleep(0.1)
        self.assertFalse(resumed)

        # Resume
        ctrl.resume()
        t.join(timeout=1.0)
        self.assertTrue(resumed)
        self.assertFalse(ctrl.is_paused)

    def test_stop_unblocks_paused_worker(self):
        ctrl = PipelineController()
        ctrl.pause()

        worker_ok = None

        def _worker():
            nonlocal worker_ok
            worker_ok = ctrl.check_point(poll_interval=0.05)

        t = threading.Thread(target=_worker)
        t.start()

        time.sleep(0.1)
        self.assertIsNone(worker_ok)

        # Stopping while paused must wake up and return False
        ctrl.stop()
        t.join(timeout=1.0)
        self.assertFalse(worker_ok)
        self.assertTrue(ctrl.is_stopped)


if __name__ == "__main__":
    unittest.main()
