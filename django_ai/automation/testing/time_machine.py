"""
Time manipulation utility for testing Django AI First workflows.

Provides a context manager that:
- Freezes/advances time via mocking django.utils.timezone.now()
- Runs an in-memory event loop that processes due events and workflows
- Optionally runs the loop in a background thread for async testing

Usage:
    from django_ai.automation.testing import time_machine, TimeMachine

    # As a context manager
    with time_machine() as tm:
        run = engine.start("my_workflow")
        tm.advance(minutes=5)  # Advances time and processes due work
        run.refresh_from_db()
        assert run.status == WorkflowStatus.COMPLETED

    # As a decorator
    @time_machine()
    def test_workflow_timing(tm):
        ...

    # With a fixed start time
    with time_machine(start=datetime(2024, 1, 1, 12, 0)) as tm:
        ...

    # Auto-process on advance (default) vs manual processing
    with time_machine(auto_process=False) as tm:
        tm.advance(hours=1)
        tm.process()  # Manually trigger processing
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import wraps
from typing import Callable, Optional, Any
from unittest.mock import patch

from django.utils import timezone


@dataclass
class ProcessingStats:
    """Statistics from a processing cycle."""
    events_processed: int = 0
    workflows_woken: int = 0
    steps_executed: int = 0


@dataclass
class TimeMachine:
    """
    A controllable time environment for testing workflows and events.

    Attributes:
        current_time: The current simulated time
        auto_process: Whether to automatically process due work when time advances
        stats: Cumulative processing statistics
    """
    current_time: datetime
    auto_process: bool = True
    _original_now: Optional[Callable[[], datetime]] = field(default=None, repr=False)
    _patcher: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _background_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _tick_interval: float = field(default=0.01, repr=False)  # 10ms
    stats: ProcessingStats = field(default_factory=ProcessingStats)

    def _now(self) -> datetime:
        """Return the current simulated time (thread-safe)."""
        with self._lock:
            return self.current_time

    def freeze(self, at: Optional[datetime] = None) -> None:
        """
        Freeze time at the specified datetime.

        Args:
            at: The time to freeze at. If None, freezes at current simulated time.
        """
        with self._lock:
            if at is not None:
                self.current_time = at

    def advance(
        self,
        *,
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
        days: float = 0,
        delta: Optional[timedelta] = None,
    ) -> ProcessingStats:
        """
        Advance time by the specified amount.

        Args:
            seconds: Number of seconds to advance
            minutes: Number of minutes to advance
            hours: Number of hours to advance
            days: Number of days to advance
            delta: A timedelta to advance by (alternative to individual units)

        Returns:
            ProcessingStats from any automatic processing that occurred
        """
        if delta is None:
            delta = timedelta(
                seconds=seconds,
                minutes=minutes,
                hours=hours,
                days=days,
            )

        with self._lock:
            self.current_time = self.current_time + delta

        if self.auto_process:
            return self.process()
        return ProcessingStats()

    def advance_to(self, target: datetime) -> ProcessingStats:
        """
        Advance time to a specific datetime.

        Args:
            target: The datetime to advance to. Must be in the future.

        Returns:
            ProcessingStats from any automatic processing that occurred

        Raises:
            ValueError: If target is in the past relative to current simulated time
        """
        with self._lock:
            if target < self.current_time:
                raise ValueError(
                    f"Cannot advance backwards: {target} < {self.current_time}"
                )
            self.current_time = target

        if self.auto_process:
            return self.process()
        return ProcessingStats()

    def process(self) -> ProcessingStats:
        """
        Process all due events and wake sleeping workflows.

        This is called automatically when time advances if auto_process=True.
        Call manually if auto_process=False.

        Returns:
            ProcessingStats with counts of processed items
        """
        from django_ai.automation.events.services import event_processor
        from django_ai.automation.workflows.core import engine
        from django_ai.automation.workflows.models import WorkflowRun, WorkflowStatus
        from django.db.models import Q

        stats = ProcessingStats()

        # Process due events
        result = event_processor.process_due_events(lookback_hours=24)
        stats.events_processed = result.get("events_processed", 0)
        self.stats.events_processed += stats.events_processed

        # Process scheduled workflows (sleeping or timed out)
        # Count how many will wake up
        now = self._now()
        ready_count = WorkflowRun.objects.filter(
            Q(status=WorkflowStatus.WAITING, wake_at__lte=now)
            | Q(
                status=WorkflowStatus.SUSPENDED,
                wake_at__lte=now,
                on_timeout_step__isnull=False,
            )
            & ~Q(on_timeout_step="")
        ).count()

        engine.process_scheduled()
        stats.workflows_woken = ready_count
        self.stats.workflows_woken += stats.workflows_woken

        return stats

    def run_until_idle(self, max_iterations: int = 100) -> ProcessingStats:
        """
        Keep processing until no more work is due.

        Useful when workflows trigger other workflows or events.

        Args:
            max_iterations: Safety limit to prevent infinite loops

        Returns:
            Cumulative ProcessingStats from all iterations
        """
        total_stats = ProcessingStats()

        for _ in range(max_iterations):
            stats = self.process()
            total_stats.events_processed += stats.events_processed
            total_stats.workflows_woken += stats.workflows_woken

            if stats.events_processed == 0 and stats.workflows_woken == 0:
                break

        return total_stats

    def run_workflow_to_completion(
        self,
        run_id: int,
        max_advance: timedelta = timedelta(days=365),
        step_size: timedelta = timedelta(minutes=1),
        max_iterations: int = 1000,
    ) -> "WorkflowRun":
        """
        Advance time until a workflow completes (or fails/cancels).

        This is a convenience method for testing workflows with time-based
        sleeps or event waits. It repeatedly advances time and processes
        until the workflow reaches a terminal state.

        Args:
            run_id: The workflow run ID to monitor
            max_advance: Maximum total time to advance before giving up
            step_size: How much time to advance each iteration
            max_iterations: Safety limit on number of iterations

        Returns:
            The final WorkflowRun instance

        Raises:
            TimeoutError: If workflow doesn't complete within max_advance time
        """
        from django_ai.automation.workflows.models import WorkflowRun, WorkflowStatus

        terminal_statuses = {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }

        total_advanced = timedelta()

        for _ in range(max_iterations):
            run = WorkflowRun.objects.get(id=run_id)

            if run.status in terminal_statuses:
                return run

            if total_advanced >= max_advance:
                raise TimeoutError(
                    f"Workflow {run_id} did not complete within {max_advance}. "
                    f"Current status: {run.status}"
                )

            # If the workflow is waiting/suspended with a wake_at, jump directly to it
            if run.wake_at and run.wake_at > self._now():
                jump = run.wake_at - self._now() + timedelta(milliseconds=1)
                self.advance(delta=jump)
                total_advanced += jump
            else:
                self.advance(delta=step_size)
                total_advanced += step_size

            self.run_until_idle()

        raise TimeoutError(
            f"Workflow {run_id} did not complete within {max_iterations} iterations"
        )

    def start_background_loop(self, tick_interval: float = 0.01) -> None:
        """
        Start a background thread that continuously processes due work.

        The loop checks for due work every tick_interval seconds.
        Useful for integration tests where you want processing to happen
        "automatically" as time advances.

        Args:
            tick_interval: How often to check for due work (in seconds)
        """
        if self._background_thread is not None:
            raise RuntimeError("Background loop already running")

        self._tick_interval = tick_interval
        self._stop_event.clear()

        def loop():
            while not self._stop_event.is_set():
                try:
                    self.process()
                except Exception:
                    pass  # Don't crash the loop on errors
                self._stop_event.wait(self._tick_interval)

        self._background_thread = threading.Thread(target=loop, daemon=True)
        self._background_thread.start()

    def stop_background_loop(self) -> None:
        """Stop the background processing loop."""
        if self._background_thread is None:
            return

        self._stop_event.set()
        self._background_thread.join(timeout=1.0)
        self._background_thread = None

    def __enter__(self) -> "TimeMachine":
        """Start the time machine (called by context manager)."""
        self._original_now = timezone.now
        self._patcher = patch("django.utils.timezone.now", self._now)
        self._patcher.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stop the time machine and restore original time."""
        self.stop_background_loop()
        if self._patcher:
            self._patcher.stop()
        return None


