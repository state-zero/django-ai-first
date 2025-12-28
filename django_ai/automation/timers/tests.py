"""
Tests for the timers app - precise scheduling and debounce functionality.

Uses cache-driven architecture where pending tasks live in cache
and only executed tasks are logged to the database.
"""

from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .models import TimedTask
from .core import (
    schedule_task,
    debounce_task,
    process_due_tasks,
    clear_pending_tasks,
    get_pending_task_count,
    _get_task,
)

# Test context to capture task executions
_test_executions = []


def _reset_test_executions():
    global _test_executions
    _test_executions = []


def test_task_simple(value):
    """Simple test task that records its execution."""
    _test_executions.append({"type": "simple", "value": value})


def test_task_with_collected(user_id, collected_args=None):
    """Test task that receives collected args from debounce."""
    _test_executions.append({
        "type": "collected",
        "user_id": user_id,
        "collected_args": collected_args or [],
    })


class ScheduleTaskTests(TestCase):
    """Tests for one-shot precise scheduling."""

    def setUp(self):
        clear_pending_tasks()

    def tearDown(self):
        clear_pending_tasks()

    def test_schedule_task_with_delay(self):
        """Schedule a task with a delay creates pending task in cache."""
        task_id = schedule_task(
            "myapp.tasks.send_email",
            args=[1, "hello"],
            kwargs={"urgent": True},
            delay=timedelta(seconds=30),
        )

        self.assertIsNotNone(task_id)
        self.assertTrue(task_id.startswith("once:"))

        # Verify task is in cache
        task = _get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.task_name, "myapp.tasks.send_email")
        self.assertEqual(task.task_args, [1, "hello"])
        self.assertEqual(task.task_kwargs, {"urgent": True})
        self.assertIsNone(task.debounce_key)
        self.assertAlmostEqual(
            (task.run_at - timezone.now()).total_seconds(),
            30,
            delta=1,
        )

    def test_schedule_task_with_run_at(self):
        """Schedule a task with explicit run_at."""
        run_at = timezone.now() + timedelta(hours=1)
        task_id = schedule_task(
            "myapp.tasks.process",
            run_at=run_at,
        )

        task = _get_task(task_id)
        # Allow for small time differences due to serialization
        self.assertAlmostEqual(
            (task.run_at - run_at).total_seconds(),
            0,
            delta=1,
        )

    def test_schedule_task_requires_timing(self):
        """Must specify either delay or run_at."""
        with self.assertRaises(ValueError):
            schedule_task("myapp.tasks.foo")

    def test_schedule_task_not_both_timings(self):
        """Cannot specify both delay and run_at."""
        with self.assertRaises(ValueError):
            schedule_task(
                "myapp.tasks.foo",
                delay=timedelta(seconds=10),
                run_at=timezone.now(),
            )


class DebounceTaskTests(TestCase):
    """Tests for debounced task scheduling."""

    def setUp(self):
        clear_pending_tasks()

    def tearDown(self):
        clear_pending_tasks()

    def test_debounce_creates_task(self):
        """First debounce call creates a new pending task."""
        task_id = debounce_task(
            "myapp.tasks.process_updates",
            debounce_key="user:123",
            debounce_window=timedelta(seconds=5),
            args=[123],
            kwargs={"mode": "batch"},
            collect="event_1",
        )

        self.assertEqual(task_id, "debounce:user:123")

        task = _get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.task_name, "myapp.tasks.process_updates")
        self.assertEqual(task.debounce_key, "user:123")
        self.assertEqual(task.task_args, [123])
        self.assertEqual(task.task_kwargs, {"mode": "batch"})
        self.assertEqual(task.collected_args, ["event_1"])

    def test_debounce_extends_window(self):
        """Subsequent debounce calls extend the window."""
        task_id = debounce_task(
            "myapp.tasks.process",
            debounce_key="user:456",
            debounce_window=timedelta(seconds=5),
        )
        task = _get_task(task_id)
        original_run_at = task.run_at

        # Small delay to ensure time advances
        import time
        time.sleep(0.01)

        task_id2 = debounce_task(
            "myapp.tasks.process",
            debounce_key="user:456",
            debounce_window=timedelta(seconds=5),
        )

        # Same task ID
        self.assertEqual(task_id, task_id2)

        # Window should be extended
        task = _get_task(task_id)
        self.assertGreater(task.run_at, original_run_at)

    def test_debounce_collects_args(self):
        """Debounce accumulates collected args."""
        debounce_task(
            "myapp.tasks.process",
            debounce_key="batch:1",
            debounce_window=timedelta(seconds=5),
            collect="item_1",
        )

        debounce_task(
            "myapp.tasks.process",
            debounce_key="batch:1",
            debounce_window=timedelta(seconds=5),
            collect="item_2",
        )

        debounce_task(
            "myapp.tasks.process",
            debounce_key="batch:1",
            debounce_window=timedelta(seconds=5),
            collect="item_3",
        )

        task = _get_task("debounce:batch:1")
        self.assertEqual(task.collected_args, ["item_1", "item_2", "item_3"])

    def test_debounce_different_keys_separate_tasks(self):
        """Different debounce keys create separate tasks."""
        task_id1 = debounce_task(
            "myapp.tasks.process",
            debounce_key="user:1",
            debounce_window=timedelta(seconds=5),
        )

        task_id2 = debounce_task(
            "myapp.tasks.process",
            debounce_key="user:2",
            debounce_window=timedelta(seconds=5),
        )

        self.assertNotEqual(task_id1, task_id2)
        self.assertEqual(get_pending_task_count(), 2)


