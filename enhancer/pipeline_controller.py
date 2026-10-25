"""
Thread-safe Pipeline Controller for FAA Data Downloads and ChartData Builds (`enhancer/pipeline_controller.py`).

Provides pause, resume, and cancellation check-points for long-running batch operations.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class PipelineController:
    """
    Thread-safe controller supporting pause, resume, and cancellation.
    """

    def __init__(self) -> None:
        self._pause_event = threading.Event()  # cleared = running; set = paused
        self._stop_event = threading.Event()   # set = stop/cancel requested

    @property
    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def pause(self) -> None:
        """Pause pipeline execution at the next check-point."""
        self._pause_event.set()

    def resume(self) -> None:
        """Resume paused pipeline execution."""
        self._pause_event.clear()

    def stop(self) -> None:
        """Signal immediate cancellation and unblock any paused threads."""
        self._stop_event.set()
        # Clear pause event so any threads waiting in check_point wake up and exit
        self._pause_event.clear()

    def check_point(self, poll_interval: float = 0.15) -> bool:
        """
        Check whether execution should continue, pause, or abort.
        - If stop was requested, returns False immediately.
        - If paused, sleeps until resumed or stopped.
        - Returns True if execution can safely proceed; False if stopped.
        """
        if self._stop_event.is_set():
            return False

        while self._pause_event.is_set():
            if self._stop_event.is_set():
                return False
            time.sleep(poll_interval)

        return not self._stop_event.is_set()