@contextmanager
def time_machine(
    start: Optional[datetime] = None,
    auto_process: bool = True,
    background_loop: bool = False,
    tick_interval: float = 0.01,
):
    """
    Context manager for time manipulation in tests.

    Args:
        start: The starting time. Defaults to the current real time.
        auto_process: Whether to process due work when time advances.
        background_loop: Whether to start a background processing loop.
        tick_interval: Interval for background loop (if enabled).

    Yields:
        TimeMachine: The time manipulation controller

    Example:
        with time_machine() as tm:
            run = engine.start("sleeper")
            engine.execute_step(run.id, "start")

            # Workflow is now sleeping
            run.refresh_from_db()
            assert run.status == WorkflowStatus.WAITING

            # Advance past the sleep duration
            tm.advance(minutes=5)

            # Workflow should have woken and completed
            run.refresh_from_db()
            assert run.status == WorkflowStatus.COMPLETED
    """
    if start is None:
        start = timezone.now()

    tm = TimeMachine(current_time=start, auto_process=auto_process)

    with tm:
        if background_loop:
            tm.start_background_loop(tick_interval)
        try:
            yield tm
        finally:
            tm.stop_background_loop()


def with_time_machine(
    start: Optional[datetime] = None,
    auto_process: bool = True,
    background_loop: bool = False,
    tick_interval: float = 0.01,
):
    """
    Decorator version of time_machine for test methods.

    The TimeMachine instance is passed as the first argument after self.

    Example:
        class TestWorkflows(TestCase):
            @with_time_machine()
            def test_sleep_workflow(self, tm):
                run = engine.start("sleeper")
                tm.advance(hours=1)
                ...

            @with_time_machine(start=datetime(2024, 6, 15, 9, 0))
            def test_scheduled_event(self, tm):
                ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with time_machine(
                start=start,
                auto_process=auto_process,
                background_loop=background_loop,
                tick_interval=tick_interval,
            ) as tm:
                return func(*args, tm, **kwargs)
        return wrapper
    return decorator