class TimedTaskModelTests(TestCase):
    """Tests for the TimedTask model (audit log)."""

    def test_str_success(self):
        """String representation for successful task."""
        task = TimedTask.objects.create(
            task_name="myapp.tasks.foo",
            run_at=timezone.now(),
            success=True,
        )
        self.assertIn("OK", str(task))
        self.assertIn("myapp.tasks.foo", str(task))

    def test_str_failure(self):
        """String representation for failed task."""
        task = TimedTask.objects.create(
            task_name="myapp.tasks.bar",
            run_at=timezone.now(),
            success=False,
            error_log="Something went wrong",
        )
        self.assertIn("FAIL", str(task))
        self.assertIn("myapp.tasks.bar", str(task))


class ProcessDueTasksTests(TestCase):
    """Tests for process_due_tasks with time_machine."""

    def setUp(self):
        _reset_test_executions()
        clear_pending_tasks()

    def tearDown(self):
        clear_pending_tasks()

    def test_scheduled_task_executes_when_due(self):
        """A scheduled task executes when time advances past run_at."""
        from django_ai.automation.testing import time_machine

        with time_machine() as tm:
            # Schedule task for 30 seconds from now
            schedule_task(
                "django_ai.automation.timers.tests.test_task_simple",
                args=["hello"],
                delay=timedelta(seconds=30),
            )

            # Not due yet
            result = process_due_tasks()
            self.assertEqual(result.dispatched, 0)
            self.assertEqual(len(_test_executions), 0)

            # Advance time past run_at
            tm.advance(seconds=31)

            # Should have been dispatched by time_machine.process()
            self.assertEqual(len(_test_executions), 1)
            self.assertEqual(_test_executions[0]["value"], "hello")

    def test_debounce_collects_and_executes_once(self):
        """Debounced task collects args and executes once after window."""
        from django_ai.automation.testing import time_machine

        with time_machine() as tm:
            # Multiple debounce calls with collect
            debounce_task(
                "django_ai.automation.timers.tests.test_task_with_collected",
                debounce_key="user:1",
                debounce_window=timedelta(seconds=5),
                args=[123],
                collect="event_a",
            )

            debounce_task(
                "django_ai.automation.timers.tests.test_task_with_collected",
                debounce_key="user:1",
                debounce_window=timedelta(seconds=5),
                collect="event_b",
            )

            debounce_task(
                "django_ai.automation.timers.tests.test_task_with_collected",
                debounce_key="user:1",
                debounce_window=timedelta(seconds=5),
                collect="event_c",
            )

            # Not due yet (window keeps extending)
            self.assertEqual(len(_test_executions), 0)

            # Only one pending task should exist
            self.assertEqual(get_pending_task_count(), 1)

            # Advance past the window
            tm.advance(seconds=6)

            # Should execute once with all collected args
            self.assertEqual(len(_test_executions), 1)
            self.assertEqual(_test_executions[0]["user_id"], 123)
            self.assertEqual(
                _test_executions[0]["collected_args"],
                ["event_a", "event_b", "event_c"]
            )

    def test_debounce_window_extension_delays_execution(self):
        """Extending debounce window delays execution."""
        from django_ai.automation.testing import time_machine

        with time_machine() as tm:
            # Initial debounce call
            debounce_task(
                "django_ai.automation.timers.tests.test_task_simple",
                debounce_key="test:extend",
                debounce_window=timedelta(seconds=5),
                args=["first"],
            )

            # Advance 3 seconds (not past window)
            tm.advance(seconds=3)
            self.assertEqual(len(_test_executions), 0)

            # Extend the window
            debounce_task(
                "django_ai.automation.timers.tests.test_task_simple",
                debounce_key="test:extend",
                debounce_window=timedelta(seconds=5),
            )

            # Advance 3 more seconds (6 total, but window was extended)
            tm.advance(seconds=3)
            self.assertEqual(len(_test_executions), 0)

            # Advance past the extended window
            tm.advance(seconds=3)
            self.assertEqual(len(_test_executions), 1)

    def test_multiple_debounce_keys_execute_separately(self):
        """Different debounce keys execute as separate tasks."""
        from django_ai.automation.testing import time_machine

        with time_machine() as tm:
            debounce_task(
                "django_ai.automation.timers.tests.test_task_with_collected",
                debounce_key="user:1",
                debounce_window=timedelta(seconds=5),
                args=[1],
                collect="a",
            )

            debounce_task(
                "django_ai.automation.timers.tests.test_task_with_collected",
                debounce_key="user:2",
                debounce_window=timedelta(seconds=5),
                args=[2],
                collect="b",
            )

            # Two pending tasks
            self.assertEqual(get_pending_task_count(), 2)

            # Advance past window
            tm.advance(seconds=6)

            # Both should execute
            self.assertEqual(len(_test_executions), 2)
            user_ids = {e["user_id"] for e in _test_executions}
            self.assertEqual(user_ids, {1, 2})


