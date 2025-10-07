# django_ai/automation/workflows/tests/test_progress_and_history.py

from datetime import timedelta
from django.test import TestCase
from pydantic import BaseModel
from rest_framework import serializers

from ..core import (
    workflow,
    step,
    goto,
    sleep,
    complete,
    fail,
    engine,
    get_context,
    Retry,
)
from ..models import WorkflowRun, WorkflowStatus, StepType
from ..statezero_action import statezero_action


class MockExecutor:
    """Mock executor that queues but doesn't auto-execute"""
    def __init__(self):
        self.queued_tasks = []

    def queue_task(self, task_name, *args, delay=None):
        self.queued_tasks.append({"task_name": task_name, "args": args, "delay": delay})

    def clear(self):
        self.queued_tasks.clear()


class TestProgressHistoryAndStepType(TestCase):
    """Test progress tracking, step execution history, and step type detection"""

    def setUp(self):
        self.executor = MockExecutor()
        engine.set_executor(self.executor)
        from ..core import _workflows
        _workflows.clear()

    def tearDown(self):
        from ..core import _workflows
        _workflows.clear()

    def test_progress_and_step_executions(self):
        """Test that progress updates correctly and step executions are recorded"""

        @workflow("progress_test")
        class ProgressWorkflow:
            class Context(BaseModel):
                pass

            @step(start=True)
            def step_one(self):
                return goto(self.step_two, progress=0.33)

            @step()
            def step_two(self):
                return goto(self.step_three, progress=0.67)

            @step()
            def step_three(self):
                return complete()

        run = engine.start("progress_test")
        self.assertEqual(run.progress, 0.0)
        self.assertEqual(run.step_executions.count(), 0)

        # Execute step one
        engine.execute_step(run.id, "step_one")
        run.refresh_from_db()
        self.assertAlmostEqual(run.progress, 0.33, places=2)
        self.assertEqual(run.step_executions.count(), 1)
        self.assertEqual(run.step_executions.first().step_name, "step_one")
        self.assertEqual(run.step_executions.first().status, "completed")

        # Execute step two
        engine.execute_step(run.id, "step_two")
        run.refresh_from_db()
        self.assertAlmostEqual(run.progress, 0.67, places=2)
        self.assertEqual(run.step_executions.count(), 2)

        # Execute step three - complete should set progress to 1.0
        engine.execute_step(run.id, "step_three")
        run.refresh_from_db()
        self.assertEqual(run.progress, 1.0)
        self.assertEqual(run.step_executions.count(), 3)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)

        # Verify chronological order
        executions = list(run.step_executions.all())
        self.assertEqual([e.step_name for e in executions], ["step_one", "step_two", "step_three"])

    def test_sleep_and_fail_no_execution_on_sleep(self):
        """Test that sleep doesn't create executions but fail does"""

        @workflow("sleep_fail_test", default_retry=Retry(max_attempts=1))
        class SleepFailWorkflow:
            class Context(BaseModel):
                should_fail: bool = False

            @step(start=True)
            def sleep_step(self):
                return goto(self.maybe_fail)

            @step()
            def maybe_fail(self):
                ctx = get_context()
                if ctx.should_fail:
                    return fail("Test failure")
                return sleep(timedelta(hours=1))

        # Test sleep path
        run1 = engine.start("sleep_fail_test", should_fail=False)
        engine.execute_step(run1.id, "sleep_step")
        engine.execute_step(run1.id, "maybe_fail")
        run1.refresh_from_db()
        
        self.assertEqual(run1.status, WorkflowStatus.WAITING)
        self.assertEqual(run1.step_executions.count(), 1)  # Only sleep_step completed
        self.assertEqual(run1.step_executions.first().step_name, "sleep_step")

        # Test fail path
        run2 = engine.start("sleep_fail_test", should_fail=True)
        engine.execute_step(run2.id, "sleep_step")
        engine.execute_step(run2.id, "maybe_fail")
        run2.refresh_from_db()
        
        self.assertEqual(run2.status, WorkflowStatus.FAILED)
        self.assertEqual(run2.step_executions.count(), 2)
        failed_exec = run2.step_executions.last()
        self.assertEqual(failed_exec.step_name, "maybe_fail")
        self.assertEqual(failed_exec.status, "failed")
        self.assertEqual(failed_exec.error, "Test failure")

    def test_current_step_type_property(self):
        """Test that current_step_type correctly identifies action, automated, and waiting steps"""

        class TestSerializer(serializers.Serializer):
            workflow_run_id = serializers.IntegerField()
            data = serializers.CharField()

        @workflow("step_type_test")
        class StepTypeWorkflow:
            class Context(BaseModel):
                data: str = ""

            @step(start=True)
            def automated_step(self):
                return goto(self.action_step)

            @statezero_action(name="test_action", serializer=TestSerializer)
            @step()
            def action_step(self, data: str):
                ctx = get_context()
                ctx.data = data
                return goto(self.automated_step_2)

            @step()
            def automated_step_2(self):
                return complete()

        run = engine.start("step_type_test")
        
        # Initial step is automated
        self.assertEqual(run.current_step, "automated_step")
        self.assertEqual(run.current_step_type, StepType.AUTOMATED)

        # Move to action step
        engine.execute_step(run.id, "automated_step")
        run.refresh_from_db()
        self.assertEqual(run.current_step, "action_step")
        self.assertEqual(run.current_step_type, StepType.ACTION)
        self.assertEqual(run.status, WorkflowStatus.SUSPENDED)

        # Simulate action completion
        from ..core import _workflows, WorkflowContextManager, _current_context
        workflow_cls = _workflows["step_type_test"]
        ctx_manager = WorkflowContextManager(run.id, workflow_cls.Context)
        token = _current_context.set(ctx_manager)
        try:
            workflow_instance = workflow_cls()
            result = workflow_instance.action_step(data="test")
            ctx_manager.commit_changes()
            engine._handle_result(run, result)
        finally:
            _current_context.reset(token)

        run.refresh_from_db()
        self.assertEqual(run.current_step, "automated_step_2")
        self.assertEqual(run.current_step_type, StepType.AUTOMATED)