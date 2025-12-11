from datetime import timedelta
from unittest.mock import Mock
from django.test import TestCase
from django.utils import timezone
from pydantic import BaseModel

from ..core import (
    workflow,
    step,
    goto,
    sleep,
    wait_for_event,
    complete,
    fail,
    engine,
    get_context,
    WorkflowEngine,
    Retry,
)
from ..models import WorkflowRun, WorkflowStatus
from ...testing import time_machine
from ...queues.sync_executor import SynchronousExecutor


class MockExecutor:
    """Simple mock executor for testing"""

    def __init__(self):
        self.queued_tasks = []

    def queue_task(self, task_name, *args, delay=None):
        self.queued_tasks.append({"task_name": task_name, "args": args, "delay": delay})

    def get_last_task(self):
        return self.queued_tasks[-1] if self.queued_tasks else None

    def clear(self):
        self.queued_tasks.clear()


class WorkflowTestCase(TestCase):
    """Base test case with common setup"""

    def setUp(self):
        self.executor = MockExecutor()
        engine.set_executor(self.executor)

    def tearDown(self):
        # Clean up any registered workflows
        from ..core import _workflows, _event_workflows

        _workflows.clear()
        _event_workflows.clear()


class TestBasicWorkflow(WorkflowTestCase):
    """Test basic workflow functionality"""

    def test_simple_workflow_start(self):
        """Test that a simple workflow can be started"""

        @workflow("test_simple")
        class SimpleWorkflow:
            class Context(BaseModel):
                message: str

            @classmethod
            def create_context(cls, message: str):
                return cls.Context(message=message)

            @step(start=True)
            def start_step(self):
                return complete(result="done")

        # Start the workflow
        run = engine.start("test_simple", message="hello")

        # Verify the run was created
        self.assertEqual(run.name, "test_simple")
        self.assertEqual(run.status, WorkflowStatus.RUNNING)
        self.assertEqual(run.current_step, "start_step")
        self.assertEqual(run.data["message"], "hello")

        # Verify the first step was queued
        task = self.executor.get_last_task()
        self.assertEqual(task["task_name"], "execute_step")
        self.assertEqual(task["args"], (run.id, "start_step"))

    def test_workflow_step_execution(self):
        """Test that workflow steps can be executed"""

        @workflow("test_execution")
        class ExecutionWorkflow:
            class Context(BaseModel):
                counter: int = 0
                final_count: int = 0  # Add this field to avoid warning

            @step(start=True)
            def increment(self):
                ctx = get_context()
                ctx.counter += 1
                return complete(final_count=ctx.counter)

        # Start and execute
        run = engine.start("test_execution")
        initial_count = run.data["counter"]

        # Execute the step
        engine.execute_step(run.id, "increment")

        # Verify the workflow completed
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.data["counter"], initial_count + 1)
        self.assertEqual(run.data["final_count"], 1)

    def test_goto_step(self):
        """Test goto functionality"""

        @workflow("test_goto")
        class GotoWorkflow:
            class Context(BaseModel):
                visited: list = []

            @classmethod
            def create_context(cls):
                return cls.Context(visited=[])

            @step(start=True)
            def first_step(self):
                ctx = get_context()
                ctx.visited.append("first")
                return goto(self.second_step)

            @step()
            def second_step(self):
                ctx = get_context()
                ctx.visited.append("second")
                return complete()

        # Start workflow
        run = engine.start("test_goto")

        # Execute first step
        engine.execute_step(run.id, "first_step")
        run.refresh_from_db()

        # Should have moved to second step
        self.assertEqual(run.current_step, "second_step")
        self.assertEqual(run.data["visited"], ["first"])

        # Execute second step
        engine.execute_step(run.id, "second_step")
        run.refresh_from_db()

        # Should be completed
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.data["visited"], ["first", "second"])


class TestWorkflowValidation(WorkflowTestCase):
    """Test workflow validation and error handling"""

    def test_missing_context_class(self):
        """Test that workflows must have a Context class"""

        with self.assertRaises(ValueError) as cm:

            @workflow("bad_workflow")
            class BadWorkflow:
                @classmethod
                def create_context(cls):
                    return {}

        self.assertIn("must define a Context class", str(cm.exception))

    def test_missing_start_step(self):
        """Test that workflows must have exactly one start step"""

        with self.assertRaises(ValueError) as cm:

            @workflow("no_start")
            class NoStartWorkflow:
                class Context(BaseModel):
                    pass

                @classmethod
                def create_context(cls):
                    return cls.Context()

                @step()
                def some_step(self):
                    return complete()

        self.assertIn(
            "must have exactly one step marked with start=True", str(cm.exception)
        )

    def test_multiple_start_steps(self):
        """Test that workflows can't have multiple start steps"""

        with self.assertRaises(ValueError) as cm:

            @workflow("multi_start")
            class MultiStartWorkflow:
                class Context(BaseModel):
                    pass

                @step(start=True)
                def first_start(self):
                    return complete()

                @step(start=True)
                def second_start(self):
                    return complete()

        self.assertIn("multiple start steps", str(cm.exception))

    def test_invalid_goto_target(self):
        """Test that goto validates target steps"""

        @workflow("invalid_goto", default_retry=Retry(max_attempts=1))
        class InvalidGotoWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def start_step(self):
                return goto("nonexistent_step")

        run = engine.start("invalid_goto")

        # This should fail when executed
        engine.execute_step(run.id, "start_step")
        run.refresh_from_db()

        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertIn("not a @step", run.error)