class TimersInProcessTests(TransactionTestCase):
    """Tests for TIMERS_IN_PROCESS background thread mode.

    Uses TransactionTestCase because the sync thread runs in a separate
    thread and needs to access the database without transaction locks.
    """

    def setUp(self):
        import time
        # Stop any running sync thread from previous tests
        from .core import stop_sync_thread
        stop_sync_thread()
        time.sleep(0.05)  # Allow thread to fully stop

        _reset_test_executions()
        clear_pending_tasks()

    def tearDown(self):
        import time
        # Clean up sync thread after each test
        from .core import stop_sync_thread
        stop_sync_thread()
        time.sleep(0.05)  # Allow thread to fully stop
        clear_pending_tasks()

    def test_sync_thread_starts_and_processes_tasks(self):
        """Background thread processes due tasks automatically."""
        import time
        from .core import start_sync_thread, stop_sync_thread, is_sync_thread_running

        # Start the sync thread
        started = start_sync_thread(poll_interval=0.05)
        self.assertTrue(started)
        self.assertTrue(is_sync_thread_running())

        # Schedule a task due immediately
        schedule_task(
            "django_ai.automation.timers.tests.test_task_simple",
            args=["sync_test"],
            delay=timedelta(milliseconds=1),
        )

        # Wait for the background thread to process it
        time.sleep(0.2)

        # Task should have been executed
        self.assertEqual(len(_test_executions), 1)
        self.assertEqual(_test_executions[0]["value"], "sync_test")

        # Stop the thread
        stopped = stop_sync_thread()
        self.assertTrue(stopped)
        self.assertFalse(is_sync_thread_running())

    def test_sync_thread_respects_run_at_timing(self):
        """Background thread doesn't execute tasks before run_at."""
        import time
        from .core import start_sync_thread, stop_sync_thread

        start_sync_thread(poll_interval=0.05)

        # Schedule a task for 500ms from now
        schedule_task(
            "django_ai.automation.timers.tests.test_task_simple",
            args=["delayed"],
            delay=timedelta(milliseconds=500),
        )

        # Wait 100ms - task shouldn't execute yet
        time.sleep(0.1)
        self.assertEqual(len(_test_executions), 0)

        # Wait past the delay
        time.sleep(0.5)
        self.assertEqual(len(_test_executions), 1)
        self.assertEqual(_test_executions[0]["value"], "delayed")

        stop_sync_thread()

    def test_sync_thread_debounce_works(self):
        """Background thread correctly handles debounced tasks."""
        import time
        from .core import start_sync_thread, stop_sync_thread

        start_sync_thread(poll_interval=0.05)

        # Multiple debounce calls
        for i in range(3):
            debounce_task(
                "django_ai.automation.timers.tests.test_task_with_collected",
                debounce_key="sync_debounce",
                debounce_window=timedelta(milliseconds=200),
                args=[999],
                collect=f"item_{i}",
            )
            time.sleep(0.05)

        # Task shouldn't execute yet (window keeps extending)
        self.assertEqual(len(_test_executions), 0)

        # Wait for debounce window to expire
        time.sleep(0.3)

        # Should execute once with all collected args
        self.assertEqual(len(_test_executions), 1)
        self.assertEqual(_test_executions[0]["user_id"], 999)
        self.assertEqual(
            _test_executions[0]["collected_args"],
            ["item_0", "item_1", "item_2"]
        )

        stop_sync_thread()

    def test_sync_thread_pauses_with_time_machine(self):
        """Sync thread pauses when time_machine is active."""
        import time
        from .core import start_sync_thread, stop_sync_thread, _sync_thread_paused
        from django_ai.automation.testing import time_machine

        start_sync_thread(poll_interval=0.05)

        # Verify thread is not paused
        self.assertFalse(_sync_thread_paused.is_set())

        with time_machine() as tm:
            # Thread should be paused inside time_machine
            self.assertTrue(_sync_thread_paused.is_set())

            # Schedule a task due immediately
            schedule_task(
                "django_ai.automation.timers.tests.test_task_simple",
                args=["paused_test"],
                delay=timedelta(milliseconds=1),
            )

            # Wait - sync thread shouldn't process (it's paused)
            time.sleep(0.2)
            self.assertEqual(len(_test_executions), 0)

            # But time_machine.process() should work
            tm.advance(seconds=1)
            self.assertEqual(len(_test_executions), 1)

        # Thread should resume after time_machine exits
        self.assertFalse(_sync_thread_paused.is_set())

        stop_sync_thread()

    def test_start_sync_thread_idempotent(self):
        """Starting sync thread twice returns False second time."""
        from .core import start_sync_thread, stop_sync_thread

        first = start_sync_thread()
        self.assertTrue(first)

        second = start_sync_thread()
        self.assertFalse(second)

        stop_sync_thread()

    def test_stop_sync_thread_when_not_running(self):
        """Stopping non-running thread returns False."""
        from .core import stop_sync_thread, is_sync_thread_running

        self.assertFalse(is_sync_thread_running())
        result = stop_sync_thread()
        self.assertFalse(result)


