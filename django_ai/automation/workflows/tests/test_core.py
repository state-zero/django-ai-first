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
    
    WorkflowEngine,
    Retry,
    on_fail,
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
                
                self.context.counter += 1
                return complete(final_count=self.context.counter)

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
                
                self.context.visited.append("first")
                return goto(self.second_step)

            @step()
            def second_step(self):
                
                self.context.visited.append("second")
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
                
                if self.context.slept:
                    return goto(self.after_sleep)
                self.context.slept = True
                return sleep(timedelta(minutes=30))

            @step()
            def after_sleep(self):
                
                self.context.completed = True
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
                
                self.context.signal_received = True
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


class TestOnFailHandler(WorkflowTestCase):
    """Test @on_fail handler functionality"""

    def test_on_fail_called_on_explicit_fail(self):
        """Test that @on_fail handler is called when workflow fails via Fail()"""
        on_fail_called = []

        @workflow("test_on_fail_explicit")
        class OnFailWorkflow:
            class Context(BaseModel):
                cleaned_up: bool = False

            @step(start=True)
            def failing_step(self):
                return fail("Something went wrong")

            @on_fail
            def cleanup(self, run: WorkflowRun):
                on_fail_called.append(run.id)
                
                self.context.cleaned_up = True

        run = engine.start("test_on_fail_explicit")
        engine.execute_step(run.id, "failing_step")
        run.refresh_from_db()

        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertEqual(on_fail_called, [run.id])
        self.assertTrue(run.data["cleaned_up"])

    def test_on_fail_receives_workflow_run_with_step_history(self):
        """Test that @on_fail handler receives WorkflowRun with step execution history"""
        step_history = []

        @workflow("test_on_fail_history")
        class OnFailHistoryWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def first_step(self):
                return goto(self.second_step)

            @step()
            def second_step(self):
                return goto(self.failing_step)

            @step()
            def failing_step(self):
                return fail("Deliberate failure")

            @on_fail
            def cleanup(self, run: WorkflowRun):
                # Capture step history from the run
                completed_steps = list(
                    run.step_executions.filter(status='completed')
                    .values_list('step_name', flat=True)
                )
                step_history.extend(completed_steps)

        run = engine.start("test_on_fail_history")

        # Execute all steps
        engine.execute_step(run.id, "first_step")
        run.refresh_from_db()
        engine.execute_step(run.id, "second_step")
        run.refresh_from_db()
        engine.execute_step(run.id, "failing_step")
        run.refresh_from_db()

        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertIn("first_step", step_history)
        self.assertIn("second_step", step_history)

    def test_on_fail_has_access_to_context(self):
        """Test that @on_fail handler can access and modify context via ()"""

        @workflow("test_on_fail_context")
        class OnFailContextWorkflow:
            class Context(BaseModel):
                value: str = "initial"
                cleanup_value: str = ""

            @step(start=True)
            def update_and_fail(self):
                
                self.context.value = "updated"
                return fail("Failure after update")

            @on_fail
            def cleanup(self, run: WorkflowRun):
                
                self.context.cleanup_value = f"cleaned_{self.context.value}"

        run = engine.start("test_on_fail_context")
        engine.execute_step(run.id, "update_and_fail")
        run.refresh_from_db()

        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertEqual(run.data["value"], "updated")
        self.assertEqual(run.data["cleanup_value"], "cleaned_updated")

    def test_on_fail_called_on_max_retries_exhausted(self):
        """Test that @on_fail handler is called when max retries are exhausted"""
        on_fail_called = []

        @workflow("test_on_fail_retries", default_retry=Retry(max_attempts=1))
        class OnFailRetriesWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def exploding_step(self):
                raise ValueError("Boom!")

            @on_fail
            def cleanup(self, run: WorkflowRun):
                on_fail_called.append(run.id)

        run = engine.start("test_on_fail_retries")
        engine.execute_step(run.id, "exploding_step")
        run.refresh_from_db()

        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertEqual(on_fail_called, [run.id])

    def test_workflow_without_on_fail_handler_works(self):
        """Test that workflows without @on_fail handler still work correctly"""

        @workflow("test_no_on_fail")
        class NoOnFailWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def failing_step(self):
                return fail("Expected failure")

        run = engine.start("test_no_on_fail")
        engine.execute_step(run.id, "failing_step")
        run.refresh_from_db()

        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertEqual(run.error, "Expected failure")

    def test_multiple_on_fail_handlers_raises_error(self):
        """Test that multiple @on_fail handlers raise a validation error"""

        with self.assertRaises(ValueError) as cm:
            @workflow("test_multi_on_fail")
            class MultiOnFailWorkflow:
                class Context(BaseModel):
                    pass

                @step(start=True)
                def start_step(self):
                    return complete()

                @on_fail
                def cleanup1(self, run: WorkflowRun):
                    pass

                @on_fail
                def cleanup2(self, run: WorkflowRun):
                    pass

        self.assertIn("multiple @on_fail handlers", str(cm.exception))

    def test_on_fail_error_does_not_affect_workflow_failure(self):
        """Test that errors in @on_fail handler are logged but don't affect workflow"""
        on_fail_called = []

        @workflow("test_on_fail_error")
        class OnFailErrorWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def failing_step(self):
                return fail("Original failure")

            @on_fail
            def buggy_cleanup(self, run: WorkflowRun):
                on_fail_called.append(True)
                raise RuntimeError("Cleanup exploded!")

        run = engine.start("test_on_fail_error")
        engine.execute_step(run.id, "failing_step")
        run.refresh_from_db()

        # Workflow should still be failed with original error
        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertEqual(run.error, "Original failure")
        # But on_fail was still called
        self.assertEqual(on_fail_called, [True])


if __name__ == "__main__":
    # Simple test runner for development
    import unittest

    unittest.main()