class TestWorkflowStates(WorkflowTestCase):
    """Test different workflow states and transitions"""

    def test_sleep_state(self):
        """Test sleep functionality"""

        @workflow("test_sleep")
        class SleepWorkflow:
            class Context(BaseModel):
                slept: bool = False

            @step(start=True)
            def sleep_step(self):
                return sleep(timedelta(seconds=1))

        run = engine.start("test_sleep")

        # Execute the sleep step
        engine.execute_step(run.id, "sleep_step")
        run.refresh_from_db()

        # Should be in WAITING state (for time) with wake_at set
        self.assertEqual(run.status, WorkflowStatus.WAITING)
        self.assertIsNotNone(run.wake_at)
        self.assertEqual(run.waiting_signal, "")  # No signal for sleep

    def test_sleep_wakes_up_after_duration(self):
        """Test that workflow resumes after sleep duration using time machine"""

        @workflow("test_sleep_wake")
        class SleepWakeWorkflow:
            class Context(BaseModel):
                slept: bool = False
                completed: bool = False

            @step(start=True)
            def sleep_step(self):
                ctx = get_context()
                if ctx.slept:
                    return goto(self.after_sleep)
                ctx.slept = True
                return sleep(timedelta(minutes=30))

            @step()
            def after_sleep(self):
                ctx = get_context()
                ctx.completed = True
                return complete()

        with time_machine() as tm:
            # Use SynchronousExecutor for full integration
            executor = SynchronousExecutor()
            engine.set_executor(executor)

            run = engine.start("test_sleep_wake")

            # Process immediately - workflow should go to sleep
            tm.process()
            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)
            self.assertFalse(run.data.get("completed", False))

            # Advance time by 15 minutes - still sleeping
            tm.advance(minutes=15)
            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.WAITING)

            # Advance another 20 minutes - should wake up and complete
            tm.advance(minutes=20)
            run.refresh_from_db()
            self.assertEqual(run.status, WorkflowStatus.COMPLETED)
            self.assertTrue(run.data["completed"])

    def test_wait_for_event_decorator(self):
        """Test wait for event decorator functionality"""

        @workflow("test_wait_decorator")
        class WaitDecoratorWorkflow:
            class Context(BaseModel):
                signal_received: bool = False

            @step(start=True)
            def start_step(self):
                return goto(self.wait_for_the_event)

            @wait_for_event("test_event")
            @step()
            def wait_for_the_event(self):
                # This step only runs AFTER the event is received
                return goto(self.after_event_step)

            @step()
            def after_event_step(self):
                ctx = get_context()
                ctx.signal_received = True
                return complete()

        run = engine.start("test_wait_decorator")

        # Execute start step, which transitions to the waiting step
        engine.execute_step(run.id, "start_step")
        run.refresh_from_db()

        # Should be suspended waiting for the event
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED)
        self.assertEqual(run.current_step, "wait_for_the_event")
        self.assertEqual(run.waiting_signal, "event:test_event")
        self.assertIsNone(run.wake_at)  # No timeout

        # Send the signal
        engine.signal("event:test_event")

        # Workflow should now be running and the waiting step should be queued
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.RUNNING)
        self.assertEqual(run.waiting_signal, "")
        task = self.executor.get_last_task()
        self.assertEqual(task["args"], (run.id, "wait_for_the_event"))

        # Execute the rest of the workflow
        engine.execute_step(run.id, "wait_for_the_event")
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.RUNNING)
        self.assertEqual(run.current_step, "after_event_step")

        engine.execute_step(run.id, "after_event_step")
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertTrue(run.data["signal_received"])

    def test_workflow_failure(self):
        """Test workflow failure handling"""

        @workflow("test_failure")
        class FailureWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def failing_step(self):
                return fail("Something went wrong")

        run = engine.start("test_failure")

        # Execute the failing step
        engine.execute_step(run.id, "failing_step")
        run.refresh_from_db()

        # Should be failed
        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertEqual(run.error, "Something went wrong")


if __name__ == "__main__":
    # Simple test runner for development
    import unittest

    unittest.main()