class AuditLogTests(TestCase):
    """Tests for database audit logging after task execution."""

    def setUp(self):
        _reset_test_executions()
        clear_pending_tasks()

    def tearDown(self):
        clear_pending_tasks()

    def test_executed_task_creates_audit_log(self):
        """When a task executes, an audit log entry is created."""
        from django_ai.automation.testing import time_machine

        with time_machine() as tm:
            schedule_task(
                "django_ai.automation.timers.tests.test_task_simple",
                args=["audit_test"],
                delay=timedelta(seconds=1),
            )

            # No audit log yet
            self.assertEqual(TimedTask.objects.count(), 0)

            # Execute the task
            tm.advance(seconds=2)

            # Audit log should exist
            self.assertEqual(TimedTask.objects.count(), 1)
            log = TimedTask.objects.first()
            self.assertEqual(log.task_name, "django_ai.automation.timers.tests.test_task_simple")
            self.assertEqual(log.task_args, ["audit_test"])
            self.assertTrue(log.success)
            self.assertIsNone(log.error_log)

    def test_failed_task_logs_error(self):
        """When a task fails, the error is logged."""
        from django_ai.automation.testing import time_machine

        with time_machine() as tm:
            # Schedule a task that will fail (non-existent function)
            schedule_task(
                "django_ai.automation.timers.tests.nonexistent_function",
                args=[],
                delay=timedelta(seconds=1),
            )

            # Execute the task
            tm.advance(seconds=2)

            # Audit log should show failure
            self.assertEqual(TimedTask.objects.count(), 1)
            log = TimedTask.objects.first()
            self.assertFalse(log.success)
            self.assertIsNotNone(log.error_log)
